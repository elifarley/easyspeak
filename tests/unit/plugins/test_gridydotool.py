"""Tests for the gridydotool plugin: the zone math, its parity with mousegrid,
and the parser that turns an utterance into zone numbers."""

from unittest.mock import Mock

import pytest
from easyspeak.plugins import gridydotool, mousegrid
from easyspeak.plugins import gridydotool as g


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
