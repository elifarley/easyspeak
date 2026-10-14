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
| Default delay | 100 ms before the action, per invocation (`--delay`) | the plugin passes **`--delay 0`** — the daemon's device is already recognised — so a double-click pair carries no sleeps at all and its gap is measured in the harness (open item 4), not assumed |
| Scroll | **no wheel verb at all** (`click`/`key`/`type`/`mousemove`/`mousemove_relative`) | scroll verbs are not expressible in this version — deferred, not silently dropped |
| Daemon socket | hardcoded `/tmp/.ydotool_socket`, `chmod 0600`, no CLI flags | `ydotoold` must run **as the user**; a system-level daemon is unreachable by the client |
| No daemon | client falls back to creating its own uinput device ("ydotoold backend unavailable (may have latency+delay issues)") | this fallback is exactly why the socket **is** a prerequisite for the click path: a device created per invocation may not be recognised before the events are emitted, so the gate requires the daemon and a fallback `notice:` classifies as `unknown` |
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
  the two backends would diverge silently and the invariant would rot. What the
  tests prove is *math and vocabulary* parity given equal inputs; that the two
  backends are handed the same output geometry (the extension reports its own
  size, this one takes GTK's monitor geometry) is a separate premise, asserted
  once on a real session rather than assumed here — the same place the
  multi-output question is settled.
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
  for any package with translatable strings, so the plugin ships **five reply
  strings** plus its `DESCRIPTION` and `COMMANDS` help entries per language in
  de/es/fr/it, drafted by the agent and flagged for human review:
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

Its module surface follows the plugin contract: `NAME`, `PRIORITY`, `handle` and
`setup` as specified above, plus `DESCRIPTION` and a `COMMANDS` list, because the
help output is the discovery path for a voice UI — a user who never learns the
grid exists will never say "grid". That list names only what this backend
implements (`scroll`, `mark` and `drag` are absent rather than listed and broken),
and its entries are translated like every other plugin's, so they belong to the
catalogs D8 sizes.

**IPC** — newline-delimited JSON, one object per line, stdin/stdout. stdout is
protocol only; diagnostics go to stderr, which the plugin logs.

| Direction | Message | Meaning |
|---|---|---|
| → helper | `{"cmd":"show","bounds":[x,y,w,h]}` | draw the 3×3 grid over this monitor-local rect |
| → helper | `{"cmd":"hide"}` / `{"cmd":"quit"}` | tear the surface down / exit |
| ← helper | `{"event":"ready","monitor":[x,y,w,h],"scale":1.0}` | surface configured; monitor geometry — **the geometry authority** |
| ← helper | `{"event":"presented"}` | first frame drawn (the harness asserts on this) |
| ← helper | `{"event":"error","message":"…"}` | a failure the plugin surfaces: during the handshake it ends the session at once, and afterwards it is logged |

The helper exits on stdin EOF, so a daemon death can never leave an orphan
overlay; `--probe` is a one-shot mode that prints `supported` / `unsupported`
(off `is_supported()`), exits 0/1, and never maps a surface.

**`show` refuses rather than degrades.** It re-checks `is_supported()` before
mapping anything, and on a compositor without layer-shell — the forced-backend
path, or a session that lost the protocol after startup — it answers
`{"event":"error","message":"layer-shell unsupported"}` and maps nothing.
Degrading to an ordinary window would be the worse failure: the overlay would look
wrong while the injections still worked, which reads as a rendering bug instead of
a refusal, and it is reachable on any GNOME session the moment someone forces the
backend to see what happens.

Geometry is in the compositor's **logical** coordinate space — the same space
layer-shell anchors in and that `ydotool`'s relative motion lands in — and the
scale factor rides along on the `ready` event so a fractional-scale session is
diagnosable from the log rather than from a mis-aimed click.

**The geometry handshake is bounded, and the bound is a mechanism, not a hope.**
`show` is not fire-and-forget: the helper is spawned with `Popen(stdin=PIPE,
stdout=PIPE)` — not `subprocess.run`, which would only return once the helper
exits — and `show` completes when `ready` arrives or when `read_until(deadline)`
gives up at `PROBE_TIMEOUT`. That helper is one function: `select.select()` on the
helper's stdout with the remaining time, then read one line if it is ready.

Why the mechanism has to be named: a `subprocess` timeout bounds a *process*, and
this wait is not a process — it is a read on a live child's pipe, where a blocking
`readline()` has no timeout at all. A helper that stays alive and writes nothing
would sail past a bound written only as a property, which is the shape this design
must not have (and the shape a prior capture records: OB `a5e9d0fc`, a documented
client timeout defeated by a peer that held the connection open and idle).

Five cases, all specified rather than left to the implementer:

- `ready` arrives — the mode starts.
- the helper dies — the read returns EOF immediately; **kill and reap the helper
  first**, then WARNING, then the grid-unavailable string.
- the helper stays alive and silent — the deadline fires, and this path does the
  same thing: **kill and reap, then speak**. Without that reap a helper that
  finished mapping just after the deadline would leave an ownerless full-screen
  overlay, which is the design's own named worst outcome; the stdin-EOF rule only
  covers the case where something closes the pipe, and closing it is exactly what
  the reap does.
- the helper answers `error` — the handshake ends *at that line* instead of waiting
  out the deadline for a failure it has already been told about: reap, log the
  helper's message verbatim, speak the grid-unavailable string. The helper is the
  only component that knows why a surface was refused, so discarding its message is
  the one case where the diagnostic matters most.
- a command arrives before `ready` — it cannot be honoured, because a subdivision
  without geometry has no coordinates. The mode has not started, so this is
  unreachable rather than queued, which is what keeps a user's utterance order
  from being silently reordered.

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
                 AND the ydotoold socket accepts a connection
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

**The daemon is required for the click path, and that is what makes `ran` mean
anything.** Without `ydotoold`, the client creates a uinput device per invocation
and upstream documents the consequence: a freshly created device needs time for the
graphical environment to recognise and enable it, and the events emitted before
that are lost. A click through such a device exits cleanly and lands nowhere, so
the seam would report `ran` for a click that never happened — a silent no-op
wearing a success signal, in the one configuration that makes it silent. So the
socket is part of `injector_ready`: a host without the daemon stays inert (and says
why in the log at INFO), rather than half-working in a way the user cannot see. The
socket check is a bare `connect()` on `/tmp/.ydotool_socket` — no injection, no
side effect.

### Routing

| Utterance (lowercased, wake-stripped by core) | Action |
|---|---|
| `grid` / `mouse` / `pointer` / `cursor` (+ `mousegrid`'s mishearing set) | open the overlay on the output the compositor chooses (no `set_monitor` call is made), and take the geometry from the helper's `ready` report — never an assumed default size; choosing an output by name is deferred with multi-output (open item 7) |
| `1`–`9`, chained (`3 7 5`, `three seven five`) | subdivide the current bounds, redraw |
| `click` / `right` / `double` / `middle click` | move to the cell centre, click, hide |
| `close` / `cancel` / `stop` / `hide` | hide, stay in command mode |
| `again` / `repeat` / `reopen` (only when a cell is remembered) | reopen at the last cell. The cell is recorded when a click is dispatched, mirroring `mousegrid` (its click path sets `last_bounds`, subdivision alone does not), so a subdivide-only session leaves `again` with nothing to reopen and the word falls through to the other plugins |

Matching mirrors `mousegrid`'s: split on whitespace and test whole tokens, so
"greatest" matches nothing; a bare trigger followed by `close`/`hide` is a
dismissal, not an open. The mode itself is driven by `core.listen_modal("grid",
…)`, so the tray stays live and an unattended grid times out instead of trapping
the session.

### Zone math

Mirrored from `mousegrid.update_grid` **except** for one thing: its safety clamp is
dropped, because it cannot fire in either implementation. Bounds always start as
the full monitor rect and each step adds at most two integer-thirds to a cell of
integer thirds, so `x + w` stays inside the monitor and every term is
non-negative — `max(new_x, 0)` and the two off-monitor branches are unreachable in
`mousegrid` as well, and the drift test proves the two still agree without them.
The invariant they appeared to guard is stated instead, which is what a future
change to the subdivision (a different divisor, a non-zero origin) must be checked
against:

```python
ZONE_MAP = {1:(0,0), 2:(1,0), 3:(2,0), 4:(0,1), 5:(1,1), 6:(2,1), 7:(0,2), 8:(1,2), 9:(2,2)}

def subdivide(bounds, zone):
    x, y, w, h = bounds
    zone_w, zone_h = w // 3, h // 3
    col, row = ZONE_MAP[zone]
    return (x + col * zone_w, y + row * zone_h, zone_w, zone_h)

def center(bounds):
    x, y, w, h = bounds
    return (x + w // 2, y + h // 2)
```

**Containment invariant** — `x + w` and `y + h` never exceed the monitor's width
and height, for every sequence of zones; it holds inductively from the first
full-rect bounds, and it is the property the deleted clamp was reaching for.

### Injection adapter

```text
BUTTONS = {"click": 1, "right": 2, "middle": 3}

move_to(x, y)      -> ["ydotool", "mousemove",   str(monitor_x + x), str(monitor_y + y)]
click(button)      -> ["ydotool", "click",       str(BUTTONS[button])]
double_click()     -> two sequential ["ydotool", "click", "1"] calls
```

`monitor_x` and `monitor_y` are the `x` and `y` of the `ready` event's `monitor`
field — the same report the bounds are computed from, so the offset and the
geometry cannot disagree and there is no second source of truth for either.

Every external process — the gate's probes, the helper, and the injections — goes
through one module-level seam, `_run_bounded(args, timeout)`, which unit tests
patch (and which the dry-run switch short-circuits by logging the argv and
returning `ran` without spawning anything). It never returns a bare exit code,
because the exit code here is `argc` and carries no failure meaning: it returns one
of exactly three outcomes.

| Outcome | Detected by | What it means |
|---|---|---|
| `ran` | returncode equals the invocation's own success code, and stderr carries no error signature | the tool emitted the events through the daemon's already-recognised device and exited cleanly. **Emission, not proof of application**: what the compositor did with them is observed in exactly one place, the harness's arm B |
| `errored` | an error signature on stderr, or a usage/parse returncode | the tool refused; nothing was clicked |
| `unknown` | `TimeoutExpired`, a negative returncode (killed by a signal), or `OSError` from the spawn | **the click may or may not have landed** |

`unknown` is the outcome a review misses, and the reason this is a table rather
than a boolean: a timeout is not a failure, and it is certainly not a success. It
is logged at WARNING with the captured stderr and spoken as its own string (D8),
and **no path retries it** — a repeated click is a second click, so the user's own
utterance is the only retry this design allows. A `notice: … backend unavailable`
line on stderr is the one signal that must not be filed as `ran`: it means the
client fell back to a device created for this invocation, which is exactly the case
the `ran` row excludes — so it classifies as `unknown`, with the stderr captured
and the unknown-outcome string spoken. The gate required the socket, so seeing this
line at all means the daemon died between setup and the click, and the log says so.

`INJECT_TIMEOUT = 2.0` bounds each injection, and every invocation passes
**`--delay 0`**: the delay exists upstream to give a fresh device time to be
recognised, and the daemon the gate requires already holds a recognised one, so
here it would buy nothing but latency. That is also what keeps a double-click pair
tight — two invocations with the default 100 ms sleep each would spend most of the
desktop's double-click window in sleeps (see open item 4). The timeout is what
keeps a wedged `ydotoold` from freezing the modal loop with the overlay up: the
loop exists so the tray stays live and an unattended mode ends itself, and a
blocking call defeats both. Moves and clicks are sequential calls, which is what
makes the ordering (move, then click) deterministic.

### Session lifecycle and failure handling

| Condition | Behaviour |
|---|---|
| Gate failed at setup | every `handle` returns `None`; `mousegrid` answers as today; INFO log names the reason |
| Helper never reports `ready` inside `PROBE_TIMEOUT`, dies first, or answers `error` | helper stopped and reaped **before** anything is spoken (a helper that mapped after the deadline must not be left ownerless); WARNING names the reason, and the helper's own message is logged verbatim when it sent one; the overlay was never wanted, so nothing is dismissed and nothing is aimed at |
| Helper stays alive and silent (the case a blocking read cannot escape) | same as above — that is what the `select` deadline is for, and why the bound is not a `subprocess` timeout |
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
| `src/plugins/gridydotool/locale/{de,es,fr,it}/LC_MESSAGES/gridydotool.po` | complete catalogs — the reply strings and help entries named in D8, agent-drafted, flagged for review |
| `tests/unit/plugins/test_gridydotool.py` | unit matrix, gate matrix, parity/drift tests |
| `tests/integration/test_gridydotool_harness.py` | headless-sway loop; opt-in; skips when binaries are absent |
| `tests/integration/grid_probe_window.py` | fullscreen GTK4 probe reporting pointer motion and button events as JSON |
| `tests/unit/core/test_main.py` | two assertions: roster set, load order (mechanical plugin-addition edit) |
| `tests/packaging/test_python_wheel.sh` | `has locale/de/LC_MESSAGES/gridydotool.po` |
| `pyproject.toml` | `easyspeak.plugins.gridydotool` in `[tool.setuptools] packages` |
| `docs/reference/plugins.md` | `::: plugins.gridydotool` entry |
| `docs/compatibility.md`, `docs/usage.md` | layer-shell/ydotoold requirements and the uinput risk wording; `EASYSPEAK_GRID_BACKEND` and `EASYSPEAK_GRID_PYTHON` |
| `docs/how-it-works.md` | the plugin tree (`:48-59`) enumerates every package, so it goes stale the moment one is added |
| `docs/commands.md` | the grid verb table (`:36-53`): mark the verbs this backend cannot honour — `scroll` (`:48-49`) is not expressible in ydotool 0.1.8 at all, and `mark`/`drag` (`:50-51`) are deferred — so the table stops promising them uniformly across backends |
| `docs/troubleshooting.md` | its grid section (`:3`) explains only the extension path; add what a missing layer-shell/ydotoold stack looks like on this backend |
| `docs/index.md`, `README.md` | the feature lines (`index.md:33`, `README.md:25`) name the mouse grid without saying the extension is one of two ways to reach it |
| `tests/integration/README.md` | the harness's layout entry, its prerequisite set in one maintained place (sway, ydotool, the layer-shell build, and a `ydotoold` for arm B only), and `EASYSPEAK_GRID_LIVE` (test-only, so it is documented with the harness, not with user settings) |
| `mgmt/ADL/<date>.adl.md` (in **system-priv**) | the agent's autonomous choices (D5, D6, D8, D9) — placed there, not here, so this fork's diff against upstream stays about the plugin, and because system-priv owns the `mgmt/` convention and the decision context for the issue |

## Testing

### Unit (`just pytest`, coverage gate 99%)

- **Gate matrix**: overlay probe outcomes × injector probe outcomes ×
  `EASYSPEAK_GRID_BACKEND` values, including "forced `uinput` while probes fail"
  (active, and the failure surfaces at use) and "default-inert without `setup()`".
- **Routing**: every verb; word-boundary negatives ("greatest"); `close`/`hide`
  after a trigger is a dismissal; unknown input returns `None` so other plugins
  still see it; `True`/`None` return contract (never `False` — that exits).
- **Zone math**: table-driven subdivision; center computation; the containment
  invariant as a property over sequences — the clamp that used to look like its
  guard is gone, so the property is what a change to the divisor must be checked
  against.
- **Parity (drift)**: for all 1-, 2- and 3-digit sequences (819 cases), drive
  `mousegrid.update_grid` with `mousegrid.dbus_call` patched and its globals reset,
  and assert our bounds/centres equal its `grid_bounds`. The tables are asserted
  by **exact equality** (`GRID_TRIGGERS` as sets, `WORD_TO_NUM` as dicts), not by
  sample: they are small, and a sampled comparison is how a drifted entry slips
  through. The parser is compared over a shared utterance sample, since a function
  cannot be enumerated — and what the whole test proves is scoped in D5.
- **Injection**: exact argv per verb; monitor offset applied to moves only;
  double-click issues two calls in order; the outcome matrix — `ran` for the
  invocation's own success code with clean stderr, `errored` for an error signature
  or a usage returncode, `unknown` for a timeout, a negative returncode or a spawn
  failure; a `notice:` line means the client fell back to a per-invocation device
  and classifies as `unknown`, never as `ran`; an
  `unknown` outcome is logged and spoken but never counted as a success; dry-run
  records without executing; nothing retries a click; the socket check is part of
  the gate matrix (a host with the binary but no daemon stays inert).
- **Bounded waits**: every probe and every injection is exercised against a stub
  that never returns, asserting that the timeout fires, that the gate then reports
  not-ready (so `mousegrid` keeps the words), and that the mode still ends with the
  overlay hidden. The handshake gets its own pair of cases, because its bound is a
  different mechanism: a stub helper that maps nothing and **writes nothing**
  (asserting the deadline fires and the helper is reaped), and a stub that maps a
  surface *after* the deadline (asserting no `_overlay.py` process and no mapped
  surface survive).
- **Lifecycle**: helper spawn/hide/reap on click, close, idle-exit and exception;
  helper death mid-mode ends the mode; no orphan process is left.

### Integration harness (`just integration`, opt-in)

`tests/integration/test_gridydotool_harness.py`, following the folder's
conventions (excluded from default discovery, skip fixtures for missing binaries)
and its conftest's heavy-dependency stubs so no mic or model is needed. It:

1. starts `sway` with `WLR_BACKENDS=headless`, `WLR_LIBINPUT_NO_DEVICES=1` and a
   generated config that **pins the output mode** (`output HEADLESS-1 mode
   1920x1080`, the syntax sway's own `output` command documents) and whose
   `input *` section sets **`accel_profile flat`** — the mode is pinned because
   arm B's literal is computed for exactly that geometry, and a headless output
   takes whatever mode the config gives it, so its default is not a premise
   anything may be built on; the flat profile because the relative move's landing
   point is otherwise biased by pointer acceleration;
2. launches the probe window and the plugin's helper inside that session, with
   `GI_TYPELIB_PATH`/`LD_LIBRARY_PATH` pointing at the layer-shell build tree;
3. exercises the gate and the plugin in **two shapes**, because the gate's daemon
   requirement and a privilege-free dry-run arm otherwise pull in opposite
   directions:

   - **as the box stands** — with no running `ydotoold`, the plugin must come out
     inert: `handle("grid")` returns `None`, the INFO log names the socket, and the
     word is left to `mousegrid`. That is a real gate assertion and it needs no
     privileges at all;
   - **with `EASYSPEAK_GRID_BACKEND=uinput`** forcing the backend, so the overlay
     and argv assertions have a session to observe. The forced value
     short-circuits the probes by design, which is what keeps this arm free of any
     `/dev/uinput` requirement — arm B is the one that keeps the daemon, because
     arm B is the one that injects.

   Then `handle("grid", fake_core)` runs with a fake `listen_modal` that replays
   `["3 7 5", "click"]`;
4. asserts, in **two arms, and the separation is the point**:

   **Arm A — dry run (the default, and the arm CI can run precisely because it is
   forced and never injects).** The plugin logs each helper event it consumes —
   `ready` with the geometry at INFO, `presented` at
   DEBUG — so the harness asserts on captured log records: the helper's stdout
   belongs to the plugin, and a test cannot claim to assert on a channel it does
   not own. Alongside that: the nine cells' sampled `grim` points differ from a
   pre-open screenshot of the same points (rendering, not "process alive"); the
   exact `ydotool` argv for the whole transcript is recorded by the dry-run seam
   and asserted against literals; no `_overlay.py` process survives the mode.

   **Arm B — live injection, opened explicitly and never by default.** With
   `EASYSPEAK_GRID_LIVE=click`, the probe window must report coordinates inside the
   expected cell and a button-1 press there, and the expectation is a
   **hand-computed literal** rather than a re-derivation — with its premise
   asserted *before* the arithmetic: the plugin's captured `ready` geometry must
   equal the pinned 1920×1080, so a config drift fails at the premise instead of
   arriving as a mysterious coordinate mismatch. Then, for the sequence `3 7 5`:
   zone 3 gives `(1280, 0, 640, 360)`, zone 7 gives `(1280, 240, 213, 120)`, and
   zone 5 gives `(1351, 280, 71, 40)` — so the click must land inside `x` in
   `[1351, 1422)` and `y` in `[280, 320)`, centre `(1386, 300)`, with a tolerance
   that is **bounded rather than left to the measurement**: inside that rect
   expanded by at most a quarter of the cell's smaller dimension (17 px here), so
   that widening the tolerance can never be the reason the assertion passes.
   Deriving the rect with the same algorithm the plugin uses would make the
   assertion agree with any shared off-by-one; the algorithmic prediction stays
   only as a cross-check, with that reason written down next to it.

**Injection safety, and why the unit is split** (D9): the run is dry-run unless
`EASYSPEAK_GRID_LIVE` is set. `=move` performs the real move (the live session's
cursor moves too — harmless); `=click` also clicks, which is the only way to prove
delivery end to end, and the harness prints the coordinates it is about to click on
before it does. Because uinput is kernel-global, arm B is the one act that reaches
the user's live session: it is never the default, and the two arms are scored as
two criteria rather than one, so that a cautious default cannot be made to look
like a failing suite — which is exactly the trap a single "the coordinates must
match" criterion creates.

### What this spec does not verify

Real-COSMIC acceptance (that cosmic-comp maps an overlay-layer surface and that
`is_supported()` returns true there) — the Pop!_OS 24.04 VM, follow-up scope.
The upstream README's Smithay claim is the current basis, and it is why the VM is
worth doing rather than a nice-to-have.

## Deployment and security

- **`ydotoold` runs as the user** (systemd `--user`), because 0.1.8 hardcodes
  `/tmp/.ydotool_socket` at `0600`; a system-level daemon would be unreachable by
  the client. It is also part of the gate, not an optional extra: upstream
  documents that a device created per invocation may not be recognised in time, so
  without the daemon a click can be emitted and lost (see the `ran` row). A host
  without it keeps `mousegrid` and reads the reason in the log, which is a better
  outcome than a grid whose clicks quietly do nothing.
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
   `types`, `pytest`, `check-translations`, `check-python-packages` — and
   `just integration` wherever sway, ydotool and the layer-shell build exist — plus
   a running `ydotoold` for arm B, which is the only arm that injects, since arm A
   forces the backend and needs none. That run is where arm A's assertions live, so
   the folder's convention of skipping gracefully when a prerequisite is absent
   means a green run has to say *which* prerequisite it skipped rather than being
   read as a pass.
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
4. **How tight is the double-click pair in practice?** With `--delay 0` the gap is
   process spawn plus connect, so measure the observed inter-click delta rather
   than assuming it — the default 100 ms sleeps are gone, and a delta beyond the
   desktop's double-click window turns the pair into two single clicks. Log the
   measured gap when it exceeds the window, and record the number.
5. **How large is the acceleration bias?** Measured in the harness with a flat
   profile; the measurement sets the tolerance *within the bound arm B states* (a
   quarter of the cell's smaller dimension), and the recorded number goes in the
   docs. The bound exists so that a measurement cannot quietly turn the assertion
   into a formality.
6. **Does headless `sway` pick up a uinput device that appears after startup?**
   (wlroots monitors udev; if not, the device is created before sway starts.)
7. **Multi-output**: the grid opens on whichever output the compositor chooses
   today (the plugin makes no `set_monitor` call), and D6's single addition (the
   monitor origin at injection time) is what a non-primary monitor needs. Whether
   ydotool's clamp-to-origin trick then lands where the offset predicts — the
   desktop's global origin is assumed to be its top-left, and the remembered cell
   is not invalidated when an output changes size — is deferred with the VM, where
   a second virtual output is easy to add.

## Acceptance criteria

Mapped to the issue's, with how each is discharged:

1. **Plugin loads in the existing daemon, zero core edits** — the updated roster
   assertion proves discovery; a daemon startup smoke on this box shows
   `Loaded: GridYdotool`; `git diff origin/main -- src/core` is empty, mechanically.
2. **Grid overlay renders as a layer surface in headless sway** — arm A: the
   plugin's captured `ready`+`presented` log records, plus the `grim` pixel
   sampling.
3. **"grid" → "3 7 5" → "click" moves the pointer to the expected coordinates,
   scripted** — scored as two criteria so the safety default cannot read as a
   failure: **3a** (always runnable, the default) asserts the exact `ydotool` argv
   per verb and the overlay lifecycle under dry run; **3b** (opened explicitly with
   `EASYSPEAK_GRID_LIVE=click`, ideally in the isolated VM) asserts the pinned
   geometry first, then the probe window's reported coordinates inside the
   hand-computed literal cell rect with the tolerance bounded to a quarter of the
   cell, and the button-1 press there.
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
