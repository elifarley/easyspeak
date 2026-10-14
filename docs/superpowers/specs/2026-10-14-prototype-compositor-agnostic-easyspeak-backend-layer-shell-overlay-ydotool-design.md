# gridydotool — compositor-agnostic grid backend (GTK4 layer-shell overlay + ydotool)

Status: design approved in brainstorm 2026-10-14 (four user decisions; see
Decisions for what was chosen and what was rejected).
Intent: `docs/superpowers/intents/2026-10-14-prototype-compositor-agnostic-easyspeak-backend-layer-shell-overlay-ydotool-intent.md`
Upstream tracker: [elifarley/system-priv#69](https://github.com/elifarley/system-priv/issues/69)
(decision context: [elifarley/system-priv#68](https://github.com/elifarley/system-priv/issues/68))

## Summary

A new bundled plugin, `gridydotool`, that gives EasySpeak a pointer grid which
does not depend on the GNOME Shell extension: the numbered overlay is a
`wlr-layer-shell` surface (GTK4 + gtk4-layer-shell) and the pointer is moved and
clicked through ydotool, which injects at kernel level via `uinput`. The plugin
keeps every piece of logic (routing, zone math, session state) in the daemon
process, and delegates only rendering to a helper process — which doubles as the
geometry authority, since the compositor is never asked for the screen size.

It claims the same words `mousegrid` claims, but only when the layer-shell/ydotool
stack is actually usable; everywhere else it stays inert and `mousegrid` keeps
them. The prototype is verified on this host in headless `sway` with assertions on
rendered pixels and on the pointer coordinates reported by a probe window; a
Pop!_OS 24.04 VM for real-COSMIC acceptance is a deliberate follow-up.

## Motivation — evidence base, verified 2026-10-14

The intent's premises were re-checked against sources rather than trusted; two of
them changed the design (the ydotool CLI semantics and the repo's own test
constraints), and one was confirmed independently:

| Claim | How it was verified | Result |
|---|---|---|
| Layer-shell works on COSMIC | gtk4-layer-shell's README ("Supported Desktops") | ✅ lists Smithay-based compositors (**COSMIC**), wlroots (**Sway**), Mir, KDE; **excludes GNOME-on-Wayland** |
| COSMIC/extension incompatibility | same README + the issue's cosmic-comp research | ✅ extension path cannot be resurrected on the target desktop |
| `mousegrid` is a live trap here | GNOME Shell 42.9 on this box; extension requires 47+ | ✅ plugin loads, every action no-ops |
| The plugin loader takes drop-in additions | `src/core/main.py:227-275` | ✅ scans one directory; needs only `NAME` + `handle` |
| ydotool CLI shape | `v0.1.8`'s own `Client/ydotool.cpp`, `Tools/*/*.cpp` | ⚠️ **differs from every current doc and from 1.0.x** — see table below |
| gtk4-layer-shell availability | Launchpad (`+source/gtk4-layer-shell`) | ⚠️ **absent from noble 24.04**; first lands in 25.10/26.04 (1.3.0) |
| uinput access on this host | `getfacl /dev/uinput`, `id -nG` | ⚠️ granted to `ecc` by a **bare ACL**, not a group or udev rule |

## Verified semantics (the contract this plugin relies on)

### ydotool 0.1.8 — read from source, because docs and convention both mislead

| Behaviour | Verified detail | Consequence for the plugin |
|---|---|---|
| Exit code | `MouseMove::Exec` and `Click::Exec` both `return argc` | `ydotool click 1` **exits 2 on success**; the code carries no failure meaning on its own |
| Success code per invocation | `argc` of the argv the tool itself sees: `mousemove_relative 0 0` returns **3**, `click 1` returns **2** — which collides with the parse-error return of the move tools | the gate's probe uses the unambiguous form (`mousemove_relative 0 0`, success 3) and never `click`, whose success code is indistinguishable from an error |
| Absolute move | `mousemove <x> <y>` — first emits `RelativeMove({-INT32_MAX, -INT32_MAX})` (clamps at the desktop origin), then the relative delta | "absolute" is an approximation; pointer acceleration can bias it |
| Move syntax | positional `x y` (1.0.x's `--absolute -x -y` does not exist here) | argv is `["mousemove", str(x), str(y)]` |
| Click buttons | `1` left, `2` right, `3` middle; press+release in one call, no `--repeat` | double-click = two sequential invocations |
| Default delay | 100 ms before the action, per invocation (`--delay`) | two invocations ~200 ms apart; inside a typical 400 ms double-click window |
| Scroll | **no wheel verb at all** (`click`/`key`/`type`/`mousemove`/`mousemove_relative`) | scroll verbs are not expressible in this version — deferred, not silently dropped |
| Daemon socket | hardcoded `/tmp/.ydotool_socket`, `chmod 0600`, no CLI flags | `ydotoold` must run **as the user**; a system-level daemon is unreachable by the client |
| No daemon | client falls back to creating its own uinput device ("ydotoold backend unavailable (may have latency+delay issues)") | a reachable socket is **not** a prerequisite; the gate probes the binary functionally |
| Failure signals | errors go to stderr (`error: …`, `Which coordinate …`, `Unknown tool …`); the daemon notice also goes to stderr | stderr signatures identify `errored`; they cannot identify `unknown`, which is why the seam returns one of three outcomes instead of a boolean |

### gtk4-layer-shell (Python binding)

| Detail | Value |
|---|---|
| Namespace | `gi.require_version('Gtk4LayerShell', '1.0')` → `from gi.repository import Gtk4LayerShell` |
| Link order | `CDLL('libgtk4-layer-shell.so')` **before** importing `gi` (it must precede `libwayland-client`) |
| Build floor | meson declares `dependency('gtk4')` with **no version floor** (checked at v1.0.4 and master) |
| Uninstalled build | `GI_TYPELIB_PATH=<build>/src LD_LIBRARY_PATH=<build>/src` runs without installing |
| Supported-desktop probe | `is_supported()` — the gate's overlay half |
| Panel helper | `auto_exclusive_zone_enable()` must **not** be called: the overlay must not reserve screen space |

### Host and repository facts

| Fact | Evidence |
|---|---|
| `sway` 1.7, `ydotool` 0.1.8, `gtk-layer-shell` 0.7 are apt-installable here; **gtk4-layer-shell is not** | `apt-cache policy` on this box |
| `python3-gi` 3.42.1 and `Gtk-4.0.typelib` present; `grim`, `xdotool`, `Xwayland`, `libvirt`, `/dev/kvm` present | local probes |
| `just` is apt-installable (`1.42.4-1pop1`); absent from PATH today | `apt-cache policy just` |
| Dev checkout has no `.venv` yet; `uv python pin 3.10` is required (pyaudio has no wheels) | install doc gotcha #1 |
| Loader contract: `NAME` + `handle(cmd, core)`; `True` consumed, `None` pass, `False` **exits the daemon**; `setup(core)` once; sort by `(PRIORITY, NAME)` | `src/core/main.py:259-275` |
| Adding a plugin breaks two existing core test assertions (exact roster set; `names[:2] == ["headtrack", "mousegrid"]`) | `tests/unit/core/test_main.py:278-306` |
| Every shipped catalog must be **complete** (no empty, no fuzzy) | `tests/unit/core/test_i18n.py::test_every_shipped_catalog_is_complete` |
| `just translations <lang>` regenerates catalogs for **every** package and CI fails on any resulting `*.po` diff | `justfile` `translations` / `check-translations` |
| Plugin packages are listed explicitly, and the wheel test asserts specific members | `pyproject.toml` `[tool.setuptools] packages`; `tests/packaging/test_python_wheel.sh` |
| `tests/integration/` is opt-in, excluded from default discovery, and each test skips gracefully when its binaries are absent | `justfile` `integration`; `tests/integration/README.md` |
| `routed_core` deliberately skips plugin `setup()` hooks ("which touch the system") | `tests/unit/plugins/conftest.py` |
| Core already spawns GUI helpers in a `gi`-capable interpreter, discovered by probing the real import chain | `src/plugins/dictation/__init__.py:526-570`, `_atspi_insert.py` docstring |

## Decisions (this brainstorm)

- **D1 — Capability-gated shadowing (user decision).** `PRIORITY = 0` (ahead of
  `mousegrid`'s 1), but the phrase is claimed only when *both* halves of the stack
  probe usable; otherwise every `handle` returns `None` and `mousegrid` keeps the
  word exactly as today. `EASYSPEAK_GRID_BACKEND=uinput|gnome` forces either side.
  Rejected: a distinct activation phrase (leaves the natural word dead on COSMIC,
  the exact gap the issue exists to close) and unconditional takeover (silently
  replaces the known-good extension backend on GNOME 47+ and makes the upstream
  change a behaviour change rather than an addition).
- **D2 — GTK4 + gtk4-layer-shell, in a helper process (user decision).** The
  daemon's venv deliberately has no PyGObject, so the overlay runs in a
  `gi`-capable interpreter, discovered by probing the helper's own import chain —
  the dictation/AT-SPI precedent. Rejected: the GTK3 binding (legacy, and the user
  chose GTK4 explicitly) and a raw Wayland client (a different order of effort
  than the "routing + math + two subprocess calls" the issue scopes).
- **D3 — Host fast loop now, Pop!_OS 24.04 VM later (user decision).** The fast
  loop runs on this workstation in headless `sway`; the VM (GUI install, source
  build of gtk4-layer-shell) carries real-COSMIC acceptance and is **out of this
  spec's scope**, recorded as a follow-up.
- **D4 — Verb surface: core loop only (user decision).** Open, chained digits,
  four click variants, dismissal, `again`. **Deferred with reasons**: `nudge`
  (expressible, low value without the rest), `scroll` (**not expressible in
  ydotool 0.1.8 at all** — no wheel verb), `mark`/`drag` (expressible as
  press `1` → move → release, but the extension's implementation is a 24-step
  animated path with a settle delay, which deserves its own timing design).
- **D5 — Parity with `mousegrid`, enforced by drift tests (agent decision).** The
  gate's whole premise is that one utterance lands on the same pixel whichever
  backend answers, so the zone math, the trigger words, and the spoken-number
  tables are mirrored, and tests assert equality against `mousegrid`'s — test-only
  coupling, no production import, because the plugin loader has no shared-library
  convention and the precedent is one plugin per concern. Without the drift tests
  the two backends would diverge silently and the invariant would rot.
- **D6 — Monitor-local coordinates (agent decision).** Bounds stay local to the
  monitor the overlay is on; the monitor's compositor-global origin is added only
  when injecting. This makes the math *identical* to `mousegrid`'s (so parity is
  exact rather than approximate), makes the helper's drawing trivial, and reduces
  the multi-output question to one addition at one call site — testable in unit
  tests instead of needing the VM.
- **D7 — "Zero core edits", restated precisely.** No changes under `src/core/**`
  (asserted mechanically in acceptance). Adding a plugin necessarily touches:
  `pyproject.toml`'s `packages` list, two assertions in
  `tests/unit/core/test_main.py` (roster set and load order), four `.po` catalogs,
  the wheel test's member list, and the docs. Every one of these is a mechanical
  consequence of adding a plugin, and the spec names them so the upstream PR is
  not a surprise.
- **D8 — Ship complete reply catalogs (agent decision).** The completeness test
  forbids empty/fuzzy entries and `check-translations` would create stub catalogs
  for any package with translatable strings, so the plugin ships **five** strings
  per language in de/es/fr/it, drafted by the agent and flagged for human review:
  the open acknowledgement (`Grid`), the dismissal (`Grid closed.`), the definite
  injection failure (`I couldn't move the pointer.`), the unknown outcome
  (`I tried, but I'm not sure it worked.`) and the grid that could not be shown
  (`I couldn't show the grid.`). The fourth string exists because a timeout leaves
  the outcome genuinely unknown, so "it didn't work" would be a claim the plugin
  cannot make — and the user acts differently on the two: one invites a retry, the
  other warns that the retry may be a second click. The fifth exists because the
  bounded handshake can fail before any overlay is mapped, and silence there would
  look like a dead daemon rather than a failed overlay.
  Rejected: silence (no `_()` calls) — a voice-driven UI with no spoken
  acknowledgement of a click, and no audible signal when injection fails or the
  overlay cannot be drawn, would be a worse prototype than `mousegrid`.
- **D9 — A dry-run seam, because uinput is kernel-global (agent decision).**
  Injected events reach *every* compositor on the machine, including the user's
  live session, even when the harness runs its own headless `sway`. Real injection
  is therefore opt-in per strength (`EASYSPEAK_GRID_LIVE=move|click`), dry-run by
  default, and the harness's click assertion is a deliberate, documented act.

## Design

### Components and process model

```text
src/plugins/gridydotool/__init__.py     # routing, zone math, gating, injection, session
src/plugins/gridydotool/_overlay.py     # GTK4 + layer-shell renderer (system interpreter)
src/plugins/gridydotool/locale/<l>/LC_MESSAGES/gridydotool.po
```

The plugin owns all state (bounds, last cell, active flag, helper handle) and runs
in the daemon's venv, where the coverage gate reaches it. The helper holds no
state beyond "what is currently drawn"; it is a real module rather than an
embedded `-c` string so Ruff lints it and unit tests can import it (the
`_atspi_insert.py` precedent), and its leading underscore keeps it out of plugin
discovery.

**IPC** — newline-delimited JSON, one object per line, stdin/stdout. stdout is
protocol only; diagnostics go to stderr, which the plugin logs.

| Direction | Message | Meaning |
|---|---|---|
| → helper | `{"cmd":"show","bounds":[x,y,w,h]}` | draw the 3×3 grid over this monitor-local rect |
| → helper | `{"cmd":"hide"}` / `{"cmd":"quit"}` | tear the surface down / exit |
| ← helper | `{"event":"ready","monitor":[x,y,w,h],"scale":1.0}` | surface configured; monitor geometry — **the geometry authority** |
| ← helper | `{"event":"presented"}` | first frame drawn (the harness asserts on this) |
| ← helper | `{"event":"error","message":"…"}` | a failure the plugin should surface |

The helper exits on stdin EOF, so a daemon death can never leave an orphan
overlay; `--probe` is a one-shot mode that prints `supported` / `unsupported`
(off `is_supported()`), exits 0/1, and never maps a surface.

Geometry is in the compositor's **logical** coordinate space — the same space
layer-shell anchors in and that `ydotool`'s relative motion lands in — and the
scale factor rides along on the `ready` event so a fractional-scale session is
diagnosable from the log rather than from a mis-aimed click.

**The geometry handshake is bounded, and it is the only read the plugin waits
on.** `show` is not fire-and-forget: it completes when `ready` arrives or after
`PROBE_TIMEOUT`, and the bounds are computed *only* from that report — never from
an assumed output size, and never from a previous session's geometry. Three cases
are specified rather than left to the implementer: `ready` arrives (the mode
starts); the helper dies or the wait times out (the mode never starts, a WARNING
names the reason, and the grid-unavailable string is spoken — the overlay was
never mapped, so there is nothing to dismiss and nothing to aim at); a command
arrives before `ready` (it cannot be honoured, because a subdivision without
geometry has no coordinates — the mode has not started, so this is unreachable
rather than queued, which is what keeps a user's utterance order from being
silently reordered).

### Overlay rendering

The surface is anchored to every edge, `keyboard_mode` is `NONE` (it must never
take focus) and its **input region is empty**, so the click ydotool injects
reaches the window underneath instead of the overlay. It draws nine cells with
their digits plus dividers; styling is otherwise the implementer's choice, with
one constraint the harness depends on: the fill must be opaque enough to differ
from the unremarkable black of an empty headless output, since that difference is
how rendering is asserted.

### Gating

`_active` is **False at import**, so a session (or a `routed_core` test) that never
runs `setup()` behaves exactly as today. `setup(core)` resolves it once:

```text
forced = EASYSPEAK_GRID_BACKEND
  "uinput" -> active        # a deliberate override; failures surface at use
  "gnome"  -> inert
  unset    -> overlay_ready AND injector_ready

overlay_ready  = grid_python() is not None
                 AND helper --probe exits 0 within PROBE_TIMEOUT
injector_ready = shutil.which("ydotool")
                 AND a no-op mousemove_relative 0 0 reports outcome `ran`
                     (success code 3, no error signature) within PROBE_TIMEOUT
```

`grid_python()` mirrors dictation's probe: `EASYSPEAK_GRID_PYTHON` override, then
`python3`, `/usr/bin/python3`, `/usr/bin/python`, each tested with the helper's
real import chain (`gi` + `Gtk 4.0` + `Gtk4LayerShell 1.0`). A failed gate logs at
INFO with the reason, so `journalctl` explains why the backend is inert.

**Every process in the gate is bounded, and the bound is the point.** `setup()`
runs during daemon startup, where a hang is worse than a failure: the loader guards
`setup()` with `try/except Exception` (`src/core/main.py:266-273`), which catches a
raise but not a wait, so an unbounded probe would leave the daemon short of its
wake-word loop with nothing in the log. The blocking risk is documented upstream,
not inferred — gtk4-layer-shell says of `gtk_layer_is_supported()` that it "May
block for a Wayland roundtrip the first time it's called" — and the precedent this
probe mirrors is *lazy*, not startup-time: dictation reaches `atspi_python()` from
`insert_via_atspi` (`src/plugins/dictation/__init__.py:579`), never from its
`setup()`. So: `PROBE_TIMEOUT = 5.0` on each candidate and on `helper --probe`
(generous for a cold `gi` import plus a GTK init and one roundtrip), and a timeout
is **not ready** — fail closed toward `mousegrid`, which is the correct backend
wherever this one cannot work.

Readiness is a *positive* signal, never the absence of a negative one. "No error
signature on stderr" also describes a probe that was killed before it could write
anything, which would activate a dead injector and then silently swallow every
click (see the outcome table below). `ran` is what the gate requires, and its
success code is unambiguous because the probe's argv is chosen for that property.
A `--probe` that exits 0 inside the timeout is the only other way in.

### Routing

| Utterance (lowercased, wake-stripped by core) | Action |
|---|---|
| `grid` / `mouse` / `pointer` / `cursor` (+ `mousegrid`'s mishearing set) | open the overlay on the **default monitor** (the one GTK reports first); choosing a monitor by pointer position or by name is deferred with multi-output (open item 7) |
| `1`–`9`, chained (`3 7 5`, `three seven five`) | subdivide the current bounds, redraw |
| `click` / `right` / `double` / `middle click` | move to the cell centre, click, hide |
| `close` / `cancel` / `stop` / `hide` | hide, stay in command mode |
| `again` / `repeat` / `reopen` (with a remembered cell) | reopen at the last cell |

Matching mirrors `mousegrid`'s: split on whitespace and test whole tokens, so
"greatest" matches nothing; a bare trigger followed by `close`/`hide` is a
dismissal, not an open. The mode itself is driven by `core.listen_modal("grid",
…)`, so the tray stays live and an unattended grid times out instead of trapping
the session.

### Zone math

Mirrored from `mousegrid.update_grid` line for line, including the integer
truncation and the safety clamp:

```python
ZONE_MAP = {1:(0,0), 2:(1,0), 3:(2,0), 4:(0,1), 5:(1,1), 6:(2,1), 7:(0,2), 8:(1,2), 9:(2,2)}

def subdivide(bounds, zone, monitor):
    x, y, w, h = bounds
    zone_w, zone_h = w // 3, h // 3
    col, row = ZONE_MAP[zone]
    new_x, new_y = x + col * zone_w, y + row * zone_h
    # safety net, as in mousegrid: never leave the monitor
    new_x = max(new_x, 0)
    new_y = max(new_y, 0)
    if new_x + zone_w > monitor[2]: new_x = monitor[2] - zone_w
    if new_y + zone_h > monitor[3]: new_y = monitor[3] - zone_h
    return (new_x, new_y, zone_w, zone_h)

def center(bounds):
    x, y, w, h = bounds
    return (x + w // 2, y + h // 2)
```

### Injection adapter

```text
BUTTONS = {"click": 1, "right": 2, "middle": 3}

move_to(x, y)      -> ["ydotool", "mousemove",   str(monitor_x + x), str(monitor_y + y)]
click(button)      -> ["ydotool", "click",       str(BUTTONS[button])]
double_click()     -> two sequential ["ydotool", "click", "1"] calls
```

Every external process — the gate's probes, the helper, and the injections — goes
through one module-level seam, `_run_bounded(args, timeout)`, which unit tests
patch (and which the dry-run switch short-circuits by logging the argv and
returning `ran` without spawning anything). It never returns a bare exit code,
because the exit code here is `argc` and carries no failure meaning: it returns one
of exactly three outcomes.

| Outcome | Detected by | What it means |
|---|---|---|
| `ran` | returncode equals the invocation's own success code, and stderr carries no error signature | the tool did what was asked |
| `errored` | an error signature on stderr, or a usage/parse returncode | the tool refused; nothing was clicked |
| `unknown` | `TimeoutExpired`, a negative returncode (killed by a signal), or `OSError` from the spawn | **the click may or may not have landed** |

`unknown` is the outcome a review misses, and the reason this is a table rather
than a boolean: a timeout is not a failure, and it is certainly not a success. It
is logged at WARNING with the captured stderr and spoken as its own string (D8),
and **no path retries it** — a repeated click is a second click, so the user's own
utterance is the only retry this design allows. A `notice: … backend unavailable`
line is not an error either: it is `ran` plus a latency warning, logged as such.

`INJECT_TIMEOUT = 2.0` bounds each injection (the tool's own pre-action delay is
100 ms, so this is generous by an order of magnitude), which is what keeps a wedged
`ydotoold` from freezing the modal loop with the overlay up — the loop exists so
the tray stays live and an unattended mode ends itself, and a blocking call
defeats both. Moves and clicks are sequential calls, which is what makes the
ordering (move, then click) deterministic.

### Session lifecycle and failure handling

| Condition | Behaviour |
|---|---|
| Gate failed at setup | every `handle` returns `None`; `mousegrid` answers as today; INFO log names the reason |
| Helper never reports `ready` inside `PROBE_TIMEOUT`, or dies first | mode never starts; WARNING names the reason; the overlay was never mapped; the grid-unavailable string is spoken |
| Helper dies with the grid open | logged; mode ends; no orphan surface; the daemon keeps running |
| The helper's output disappears mid-mode (monitor unplugged, compositor restarted) | the surface is gone, so the mode ends and the bounds are discarded — a click is never injected against geometry that no longer exists |
| A protocol line does not parse | WARNING naming the line, and it is ignored: the plugin never raises on a bad line and never treats one as an event |
| `ydotool` reports `errored` | WARNING with the captured stderr; overlay hidden anyway (never leave a stuck overlay); the definite-failure string |
| `ydotool` reports `unknown` (timeout, killed, spawn failure) | WARNING with the captured stderr; overlay hidden anyway; the unknown-outcome string — never silence, and never a claim about which way it went |
| Mode ends by idle timeout, `close`, or a completed click | the same `finally` path hides the surface and reaps the helper |
| Daemon exits mid-mode | helper exits on stdin EOF; `atexit` reap as belt-and-braces |
| Any exception inside `handle` | core catches it per-plugin (loader contract); our cleanup runs first |

## Files

| File | Content |
|---|---|
| `src/plugins/gridydotool/__init__.py` | routing, zone math, gating, injection adapter, session lifecycle, `_ = translator(__file__)` |
| `src/plugins/gridydotool/_overlay.py` | GTK4 + gtk4-layer-shell renderer; `--probe`; JSON IPC; `CDLL` before `gi` |
| `src/plugins/gridydotool/locale/{de,es,fr,it}/LC_MESSAGES/gridydotool.po` | complete catalogs — the 5 strings named in D8, agent-drafted, flagged for review |
| `tests/unit/plugins/test_gridydotool.py` | unit matrix, gate matrix, parity/drift tests |
| `tests/integration/test_gridydotool_harness.py` | headless-sway loop; opt-in; skips when binaries are absent |
| `tests/integration/grid_probe_window.py` | fullscreen GTK4 probe reporting pointer motion and button events as JSON |
| `tests/unit/core/test_main.py` | two assertions: roster set, load order (mechanical plugin-addition edit) |
| `tests/packaging/test_python_wheel.sh` | `has locale/de/LC_MESSAGES/gridydotool.po` |
| `pyproject.toml` | `easyspeak.plugins.gridydotool` in `[tool.setuptools] packages` |
| `docs/reference/plugins.md` | `::: plugins.gridydotool` entry |
| `docs/compatibility.md`, `docs/usage.md` | layer-shell/ydotoold requirements and the uinput risk wording; `EASYSPEAK_GRID_BACKEND` and `EASYSPEAK_GRID_PYTHON` |
| `tests/integration/README.md` | the harness's layout entry and `EASYSPEAK_GRID_LIVE` (test-only, so it is documented with the harness, not with user settings) |
| `mgmt/ADL/<date>.adl.md` (in **system-priv**) | the agent's autonomous choices (D5, D6, D8, D9) — placed there, not here, so this fork's diff against upstream stays about the plugin, and because system-priv owns the `mgmt/` convention and the decision context for the issue |

## Testing

### Unit (`just pytest`, coverage gate 99%)

- **Gate matrix**: overlay probe outcomes × injector probe outcomes ×
  `EASYSPEAK_GRID_BACKEND` values, including "forced `uinput` while probes fail"
  (active, and the failure surfaces at use) and "default-inert without `setup()`".
- **Routing**: every verb; word-boundary negatives ("greatest"); `close`/`hide`
  after a trigger is a dismissal; unknown input returns `None` so other plugins
  still see it; `True`/`None` return contract (never `False` — that exits).
- **Zone math**: table-driven subdivision; center computation; clamp behaviour.
- **Parity (drift)**: for all 1-, 2- and 3-digit sequences (819 cases), drive
  `mousegrid.update_grid` with `mousegrid.dbus_call` patched and its globals reset,
  and assert our bounds/centres equal its `grid_bounds`. Also assert our
  `GRID_TRIGGERS`, `WORD_TO_NUM` and parser behaviour match `mousegrid`'s over a
  shared sample.
- **Injection**: exact argv per verb; monitor offset applied to moves only;
  double-click issues two calls in order; the outcome matrix — `ran` for the
  invocation's own success code with clean stderr, `errored` for an error signature
  or a usage returncode, `unknown` for a timeout, a negative returncode or a spawn
  failure; `notice:` lines are `ran` with a latency warning, never failures; an
  `unknown` outcome is logged and spoken but never counted as a success; dry-run
  records without executing; nothing retries a click.
- **Bounded waits**: every probe and every injection is exercised against a stub
  that never returns, asserting that the timeout fires, that the gate then reports
  not-ready (so `mousegrid` keeps the words), and that the mode still ends with the
  overlay hidden.
- **Lifecycle**: helper spawn/hide/reap on click, close, idle-exit and exception;
  helper death mid-mode ends the mode; no orphan process is left.

### Integration harness (`just integration`, opt-in)

`tests/integration/test_gridydotool_harness.py`, following the folder's
conventions (excluded from default discovery, skip fixtures for missing binaries)
and its conftest's heavy-dependency stubs so no mic or model is needed. It:

1. starts `sway` with `WLR_BACKENDS=headless`, `WLR_LIBINPUT_NO_DEVICES=1` and a
   generated config whose `input *` section sets **`accel_profile flat`** — the
   relative move's landing point is otherwise biased by pointer acceleration;
2. launches the probe window and the plugin's helper inside that session, with
   `GI_TYPELIB_PATH`/`LD_LIBRARY_PATH` pointing at the layer-shell build tree;
3. calls `gridydotool.setup(fake_core)` for real (the gate is part of what is
   under test), then `handle("grid", fake_core)` with a fake `listen_modal` that
   replays `["3 7 5", "click"]`;
4. asserts, in order:
   - the helper reported `ready` with the headless output's geometry, then
     `presented` — and a `grim` screenshot's sampled points inside the nine cells
     differ from a pre-open screenshot of the same points (rendering, not just
     "process alive");
   - the probe window reported pointer coordinates inside the **predicted final
     cell rect**, computed independently in the harness from the output geometry
     and the digit sequence;
   - the probe window reported a button-1 press at those coordinates (the click
     arrived where it was aimed — and note that with layer-shell's empty input
     region the click *must* pass through the overlay to reach it);
   - no `_overlay.py` process survives the mode.

**Injection safety** (D9): the run is dry-run unless `EASYSPEAK_GRID_LIVE` is set.
`=move` performs the real move (the live session's cursor moves too — harmless);
`=click` also clicks, which is the only way to prove delivery end to end, and the
harness prints the coordinates it is about to click on before doing it.

### What this spec does not verify

Real-COSMIC acceptance (that cosmic-comp maps an overlay-layer surface and that
`is_supported()` returns true there) — the Pop!_OS 24.04 VM, follow-up scope.
The upstream README's Smithay claim is the current basis, and it is why the VM is
worth doing rather than a nice-to-have.

## Deployment and security

- **`ydotoold` runs as the user** (systemd `--user`), because 0.1.8 hardcodes
  `/tmp/.ydotool_socket` at `0600`; a system-level daemon would be unreachable by
  the client. Injection also works with no daemon at all, which is why it is not a
  gate.
- **One daemon owner stays the rule.** The install doc records this as a
  hit-live trap, and this plugin sharpens the consequence: two daemons mean two
  helpers, two stacked overlays and two injections per utterance — one command,
  two clicks. The deployment note names it so the failure is diagnosed as a
  duplicate owner rather than as a plugin bug; the plugin itself does no
  cross-process locking (that is the operator's rule, enforced by having a single
  autostart unit).
- **`/dev/uinput` on this box already grants `ecc` rw via a POSIX ACL**
  (`user:ecc:rw-`; `ecc` is not in the `input` group), so this prototype needs **no
  new privilege here**. A bare `setfacl` does not survive a reboot, so making it
  durable means the udev rule the issue's security note presumes — a persisted
  grant, not an ad-hoc one. That is the first open item below, not an assumption.
- **The risk class is stated, not implied**: uinput access is input-spoofing grade,
  the same class as the existing `input`-group grant for hold-to-dictate. The docs
  change carries this wording, because a user enabling the backend is granting it.
- **Environment variables**: `EASYSPEAK_GRID_BACKEND=uinput|gnome` and
  `EASYSPEAK_GRID_PYTHON` are user-facing and documented in `docs/usage.md`;
  `EASYSPEAK_GRID_LIVE=move|click` is test-only and documented with the harness in
  `tests/integration/README.md`.

## Delivery

1. Branch pushed to the fork (`hug bpush`), CI green: `just format`, `lint`,
   `types`, `pytest`, `check-translations`, `check-python-packages`.
2. Upstream issue on `ctsdownloads/easyspeak` carrying the findings: the ydotool
   0.1.8 semantics table (exit codes, `mousemove`, the 0600 socket), the
   layer-shell-on-Smithay-but-not-GNOME boundary as the natural gate, the
   packaging friction (a plugin addition must touch the roster test, the packages
   list and four catalogs), and the security note's wording.
3. A PR only if upstream asks for one — and note that **gtk4-layer-shell's own
   repository forbids AI-written production code and mostly-AI issue text**, so
   nothing is filed there; its README is cited, not changed.

## Open items — probe at implementation start, not blockers

1. **Does the `/dev/uinput` ACL survive a reboot?** (`udevadm test`/reboot.) If
   not, a udev rule becomes part of the deployment docs.
2. **Which gtk4-layer-shell release builds against GTK 4.6?** Start at v1.0.4;
   the meson declares no floor but the C code may use newer API.
3. **Click-through**: whether `window.set_can_target(False)` yields an empty input
   region on a layer surface in GTK4, or whether a `GdkSurface` input region is
   needed. The harness settles it behaviourally (the probe must receive the click).
4. **Is a double-click (two invocations, ~200 ms apart) inside the desktop's
   window?** Measure on the real session; adjust with `--delay` if not.
5. **How large is the acceleration bias?** Measured in the harness with a flat
   profile; the assertion tolerance is set from the measurement, and the numbers
   are recorded in the docs.
6. **Does headless `sway` pick up a uinput device that appears after startup?**
   (wlroots monitors udev; if not, the device is created before sway starts.)
7. **Multi-output**: the grid opens on the default monitor today, and D6's single
   addition (the monitor origin at injection time) is what a non-primary monitor
   needs. Whether ydotool's clamp-to-origin trick then lands where the offset
   predicts — the desktop's global origin is assumed to be its top-left — is
   deferred with the VM, where a second virtual output is easy to add.

## Acceptance criteria

Mapped to the issue's, with how each is discharged:

1. **Plugin loads in the existing daemon, zero core edits** — the updated roster
   assertion proves discovery; a daemon startup smoke on this box shows
   `Loaded: GridYdotool`; `git diff origin/main -- src/core` is empty, mechanically.
2. **Grid overlay renders as a layer surface in headless sway** — the harness's
   `ready`+`presented` events and the `grim` pixel sampling.
3. **"grid" → "3 7 5" → "click" moves the pointer to the expected coordinates,
   scripted** — the probe window's reported coordinates inside the predicted cell
   rect, plus the button-1 press at that point (with the argv assertions in the
   unit layer).
4. **Daemon-only stack shows no regression** — the full `just pytest` suite
   (including `mousegrid`'s own), `just integration`, and a live daemon smoke on
   this box: wake word, commands, dictation, TTS. The gate matrix proves the
   plugin is inert wherever the stack is absent, so `mousegrid` answers there.
5. **Prototype pushed to the fork; upstream issue opened with findings** — the
   Delivery section, including what upstream will have to touch to take it.

## References

- Intent: `docs/superpowers/intents/2026-10-14-prototype-compositor-agnostic-easyspeak-backend-layer-shell-overlay-ydotool-intent.md`
- Issue: [elifarley/system-priv#69](https://github.com/elifarley/system-priv/issues/69) ·
  decision context: [elifarley/system-priv#68](https://github.com/elifarley/system-priv/issues/68)
- Install doc: `03-solutions/20261014-easyspeak-install.md` (system-priv)
- ydotool 0.1.8 source: `Client/ydotool.cpp`, `Daemon/ydotoold.cpp`,
  `Tools/{MouseMove,Click}/{MouseMove,Click}.cpp` (tag `v0.1.8`)
- gtk4-layer-shell: README (supported desktops), `examples/simple-example.py`
  (the `CDLL`-before-`gi` requirement), `meson.build` at v1.0.4
- Launchpad: `gtk4-layer-shell` has no noble release
- Repo precedents: `src/plugins/dictation/_atspi_insert.py` (GUI helper in a
  `gi`-capable interpreter), `src/plugins/dictation/__init__.py:526-570`
  (interpreter probe), `src/plugins/mousegrid/__init__.py` (the backend being
  shadowed), `tests/integration/README.md` (harness conventions),
  `tests/unit/plugins/conftest.py` (`routed_core`, `attach_listen_modal`)
