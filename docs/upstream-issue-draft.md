# Upstream issue draft — the grid backends' two dialects and the layer-shell gate

Status: **filed** as ctsdownloads/easyspeak#234 (implemented by
ctsdownloads/easyspeak#233). Nothing is filed at
gtk4-layer-shell: that repository forbids AI-written production code and
mostly-AI issue text, so its README is cited here rather than changed.

What the issue asks for: take the `gridydotool` backend (the mouse grid that
does not need the GNOME Shell extension) — with the findings below as the
justification for the shape it has, and the packaging friction below as the
work a plugin addition costs. Everything here is measured on a real stack
unless a sentence says otherwise; the acceptance evidence is the provisioned
Ubuntu 26.04.1 LTS VM described at the end.

## Finding 1 — ydotool 0.1.8's CLI does not mean what its documentation implies

Read from `v0.1.8`'s own sources (`Client/ydotool.cpp`, `Tools/*/*.cpp`) and
measured on Ubuntu 25.10 (`ydotool` 0.1.8-3build1). A backend built on the
documented reading of this client misreads both its exit codes and its "absolute"
move.

| Behaviour | Verified detail | What a caller must do about it |
|---|---|---|
| Exit code | `MouseMove::Exec` and `Click::Exec` both `return argc`, but the client's `main()` discards it (`int rc = this_tool->Exec(...)` is never returned), so **any dispatched tool exits 0** | an exit code carries no failure meaning on this build; a caller must classify stderr, not the code |
| Success code per invocation | the argv-shaped numbers (`mousemove 0 0` → 3, `click 1` → 2, `type "" --delay 0` → 5) exist only inside `Exec` | accept 0 (this client) or `len(args) - 1` (a build that forwards the return), and require a clean stderr |
| Absolute move | `mousemove <x> <y>` first emits `RelativeMove({-INT32_MAX, -INT32_MAX})` (a clamp at the desktop origin) and then the **relative** delta | "absolute" is an approximation; pointer acceleration can bias the landing point |
| Available verbs | `type`, `recorder`, `mousemove`, `key`, `click` — and **no `mousemove_relative` at all** | capability must be read from the binary's own list; an argv built for a verb the build lacks fails at the pointer |
| Scroll | **no wheel verb exists** in this version | "scroll the page by voice" is not expressible through ydotool 0.1.8; it is deferred, not silently dropped |
| Click buttons | `1` left, `2` right, `3` middle, press+release in one call, no `--repeat` | a double-click is two invocations |
| Delay | 100 ms before the action, per invocation (`--delay`) | the default sleeps dominate a double-click pair; pass `--delay 0` |
| Daemon socket | hardcoded `/tmp/.ydotool_socket`, `chmod 0600`, **no CLI flag and no `YDOTOOL_SOCKET` override** | `ydotoold` must run as the user; a system-level daemon is unreachable by the client |
| No daemon | the client **creates a per-invocation uinput device and still works**: `type ""`, `click 0` and `mousemove 0 0` all exit 0 without a daemon, printing `ydotoold backend unavailable (may have latency+delay issues)` on stderr | the daemon is a latency caveat here, not a capability: a device created per invocation may not be recognised in time, and a click can be emitted and lost |
| Failure signals | `error: …`, `Which coordinate …`, `Unknown tool …` on stderr; the daemon notice also goes to stderr | signatures can identify "refused"; they cannot identify "unknown" (a timeout or a killed process), which is why the seam has three outcomes, not a boolean |

## Finding 2 — a second dialect, and it is not argv-compatible

Ubuntu 26.04 LTS ships ydotool 1.0.4, measured in the acceptance VM. Code
written against 0.1.8's argv is not merely deprecated there — it silently emits
nothing, or aborts:

