"""Tests for the gridydotool plugin: the zone math, its parity with mousegrid,
and the parser that turns an utterance into zone numbers."""

import importlib.util
import io
import json
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from easyspeak.plugins import gridydotool, mousegrid
from easyspeak.plugins import gridydotool as g


@pytest.fixture(autouse=True)
def _reap_leaked_helper():
    """No test — passing or failing — leaves a spawned helper behind."""
    yield
    if g._helper is not None:
        g.stop_helper("test cleanup")


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


@pytest.fixture
def core():
    """A core stub with the plugin-facing surface this module touches."""
    stub = Mock()
    stub.speak = Mock()
    stub.listen_modal = Mock(return_value=iter([]))
    return stub


class TestOutcomeClassification:
    """`ydotool` exits with argc, so the exit code alone says nothing; the seam
    returns one of three outcomes and the tests pin all three."""

    def _result(self, returncode=3, stderr="", stdout=""):
        return Mock(returncode=returncode, stderr=stderr, stdout=stdout)

    def test_ran_is_the_invocations_own_success_code(self, monkeypatch):
        monkeypatch.setattr(g, "_spawn", lambda *a, **k: self._result(3))
        assert g._run_bounded(["ydotool", "mousemove_relative", "0", "0"]) == "ran"

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

    def test_notice_is_unknown_not_ran(self, monkeypatch):
        """A fallback notice means the client built a one-shot device, which is
        exactly the case `ran` excludes."""
        monkeypatch.setattr(
            g,
            "_spawn",
            lambda *a, **k: self._result(
                3, stderr="ydotool: notice: ydotoold backend unavailable ..."
            ),
        )
        assert g._run_bounded(["ydotool", "mousemove_relative", "0", "0"]) == "unknown"

    def test_dry_run_records_without_spawning(self, monkeypatch):
        spawn = Mock()
        monkeypatch.setattr(g, "_spawn", spawn)
        monkeypatch.setenv("EASYSPEAK_GRID_DRY_RUN", "1")
        assert g._run_bounded(["ydotool", "click", "1"]) == "ran"
        spawn.assert_not_called()


class TestAdapterArgv:
    def test_move_applies_the_monitor_origin(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            g, "_run_bounded", lambda args, **k: seen.append(args) or "ran"
        )
        g._monitor_origin = (1920, 0)
        g.move_to(1351, 280)
        assert seen == [["ydotool", "mousemove", "3271", "280", "--delay", "0"]]

    def test_click_buttons(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            g, "_run_bounded", lambda args, **k: seen.append(args) or "ran"
        )
        g.click("click")
        g.click("right")
        g.click("middle")
        assert seen == [
            ["ydotool", "click", "1", "--delay", "0"],
            ["ydotool", "click", "2", "--delay", "0"],
            ["ydotool", "click", "3", "--delay", "0"],
        ]

    def test_double_click_is_two_calls_in_order(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            g, "_run_bounded", lambda args, **k: seen.append(args) or "ran"
        )
        g.double_click()
        assert [a[2] for a in seen] == ["1", "1"]


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

    def test_injector_probe_requires_the_socket(self, monkeypatch):
        monkeypatch.setattr(g.shutil, "which", lambda name: f"/usr/bin/{name}")
        # `connect` raises, so the probe fails on the socket alone and never
        # spawns: deterministic also on a host with ydotool and a live daemon.
        stub = Mock()
        stub.connect.side_effect = OSError("connection refused")
        monkeypatch.setattr(g.socket, "socket", Mock(return_value=stub))
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


class TestRedraw:
    def test_sends_the_show_command(self, monkeypatch):
        sent = []
        # `_send` is Task 4's seam and is not defined at this commit; the stub
        # is injected anyway because the show payload is this test's subject.
        monkeypatch.setattr(g, "_send", sent.append, raising=False)
        g._redraw((0, 0, 900, 600))
        assert sent == [{"cmd": "show", "bounds": [0, 0, 900, 600]}]


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


@pytest.fixture
def fake_helper(monkeypatch, tmp_path):
    """Point the plugin at a fake helper written from `script`."""

    def _point(script):
        path = tmp_path / "helper.py"
        path.write_text(script, encoding="utf-8")
        monkeypatch.setattr(g, "HELPER", str(path))
        monkeypatch.setattr(g, "grid_python", lambda: "python3")

    return _point


@pytest.fixture
def start_helper(fake_helper):
    """Run the handshake against a fake helper written from `script`."""

    def _start(script):
        fake_helper(script)
        return g._start_helper()

    return _start


