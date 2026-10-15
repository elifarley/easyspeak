"""Standalone GTK4 layer-shell overlay for the gridydotool plugin.

Runs as a script in an interpreter that has PyGObject and the gtk4-layer-shell
typelib, both of which the daemon's venv lacks. It is a real module rather than
an embedded `-c` string so Ruff lints it and the unit suite can exercise the pure
parts (the `_atspi_insert.py` precedent), which is also why `gi` is imported
inside functions: importing it at module scope would make the whole file
unimportable from the venv.

Protocol: newline-delimited JSON on stdin and stdout, diagnostics on stderr.

    {"cmd": "show", "bounds": [x, y, w, h]}   draw a 3x3 grid over this rect
    {"cmd": "hide"}                            tear the surface down
    {"cmd": "stop"}                            exit (alias: "quit"; EOF does too)

    {"event": "ready", "monitor": [x, y, w, h], "scale": 1.0}
    {"event": "presented"}
    {"event": "error", "message": "..."}

`--probe` answers whether this compositor supports layer-shell and exits 0 or 1
without mapping anything.
"""

import json
import os
import sys
from pathlib import Path

DEFAULT_NAMESPACE = "gridydotool"


def cell_rects(bounds):
    """Return the nine cell rectangles of a 3x3 grid over `bounds`, row-major."""
    x, y, w, h = bounds
    cell_w, cell_h = w // 3, h // 3
    return [
        (x + col * cell_w, y + row * cell_h, cell_w, cell_h)
        for row in range(3)
        for col in range(3)
    ]


def surface_rects(bounds, origin):
    """Map monitor-local bounds into the surface's own coordinate space.

    The plugin computes monitor-local rects and adds the monitor's
    compositor-global origin only at injection time (design D6). The layer
    surface covers exactly the monitor it is anchored to, so its own drawing
    space *is* the monitor-local space, and `origin` — the compositor-global
    origin `report_monitor` puts on the `ready` event for the plugin's
    arithmetic — must never shift a cell here. An earlier revision subtracted
    it, which slid the grid and its digits off the visible surface on any
    output whose origin was not (0, 0) while the click still landed correctly;
    the parameter stays in the signature so a unit test can pin the identity
    mapping against a non-zero origin instead of leaving it to be re-derived
    by eye.
    """
    del origin  # D6: monitor-local bounds are drawn in the surface's own space
    return cell_rects(bounds)


def parse_command(line):
    """Return the command object on a protocol line, or None if there is none."""
    line = line.strip()
    if not line:
        return None
    try:
        payload = json.loads(line)
    except ValueError:
        sys.stderr.write(f"grid-overlay: ignoring unparsable line: {line[:120]}\n")
        return None
    return payload if isinstance(payload, dict) else None


def take_lines(pending, chunk):
    """Split every complete line out of `pending + chunk`.

    Returns `(lines, remainder)`: the newline-terminated lines in order, and
    the trailing fragment that has not arrived completely yet. Splitting the
    whole buffer - instead of reading one line per wake-up - is what keeps a
    command that sits behind another one in the same write from being
    stranded where no io watch can see it.
    """
    lines = (pending + chunk).split("\n")
    return lines[:-1], lines[-1]


def emit(payload):
    """Write one event to the parent."""
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def layer_shell_names():
    """The loadable names for gtk4-layer-shell, most specific first.

    `ctypes.util.find_library` asks the loader's own cache, so an installed
    runtime is found by *soname*: Ubuntu 25.10's `libgtk4-layer-shell0` ships
    only `libgtk4-layer-shell.so.0`, and the unversioned `.so` symlink belongs
    to the `-dev` package a normal user does not have. The soname and the plain
    name are literal fallbacks: the plain one covers an uninstalled build tree
    on `LD_LIBRARY_PATH`, which ldconfig cannot see.

    Only a result that names this library is trusted. `find_library` answers
    with the soname *inside* the file that spells the queried name, so a stale
    or misnamed `libgtk4-layer-shell.so` on the loader path answers with that
    file's own soname (measured: a copy of libm under that name answered
    `libm.so.6`). Loading that answer would preload an unrelated library and
    the link order this load exists for would be silently lost.
    """
    from ctypes.util import find_library

    names = []
    found = find_library("gtk4-layer-shell")
    if found and Path(found).name.startswith("libgtk4-layer-shell."):
        names.append(found)
    for candidate in ("libgtk4-layer-shell.so.0", "libgtk4-layer-shell.so"):
        if candidate not in names:
            names.append(candidate)
    return names


