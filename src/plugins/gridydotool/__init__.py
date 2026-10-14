"""Grid mouse control through ydotool, without the GNOME Shell extension.

Routes the grid conversation, computes cell bounds, and delegates rendering to
`_overlay.py` (a GTK4 layer-shell surface in a gi-capable interpreter) while
`ydotool` moves and clicks the pointer at kernel level. The zone math, the
trigger words and the number tables mirror `mousegrid` on purpose: this plugin
shadows the same phrases, so one utterance must land on the same pixel whichever
backend answers.
"""

import logging
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

from easyspeak.core.i18n import translator

logger = logging.getLogger(__name__)

_ = translator(__file__)

NAME = "gridydotool"
DESCRIPTION = "Voice-controlled mouse grid (overlay + kernel-level injection)"
PRIORITY = 0

COMMANDS = [
    _("grid/mouse/pointer/cursor - show mouse grid"),
    _("1-9 or chains like '3 7 5' - zoom to zone(s)"),
    _("click - left click"),
    _("right click / double click / middle click"),
    _("close/cancel - hide the grid"),
    _("again - reopen at the last spot"),
]

# Recognition data copied from mousegrid (asserted equal by the test suite):
# Whisper mishears "grid" as any of these often enough that the extension
# backend tunes a whole set, and a backend that hears fewer words than the one
# it shadows is a regression in the user's eyes, not a simplification.
GRID_TRIGGERS = {
    "grid",
    "grit",
    "grip",
    "great",
    "greed",
    "grade",
    "grad",
    "grill",
    "guild",
    "greet",
    "grin",
    "green",
    "grey",
    "gray",
    "grab",
    "grand",
    "greek",
    "grew",
    "mouse",
    "pointer",
    "cursor",
    "crosshair",
}

WORD_TO_NUM = {
    "one": 1,
    "won": 1,
    "wan": 1,
    "two": 2,
    "to": 2,
    "too": 2,
    "tu": 2,
    "three": 3,
    "tree": 3,
    "free": 3,
    "four": 4,
    "for": 4,
    "fore": 4,
    "five": 5,
    "six": 6,
    "sex": 6,
    "seven": 7,
    "eight": 8,
    "ate": 8,
    "nine": 9,
    "nein": 9,
}

ZONE_MAP = {
    1: (0, 0),
    2: (1, 0),
    3: (2, 0),
    4: (0, 1),
    5: (1, 1),
    6: (2, 1),
    7: (0, 2),
    8: (1, 2),
    9: (2, 2),
}


def subdivide(bounds, zone):
    """Return the ninth of `bounds` named by `zone` (1-9, row-major).

    Integer thirds, exactly as `mousegrid.update_grid` computes them, including
    its truncation: `w // 3` of a 1000-wide cell is 333, not 333.33, and the two
    backends must agree on that. mousegrid's off-monitor safety clamp is absent
    here because it cannot fire: x + w stays inside the monitor by construction.
    """
    x, y, w, h = bounds
    zone_w, zone_h = w // 3, h // 3
    col, row = ZONE_MAP[zone]
    return (x + col * zone_w, y + row * zone_h, zone_w, zone_h)


def center(bounds):
    """Return the (x, y) centre of `bounds`."""
    x, y, w, h = bounds
    return (x + w // 2, y + h // 2)


def parse_number_sequence(text):
    """Extract every zone number from an utterance, in order.

    `"3 7 5"` and `"three seven five"` both give `[3, 7, 5]`; the word table
    carries Whisper's usual mishearings, mirroring mousegrid's parser so the same
    utterance chains the same zooms on either backend.
    """
    lowered = text.lower()
    for word, num in WORD_TO_NUM.items():
        lowered = re.sub(rf"\b{word}\b", str(num), lowered)
    return [int(char) for char in lowered if char.isdigit() and char != "0"]


# The gate's probes run inside setup(), during daemon startup, where a hang is
# worse than a failure: the loader guards setup() with try/except Exception
# (core/main.py), which catches a raise but not a wait.
PROBE_TIMEOUT = 5.0
INJECT_TIMEOUT = 2.0
REAP_GRACE = 2.0
YDTOOL_SOCKET = "/tmp/.ydotool_socket"  # noqa: S108 -- ydotoold's hardcoded path
ERROR_SIGNATURES = ("error:", "Which coordinate", "Unknown tool", "failed to")
FALLBACK_NOTICE = "backend unavailable"

_core = None
_active = False
_bounds = None
_monitor = None
_monitor_origin = (0, 0)
_grid_open = False
_last_bounds = None


def active():
    """Return whether the gate let this plugin claim the grid words."""
    return _active


def _spawn(cmd, timeout):
    """The only place a ydotool process is started; tests patch this."""
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )


