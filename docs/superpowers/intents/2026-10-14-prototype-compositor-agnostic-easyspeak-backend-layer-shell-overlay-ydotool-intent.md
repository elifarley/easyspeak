# Intent: compositor-agnostic EasySpeak grid backend (layer-shell overlay + ydotool)

Author: Elifarley (with Command Code). Status: draft.

## Problem

EasySpeak's pointer control ships as a bundled GNOME Shell extension requiring
GNOME 47+. GNOME Shell 42.9 (Pop!_OS 22.04, frozen at 42) is the concrete
instance of the trap: the `mousegrid` plugin loads and appears in the startup log, but every action is
a silent no-op — a live trap recorded in the private work journal as
`system-priv#68` (not publicly linkable) and in the install doc
(`03-solutions/20261014-easyspeak-install.md`).

COSMIC — the migration target — is not GNOME, so the extension can never run
there. Worse, cosmic-comp deliberately lacks `wlr-virtual-pointer` (System76
markets COSMIC as eliminating "input spoofing"), so the compositor-cooperative
injection path is permanently unavailable on the target desktop. The
compositor-agnostic replacement is **uinput (ydotool)**, which injects at kernel
level and is indistinguishable from real hardware on any compositor.

Upstream has zero COSMIC or KDE backend issues/PRs (searched 2026-10-14), and the
plugin architecture takes drop-in additions without touching core: the loader
scans a single directory and accepts any module exposing `NAME` + `handle`
(`src/core/main.py:227-275`). Verified premise: gtk4-layer-shell's own README
lists Smithay-based compositors (COSMIC) as layer-shell capable and excludes
GNOME-on-Wayland — the overlay is renderable on the target and provably not on
this box's GNOME 42.

## Proposed outcome

A drop-in plugin, `src/plugins/gridydotool/`, that routes the grid conversation,
computes cell bounds, renders the numbered grid as a `wlr-layer-shell` surface
(GTK4 + gtk4-layer-shell), and injects pointer moves/clicks through ydotool.
The plugin keeps all state in the daemon process (where the repo's coverage gate
reaches it); the renderer is a thin helper in a `gi`-capable interpreter, and
**also the geometry authority** — it reports its monitor's compositor-global
geometry, replacing `mousegrid`'s `/sys/class/drm` guess.

