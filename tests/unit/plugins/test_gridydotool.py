"""Tests for the gridydotool plugin: the zone math, its parity with mousegrid,
and the parser that turns an utterance into zone numbers."""

import ctypes
import ctypes.util
import importlib.util
import io
import json
import os
import sys
import time
import types
from pathlib import Path
from unittest.mock import Mock

import pytest
from easyspeak.plugins import gridydotool, mousegrid
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
def _reap_leaked_helper():
    """No test — passing or failing — leaves a spawned helper behind."""
    yield
    if g._helper is not None:
        g.stop_helper("test cleanup")


@pytest.fixture(autouse=True)
def _fresh_client_state(monkeypatch):
    """The dialect is cached module state; every test resolves its own."""
    monkeypatch.setattr(g, "_dialect", None)
    monkeypatch.setattr(g, "_monitor_origin", (0, 0))


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