def load_layer_shell():
    """Load gtk4-layer-shell and Gtk, or return (None, None).

    The library must be loaded before Gtk's Wayland client is, which is what the
    CDLL call buys: linking it later lets libwayland-client win the symbol
    resolution and the layer surface silently comes up as an ordinary window.
    The names are tried in `layer_shell_names()` order, so a runtime that ships
    only the soname still loads; the load happens before `gi` regardless.
    """
    from ctypes import CDLL

    names = layer_shell_names()
    error = None
    for name in names:
        try:
            CDLL(name)
        except OSError as exc:
            error = exc
        else:
            break
    else:
        sys.stderr.write(
            f"grid-overlay: gtk4-layer-shell not found "
            f"(tried {', '.join(names)}): {error}\n"
        )
        return None, None
    try:
        import gi
    except ModuleNotFoundError as exc:
        # The daemon's venv: a loadable library with PyGObject missing. A bare
        # import here used to escape as a traceback, skipping every caller's
        # error path (reproduced with a dummy library on the loader path:
        # `--probe` under the venv died on this line).
        sys.stderr.write(f"grid-overlay: PyGObject is not installed: {exc}\n")
        return None, None

    try:  # pragma: no cover - needs the layer-shell typelib from the live stack
        gi.require_version("Gtk", "4.0")
        gi.require_version("Gtk4LayerShell", "1.0")
        from gi.repository import Gtk, Gtk4LayerShell
    except (ValueError, ImportError) as exc:  # pragma: no cover - live stack only
        sys.stderr.write(f"grid-overlay: cannot load Gtk4LayerShell: {exc}\n")
        return None, None
    return Gtk, Gtk4LayerShell  # pragma: no cover - live stack only


def is_supported():
    """True when this compositor speaks wlr-layer-shell."""
    _, layer_shell = load_layer_shell()
    if layer_shell is None:
        return False
    return bool(layer_shell.is_supported())  # pragma: no cover - live shell only


def clear_input_region(surface):
    """Empty the surface's input region, so clicks pass through it.

    `set_can_target(False)` is not enough on this stack: with it alone the
    click is swallowed - neither window receives it - while an empty input
    region lets the window below receive it (measured on GTK 4.6.9, two
    stacked windows, an XTest click). The feature is exactly a click injected
    while the pointer is over this surface, so a swallowing surface breaks it
    silently.

    Returns None on success, or a diagnostic when the region cannot be
    applied; the caller must refuse rather than present the surface without it.
    """
    try:
        import cairo

        surface.set_input_region(cairo.Region())
    except (ImportError, AttributeError, TypeError) as exc:
        return f"cannot clear the input region: {exc}"
    return None


class Overlay:  # pragma: no cover - needs GTK; run live
    """The layer surface, its nine cells, and the redraws they ask for."""

    def __init__(self, gtk, layer_shell, app):
        self.gtk = gtk
        self.layer_shell = layer_shell
        # Associate the window with the application: an app-managed window is what
        # the application's lifecycle (activate, quit, and the stdin watch that
        # ends the process) is written for.
        self.window = gtk.Window(application=app)
        self.area = gtk.DrawingArea()
        self.bounds = None
        self.origin = (0, 0)
        self.presented = False
        self.window.set_child(self.area)
        self.area.set_draw_func(self._draw)
        layer_shell.init_for_window(self.window)
        for edge in (
            layer_shell.Edge.LEFT,
            layer_shell.Edge.RIGHT,
            layer_shell.Edge.TOP,
            layer_shell.Edge.BOTTOM,
        ):
            layer_shell.set_anchor(self.window, edge, True)  # noqa: FBT003
        layer_shell.set_layer(self.window, layer_shell.Layer.OVERLAY)
        # Never take focus, and never eat the injected click. `set_can_target`
        # stops GTK from targeting the window; the empty input region - applied
        # by `make_click_through` as soon as the surface exists - is what
        # actually lets the click reach whatever is underneath.
        layer_shell.set_keyboard_mode(self.window, layer_shell.KeyboardMode.NONE)
        self.window.set_can_target(False)
        self.click_through_surface = None

    def _draw(self, area, cr, width, height):
        """Paint the nine cells with their digits.

        PyGObject calls a `set_draw_func` callback with exactly these four
        arguments - it does not pass the C callback's trailing user_data - and a
        five-argument signature raises TypeError on every frame, which loses
        both the grid and the `presented` event.
        """
        if not self.bounds:
            return
        cr.set_source_rgba(0.05, 0.05, 0.08, 0.55)
        cr.paint()
        cr.set_source_rgba(0.95, 0.95, 0.95, 0.95)
        cr.set_font_size(28)
        for index, (left, top, w, h) in enumerate(
            surface_rects(self.bounds, self.origin), start=1
        ):
            cr.rectangle(left, top, w, h)
            cr.stroke()
            cr.move_to(left + w / 2 - 8, top + h / 2 + 8)
            cr.show_text(str(index))
        del area, width, height
        if not self.presented:
            # The first frame of an actual grid is on the glass now; the plugin
            # reads this as "it is on screen", once, never per redraw.
            self.presented = True
            emit({"event": "presented"})

    def make_click_through(self):
        """Give the surface an empty input region, or say why it could not.

        Returns None when the surface is click-through already, or has not
        been created yet - the ready poll retries - and a diagnostic string
        otherwise; the caller refuses the session on a diagnostic, because a
        surface that cannot be made click-through must not be presented as if
        the click would pass through it.
        """
        surface = self.window.get_surface()
        if surface is None or surface is self.click_through_surface:
            return None
        problem = clear_input_region(surface)
        if problem is None:
            self.click_through_surface = surface
        return problem

    def show(self, bounds):
        """Draw the grid over `bounds`; a diagnostic means refuse the session.

        The region is re-checked here, on every show: a surface presented later
        must not go up without it.
        """
        self.bounds = tuple(bounds)
        self.window.present()
        self.area.queue_draw()  # the bounds changed: repaint, mapped or not
        return self.make_click_through()

    def hide(self):
        self.window.set_visible(False)


