"""The gridydotool end-to-end loop, in a headless sway session.

Three checks: the gate stays inert when there is no daemon (no privileges
needed), the overlay renders and the argv is exact under a forced backend (the
dry-run arm, which is the arm CI can run), and, only when EASYSPEAK_GRID_LIVE is
set, that a click lands where the grid aimed it.

The literal in the live arm is hand-computed. Deriving it with the same algorithm
the plugin uses would make the assertion agree with any shared off-by-one, so the
premise (a pinned 1920x1080 output) is asserted first and the algorithmic
prediction is kept only as a cross-check.

Injection is kernel-global: even inside this session, arm B moves the real
cursor and clicks wherever it aims on the live desktop. That is why it is opt-in.
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


class TestGateWithoutADaemon:
    def test_the_plugin_stays_inert(self, monkeypatch):
        """A box with no ydotoold must keep mousegrid in charge: this is the
        gate assertion that needs no privileges at all."""
        monkeypatch.delenv("EASYSPEAK_GRID_BACKEND", raising=False)
        monkeypatch.setattr(gridydotool, "_probe_overlay", lambda: True)
        monkeypatch.setattr(gridydotool, "_probe_injector", lambda: False)
        gridydotool.setup(core=None)
        assert gridydotool.active() is False
        assert gridydotool.handle("grid", core=None) is None


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
        probe = subprocess.Popen(
            [python, str(repo_root / "tests" / "integration" / "grid_probe_window.py")],
            env=session,
            stdout=subprocess.PIPE,
            text=True,
        )
        time.sleep(1.5)
        before = tmp_path / "before.png"
        _grim(session, before)

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
        assert presses, f"the probe window saw no click: {events}"
        left, top, width, height = EXPECTED_CELL
        px, py = presses[-1]["x"], presses[-1]["y"]
        assert left - EXPECTED_TOLERANCE <= px <= left + width + EXPECTED_TOLERANCE
        assert top - EXPECTED_TOLERANCE <= py <= top + height + EXPECTED_TOLERANCE