def _run_bounded(args, timeout=INJECT_TIMEOUT):
    """Run one ydotool invocation and return its outcome, never its exit code.

    `MouseMove::Exec` and `Click::Exec` both `return argc`, so a successful
    `click 1` exits 2 and a parse error can exit 2 as well: the code carries no
    failure meaning. The three outcomes are what the rest of the module reasons
    about.

    `timeout` bounds the process, which is what keeps a wedged ydotoold from
    freezing the modal loop with the overlay on screen.
    """
    if os.environ.get("EASYSPEAK_GRID_DRY_RUN"):
        logger.info("dry run: %s", " ".join(args))
        return "ran"
    # The tool returns the argc it sees, which is this argv minus its own program
    # name: ["ydotool", "mousemove_relative", "0", "0"] succeeds with 3, and the
    # same probe with "--delay", "0" appended succeeds with 5. Deriving it from
    # the argv we built is what keeps the expectation honest as the argv changes.
    expected = len(args) - 1
    try:
        result = _spawn(args, timeout)
    except subprocess.TimeoutExpired:
        logger.warning("ydotool timed out after %ss: %s", timeout, " ".join(args))
        return "unknown"
    except OSError as exc:
        logger.warning("ydotool could not start: %s", exc)
        return "unknown"
    if any(sig in result.stderr for sig in ERROR_SIGNATURES):
        logger.warning("ydotool refused: %s", result.stderr.strip())
        return "errored"
    if FALLBACK_NOTICE in result.stderr:
        # No daemon: the client built a device for this invocation, and a device
        # that new may not be recognised before the events are emitted.
        logger.warning("ydotool fell back to a per-invocation device")
        return "unknown"
    if result.returncode == expected:
        return "ran"
    if result.returncode < 0:
        logger.warning("ydotool was killed by signal %s", -result.returncode)
        return "unknown"
    return "errored"


def run_ydotool(args):
    """Run an injection, logging whatever it was that happened."""
    outcome = _run_bounded(args)
    if outcome != "ran":
        logger.warning("injection outcome: %s", outcome)
    return outcome


BUTTONS = {"click": 1, "right": 2, "middle": 3}


def move_to(x, y):
    """Move the pointer to a monitor-local point.

    The move is issued in the global space ydotool works in.
    """
    return run_ydotool(
        [
            "ydotool",
            "mousemove",
            str(_monitor_origin[0] + x),
            str(_monitor_origin[1] + y),
            "--delay",
            "0",
        ]
    )


def click(button="click"):
    """Click a button at the current pointer position."""
    return run_ydotool(["ydotool", "click", str(BUTTONS[button]), "--delay", "0"])


def double_click():
    """Two sequential left clicks, with no sleeps between them.

    `--delay 0` is what keeps the pair inside the desktop's double-click window:
    the tool's default 100 ms sleep per invocation would spend most of it.
    """
    first = click("click")
    second = click("click")
    return "ran" if first == second == "ran" else "unknown"


GRID_PYTHON_CANDIDATES = ("python3", "/usr/bin/python3", "/usr/bin/python")
HELPER = str(Path(__file__).with_name("_overlay.py"))
HELPER_PROBE = (
    "import gi; gi.require_version('Gtk', '4.0'); "
    "gi.require_version('Gtk4LayerShell', '1.0'); "
    "from gi.repository import Gtk, Gtk4LayerShell"
)