- **Capability-gated activation.** `PRIORITY = 0` (ahead of `mousegrid`'s 1), but
  the phrase is claimed only when a layer-shell-capable compositor *and* a
  functional ydotool are both present; otherwise the plugin stays inert and
  `mousegrid` keeps the word. `EASYSPEAK_GRID_BACKEND=uinput|gnome` forces
  either side.
- **Parity with the extension backend.** Zone math mirrors `mousegrid` exactly
  (recursive thirds with integer truncation, the col/row map, clamping, cell
  centre), so one utterance lands on the same pixel on either backend.
- **Verb surface.** Open (`grid`/`mouse`/`pointer`/`cursor`), chained digits,
  `click` + right/double/middle, `close`/`cancel`/`stop`, `again`. Nudge, scroll
  and mark/drag are explicitly deferred (scroll is not expressible in ydotool
  0.1.8 at all; drag needs its own timing design).
- **Verification by assertion, not eyeball.** ~~A scripted headless-sway loop:
  the helper reports the surface presented plus geometry, `grim` pixel-samples
  the rendered cells, and a fullscreen probe window reports the click arriving
  at the predicted cell's coordinates.~~ Superseded by the amendment below: the
  loop is scripted in two arms — a dry-run half that always runs (overlay
  rendering, exact argv) and an explicitly opened live half that proves the
  pointer coordinates and the click.
- **Delivery.** Branch pushed to the fork; an upstream issue on
  ctsdownloads/easyspeak carrying the findings; a PR only if upstream wants one.

Deferred to a follow-up (out of this spec): provisioning the Pop!_OS 24.04 VM
for real-COSMIC acceptance, and its source build of gtk4-layer-shell.

## Affected users and systems

- **Layer-shell desktops.** The grid becomes usable on them (COSMIC, wlroots,
  KDE) without the GNOME extension. GNOME-47+ users see no
  behaviour change — the gate keeps the extension path authoritative wherever it
  works, so the upstream PR is an addition rather than a behaviour change.
  The live deployment (`/volumes/APM-data/opt/easyspeak`, `easyspeak-live.service`)
  is untouched: prototype work happens in the dev checkout.
- **The fork** (`elifarley/easyspeak`): a pushed branch. **Upstream**
  (`ctsdownloads/easyspeak`): findings, via issue.
- **Host state required for the fast loop** (user-run root steps): apt `sway`,
  `ydotool`, `just`; a pinned gtk4-layer-shell source build (GTK 4.6
  compatibility); a user-level `ydotoold`; a dev venv in the checkout
  (`uv python pin 3.10` + `uv sync` — pyaudio has no wheels).
- **Security posture.** uinput access is input-spoofing grade, the same risk
  class as the existing `input` group grant for hold-to-dictate (2026-10-14).
  `/dev/uinput` already grants `ecc` rw through a POSIX ACL (`user:ecc:rw-`;
  `ecc` is *not* in the `input` group), so no new privilege is introduced here —
  but that ACL is not reboot-durable and the documented deployment widens the
  surface for every user of the box. Deliberate; to be documented.

## Constraints

- **Zero `src/core/` edits.** Drop-in only: the plugin package, its tests, docs,
  the `pyproject.toml` `packages` entry (else `just check-python-packages`
  fails) and four generated `.po` catalogs (else `just check-translations`
  fails). Both mechanical; neither changes core behaviour.
- **ydotool 0.1.8 is the version on both this box and noble**, with semantics
  that contradict the obvious assumptions: the exit code is `argc` (so a
  successful `click` exits 2), there is no wheel verb, `mousemove` is *not*
  absolute (it clamps to the origin with a `-INT32_MAX` relative jump, then
  applies a relative delta — so acceleration can bias the landing), and the
  daemon socket is hardcoded `/tmp/.ydotool_socket` at 0600, which forces a
  **user-level** `ydotoold` (0.1.8 has no `YDOTOOL_SOCKET`). Injection also
  works with no daemon at all (in-process uinput), so the gate probes the binary
  functionally rather than checking for a socket.
- **The daemon venv has no PyGObject.** The overlay must run in a separate
  interpreter that has `gi`, discovered by a probe — the precedent is
  dictation's AT-SPI helper (`src/plugins/dictation/__init__.py:526-570`).
- **gtk4-layer-shell is not packaged on noble** (it lands in 25.10/26.04), so the
  host build is from source, pinned to a GTK-4.6-compatible release. The fast
  loop therefore tests an older GTK than the target runs; accepted.
- **GTK4 + gtk4-layer-shell only** (decided): no GTK3 binding, no raw Wayland
  client.
- **No `sudo` from agent sessions.** Root steps are handed to the user.
- **Tests must never inject into the live session.** A dry-run seam records
  intended argv by default; real injection happens only inside headless sway.

## Open questions

1. **Does the `/dev/uinput` ACL survive a reboot?** (`udevadm test` or a reboot.)
   If not, it needs the udev rule whose need the private work journal's
   `system-priv#69` (not publicly linkable) security note already presumes — a
   persisted grant, not an ad-hoc `setfacl`.
2. **Which gtk4-layer-shell release compiles against GTK 4.6?** Its meson
   declares no GTK floor (checked in v1.0.4 and master), but the C code may use
   newer API. Start at 1.0.4.
3. **How much does pointer acceleration bias the clamp-then-relative move?**
   Measured in the harness; mitigated with a flat acceleration profile in the
   test session, and assertions made on cell containment with a documented
   tolerance rather than pixel equality.
4. **Is a double-click (two `ydotool click 1` invocations, each with a default
   100 ms delay) reliably inside the desktop's double-click window?**
5. **Does COSMIC honour `gtk4_layer_shell_is_supported()` and map an
   overlay-layer surface?** The upstream README claims Smithay support; real
   confirmation waits for the Pop!_OS 24.04 VM.
6. **Multi-output correctness.** The clamp-to-origin trick assumes the
   compositor's global origin is the desktop's top-left; a grid opened on a
   non-primary output needs the monitor's global offset added to every cell
   centre. One output in the harness; deferred to the VM.

## Amendment (2026-10-14, spec-roast reception round 3)

The verification bullet above is superseded by the design spec's testing
section. The roast's S3 pass showed that a single "the coordinates must match"
criterion scores the *unsafe* arm better than the safe one: with injection
dry-run by default (D9), the arm that skips the safety property is the only one
that can pass. The loop is therefore scored as two criteria: 3a runs under the
dry-run default and asserts the exact `ydotool` argv plus the overlay lifecycle,
while 3b — pointer coordinates and click delivery — runs only with
`EASYSPEAK_GRID_LIVE=click`, ideally inside the deferred VM where uinput
injection is actually isolated rather than merely headless. The original wording
is kept above, struck through, for the record.
