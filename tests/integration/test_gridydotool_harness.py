"""The gridydotool end-to-end loop, in a headless sway session.

Checks: a failed injector probe keeps `mousegrid` in charge (no privileges
needed), the injector gate passes daemonless on a real ydotool (the socket is a
logged mode, not a condition), the overlay renders and the argv is exact under a
forced backend (the dry-run arm, which is the arm CI can run), and, only when
EASYSPEAK_GRID_LIVE is set, that a click lands where the grid aimed it.

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
    monkeypatch.setenv("WLR_BACKENDS", "headless")
    # `WLR_LIBINPUT_NO_DEVICES=1` asks wlroots for a libinput backend with no
    # devices at all: right for the arms that only look at pixels, whose
    # session nothing real should be able to perturb, and fatal for the one
    # arm whose click must be routed through the compositor's seat. So the
    # flag is set only when the live opt-in is absent; a value inherited from
    # the caller is dropped for the live arm rather than trusted, so its seat
    # exists by the harness's own construction.
    if os.environ.get("EASYSPEAK_GRID_LIVE") == "click":
        monkeypatch.delenv("WLR_LIBINPUT_NO_DEVICES", raising=False)
    else:
        monkeypatch.setenv("WLR_LIBINPUT_NO_DEVICES", "1")
    sway = subprocess.Popen(
        ["sway", "-c", str(config)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
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
            pytest.skip("sway exited before it bound a socket")
        time.sleep(0.2)
    if display is None:
        sway.terminate()
        pytest.skip("sway never created a Wayland socket")
    monkeypatch.setenv("WAYLAND_DISPLAY", display)
    yield os.environ
    sway.terminate()
    sway.wait(timeout=5)


def _grim(env, path):
    subprocess.run(["grim", str(path)], env=env, check=False, timeout=10)


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


def _input_report(env):
    """What input the session had, one line of diagnosis for a lost click.

    The compositor is asked first: `swaymsg -t get_inputs` is its own list, and
    the one that decides whether an injected click has a route, because with
    `WLR_LIBINPUT_NO_DEVICES=1` it is empty while `/proc/bus/input/devices`
    still lists the kernel's devices. That gap is the "no seat" reading this
    report exists to expose; the kernel list is the named fallback, and both
    failures are tolerated because this runs to explain a failed assertion and
    must not raise an error of its own.

    Sway's IPC socket is named inside `XDG_RUNTIME_DIR` but is never exported
    to us (only sway's own children inherit `SWAYSOCK`), so it is discovered
    here the same way the session fixture discovers the Wayland socket. The
    answer is a snapshot: a daemonless ydotool destroys its per-invocation
    device when its client exits, so "no devices" can also mean "the device
    came and went", which is why the line names its source rather than
    judging.
    """
    runtime = env.get("XDG_RUNTIME_DIR")
    sockets = sorted(Path(runtime).glob("sway-ipc.*.sock")) if runtime else []
    ask = None
    if sockets and shutil.which("swaymsg", path=env.get("PATH")):
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
            ask = None
    if ask is not None and ask.returncode == 0:
        try:
            devices = [
                device.get("identifier", "?") for device in json.loads(ask.stdout)
            ]
        except (ValueError, AttributeError):
            devices = None
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

    def test_the_injector_gate_passes_daemonless(self, monkeypatch, caplog):
        """An Ubuntu box may have no `ydotoold` installed at all (the `ydotool`
        package ships only the client), and the VM measured the daemonless path
        working: the socket is a logged mode, not a condition. This runs the
        real `ydotool help` and `type ""` probe, so it skips where ydotool is
        absent or /dev/uinput is not reachable."""
        if shutil.which("ydotool") is None:
            pytest.skip("ydotool is not installed")
        if not os.access("/dev/uinput", os.W_OK):
            pytest.skip("no /dev/uinput access for the daemonless probe")
        monkeypatch.delenv("EASYSPEAK_GRID_BACKEND", raising=False)
        with caplog.at_level("INFO"):
            assert gridydotool._probe_injector() is True
        assert any("injections " in r.message for r in caplog.records)
        monkeypatch.setattr(gridydotool, "_probe_overlay", lambda: True)
        gridydotool.setup(core=None)
        assert gridydotool.active() is True


class TestForcedBackendArm:
    def test_reads_the_geometry_and_records_the_argv(
        self, session, tmp_path, monkeypatch, caplog
    ):
        """Arm A: the backend is forced, so no daemon is needed, and the overlay
        is observed through the plugin's own log records (its stdout belongs to
        the plugin, so the harness cannot read that stream itself)."""
        monkeypatch.setenv("EASYSPEAK_GRID_BACKEND", "uinput")
        monkeypatch.setenv("EASYSPEAK_GRID_DRY_RUN", "1")

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
        assert any("mousemove" in r.message for r in dry)
        assert any("click 1" in r.message for r in dry)
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
        # With the probe window proven on screen, an empty press list has two
        # readings left and this report tells them apart: a compositor that saw
        # no input device had no seat to route the click through, while one
        # that saw a device means the click had a real route and was swallowed
        # before the probe observed it - the reading that points at the
        # overlay's input region. The report names its source, because a
        # daemonless ydotool destroys its per-invocation device when its client
        # exits and the snapshot can then read as empty for that reason too.
        session_input = _input_report(session)
        assert presses, (
            f"the probe window saw no click: {events}; "
            f"probe window on screen: {probe_rendered}; {session_input}"
        )
        left, top, width, height = EXPECTED_CELL
        px, py = presses[-1]["x"], presses[-1]["y"]
        assert left - EXPECTED_TOLERANCE <= px <= left + width + EXPECTED_TOLERANCE
        assert top - EXPECTED_TOLERANCE <= py <= top + height + EXPECTED_TOLERANCE
