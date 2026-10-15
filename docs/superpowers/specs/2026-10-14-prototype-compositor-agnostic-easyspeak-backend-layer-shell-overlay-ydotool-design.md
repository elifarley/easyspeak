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
rendered pixels and on the pointer coordinates reported by a probe window, and the
same harness is **5-for-5 green in a provisioned Ubuntu 26.04 LTS VM with the
packaged gtk4-layer-shell 1.3.0** (the acceptance evidence the contract tables
below cite). Real-COSMIC acceptance — cosmic-comp itself — remains the
follow-up the harness cannot stand in for.

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
| VM re-check (Ubuntu 25.10, questing cloud image) | packaged `libgtk4-layer-shell0` 1.0.4-2, `ydotool` 0.1.8-3build1, `python3-gi` 3.50; probes run over SSH | ⚠️ **three contract rows changed** — the packaged soname, the five ydotool verbs, and the daemonless notice; the contract tables below carry each as a row |
| VM acceptance (Ubuntu 26.04.1 LTS, resolute) | packaged gtk4-layer-shell 1.3.0, `ydotool` 1.0.4; the harness run in the guest against headless sway | ✅ **5 passed** — three gate tests, the forced-backend arm (helper handshake, geometry `[0,0,1920,1080]`, overlay renders, hides, re-presents) and the live arm, whose injected click is observed landing in the expected cell. The same run measured the second ydotool dialect: no per-invocation fallback, so the gate must find a socket and fail closed without one |

## Verified semantics (the contract this plugin relies on)

### ydotool 0.1.8 — read from source, because docs and convention both mislead