def grid_python():
    """Return an interpreter that can run the helper, or None.

    Mirrors dictation's AT-SPI probe: the helper needs PyGObject and the
    layer-shell typelib, which the daemon's venv does not have, so each
    candidate is probed with the helper's own import chain. Bounded, because a
    startup hang is invisible in a way an exception is not.
    """
    override = os.environ.get("EASYSPEAK_GRID_PYTHON")
    candidates = (override,) if override else GRID_PYTHON_CANDIDATES
    for candidate in candidates:
        try:
            probe = subprocess.run(
                [candidate, "-c", HELPER_PROBE],
                capture_output=True,
                check=False,
                timeout=PROBE_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return candidate
    return None


def _probe_overlay():
    """True when an interpreter can run the helper and layer-shell is supported.

    The helper answers the layer-shell question in --probe mode.
    """
    python = grid_python()
    if python is None:
        logger.info("grid: no interpreter with PyGObject + Gtk4LayerShell")
        return False
    try:
        probe = subprocess.run(
            [python, HELPER, "--probe"],
            capture_output=True,
            check=False,
            timeout=PROBE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.info("grid: the helper probe did not answer in time")
        return False
    if probe.returncode != 0:
        logger.info("grid: layer-shell unsupported here (%s)", probe.stdout.strip())
        return False
    return True


def _socket_accepts(sock_path=YDTOOL_SOCKET):
    """True when the ydotoold socket takes a connection.

    No injection, no side effect; the daemon holds an already-recognised device,
    which is what makes a successful injection mean anything.
    """
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.settimeout(PROBE_TIMEOUT)
        conn.connect(sock_path)
    except OSError:
        return False
    finally:
        conn.close()
    return True


def _probe_injector():
    """True when ydotool exists, the daemon answers, and a no-op move reports `ran`.

    `mousemove_relative 0 0` is the probe because it is a true no-op whose
    success code (3) is unambiguous: `click 1` succeeds with 2, which collides
    with the move tools' parse-error return.
    """
    if shutil.which("ydotool") is None:
        logger.info("grid: ydotool is not installed")
        return False
    if not _socket_accepts():
        logger.info(
            "grid: no ydotoold on %s (clicks would be lost silently)", YDTOOL_SOCKET
        )
        return False
    outcome = _run_bounded(["ydotool", "mousemove_relative", "0", "0", "--delay", "0"])
    if outcome != "ran":
        logger.info("grid: the injector probe reported %s", outcome)
        return False
    return True


def setup(core):
    """Resolve the gate once, at daemon startup, and remember the core."""
    global _core, _active
    _core = core
    forced = os.environ.get("EASYSPEAK_GRID_BACKEND")
    if forced == "uinput":
        # A deliberate override: it short-circuits the probes, because asking for
        # this backend explicitly is the one case where a probe's answer is not
        # the point (the harness uses it to observe the overlay without a daemon).
        _active = True
        logger.info("grid: forced active by EASYSPEAK_GRID_BACKEND=uinput")
    elif forced == "gnome":
        _active = False
        logger.info("grid: forced inert by EASYSPEAK_GRID_BACKEND=gnome")
    else:
        overlay = _probe_overlay()
        injector = _probe_injector() if overlay else False
        _active = overlay and injector
    logger.info("grid backend %s", "active" if _active else "inert")


DISMISS_WORDS = {"close", "cancel", "stop", "hide", "escape", "exit", "done", "quit"}
AGAIN_WORDS = {"again", "repeat", "reopen"}


def _dismiss():
    """Hide the grid and end whatever session is open."""
    global _grid_open, _bounds
    _close_session()
    _grid_open = False
    _bounds = None


def _open_session(core, bounds=None):
    """Open the overlay, take its geometry, and drive the mode until it ends.

    `bounds` is the cell to reopen at (the "again" path) and defaults to the
    whole output. Returns the mode's exit reason so the caller can decide what
    to say.
    """
    global _monitor, _monitor_origin, _bounds, _grid_open
    geometry = _start_helper()  # Task 4 fills this in
    if geometry is None:
        return None
    _monitor = geometry
    _monitor_origin = (geometry[0], geometry[1])
    _bounds = tuple(bounds) if bounds else (0, 0, geometry[2], geometry[3])
    _redraw(_bounds)  # Task 3 fills this in: without a show the
    # grid is never drawn and the session
    # looks like a dead daemon
    _grid_open = True
    core.speak(_("Grid"))
    try:
        _run_mode(core)
    finally:
        _close_session()
        _grid_open = False
    return "done"


def handle(cmd, core):
    """Claim the grid words when the gate passed, and only then.

    Returns None when inert (so mousegrid keeps the words) or when the utterance
    is not ours; True whenever the phrase was claimed and answered, failures
    included, because the plugin has already spoken by then.
    """
    global _bounds, _last_bounds
    if not _active:
        return None
    words = cmd.lower().strip().split()
    if not words:
        return None

    if any(word in words for word in AGAIN_WORDS) and _last_bounds:
        # Reopen on the remembered cell. It is passed in rather than assigned to
        # the global first: _open_session overwrites the bounds with the whole
        # output otherwise, which is what a fresh open wants and a reopen does not.
        _open_session(core, bounds=_last_bounds)
        return True

    if any(word in words for word in GRID_TRIGGERS) and not (
        DISMISS_WORDS & set(words)
    ):
        _bounds = None
        _open_session(core)
        return True

    if (_grid_open or (GRID_TRIGGERS & set(words))) and (DISMISS_WORDS & set(words)):
        # A phrase that names the grid and asks to close it is a dismissal, never
        # an open — and it is ours even when no session is tracked, so mousegrid
        # is not offered the same words twice.
        _dismiss()
        core.speak(_("Grid closed"))
        return True

    return None


CLICK_VERBS = {
    "double": "double",
    "middle": "middle",
    "right": "right",
}


def _redraw(bounds):
    """Tell the helper to redraw the numbered grid over these bounds."""
    _send({"cmd": "show", "bounds": list(bounds)})


def _run_mode(core):
    """Dispatch one utterance per round until the mode ends.

    Core owns the loop (see listen_modal) so the tray stays live and an
    unattended grid times out instead of holding the microphone for ever.
    """
    global _bounds, _last_bounds
    for spoken in core.listen_modal(
        "grid",
        prompt=(
            "one two three four five six seven eight nine click double "
            "right close cancel"
        ),
        timeout=10,
        idle_timeout=30,
    ):
        text = spoken.lower()
        words = set(text.split())

        if words & DISMISS_WORDS:
            _dismiss()
            core.speak(_("Grid closed"))
            return

        if _click_kind(text, words):
            _fire_click(_click_kind(text, words), core)
            return

        zones = parse_number_sequence(text)
        if zones and _bounds:
            for zone in zones:
                _bounds = subdivide(_bounds, zone)
            _redraw(_bounds)


def _click_kind(_text, words):
    """Return the click flavour named by an utterance, or None."""
    for marker, kind in CLICK_VERBS.items():
        if marker in words:
            return kind
    if "click" in words or "lick" in words or "clique" in words:
        return "click"
    return None


def _fire_click(kind, core):
    """Move to the cell centre, click, remember the cell, and hide the grid.

    The cell is remembered here rather than on every subdivision, mirroring
    mousegrid: "again" means "back to where I last clicked", not "back to the
    last cell I walked past".
    """
    global _last_bounds
    if not _bounds:
        return
    x, y = center(_bounds)
    _last_bounds = _bounds
    outcome = move_to(x, y)
    if outcome == "ran":
        # `ran` is the only positive signal the seam can produce: the move was
        # emitted through an already-recognised device. A move left `unknown`
        # leaves the pointer's position no more proven than a refusal, and a
        # click lands wherever the pointer happens to be — so it is not fired.
        outcome = double_click() if kind == "double" else click(kind)
    _dismiss()
    if outcome == "errored":
        core.speak(_("I couldn't move the pointer."))
    elif outcome == "unknown":
        core.speak(_("I tried, but I'm not sure it worked."))
