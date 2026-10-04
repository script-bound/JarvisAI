"""Visual layer for JARVIS: theme, widgets, the arc reactor and the mini window.

Nothing in here talks to the backend. The widgets only read a few attributes
from the app object (state, animating, audio) so they stay easy to restyle.
"""

from __future__ import annotations

import base64
import io
import math
import random
import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

try:
    from PIL import Image, ImageDraw
except ImportError:  # only needed for the tray icon and window icon
    Image = ImageDraw = None

# --------------------------------------------------------------- palette
BG = "#02050a"
PANEL = "#07101a"
PANEL_2 = "#0b1826"
PANEL_3 = "#112438"
BORDER = "#15303f"
BORDER_HI = "#1f4d63"
CYAN = "#19e3ff"
CYAN_DIM = "#0e7f93"
CYAN_DARK = "#073340"
WHITE = "#e6f6ff"
GRAY = "#7a92a6"
GREEN = "#2cff9a"
AMBER = "#ffc247"
RED = "#ff5470"

# state -> (accent colour, spin speed, bar behaviour)
STATES = {
    "BOOTING": (CYAN, 5.0, "wave"),
    "ONLINE": (CYAN, 1.0, "idle"),
    "STANDBY": (CYAN_DIM, 0.6, "idle"),
    "LISTENING": (GREEN, 1.8, "voice"),
    "TRANSCRIBING": (AMBER, 3.0, "wave"),
    "THINKING": (AMBER, 4.0, "wave"),
    "SPEAKING": (CYAN, 2.0, "voice"),
    "CONFIRM": (AMBER, 2.0, "idle"),
    "ALERT": (AMBER, 3.0, "voice"),
    "ERROR": (RED, 0.3, "flat"),
    "VOICE ERROR": (RED, 0.3, "flat"),
}
SHORT = {"TRANSCRIBING": "DECODING", "VOICE ERROR": "AUDIO FAULT"}


def state_color(state: str) -> str:
    return STATES.get(state, STATES["ONLINE"])[0]