| Behaviour | Verified detail | Consequence for the plugin |
|---|---|---|
| Exit code | `MouseMove::Exec` and `Click::Exec` both `return argc`, but the client's `main()` **discards that value**: `int rc = this_tool->Exec(...)` is never returned, so any dispatched tool exits 0 | `ydotool click 1` **exits 0 on success** (VM-measured), so the code carries no failure meaning on its own; the seam reads 0 with a clean stderr as `ran` and still accepts the argv-shaped code for a build that forwards it |
| Available verbs (VM-measured) | Ubuntu's `ydotool help` lists exactly `type`, `recorder`, `mousemove`, `key`, `click` — and **no `mousemove_relative`** at all | capability is read from that list (`parse_tool_list`), and every argv the plugin builds uses a verb from it: the probe's no-op is `type ""`, never `click` (a real click) and never `mousemove` (a real move) |
| Success code per invocation | the argv-shaped numbers (`mousemove 0 0` → 3, with `--delay 0` → 5, `click 1` → 2) exist only inside `Exec`; the process reports 0 | the seam accepts 0 (the measured client) or `len(args) - 1` (the forwarded-argc build) with no error signature; nothing about the probe depends on the number matching a particular argv |
| Absolute move | `mousemove <x> <y>` — first emits `RelativeMove({-INT32_MAX, -INT32_MAX})` (clamps at the desktop origin), then the relative delta | "absolute" is an approximation; pointer acceleration can bias it |
| Move syntax | positional `x y` (1.0.x's `--absolute -x -y` does not exist here) | argv is `["mousemove", str(x), str(y)]` |
| Click buttons | `1` left, `2` right, `3` middle; press+release in one call, no `--repeat` | double-click = two sequential invocations |
| Default delay | 100 ms before the action, per invocation (`--delay`) | the plugin passes **`--delay 0`** on this dialect — 1.0.x has no delay flag at all (the next table) — so a double-click pair carries no sleeps at all and its gap is measured in the harness (open item 4), not assumed |
| Scroll | **no wheel verb at all** (the five verbs the VM's build lists: `type`, `recorder`, `mousemove`, `key`, `click`) | scroll verbs are not expressible in this version — deferred, not silently dropped |
| Daemon socket | hardcoded `/tmp/.ydotool_socket`, `chmod 0600`, no CLI flags, and no `YDOTOOL_SOCKET` override | `ydotoold` must run **as the user** when it is used; a system-level daemon is unreachable by the client. Ubuntu 25.10 carries the daemon in a separate package (`ydotool` itself ships only `/usr/bin/ydotool`); on 26.04 it ships in the `ydotool` package |
| No daemon (0.1.8, VM-measured) | the client falls back to creating its own uinput device and **still works**: `type ""`, `click 0` and `mousemove 0 0` all exit 0 without a daemon, printing `ydotoold backend unavailable (may have latency+delay issues)` on stderr | the fallback is a **latency caveat on this dialect, not a capability gap**: here the socket is a logged mode rather than a gate condition, and a fallback `notice:` no longer excludes `ran`. On 1.0.x there is no fallback and the socket decides whether an injector exists at all (next table) |
| Failure signals | errors go to stderr (`error: …`, `Which coordinate …`, `Unknown tool …`); the daemon notice also goes to stderr. 1.0.x adds `failed to …` and `unrecognized option`, and rejects a 0.1.8-shaped argv loudly (`click: unrecognized option '--delay'` exits 0 having emitted nothing; the same argv on `mousemove` aborts with SIGABRT) | stderr signatures identify `errored`; they cannot identify `unknown`, which is why the seam returns one of three outcomes instead of a boolean. `unrecognized option` is an error signature because a rejected argv is not an emission |

### ydotool 1.0.x — the second dialect, VM-measured on Ubuntu 26.04 LTS

Ubuntu 26.04 ships ydotool 1.0.4, and it is not argv-compatible with 0.1.8. The
plugin reads the dialect off the client itself (`ydotool mousemove --help` names
`--absolute` on 1.0.x; 0.1.8's usage has no such option) and builds every
invocation to match; a client that cannot answer keeps the legacy argv.

| Behaviour | Verified detail | Consequence for the plugin |
|---|---|---|
| Available verbs (VM-measured) | `ydotool help` lists `type`, `recorder`, `mousemove`, `key`, `click`, `debug`, `bakers` | the gate's verb check still reads the binary's own list; `mousemove`, `click` and the probe's `type` are present on both dialects |
| Absolute move | `mousemove --absolute -x <x> -y <y>`; bare `-x`/`-y` are *relative* | the modern argv pins `--absolute`, so the move lands where the grid aimed rather than stepping from wherever the pointer was |
| Click buttons | hex selector plus press/release mask bits: `0xC0` left, `0xC1` right, `0xC2` middle (measured in the VM against a GTK window); 0.1.8's `click 1` is a bare selector with no mask and emits nothing at all | `click()` spells the buttons as `0xC0`/`0xC1`/`0xC2` on this dialect; the legacy argv is never emitted to a modern client |
| Delay | no `--delay` flag; passing one is rejected (`unrecognized option`) | the modern argv carries no delay — and the rejection is an error signature, so it can never read as `ran` |
| No daemon | **there is no per-invocation uinput fallback**: with no socket the client exits 2 (`failed to connect socket`) and emits nothing | on this dialect "no socket answering" means "no injector": the capability probe fails closed, so a host that cannot click keeps `mousegrid`'s grid rather than promising one that will not act |
| Daemon socket | defaults to `$XDG_RUNTIME_DIR/.ydotool_socket`; `YDOTOOL_SOCKET` overrides it; on 26.04 the daemon ships in the `ydotool` package | `ydotool_socket_candidates()` probes `$YDOTOOL_SOCKET`, `$XDG_RUNTIME_DIR`, the login session's `/run/user/<uid>/` path and 0.1.8's hardcoded `/tmp/.ydotool_socket`; the first that accepts a connection is exported to the client as `YDOTOOL_SOCKET` |
| Socket type | 1.0.x's daemon listens on a datagram socket, 0.1.8's on a stream one | `_socket_accepts` tries both types: the wrong type answers `EPROTOTYPE`, which would read as "no daemon" on a host that runs one — and on this dialect that reading decides whether the gate opens |

### gtk4-layer-shell (Python binding)

| Detail | Value |
|---|---|
| Namespace | `gi.require_version('Gtk4LayerShell', '1.0')` → `from gi.repository import Gtk4LayerShell` |
| Runtime name (VM-measured) | Ubuntu 25.10's `libgtk4-layer-shell0` installs only `libgtk4-layer-shell.so.0` → `libgtk4-layer-shell.so.1.0.4`, and 26.04's 1.3.0 package is the same shape; the unversioned `.so` symlink is **`-dev`-package-only** | the helper resolves the soname through `ctypes.util.find_library`, trusting the result only when it names this library (the call reports the soname inside the file that spelled the name, so a stale or misnamed file would answer with its own soname), and falling back to the literal soname and then the plain name (an uninstalled build tree on `LD_LIBRARY_PATH`) |
| Link order | the library load (first name that loads, soname first) **before** importing `gi` (it must precede `libwayland-client`) |
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

**Stopping the helper is bounded too, or the reap becomes the next hang.**
`stop_helper(reason)` is `terminate()`, then `wait(timeout=REAP_GRACE)` (a second
or two), then `kill()` — logging which rung was needed, since that is the only
signal a misbehaving helper leaves. An unbounded `wait()` would freeze the mode at
exactly the point these paths exist to make safe, and a process that cannot be
scheduled to handle SIGTERM still takes a `kill` from the kernel: the same
escalation a prior capture records for orphaned children (OB `27b7f9eb`). Every
pre-ready path calls it before speaking.

**`handle` returns `True` on every one of these paths**, failure included — the
phrase was claimed and answered. Only the gate-inert case returns `None`, which is
what lets `mousegrid` keep the word by design. Returning `None` here would hand the
same utterance to the other backend *after* this one had already spoken, and the
user would hear two answers to one word.

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
                 AND `ydotool help`'s tool list offers the verbs the plugin
                     builds (`mousemove`, `click`) and the probe's no-op (`type`)
                 AND a no-op reports outcome `ran` within PROBE_TIMEOUT, built in
                     the dialect the binary reported for itself (`type "" --delay 0`
                     on 0.1.8; `type ""` on 1.0.x, which rejects `--delay`)
                 AND, on the 1.0.x dialect, a ydotoold socket that accepts a
                     connection: that client has no per-invocation fallback, so
                     with no socket it exits 2 and emits nothing
```

The socket's role is **dialect-dependent**. 0.1.8 falls back to a per-invocation
uinput device, so there the gate logs which mode is in use and the socket is not
a condition; 1.0.x removed that fallback, so there "no socket answering" means
"no injector" and the probe fails closed — a host that cannot click keeps
`mousegrid`'s grid rather than promising one that will not act (the two tables
above, and the paragraph below).

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
click (see the outcome table below). `ran` is what the gate requires, and the
probe is chosen so that reading it is unambiguous: `type ""` emits nothing, so
its exit 0 means the tool ran and no side effect happened. A `--probe` that exits
0 inside the timeout is the only other way in.

**The daemon is a latency caveat on 0.1.8 and a prerequisite on 1.0.x — both VM-measured.**
On Ubuntu 25.10 the client works without one: it creates a uinput device per
invocation and says so (`ydotoold backend unavailable (may have latency+delay
issues)`), with `type ""`, `click 0` and `mousemove 0 0` all exiting 0
daemonless. Upstream's own warning is about latency — a freshly created device
needs time for the graphical environment to recognise and enable it — and that
caveat is real (a click through such a device can be lost), but it is not a
capability gap, so on 0.1.8 the socket is a logged mode, not a gate condition.
Ubuntu 26.04's 1.0.4 removed the fallback: with no socket it exits 2
(`failed to connect socket`) and emits nothing at all, so there the gate
discovers a socket among its candidates — `YDOTOOL_SOCKET`, the runtime
directory, the login session's own `/run/user/<uid>/`, and 0.1.8's hardcoded
path — with a bare `connect()` (no injection, no side effect, both socket types
tried because the two daemons speak different ones), and fails closed when none
answers. The functional capability probe (`type ""`, the only side-effect-free
verb of the five, because `mousemove 0 0` moves and `click 0` clicks) still
runs on both dialects, built in the dialect the client reported for itself. A
probe that fails (no binary, no verb, no `ran`, or on 1.0.x no socket) fails
closed to `mousegrid`.

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
because the process code carries no failure meaning — 0 for any dispatched tool on
the measured client — so it returns one of exactly three outcomes.

| Outcome | Detected by | What it means |
|---|---|---|
| `ran` | exit 0 (or the argv-shaped code on a build that forwards `Exec`'s return), and stderr carries no error signature | the tool dispatched cleanly. With a daemon it emitted through its already-recognised device; on 0.1.8 without one it built a per-invocation device and the log names that mode (1.0.x has no fallback and refuses without a socket). **Emission, not proof of application**: what the compositor did with them is observed in exactly one place, the harness's live arm |
| `errored` | an error signature on stderr, or a returncode that matches neither success form | the tool refused; nothing was clicked |
| `unknown` | `TimeoutExpired`, a negative returncode (killed by a signal), or `OSError` from the spawn | **the click may or may not have landed** |

`unknown` is the outcome a review misses, and the reason this is a table rather
than a boolean: a timeout is not a failure, and it is certainly not a success. It
is logged at WARNING with the captured stderr and spoken as its own string (D8),
and **no path retries it** — a repeated click is a second click, so the user's own
utterance is the only retry this design allows. A `notice: … backend unavailable`
line on stderr is 0.1.8's daemonless fallback naming its latency caveat: the VM
measured that path working, so it is logged (with the mode it implies) and does
not change the outcome — the invocation still counts as `ran`.

`INJECT_TIMEOUT = 2.0` bounds each injection, and on 0.1.8 every invocation
passes **`--delay 0`**: with a daemon the device is already recognised and the
delay would buy nothing but latency, and without one the invocation is the
per-invocation shape the VM measured working (the notice itself carries the
latency caveat). 1.0.x has no delay flag — passing one is rejected — so the
modern argv carries none, and the dialect is read from the client before any
argv is built. Either way a double-click pair is kept tight — two invocations
with the default 100 ms sleep each would spend most of the desktop's
double-click window in sleeps (see open item 4). The timeout is what keeps a
wedged ydotool or `ydotoold` from freezing the modal loop with the overlay up:
the loop exists so the tray stays live and an unattended mode ends itself, and a
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
| Daemon exits mid-mode | helper exits on stdin EOF — the pipe closes with its owner |
| Any exception inside `handle` | core catches it per-plugin (loader contract); our cleanup runs first |

## Files

| File | Content |
|---|---|
| `src/plugins/gridydotool/__init__.py` | routing, zone math, gating, injection adapter, session lifecycle, `_ = translator(__file__)` |
| `src/plugins/gridydotool/_overlay.py` | GTK4 + gtk4-layer-shell renderer; `--probe`; JSON IPC; the soname-first `CDLL` before `gi` |
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
| `tests/integration/README.md` | the harness's layout entry, its prerequisite set in one maintained place (sway, ydotool, the layer-shell stack, and a `ydotoold` for the gate's passing shape and the live arm), and `EASYSPEAK_GRID_LIVE` (test-only, so it is documented with the harness, not with user settings) |
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
- **Injection**: exact argv per verb and per dialect, and every verb checked against
  the binary's own list so `mousemove_relative` cannot sneak back in; monitor offset
  applied to moves only; double-click issues two calls in order; the outcome matrix
  — `ran` for exit 0 (or the argv-shaped code) with clean stderr, `errored` for an
  error signature or a returncode matching neither success form, `unknown` for a
  timeout, a negative returncode or a spawn failure; a `notice:` line means 0.1.8's
  client fell back to a per-invocation device, is logged with that mode, and still
  counts as `ran`; a 0.1.8-shaped argv on 1.0.4 (`unrecognized option`, exit 0) is
  `errored`, never `ran`; an `unknown` outcome is logged and spoken but never
  counted as a success; dry-run records without executing; nothing retries a click;
  the socket check is exercised as a mode log where 0.1.8 needs no daemon and as a
  gate condition on 1.0.x, where no socket means no injector (a binary with no
  daemon stays active on 0.1.8 and stays inert on 1.0.x; a missing verb or a failed
  no-op probe keeps it inert on both).
- **Bounded waits**: every probe and every injection is exercised against a stub
  that never returns, asserting that the timeout fires, that the gate then reports
  not-ready (so `mousegrid` keeps the words), and that the mode still ends with the
  overlay hidden. The handshake gets its own pair of cases, because its bound is a
  different mechanism: a stub helper that maps nothing and **writes nothing**
  (asserting the deadline fires and the helper is reaped), and a stub that maps a
  surface *after* the deadline (asserting no `_overlay.py` process and no mapped
  surface survive).
- **Lifecycle**: helper spawn/hide/stop on click, close, idle-exit and exception;
  helper death mid-mode ends the mode; no orphan process is left; `stop_helper`'s
  escalation is exercised with a stub that ignores SIGTERM (the rung is logged and
  the mode still ends), and the return contract is asserted on the failure paths —
  `True` when the phrase was claimed and answered, `None` only when the gate is
  inert.

### Integration harness (`just integration`, opt-in)

`tests/integration/test_gridydotool_harness.py`, following the folder's
conventions (excluded from default discovery, skip fixtures for missing binaries)
and its conftest's heavy-dependency stubs so no mic or model is needed. It:

1. starts `sway` with `WLR_BACKENDS=headless` (the live arm adds `libinput`, so
   its session has a seat able to route an injected event) and
   `WLR_LIBINPUT_NO_DEVICES=1` (dropped for the live arm, whose seat is the point),
   and a generated config that **pins the output mode** (`output HEADLESS-1 mode
   1920x1080`, the syntax sway's own `output` command documents) and whose
   `input *` section sets **`accel_profile flat`** — the mode is pinned because
   arm B's literal is computed for exactly that geometry, and a headless output
   takes whatever mode the config gives it, so its default is not a premise
   anything may be built on; the flat profile because the legacy relative move's
   landing point is otherwise biased by pointer acceleration;
2. launches the probe window and the plugin's helper inside that session, with
   `GI_TYPELIB_PATH`/`LD_LIBRARY_PATH` pointing at the layer-shell build tree when
   `EASYSPEAK_GRID_TYPELIB` names one (packaged, they are not needed);
3. exercises the gate and the plugin in **two shapes**, because the injector's
   capability differs by dialect and a privilege-free dry-run arm pulls in the
   opposite direction:

   - **as the box stands** — against the real client: a failed injector probe keeps
     `mousegrid` in charge (asserted with a stub, so it needs no privileges), the
     gate's passing shape holds where a ydotoold socket answers (the client's own
     verb list, the no-op built in the dialect the client reported, `ran`), and on
     1.0.x an injector probe pointed at a dead socket fails closed, naming that
     there is no injector. Each of these skips by name when `ydotool` or a socket
     is absent;
   - **with `EASYSPEAK_GRID_BACKEND=uinput`** forcing the backend, so the overlay
     and argv assertions have a session to observe. The forced value
     short-circuits the probes by design, which is what keeps this arm free of any
     `/dev/uinput` requirement — the live arm is the one that injects.

   Then `handle("grid", fake_core)` runs with a fake `listen_modal` that replays
   `["3 7 5", "click"]`;
4. asserts, in **two arms, and the separation is the point**:

   **Arm A — dry run (the default, and the arm CI can run precisely because it is
   forced and never injects).** The plugin logs each helper event it consumes —
   `ready` with the geometry at INFO, `presented` at
   DEBUG — so the harness asserts on captured log records: the helper's stdout
   belongs to the plugin, and a test cannot claim to assert on a channel it does
   not own. Alongside that: a `grim` capture taken with the grid open differs from
   one taken before it (rendering, not "process alive"); the exact `ydotool` argv
   for the whole transcript is recorded by the dry-run seam and asserted against
   literals per dialect; no helper process survives the mode.

   **Arm B — live injection, opened explicitly and never by default.** With
   `EASYSPEAK_GRID_LIVE=click`, the arm first proves its two premises, so a lost
   click cannot masquerade as "nothing to click on" or "no route": the probe
   window rendered (a bare-session `grim` and one taken with the probe up differ),
   and the session has a seat (`swaymsg -t get_inputs` lists a device). The
   helper then drives the hide → show lifecycle — the fresh surface must still be
   click-through, and the later click is the observable — before the probe window
   must report coordinates inside the expected cell and a button-1 press there,
   and the expectation is a **hand-computed literal** rather than a
   re-derivation — with its premise asserted *before* the arithmetic: the plugin's
   captured `ready` geometry must equal the pinned 1920×1080, so a config drift
   fails at the premise instead of arriving as a mysterious coordinate mismatch.
   Then, for the sequence `3 7 5`:
   zone 3 gives `(1280, 0, 640, 360)`, zone 7 gives `(1280, 240, 213, 120)`, and
   zone 5 gives `(1351, 280, 71, 40)` — so the click must land inside `x` in
   `[1351, 1422)` and `y` in `[280, 320)`, centre `(1386, 300)`, with a tolerance
   that is **bounded rather than left to the measurement**: inside that rect
   expanded by at most a quarter of the cell's smaller dimension — 10 px here,
   the cell being 71 by 40 — so that widening the tolerance can never be the
   reason the assertion passes.
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
`is_supported()` returns true there) — still the follow-up scope, and the one
premise the packaged-stack acceptance does not touch. It needs a guest with
`cosmic-session`, which **Ubuntu's archive does not carry**: it comes from the
COSMIC PPA (`ppa:system76-dev/stable`) or a Pop!_OS 24.04 image, so the run is
"add the PPA (or boot Pop!_OS), install `cosmic-session`, start a COSMIC session
in the guest, and run the same five harness cases under cosmic-comp" — the
harness needs no change, because the compositor it starts is only sway's
stand-in for the real session. Everything short of that
boundary is now verified on a real stack: in a provisioned Ubuntu 26.04.1 LTS VM
with the packaged gtk4-layer-shell 1.3.0, the integration harness reports **5
passed** — three gate tests, the forced-backend arm (helper handshake, geometry
`[0,0,1920,1080]`, overlay renders, hides, re-presents) and the live arm whose
injected click is observed landing in the expected cell. The upstream README's
Smithay claim is still the basis for COSMIC itself.

## Deployment and security

- **`ydotoold` runs as the user** (systemd `--user`), because 0.1.8 hardcodes
  `/tmp/.ydotool_socket` at `0600` and a system-level daemon would be unreachable
  by the client; 1.0.x defaults to `$XDG_RUNTIME_DIR/.ydotool_socket` but honours
  `YDOTOOL_SOCKET`. On **0.1.8 it is optional**: upstream documents that a device
  created per invocation may not be recognised in time, so without the daemon a
  click can be emitted and lost — the VM measured the daemonless path working,
  but that latency caveat is real, and the gate logs which mode is in use rather
  than refusing to activate. On **1.0.x it is a precondition**: there is no
  per-invocation fallback, so the capability probe fails closed when no socket
  answers, and a host without it keeps `mousegrid`'s grid rather than promising
  one that will not act.
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
  grant, not an ad-hoc one. That rule is now the one `docs/troubleshooting.md`
  carries (`KERNEL=="uinput", MODE="0660", GROUP="input"`, plus the user in
  `input`; open item 1 below records it), which is also the shape the acceptance
  VM runs with — not an assumption any more.
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
   `just integration` wherever sway, ydotool and the layer-shell stack exist
   (packaged or a build tree). A running `ydotoold` is what the gate's passing
   shape needs on 1.0.x and a latency-caveat remover on 0.1.8; the
   forced-backend arm needs none. That run is where arm A's assertions live, so
   the folder's convention of skipping gracefully when a prerequisite is absent
   means a green run has to say *which* prerequisite it skipped rather than being
   read as a pass.
2. Upstream issue on `ctsdownloads/easyspeak` carrying the findings — drafted in
   `docs/upstream-issue-draft.md`, not filed: both ydotool
   dialects (0.1.8's argc exit codes, clamp-then-relative `mousemove` and 0600
   socket; 1.0.x's `--absolute` move, hex button codes and no per-invocation
   fallback), the layer-shell-on-Smithay-but-not-GNOME boundary as the natural
   gate, the packaging friction (a plugin addition must touch the roster test, the
   packages list and four catalogs), and the security note's wording.
3. A PR only if upstream asks for one — and note that **gtk4-layer-shell's own
   repository forbids AI-written production code and mostly-AI issue text**, so
   nothing is filed there; its README is cited, not changed.

## Open items — settled by the implementation and the acceptance run, or still owed

Settled: 1, 3, 4, 5 and 6 below. Item 2 (the GTK 4.6 build floor) and item 7
(multi-output) stay open and unchanged — the acceptance VM packages 1.3.0, so it
cannot answer a build-floor question, and multi-output needs a second output.

1. **Does the `/dev/uinput` ACL survive a reboot?** Settled, by the shape of the
   grant rather than by a reboot: a bare `setfacl` is re-applied by nothing, so it
   does not; the durable form is a udev rule, and `docs/troubleshooting.md` now
   carries it (`KERNEL=="uinput", MODE="0660", GROUP="input"` plus the user in
   `input` — the shape the acceptance VM runs with, `/dev/uinput` `root:input`
   `0660`). This workstation's own grant is the transient form and is untouched by
   the change.
2. **Which gtk4-layer-shell release builds against GTK 4.6?** Start at v1.0.4;
   the meson declares no floor but the C code may use newer API. (Still open: the
   acceptance VM runs the packaged 1.3.0, and no build was made here.)
3. **Click-through — settled by measurement.** `window.set_can_target(False)`
   alone is **not** enough on a layer surface in GTK4: with it alone the click is
   swallowed by the overlay and reaches neither window (measured on GTK 4.6.9,
   two stacked windows, an XTest click). What works is an **empty input region on
   the `GdkSurface`** — `surface.set_input_region(cairo.Region())`, applied as
   soon as the surface exists and *before* `ready` is emitted, and re-applied for
   the surface a show-after-hide creates — and the real stack observed it: in the
   Ubuntu 26.04 VM the injected click passed through the presented overlay and the
   probe window under it saw the press in the expected cell.
4. **How tight is the double-click pair in practice? — partly settled, the number
   still owed.** The pair spends no time in sleeps: 0.1.8's invocations pass
   `--delay 0` (its 100 ms default is gone) and 1.0.x has no delay flag, so the
   gap is whatever the first invocation itself takes — process spawn, the daemon
   connect, the emission and the exit — and its only stated upper bound today is
   the plugin's own 2 s injection timeout (`INJECT_TIMEOUT`), which is not the same
   number as the observed delta and is not what the measurement is for: a delta
   beyond the desktop's double-click window (typically 400-500 ms) turns the pair
   into two single clicks. What is *not* in any artifact is that observed delta:
   the acceptance run clicked once, and `grid_probe_window.py` does not timestamp
   its presses, so measuring it needs the probe to carry a timestamp and the live
   arm to drive a double click. Until that run, no number is quoted here.
5. **How large is the acceleration bias? — the acceptance run bounds the landing,
   the bias itself is still owed.** The live arm asserts that the observed press
   falls inside the hand-computed cell `(1351, 280, 71, 40)` expanded by at most
   10 px on each side (a quarter of the cell's smaller dimension, 40 px), and on
   Ubuntu 26.04 with ydotool 1.0.4 and `accel_profile flat` it did — so the
   composed error (acceleration bias, integer rounding, and whatever the
   compositor does with injected motion) stayed inside that rect, and the
   tolerance was not widened to make the assertion pass. It is a bound, not a
   measurement of the bias: the intended point is the cell's centre `(1386, 300)`,
   which is ~46 px from the rect's edge in x, so a bias an order larger than the
   bound itself could still be hiding inside it. The observed offset was not
   logged (a passing run records nothing), and 0.1.8's clamp-then-relative move is
   the dialect most exposed to acceleration; logging the offset from the same
   harness change item 4 needs is what turns the bound into a number.
6. **Does headless `sway` pick up a uinput device that appears after startup? —
   still owed, and the acceptance run cannot answer it.** The run's seat assertion
   (`swaymsg -t get_inputs` listing a device before anything was injected) proves
   the compositor's input list carried the injecting device at click time, but the
   harness starts no `ydotoold`, so the guest's daemon — and its uinput device —
   were already up when sway started and the birth order is in no artifact.
   wlroots hot-plugs input devices at runtime in general (a USB mouse plugged in
   after login works), which is the reason to expect yes; the measurement is a
   harness shape that starts sway first, then the daemon, and asserts the device
   appears in `get_inputs` afterwards. Operator rule until then: start `ydotoold`
   (or let the distro's user unit start it at login) before relying on the grid.
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
   assertion proves discovery; a daemon startup smoke on this box shows the
   loader's own line, `✓ Loaded: gridydotool` (the loader prints the module's
   `NAME`, which is lowercase like every other plugin's — not the
   `GridYdotool` the plan's step text guessed), directly under
   `grid backend inert` and its reason; `git diff origin/main -- src/core` is
   empty, mechanically.
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
