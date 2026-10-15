# Integration tests

Real-execution checks for the boundaries the unit suite can only mock: the
text-to-speech pipeline, the systemd user unit, and the gridydotool loop. The
unit tests prove the right argv is *built* or the right `systemctl` call is
*made*; these run the real `piper`/audio-player binaries, the genuine
`systemctl --user`, and the real overlay + injector pair to prove those
invocations are actually accepted.

## How they run

They drive external commands for real, so this folder is **excluded from
default discovery** (see `pyproject.toml`) and is opt-in:

```sh
just integration       # uv run --extra=integration pytest tests/integration
just integration -q    # extra pytest args pass through
```

Each test **skips gracefully** when the binary or server it needs is absent
(missing voice model, no PulseAudio/PipeWire, no user systemd manager, no
sway/ydotool/gtk4-layer-shell), so on an unprepared machine — including headless
CI — the folder degrades to a no-op rather than a failure.

## Layout

```sh
test_speech_pipeline.py        # piper synthesis + player playback for real
test_gnome_extension_unit.py   # install_refresh_unit enabled by real systemd
test_gridydotool_harness.py    # gridydotool: gate, overlay + argv, live click arm
grid_probe_window.py           # fullscreen GTK4 window reporting pointer events as JSON
conftest.py                    # stubs heavy deps; skip fixtures for audio/systemd
```

## The gridydotool harness

`test_gridydotool_harness.py` asserts the gate stays inert without a daemon,
observes the overlay geometry and the exact ydotool argv under a forced backend,
and — only when opted in — proves that a click lands in the cell the utterance
aimed at, by watching `grid_probe_window.py` inside a headless sway session the
harness starts and tears down itself. When a prerequisite is absent the file
skips, naming what is missing.

Its prerequisites, in one place:

- `sway` (the headless compositor the harness starts), `ydotool` and `grim` on
  `PATH`.
- An interpreter with PyGObject and the gtk4-layer-shell typelib. Where the
  library is not packaged, the reference setup is an uninstalled build tree:
  point `EASYSPEAK_GRID_TYPELIB` at its `src` directory (the harness exports it
  as both `GI_TYPELIB_PATH` and `LD_LIBRARY_PATH`), or `EASYSPEAK_GRID_PYTHON`
  at an interpreter that already has the library and the typelib.
- A running `ydotoold`, **for the live arm only**.

Two environment variables the harness reads: `EASYSPEAK_GRID_LIVE=click` opts
into the live click arm, and `EASYSPEAK_GRID_BACKEND=uinput` (set by the harness
itself) forces the plugin active without probing a daemon. The live arm injects
through `ydotool`, which is kernel-global, so it stays opt-in even though it runs
inside the harness's own session.
