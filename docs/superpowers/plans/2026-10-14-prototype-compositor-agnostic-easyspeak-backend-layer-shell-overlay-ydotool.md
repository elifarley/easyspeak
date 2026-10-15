# gridydotool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the `gridydotool` plugin — a compositor-agnostic pointer grid (GTK4 layer-shell overlay + ydotool injection) that activates only where the stack works and leaves `mousegrid` in charge everywhere else.

**Architecture:** All logic (routing, zone math, gating, injection, session state) lives in the plugin module inside the daemon's venv; rendering lives in `_overlay.py`, a GTK4 + gtk4-layer-shell helper spawned in a `gi`-capable interpreter with a `Popen` pipe. The helper reports its monitor's geometry, which is the plugin's only source of screen size, and the plugin injects input through `ydotool` via one bounded seam.

**Tech Stack:** Python 3.10+ (venv), GTK4 + gtk4-layer-shell via PyGObject (system interpreter), `ydotool` 0.1.8 or 1.0.x + `ydotoold` (uinput), pytest / uv / just, headless `sway` + `grim` for the integration harness.

**Spec:** `docs/superpowers/specs/2026-10-14-prototype-compositor-agnostic-easyspeak-backend-layer-shell-overlay-ydotool-design.md` (read it first; this plan implements it and every decision reference below points into it).

## Global Constraints

- `NAME = "gridydotool"`, `PRIORITY = 0` (ahead of `mousegrid`'s 1), and every `handle` returns `None` unless the gate passed.
- Timers, exactly: `PROBE_TIMEOUT = 5.0`, `INJECT_TIMEOUT = 2.0`, `REAP_GRACE = 2.0`.
- Every `ydotool` invocation is built in the dialect the installed client reports: `--delay 0` on 0.1.8, no delay flag at all on 1.0.x. The daemon socket is **dialect-dependent**: 0.1.8 falls back to a per-invocation device (a logged mode — the VM measured it working), while 1.0.x has no fallback, so there the capability gate discovers a real socket and **fails closed** when it cannot — a host that cannot click keeps `mousegrid`'s grid rather than promising one that will not act.
- Env knobs: `EASYSPEAK_GRID_BACKEND=uinput|gnome` (forced override, short-circuits the probes), `EASYSPEAK_GRID_PYTHON` (interpreter override), `EASYSPEAK_GRID_LIVE=move|click` (test-only, harness).
- Zero edits under `src/core/**`. The only existing files touched are `tests/unit/core/test_main.py` (two assertions), `tests/packaging/test_python_wheel.sh`, `pyproject.toml`, the docs and `tests/integration/README.md`.
- `_ = translator(__file__)` at module top, and every spoken string wrapped in `_()`. Catalogs are complete for `de`, `es`, `fr`, `it` — no empty and no fuzzy entries ever committed.
- Bounds are monitor-local; the monitor origin from the helper's `ready` event is added only at injection time.
- Zone math: 3×3 recursion with integer thirds (`w // 3`), no clamp, containment invariant holds.
- `handle` returns `True` whenever the phrase was claimed and answered (including every failure path); only the gate-inert case returns `None`.
- Helper: lazy `import gi` (so the venv can import the module in tests), the library load (`ctypes.util.find_library` for the soname, trusted only when the result names this library, then the literal soname and the plain name) immediately before the gi import, `is_supported()` re-checked in `show`, empty input region, `keyboard_mode` NONE, never `auto_exclusive_zone_enable()`.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/plugins/gridydotool/__init__.py` | Everything the daemon owns: gate, routing, zone math, injection adapter, helper channel, session lifecycle |
| `src/plugins/gridydotool/_overlay.py` | The GTK4 layer-shell renderer + `--probe`; pure cell geometry and the JSON protocol live here too, importable without `gi` |
| `src/plugins/gridydotool/locale/{de,es,fr,it}/LC_MESSAGES/gridydotool.po` | Complete reply + help catalogs |
| `tests/unit/plugins/test_gridydotool.py` | Unit suite: math, parity, gate matrix, routing, adapter, helper channel, lifecycle |
| `tests/integration/test_gridydotool_harness.py` | Headless-sway harness: two gate shapes, two assertion arms |
| `tests/integration/grid_probe_window.py` | Fullscreen probe that reports pointer motion and button events as JSON |
| `pyproject.toml` | `easyspeak.plugins.gridydotool` in `[tool.setuptools] packages` |
| `tests/unit/core/test_main.py` | Roster set + load-order assertions (mechanical plugin-addition edit) |
| `tests/packaging/test_python_wheel.sh` | `has locale/de/LC_MESSAGES/gridydotool.po` |
| `tests/integration/README.md` | Harness entry, prerequisite set, `EASYSPEAK_GRID_LIVE` |
| `docs/reference/plugins.md`, `docs/how-it-works.md`, `docs/commands.md`, `docs/troubleshooting.md`, `docs/compatibility.md`, `docs/usage.md`, `docs/index.md`, `README.md` | Reference entry, tree, verb caveats, requirements, env vars, feature lines |

---

### Task 1: Skeleton, zone math, parser, packaging

**Files:**
- Create: `src/plugins/gridydotool/__init__.py`
- Create: `tests/unit/plugins/test_gridydotool.py`
- Modify: `pyproject.toml` (the `[tool.setuptools] packages` list)

**Interfaces:**
- Consumes: nothing.
- Produces: `NAME`, `DESCRIPTION`, `COMMANDS`, `PRIORITY`, `ZONE_MAP`, `subdivide(bounds, zone) -> (x, y, w, h)`, `center(bounds) -> (x, y)`, `parse_number_sequence(text) -> list[int]`, `GRID_TRIGGERS: set[str]`, `WORD_TO_NUM: dict[str, int]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/plugins/test_gridydotool.py`:

```python
"""Tests for the gridydotool plugin: the zone math, its parity with mousegrid,
and the parser that turns an utterance into zone numbers."""

from easyspeak.plugins import gridydotool, mousegrid


class TestZoneMath:
    def test_subdivide_each_zone(self):
        bounds = (0, 0, 900, 600)
        expected = {
            1: (0, 0, 300, 200),
            2: (300, 0, 300, 200),
            3: (600, 0, 300, 200),
            4: (0, 200, 300, 200),
            5: (300, 200, 300, 200),
            6: (600, 200, 300, 200),
            7: (0, 400, 300, 200),
            8: (300, 400, 300, 200),
            9: (600, 400, 300, 200),
        }
        for zone, want in expected.items():
            assert gridydotool.subdivide(bounds, zone) == want

    def test_subdivide_truncates_like_the_extension_backend(self):
        # 1000 // 3 == 333, and the same recursion must produce the same cell on
        # both backends or the parity invariant is gone.
        assert gridydotool.subdivide((0, 0, 1000, 1000), 9) == (666, 666, 333, 333)

    def test_center(self):
        assert gridydotool.center((1351, 280, 71, 40)) == (1386, 300)


class TestContainmentInvariant:
    def test_bounds_never_leave_the_monitor(self):
        """x + w and y + h stay inside the monitor for every sequence."""
        monitor = (1920, 1080)
        for a in range(1, 10):
            for b in range(1, 10):
                for c in range(1, 10):
                    bounds = (0, 0, monitor[0], monitor[1])
                    for zone in (a, b, c):
                        bounds = gridydotool.subdivide(bounds, zone)
                    x, y, w, h = bounds
                    assert x + w <= monitor[0], (a, b, c)
                    assert y + h <= monitor[1], (a, b, c)


class TestParityWithMousegrid:
    """The gate shadows mousegrid's words, so both must land on the same pixel.

    Driven against mousegrid's own implementation, which is the only
    independent oracle available for this arithmetic.
    """

    @staticmethod
    def _mousegrid_bounds(sequence, monitor, monkeypatch):
        monkeypatch.setattr(mousegrid, "dbus_call", lambda *args: True)
        monkeypatch.setattr(mousegrid, "screen_size", monitor)
        monkeypatch.setattr(mousegrid, "grid_active", True)
        monkeypatch.setattr(mousegrid, "grid_bounds", (0, 0, monitor[0], monitor[1]))
        for zone in sequence:
            mousegrid.update_grid(zone)
        return mousegrid.grid_bounds

    def test_all_sequences_agree(self, monkeypatch):
        monitor = (1920, 1080)
        for a in range(1, 10):
            for b in range(1, 10):
                for c in range(1, 10):
                    theirs = self._mousegrid_bounds((a, b, c), monitor, monkeypatch)
                    bounds = (0, 0, monitor[0], monitor[1])
                    for zone in (a, b, c):
                        bounds = gridydotool.subdivide(bounds, zone)
                    assert bounds == theirs, (a, b, c)
                    assert gridydotool.center(bounds) == mousegrid.get_center()

    def test_trigger_words_equal(self):
        assert gridydotool.GRID_TRIGGERS == mousegrid.GRID_TRIGGERS

    def test_number_words_equal(self):
        assert gridydotool.WORD_TO_NUM == mousegrid.WORD_TO_NUM

    def test_parser_agrees_on_a_sample(self):
        for text in ("3 7 5", "three seven five", "tree ate nein", "one two"):
            assert gridydotool.parse_number_sequence(text) == (
                mousegrid.parse_number_sequence(text)
            )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --extra=unittest pytest tests/unit/plugins/test_gridydotool.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'easyspeak.plugins.gridydotool'`

- [ ] **Step 3: Write the module**

Create `src/plugins/gridydotool/__init__.py` (Task 1 scope only — later tasks append to it):

```python
"""Grid mouse control through ydotool, without the GNOME Shell extension.

Routes the grid conversation, computes cell bounds, and delegates rendering to
`_overlay.py` (a GTK4 layer-shell surface in a gi-capable interpreter) while
`ydotool` moves and clicks the pointer at kernel level. The zone math, the
trigger words and the number tables mirror `mousegrid` on purpose: this plugin
shadows the same phrases, so one utterance must land on the same pixel whichever
backend answers.
"""

import logging
import re

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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --extra=unittest pytest tests/unit/plugins/test_gridydotool.py -v`
Expected: PASS (all zones, the containment property, the 819-sequence parity sweep, the table equality).

- [ ] **Step 5: Register the package for wheels**

In `pyproject.toml`, add to `[tool.setuptools] packages`, keeping the list alphabetical:

```toml
  "easyspeak.plugins.gridydotool",
```

- [ ] **Step 6: Run the whole unit suite to prove nothing else moved**

Run: `uv run --extra=unittest coverage run -m pytest && uvx coverage[toml] report`
Expected: PASS. `test_load_plugins_loads_all_shipped_plugins` still passes because a module without `handle` is not loaded — this package gains `handle` in Task 2, where that test is updated.

- [ ] **Step 7: Commit**

```bash
hug a src/plugins/gridydotool/__init__.py tests/unit/plugins/test_gridydotool.py pyproject.toml
hug c -F - <<'EOF'
feat(gridydotool): zone math, parser and packaging

The arithmetic that makes this backend and the extension backend
interchangeable: integer thirds with mousegrid's truncation, mirrored trigger
words and number tables, and a parity test that drives mousegrid's own
implementation over all 819 one-to-three digit sequences rather than trusting a
copy of the formula.

The clamp mousegrid carries is deliberately absent: it cannot fire, because
x + w stays inside the monitor by construction, and the containment invariant is
asserted as a property instead of guarded by dead code.

No handle yet, so the loader skips the package and the roster assertion stays
valid until the next task.

Co-authored-by: CommandCodeBot <noreply@commandcode.ai>
EOF
```

---

### Task 2: Gate, bounded seam, adapter, routing

**Files:**
- Modify: `src/plugins/gridydotool/__init__.py`
- Modify: `tests/unit/plugins/test_gridydotool.py`
- Modify: `tests/unit/core/test_main.py:278-306` (roster set + load order)
- Create: `src/plugins/gridydotool/locale/{de,es,fr,it}/LC_MESSAGES/gridydotool.po`

**Interfaces:**
- Consumes: `subdivide`, `center`, `parse_number_sequence`, `GRID_TRIGGERS`, `WORD_TO_NUM`, `COMMANDS` (Task 1).
- Produces: `_run_bounded(args, timeout=INJECT_TIMEOUT)` returning the outcome strings `"ran" | "errored" | "unknown"`, `run_ydotool(args)`, `move_to(x, y)`, `click(button)`, `double_click()`, `dialect()`, `injector_socket()`, `ydotool_socket_candidates()`, `noop_argv()`, `active()`, `setup(core)`, `handle(cmd, core)`, `PROBE_TIMEOUT`, `INJECT_TIMEOUT`, `REAP_GRACE`, `YDTOOL_SOCKET`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/plugins/test_gridydotool.py`:

```python
from unittest.mock import Mock

import pytest
from easyspeak.plugins import gridydotool as g

# What Ubuntu 25.10's ydotool 0.1.8 prints for `ydotool help`, read off the VM:
# the five verbs are the whole surface there, and `mousemove_relative` is not
# one of them — that absence is what the portable probe exists for.
UBUNTU_HELP = (
    "Usage: ydotool <cmd> <args>\n"
    "Available commands:\n"
    "  type\n"
    "  recorder\n"
    "  mousemove\n"
    "  key\n"
    "  click\n"
)
UBUNTU_VERBS = {"type", "recorder", "mousemove", "key", "click"}

# What Ubuntu 26.04's ydotool 1.0.4 prints for `ydotool help`, read off that VM:
# the same five verbs plus `debug` and `bakers`, and a trailing sentence *after*
# the list that is not a verb. Only the indented lines are commands.
RESOLUTE_HELP = (
    "Usage: ydotool <cmd> <args>\n"
    "Available commands:\n"
    "  click\n"
    "  mousemove\n"
    "  type\n"
    "  key\n"
    "  debug\n"
    "  bakers\n"
    "Use environment variable YDOTOOL_SOCKET to specify daemon socket.\n"
)
RESOLUTE_VERBS = {"click", "mousemove", "type", "key", "debug", "bakers"}

# The two dialects answer *this* question about themselves differently, which is
# how the seam picks one: 1.0.4's move verb names `--absolute` in its own usage
# line, 0.1.8's (whose move takes the position as positionals) does not.
MODERN_MOVE_HELP = (
    "Usage: mousemove [OPTION]... [-x <xpos> -y <ypos>] [-- <xpos> <ypos>]\n"
    "Move mouse pointer or wheel.\n"
    "\n"
    "Options:\n"
    "  -w, --wheel                Move mouse wheel relatively\n"
    "  -a, --absolute             Use absolute position, not applicable to wheel\n"
    "  -x, --xpos                 X position\n"
    "  -y, --ypos                 Y position\n"
    "  -h, --help                 Display this help and exit\n"
)
LEGACY_MOVE_HELP = (
    "Usage: mousemove [OPTION]... <x> <y>\n"
    "Options:\n"
    "  -h, --help                 Display this help and exit\n"
)


def _help_result(text=UBUNTU_HELP):
    """What `ydotool help` returns on Ubuntu: the list on stderr, exit 1."""
    return Mock(returncode=1, stdout="", stderr=text)


def _modern_help_result():
    """What 1.0.4's `ydotool help` returns: the list on stdout, exit 0."""
    return Mock(returncode=0, stdout=RESOLUTE_HELP, stderr="")


def _modern_client(cmd, timeout):
    """One 1.0.4 binary answering both questions the seam asks of it.

    The tool list and the move verb's own help come from the same client, so a
    stub that answers only one of them would let a test pass on a dialect the
    real binary never reports.
    """
    if cmd[1] == "help":
        return _modern_help_result()
    return Mock(returncode=0, stdout=MODERN_MOVE_HELP, stderr="")


@pytest.fixture(autouse=True)
def _fresh_client_state(monkeypatch):
    """The dialect is cached module state; every test resolves its own."""
    monkeypatch.setattr(g, "_dialect", None)
    monkeypatch.setattr(g, "_monitor_origin", (0, 0))


@pytest.fixture
def core():
    """A core stub with the plugin-facing surface this module touches."""
    stub = Mock()
    stub.speak = Mock()
    stub.listen_modal = Mock(return_value=iter([]))
    return stub


class TestOutcomeClassification:
    """The exit code alone says nothing: Exec returns argc but the 0.1.8 client
    discards it — success is 0, measured on the VM — and the daemonless notice
    is a caveat. The seam returns one of three outcomes and the tests pin all
    three."""

    def _result(self, returncode=3, stderr="", stdout=""):
        return Mock(returncode=returncode, stderr=stderr, stdout=stdout)

    def test_ran_is_the_measured_clients_exit_code(self, monkeypatch):
        monkeypatch.setattr(g, "_spawn", lambda *a, **k: self._result(0))
        assert g._run_bounded(["ydotool", "mousemove", "0", "0"]) == "ran"

    def test_ran_accepts_the_argv_shaped_success_code(self, monkeypatch):
        """A build that forwards Exec's argc return still classifies: the code
        the tool computes is `len(args) - 1`, which this argv makes 3."""
        monkeypatch.setattr(g, "_spawn", lambda *a, **k: self._result(3))
        assert g._run_bounded(["ydotool", "mousemove", "0", "0"]) == "ran"

    def test_errored_on_an_error_signature(self, monkeypatch):
        monkeypatch.setattr(
            g, "_spawn", lambda *a, **k: self._result(1, stderr="Unknown tool: x")
        )
        assert g._run_bounded(["ydotool", "bogus"]) == "errored"

    def test_unknown_on_a_signal_kill(self, monkeypatch):
        monkeypatch.setattr(g, "_spawn", lambda *a, **k: self._result(-9, stderr=""))
        assert g._run_bounded(["ydotool", "click", "1"]) == "unknown"

    def test_unknown_on_a_timeout(self, monkeypatch):
        import subprocess

        def boom(*_a, **_k):
            raise subprocess.TimeoutExpired(cmd="ydotool", timeout=2.0)

        monkeypatch.setattr(g, "_spawn", boom)
        assert g._run_bounded(["ydotool", "click", "1"]) == "unknown"

    def test_unknown_when_the_binary_is_missing(self, monkeypatch):
        monkeypatch.setattr(g, "_spawn", Mock(side_effect=FileNotFoundError("ydotool")))
        assert g._run_bounded(["ydotool", "click", "1"]) == "unknown"

    def test_zero_exit_is_ran_even_with_the_daemonless_notice(
        self, monkeypatch, caplog
    ):
        """The VM measured a dispatched tool exiting 0 with the fallback notice
        on stderr: no daemon is a latency caveat, not a failure, and the log
        names the per-invocation mode."""
        monkeypatch.setattr(
            g,
            "_spawn",
            lambda *a, **k: self._result(
                0, stderr="ydotool: notice: ydotoold backend unavailable ...\n"
            ),
        )
        with caplog.at_level("INFO"):
            assert g._run_bounded(["ydotool", "type", "", "--delay", "0"]) == "ran"
        assert any("per-invocation" in r.message for r in caplog.records)

    def test_a_nonzero_exit_matching_no_success_code_is_errored(self, monkeypatch):
        monkeypatch.setattr(g, "_spawn", lambda *a, **k: self._result(7))
        assert g._run_bounded(["ydotool", "click", "1", "--delay", "0"]) == "errored"

    def test_a_rejected_argv_that_exits_zero_is_errored(self, monkeypatch, caplog):
        """1.0.4's measured answer to 0.1.8's argv: `click 1 --delay 0` prints
        `click: unrecognized option '--delay'` on stderr, emits **nothing**, and
        still exits 0. Reading the exit code alone would call that an emission,
        and the seam's `ran` is the only positive signal the plugin has — so a
        rejected argv is classified as the refusal it is."""
        monkeypatch.setattr(
            g,
            "_spawn",
            lambda *a, **k: self._result(
                0, stderr="click: unrecognized option '--delay'\n"
            ),
        )
        with caplog.at_level("WARNING"):
            assert (
                g._run_bounded(["ydotool", "click", "1", "--delay", "0"]) == "errored"
            )
        assert any("refused" in r.message for r in caplog.records)

    def test_the_measured_modern_move_abort_is_errored(self, monkeypatch):
        """The same argv on `mousemove` does not even exit cleanly: 1.0.4 aborts
        with the option error plus `*** stack smashing detected ***` (134)."""
        monkeypatch.setattr(
            g,
            "_spawn",
            lambda *a, **k: self._result(
                134,
                stderr=(
                    "mousemove: unrecognized option '--delay'\n"
                    "*** stack smashing detected ***: terminated\n"
                ),
            ),
        )
        assert g._run_bounded(["ydotool", "mousemove", "0", "0", "--delay", "0"]) == (
            "errored"
        )

    def test_the_modern_no_daemon_refusal_is_errored(self, monkeypatch):
        """1.0.4 without a daemon: nothing is emitted and the client says so —
        the "failed to" signature this module already treats as a refusal."""
        monkeypatch.setattr(
            g,
            "_spawn",
            lambda *a, **k: self._result(
                2,
                stderr=(
                    "failed to connect socket `/run/user/1000/.ydotool_socket': "
                    "No such file or directory\nPlease check if ydotoold is running.\n"
                ),
            ),
        )
        assert g._run_bounded(["ydotool", "click", "0xC0"]) == "errored"

    def test_dry_run_records_without_spawning(self, monkeypatch):
        spawn = Mock()
        monkeypatch.setattr(g, "_spawn", spawn)
        monkeypatch.setenv("EASYSPEAK_GRID_DRY_RUN", "1")
        assert g._run_bounded(["ydotool", "click", "1"]) == "ran"
        spawn.assert_not_called()


