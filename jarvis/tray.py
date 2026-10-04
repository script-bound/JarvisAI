"""Background-running helpers: tray icon, global hotkeys, single-instance guard.

All three are optional. If a package is missing the feature quietly turns off
and JARVIS tells you which package to install.

    pip install pystray pynput pillow psutil
"""

from __future__ import annotations

import socket
import threading

from .hud import render_icon

try:
    import pystray
except Exception:  # not installed, or no display backend
    pystray = None

try:
    from pynput import keyboard as pynput_keyboard
except Exception:
    pynput_keyboard = None


class Tray:
    """Tray icon with Open / Mini / Listen / Mute / Quit. Callbacks hop onto the Tk thread."""

    def __init__(self, app):
        self.app = app
        self.icon = None
        self.color = None
        self.error = ""

    @property
    def available(self) -> bool:
        return self.icon is not None

    def start(self) -> bool:
        if pystray is None:
            self.error = "pystray is not installed (pip install pystray pillow)"
            return False
        image = render_icon()
        if image is None:
            self.error = "Pillow is not installed (pip install pillow)"
            return False
        app = self.app
        try:
            menu = pystray.Menu(
                pystray.MenuItem("Open JARVIS", lambda icon, item: app.ui(app.show_main), default=True),
                pystray.MenuItem("Mini HUD", lambda icon, item: app.ui(app.toggle_mini)),
                pystray.MenuItem("Listen now", lambda icon, item: app.ui(app.summon)),
                pystray.MenuItem("Mute voice", lambda icon, item: app.ui(app.toggle_mute),
                                 checked=lambda item: app.muted),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", lambda icon, item: app.ui(app.quit_app)),
            )
            self.icon = pystray.Icon("jarvis", image, "JARVIS", menu)
            self.icon.run_detached()
            return True
        except Exception as exc:  # e.g. no display backend on Linux
            self.icon = None
            self.error = str(exc)
            return False

    def set_state(self, color: str, title: str):
        if not self.icon or color == self.color:
            if self.icon:
                self.icon.title = f"JARVIS - {title}"
            return
        self.color = color
        try:
            image = render_icon(color)
            if image is not None:
                self.icon.icon = image
            self.icon.title = f"JARVIS - {title}"
        except Exception:
            pass

    def notify(self, title: str, message: str):
        if not self.icon:
            return
        try:
            self.icon.notify(message[:240], title)
        except Exception:
            pass

    def stop(self):
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
            self.icon = None


def parse_hotkey(combo: str) -> str:
    """'ctrl+alt+j' -> '<ctrl>+<alt>+j' (the format pynput wants)."""
    names = {"ctrl": "<ctrl>", "control": "<ctrl>", "alt": "<alt>", "shift": "<shift>",
             "win": "<cmd>", "cmd": "<cmd>", "super": "<cmd>", "space": "<space>",
             "esc": "<esc>", "enter": "<enter>", "tab": "<tab>"}
    out = []
    for part in combo.lower().replace(" ", "").split("+"):
        if not part:
            continue
        if part in names:
            out.append(names[part])
        elif len(part) > 1 and part[0] == "f" and part[1:].isdigit():
            out.append(f"<{part}>")
        else:
            out.append(part)
    return "+".join(out)


class Hotkeys:
    def __init__(self):
        self.listener = None
        self.error = ""
        self.active: dict[str, str] = {}

    def start(self, bindings: dict[str, callable]) -> bool:
        """bindings: {'ctrl+alt+j': callback}"""
        if pynput_keyboard is None:
            self.error = "pynput is not installed (pip install pynput)"
            return False
        try:
            mapping = {parse_hotkey(k): v for k, v in bindings.items()}
            self.listener = pynput_keyboard.GlobalHotKeys(mapping)
            self.listener.daemon = True
            self.listener.start()
            self.active = {k: v.__name__ for k, v in bindings.items()}
            return True
        except Exception as exc:
            self.error = str(exc)
            self.listener = None
            return False

    def stop(self):
        if self.listener:
            try:
                self.listener.stop()
            except Exception:
                pass
            self.listener = None


class SingleInstance:
    """Second launch pokes the first one to show itself, then exits."""

    TOKEN = b"JARVIS-SHOW\n"
    REPLY = b"OK\n"

    def __init__(self, port: int = 47653):
        self.port = port
        self.sock: socket.socket | None = None

    def acquire(self) -> bool:
        """True if we are the only instance (or the port is not ours to judge)."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", self.port))
            sock.listen(2)
            self.sock = sock
            return True
        except OSError:
            sock.close()
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=0.6) as c:
                c.sendall(self.TOKEN)
                c.settimeout(0.6)
                return c.recv(8) != self.REPLY  # a real JARVIS answered -> do not start another
        except OSError:
            return True

    def listen(self, on_show):
        if not self.sock:
            return

        def loop():
            while True:
                try:
                    conn, _ = self.sock.accept()
                except OSError:
                    return
                try:
                    conn.settimeout(1)
                    if conn.recv(32).startswith(b"JARVIS-SHOW"):
                        conn.sendall(self.REPLY)
                        on_show()
                except OSError:
                    pass
                finally:
                    conn.close()

        threading.Thread(target=loop, daemon=True).start()

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