| Behaviour | ydotool 1.0.4 (measured) |
|---|---|
| Verbs | `type`, `recorder`, `mousemove`, `key`, `click`, `debug`, `bakers` (the list's trailing "Use environment variable YDOTOOL_SOCKET …" sentence is not a verb) |
| Absolute move | `mousemove --absolute -x <x> -y <y>`; bare `-x`/`-y` are **relative** |
| Buttons | a hex selector plus press/release mask bits: `0xC0` left, `0xC1` right, `0xC2` middle; a bare selector (`click 1`) emits **nothing** |
| Delay | no `--delay` flag at all; passing one is rejected with `unrecognized option` — and `click` can still **exit 0** while having emitted nothing, so `unrecognized option` must be an error signature |
| No daemon | **no per-invocation fallback**: with no socket the client exits 2 (`failed to connect socket`) and emits nothing |
| Socket | defaults to `$XDG_RUNTIME_DIR/.ydotool_socket`, honours `YDOTOOL_SOCKET`, and listens on a **datagram** socket (0.1.8's is a stream socket), so a stream connect answers `EPROTOTYPE` — which reads as "no daemon" on a host that runs one |
| Packaging | 26.04 ships the daemon inside the `ydotool` package; 25.10 needed a separate `ydotoold` package |

The consequence for a backend: read the dialect off the client (`ydotool
mousemove --help` naming `--absolute` selects 1.0.x) and build every argv to
match, resolve the socket across `YDOTOOL_SOCKET`, `$XDG_RUNTIME_DIR`, the login
session's `/run/user/<uid>/` and 0.1.8's `/tmp/.ydotool_socket`, try both socket
types, export the one that answers, and **fail closed** on 1.0.x when none does:
there "no socket" means "no injector", and a grid that takes the words without
being able to click is worse than one that stays inert.

## Finding 3 — layer-shell support is the natural gate, and GNOME is on the wrong side of it

gtk4-layer-shell's README lists the supported desktops: wlroots (**Sway**),
Smithay (**COSMIC**), Mir and KDE; GNOME-on-Wayland is excluded by design. That
boundary is exactly the boundary of the problem the backend exists for — the
extension path works on GNOME and cannot be resurrected elsewhere — so the
capability probe (`gtk_layer_is_supported()`) is the gate rather than a
maintained desktop list.

Two wrinkles a consumer must know:

- **Packaged availability.** gtk4-layer-shell is absent from Ubuntu 24.04
  (noble); it first lands in 25.10 and 26.04 (1.3.0). On those it is a runtime
  package, so the supported shape there is "install the library and the typelib".
  Where no package exists, an uninstalled meson build tree works
  (`GI_TYPELIB_PATH=<build>/src LD_LIBRARY_PATH=<build>/src`).
- **The library must be resolved by soname.** The packaged install ships only
  `libgtk4-layer-shell.so.0` → `libgtk4-layer-shell.so.1.0.4`; the unversioned
  `.so` symlink is `-dev`-only. `ctypes.CDLL("libgtk4-layer-shell.so")` therefore
  fails on every host where the library is merely installed (measured: the
  helper refused everywhere on Ubuntu 25.10 until it used
  `ctypes.util.find_library("gtk4-layer-shell")`, which answers with the soname).
  The load must also happen **before** `import gi`, to precede
  `libwayland-client`.

## Finding 4 — what adding a plugin costs this repository

A drop-in plugin is one directory and the loader picks it up
(`src/core/main.py` scans one directory and needs only `NAME` + `handle`), but a
**shipped** plugin is not: adding one mechanically touches

1. `pyproject.toml`'s `[tool.setuptools] packages` list;
2. two assertions in `tests/unit/core/test_main.py` — the exact roster set and
   `names[:3] == ["gridydotool", "headtrack", "mousegrid"]` (this draft
   predated the plugin, whose insertion into the roster is the example);
3. four complete `.po` catalogs (`src/plugins/<name>/locale/{de,es,fr,it}/…`):
   the completeness test forbids empty or fuzzy entries, and CI fails on any
   `*.po` diff `just translations` would produce;
4. `tests/packaging/test_python_wheel.sh`'s member list, so the wheel's
   perimeter is asserted, not assumed;
5. the docs that enumerate the tree or the verbs: `how-it-works.md` (the plugin
   tree), `commands.md` (what the backend can honour), a
   `docs/reference/plugins.md` entry, `compatibility.md`, `usage.md` and
   `troubleshooting.md`.

None of it is surprising once known; all of it is invisible until CI is red, so
the issue names it.

## The security note, as the docs carry it

Enabling this backend grants input-injection-grade access: `ydotool` needs write
access to `/dev/uinput`, the same class as the existing `input`-group grant for
hold-to-dictate. Two operational rules go with it:

- **One daemon owner.** Two `ydotoold` processes mean two helpers, two stacked
  overlays and two clicks per utterance — one command, two clicks. The deployment
  note names it so the failure is diagnosed as a duplicate owner rather than as a
  plugin bug.
- **A durable grant is a udev rule, not a `setfacl`.** A bare ACL does not
  survive a reboot; the persisted shape is
  `KERNEL=="uinput", MODE="0660", GROUP="input"` plus the user in `input`.

## What is verified, and what is not

- **Verified on a real stack:** the integration harness reports **5 passed** in a
  provisioned Ubuntu 26.04.1 LTS VM (sway, `ydotool` 1.0.4, `grim`, packaged
  gtk4-layer-shell 1.3.0): a failed injector probe keeps `mousegrid` in charge, the
  gate passes with a daemon socket and fails closed without one, the forced-backend
  arm reads the compositor-global geometry (`[0,0,1920,1080]`), renders, hides and
  re-presents the overlay, and the live arm's injected click is **observed** by a
  probe window landing in the expected cell (`3 7 5` → `(1351, 280, 71, 40)`).
- **Not verified:** real COSMIC acceptance — resolved 2026-10-15: on a Pop!_OS
  24.04 VM whose cosmic-comp was upgraded to the distro-current build
  (`0.1~1791491785~24.04~4d14528`), `wayland-info` advertises
  `zwlr_layer_shell_v1` (v5) to an ordinary client and the overlay's
  `--probe` answers `supported`; the live conversation drew the grid, zoomed
  `3`→`7`→`5`, injected via a per-invocation uinput device (ydotool 0.1.8 has
  no daemon) and closed cleanly. Version caveat: the ISO-era cosmic-comp
  (`0.1~1759311734`, 2025-10-01) gates layer-shell behind
  `client_is_privileged` and does **not** advertise the global to ordinary
  clients — COSMIC acceptance therefore needs a cosmic-comp from after the
  2025-10-30 security rework (`client_not_sandboxed`), which Pop!_OS's own
  repos ship today. Also unmeasured: the double-click pair's inter-click gap
  and the exact acceleration bias (the live arm bounds the landing to the cell
  ± 10 px instead of recording the offset), and whether a compositor picks up a
  uinput device that appears *after* it started (the acceptance run's daemon
  was already up, so its device predated sway).