def blend(fg: str, bg: str, t: float) -> str:
    f = [int(fg[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(b[k] + (f[k] - b[k]) * t) for k in range(3))


# ----------------------------------------------------------------- fonts
FONTS = {"hud": "Consolas", "ui": "Segoe UI", "mono": "Consolas"}


def init_fonts(root: tk.Misc) -> None:
    """Pick the best installed fonts. Tk silently substitutes missing ones, so check first."""
    have = {f.lower(): f for f in tkfont.families(root)}

    def pick(*names, default):
        for n in names:
            if n.lower() in have:
                return have[n.lower()]
        return default

    FONTS["hud"] = pick("Bahnschrift", "Segoe UI Semibold", "Consolas", "DejaVu Sans Mono", default="Courier")
    FONTS["ui"] = pick("Segoe UI Variable Text", "Segoe UI", "Helvetica Neue", "DejaVu Sans", default="Helvetica")
    FONTS["mono"] = pick("Cascadia Mono", "Consolas", "DejaVu Sans Mono", default="Courier")


def fnt(kind: str, size: int, bold: bool = False):
    return (FONTS[kind], size, "bold" if bold else "normal")


# ------------------------------------------------------ window chrome bits
def style_dark_titlebar(window: tk.Tk) -> None:
    """Windows 10/11: dark native title bar that matches the HUD. No-op elsewhere."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        for attr, value in ((20, 1), (19, 1)):  # immersive dark mode (new / old attribute id)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(ctypes.c_int(value)), 4)
        r, g, b = (int(PANEL[i:i + 2], 16) for i in (1, 3, 5))
        colour = ctypes.c_int(r | (g << 8) | (b << 16))  # caption colour, Windows 11
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(colour), 4)
    except Exception:
        pass


def style_scrollbar(root: tk.Tk) -> None:
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        return
    style.layout("Hud.Vertical.TScrollbar", [
        ("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
            ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
    style.configure("Hud.Vertical.TScrollbar", troughcolor=PANEL, background=BORDER_HI,
                    bordercolor=PANEL, lightcolor=BORDER_HI, darkcolor=BORDER_HI, gripcount=0, width=8)
    style.map("Hud.Vertical.TScrollbar", background=[("active", CYAN_DIM), ("pressed", CYAN)])


def render_icon(color: str = CYAN, size: int = 64):
    """The JARVIS ring icon, drawn with Pillow. Returns a PIL image or None."""
    if Image is None:
        return None
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = size / 64
    d.ellipse((2 * m, 2 * m, 62 * m, 62 * m), fill=PANEL)
    d.ellipse((2 * m, 2 * m, 62 * m, 62 * m), outline=blend(color, PANEL, 0.35), width=max(1, round(2 * m)))
    for start in (200, 20):
        d.arc((9 * m, 9 * m, 55 * m, 55 * m), start, start + 130, fill=color, width=max(2, round(4 * m)))
    d.ellipse((24 * m, 24 * m, 40 * m, 40 * m), fill=color)
    d.ellipse((29 * m, 29 * m, 35 * m, 35 * m), fill=PANEL)
    return img


def icon_photo(color: str = CYAN, size: int = 64):
    """tk.PhotoImage of the icon (Tk 8.6 reads PNG data natively, no ImageTk needed)."""
    img = render_icon(color, size)
    if img is None:
        return None
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return tk.PhotoImage(data=base64.b64encode(buf.getvalue()))


# --------------------------------------------------------------- widgets
BUTTON_STYLES = {
    "primary": dict(bg=CYAN_DARK, fg=CYAN, hover_bg=CYAN, hover_fg=BG, border=CYAN_DIM),
    "ghost": dict(bg=PANEL_2, fg=WHITE, hover_bg=PANEL_3, hover_fg=CYAN, border=BORDER),
    "chip": dict(bg=PANEL, fg=CYAN, hover_bg=CYAN_DARK, hover_fg=WHITE, border=BORDER),
    "on": dict(bg=CYAN_DARK, fg=GREEN, hover_bg=PANEL_3, hover_fg=GREEN, border=GREEN),
    "danger": dict(bg=PANEL_2, fg=RED, hover_bg=RED, hover_fg=BG, border="#5a1f2b"),
}


class HudButton(tk.Label):
    """Flat label-based button with hover and disabled states (no native chrome)."""

    def __init__(self, parent, text, command=None, style="ghost", font=None, padx=14, pady=8, **kw):
        st = BUTTON_STYLES[style]
        super().__init__(parent, text=text, bg=st["bg"], fg=st["fg"], font=font or fnt("hud", 9, True),
                         padx=padx, pady=pady, cursor="hand2", highlightthickness=1,
                         highlightbackground=st["border"], **kw)
        self.command = command
        self.style_name = style
        self.enabled = True
        self._hover = False
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._release)

    def _paint(self):
        st = BUTTON_STYLES[self.style_name]
        if not self.enabled:
            self.configure(bg=PANEL, fg=blend(GRAY, PANEL, 0.6), highlightbackground=BORDER, cursor="arrow")
        elif self._hover:
            self.configure(bg=st["hover_bg"], fg=st["hover_fg"], highlightbackground=st["hover_bg"], cursor="hand2")
        else:
            self.configure(bg=st["bg"], fg=st["fg"], highlightbackground=st["border"], cursor="hand2")

    def _set_hover(self, on):
        self._hover = on
        self._paint()

    def _release(self, event):
        inside = 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()
        if self.enabled and inside and self.command:
            self.command()

    def set_enabled(self, on: bool):
        self.enabled = on
        self._paint()

    def set_style(self, style: str):
        self.style_name = style
        self._paint()

    def set_text(self, text: str):
        self.configure(text=text)


class Panel(tk.Frame):
    """Dark panel with chamfered corners and cyan corner accents. Fill `.content`."""

    CUT = 14

    def __init__(self, parent, title: str = "", tag: str = ""):
        super().__init__(parent, bg=BG)
        self.deco = tk.Canvas(self, bg=BG, highlightthickness=0)
        self.deco.place(x=0, y=0, relwidth=1, relheight=1)
        self.deco.bind("<Configure>", self._draw)

        body = tk.Frame(self, bg=PANEL)
        body.pack(fill="both", expand=True, padx=9, pady=9)

        if title:
            head = tk.Frame(body, bg=PANEL)
            head.pack(fill="x", padx=12, pady=(10, 6))
            tk.Frame(head, bg=CYAN, width=3, height=13).pack(side="left", padx=(0, 8))
            tk.Label(head, text=title, fg=WHITE, bg=PANEL, font=fnt("hud", 10, True)).pack(side="left")
            self.tag = tk.Label(head, text=tag, fg=GRAY, bg=PANEL, font=fnt("hud", 8))
            self.tag.pack(side="right")
            tk.Frame(body, bg=BORDER, height=1).pack(fill="x", padx=12)

        self.content = tk.Frame(body, bg=PANEL)
        self.content.pack(fill="both", expand=True)

    def set_tag(self, text: str):
        if hasattr(self, "tag"):
            self.tag.configure(text=text)

    def _draw(self, event):
        c, w, h, k = self.deco, event.width, event.height, self.CUT
        c.delete("all")
        c.create_polygon(k, 3, w - 3, 3, w - 3, h - k, w - k, h - 3, 3, h - 3, 3, k,
                         outline=BORDER_HI, fill="", width=1)
        a = 22  # accent length
        c.create_line(3, k + a, 3, k, k, 3, k + a, 3, fill=CYAN, width=2)            # top-left, chamfered
        c.create_line(w - 3, h - k - a, w - 3, h - k, w - k, h - 3, w - k - a, h - 3, fill=CYAN, width=2)
        c.create_line(w - 3, 3, w - 3, 3 + a, fill=CYAN_DIM, width=2)
        c.create_line(w - 3, 3, w - 3 - a, 3, fill=CYAN_DIM, width=2)
        c.create_line(3, h - 3, 3 + a, h - 3, fill=CYAN_DIM, width=2)
        c.create_line(3, h - 3, 3, h - 3 - a, fill=CYAN_DIM, width=2)


class StatusDot(tk.Canvas):
    def __init__(self, parent, bg=PANEL, size=12):
        super().__init__(parent, width=size, height=size, bg=bg, highlightthickness=0)
        self.halo = self.create_oval(0, 0, size, size, outline="")
        self.dot = self.create_oval(3, 3, size - 3, size - 3, outline="")

    def set_color(self, color: str, bg=PANEL):
        self.itemconfigure(self.halo, fill=blend(color, bg, 0.3))
        self.itemconfigure(self.dot, fill=color)


class Beam(tk.Canvas):
    """A thin accent line with a light that sweeps along it."""

    def __init__(self, parent, app, bg=BG):
        super().__init__(parent, height=3, bg=bg, highlightthickness=0)
        self.app = app
        self.x = 0.0
        self.line = self.create_rectangle(0, 1, 1, 2, fill=CYAN_DARK, outline="")
        self.glow = [self.create_rectangle(0, 0, 0, 3, fill=CYAN_DARK, outline="") for _ in range(10)]
        self.bind("<Configure>", lambda e: self.coords(self.line, 0, 1, e.width, 2))
        self.after(40, self.tick)

    def tick(self):
        if not getattr(self.app, "animating", True):
            self.after(300, self.tick)
            return
        w = max(self.winfo_width(), 1)
        color = state_color(self.app.state)
        self.x = (self.x + 7 + 5 * (STATES.get(self.app.state, STATES["ONLINE"])[1] > 2)) % (w + 160)
        for i, item in enumerate(self.glow):
            x1 = self.x - i * 16
            self.coords(item, x1 - 16, 0, x1, 3)
            self.itemconfigure(item, fill=blend(color, BG, max(0.0, 0.95 - i * 0.1)), state="normal" if 0 < x1 < w + 16 else "hidden")
        self.after(40, self.tick)


class Reactor(tk.Canvas):
    """Animated arc reactor. Colour, spin and the bar ring follow app.state."""

    def __init__(self, parent, app, size: int = 300, bg: str = PANEL):
        super().__init__(parent, width=size, height=size, bg=bg, highlightthickness=0)
        self.app = app
        self.size = size
        self.s = size / 300
        self.c = size / 2
        self.bg = bg
        self.t = 0.0
        self.rot = 0.0
        self.shown_state = None
        self.show_label = size >= 200
        self.n_ticks = 60 if size >= 200 else 24
        self.n_bars = 48 if size >= 200 else 24
        self.amps = [0.05] * self.n_bars
        s, c = self.s, self.c

        def w(x):
            return max(1, round(x * s))

        self.glow_fill = [self.create_oval(c - r * s, c - r * s, c + r * s, c + r * s, outline="") for r in (150, 124, 100)]
        self.ticks = [self.create_line(0, 0, 0, 0, width=w(1)) for _ in range(self.n_ticks)]
        self.scan = [self.create_arc(c - 134 * s, c - 134 * s, c + 134 * s, c + 134 * s, start=0, extent=14,
                                     style="arc", width=w(2)) for _ in range(8)]
        self.arcs_a = [self.create_arc(c - 124 * s, c - 124 * s, c + 124 * s, c + 124 * s, start=o, extent=70,
                                       style="arc", width=w(3)) for o in (0, 120, 240)]
        self.arcs_b = [self.create_arc(c - 106 * s, c - 106 * s, c + 106 * s, c + 106 * s, start=o, extent=140,
                                       style="arc", width=w(5)) for o in (0, 180)]
        self.ring = self.create_oval(c - 92 * s, c - 92 * s, c + 92 * s, c + 92 * s, width=w(1))
        self.bars = [self.create_line(0, 0, 0, 0, width=w(3), capstyle="round") for _ in range(self.n_bars)]
        self.halo = [self.create_oval(0, 0, 0, 0, width=w(wd)) for wd in (2, 1)]
        self.core = self.create_oval(c - 44 * s, c - 44 * s, c + 44 * s, c + 44 * s, width=w(2), fill=PANEL_2)
        self.label = self.create_text(c, c - 4 * s, text="", font=fnt("hud", 9, True))
        self.sub = self.create_text(c, c + 12 * s, text="", font=fnt("hud", 7))

        self.after(33, self.tick)

    def recolor(self, state: str):
        color = state_color(state)
        bg = self.bg
        for item, k in zip(self.glow_fill, (0.05, 0.08, 0.11)):
            self.itemconfigure(item, fill=blend(color, bg, k))
        for item in self.ticks:
            self.itemconfigure(item, fill=blend(color, bg, 0.55))
        for i, item in enumerate(self.scan):
            self.itemconfigure(item, outline=blend(color, bg, max(0.08, 0.95 - i * 0.12)))
        for item in self.arcs_a:
            self.itemconfigure(item, outline=color)
        for item in self.arcs_b:
            self.itemconfigure(item, outline=blend(color, bg, 0.6))
        for item in self.bars:
            self.itemconfigure(item, fill=color)
        self.itemconfigure(self.ring, outline=blend(color, bg, 0.3))
        self.itemconfigure(self.halo[0], outline=blend(color, bg, 0.5))
        self.itemconfigure(self.halo[1], outline=blend(color, bg, 0.25))
        self.itemconfigure(self.core, outline=color)
        label = SHORT.get(state, state) if self.show_label else ""
        self.itemconfigure(self.label, fill=color, text=label)
        self.itemconfigure(self.sub, fill=GRAY, text="J.A.R.V.I.S" if self.show_label else "")
        self.shown_state = state

    def tick(self):
        if not getattr(self.app, "animating", True) or not self.winfo_viewable():
            self.after(250, self.tick)
            return

        state = self.app.state
        _, speed, mode = STATES.get(state, STATES["ONLINE"])
        if state != self.shown_state:
            self.recolor(state)

        self.t += 0.033
        self.rot += speed
        c, s = self.c, self.s

        for i, item in enumerate(self.ticks):
            a = math.radians(i * 360 / self.n_ticks + self.rot * 0.15)
            r1 = 148 * s
            r0 = r1 - (10 if i % 5 == 0 else 5) * s
            self.coords(item, c + r0 * math.cos(a), c + r0 * math.sin(a), c + r1 * math.cos(a), c + r1 * math.sin(a))

        head = (-self.rot * 0.9) % 360
        for i, item in enumerate(self.scan):
            self.itemconfigure(item, start=(head + i * 14) % 360)
        for k, item in enumerate(self.arcs_a):
            self.itemconfigure(item, start=(k * 120 + self.rot * 0.8) % 360)
        for k, item in enumerate(self.arcs_b):
            self.itemconfigure(item, start=(k * 180 - self.rot * 1.2) % 360)

        level = getattr(getattr(self.app, "audio", None), "level", 0.0)
        for i, item in enumerate(self.bars):
            if state == "LISTENING":
                target = 0.08 + 0.92 * level * (0.6 + 0.4 * abs(math.sin(self.t * 7 + i * 0.7)))
            elif mode == "voice":
                target = 0.25 + 0.75 * abs(math.sin(self.t * 7 + i * 0.7)) * random.uniform(0.4, 1.0)
            elif mode == "wave":
                target = 0.2 + 0.6 * (0.5 + 0.5 * math.sin(self.t * 9 - i * 0.45))
            elif mode == "idle":
                target = 0.06 + 0.04 * math.sin(self.t * 2 + i * 0.3)
            else:
                target = 0.03
            self.amps[i] += (target - self.amps[i]) * 0.35
            a = 2 * math.pi * i / self.n_bars - math.pi / 2
            r0, r1 = 56 * s, (58 + self.amps[i] * 28) * s
            self.coords(item, c + r0 * math.cos(a), c + r0 * math.sin(a), c + r1 * math.cos(a), c + r1 * math.sin(a))

        pulse = 0.5 + 0.5 * math.sin(self.t * 3 * math.sqrt(speed))
        for item, base, swing in ((self.halo[0], 48, 3), (self.halo[1], 52, 4)):
            r = (base + swing * pulse) * s
            self.coords(item, c - r, c - r, c + r, c + r)

        self.after(33, self.tick)


class Telemetry(tk.Canvas):
    """Segmented meters plus key/value rows."""

    SEGS = 18

    def __init__(self, parent, bars=("CPU", "MEM", "BAT"), rows=("LAST REPLY", "UPTIME"), width=270):
        height = 14 + len(bars) * 26 + 10 + len(rows) * 22
        super().__init__(parent, width=width, height=height, bg=PANEL, highlightthickness=0)
        self.w = width
        self.segs, self.vals, self.rows = {}, {}, {}
        seg_w, gap = 7, 3
        x0 = 44
        y = 16
        for name in bars:
            self.create_text(6, y, text=name, anchor="w", fill=GRAY, font=fnt("hud", 8, True))
            self.segs[name] = [self.create_rectangle(x0 + i * (seg_w + gap), y - 6, x0 + i * (seg_w + gap) + seg_w, y + 6,
                                                     fill=CYAN_DARK, outline="") for i in range(self.SEGS)]
            self.vals[name] = self.create_text(width - 4, y, text="N/A", anchor="e", fill=WHITE, font=fnt("hud", 8, True))
            y += 26
        y += 8
        self.create_line(6, y - 12, width - 6, y - 12, fill=BORDER)
        for name in rows:
            self.create_text(6, y, text=name, anchor="w", fill=GRAY, font=fnt("hud", 8, True))
            self.rows[name] = self.create_text(width - 4, y, text="--", anchor="e", fill=WHITE, font=fnt("hud", 9))
            y += 22

    def refresh(self, bars: dict, rows: dict):
        for name, (pct, text) in bars.items():
            lit = 0 if pct is None else round(pct / 100 * self.SEGS)
            color = RED if (pct or 0) > 85 else AMBER if (pct or 0) > 65 else CYAN
            for i, seg in enumerate(self.segs[name]):
                self.itemconfigure(seg, fill=color if i < lit else CYAN_DARK)
            self.itemconfigure(self.vals[name], text=text if text is not None else "N/A")
        for name, text in rows.items():
            self.itemconfigure(self.rows[name], text=text)


class ListCanvas(tk.Canvas):
    """A compact list: badge, text, right-hand value and an optional progress line."""

    ROW_H = 40

    def __init__(self, parent, rows=5, empty="NOTHING HERE", on_activate=None):
        super().__init__(parent, bg=PANEL, highlightthickness=0, height=rows * self.ROW_H + 6, width=250)
        self.max_rows = rows
        self.empty = empty
        self.on_activate = on_activate
        self.rows: list[dict] = []
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Double-Button-1>", self._double)

    def set_rows(self, rows: list[dict]):
        rows = rows[: self.max_rows]
        if rows != self.rows:
            self.rows = rows
            self.redraw()

    def redraw(self):
        self.delete("all")
        w = max(self.winfo_width(), 200)
        if not self.rows:
            self.create_text(w / 2, self.ROW_H, text=self.empty, fill=blend(GRAY, PANEL, 0.7), font=fnt("hud", 8))
            return
        for i, row in enumerate(self.rows):
            y = 4 + i * self.ROW_H
            color = row.get("color", CYAN)
            self.create_rectangle(6, y + 8, 6 + 38, y + 24, outline=blend(color, PANEL, 0.6), fill=blend(color, PANEL, 0.12))
            self.create_text(6 + 19, y + 16, text=row.get("badge", ""), fill=color, font=fnt("hud", 7, True))
            text = row.get("text", "")
            limit = max(10, int((w - 150) / 7))
            self.create_text(54, y + 16, text=text if len(text) <= limit else text[: limit - 1] + "...",
                             anchor="w", fill=WHITE, font=fnt("ui", 10))
            self.create_text(w - 6, y + 16, text=row.get("right", ""), anchor="e", fill=color, font=fnt("hud", 9, True))
            prog = row.get("progress")
            if prog is not None:
                self.create_rectangle(54, y + 31, w - 6, y + 33, outline="", fill=BORDER)
                self.create_rectangle(54, y + 31, 54 + (w - 60) * max(0.0, min(1.0, prog)), y + 33, outline="", fill=color)

    def _double(self, event):
        idx = int((event.y - 4) // self.ROW_H)
        if self.on_activate and 0 <= idx < len(self.rows):
            self.on_activate(idx)


class MiniHUD(tk.Toplevel):
    """Small always-on-top window for working with JARVIS while it stays out of the way."""

    W, H = 480, 176

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.pinned = False
        self._timer = None
        self._pos = None
        self._drag = (0, 0)
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        try:
            self.attributes("-alpha", 0.97)
        except tk.TclError:
            pass
        self.configure(bg=CYAN_DIM)

        inner = tk.Frame(self, bg=PANEL)
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        left = tk.Frame(inner, bg=PANEL)
        left.pack(side="left", padx=(10, 2), pady=10)
        self.reactor = Reactor(left, app, size=112)
        self.reactor.pack()

        right = tk.Frame(inner, bg=PANEL)
        right.pack(side="left", fill="both", expand=True, padx=(4, 12), pady=10)

        top = tk.Frame(right, bg=PANEL)
        top.pack(fill="x")
        self.title_lbl = tk.Label(top, text="J.A.R.V.I.S", fg=CYAN, bg=PANEL, font=fnt("hud", 11, True))
        self.title_lbl.pack(side="left")
        self.state_lbl = tk.Label(top, text="ONLINE", fg=CYAN, bg=PANEL, font=fnt("hud", 8, True))
        self.state_lbl.pack(side="right")

        self.reply = tk.Label(right, text="Standing by.", fg=WHITE, bg=PANEL, font=fnt("ui", 10),
                              wraplength=300, justify="left", anchor="nw", height=4)
        self.reply.pack(fill="x", pady=(6, 6))

        row = tk.Frame(right, bg=PANEL)
        row.pack(fill="x", side="bottom")
        self.entry = tk.Entry(row, bg=PANEL_2, fg=WHITE, insertbackground=CYAN, relief="flat", borderwidth=0,
                              font=fnt("ui", 10), highlightthickness=1, highlightbackground=BORDER, highlightcolor=CYAN)
        self.entry.pack(side="left", fill="x", expand=True, ipady=5, padx=(0, 6))
        self.entry.bind("<Return>", self._submit)
        self.entry.bind("<Escape>", lambda e: self.hide())
        self.mic = HudButton(row, "MIC", app.start_voice, "primary", padx=9, pady=4, font=fnt("hud", 8, True))
        self.mic.pack(side="left", padx=(0, 4))
        HudButton(row, "OPEN", app.show_main, "ghost", padx=9, pady=4, font=fnt("hud", 8, True)).pack(side="left", padx=(0, 4))
        HudButton(row, "X", self.hide, "ghost", padx=8, pady=4, font=fnt("hud", 8, True)).pack(side="left")

        for w in (inner, left, right, top, self.title_lbl, self.state_lbl, self.reply, self.reactor):
            w.bind("<ButtonPress-1>", self._grab)
            w.bind("<B1-Motion>", self._move)
        self.after(250, self._sync)

    # ----------------------------------------------------------- dragging
    def _grab(self, e):
        self._drag = (e.x_root - self.winfo_x(), e.y_root - self.winfo_y())

    def _move(self, e):
        self._pos = (e.x_root - self._drag[0], e.y_root - self._drag[1])
        self.geometry(f"+{self._pos[0]}+{self._pos[1]}")

    # ------------------------------------------------------------ showing
    def show(self, pin: bool = False, focus: bool = False):
        self.pinned = self.pinned or pin
        if not self.winfo_viewable():
            if self._pos is None:
                x = self.winfo_screenwidth() - self.W - 24
                y = self.winfo_screenheight() - self.H - 96
                self._pos = (x, y)
            self.geometry(f"{self.W}x{self.H}+{self._pos[0]}+{self._pos[1]}")
            self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        if focus:
            self.entry.focus_force()
        self._arm()

    def hide(self):
        self.pinned = False
        self._cancel()
        self.withdraw()

    @property
    def visible(self) -> bool:
        return bool(self.winfo_viewable())

    def touch(self):
        if self.visible:
            self._arm()

    def _cancel(self):
        if self._timer:
            self.after_cancel(self._timer)
            self._timer = None

    def _arm(self):
        self._cancel()
        if not self.pinned:
            self._timer = self.after(10000, self._auto_hide)

    def _auto_hide(self):
        busy = self.app.state in ("LISTENING", "THINKING", "TRANSCRIBING", "SPEAKING", "CONFIRM")
        typing = self.focus_get() is self.entry and bool(self.entry.get())
        if busy or typing or self.pinned:
            self._arm()
        else:
            self.hide()

    # ------------------------------------------------------------ content
    def set_reply(self, text: str):
        text = " ".join(str(text).split())
        self.reply.configure(text=text if len(text) <= 230 else text[:227] + "...")

    def _submit(self, event=None):
        text = self.entry.get().strip()
        if text:
            self.entry.delete(0, "end")
            self.app.send_message(text)
        return "break"

    def _sync(self):
        if self.visible:
            state = self.app.state
            self.state_lbl.configure(text=SHORT.get(state, state), fg=state_color(state))
            self.mic.set_enabled(not self.app.processing)
        self.after(250, self._sync)