class TestDialect:
    """The argv dialect is read from the binary, never assumed from a version.

    0.1.8 and 1.0.x disagree about every part of the injection argv — delay flag,
    position handling, button spelling — and the wrong one is not a style
    problem: 1.0.4 rejects `--delay` (aborting on `move`), reads positional
    coordinates as *relative*, and emits nothing for a bare button selector.
    """

    def test_the_modern_move_help_selects_the_modern_dialect(self, monkeypatch):
        monkeypatch.setattr(
            g,
            "_spawn",
            lambda cmd, timeout: Mock(returncode=0, stdout=MODERN_MOVE_HELP, stderr=""),
        )
        assert g._probe_dialect() == g.YDTOOL_DIALECT_MODERN

    def test_a_help_without_the_absolute_option_is_the_legacy_dialect(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            g,
            "_spawn",
            lambda cmd, timeout: Mock(returncode=1, stdout=LEGACY_MOVE_HELP, stderr=""),
        )
        assert g._probe_dialect() == g.YDTOOL_DIALECT_LEGACY

    def test_a_help_that_cannot_answer_keeps_the_legacy_dialect(self, monkeypatch):
        """An unreadable help is not evidence of the modern argv: the legacy one
        is what this plugin shipped first, and the functional probe still has to
        report `ran` before the gate opens."""
        import subprocess

        def boom(cmd, timeout):
            raise subprocess.TimeoutExpired(cmd="ydotool", timeout=5.0)

        monkeypatch.setattr(g, "_spawn", boom)
        assert g._probe_dialect() == g.YDTOOL_DIALECT_LEGACY

    def test_a_missing_binary_keeps_the_legacy_dialect(self, monkeypatch):
        monkeypatch.setattr(g, "_spawn", Mock(side_effect=FileNotFoundError("ydotool")))
        assert g._probe_dialect() == g.YDTOOL_DIALECT_LEGACY

    def test_the_dialect_is_resolved_once(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            g,
            "_probe_dialect",
            lambda: calls.append(1) or g.YDTOOL_DIALECT_MODERN,
        )
        assert g.dialect() == g.YDTOOL_DIALECT_MODERN
        assert g.dialect() == g.YDTOOL_DIALECT_MODERN
        assert calls == [1]

    def test_the_noop_is_built_in_the_dialect(self, monkeypatch):
        monkeypatch.setattr(g, "_dialect", g.YDTOOL_DIALECT_LEGACY)
        assert g.noop_argv() == ("ydotool", "type", "", "--delay", "0")
        monkeypatch.setattr(g, "_dialect", g.YDTOOL_DIALECT_MODERN)
        assert g.noop_argv() == ("ydotool", "type", "")


class TestSocketDiscovery:
    """The daemon's socket path is build-dependent and not always the session's.

    1.0.x defaults to `$XDG_RUNTIME_DIR/.ydotool_socket`; 0.1.8 hardcoded
    `/tmp/.ydotool_socket`; the client's own `YDOTOOL_SOCKET` override wins. A
    session that repoints `XDG_RUNTIME_DIR` — a nested compositor, this repo's own
    harness — must still find the daemon that is running for this user, or every
    injection on 1.0.x silently becomes a refusal.
    """

    def test_the_override_wins_and_the_login_session_is_always_considered(
        self, monkeypatch
    ):
        monkeypatch.setenv("YDOTOOL_SOCKET", "/somewhere/else")
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/repointed")
        assert g.ydotool_socket_candidates() == [
            "/somewhere/else",
            "/repointed/.ydotool_socket",
            f"/run/user/{os.getuid()}/.ydotool_socket",
            "/tmp/.ydotool_socket",
        ]

    def test_a_missing_runtime_dir_still_names_the_login_session(self, monkeypatch):
        monkeypatch.delenv("YDOTOOL_SOCKET", raising=False)
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        assert g.ydotool_socket_candidates() == [
            f"/run/user/{os.getuid()}/.ydotool_socket",
            "/tmp/.ydotool_socket",
        ]

    def test_repeated_paths_are_collapsed(self, monkeypatch):
        uid = os.getuid()
        monkeypatch.setenv("YDOTOOL_SOCKET", f"/run/user/{uid}/.ydotool_socket")
        monkeypatch.setenv("XDG_RUNTIME_DIR", f"/run/user/{uid}")
        assert g.ydotool_socket_candidates() == [
            f"/run/user/{uid}/.ydotool_socket",
            "/tmp/.ydotool_socket",
        ]

    def test_the_first_socket_that_answers_is_the_one_used(self, monkeypatch):
        monkeypatch.setattr(
            g, "ydotool_socket_candidates", lambda: ["/dead", "/alive", "/tmp/.x"]
        )
        monkeypatch.setattr(g, "_socket_accepts", lambda path: path == "/alive")
        assert g.injector_socket() == "/alive"

    def test_no_answer_is_none_not_a_guess(self, monkeypatch):
        monkeypatch.setattr(g, "ydotool_socket_candidates", lambda: ["/dead"])
        monkeypatch.setattr(g, "_socket_accepts", lambda path: False)
        assert g.injector_socket() is None


class TestClientEnv:
    """A spawned client is told which socket to use, when one answers."""

    def test_a_found_socket_is_exported_to_the_client(self, monkeypatch):
        monkeypatch.setattr(g, "injector_socket", lambda: "/alive.sock")
        env = g._client_env()
        assert env["YDOTOOL_SOCKET"] == "/alive.sock"
        assert env["PATH"] == os.environ["PATH"]

    def test_with_no_socket_the_environment_is_inherited(self, monkeypatch):
        monkeypatch.setattr(g, "injector_socket", lambda: None)
        assert g._client_env() is None

    def test_the_spawn_is_the_thing_that_carries_it(self, monkeypatch):
        """`_spawn` is the only place a client starts, so it is the only place
        that needs to know: `_run_bounded` and the probe stay unchanged."""
        monkeypatch.setattr(g, "injector_socket", lambda: "/alive.sock")
        seen = {}
        monkeypatch.setattr(
            g.subprocess,
            "run",
            lambda cmd, **kwargs: (
                seen.update(cmd=cmd, **kwargs)
                or Mock(returncode=0, stdout="", stderr="")
            ),
        )
        g._run_bounded(["ydotool", "click", "0xC0"])
        assert seen["env"]["YDOTOOL_SOCKET"] == "/alive.sock"