class TestHelperLifecycle:
    """Closing is idempotent: every mode exit closes the session twice.

    A dismissal runs `_dismiss()` → `_close_session()`, then `_open_session`'s
    `finally` calls it again, and the same two calls happen when no helper was
    ever started.
    """

    def _ready_script(self):
        return (
            "import json, sys\n"
            "print(json.dumps({'event': 'ready', 'monitor': [0, 0, 900, 600]}),"
            " flush=True)\n"
            "sys.stdin.readline()\n"
        )

    def test_a_real_session_that_ends_on_idle_still_closes(self, core, fake_helper):
        """Nothing dismissed the grid, so `_open_session`'s `finally` is what
        has to reap the helper and drop the session."""
        fake_helper(self._ready_script())
        core.listen_modal = Mock(return_value=iter([]))
        assert g._open_session(core) == "done"
        assert g._helper is None
        assert g._grid_open is False
        assert [call.args[0] for call in core.speak.call_args_list] == [g._("Grid")]

    def test_a_real_session_dismisses_then_closes_again(self, core, fake_helper):
        """The production shape of the double close: `_run_mode` dismisses the
        grid, then the `finally` closes the session a second time."""
        fake_helper(self._ready_script())
        core.listen_modal = Mock(return_value=iter(["close"]))
        assert g._open_session(core) == "done"
        assert g._helper is None
        assert g._grid_open is False
        assert [call.args[0] for call in core.speak.call_args_list] == [
            g._("Grid"),
            g._("Grid closed"),
        ]

    def test_closing_twice_reaps_once_and_survives_the_second_call(self, start_helper):
        assert start_helper(
            "import json, sys\n"
            "print(json.dumps({'event': 'ready', 'monitor': [0, 0, 8, 8]}), flush=True)\n"
            "sys.stdin.readline()\n",
        ) == (0, 0, 8, 8)
        helper = g._helper
        g._close_session()
        g._close_session()
        assert g._helper is None
        assert helper.poll() is not None

    def test_closing_twice_writes_one_hide(self, monkeypatch):
        stdin = io.StringIO()
        monkeypatch.setattr(g, "_helper", Mock(stdin=stdin, poll=Mock(return_value=0)))
        g._close_session()
        g._close_session()
        assert stdin.getvalue() == '{"cmd": "hide"}\n'

    def test_closing_with_nothing_running_is_a_no_op(self, monkeypatch):
        monkeypatch.setattr(g, "_helper", None)
        assert g._send({"cmd": "hide"}) is False
        g._close_session()
        g._close_session()

    def test_a_helper_that_ignores_sigterm_is_killed(
        self, monkeypatch, start_helper, caplog
    ):
        monkeypatch.setattr(g, "REAP_GRACE", 0.2)
        assert start_helper(
            "import json, signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print(json.dumps({'event': 'ready', 'monitor': [0, 0, 8, 8]}), flush=True)\n"
            "time.sleep(60)\n",
        ) == (0, 0, 8, 8)
        helper = g._helper
        with caplog.at_level("WARNING"):
            g.stop_helper("stubborn")
        assert helper.poll() is not None
        assert g._helper is None
        assert any("ignored SIGTERM" in r.message for r in caplog.records)

    def test_a_broken_pipe_swallows_the_command(self, monkeypatch, caplog):
        """A helper that died between the handshake and a command is not a
        crash: the write fails, the line is dropped, and the log says so."""
        stdin = Mock()
        stdin.write.side_effect = BrokenPipeError("the helper is gone")
        monkeypatch.setattr(g, "_helper", Mock(stdin=stdin, poll=Mock(return_value=1)))
        with caplog.at_level("WARNING"):
            assert g._send({"cmd": "hide"}) is False
        assert any("the helper is gone" in r.message for r in caplog.records)


class TestHelperStartFailures:
    def test_no_interpreter_means_no_helper(self, monkeypatch):
        spawn = Mock()
        monkeypatch.setattr(g, "grid_python", lambda: None)
        monkeypatch.setattr(g.subprocess, "Popen", spawn)
        assert g._start_helper() is None
        spawn.assert_not_called()

    def test_a_helper_that_cannot_spawn_is_logged(self, monkeypatch, caplog):
        monkeypatch.setattr(g, "grid_python", lambda: "python3")
        monkeypatch.setattr(g.subprocess, "Popen", Mock(side_effect=OSError("gone")))
        with caplog.at_level("WARNING"):
            assert g._start_helper() is None
        assert g._helper is None
        assert any("could not start" in r.message for r in caplog.records)

    def test_a_ready_event_without_geometry_is_refused(self, start_helper, caplog):
        with caplog.at_level("WARNING"):
            assert (
                start_helper(
                    "import json, time\n"
                    "print(json.dumps({'event': 'ready', 'monitor': []}), flush=True)\n"
                    "time.sleep(60)\n",
                )
                is None
            )
        assert g._helper is None
        assert any("no usable geometry" in r.message for r in caplog.records)

    def test_an_event_before_ready_is_logged_and_skipped(self, start_helper, caplog):
        with caplog.at_level("DEBUG"):
            assert start_helper(
                "import json, sys\n"
                "print(json.dumps({'event': 'presented'}), flush=True)\n"
                "print(json.dumps({'event': 'ready', 'monitor': [0, 0, 10, 10]}),"
                " flush=True)\n"
                "sys.stdin.readline()\n",
            ) == (0, 0, 10, 10)
        assert any("presented" in r.message for r in caplog.records)
        g.stop_helper("test")

    def test_open_session_speaks_when_the_helper_cannot_start(self, monkeypatch, core):
        monkeypatch.setattr(g, "_start_helper", lambda: None)
        assert g._open_session(core) is None
        core.speak.assert_called_once_with("I couldn't show the grid.")


class TestReadUntil:
    def test_a_deadline_that_has_passed_gives_up(self, tmp_path):
        path = tmp_path / "stream"
        path.write_text("", encoding="utf-8")
        with path.open() as stream:
            assert g.read_until(stream, 0) is None

    def test_a_stream_that_cannot_be_polled_gives_up(self):
        stream = Mock()
        stream.fileno.side_effect = ValueError("closed")
        assert g.read_until(stream, time.monotonic() + 1) is None


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

    def test_probe_mode_reports_unsupported_without_a_display(self):
        overlay = _overlay_module()
        assert overlay.main(["--probe"]) == 1

    def test_the_module_imports_without_gi(self):
        assert _overlay_module() is not None


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