def report_monitor(overlay):  # pragma: no cover - needs a live surface
    """Emit the ready event once the surface is configured.

    The monitor geometry comes from the surface, not from an assumption: the
    compositor chooses the output (the helper makes no set_monitor call), and
    this report is the plugin's only source of screen size. The origin is the
    compositor-global one and rides along for the plugin's injection
    arithmetic; the drawing must stay in the surface's own space and never
    apply it (see `surface_rects`).
    """
    surface = overlay.window.get_surface()
    display = surface.get_display()
    monitor = display.get_monitor_at_surface(surface)
    geometry = monitor.get_geometry()
    overlay.origin = (geometry.x, geometry.y)
    emit(
        {
            "event": "ready",
            "monitor": [geometry.x, geometry.y, geometry.width, geometry.height],
            "scale": monitor.get_scale_factor(),
        }
    )


def run(gtk, layer_shell):  # pragma: no cover - drives the GTK loop
    """Wire the protocol to the surface and pump the GTK loop."""
    import gi

    GLib = gi.repository.GLib

    app = gtk.Application(application_id="dev.easyspeak.gridydotool.overlay")
    overlay = None
    fatal = None

    def refuse(problem):
        """Emit the refusal instead of the surface that could not be made safe."""
        nonlocal fatal
        fatal = problem
        emit({"event": "error", "message": problem})
        app.quit()
        return False

    def on_activate(_app):
        """Build and present the surface inside the application's lifetime.

        The window cannot be made before `app.run`: GTK refuses to add a window
        created before the `startup` signal ("New application windows must be
        added after the GApplication::startup signal has been emitted"), and an
        application holding no windows returns from `run` as soon as activation
        ends - which would end the helper right after `ready` and leave the
        plugin talking to a corpse.
        """
        nonlocal overlay
        overlay = Overlay(gtk, layer_shell, app)
        overlay.window.present()
        GLib.idle_add(emit_ready_when_mapped)

    def emit_ready_when_mapped():
        """Report geometry only once the surface exists, then stop polling.

        `ready` is what the plugin waits for before it can compute a single
        coordinate, so it must be sent after GTK has a surface to ask, not
        from the constructor where there is none yet - and only after the
        surface has been made click-through, or the click this grid aims would
        be swallowed before it arrived.
        """
        if overlay is None or overlay.window.get_surface() is None:
            return True
        problem = overlay.make_click_through()
        if problem is not None:
            return refuse(problem)
        report_monitor(overlay)
        return False

    app.connect("activate", on_activate)

    pending = ""

    def handle_line(line):
        """Act on one protocol line; False means the process should end."""
        command = parse_command(line)
        if command is None:
            return True
        name = command.get("cmd")
        if name == "show" and overlay is not None:
            problem = overlay.show(command["bounds"])
            if problem is not None:
                return refuse(problem)
        elif name == "hide" and overlay is not None:
            overlay.hide()
        elif name in ("stop", "quit"):
            app.quit()
            return False
        return True

    def on_stdin_ready(_source, _condition):
        """Consume every complete line the descriptor holds, not just one.

        One chunk is read per wake-up and `take_lines` splits every complete
        line out of it - a `stop` behind another command in the same write must
        not be stranded where no watch can see it - while a partial line waits
        in `pending` for the next wake-up.
        """
        nonlocal pending
        chunk = os.read(sys.stdin.fileno(), 4096)
        if not chunk:  # the plugin is gone; never outlive it
            app.quit()
            return False
        lines, pending = take_lines(pending, chunk.decode("utf-8", "replace"))
        # `all` short-circuits at the first `stop`/`quit`, same as a loop would.
        return all(handle_line(line) for line in lines)

    GLib.io_add_watch(sys.stdin.fileno(), GLib.IO_IN | GLib.IO_HUP, on_stdin_ready)
    app.run([])
    return 1 if fatal is not None else 0


def main(argv=None):
    """Entry point: `--probe` one-shots the support question, otherwise serve."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--probe" in argv:
        supported = is_supported()
        sys.stdout.write("supported\n" if supported else "unsupported\n")
        return 0 if supported else 1
    Gtk, layer_shell = load_layer_shell()
    if Gtk is None:
        emit(
            {
                "event": "error",
                "message": "PyGObject or the layer-shell typelib is missing",
            }
        )
        return 1
    if not layer_shell.is_supported():
        # Refuse rather than degrade: an ordinary window would look like a
        # rendering bug while the injections kept working.
        emit({"event": "error", "message": "layer-shell unsupported"})
        return 1
    return run(Gtk, layer_shell)


if __name__ == "__main__":
    raise SystemExit(main())