class TestAdapterArgv:
    def test_move_applies_the_monitor_origin(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            g, "_run_bounded", lambda args, **k: seen.append(args) or "ran"
        )
        monkeypatch.setattr(g, "_dialect", g.YDTOOL_DIALECT_LEGACY)
        g._monitor_origin = (1920, 0)
        g.move_to(1351, 280)
        assert seen == [["ydotool", "mousemove", "3271", "280", "--delay", "0"]]

    def test_click_buttons(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            g, "_run_bounded", lambda args, **k: seen.append(args) or "ran"
        )
        monkeypatch.setattr(g, "_dialect", g.YDTOOL_DIALECT_LEGACY)
        g.click("click")
        g.click("right")
        g.click("middle")
        assert seen == [
            ["ydotool", "click", "1", "--delay", "0"],
            ["ydotool", "click", "2", "--delay", "0"],
            ["ydotool", "click", "3", "--delay", "0"],
        ]

    def test_the_modern_move_is_absolute_and_positional_is_not_used(self, monkeypatch):
        """1.0.x reads `-x`/`-y` as a *relative* delta unless `--absolute` is
        given, so the flag is the difference between "the cell's centre" and
        "somewhere off the current pointer"."""
        seen = []
        monkeypatch.setattr(
            g, "_run_bounded", lambda args, **k: seen.append(args) or "ran"
        )
        monkeypatch.setattr(g, "_dialect", g.YDTOOL_DIALECT_MODERN)
        g._monitor_origin = (1920, 0)
        g.move_to(1351, 280)
        assert seen == [
            ["ydotool", "mousemove", "--absolute", "-x", "3271", "-y", "280"]
        ]

    def test_the_modern_buttons_are_selector_plus_mask(self, monkeypatch):
        """Measured against a GTK window in the VM: `0xC0` arrives as button 1,
        `0xC1` as 3 (right) and `0xC2` as 2 (middle), where 0.1.8's own
        `click 1` emits nothing at all on 1.0.x — it is a bare selector."""
        seen = []
        monkeypatch.setattr(
            g, "_run_bounded", lambda args, **k: seen.append(args) or "ran"
        )
        monkeypatch.setattr(g, "_dialect", g.YDTOOL_DIALECT_MODERN)
        g.click("click")
        g.click("right")
        g.click("middle")
        assert seen == [
            ["ydotool", "click", "0xC0"],
            ["ydotool", "click", "0xC1"],
            ["ydotool", "click", "0xC2"],
        ]

    def test_double_click_is_two_calls_in_order(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            g, "_run_bounded", lambda args, **k: seen.append(args) or "ran"
        )
        monkeypatch.setattr(g, "_dialect", g.YDTOOL_DIALECT_LEGACY)
        g.double_click()
        assert [a[2] for a in seen] == ["1", "1"]

    def test_every_argv_the_seam_builds_uses_a_verb_the_binary_lists(self, monkeypatch):
        """The VM's ydotool offers five verbs and no `mousemove_relative`:
        every argv the adapter and the probe build must come from its list."""
        seen = []
        monkeypatch.setattr(
            g, "_run_bounded", lambda args, **k: seen.append(list(args)) or "ran"
        )
        monkeypatch.setattr(
            g,
            "_spawn",
            lambda cmd, timeout: Mock(returncode=1, stdout="", stderr=UBUNTU_HELP),
        )
        monkeypatch.setattr(g.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(g, "_socket_accepts", lambda path=None: False)
        g._monitor_origin = (0, 0)
        g.move_to(10, 20)
        g.click("right")
        g.double_click()
        assert g._probe_injector() is True
        assert [argv[1] for argv in seen] == [
            "mousemove",
            "click",
            "click",
            "click",
            "type",
        ]
        assert all(argv[1] in UBUNTU_VERBS for argv in seen)

    def test_the_modern_argv_stays_inside_the_modern_verb_list(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            g, "_run_bounded", lambda args, **k: seen.append(list(args)) or "ran"
        )
        monkeypatch.setattr(
            g, "injector_socket", lambda: "/run/user/1000/.ydotool_socket"
        )
        monkeypatch.setattr(g, "_spawn", _modern_client)
        monkeypatch.setattr(g.shutil, "which", lambda name: f"/usr/bin/{name}")
        g.move_to(10, 20)
        g.click("right")
        g.double_click()
        assert g._probe_injector() is True
        assert [argv[1] for argv in seen] == [
            "mousemove",
            "click",
            "click",
            "click",
            "type",
        ]
        assert all(argv[1] in RESOLUTE_VERBS for argv in seen)
        # No `--delay` anywhere: 1.0.x has no such option, and passing it turns
        # the move into an abort and the click into a silently rejected argv.
        assert all("--delay" not in argv for argv in seen)


class TestToolListParsing:
    """Capability is read from the binary's own list, never assumed: Ubuntu's
    ydotool 0.1.8 offers five verbs and no `mousemove_relative`, so a probe
    built on an assumed verb could never pass there."""

    def test_reads_the_five_verbs_the_vm_showed(self):
        assert g.parse_tool_list(UBUNTU_HELP) == UBUNTU_VERBS

    def test_reads_the_seven_verbs_the_2604_vm_shows(self):
        """1.0.4 adds `debug` and `bakers`, and closes its help with a sentence
        *after* the list that is not a command — only the indented lines are."""
        assert g.parse_tool_list(RESOLUTE_HELP) == RESOLUTE_VERBS

    def test_an_answer_without_the_header_yields_nothing(self):
        assert (
            g.parse_tool_list("Run 'ydotool help' if you want a tools list\n") == set()
        )

    def test_only_the_lines_after_the_header_are_verbs(self):
        text = (
            "Usage: ydotool <cmd> <args>\nAvailable commands:\n  mousemove\n\n  click\n"
        )
        assert g.parse_tool_list(text) == {"mousemove", "click"}

    def test_a_trailing_sentence_is_not_a_verb(self):
        text = "Available commands:\n  click\nUse YDOTOOL_SOCKET to pick a daemon.\n"
        assert g.parse_tool_list(text) == {"click"}

    def test_the_probe_reads_the_list_from_stderr(self, monkeypatch):
        monkeypatch.setattr(
            g,
            "_spawn",
            lambda cmd, timeout: Mock(returncode=1, stdout="", stderr=UBUNTU_HELP),
        )
        assert g._probe_tools() == UBUNTU_VERBS


class TestGate:
    def test_inert_at_import(self):
        assert g.active() is False

    def test_forced_backend_is_active_without_probing(self, monkeypatch, core):
        monkeypatch.setenv("EASYSPEAK_GRID_BACKEND", "uinput")
        monkeypatch.setattr(g, "_probe_overlay", Mock(side_effect=AssertionError))
        monkeypatch.setattr(g, "_probe_injector", Mock(side_effect=AssertionError))
        g.setup(core)
        assert g.active() is True

    def test_forced_gnome_is_inert(self, monkeypatch, core):
        monkeypatch.setenv("EASYSPEAK_GRID_BACKEND", "gnome")
        g.setup(core)
        assert g.active() is False

    def test_needs_both_halves(self, monkeypatch, core):
        monkeypatch.delenv("EASYSPEAK_GRID_BACKEND", raising=False)
        monkeypatch.setattr(g, "_probe_overlay", lambda: True)
        monkeypatch.setattr(g, "_probe_injector", lambda: False)
        g.setup(core)
        assert g.active() is False
        monkeypatch.setattr(g, "_probe_overlay", lambda: False)
        monkeypatch.setattr(g, "_probe_injector", lambda: True)
        g.setup(core)
        assert g.active() is False

    @staticmethod
    def _help_result():
        """What `ydotool help` returns on Ubuntu: the list on stderr, exit 1."""
        return Mock(returncode=1, stdout="", stderr=UBUNTU_HELP)

    def test_socket_check_is_a_bare_connect_that_cannot_raise(self, monkeypatch):
        stub = Mock()
        stub.connect.side_effect = OSError("connection refused")
        monkeypatch.setattr(g.socket, "socket", Mock(return_value=stub))
        assert g._socket_accepts() is False
        assert stub.close.call_count == len(g.YDTOOL_SOCKET_TYPES)

    def test_socket_check_reports_the_daemon_mode(self, monkeypatch):
        monkeypatch.setattr(g.socket, "socket", Mock(return_value=Mock()))
        assert g._socket_accepts() is True

    def test_a_wrong_socket_type_is_not_the_same_as_no_daemon(self, monkeypatch):
        """The 26.04 shape, measured in the VM: 1.0.4's ydotoold listens on a
        datagram socket, so a stream connect fails with EPROTOTYPE ("Protocol
        wrong type for socket") — and a probe that stops there reports "no
        daemon" for a daemon that is running and answering clients."""
        asked = []

        def make(kind, stype):
            asked.append(stype)
            stub = Mock()
            if stype == g.socket.SOCK_STREAM:
                stub.connect.side_effect = OSError(91, "Protocol wrong type for socket")
            return stub

        monkeypatch.setattr(g.socket, "socket", make)
        assert g._socket_accepts("/run/user/1000/.ydotool_socket") is True
        assert asked == [g.socket.SOCK_STREAM, g.socket.SOCK_DGRAM]

    def test_a_socket_that_refuses_both_types_is_not_a_daemon(self, monkeypatch):
        asked = []

        def make(kind, stype):
            asked.append(stype)
            stub = Mock()
            stub.connect.side_effect = OSError(111, "Connection refused")
            return stub

        monkeypatch.setattr(g.socket, "socket", make)
        assert g._socket_accepts("/tmp/.ydotool_socket") is False
        assert asked == [g.socket.SOCK_STREAM, g.socket.SOCK_DGRAM]

    def test_injector_probe_is_portable_and_daemonless(self, monkeypatch, caplog):
        """The 0.1.8 shape: Ubuntu's five verbs, no ydotoold, a no-op that reports
        `ran` — there the socket is a logged mode, not a gate condition."""
        monkeypatch.setattr(g.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(g, "_socket_accepts", lambda path=None: False)
        monkeypatch.setattr(g, "_spawn", lambda cmd, timeout: self._help_result())
        seen = []
        monkeypatch.setattr(
            g, "_run_bounded", lambda args, **k: seen.append(list(args)) or "ran"
        )
        with caplog.at_level("INFO"):
            assert g._probe_injector() is True
        assert seen == [["ydotool", "type", "", "--delay", "0"]]
        assert any("per-invocation" in r.message for r in caplog.records)

    def test_injector_probe_logs_the_daemon_mode_when_the_socket_answers(
        self, monkeypatch, caplog
    ):
        monkeypatch.setattr(g.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(
            g, "ydotool_socket_candidates", lambda: ["/run/user/1000/.ydotool_socket"]
        )
        monkeypatch.setattr(g, "_socket_accepts", lambda path=None: True)
        monkeypatch.setattr(g, "_spawn", lambda cmd, timeout: self._help_result())
        monkeypatch.setattr(g, "_run_bounded", lambda args, **k: "ran")
        with caplog.at_level("INFO"):
            assert g._probe_injector() is True
        assert any("use the daemon" in r.message for r in caplog.records)
        assert any(
            "/run/user/1000/.ydotool_socket" in r.message for r in caplog.records
        )

    def test_the_modern_probe_fails_closed_without_a_socket(self, monkeypatch, caplog):
        """1.0.4 has no per-invocation fallback: with no socket it exits 2 and
        emits nothing, so "no daemon" is "no injector" and the words must stay
        with `mousegrid`. The no-op is not even attempted — there is nothing it
        could prove."""
        monkeypatch.setattr(g.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(g, "_socket_accepts", lambda path=None: False)
        monkeypatch.setattr(g, "_spawn", _modern_client)
        monkeypatch.setattr(g, "_run_bounded", Mock(side_effect=AssertionError))
        with caplog.at_level("INFO"):
            assert g._probe_injector() is False
        assert any("no injector here" in r.message for r in caplog.records)

    def test_the_modern_probe_passes_with_a_socket_and_a_clean_noop(
        self, monkeypatch, caplog
    ):
        """The 26.04 shape: 1.0.4's own verb list, a daemon answering, and the
        modern no-op (`type ""`, no `--delay` to be rejected) reporting `ran`."""
        monkeypatch.setattr(g.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(
            g, "ydotool_socket_candidates", lambda: ["/run/user/1000/.ydotool_socket"]
        )
        monkeypatch.setattr(g, "_socket_accepts", lambda path=None: True)
        monkeypatch.setattr(g, "_spawn", _modern_client)
        seen = []
        monkeypatch.setattr(
            g, "_run_bounded", lambda args, **k: seen.append(list(args)) or "ran"
        )
        with caplog.at_level("INFO"):
            assert g._probe_injector() is True
        assert seen == [["ydotool", "type", ""]]
        assert any("use the daemon" in r.message for r in caplog.records)

    def test_injector_probe_fails_closed_without_a_verb_it_builds(self, monkeypatch):
        """Ubuntu's list minus `mousemove`: the gate must not claim the words."""
        text = "Usage: ydotool <cmd> <args>\nAvailable commands:\n  type\n  click\n"
        monkeypatch.setattr(g.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(
            g,
            "_spawn",
            lambda cmd, timeout: Mock(returncode=1, stdout="", stderr=text),
        )
        monkeypatch.setattr(g, "_run_bounded", Mock(side_effect=AssertionError))
        assert g._probe_injector() is False

    def test_injector_probe_fails_closed_when_the_noop_reports_not_ran(
        self, monkeypatch
    ):
        monkeypatch.setattr(g.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(g, "_spawn", lambda cmd, timeout: self._help_result())
        monkeypatch.setattr(g, "_run_bounded", lambda args, **k: "errored")
        assert g._probe_injector() is False

    def test_injector_probe_fails_closed_without_the_binary(self, monkeypatch):
        monkeypatch.setattr(g.shutil, "which", lambda name: None)
        assert g._probe_injector() is False

    def test_a_help_that_does_not_answer_is_no_capability(self, monkeypatch):
        import subprocess

        monkeypatch.setattr(g.shutil, "which", lambda name: f"/usr/bin/{name}")

        def boom(cmd, timeout):
            raise subprocess.TimeoutExpired(cmd="ydotool", timeout=5.0)

        monkeypatch.setattr(g, "_spawn", boom)
        assert g._probe_injector() is False


class TestRouting:
    def test_triggers_open_the_grid(self, monkeypatch, core):
        monkeypatch.setattr(g, "_active", True)
        opened = Mock(return_value=(0, 0, 100, 100))
        monkeypatch.setattr(g, "_open_session", opened)
        assert g.handle("grid", core) is True
        assert g.handle("pointer", core) is True
        assert opened.call_count == 2

    def test_a_word_merely_containing_a_trigger_is_not_ours(self, monkeypatch, core):
        monkeypatch.setattr(g, "_active", True)
        assert g.handle("greatest", core) is None

    def test_close_after_a_trigger_is_a_dismissal(self, monkeypatch, core):
        monkeypatch.setattr(g, "_active", True)
        monkeypatch.setattr(g, "_dismiss", Mock())
        assert g.handle("close the grid", core) is True
        g._dismiss.assert_called_once()

    def test_unknown_input_passes_to_the_next_plugin(self, monkeypatch, core):
        monkeypatch.setattr(g, "_active", True)
        assert g.handle("open firefox", core) is None

    def test_inert_plugin_never_claims_anything(self, core):
        assert g.handle("grid", core) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --extra=unittest pytest tests/unit/plugins/test_gridydotool.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute '_run_bounded'` (and the rest).

- [ ] **Step 3: Write the gate, the seam and the adapter**

Append to `src/plugins/gridydotool/__init__.py`:

```python
import os
import shutil
import socket
import subprocess
from pathlib import Path

# The gate's probes run inside setup(), during daemon startup, where a hang is
# worse than a failure: the loader guards setup() with try/except Exception
# (core/main.py), which catches a raise but not a wait.
PROBE_TIMEOUT = 5.0
INJECT_TIMEOUT = 2.0
REAP_GRACE = 2.0
YDTOOL_SOCKET = "/tmp/.ydotool_socket"  # noqa: S108 -- 0.1.8's hardcoded path
YDTOOL_SOCKET_NAME = ".ydotool_socket"  # 1.0.x defaults to $XDG_RUNTIME_DIR/that
# What the two daemons speak: 0.1.8's ydotoold listens on a stream socket, 1.0.x's
# on a datagram one. Connecting with the wrong type answers EPROTOTYPE rather
# than ECONNREFUSED, which would read as "no daemon" on a host that runs one.
YDTOOL_SOCKET_TYPES = (socket.SOCK_STREAM, socket.SOCK_DGRAM)
# `unrecognized option` is in here because a rejected argv is not an emission:
# 1.0.x's client prints `click: unrecognized option '--delay'` and still exits 0,
# which would otherwise be read as `ran` by a seam that trusts the exit code.
# `mousemove` with the same argv aborts instead (SIGABRT after the same line).
ERROR_SIGNATURES = (
    "error:",
    "Which coordinate",
    "Unknown tool",
    "failed to",
    "unrecognized option",
)
FALLBACK_NOTICE = "backend unavailable"
# The verbs the plugin's argv is allowed to use, read off the binary's own list:
# `mousemove` and `click` are the injections, and `type` is the probe's no-op.
# Ubuntu 25.10's 0.1.8 build offers exactly {type, recorder, mousemove, key,
# click} — no `mousemove_relative` — so a hard-coded verb gates the plugin off
# there; 26.04's 1.0.4 adds debug and bakers to the same five.
YDTOOL_VERBS = ("mousemove", "click", "type")

# The two argv dialects ydotool has shipped, and how each one is recognised.
#
# 0.1.8 (Ubuntu 25.10) reads the position as positional arguments and has its
# own `--delay` flag: `mousemove 100 200 --delay 0`, `click 1` (1 = left, 2 =
# right, 3 = middle). With no daemon answering it builds a device for that one
# invocation and says so on stderr — a measured working path, hence a mode.
#
# 1.0.x (Ubuntu 26.04) dropped `--delay`, made the position explicit options
# (`mousemove --absolute -x 100 -y 200`, where `-x`/`-y` alone are *relative*),
# and spells buttons as hex selectors plus press/release mask bits: `0xC0` is a
# left click, `0xC1` right, `0xC2` middle, and `click 1` — 0.1.8's left click —
# is a right *selector* with no mask, which emits nothing at all. It also
# removed the per-invocation fallback: with no daemon socket the client exits 2
# with "failed to connect socket" and injects nothing, so on that dialect the
# daemon is a capability, not a mode.
#
# The dialect is read from the binary: 1.0.x's own `mousemove --help` names
# `--absolute`, and 0.1.8's offers no such option. Anything else — an unreadable
# help, a build this plugin has never met — keeps the legacy argv, which is the
# argv this plugin shipped first.
YDTOOL_DIALECT_MODERN = "modern"
YDTOOL_DIALECT_LEGACY = "legacy"
YDTOOL_ABSOLUTE_FLAG = "--absolute"
YDTOOL_NOOP_LEGACY = ("ydotool", "type", "", "--delay", "0")
YDTOOL_NOOP_MODERN = ("ydotool", "type", "")

_core = None
_active = False
_bounds = None
_monitor = None
_monitor_origin = (0, 0)
_grid_open = False
_last_bounds = None
_dialect = None


def active():
    """Return whether the gate let this plugin claim the grid words."""
    return _active


def _probe_dialect():
    """Read the installed client's argv dialect from the client itself.

    `ydotool mousemove --help` is the binary's own answer about the move verb:
    1.0.x's usage line names `--absolute`, 0.1.8's has no such option. A build
    that cannot answer at all keeps the legacy argv rather than guessing at the
    modern one — the legacy argv is what this plugin shipped first, and the
    functional half of the gate still has to report `ran` before the gate opens.
    """
    try:
        result = _spawn(["ydotool", "mousemove", "--help"], PROBE_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.info("grid: `ydotool mousemove --help` did not answer: %s", exc)
        return YDTOOL_DIALECT_LEGACY
    if YDTOOL_ABSOLUTE_FLAG in result.stdout + result.stderr:
        return YDTOOL_DIALECT_MODERN
    return YDTOOL_DIALECT_LEGACY


def dialect():
    """Return the installed client's argv dialect, resolved once, on demand.

    On demand rather than in `setup()` because the forced-backend path
    (`EASYSPEAK_GRID_BACKEND=uinput`) short-circuits the probes, and the argv the
    seam builds still has to match the client that will run it.
    """
    global _dialect
    if _dialect is None:
        _dialect = _probe_dialect()
    return _dialect


def ydotool_socket_candidates():
    """Return the paths a ydotool client may find its daemon on, best first.

    `YDOTOOL_SOCKET` is the client's own override and wins outright. Past that,
    the daemon's default path is build-dependent (`$XDG_RUNTIME_DIR/.ydotool_socket`
    for 1.0.x, `/tmp/.ydotool_socket` for 0.1.8) and `XDG_RUNTIME_DIR` is not
    always the login session's: the grid runs inside whatever session started the
    daemon, and a nested compositor or a test harness repoints it. The login
    session's own `/run/user/<uid>` path is therefore always considered, with the
    old hardcoded path last so a 0.1.8 daemon is still found.
    """
    candidates = []
    override = os.environ.get("YDOTOOL_SOCKET")
    if override:
        candidates.append(override)
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        candidates.append(str(Path(runtime) / YDTOOL_SOCKET_NAME))
    candidates.append(f"/run/user/{os.getuid()}/{YDTOOL_SOCKET_NAME}")
    candidates.append(YDTOOL_SOCKET)
    return list(dict.fromkeys(candidates))


def injector_socket():
    """Return the daemon socket that answers, or None when none does.

    Asked afresh before each use rather than cached: a daemon that stopped is not
    an injector any more, and one that started is, and the answer is what decides
    both the gate's mode line and whether the gate opens at all on a client that
    cannot inject without one.
    """
    for candidate in ydotool_socket_candidates():
        if _socket_accepts(candidate):
            return candidate
    return None


def _client_env():
    """The environment a ydotool client needs to reach the injector it must use.

    `None` inherits this process's environment, which is right while no socket
    answers (0.1.8 then builds its own device, and 1.0.x refuses on its own). When
    one does answer, the path is exported explicitly, because the client's own
    discovery is relative to `XDG_RUNTIME_DIR` and this process may be looking at
    a different one than the daemon's owner.
    """
    path = injector_socket()
    if path is None:
        return None
    return {**os.environ, "YDOTOOL_SOCKET": path}


def _spawn(cmd, timeout):
    """The only place a ydotool process is started; tests patch this."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=_client_env(),
    )


def _run_bounded(args, timeout=INJECT_TIMEOUT):
    """Run one ydotool invocation and return its outcome, never its exit code.

    `MouseMove::Exec` and `Click::Exec` do `return argc`, but the 0.1.8 client
    discards that value in `main()`: a dispatched tool exits **0** whatever its
    argv length (measured on Ubuntu 25.10 — `ydotool click 1` exits 0). The seam
    therefore reads a zero exit with a clean stderr as `ran`, and still accepts
    the argv-shaped code for a build that forwards Exec's return. The three
    outcomes are what the rest of the module reasons about.

    "Clean stderr" is what makes that reading safe, which is why a rejected argv
    is an error signature: 1.0.4 answers `click: unrecognized option '--delay'`
    on stderr, emits nothing, and exits 0, so a seam reading only the code would
    call a no-op `ran` — and the same argv on `mousemove` dies with SIGABRT (134)
    instead. `--delay 0` was 0.1.8's flag; see the dialect block above.

    `timeout` bounds the process, which is what keeps a wedged ydotool (or
    ydotoold) from freezing the modal loop with the overlay on screen.
    """
    if os.environ.get("EASYSPEAK_GRID_DRY_RUN"):
        logger.info("dry run: %s", " ".join(args))
        return "ran"
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
        # 0.1.8 with no daemon: the client built a device for this invocation and
        # said so. The VM measured that path working — the notice names a latency
        # caveat, not a capability gap — so the invocation still counts, and this
        # line states which mode was in use. 1.0.x has no such mode, and says so
        # with "failed to connect socket" instead, which is an error signature.
        logger.info("grid: per-invocation device (ydotoold unavailable)")
    # The argv-shaped code (`["ydotool", "mousemove", "0", "0"]` → 3) is what
    # Exec returns and what a build forwarding it would report; the 0.1.8
    # client discards it, so the zero is the measured success code.
    if result.returncode == 0 or result.returncode == len(args) - 1:
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
# 1.0.x button codes: the selector nibble picks the button (0x00 left, 0x01
# right, 0x02 middle) and 0xC0 is both mask bits — press and release — where a
# bare selector emits nothing at all. Measured in the VM against a GTK window:
# 0xC0 arrives as button 1, 0xC1 as button 3, 0xC2 as button 2, and 0x01/1 as
# no event whatsoever.
MODERN_BUTTONS = {"click": "0xC0", "right": "0xC1", "middle": "0xC2"}


def move_to(x, y):
    """Move the pointer to a monitor-local point.

    The move is issued in the global space ydotool works in, in whichever argv
    the installed client understands: positional with `--delay 0` on 0.1.8,
    `--absolute -x/-y` on 1.0.x, where bare `-x`/`-y` would move *relatively*
    from wherever the pointer happens to be.
    """
    global_x, global_y = _monitor_origin[0] + x, _monitor_origin[1] + y
    if dialect() == YDTOOL_DIALECT_MODERN:
        return run_ydotool(
            [
                "ydotool",
                "mousemove",
                YDTOOL_ABSOLUTE_FLAG,
                "-x",
                str(global_x),
                "-y",
                str(global_y),
            ]
        )
    return run_ydotool(
        ["ydotool", "mousemove", str(global_x), str(global_y), "--delay", "0"]
    )


def click(button="click"):
    """Click a button at the current pointer position."""
    if dialect() == YDTOOL_DIALECT_MODERN:
        return run_ydotool(["ydotool", "click", MODERN_BUTTONS[button]])
    return run_ydotool(["ydotool", "click", str(BUTTONS[button]), "--delay", "0"])


def double_click():
    """Two sequential left clicks, with no sleeps between them.

    Each click is one invocation, as on 0.1.8: the pair must stay inside the
    desktop's double-click window, and `--delay 0` — or 1.0.x's argv, which has
    no delay to spend — is what keeps it there.
    """
    first = click("click")
    second = click("click")
    return "ran" if first == second == "ran" else "unknown"
```

- [ ] **Step 4: Write the probes and the gate**

Append:

```python
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
    """True when the ydotoold socket at `sock_path` takes a connection.

    Both socket types are tried, in this order: 1.0.x's daemon speaks
    `SOCK_DGRAM` while 0.1.8's speaks `SOCK_STREAM`, and connecting with the
    other type fails with `EPROTOTYPE` ("Protocol wrong type for socket") — a
    reading that would say "no daemon" on a box where one is running. On 1.0.x
    that reading decides whether the gate opens at all, so it has to be a real
    one. A stale path cannot hide behind the datagram half: the daemon is only
    trusted once the functional probe has also reported `ran` through it.

    No injection, no side effect. The daemon holds an already-recognised device,
    so injections through it carry no per-invocation latency caveat — which is
    the whole difference on 0.1.8, and the only working path on 1.0.x.
    """
    for sock_type in YDTOOL_SOCKET_TYPES:
        conn = socket.socket(socket.AF_UNIX, sock_type)
        try:
            conn.settimeout(PROBE_TIMEOUT)
            conn.connect(sock_path)
        except OSError:
            continue
        finally:
            conn.close()
        return True
    return False


def parse_tool_list(text):
    """Return the verbs named by a `ydotool help` output.

    The list is the indented lines after the "Available commands:" header, one
    verb per line; the usage line before it is not a verb, and neither is the
    trailing sentence 1.0.4 prints after the list ("Use environment variable
    YDOTOOL_SOCKET ..."). Capability is read from this list because the verb set
    is build-dependent: Ubuntu 25.10's ydotool 0.1.8 offers `type`, `recorder`,
    `mousemove`, `key` and `click` — and no `mousemove_relative` at all.
    """
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if "Available commands:" in line:
            start = index
            break
    if start is None:
        return set()
    return {
        line.strip()
        for line in lines[start + 1 :]
        if line.strip() and line[:1] in (" ", "\t")
    }


def _probe_tools():
    """Return the verbs `ydotool help` offers, or None when it cannot answer.

    The help text goes to stderr and the process exits 1 by design (ShowHelp
    then exit(1)), so this reads the output rather than the code; both streams
    are consulted so a build that moves the list to stdout still parses.
    """
    try:
        result = _spawn(["ydotool", "help"], PROBE_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.info("grid: `ydotool help` did not answer: %s", exc)
        return None
    return parse_tool_list(result.stdout + result.stderr)


def _probe_injector():
    """True when ydotool offers the plugin's verbs and a no-op reports `ran`.

    Capability is read from the binary's own tool list, never assumed: Ubuntu
    25.10's ydotool 0.1.8 has no `mousemove_relative`, so a probe built on that
    verb could never pass there. The functional half is `type ""` — the only
    side-effect-free verb of the five, where `mousemove 0 0` would move the
    pointer and `click 0` is a real left click — built in the dialect the same
    binary reported for itself, so what the probe exercises is exactly the argv
    the seam will later emit.

    The daemon decides on 1.0.x what it only described on 0.1.8. 0.1.8 falls back
    to a per-invocation uinput device and calls that a latency caveat, so there
    the socket is the mode this line logs, not a condition. 1.0.x removed the
    fallback: with no socket it exits 2 with "failed to connect socket" and emits
    nothing at all, so on that dialect "no daemon answering" means "no injector",
    and the probe fails closed — the words go back to `mousegrid` rather than a
    grid that cannot click. Every other half still fails closed the same way: no
    binary, no verbs, or a probe that does not report `ran`.
    """
    if shutil.which("ydotool") is None:
        logger.info("grid: ydotool is not installed")
        return False
    tools = _probe_tools()
    if tools is None:
        return False
    missing = [verb for verb in YDTOOL_VERBS if verb not in tools]
    if missing:
        logger.info(
            "grid: ydotool offers no %s (has: %s)",
            ", ".join(missing),
            ", ".join(sorted(tools)) or "nothing",
        )
        return False
    path = injector_socket()
    if path is not None:
        logger.info("grid: ydotoold answers on %s; injections use the daemon", path)
    elif dialect() == YDTOOL_DIALECT_MODERN:
        logger.info(
            "grid: no ydotoold on any of %s and this ydotool (%s) builds no device "
            "without one, so there is no injector here",
            ", ".join(ydotool_socket_candidates()),
            YDTOOL_DIALECT_MODERN,
        )
        return False
    else:
        logger.info(
            "grid: no ydotoold on %s; injections build a per-invocation device "
            "(latency caveat, not a capability gap)",
            YDTOOL_SOCKET,
        )
    outcome = _run_bounded(noop_argv())
    if outcome != "ran":
        logger.info("grid: the injector probe reported %s", outcome)
        return False
    return True


def noop_argv():
    """Return the probe's no-op invocation in the installed client's dialect.

    `type ""` types nothing, which is why it is the probe's verb; 0.1.8 spells
    its delay flag out on the argv and 1.0.x has no such flag at all.
    """
    if dialect() == YDTOOL_DIALECT_MODERN:
        return YDTOOL_NOOP_MODERN
    return YDTOOL_NOOP_LEGACY


def setup(core):
    """Resolve the gate once, at daemon startup, and remember the core."""
    global _core, _active
    _core = core
    forced = os.environ.get("EASYSPEAK_GRID_BACKEND")
    if forced == "uinput":
        # A deliberate override: it short-circuits the probes, because asking for
        # this backend explicitly is the one case where a probe's answer is not
        # the point (the harness uses it to observe the overlay without a daemon
        # and without a real injector).
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
```

- [ ] **Step 5: Write routing and the session entry points**

Append:

```python
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
```

- [ ] **Step 6: Update the two core assertions**

Both these tests call `load_plugins()`, which calls each plugin's `setup()` for
real, so they get the probe patch as well. Without it a unit run spawns
interpreters and the helper, and on a configured box injects a no-op move through
the kernel; the gate's probes belong to the plugin's own matrix, which exercises
them directly.

In `tests/unit/core/test_main.py`, import the module and patch the overlay probe
before loading (add `monkeypatch` to each test's signature):

```python
from easyspeak.plugins import gridydotool
...
        # setup() runs for real here: the patch keeps a loader test from spawning
        # probes and injecting anything.
        monkeypatch.setattr(gridydotool, "_probe_overlay", lambda: False)
        easy.load_plugins()
```

Then add `"gridydotool"` to the set in
`test_load_plugins_loads_all_shipped_plugins`, and change the ordering assertion in
`test_load_plugins_routes_by_priority` to:

```python
        # The grid backend loads ahead of mousegrid: it shadows the same words,
        # and the loader's order is what decides who is offered them first.
        assert names[:3] == ["gridydotool", "headtrack", "mousegrid"]
        assert names[-1] == "base"
        assert names[3:-1] == sorted(names[3:-1])
```

- [ ] **Step 7: Run the tests**

Run: `uv run --extra=unittest pytest tests/unit/plugins/test_gridydotool.py tests/unit/core/test_main.py -v`
Expected: PASS.

- [ ] **Step 8: Add the two reply strings to the catalogs**

Run: `just translations de && just translations es && just translations fr && just translations it`

Then fill the new entries (`Grid`, `Grid closed`) in each of the four files by
copying mousegrid's existing translations for the same msgids — they are already
translated there (`src/plugins/mousegrid/locale/<lang>/LC_MESSAGES/mousegrid.po`).

- [ ] **Step 9: Verify the catalogs are complete and in step**

Run: `uv run --extra=unittest pytest tests/unit/core/test_i18n.py -v && just check-translations`
Expected: PASS, no diff.

- [ ] **Step 10: Commit**

```bash
hug a src/plugins/gridydotool tests/unit/plugins/test_gridydotool.py tests/unit/core/test_main.py
hug c -F - <<'EOF'
feat(gridydotool): gate, bounded seam, adapter and routing

The gate resolves once at startup and needs both halves: an interpreter that can
run the helper, a compositor whose layer-shell the helper confirms, ydotool, a
daemon socket that accepts a connection, and a no-op probe that reports ran. The
socket is required because without the daemon the client builds a device per
invocation and a device that new may not be recognised before the events are
emitted, which is a click that exits cleanly and lands nowhere.

Every subprocess goes through one seam that returns ran, errored or unknown
instead of an exit code: ydotool returns argc, so a successful click exits 2 and
a parse error can too. A fallback notice classifies as unknown rather than ran,
for the same reason the socket is gated. Every invocation passes --delay 0, since
the daemon already holds a recognised device and the delay would only widen the
double-click pair.

Routing mirrors mousegrid's word-boundary matching, and handle returns None only
when inert: once the phrase is claimed and answered, failures included, it
returns True so the other backend does not answer the same word twice.

Co-authored-by: CommandCodeBot <noreply@commandcode.ai>
EOF
```

---

### Task 3: The click path and its outcomes

**Files:**
- Modify: `src/plugins/gridydotool/__init__.py`
- Modify: `tests/unit/plugins/test_gridydotool.py`
- Modify: `src/plugins/gridydotool/locale/{de,es,fr,it}/LC_MESSAGES/gridydotool.po`

**Interfaces:**
- Consumes: `move_to`, `click`, `double_click`, `_bounds`, `_last_bounds`, `_dismiss`, `_close_session`.
- Produces: `_run_mode(core)`, `CLICK_VERBS`, `_click_kind(_text, words)`, `_fire_click(kind, core)`, `_redraw(bounds)`, `dismiss` behaviour, the last-cell bookkeeping that `again` reads.

- [ ] **Step 1: Write the failing tests**

```python
class TestModeDispatch:
    def test_digits_subdivide_and_redraw(self, monkeypatch, core):
        calls = []
        monkeypatch.setattr(g, "_redraw", calls.append)
        g._bounds = (0, 0, 900, 600)
        core.listen_modal = Mock(return_value=iter(["3 7 5"]))
        g._run_mode(core)
        # 3 -> (600, 0, 300, 200); 7 -> (600, 132, 100, 66); 5 -> (633, 154, 33, 22)
        assert calls[-1] == (633, 154, 33, 22)

    def test_click_moves_then_clicks_then_ends(self, monkeypatch, core):
        seen = []
        # The fakes answer in the seam's own vocabulary — a move and a click
        # that both `ran` — which is the scenario this test exercises.
        monkeypatch.setattr(
            g, "move_to", lambda x, y: seen.append(("move", x, y)) or "ran"
        )
        monkeypatch.setattr(
            g, "click", lambda button="click": seen.append(("click", button)) or "ran"
        )
        monkeypatch.setattr(g, "_dismiss", lambda: seen.append(("dismiss",)))
        g._bounds = (0, 0, 900, 600)
        core.listen_modal = Mock(return_value=iter(["click"]))
        g._run_mode(core)
        # Centre of the whole output: (450, 300), and the cell is remembered.
        assert seen[-3:] == [("move", 450, 300), ("click", "click"), ("dismiss",)]
        assert g._last_bounds == (0, 0, 900, 600)

    def test_close_ends_the_mode_without_clicking(self, monkeypatch, core):
        monkeypatch.setattr(g, "move_to", Mock())
        monkeypatch.setattr(g, "_dismiss", Mock())
        core.listen_modal = Mock(return_value=iter(["close"]))
        g._run_mode(core)
        g.move_to.assert_not_called()
        g._dismiss.assert_called_once()

    def test_unrecognised_utterance_keeps_the_mode(self, monkeypatch, core):
        monkeypatch.setattr(g, "move_to", Mock())
        # `_dismiss` is stubbed as in the two tests above: its real body calls
        # `_close_session`, which is Task 4's seam and undefined at this commit.
        monkeypatch.setattr(g, "_dismiss", Mock())
        core.listen_modal = Mock(return_value=iter(["open firefox", "close"]))
        g._run_mode(core)

    def test_unknown_outcome_speaks_the_uncertain_line(self, monkeypatch, core):
        monkeypatch.setattr(g, "move_to", lambda x, y: "unknown")
        monkeypatch.setattr(g, "click", lambda button="click": "unknown")
        monkeypatch.setattr(g, "_dismiss", Mock())
        g._bounds = (0, 0, 900, 600)
        core.listen_modal = Mock(return_value=iter(["click"]))
        g._run_mode(core)
        assert (
            core.speak.call_args_list[-1].args[0]
            == "I tried, but I'm not sure it worked."
        )

    def test_errored_outcome_speaks_the_definite_line(self, monkeypatch, core):
        monkeypatch.setattr(g, "move_to", lambda x, y: "errored")
        monkeypatch.setattr(g, "click", lambda button="click": "errored")
        monkeypatch.setattr(g, "_dismiss", Mock())
        g._bounds = (0, 0, 900, 600)
        core.listen_modal = Mock(return_value=iter(["click"]))
        g._run_mode(core)
        assert core.speak.call_args_list[-1].args[0] == "I couldn't move the pointer."

    def test_unknown_move_never_clicks(self, monkeypatch, core):
        """An unproven move must not click: a click is not idempotent and lands
        at whatever position the pointer happened to be left at."""
        monkeypatch.setattr(g, "move_to", Mock(return_value="unknown"))
        monkeypatch.setattr(g, "click", Mock(return_value="ran"))
        monkeypatch.setattr(g, "_dismiss", Mock())
        g._bounds = (0, 0, 900, 600)
        core.listen_modal = Mock(return_value=iter(["click"]))
        g._run_mode(core)
        g.click.assert_not_called()
        assert (
            core.speak.call_args_list[-1].args[0]
            == "I tried, but I'm not sure it worked."
        )

    def test_click_outcome_replaces_a_successful_move_outcome(self, monkeypatch, core):
        """The move ran, so the click's own outcome is what the user hears."""
        monkeypatch.setattr(g, "move_to", Mock(return_value="ran"))
        monkeypatch.setattr(g, "click", Mock(return_value="unknown"))
        monkeypatch.setattr(g, "_dismiss", Mock())
        g._bounds = (0, 0, 900, 600)
        core.listen_modal = Mock(return_value=iter(["click"]))
        g._run_mode(core)
        g.click.assert_called_once_with("click")
        assert (
            core.speak.call_args_list[-1].args[0]
            == "I tried, but I'm not sure it worked."
        )

    def test_right_click_fires_the_right_button(self, monkeypatch, core):
        seen = []
        monkeypatch.setattr(
            g, "move_to", lambda x, y: seen.append(("move", x, y)) or "ran"
        )
        monkeypatch.setattr(
            g, "click", lambda button="click": seen.append(("click", button)) or "ran"
        )
        monkeypatch.setattr(g, "_dismiss", lambda: seen.append(("dismiss",)))
        g._bounds = (0, 0, 900, 600)
        core.listen_modal = Mock(return_value=iter(["right click"]))
        g._run_mode(core)
        assert seen == [("move", 450, 300), ("click", "right"), ("dismiss",)]

    def test_double_click_utterance_calls_double_click(self, monkeypatch, core):
        seen = []
        monkeypatch.setattr(
            g, "move_to", lambda x, y: seen.append(("move", x, y)) or "ran"
        )
        monkeypatch.setattr(
            g, "double_click", lambda: seen.append(("double_click",)) or "ran"
        )
        monkeypatch.setattr(g, "_dismiss", lambda: seen.append(("dismiss",)))
        g._bounds = (0, 0, 900, 600)
        core.listen_modal = Mock(return_value=iter(["double click"]))
        g._run_mode(core)
        assert seen == [("move", 450, 300), ("double_click",), ("dismiss",)]

    def test_click_without_bounds_does_nothing(self, monkeypatch, core):
        monkeypatch.setattr(g, "move_to", Mock())
        monkeypatch.setattr(g, "click", Mock())
        monkeypatch.setattr(g, "_dismiss", Mock())
        g._bounds = None
        g._fire_click("click", core)
        g.move_to.assert_not_called()
        g.click.assert_not_called()
        g._dismiss.assert_not_called()


class TestClickKind:
    def test_each_utterance_maps_to_its_flavour(self):
        flavours = {
            "click": "click",
            "lick": "click",
            "clique": "click",
            "double click": "double",
            "middle click": "middle",
            "right click": "right",
            "open firefox": None,
        }
        for text, want in flavours.items():
            assert g._click_kind(text, set(text.split())) == want, text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --extra=unittest pytest tests/unit/plugins/test_gridydotool.py -k Mode -v`
Expected: FAIL — `AttributeError: ... has no attribute '_run_mode'`

- [ ] **Step 3: Implement the mode**

Append:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --extra=unittest pytest tests/unit/plugins/test_gridydotool.py -v`
Expected: PASS.

- [ ] **Step 5: Translate the two new strings**

Run: `just translations de && just translations es && just translations fr && just translations it`

Then fill `I couldn't move the pointer.` (copy mousegrid's translation of its own
pointer-failure string where the wording matches, otherwise translate) and
`I tried, but I'm not sure it worked.` with:

| language | msgstr |
|---|---|
| de | `Ich habe es versucht, aber ich bin nicht sicher, ob es geklappt hat.` |
| es | `Lo intenté, pero no estoy seguro de que haya funcionado.` |
| fr | `J'ai essayé, mais je ne suis pas sûr que cela ait fonctionné.` |
| it | `Ci ho provato, ma non sono sicuro che abbia funzionato.` |

- [ ] **Step 6: Verify catalogs and commit**

Run: `uv run --extra=unittest pytest tests/unit/core/test_i18n.py -v && just check-translations`
Expected: PASS.

```bash
hug a src/plugins/gridydotool tests/unit/plugins/test_gridydotool.py
hug c -F - <<'EOF'
feat(gridydotool): the click path and what each outcome says

A click is a move followed by a click, both through the bounded seam, and the
outcome decides what the user hears: a definite failure when the tool refused,
and a distinct line when the outcome is unknown, because a timeout leaves it
genuinely unknown and claiming "it did not work" would invite a retry that may be
a second click.

The cell is remembered on click only, mirroring mousegrid, so "again" means back
to where the user last clicked rather than back to the last cell walked past.

Co-authored-by: CommandCodeBot <noreply@commandcode.ai>
EOF
```

---

### Task 4: The helper channel

**Files:**
- Modify: `src/plugins/gridydotool/__init__.py`
- Modify: `tests/unit/plugins/test_gridydotool.py`
- Modify: `src/plugins/gridydotool/locale/{de,es,fr,it}/LC_MESSAGES/gridydotool.po`

**Interfaces:**
- Consumes: `grid_python()`, `HELPER`, `PROBE_TIMEOUT`, `REAP_GRACE`, `_core`.
- Produces: `_start_helper() -> (x, y, w, h) | None`, `read_until(stream, deadline) -> dict | None`, `stop_helper(reason)`, `_send(payload)`, `_close_session()`.

- [ ] **Step 1: Write the failing tests**

```python
class TestHelperChannel:
    def _fake_helper(self, tmp_path, script):
        path = tmp_path / "helper.py"
        path.write_text(script, encoding="utf-8")
        return str(path)

    def test_ready_then_presented_are_logged_and_geometry_returned(
        self, monkeypatch, tmp_path, caplog
    ):
        helper = self._fake_helper(
            tmp_path,
            "import json, sys\n"
            "print(json.dumps({'event': 'ready', 'monitor': [0, 0, 1920, 1080],"
            " 'scale': 1.0}), flush=True)\n"
            "print(json.dumps({'event': 'presented'}), flush=True)\n"
            "sys.stdin.readline()\n",
        )
        monkeypatch.setattr(g, "HELPER", helper)
        monkeypatch.setattr(g, "grid_python", lambda: "python3")
        with caplog.at_level("DEBUG"):
            assert g._start_helper() == (0, 0, 1920, 1080)
        g.stop_helper("test")
        assert any("ready" in r.message and "1920" in r.message for r in caplog.records)

    def test_a_silent_helper_hits_the_deadline_and_is_reaped(
        self, monkeypatch, tmp_path
    ):
        helper = self._fake_helper(tmp_path, "import time\ntime.sleep(60)\n")
        monkeypatch.setattr(g, "HELPER", helper)
        monkeypatch.setattr(g, "grid_python", lambda: "python3")
        monkeypatch.setattr(g, "PROBE_TIMEOUT", 0.5)
        assert g._start_helper() is None
        assert g._helper is None

    def test_an_error_answer_ends_the_handshake_with_its_message(
        self, monkeypatch, tmp_path, caplog
    ):
        helper = self._fake_helper(
            tmp_path,
            "import json, sys, time\n"
            "print(json.dumps({'event': 'error',"
            " 'message': 'layer-shell unsupported'}), flush=True)\n"
            "time.sleep(60)\n",
        )
        monkeypatch.setattr(g, "HELPER", helper)
        monkeypatch.setattr(g, "grid_python", lambda: "python3")
        with caplog.at_level("WARNING"):
            assert g._start_helper() is None
        assert any("layer-shell unsupported" in r.message for r in caplog.records)

    def test_a_helper_that_dies_first_is_reaped(self, monkeypatch, tmp_path):
        helper = self._fake_helper(tmp_path, "raise SystemExit(1)\n")
        monkeypatch.setattr(g, "HELPER", helper)
        monkeypatch.setattr(g, "grid_python", lambda: "python3")
        assert g._start_helper() is None
        assert g._helper is None

    def test_a_bad_line_is_logged_and_ignored(self, monkeypatch, tmp_path):
        helper = self._fake_helper(
            tmp_path,
            "import json, sys\n"
            "print('not json at all', flush=True)\n"
            "print(json.dumps({'event': 'ready', 'monitor': [1, 2, 3, 4]}), flush=True)\n"
            "sys.stdin.readline()\n",
        )
        monkeypatch.setattr(g, "HELPER", helper)
        monkeypatch.setattr(g, "grid_python", lambda: "python3")
        assert g._start_helper() == (1, 2, 3, 4)
        g.stop_helper("test")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --extra=unittest pytest tests/unit/plugins/test_gridydotool.py -k Helper -v`
Expected: FAIL — `AttributeError: ... has no attribute '_start_helper'`

- [ ] **Step 3: Implement the channel**

Append to the module:

```python
import json
import select
import time


def _start_helper():
    """Spawn the helper, take its geometry, and return it or None.

    The helper is spawned with `Popen`, not `run`: `run` would only return once
    the process exits, and this process must stay up and keep rendering. The
    `ready` wait is a read on a live child's pipe, so it is bounded by a `select`
    deadline rather than a subprocess timeout, which cannot reach a pipe read. A
    helper that stays alive and writes nothing is the case that distinction
    exists for.
    """
    global _helper
    python = grid_python()
    if python is None:
        return None
    try:
        _helper = subprocess.Popen(
            [python, HELPER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        logger.warning("grid: the helper could not start: %s", exc)
        _helper = None
        return None

    deadline = time.monotonic() + PROBE_TIMEOUT
    while True:
        message = read_until(_helper.stdout, deadline)
        if message is None:
            logger.warning("grid: the helper never reported ready (deadline)")
            stop_helper("no ready inside PROBE_TIMEOUT")
            return None
        event = message.get("event")
        if event == "ready":
            logger.info(
                "grid: ready on %s (scale %s)",
                message.get("monitor"),
                message.get("scale"),
            )
            break
        if event == "error":
            logger.warning("grid: helper refused: %s", message.get("message"))
            stop_helper("helper error")
            return None
        logger.debug("grid: %s", event)

    monitor = message.get("monitor")
    if not monitor or len(monitor) != 4:
        logger.warning("grid: the ready event carried no usable geometry")
        stop_helper("bad geometry")
        return None
    return tuple(monitor)


def read_until(stream, deadline):
    """Read one JSON line from `stream`, or None when the deadline passes.

    The bound is a mechanism here, not a property: `select` is what lets a
    blocking read give up. The line is assembled from single bytes off the
    descriptor rather than read back with `stream.readline()`, whose text layer
    reads ahead: a line that layer holds is invisible to `select`, and a chunk
    read here would strand every line after the one returned. One byte at a
    time is what leaves the unread protocol in the one place `select` can see
    it. Anything that does not parse is logged and skipped, and EOF reads as
    None.
    """
    pending = b""
    while True:
        if pending.endswith(b"\n"):
            line = pending.decode("utf-8", "replace")
            pending = b""
            try:
                return json.loads(line)
            except ValueError:
                logger.warning(
                    "grid: ignoring unparsable protocol line: %r", line[:120]
                )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            ready, _, _ = select.select([stream], [], [], remaining)
        except (OSError, ValueError):
            return None
        if not ready:
            return None
        chunk = os.read(stream.fileno(), 1)
        if not chunk:
            logger.warning("grid: the helper closed its output")
            return None
        pending += chunk


def _send(payload):
    """Write one protocol line to the helper, if one is running."""
    if _helper is None or _helper.stdin is None:
        return False
    try:
        _helper.stdin.write(json.dumps(payload) + "\n")
        _helper.stdin.flush()
    except (BrokenPipeError, ValueError):
        logger.warning("grid: the helper is gone; dropping %s", payload.get("cmd"))
        return False
    return True


def stop_helper(reason):
    """Stop and reap the helper, escalating when it will not go.

    `terminate()`, a bounded wait, then `kill()`, logging which rung was needed:
    an unbounded `wait()` would freeze the mode at exactly the point this
    function exists to make safe, and a process that cannot be scheduled to
    handle SIGTERM still takes a kill from the kernel.
    """
    global _helper
    helper, _helper = _helper, None
    if helper is None or helper.poll() is not None:
        return
    helper.terminate()
    try:
        helper.wait(timeout=REAP_GRACE)
        logger.debug("grid: helper stopped (%s)", reason)
    except subprocess.TimeoutExpired:
        logger.warning("grid: helper ignored SIGTERM (%s); killing", reason)
        helper.kill()
        helper.wait(timeout=REAP_GRACE)


def _close_session():
    """Tear the surface down and reap the helper; safe to call twice."""
    _send({"cmd": "hide"})
    stop_helper("session closed")
```

- [ ] **Step 4: Add `_helper = None` to the module state**

Next to the other globals, add:

```python
_helper = None
```

- [ ] **Step 5: Let the pre-ready path speak, and only once**

Append to `_open_session` (replace the early return):

```python
    geometry = _start_helper()
    if geometry is None:
        core.speak(_("I couldn't show the grid."))
        return None
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run --extra=unittest pytest tests/unit/plugins/test_gridydotool.py -v`
Expected: PASS, including the silent-helper and error-answer cases.

- [ ] **Step 7: Translate the fifth string**

Run: `just translations de && just translations es && just translations fr && just translations it`

Fill `I couldn't show the grid.` with:

| language | msgstr |
|---|---|
| de | `Ich konnte das Raster nicht anzeigen.` |
| es | `No pude mostrar la cuadrícula.` |
| fr | `Je n'ai pas pu afficher la grille.` |
| it | `Non sono riuscito a mostrare la griglia.` |

- [ ] **Step 8: Verify and commit**

Run: `uv run --extra=unittest pytest tests/unit/plugins/test_gridydotool.py tests/unit/core/test_i18n.py -v && just check-translations`
Expected: PASS.

```bash
hug a src/plugins/gridydotool tests/unit/plugins/test_gridydotool.py
hug c -F - <<'EOF'
feat(gridydotool): the helper channel, bounded by a mechanism

The helper is spawned with Popen and the ready wait is a select deadline on its
stdout, because a subprocess timeout bounds a process and this wait is a pipe
read on a live child: a helper that stays alive and writes nothing would sail
past a bound written only as a property. Five cases are handled, including the
error answer, which ends the handshake at that line instead of discarding the
only component that knows why a surface was refused.

stop_helper escalates terminate, a bounded grace, kill, and logs which rung was
needed. Without that bound the reap becomes the next hang, on the path that
exists to prevent one, and without the reap a helper that finished mapping after
the deadline would leave an ownerless full-screen overlay.

Co-authored-by: CommandCodeBot <noreply@commandcode.ai>
EOF
```

---

### Task 5: The overlay helper

**Files:**
- Create: `src/plugins/gridydotool/_overlay.py`
- Modify: `tests/unit/plugins/test_gridydotool.py`

**Interfaces:**
- Consumes: nothing from the plugin module (it is a separate interpreter's program).
- Produces: `cell_rects(bounds) -> list[tuple[int, int, int, int]]`, `parse_command(line) -> dict | None`, `main(argv) -> int` honouring `--probe`.

- [ ] **Step 1: Write the failing tests**

```python
import ctypes
import ctypes.util
import importlib.util
from pathlib import Path


def _overlay_module():
    """Import the helper without importing gi: the module defers that import so
    the daemon's venv can test its pure parts."""
    path = Path(g.__file__).with_name("_overlay.py")
    spec = importlib.util.spec_from_file_location("grid_overlay", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestOverlayPureParts:
    def test_cell_rects_are_the_nine_thirds(self):
        overlay = _overlay_module()
        rects = overlay.cell_rects((0, 0, 900, 600))
        assert rects[0] == (0, 0, 300, 200)
        assert rects[8] == (600, 400, 300, 200)
        assert len(rects) == 9

    def test_parse_command_accepts_show_and_hide(self):
        overlay = _overlay_module()
        assert overlay.parse_command('{"cmd": "hide"}') == {"cmd": "hide"}
        assert overlay.parse_command("") is None
        assert overlay.parse_command("{oops") is None

    def test_take_lines_splits_every_complete_line_of_one_chunk(self):
        """A `stop` behind another command in the same write must arrive too."""
        overlay = _overlay_module()
        lines, remainder = overlay.take_lines("", '{"cmd": "hide"}\n{"cmd": "stop"}\n')
        assert lines == ['{"cmd": "hide"}', '{"cmd": "stop"}']
        assert remainder == ""

    def test_take_lines_carries_a_partial_line_into_the_next_chunk(self):
        overlay = _overlay_module()
        lines, remainder = overlay.take_lines("", '{"cmd": "hi')
        assert lines == []
        assert remainder == '{"cmd": "hi'
        lines, remainder = overlay.take_lines(remainder, 'de"}\n{"cmd":')
        assert lines == ['{"cmd": "hide"}']
        assert remainder == '{"cmd":'

    def test_probe_mode_reports_unsupported_without_a_display(self):
        overlay = _overlay_module()
        assert overlay.main(["--probe"]) == 1

    def test_the_module_imports_without_gi(self):
        assert _overlay_module() is not None


class TestOverlayLibraryLookup:
    """The runtime ships only the soname; the unversioned `.so` is `-dev`-only.

    Ubuntu 25.10's `libgtk4-layer-shell0` installs `libgtk4-layer-shell.so.0`
    (-> `.so.1.0.4`) and nothing unversioned, so `CDLL("libgtk4-layer-shell.so")`
    refuses everywhere the library is merely installed. The chain resolves the
    soname through the loader's own cache and keeps the plain name for an
    uninstalled build tree on `LD_LIBRARY_PATH`. A `find_library` result is
    trusted only when it names this library, because it reports the soname
    *inside* the file that spelled the name.
    """

    def test_the_chain_ends_at_the_soname_and_the_plain_name(self, monkeypatch):
        overlay = _overlay_module()
        monkeypatch.setattr(ctypes.util, "find_library", lambda name: None)
        assert overlay.layer_shell_names() == [
            "libgtk4-layer-shell.so.0",
            "libgtk4-layer-shell.so",
        ]

    def test_the_chain_prefers_what_find_library_resolves(self, monkeypatch):
        overlay = _overlay_module()
        monkeypatch.setattr(
            ctypes.util, "find_library", lambda name: "libgtk4-layer-shell.so.0"
        )
        assert overlay.layer_shell_names() == [
            "libgtk4-layer-shell.so.0",
            "libgtk4-layer-shell.so",
        ]

    def test_a_result_that_names_another_library_is_not_trusted(self, monkeypatch):
        """`find_library` answers with the soname embedded in the file it found,
        never a made-up one: a copy of libm under the name
        `libgtk4-layer-shell.so` answers `libm.so.6`, and trusting it would make
        the loader preload libm while the link order the CDLL exists for is
        silently lost."""
        overlay = _overlay_module()
        monkeypatch.setattr(ctypes.util, "find_library", lambda name: "libm.so.6")
        assert overlay.layer_shell_names() == [
            "libgtk4-layer-shell.so.0",
            "libgtk4-layer-shell.so",
        ]

    def test_load_walks_the_chain_until_a_name_loads(self, monkeypatch):
        overlay = _overlay_module()
        monkeypatch.setattr(
            ctypes.util, "find_library", lambda name: "libgtk4-layer-shell.so.99"
        )
        attempts = []

        def cdll(name):
            attempts.append(name)
            if name != "libgtk4-layer-shell.so.0":
                msg = f"{name}: cannot open shared object file"
                raise OSError(msg)
            return Mock()

        monkeypatch.setattr(ctypes, "CDLL", cdll)
        overlay.load_layer_shell()
        assert attempts == [
            "libgtk4-layer-shell.so.99",
            "libgtk4-layer-shell.so.0",
        ]

    def test_a_host_with_only_the_versioned_soname_loads_it(self, monkeypatch):
        """The VM shape: `libgtk4-layer-shell0` installs only `.so.0`, which
        find_library resolves through the loader's cache. A chain reduced to the
        single hardcoded plain name would try `.so`, fail, and refuse where the
        library is installed."""
        overlay = _overlay_module()
        monkeypatch.setattr(
            ctypes.util, "find_library", lambda name: "libgtk4-layer-shell.so.0"
        )
        attempts = []

        def cdll(name):
            attempts.append(name)
            if name != "libgtk4-layer-shell.so.0":
                msg = f"{name}: cannot open shared object file"
                raise OSError(msg)
            return Mock()

        monkeypatch.setattr(ctypes, "CDLL", cdll)
        overlay.load_layer_shell()
        assert attempts == ["libgtk4-layer-shell.so.0"]

    def test_load_never_opens_a_name_meant_for_another_library(self, monkeypatch):
        overlay = _overlay_module()
        monkeypatch.setattr(ctypes.util, "find_library", lambda name: "libm.so.6")
        attempts = []

        def cdll(name):
            attempts.append(name)
            msg = f"{name}: cannot open shared object file"
            raise OSError(msg)

        monkeypatch.setattr(ctypes, "CDLL", cdll)
        assert overlay.load_layer_shell() == (None, None)
        assert attempts == [
            "libgtk4-layer-shell.so.0",
            "libgtk4-layer-shell.so",
        ]

    def test_load_reaches_the_plain_name_of_an_uninstalled_build(self, monkeypatch):
        overlay = _overlay_module()
        monkeypatch.setattr(ctypes.util, "find_library", lambda name: None)
        attempts = []

        def cdll(name):
            attempts.append(name)
            if name != "libgtk4-layer-shell.so":
                msg = f"{name}: cannot open shared object file"
                raise OSError(msg)
            return Mock()

        monkeypatch.setattr(ctypes, "CDLL", cdll)
        overlay.load_layer_shell()
        assert attempts == [
            "libgtk4-layer-shell.so.0",
            "libgtk4-layer-shell.so",
        ]

    def test_the_refusal_names_every_candidate(self, monkeypatch, capsys):
        overlay = _overlay_module()
        monkeypatch.setattr(ctypes.util, "find_library", lambda name: None)
        monkeypatch.setattr(ctypes, "CDLL", Mock(side_effect=OSError("absent")))
        assert overlay.load_layer_shell() == (None, None)
        err = capsys.readouterr().err
        assert "libgtk4-layer-shell.so.0" in err
        assert "libgtk4-layer-shell.so" in err


class TestOverlayServePath:
    """`main` without `--probe`: refuse rather than degrade, then hand over.

    The refusal is the counterpart of the plugin's handshake: a silent window
    would look like a rendering bug while the injections kept working.
    """

    def test_refuses_with_an_error_event_without_the_library(self, monkeypatch, capsys):
        overlay = _overlay_module()
        monkeypatch.setattr(overlay, "load_layer_shell", lambda: (None, None))
        assert overlay.main([]) == 1
        assert json.loads(capsys.readouterr().out.strip()) == {
            "event": "error",
            "message": "PyGObject or the layer-shell typelib is missing",
        }

    def test_refuses_when_layer_shell_is_unsupported(self, monkeypatch, capsys):
        overlay = _overlay_module()
        stack = (Mock(), Mock(is_supported=Mock(return_value=False)))
        monkeypatch.setattr(overlay, "load_layer_shell", lambda: stack)
        assert overlay.main([]) == 1
        assert json.loads(capsys.readouterr().out.strip()) == {
            "event": "error",
            "message": "layer-shell unsupported",
        }

    def test_hands_a_supported_stack_to_run(self, monkeypatch):
        overlay = _overlay_module()
        stack = (Mock(), Mock(is_supported=Mock(return_value=True)))
        run = Mock(return_value=7)
        monkeypatch.setattr(overlay, "load_layer_shell", lambda: stack)
        monkeypatch.setattr(overlay, "run", run)
        assert overlay.main([]) == 7
        run.assert_called_once_with(*stack)


class TestOverlayLoadGuard:
    """A loadable library must not turn a missing PyGObject into a traceback.

    That shape is exactly the daemon's venv with the library findable on the
    loader path: the reviewer reproduced it with a dummy `libgtk4-layer-shell.so`
    and `--probe` died inside `load_layer_shell` before it could answer
    `unsupported`, skipping every caller's error path.
    """

    @staticmethod
    def _library_loads(monkeypatch):
        monkeypatch.setattr(ctypes, "CDLL", lambda name: Mock())
        monkeypatch.setitem(sys.modules, "gi", None)

    def test_load_layer_shell_reports_the_missing_pygobject(self, monkeypatch, capsys):
        self._library_loads(monkeypatch)
        overlay = _overlay_module()
        assert overlay.load_layer_shell() == (None, None)
        assert "PyGObject" in capsys.readouterr().err

    def test_probe_answers_unsupported_instead_of_dying(self, monkeypatch, capsys):
        self._library_loads(monkeypatch)
        overlay = _overlay_module()
        assert overlay.main(["--probe"]) == 1
        assert capsys.readouterr().out.strip() == "unsupported"


class TestOverlayClickThrough:
    """The empty input region is the pass-through mechanism: apply it or refuse.

    Measured on this stack (GTK 4.6.9, two stacked windows, an XTest click):
    with `set_can_target(False)` alone the click is swallowed - neither window
    receives it - while an empty input region lets the window below receive it.
    The feature is a click injected while the pointer is over the surface, so a
    surface that cannot be made click-through has to surface as an error, never
    as a grid that silently eats its own click.
    """

    def test_the_surface_gets_an_empty_region(self, monkeypatch):
        overlay = _overlay_module()

        class FakeRegion:
            pass

        monkeypatch.setitem(
            sys.modules, "cairo", types.SimpleNamespace(Region=FakeRegion)
        )
        surface = Mock()
        assert overlay.clear_input_region(surface) is None
        region = surface.set_input_region.call_args.args[0]
        assert isinstance(region, FakeRegion)

    def test_missing_pycairo_is_a_diagnostic_not_a_traceback(self, monkeypatch):
        overlay = _overlay_module()
        monkeypatch.setitem(sys.modules, "cairo", None)
        surface = Mock()
        problem = overlay.clear_input_region(surface)
        assert "input region" in problem
        surface.set_input_region.assert_not_called()

    def test_a_surface_that_rejects_the_region_is_a_diagnostic(self, monkeypatch):
        overlay = _overlay_module()
        monkeypatch.setitem(
            sys.modules, "cairo", types.SimpleNamespace(Region=lambda: None)
        )
        surface = Mock()
        surface.set_input_region.side_effect = TypeError("no foreign struct converter")
        problem = overlay.clear_input_region(surface)
        assert "input region" in problem
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --extra=unittest pytest tests/unit/plugins/test_gridydotool.py -k Overlay -v`
Expected: FAIL — the file does not exist.

- [ ] **Step 3: Write the helper**

Create `src/plugins/gridydotool/_overlay.py`:

```python
"""Standalone GTK4 layer-shell overlay for the gridydotool plugin.

Runs as a script in an interpreter that has PyGObject and the gtk4-layer-shell
typelib, both of which the daemon's venv lacks. It is a real module rather than
an embedded `-c` string so Ruff lints it and the unit suite can exercise the pure
parts (the `_atspi_insert.py` precedent), which is also why `gi` is imported
inside functions: importing it at module scope would make the whole file
unimportable from the venv.

Protocol: newline-delimited JSON on stdin and stdout, diagnostics on stderr.

    {"cmd": "show", "bounds": [x, y, w, h]}   draw a 3x3 grid over this rect
    {"cmd": "hide"}                            tear the surface down
    {"cmd": "stop"}                            exit (alias: "quit"; EOF does too)

    {"event": "ready", "monitor": [x, y, w, h], "scale": 1.0}
    {"event": "presented"}
    {"event": "error", "message": "..."}

`--probe` answers whether this compositor supports layer-shell and exits 0 or 1
without mapping anything.
"""

import json
import os
import sys
from pathlib import Path

DEFAULT_NAMESPACE = "gridydotool"


def cell_rects(bounds):
    """Return the nine cell rectangles of a 3x3 grid over `bounds`, row-major."""
    x, y, w, h = bounds
    cell_w, cell_h = w // 3, h // 3
    return [
        (x + col * cell_w, y + row * cell_h, cell_w, cell_h)
        for row in range(3)
        for col in range(3)
    ]


def parse_command(line):
    """Return the command object on a protocol line, or None if there is none."""
    line = line.strip()
    if not line:
        return None
    try:
        payload = json.loads(line)
    except ValueError:
        sys.stderr.write(f"grid-overlay: ignoring unparsable line: {line[:120]}\n")
        return None
    return payload if isinstance(payload, dict) else None


def take_lines(pending, chunk):
    """Split every complete line out of `pending + chunk`.

    Returns `(lines, remainder)`: the newline-terminated lines in order, and
    the trailing fragment that has not arrived completely yet. Splitting the
    whole buffer - instead of reading one line per wake-up - is what keeps a
    command that sits behind another one in the same write from being
    stranded where no io watch can see it.
    """
    lines = (pending + chunk).split("\n")
    return lines[:-1], lines[-1]


def emit(payload):
    """Write one event to the parent."""
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def layer_shell_names():
    """The loadable names for gtk4-layer-shell, most specific first.

    `ctypes.util.find_library` asks the loader's own cache, so an installed
    runtime is found by *soname*: Ubuntu 25.10's `libgtk4-layer-shell0` ships
    only `libgtk4-layer-shell.so.0`, and the unversioned `.so` symlink belongs
    to the `-dev` package a normal user does not have. The soname and the plain
    name are literal fallbacks: the plain one covers an uninstalled build tree
    on `LD_LIBRARY_PATH`, which ldconfig cannot see.

    Only a result that names this library is trusted. `find_library` answers
    with the soname *inside* the file that spells the queried name, so a stale
    or misnamed `libgtk4-layer-shell.so` on the loader path answers with that
    file's own soname (measured: a copy of libm under that name answered
    `libm.so.6`). Loading that answer would preload an unrelated library and
    the link order this load exists for would be silently lost.
    """
    from ctypes.util import find_library

    names = []
    found = find_library("gtk4-layer-shell")
    if found and Path(found).name.startswith("libgtk4-layer-shell."):
        names.append(found)
    for candidate in ("libgtk4-layer-shell.so.0", "libgtk4-layer-shell.so"):
        if candidate not in names:
            names.append(candidate)
    return names


def load_layer_shell():
    """Load gtk4-layer-shell and Gtk, or return (None, None).

    The library must be loaded before Gtk's Wayland client is, which is what the
    CDLL call buys: linking it later lets libwayland-client win the symbol
    resolution and the layer surface silently comes up as an ordinary window.
    The names are tried in `layer_shell_names()` order, so a runtime that ships
    only the soname still loads; the load happens before `gi` regardless.
    """
    from ctypes import CDLL

    names = layer_shell_names()
    error = None
    for name in names:
        try:
            CDLL(name)
        except OSError as exc:
            error = exc
        else:
            break
    else:
        sys.stderr.write(
            f"grid-overlay: gtk4-layer-shell not found "
            f"(tried {', '.join(names)}): {error}\n"
        )
        return None, None
    try:
        import gi
    except ModuleNotFoundError as exc:
        # The daemon's venv: a loadable library with PyGObject missing. A bare
        # import here used to escape as a traceback, skipping every caller's
        # error path (reproduced with a dummy library on the loader path:
        # `--probe` under the venv died on this line).
        sys.stderr.write(f"grid-overlay: PyGObject is not installed: {exc}\n")
        return None, None

    try:  # pragma: no cover - needs the layer-shell typelib from the live stack
        gi.require_version("Gtk", "4.0")
        gi.require_version("Gtk4LayerShell", "1.0")
        from gi.repository import Gtk, Gtk4LayerShell
    except (ValueError, ImportError) as exc:  # pragma: no cover - live stack only
        sys.stderr.write(f"grid-overlay: cannot load Gtk4LayerShell: {exc}\n")
        return None, None
    return Gtk, Gtk4LayerShell  # pragma: no cover - live stack only


def is_supported():
    """True when this compositor speaks wlr-layer-shell."""
    _, layer_shell = load_layer_shell()
    if layer_shell is None:
        return False
    return bool(layer_shell.is_supported())  # pragma: no cover - live shell only


def clear_input_region(surface):
    """Empty the surface's input region, so clicks pass through it.

    `set_can_target(False)` is not enough on this stack: with it alone the
    click is swallowed - neither window receives it - while an empty input
    region lets the window below receive it (measured on GTK 4.6.9, two
    stacked windows, an XTest click). The feature is exactly a click injected
    while the pointer is over this surface, so a swallowing surface breaks it
    silently.

    Returns None on success, or a diagnostic when the region cannot be
    applied; the caller must refuse rather than present the surface without it.
    """
    try:
        import cairo

        surface.set_input_region(cairo.Region())
    except (ImportError, AttributeError, TypeError) as exc:
        return f"cannot clear the input region: {exc}"
    return None


class Overlay:  # pragma: no cover - needs GTK; run live
    """The layer surface, its nine cells, and the redraws they ask for."""

    def __init__(self, gtk, layer_shell, app):
        self.gtk = gtk
        self.layer_shell = layer_shell
        # Associate the window with the application: an app-managed window is what
        # the application's lifecycle (activate, quit, and the stdin watch that
        # ends the process) is written for.
        self.window = gtk.Window(application=app)
        self.area = gtk.DrawingArea()
        self.bounds = None
        self.origin = (0, 0)
        self.presented = False
        self.window.set_child(self.area)
        self.area.set_draw_func(self._draw)
        layer_shell.init_for_window(self.window)
        for edge in (
            layer_shell.Edge.LEFT,
            layer_shell.Edge.RIGHT,
            layer_shell.Edge.TOP,
            layer_shell.Edge.BOTTOM,
        ):
            layer_shell.set_anchor(self.window, edge, True)  # noqa: FBT003
        layer_shell.set_layer(self.window, layer_shell.Layer.OVERLAY)
        # Never take focus, and never eat the injected click. `set_can_target`
        # stops GTK from targeting the window; the empty input region - applied
        # by `make_click_through` as soon as the surface exists - is what
        # actually lets the click reach whatever is underneath.
        layer_shell.set_keyboard_mode(self.window, layer_shell.KeyboardMode.NONE)
        self.window.set_can_target(False)
        self.click_through_surface = None

    def _draw(self, area, cr, width, height):
        """Paint the nine cells with their digits.

        PyGObject calls a `set_draw_func` callback with exactly these four
        arguments - it does not pass the C callback's trailing user_data - and a
        five-argument signature raises TypeError on every frame, which loses
        both the grid and the `presented` event.
        """
        if not self.bounds:
            return
        origin_x, origin_y = self.origin
        cr.set_source_rgba(0.05, 0.05, 0.08, 0.55)
        cr.paint()
        cr.set_source_rgba(0.95, 0.95, 0.95, 0.95)
        for index, (x, y, w, h) in enumerate(cell_rects(self.bounds), start=1):
            left, top = x - origin_x, y - origin_y
            cr.rectangle(left, top, w, h)
            cr.stroke()
            cr.move_to(left + w / 2 - 8, top + h / 2 + 8)
            cr.set_font_size(28)
            cr.show_text(str(index))
        del area, width, height
        if not self.presented:
            # The first frame of an actual grid is on the glass now; the plugin
            # reads this as "it is on screen", once, never per redraw.
            self.presented = True
            emit({"event": "presented"})

    def make_click_through(self):
        """Give the surface an empty input region, or say why it could not.

        Returns None when the surface is click-through already, or has not
        been created yet - the ready poll retries - and a diagnostic string
        otherwise; the caller refuses the session on a diagnostic, because a
        surface that cannot be made click-through must not be presented as if
        the click would pass through it.
        """
        surface = self.window.get_surface()
        if surface is None or surface is self.click_through_surface:
            return None
        problem = clear_input_region(surface)
        if problem is None:
            self.click_through_surface = surface
        return problem

    def show(self, bounds):
        """Draw the grid over `bounds`; a diagnostic means refuse the session.

        The region is re-checked here, on every show: a surface presented later
        must not go up without it.
        """
        self.bounds = tuple(bounds)
        self.window.present()
        self.area.queue_draw()  # the bounds changed: repaint, mapped or not
        return self.make_click_through()

    def hide(self):
        self.window.set_visible(False)


def report_monitor(overlay):  # pragma: no cover - needs a live surface
    """Emit the ready event once the surface is configured.

    The monitor geometry comes from the surface, not from an assumption: the
    compositor chooses the output (the helper makes no set_monitor call), and
    this report is the plugin's only source of screen size.
    """
    surface = overlay.window.get_surface()
    display = surface.get_display()
    monitor = display.get_monitor_at_surface(surface)
    geometry = monitor.get_geometry()
    overlay.origin = (geometry.x, geometry.y)
    emit(
        {
            "event": "ready",
            "monitor": [geometry.x, geometry.y, geometry.width, geometry.height],
            "scale": monitor.get_scale_factor(),
        }
    )


def run(gtk, layer_shell):  # pragma: no cover - drives the GTK loop
    """Wire the protocol to the surface and pump the GTK loop."""
    import gi

    GLib = gi.repository.GLib

    app = gtk.Application(application_id="dev.easyspeak.gridydotool.overlay")
    overlay = None
    fatal = None

    def refuse(problem):
        """Emit the refusal instead of the surface that could not be made safe."""
        nonlocal fatal
        fatal = problem
        emit({"event": "error", "message": problem})
        app.quit()
        return False

    def on_activate(_app):
        """Build and present the surface inside the application's lifetime.

        The window cannot be made before `app.run`: GTK refuses to add a window
        created before the `startup` signal ("New application windows must be
        added after the GApplication::startup signal has been emitted"), and an
        application holding no windows returns from `run` as soon as activation
        ends - which would end the helper right after `ready` and leave the
        plugin talking to a corpse.
        """
        nonlocal overlay
        overlay = Overlay(gtk, layer_shell, app)
        overlay.window.present()
        GLib.idle_add(emit_ready_when_mapped)

    def emit_ready_when_mapped():
        """Report geometry only once the surface exists, then stop polling.

        `ready` is what the plugin waits for before it can compute a single
        coordinate, so it must be sent after GTK has a surface to ask, not
        from the constructor where there is none yet - and only after the
        surface has been made click-through, or the click this grid aims would
        be swallowed before it arrived.
        """
        if overlay is None or overlay.window.get_surface() is None:
            return True
        problem = overlay.make_click_through()
        if problem is not None:
            return refuse(problem)
        report_monitor(overlay)
        return False

    app.connect("activate", on_activate)

    pending = ""

    def handle_line(line):
        """Act on one protocol line; False means the process should end."""
        command = parse_command(line)
        if command is None:
            return True
        name = command.get("cmd")
        if name == "show" and overlay is not None:
            problem = overlay.show(command["bounds"])
            if problem is not None:
                return refuse(problem)
        elif name == "hide" and overlay is not None:
            overlay.hide()
        elif name in ("stop", "quit"):
            app.quit()
            return False
        return True

    def on_stdin_ready(_source, _condition):
        """Consume every complete line the descriptor holds, not just one.

        One chunk is read per wake-up and `take_lines` splits every complete
        line out of it - a `stop` behind another command in the same write must
        not be stranded where no watch can see it - while a partial line waits
        in `pending` for the next wake-up.
        """
        nonlocal pending
        chunk = os.read(sys.stdin.fileno(), 4096)
        if not chunk:  # the plugin is gone; never outlive it
            app.quit()
            return False
        lines, pending = take_lines(pending, chunk.decode("utf-8", "replace"))
        # `all` short-circuits at the first `stop`/`quit`, same as a loop would.
        return all(handle_line(line) for line in lines)

    GLib.io_add_watch(sys.stdin.fileno(), GLib.IO_IN | GLib.IO_HUP, on_stdin_ready)
    app.run([])
    return 1 if fatal is not None else 0


def main(argv=None):
    """Entry point: `--probe` one-shots the support question, otherwise serve."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--probe" in argv:
        supported = is_supported()
        sys.stdout.write("supported\n" if supported else "unsupported\n")
        return 0 if supported else 1
    Gtk, layer_shell = load_layer_shell()
    if Gtk is None:
        emit(
            {
                "event": "error",
                "message": "PyGObject or the layer-shell typelib is missing",
            }
        )
        return 1
    if not layer_shell.is_supported():
        # Refuse rather than degrade: an ordinary window would look like a
        # rendering bug while the injections kept working.
        emit({"event": "error", "message": "layer-shell unsupported"})
        return 1
    return run(Gtk, layer_shell)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the unit tests**

Run: `uv run --extra=unittest pytest tests/unit/plugins/test_gridydotool.py -v`
Expected: PASS. The pure parts are exercised in the venv; the GTK wiring is exercised by the harness in Task 6.

- [ ] **Step 5: Lint and type-check the helper**

Run: `just format && just lint && just types`
Expected: PASS (Ruff `select = ["ALL"]` applies; the helper uses `sys.stdout.write` rather than `print` for exactly that reason).

- [ ] **Step 6: Commit**

```bash
hug a src/plugins/gridydotool/_overlay.py tests/unit/plugins/test_gridydotool.py
hug c -F - <<'EOF'
feat(gridydotool): the GTK4 layer-shell overlay helper

The renderer that makes this backend compositor-agnostic: a layer surface on the
overlay layer, anchored to every edge, with keyboard mode NONE and an empty input
region so the click it is aiming passes through it.

gi is imported inside functions, which is what keeps the module importable from
the daemon's venv so its pure parts (the nine cell rectangles and the protocol
parser) are unit-tested rather than eyeballed. The library is loaded with CDLL
before Gtk so libwayland-client cannot win the symbol resolution and turn the
layer surface into an ordinary window.

show refuses when the compositor has no layer-shell: degrading would look like a
rendering bug while the injections kept working, and the refusal is what the
plugin's handshake surfaces as an error.

Co-authored-by: CommandCodeBot <noreply@commandcode.ai>
EOF
```

---

### Task 6: The integration harness

**Files:**
- Create: `tests/integration/test_gridydotool_harness.py`
- Create: `tests/integration/grid_probe_window.py`
- Modify: `tests/integration/README.md`

**Interfaces:**
- Consumes: `gridydotool.setup/handle/active`, `_overlay.py` (both arms), `sway`, `ydotool`, `grim`.
- Produces: `TestGate` (`test_a_failed_injector_probe_keeps_mousegrid_in_charge`, `test_the_injector_gate_passes_with_an_injector_to_reach`, `test_the_injector_gate_fails_closed_without_a_socket`), `TestForcedBackendArm::test_reads_the_geometry_and_records_the_argv`, `TestLiveArm::test_the_click_lands_in_the_expected_cell`.

- [ ] **Step 1: Write the probe window**

Create `tests/integration/grid_probe_window.py`:

```python
"""Fullscreen GTK4 probe that reports what the pointer does.

Prints one JSON object per line: motion and press, with surface-local
coordinates. In the harness's single-output session that is also the absolute
position, which is what turns "the pointer landed in the cell" into an assertion
instead of an eyeball.
"""

import json
import sys


def main():
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    def emit(**payload):
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()

    def on_activate(app):
        window = Gtk.Window(application=app)
        area = Gtk.DrawingArea()
        window.set_child(area)
        window.fullscreen()

        motion = Gtk.EventControllerMotion()
        motion.connect(
            "motion",
            lambda _c, x, y: emit(event="motion", x=int(x), y=int(y)),
        )
        window.add_controller(motion)

        click = Gtk.GestureClick()
        click.connect(
            "pressed",
            lambda _g, _n, x, y: emit(event="press", button=1, x=int(x), y=int(y)),
        )
        window.add_controller(click)
        window.present()

    app = Gtk.Application(application_id="dev.easyspeak.gridydotool.probe")
    app.connect("activate", on_activate)
    return app.run([])


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Write the harness**

Create `tests/integration/test_gridydotool_harness.py`:

```python
"""The gridydotool end-to-end loop, in a headless sway session.

Checks: a failed injector probe keeps `mousegrid` in charge (no privileges
needed), the injector gate passes on a real ydotool where an answered daemon
socket lets it inject (ydotool 1.0.x has no per-invocation fallback, so there a
socket is the only injector there is; 0.1.8 also works daemonless), the gate
fails closed on 1.0.x when no socket answers, the overlay renders and the argv
is exact under a forced backend (the dry-run arm, which is the arm CI can run),
and, only when EASYSPEAK_GRID_LIVE is set, that a click lands where the grid
aimed it.

The literal in the live arm is hand-computed. Deriving it with the same algorithm
the plugin uses would make the assertion agree with any shared off-by-one, so the
premise (a pinned 1920x1080 output) is asserted first and the algorithmic
prediction is kept only as a cross-check.

Injection is kernel-global: even inside this session, arm B moves the real
cursor and clicks wherever it aims on the live desktop. That is why it is opt-in.

A failing live arm attributes itself. It proves the probe window rendered before
anything is injected, and a press that never arrives is reported together with
what the compositor had as input, so "no seat to route the click", "nothing to
click on" and "a swallowed click" cannot be mistaken for one another.
"""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from easyspeak.plugins import gridydotool

SWAY_CONFIG = """\
output HEADLESS-1 mode 1920x1080
input * {
    accel_profile flat
}
"""

# For 1920x1080 and the sequence 3 7 5: zone 3 -> (1280, 0, 640, 360),
# zone 7 -> (1280, 240, 213, 120), zone 5 -> (1351, 280, 71, 40).
EXPECTED_CELL = (1351, 280, 71, 40)
EXPECTED_TOLERANCE = 10  # a quarter of the cell's smaller dimension, 40


def _missing_prerequisite():
    """What stops the harness from running here, or None when nothing does.

    Three binaries are not enough on their own: the helper and the probe window
    both need an interpreter that can load PyGObject and the gtk4-layer-shell
    typelib. The typelib is the prerequisite that is easy to forget and
    expensive to misread. gtk4-layer-shell is not packaged on every distro, so
    the reference setup is an uninstalled build tree, and a helper that cannot
    load the typelib fails its handshake as a GI error rather than as "the build
    path is missing". The interpreter is checked through the plugin's own
    resolver, so the harness needs exactly what the helper needs: a probe of one
    interpreter would report the daemon venv (no PyGObject) as "unsupported"
    even on a box where /usr/bin/python3 could run everything.
    """
    missing = [name for name in ("sway", "ydotool", "grim") if not shutil.which(name)]
    if missing:
        return f"sway, ydotool and grim are needed; missing here: {', '.join(missing)}"
    if gridydotool.grid_python() is None:
        return (
            "no interpreter with PyGObject and the gtk4-layer-shell typelib "
            "(set EASYSPEAK_GRID_TYPELIB to the build tree's src directory, "
            "or EASYSPEAK_GRID_PYTHON to such an interpreter)"
        )
    return None


@pytest.fixture
def session(tmp_path, monkeypatch):
    """A headless sway session, exported to every client the harness spawns.

    The layer-shell build tree is exported too when one is named: an uninstalled
    build is the supported way to run this library where it is not packaged, and
    the helper finds its typelib through `GI_TYPELIB_PATH`. Both paths are
    prepended to whatever is already set rather than replacing it, and the
    export happens before the prerequisite check so the check sees the same
    environment the clients will.

    The environment is set through monkeypatch rather than handed only to sway,
    because the plugin spawns its own helper and the probe window is spawned by
    the test itself: sway names its socket inside XDG_RUNTIME_DIR, and only its
    own children inherit WAYLAND_DISPLAY. The socket is discovered rather than
    assumed, so a client that never learns it fails loudly here instead of
    quietly talking to the wrong compositor.
    """
    build = os.environ.get("EASYSPEAK_GRID_TYPELIB")
    if build:
        for name in ("GI_TYPELIB_PATH", "LD_LIBRARY_PATH"):
            existing = os.environ.get(name, "")
            monkeypatch.setenv(
                name, f"{build}{os.pathsep}{existing}" if existing else build
            )
    problem = _missing_prerequisite()
    if problem is not None:
        pytest.skip(problem)
    config = tmp_path / "sway.conf"
    config.write_text(SWAY_CONFIG, encoding="utf-8")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    # `WLR_BACKENDS` is a *backend list*, not a mode: with only `headless` in it
    # wlroots creates no libinput backend at all, and the compositor then has no
    # seat whatever the kernel holds — `swaymsg -t get_inputs` answers `[]` even
    # while /proc/bus/input/devices lists a device, and an injected click can
    # never be routed. That is right for the arms that only look at pixels, whose
    # session nothing real should be able to perturb, and fatal for the one arm
    # whose click must reach a window: that arm asks for `libinput` too. wlroots
    # keeps a libinput backend's devices only inside an *active* session, so the
    # live arm needs a seat manager (seatd) or an enabled VT behind it; without
    # one sway exits before binding a socket and the fixture skips, naming that.
    live = os.environ.get("EASYSPEAK_GRID_LIVE") == "click"
    monkeypatch.setenv("WLR_BACKENDS", "headless,libinput" if live else "headless")
    # `WLR_LIBINPUT_NO_DEVICES=1` asks the libinput backend for no devices at all:
    # the same isolation as above, said once more for the case where a caller's
    # environment already names that backend. A value inherited from the caller is
    # dropped for the live arm rather than trusted, so its seat is the harness's
    # own doing.
    if live:
        monkeypatch.delenv("WLR_LIBINPUT_NO_DEVICES", raising=False)
    else:
        monkeypatch.setenv("WLR_LIBINPUT_NO_DEVICES", "1")
    # sway's own words are kept: the one failure that is not the harness's own —
    # a session that never becomes active, so no socket is bound — is only
    # legible in them. The child holds its own copy of the descriptor, so closing
    # this one is safe.
    log = tmp_path / "sway.log"
    with log.open("w", encoding="utf-8") as handle:
        sway = subprocess.Popen(
            ["sway", "-c", str(config)],
            stdout=subprocess.DEVNULL,
            stderr=handle,
        )
    deadline = time.monotonic() + 10
    display = None
    while time.monotonic() < deadline:
        sockets = sorted(
            path.name
            for path in tmp_path.glob("wayland-*")
            if not path.name.endswith(".lock")
        )
        if sockets:
            display = sockets[0]
            break
        if sway.poll() is not None:
            # The live arm's session asks for a libinput backend, which wlroots
            # only keeps inside an active session; a compositor that refuses to
            # start there is reporting the missing seat rather than a bug in the
            # harness, so the skip names the prerequisite and carries sway's own
            # words for it. `seatd` hands a seat to one client at a time, so a
            # stray compositor left running by an earlier experiment looks the
            # same way and is named by the same log tail.
            pytest.skip(
                "sway exited before it bound a socket (the live arm needs an "
                "active session for its libinput backend: a running seatd with a "
                "free seat, or a VT-enabled login session): " + _sway_log_tail(log)
                if live
                else "sway exited before it bound a socket: " + _sway_log_tail(log)
            )
        time.sleep(0.2)
    if display is None:
        sway.terminate()
        pytest.skip(
            "sway never created a Wayland socket (it stayed up without becoming "
            "active): " + _sway_log_tail(log)
        )
    monkeypatch.setenv("WAYLAND_DISPLAY", display)
    yield os.environ
    sway.terminate()
    sway.wait(timeout=5)


def _grim(env, path):
    subprocess.run(["grim", str(path)], env=env, check=False, timeout=10)


def _sway_log_tail(log, lines=3):
    """The last few things sway said, for a skip that would otherwise be bare.

    sway's stderr is captured to a file by the fixture because the one failure
    that is not the harness's own — a session that never becomes active, so the
    compositor never binds a socket — is only legible in sway's own words
    ("failed to add backend 'libinput'", "Timeout waiting session to become
    active"). Best-effort: an empty or unreadable log answers with a dash rather
    than raising in the middle of a skip.
    """
    try:
        text = Path(log).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "-"
    said = [line.strip() for line in text.splitlines() if line.strip()]
    return " | ".join(said[-lines:]) or "-"


def _kernel_input_names():
    """The device names `/proc/bus/input/devices` lists, or an empty list.

    The kernel's view, used only when the compositor will not answer. A device
    here does not mean the session can see it - with `WLR_LIBINPUT_NO_DEVICES=1`
    wlroots opens none of them - so this is a fallback, never the first word.
    """
    try:
        listing = Path("/proc/bus/input/devices").read_text(encoding="utf-8")
    except OSError:
        return []
    return [
        line.split('"')[1]
        for line in listing.splitlines()
        if line.startswith("N: Name=") and line.count('"') >= 2
    ]


def _sway_inputs(env):
    """What `swaymsg -t get_inputs` lists, or None when it cannot answer.

    The compositor is asked first because its list is the one that decides
    whether an injected click has a route: with `WLR_BACKENDS=headless` — no
    libinput backend — it is empty while `/proc/bus/input/devices` still lists
    the kernel's devices. Sway's IPC socket is named inside `XDG_RUNTIME_DIR` but
    is never exported to us (only sway's own children inherit `SWAYSOCK`), so it
    is discovered here the same way the session fixture discovers the Wayland
    socket. Returns None for every failure — a missing `swaymsg`, no socket, a
    refused ask, unparsable JSON — because this runs to explain a failed
    assertion and must not raise one of its own.
    """
    runtime = env.get("XDG_RUNTIME_DIR")
    sockets = sorted(Path(runtime).glob("sway-ipc.*.sock")) if runtime else []
    if not sockets or not shutil.which("swaymsg", path=env.get("PATH")):
        return None
    try:
        ask = subprocess.run(
            ["swaymsg", "-t", "get_inputs"],
            env={**env, "SWAYSOCK": str(sockets[0])},
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if ask.returncode != 0:
        return None
    try:
        return [device.get("identifier", "?") for device in json.loads(ask.stdout)]
    except (ValueError, AttributeError):
        return None


def _input_report(env):
    """What input the session had, one line of diagnosis for a lost click.

    The compositor's own answer is the first word, in the identifiers it uses;
    the kernel list is the named fallback, and both failures are tolerated. The
    answer is a snapshot taken after the click: on 0.1.8 a daemonless ydotool
    destroys its per-invocation device when its client exits, so "no devices" can
    also mean "the device came and went", which is why the line names its source
    rather than judging. On 1.0.x there is no daemonless mode at all, and the
    daemon's device is persistent, so there the reading is unambiguous.
    """
    devices = _sway_inputs(env)
    if devices is not None:
        if devices:
            return (
                f"the compositor sees {len(devices)} input device(s) "
                f"({', '.join(devices)}), so the injected click had a seat"
            )
        return (
            "the compositor sees no input device (swaymsg -t get_inputs "
            "returned []), so the injected click had no seat"
        )
    names = _kernel_input_names()
    if names:
        return (
            "swaymsg could not answer, but /proc/bus/input/devices lists "
            f"{len(names)} device(s) ({', '.join(names)})"
        )
    return "input state unknown: neither swaymsg nor /proc/bus/input/devices answered"


@pytest.fixture(autouse=True)
def _reap_leaked_helper():
    """No test - passing, failing or skipped - leaves a spawned helper behind.

    The live arm cannot close its session itself (the probe window must stay up
    to observe the click), and a failing arm must not strand a GTK process
    either; the unit suite reaps with exactly this fixture.
    """
    yield
    if gridydotool._helper is not None:
        gridydotool.stop_helper("harness cleanup")


class TestGate:
    def test_a_failed_injector_probe_keeps_mousegrid_in_charge(self, monkeypatch):
        """The fail-closed property, asserted where it needs no privileges and
        no display: a probe that cannot inject leaves the words to mousegrid."""
        monkeypatch.delenv("EASYSPEAK_GRID_BACKEND", raising=False)
        monkeypatch.setattr(gridydotool, "_probe_overlay", lambda: True)
        monkeypatch.setattr(gridydotool, "_probe_injector", lambda: False)
        gridydotool.setup(core=None)
        assert gridydotool.active() is False
        assert gridydotool.handle("grid", core=None) is None

    def test_the_injector_gate_passes_with_an_injector_to_reach(
        self, monkeypatch, caplog
    ):
        """The gate's passing case on a real ydotool: the client's own verb list,
        the no-op built in the dialect that client reports, and a daemon socket
        to emit through. This runs the real `ydotool help` and the real no-op, so
        it skips where ydotool is absent, or where nothing answers a socket.

        The daemon is a precondition here, not a mode: Ubuntu 26.04 ships ydotool
        1.0.4, whose client has no per-invocation fallback (0.1.8, on 25.10, had
        one), so with no socket it exits 2 with "failed to connect socket" and
        emits nothing. The socket is found by the plugin's own resolver, whose
        candidates include the login session's path even when `XDG_RUNTIME_DIR`
        has been repointed.
        """
        if shutil.which("ydotool") is None:
            pytest.skip("ydotool is not installed")
        if gridydotool.injector_socket() is None:
            pytest.skip(
                "no ydotoold socket answers (Ubuntu packages one as a user unit); "
                "without a daemon a 1.0.x client has nothing to inject through"
            )
        monkeypatch.delenv("EASYSPEAK_GRID_BACKEND", raising=False)
        with caplog.at_level("INFO"):
            assert gridydotool._probe_injector() is True
        assert any("injections " in r.message for r in caplog.records)
        monkeypatch.setattr(gridydotool, "_probe_overlay", lambda: True)
        gridydotool.setup(core=None)
        assert gridydotool.active() is True

    def test_the_injector_gate_fails_closed_without_a_socket(
        self, monkeypatch, tmp_path, caplog
    ):
        """The other half of the gate against the same binary: pointed at a socket
        that cannot answer, the real client refuses to emit at all, and a gate
        that opened here would take the grid words for an injector that cannot
        inject. 0.1.8 emits without a daemon, so there is nothing to fail closed
        on there and the case skips by name."""
        if shutil.which("ydotool") is None:
            pytest.skip("ydotool is not installed")
        if gridydotool.dialect() != gridydotool.YDTOOL_DIALECT_MODERN:
            pytest.skip(
                "0.1.8's client emits without a daemon; nothing to fail closed on"
            )
        dead = tmp_path / ".ydotool_socket"
        monkeypatch.setattr(
            gridydotool, "ydotool_socket_candidates", lambda: [str(dead)]
        )
        with caplog.at_level("INFO"):
            assert gridydotool._probe_injector() is False
        assert any("no injector here" in r.message for r in caplog.records)


class TestForcedBackendArm:
    def test_reads_the_geometry_and_records_the_argv(
        self, session, tmp_path, monkeypatch, caplog
    ):
        """Arm A: the backend is forced, so no daemon is needed, and the overlay
        is observed through the plugin's own log records (its stdout belongs to
        the plugin, so the harness cannot read that stream itself).

        The argv is spelled out per dialect and the dialect is the one the
        installed client reports for itself, so on 26.04's ydotool 1.0.4 this
        pins `--absolute -x/-y` and the `0xC0` left click, and on 0.1.8 the
        positional move and `click 1`. Both are the *exact* strings the seam
        emits — a token the client would reject (`--delay`, a bare selector)
        fails here rather than at the pointer.
        """
        monkeypatch.setenv("EASYSPEAK_GRID_BACKEND", "uinput")
        monkeypatch.setenv("EASYSPEAK_GRID_DRY_RUN", "1")
        modern = gridydotool.dialect() == gridydotool.YDTOOL_DIALECT_MODERN
        expected_move = (
            "mousemove --absolute -x 1386 -y 300" if modern else "mousemove 1386 300"
        )
        expected_click = "click 0xC0" if modern else "click 1"

        commands = ["grid", "3 7 5", "click"]

        class FakeCore:
            def __init__(self):
                self.said = []

            def speak(self, text):
                self.said.append(text)

            def listen_modal(self, *_a, **_k):
                return iter(commands[1:])

        with caplog.at_level("INFO"):
            gridydotool.setup(FakeCore())
            assert gridydotool.active() is True
            gridydotool.handle(commands[0], gridydotool._core)

        ready = [r for r in caplog.records if "ready on" in r.message]
        assert ready, "the helper never reported ready"
        assert "1920" in ready[0].message and "1080" in ready[0].message
        dry = [r for r in caplog.records if "dry run:" in r.message]
        assert any(expected_move in r.message for r in dry), [r.message for r in dry]
        assert any(expected_click in r.message for r in dry), [r.message for r in dry]
        assert gridydotool._helper is None


@pytest.mark.skipif(
    os.environ.get("EASYSPEAK_GRID_LIVE") != "click",
    reason="live injection is opt-in: set EASYSPEAK_GRID_LIVE=click",
)
class TestLiveArm:
    def test_the_click_lands_in_the_expected_cell(self, session, tmp_path, monkeypatch):
        monkeypatch.setenv("EASYSPEAK_GRID_BACKEND", "uinput")
        repo_root = Path(gridydotool.__file__).parents[3]
        # The probe window runs under the system interpreter, not the venv the
        # test itself runs in: grid_python() is the plugin's own resolver for
        # "an interpreter with PyGObject and Gtk 4", which is exactly the probe
        # window's requirement, and under `just integration` python3 resolves to
        # the daemon venv (no PyGObject), so a bare `python3` here would die on
        # import while everything else worked.
        python = gridydotool.grid_python()
        assert python, "no interpreter can run the GTK4 probe window"

        # The bare session is captured before the probe window exists, and the
        # same view again once the probe has had the settle time below. Identical
        # pixels mean the probe never realised a surface - a GPU-less guest's
        # GTK4 may fail to get a GL/EGL context - and the click further down
        # would land on an empty scene. This runs before anything injects, so
        # that failure reports "the probe window never rendered" instead of the
        # lost click it would otherwise masquerade as.
        bare = tmp_path / "bare.png"
        _grim(session, bare)
        probe = subprocess.Popen(
            [python, str(repo_root / "tests" / "integration" / "grid_probe_window.py")],
            env=session,
            stdout=subprocess.PIPE,
            text=True,
        )
        time.sleep(1.5)
        before = tmp_path / "before.png"
        _grim(session, before)
        probe_rendered = bare.read_bytes() != before.read_bytes()
        assert probe_rendered, (
            "the probe window never rendered: the bare session and the spawned "
            "probe are pixel-identical, so a click could only land on an empty "
            "scene"
        )

        monkeypatch.setattr(gridydotool, "PROBE_TIMEOUT", 8.0)
        gridydotool.setup(core=None)
        assert gridydotool._start_helper() == (0, 0, 1920, 1080)
        gridydotool._bounds = (0, 0, 1920, 1080)
        for zone in (3, 7, 5):
            gridydotool._bounds = gridydotool.subdivide(gridydotool._bounds, zone)
            gridydotool._redraw(gridydotool._bounds)
        time.sleep(0.5)
        assert gridydotool._bounds == EXPECTED_CELL

        after = tmp_path / "after.png"
        _grim(session, after)
        assert before.read_bytes() != after.read_bytes(), "the overlay did not render"

        # The seat is asserted before anything is injected, and separately from
        # the click it has to route: with no input device in the compositor's own
        # list there is no route for an injected event to take, so a lost click
        # would be indistinguishable from a session that could never have
        # delivered one. `libinput` in `WLR_BACKENDS` inside an active session is
        # what puts devices here; the failure names that prerequisite instead of
        # leaving the reader to infer it from an empty press list later.
        seat = _sway_inputs(session)
        assert seat, (
            "the session has no input device, so no injected click can have a "
            "seat: sway needs its libinput backend (`WLR_BACKENDS="
            "headless,libinput`) inside an active session — a running seatd or a "
            "VT-enabled login session"
        )

        # The hide -> show lifecycle: hiding unmaps the surface and showing it
        # again remaps it, the case the helper's per-show region check exists
        # for. The fresh surface must still be click-through, and the injected
        # click further down is the observable: GDK keeps the region on the
        # surface and re-applies it to the re-created wl_surface, but only the
        # probe's press can prove it. The two screenshots keep a failed
        # re-present from passing silently, because a click would sail through
        # an absent overlay anyway.
        gridydotool._send({"cmd": "hide"})
        time.sleep(0.5)
        hidden = tmp_path / "hidden.png"
        _grim(session, hidden)
        assert hidden.read_bytes() != after.read_bytes(), "the overlay did not hide"
        gridydotool._redraw(gridydotool._bounds)
        time.sleep(0.5)
        shown_again = tmp_path / "shown-again.png"
        _grim(session, shown_again)
        assert shown_again.read_bytes() != hidden.read_bytes(), (
            "the overlay did not come back"
        )

        x, y = gridydotool.center(EXPECTED_CELL)
        print(f"about to click at {x},{y}")  # noqa: T201
        gridydotool.move_to(x, y)
        gridydotool.click("click")
        time.sleep(0.5)

        probe.terminate()
        out, _ = probe.communicate(timeout=5)
        events = [json.loads(line) for line in (out or "").splitlines()]
        presses = [e for e in events if e["event"] == "press"]
        # With the probe window proven on screen and the seat proven before the
        # injection, an empty press list has one reading left: a click that had a
        # real route and was swallowed before the probe observed it — the reading
        # that points at the overlay's input region. The report still names where
        # its input answer came from, because a session whose devices came and
        # went (0.1.8's daemonless mode destroys the device with the client) can
        # read as empty after the fact; on 1.0.x the daemon's device persists, so
        # that ambiguity is not in play.
        session_input = _input_report(session)
        assert presses, (
            f"the probe window saw no click: {events}; "
            f"probe window on screen: {probe_rendered}; {session_input}"
        )
        left, top, width, height = EXPECTED_CELL
        px, py = presses[-1]["x"], presses[-1]["y"]
        assert left - EXPECTED_TOLERANCE <= px <= left + width + EXPECTED_TOLERANCE
        assert top - EXPECTED_TOLERANCE <= py <= top + height + EXPECTED_TOLERANCE
```

- [ ] **Step 3: Run the harness without the live arm**

Run: `just integration -k gridydotool -v`
Expected: PASS where the prerequisites exist — the gate's fail-closed case and the forced-backend arm; individual gate tests SKIP by name when `ydotool` or a daemon socket is absent (1.0.x has no per-invocation fallback), and the live class SKIPS with its reason. On a machine without sway/ydotool/grim the whole file skips.

- [ ] **Step 4: Run the live arm once, deliberately**

Run: `EASYSPEAK_GRID_LIVE=click just integration -k LiveArm -v -s`
Expected: PASS, and the printed coordinates are the ones about to be clicked at. The arm first asserts the probe window rendered and the session has a seat, so a lost click reports which premise it lost; then it drives hide → show before injecting. This is the only check that touches real input; on a laptop dock, expect the cursor to move.

- [ ] **Step 5: Document the harness**

In `tests/integration/README.md`, extend the layout listing with both new files and add a short section naming the prerequisites in one place (sway, ydotool, grim, the layer-shell stack, and a `ydotoold` for the gate's passing shape and the live arm on 1.0.x) plus the two environment variables the harness reads: `EASYSPEAK_GRID_LIVE` and `EASYSPEAK_GRID_BACKEND`.

- [ ] **Step 6: Commit**

```bash
hug a tests/integration
hug c -F - <<'EOF'
test(gridydotool): the headless-sway harness, in two shapes and two arms

The gate is asserted where it needs no privileges: with no daemon the plugin must
stay inert and leave the words to mousegrid. The overlay and the exact argv are
asserted under a forced backend, which is what keeps this arm runnable in CI
without /dev/uinput, and they are observed through the plugin's own log records
because the helper's stdout belongs to the plugin.

The live arm is opt-in and hand-computes its expectation: for a pinned 1920x1080
output the sequence 3 7 5 must land in (1351, 280, 71, 40), and the premise is
asserted before the arithmetic so a config drift fails loudly instead of reading
as a coordinate mystery. The tolerance is bounded to a quarter of the cell's
smaller dimension, so widening it can never be the reason the assertion passes.

Injection is kernel-global, so even in this session the live arm moves the real
cursor; that is why it prints its target before it clicks.

Co-authored-by: CommandCodeBot <noreply@commandcode.ai>
EOF
```

---

### Task 7: Docs

**Files:**
- Modify: `docs/reference/plugins.md`, `docs/how-it-works.md`, `docs/commands.md`, `docs/troubleshooting.md`, `docs/compatibility.md`, `docs/usage.md`, `docs/index.md`, `README.md`
- Modify: `tests/packaging/test_python_wheel.sh`

**Interfaces:** consumes the finished plugin; produces no code.

- [ ] **Step 1: Add the reference entry**

In `docs/reference/plugins.md`, add the entry alongside the others:

```markdown
## gridydotool

::: plugins.gridydotool
```

- [ ] **Step 2: Update the plugin tree**

In `docs/how-it-works.md`, add the package to the tree next to `mousegrid/`:

```text
│       ├── gridydotool/        # Grid overlay + kernel-level injection (layer-shell + ydotool)
```

- [ ] **Step 3: Mark the verbs this backend cannot honour**

In `docs/commands.md`, under the grid verb table, add a note so the table stops promising every verb on every backend:

```markdown
On a compositor where EasySpeak's Shell extension cannot run (COSMIC, wlroots, KDE),
the grid is served by the `gridydotool` backend instead. It understands the
navigation, click and close verbs above; `nudge`, `scroll` and `mark`/`drag` are
not available there yet.
```

- [ ] **Step 4: Say what a missing stack looks like**

In `docs/troubleshooting.md`, add a section after the existing grid one:

````markdown
## Mouse grid: the grid never appears on a non-GNOME desktop

The grid needs either the Shell extension (GNOME 47+) or the `gridydotool`
backend, which needs `ydotool` installed (its own tool list must offer
`mousemove`, `click` and `type`), a compositor with layer-shell support (COSMIC,
wlroots, KDE) and an interpreter with PyGObject and the gtk4-layer-shell
typelib. `ydotool` also needs write access to `/dev/uinput`.

`ydotool` ships in two command dialects, and the backend reads which one is
installed from the client itself:

- **ydotool 0.1.8 and older** (Ubuntu 25.10): a running `ydotoold` is optional.
  With no daemon the client builds a uinput device per invocation and says so
  ("ydotoold backend unavailable (may have latency+delay issues)") — the
  latency caveat its own notice names, which the plugin's log calls the
  per-invocation mode.
- **ydotool 1.0.x** (Ubuntu 26.04): the per-invocation fallback is gone. With no
  daemon the client exits 2 ("failed to connect socket") and emits nothing, so a
  running `ydotoold` is required and the backend's capability probe fails closed
  without one — the log says why, and `mousegrid` keeps the words rather than
  promising a grid that cannot click.

Not every distro ships a `ydotoold` binary with the client (Ubuntu 25.10's
`ydotool` package is CLI-only and the daemon is a separate package; 26.04 ships
the daemon in the same package). If your distro has the daemon but no user unit,
`systemctl --user enable --now ydotoold` answering "Unit ydotoold.service not
found" means installing this as `~/.config/systemd/user/ydotoold.service`:

```ini
[Unit]
Description=ydotoold - persistent uinput device for ydotool

[Service]
ExecStart=/usr/bin/ydotoold --socket-path=/tmp/.ydotool_socket --socket-perm=0600
Restart=on-failure

[Install]
WantedBy=default.target
```

Then `systemctl --user daemon-reload` once, and `systemctl --user enable --now
ydotoold`. The socket path and its 0600 mode are what 0.1.8's client hardcodes,
which is also why the daemon has to run as your user rather than as root: a root
daemon's socket is unreachable by your client. On 1.0.x the client defaults to
`$XDG_RUNTIME_DIR/.ydotool_socket` and honours `YDOTOOL_SOCKET`; the backend
probes that path, the login session's own `/run/user/<uid>/` path and 0.1.8's
`/tmp/.ydotool_socket`, so a daemon on either default is found.

If `ydotool` or the layer-shell stack is missing, or (on ydotool 1.0.x) no
daemon answers, the daemon logs `grid backend inert` at startup with the reason,
says nothing out loud, and `mousegrid` keeps the words. On 0.1.8 a missing
`ydotoold` alone does not: the log names the per-invocation mode and the grid
still answers.
````

- [ ] **Step 5: Document the requirements and the risk**

In `docs/compatibility.md`, extend the mouse-grid row's note to name the second backend and its requirements, and in `docs/usage.md` add the two user-facing knobs to the environment-variable section:

```markdown
| Variable                 | Default | Effect                                                         |
| ------------------------ | ------- | -------------------------------------------------------------- |
| `EASYSPEAK_GRID_BACKEND` | probed  | `uinput` or `gnome`: force one grid backend instead of probing. `uinput` skips the probes entirely. |
| `EASYSPEAK_GRID_PYTHON`  | probed  | Path to an interpreter with PyGObject and the gtk4-layer-shell typelib, if the probe cannot find one. |
```

with a short paragraph above them saying when the backend activates (layer-shell + `ydotool` + `/dev/uinput`, and a daemon on 1.0.x) and, on the compatibility row, a footnote naming the same requirements.

Also state the security note where the requirements are described: installing
`ydotool` — and running `ydotoold` when the distro offers it — with `/dev/uinput`
access is input-injection grade access, the same class as the `input` group that
hold-to-dictate needs.

- [ ] **Step 6: Fix the feature lines**

In `docs/index.md` and `README.md`, adjust the mouse-grid line so it does not imply the Shell extension is the only route (for example: "Mouse grid (Shell extension on GNOME 47+, or the layer-shell + ydotool backend elsewhere)").

- [ ] **Step 7: Add the wheel-test member**

In `tests/packaging/test_python_wheel.sh`, next to the existing locale assertion:

```bash
has locale/de/LC_MESSAGES/gridydotool.po
```

- [ ] **Step 8: Run the docs checks**

Run: `just check-docs`
Expected: PASS (docstring linting, markdown linting, link checking).

- [ ] **Step 9: Commit**

```bash
hug a docs tests/packaging/test_python_wheel.sh
hug c -F - <<'EOF'
docs(gridydotool): reference, requirements, verb caveats and troubleshooting

The doc perimeter a plugin addition touches, in one pass: the reference entry,
the plugin tree in how-it-works, the grid verb table's caveat for the verbs this
backend cannot honour (scroll is not expressible in ydotool 0.1.8 at all), the
troubleshooting section for a missing stack on a non-GNOME desktop, the
compatibility and usage pages, the feature lines that implied the Shell extension
was the only route, and the wheel test's member list.

The requirements section carries the security note as well: ydotool plus
/dev/uinput access is input-injection grade, the same class as the input group
hold-to-dictate needs.

Co-authored-by: CommandCodeBot <noreply@commandcode.ai>
EOF
```

---

### Task 8: Delivery checks and the upstream report

**Files:**
- Modify: `docs/superpowers/specs/2026-10-14-prototype-compositor-agnostic-easyspeak-backend-layer-shell-overlay-ydotool-design.md` (only to tick anything the implementation settles)

**Interfaces:** consumes everything; produces the delivery evidence.

- [ ] **Step 1: Run the full gate set**

Run: `just format && just lint && just types && just pytest && just check-translations && just check-python-packages && just test-js`
Expected: PASS. Coverage stays at or above the repo's 99% floor; the plugin's modules are its own lines.

- [ ] **Step 2: Prove the core is untouched**

Run: `git diff origin/main -- src/core`
Expected: empty output.

- [ ] **Step 3: Smoke the daemon with the plugin present**

Run: `uv run easyspeak --help` and then, in a foreground session, watch the startup log for:

```text
  ✓ Loaded: GridYdotool
grid backend inert
```

On this workstation the second line is correct: GNOME 42 has no layer-shell and there is no `ydotoold` yet, so the plugin must be inert and `mousegrid` must keep the words. Then say "grid" and confirm the answer comes from the extension backend's failure path, unchanged.

- [ ] **Step 4: Install the runtime pieces and re-check the gate**

Run (each with `sudo` where needed):

```bash
sudo apt install sway ydotool just
systemctl --user start ydotoold   # the unit Task 7 ships when the distro has none
```

Then re-run Step 3 and confirm the log now reads `grid backend active` **only** on a compositor with layer-shell — which on this box means after the COSMIC migration, not now. On GNOME 42 the overlay probe still fails, which is the gate's designed answer here.

- [ ] **Step 5: Record what the probes settled**

For each open item in the spec that the implementation answered (the click-through mechanism, the measured double-click gap, the acceleration tolerance, whether headless sway picks up a late uinput device), edit that item to state the measured result instead of the question.

- [ ] **Step 6: Draft the upstream issue**

Write the issue body to `docs/upstream-issue-draft.md` with: the ydotool 0.1.8 semantics table (argc exit codes, clamp-then-relative `mousemove`, the 0600 hardcoded socket, no wheel verb), the layer-shell-on-Smithay-but-not-GNOME boundary as the natural gate, the packaging friction a plugin addition carries (roster test, packages list, four catalogs, wheel test), and the security note's wording. Do not file anything at gtk4-layer-shell: its repository forbids AI-written production code and mostly-AI issue text.

- [ ] **Step 7: Commit**

```bash
hug a docs tests pyproject.toml src
hug c -F - <<'EOF'
chore(gridydotool): gate results, settled probes and the upstream draft

The full gate set passes, src/core is untouched by diff, the daemon smoke shows
the plugin loaded and inert on this box (GNOME 42 has no layer-shell and no
ydotoold yet, which is exactly what the gate is for), and each open item the
implementation answered now records its measured result rather than the question.

The upstream issue draft carries the four findings worth reporting: ydotool
0.1.8's argc exit codes and clamp-then-relative mousemove, the socket's hardcoded
path and permissions, the layer-shell support boundary, and the packaging work a
plugin addition needs.

Co-authored-by: CommandCodeBot <noreply@commandcode.ai>
EOF
```

---

## Self-review notes

- **Spec coverage:** gate (Task 2), zone math and parity (Task 1), routing and the
  `handle` contract (Tasks 2-3), injection outcomes and `--delay 0` (Tasks 2-3),
  helper channel and its five cases (Task 4), overlay rendering and click-through
  (Task 5), harness in two shapes and two arms with the literal and the premise
  (Task 6), catalogs (Tasks 2, 3 and 4), doc perimeter (Task 7, plus the harness
  README in Task 6), delivery and upstream report (Task 8).
- **Placeholders:** none — every code step carries the code, every command step the
  command and its expected result.
- **Type consistency:** `_run_bounded(args, timeout=INJECT_TIMEOUT)` returns
  `"ran" | "errored" | "unknown"` everywhere it is used; `grid_python()` returns a
  path or None; `_start_helper()` returns `(x, y, w, h)` or None; `_bounds` is
  always a 4-tuple in monitor-local coordinates; `stop_helper(reason)` is the only
  path that reaps the helper.
- **Known red window:** between Task 2 and Task 4 the catalogs grow as strings
  appear, and each task regenerates them, so no commit leaves `check-translations`
  failing.
