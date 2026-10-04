from __future__ import annotations

import logging
import math
import queue
import random
import re
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from .app import handle_command
from .audio import AudioService
from .config import load_settings
from .gemini import GeminiService
from .logger import setup_logger
from .memory import MemoryStore
from .security import SecurityManager
from .tools.registry import ToolRegistry
from .tools.safe_tools import register_safe_tools

try:
    import psutil
except ImportError:
    psutil = None


ROOT = Path(__file__).resolve().parent
TTS_DIR = ROOT / "data" / "tts"

load_dotenv(Path.home() / ".env")
load_dotenv(ROOT / ".env")


# ------------------------------------------------------------- identity

ADDRESS = "Mr. Tyagi"
SPEAK_GREETING = True
MAX_SPOKEN_CHARS = 700

PERSONA = (
    f"You are JARVIS, a polished, dry-witted AI assistant. The user is {ADDRESS}; "
    f"address him as '{ADDRESS}' naturally, the way JARVIS addresses Tony Stark. "
    "Keep replies to one to three sentences unless he explicitly asks for detail. "
    "Do not use markdown, bullet points or emoji, because replies are read aloud."
)

ACKS = [
    f"Of course, {ADDRESS}.",
    f"Right away, {ADDRESS}.",
    f"Certainly, {ADDRESS}.",
]


# --------------------------------------------------------------- theme

BG = "#03060a"
PANEL = "#08101a"
PANEL_2 = "#0c1622"
BORDER = "#16303f"
CYAN = "#00e5ff"
CYAN_DARK = "#07323b"
WHITE = "#e8f7ff"
GRAY = "#7f95a6"
GREEN = "#00e676"
RED = "#ff5252"
YELLOW = "#ffd740"

UI = "Segoe UI"
MONO = "Consolas"


STATES = {
    "BOOTING": (CYAN, 5.0, "wave"),
    "ONLINE": (CYAN, 1.0, "idle"),
    "LISTENING": (GREEN, 1.8, "voice"),
    "TRANSCRIBING": (YELLOW, 3.0, "wave"),
    "THINKING": (YELLOW, 4.0, "wave"),
    "SPEAKING": (CYAN, 2.0, "voice"),
    "ERROR": (RED, 0.3, "flat"),
    "VOICE ERROR": (RED, 0.3, "flat"),
}

SHORT = {
    "TRANSCRIBING": "DECODING",
    "VOICE ERROR": "AUDIO FAULT",
}

KINDS = {
    "user": CYAN,
    "jarvis": GREEN,
    "system": YELLOW,
    "error": RED,
}

QUICK_COMMANDS = [
    ("TIME", "What time is it?"),
    ("DATE", "What is today's date?"),
    ("MATH", "What is 25 times 8?"),
    ("HELP", "What can you do?"),
]


# ------------------------------------------------------------- helpers

def blend(fg: str, bg: str, t: float) -> str:
    f = [int(fg[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]

    return "#%02x%02x%02x" % tuple(
        round(b[k] + (f[k] - b[k]) * t)
        for k in range(3)
    )


def greeting_for_hour() -> str:
    h = datetime.now().hour

    if h < 5:
        return "Working late"

    if h < 12:
        return "Good morning"

    if h < 18:
        return "Good afternoon"

    return "Good evening"


def split_for_speech(
    text: str,
    first_max: int = 140,
    max_len: int = 260,
) -> list[str]:

    text = re.sub(r"[*_`#>]+", "", text)
    text = " ".join(text.split())

    if len(text) > MAX_SPOKEN_CHARS:
        cut = text[:MAX_SPOKEN_CHARS]

        end = max(
            cut.rfind(". "),
            cut.rfind("! "),
            cut.rfind("? "),
        )

        text = cut[:end + 1] if end > 80 else cut

    chunks = []
    cur = ""

    for sentence in re.split(r"(?<=[.!?])\s+", text):
        limit = first_max if not chunks else max_len

        if cur and len(cur) + len(sentence) + 1 > limit:
            chunks.append(cur)
            cur = sentence
        else:
            cur = f"{cur} {sentence}".strip()

    if cur:
        chunks.append(cur)

    return chunks


def button(
    parent,
    text,
    command,
    bg,
    fg,
    active_bg,
    active_fg,
    font,
    **kw,
):
    return tk.Button(
        parent,
        text=text,
        command=command,
        bg=bg,
        fg=fg,
        activebackground=active_bg,
        activeforeground=active_fg,
        relief="flat",
        borderwidth=0,
        cursor="hand2",
        font=font,
        **kw,
    )


# ------------------------------------------------------------- widgets

class Panel(tk.Frame):

    def __init__(self, parent, title: str = ""):
        super().__init__(parent, bg=BG)

        self.deco = tk.Canvas(
            self,
            bg=BG,
            highlightthickness=0,
        )

        self.deco.place(
            x=0,
            y=0,
            relwidth=1,
            relheight=1,
        )

        self.deco.bind(
            "<Configure>",
            self._draw,
        )

        body = tk.Frame(
            self,
            bg=PANEL,
        )

        body.pack(
            fill="both",
            expand=True,
            padx=8,
            pady=8,
        )

        if title:
            tk.Label(
                body,
                text=f"▌{title}",
                fg=CYAN,
                bg=PANEL,
                font=(MONO, 9, "bold"),
            ).pack(
                anchor="w",
                padx=12,
                pady=(8, 4),
            )

            tk.Frame(
                body,
                bg=BORDER,
                height=1,
            ).pack(
                fill="x",
                padx=10,
            )

        self.content = tk.Frame(
            body,
            bg=PANEL,
        )

        self.content.pack(
            fill="both",
            expand=True,
        )

    def _draw(self, event):
        c = self.deco
        w = event.width
        h = event.height
        n = 18

        c.delete("all")

        c.create_rectangle(
            4,
            4,
            w - 4,
            h - 4,
            outline=BORDER,
        )

        for x, y, dx, dy in (
            (1, 1, 1, 1),
            (w - 2, 1, -1, 1),
            (1, h - 2, 1, -1),
            (w - 2, h - 2, -1, -1),
        ):
            c.create_line(
                x,
                y,
                x + dx * n,
                y,
                fill=CYAN,
                width=2,
            )

            c.create_line(
                x,
                y,
                x,
                y + dy * n,
                fill=CYAN,
                width=2,
            )


class Reactor(tk.Canvas):

    BARS = 48
    TICKS = 60

    def __init__(self, parent, app, size: int = 300):
        super().__init__(
            parent,
            width=size,
            height=size,
            bg=PANEL,
            highlightthickness=0,
        )

        self.app = app
        self.c = size / 2
        self.t = 0.0
        self.rot = 0.0
        self.shown_state = None
        self.amps = [0.05] * self.BARS

        c = self.c

        self.ticks = [
            self.create_line(0, 0, 0, 0)
            for _ in range(self.TICKS)
        ]

        self.arcs_a = [
            self.create_arc(
                c - 132,
                c - 132,
                c + 132,
                c + 132,
                start=o,
                extent=70,
                style="arc",
                width=3,
            )
            for o in (0, 120, 240)
        ]

        self.arcs_b = [
            self.create_arc(
                c - 112,
                c - 112,
                c + 112,
                c + 112,
                start=o,
                extent=140,
                style="arc",
                width=5,
            )
            for o in (0, 180)
        ]

        self.ring = self.create_oval(
            c - 92,
            c - 92,
            c + 92,
            c + 92,
            width=1,
        )

        self.bars = [
            self.create_line(
                0,
                0,
                0,
                0,
                width=3,
                capstyle="round",
            )
            for _ in range(self.BARS)
        ]

        self.glow = [
            self.create_oval(
                0,
                0,
                0,
                0,
                width=w,
            )
            for w in (2, 1)
        ]

        self.core = self.create_oval(
            c - 44,
            c - 44,
            c + 44,
            c + 44,
            width=2,
            fill=PANEL_2,
        )

        self.label = self.create_text(
            c,
            c,
            text="",
            font=(MONO, 8, "bold"),
        )

        self.after(33, self.tick)

    def recolor(self, state: str):

        color = STATES.get(
            state,
            STATES["ONLINE"],
        )[0]

        for item in self.ticks:
            self.itemconfigure(
                item,
                fill=blend(color, PANEL, 0.55),
            )

        for item in self.arcs_a:
            self.itemconfigure(
                item,
                outline=color,
            )

        for item in self.arcs_b:
            self.itemconfigure(
                item,
                outline=blend(color, PANEL, 0.6),
            )

        for item in self.bars:
            self.itemconfigure(
                item,
                fill=color,
            )

        self.itemconfigure(
            self.ring,
            outline=blend(color, PANEL, 0.3),
        )

        self.itemconfigure(
            self.glow[0],
            outline=blend(color, PANEL, 0.5),
        )

        self.itemconfigure(
            self.glow[1],
            outline=blend(color, PANEL, 0.25),
        )

        self.itemconfigure(
            self.core,
            outline=color,
        )

        self.itemconfigure(
            self.label,
            fill=color,
            text=SHORT.get(state, state),
        )

        self.shown_state = state

    def tick(self):

        state = self.app.state
        _, speed, mode = STATES.get(
            state,
            STATES["ONLINE"],
        )

        if state != self.shown_state:
            self.recolor(state)

        self.t += 0.033
        self.rot += speed

        c = self.c

        for i, item in enumerate(self.ticks):
            a = math.radians(
                i * 360 / self.TICKS + self.rot * 0.15
            )

            r1 = 148
            r0 = r1 - (10 if i % 5 == 0 else 5)

            self.coords(
                item,
                c + r0 * math.cos(a),
                c + r0 * math.sin(a),
                c + r1 * math.cos(a),
                c + r1 * math.sin(a),
            )

        for k, item in enumerate(self.arcs_a):
            self.itemconfigure(
                item,
                start=(k * 120 + self.rot * 0.8) % 360,
            )

        for k, item in enumerate(self.arcs_b):
            self.itemconfigure(
                item,
                start=(k * 180 - self.rot * 1.2) % 360,
            )

        for i, item in enumerate(self.bars):

            if mode == "voice":
                target = (
                    0.25
                    + 0.75
                    * abs(math.sin(self.t * 7 + i * 0.7))
                    * random.uniform(0.4, 1.0)
                )

            elif mode == "wave":
                target = (
                    0.2
                    + 0.6
                    * (
                        0.5
                        + 0.5
                        * math.sin(
                            self.t * 9 - i * 0.45
                        )
                    )
                )

            elif mode == "idle":
                target = (
                    0.06
                    + 0.04
                    * math.sin(
                        self.t * 2 + i * 0.3
                    )
                )

            else:
                target = 0.03

            self.amps[i] += (
                target - self.amps[i]
            ) * 0.35

            a = (
                2 * math.pi * i / self.BARS
                - math.pi / 2
            )

            r0 = 56
            r1 = 58 + self.amps[i] * 28

            self.coords(
                item,
                c + r0 * math.cos(a),
                c + r0 * math.sin(a),
                c + r1 * math.cos(a),
                c + r1 * math.sin(a),
            )

        pulse = (
            0.5
            + 0.5
            * math.sin(
                self.t * 3 * math.sqrt(speed)
            )
        )

        for item, base, swing in (
            (self.glow[0], 48, 3),
            (self.glow[1], 52, 4),
        ):
            r = base + swing * pulse

            self.coords(
                item,
                c - r,
                c - r,
                c + r,
                c + r,
            )

        self.after(33, self.tick)


class Telemetry(tk.Canvas):

    SEGS = 24
    BARS = ("CPU", "MEM")

    def __init__(self, parent, width: int = 340):
        super().__init__(
            parent,
            width=width,
            height=118,
            bg=PANEL,
            highlightthickness=0,
        )

        self.w = width
        self.segs = {}
        self.vals = {}

        y = 16

        for name in self.BARS:

            self.create_text(
                10,
                y,
                text=name,
                anchor="w",
                fill=GRAY,
                font=(MONO, 9, "bold"),
            )

            self.segs[name] = [
                self.create_rectangle(
                    52 + i * 10,
                    y - 6,
                    59 + i * 10,
                    y + 6,
                    fill=CYAN_DARK,
                    outline="",
                )
                for i in range(self.SEGS)
            ]

            self.vals[name] = self.create_text(
                width - 4,
                y,
                text="N/A",
                anchor="e",
                fill=WHITE,
                font=(MONO, 9),
            )

            y += 26

        self.latency = self._row(
            "LAST REPLY",
            y + 4,
        )

        self.uptime = self._row(
            "UPTIME",
            y + 26,
        )

    def _row(self, name, y):

        self.create_text(
            10,
            y,
            text=name,
            anchor="w",
            fill=GRAY,
            font=(MONO, 9, "bold"),
        )

        return self.create_text(
            self.w - 4,
            y,
            text="--",
            anchor="e",
            fill=WHITE,
            font=(MONO, 9),
        )

    def refresh(
        self,
        cpu,
        mem,
        latency,
        uptime,
    ):

        for name, pct in (
            ("CPU", cpu),
            ("MEM", mem),
        ):

            lit = (
                0
                if pct is None
                else round(
                    pct / 100 * self.SEGS
                )
            )

            color = (
                RED
                if (pct or 0) > 85
                else YELLOW
                if (pct or 0) > 65
                else CYAN
            )

            for i, seg in enumerate(
                self.segs[name]
            ):
                self.itemconfigure(
                    seg,
                    fill=(
                        color
                        if i < lit
                        else CYAN_DARK
                    ),
                )

            self.itemconfigure(
                self.vals[name],
                text=(
                    "N/A"
                    if pct is None
                    else f"{pct:.0f}%"
                ),
            )

        self.itemconfigure(
            self.latency,
            text=(
                "--"
                if latency is None
                else f"{latency:.1f}s"
            ),
        )

        self.itemconfigure(
            self.uptime,
            text=uptime,
        )


# ----------------------------------------------------------------- app

class JarvisGUI(tk.Tk):

    def __init__(self):

        super().__init__()

        self.title("J.A.R.V.I.S")
        self.geometry("1280x840")
        self.minsize(1000, 740)
        self.configure(bg=BG)

        self.processing = False
        self.muted = False

        # Generation number used to cancel old speech.
        self.speech_gen = 0

        self.state = "BOOTING"
        self.started = time.time()
        self.last_latency = None

        self._queue: list[
            tuple[str, str, str]
        ] = []

        self._typing = False
        self._token = 0

        self.backend_ready = False
        self.startup_error = ""

        self.logger = logging.getLogger(
            "jarvis"
        )

        self.init_backend()
        self.build_ui()

        self.bind(
            "<Escape>",
            lambda e: self.cancel_speech(),
        )

        self.update_clock()
        self.update_telemetry()
        self.boot()

    # -------------------------------------------------------- backend/boot

    def init_backend(self):

        try:

            self.settings = load_settings()

            self.logger = setup_logger(
                ROOT
            )

            self.security = SecurityManager(
                self.settings
            )

            self.registry = ToolRegistry(
                self.security,
                self.logger,
            )

            register_safe_tools(
                self.registry
            )

            self.audio = AudioService(
                self.settings
            )

            self.gemini = GeminiService(
                self.settings,
                self.logger,
            )

            self.memory = MemoryStore(
                ROOT / "data"
            )

            self.backend_ready = True

        except Exception as exc:

            self.startup_error = str(exc)

    def boot(self):

        self.set_status("BOOTING")

        if not self.backend_ready:

            self.add_message(
                "SYSTEM",
                (
                    "Backend initialization failed:\n"
                    f"{self.startup_error}"
                ),
                "error",
            )

            self.set_status("ERROR")
            return

        TTS_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        for old in TTS_DIR.glob("*.wav"):

            try:
                old.unlink()
            except OSError:
                pass

        self.after(
            1400,
            self.finish_boot,
        )

    def finish_boot(self):

        greeting = (
            f"{greeting_for_hour()}, {ADDRESS}. "
            "All systems are online and standing by."
        )

        self.set_status("ONLINE")

        self.add_message(
            "JARVIS",
            greeting,
        )

        self.entry.focus_force()

        if SPEAK_GREETING:

            generation = self.speech_gen

            threading.Thread(
                target=self.speak_response,
                args=(greeting, generation),
                daemon=True,
            ).start()

    # ------------------------------------------------------------------ UI

    def build_ui(self):

        self.build_topbar()

        main = tk.Frame(
            self,
            bg=BG,
        )

        main.pack(
            fill="both",
            expand=True,
            padx=14,
            pady=10,
        )

        left = tk.Frame(
            main,
            bg=BG,
            width=380,
        )

        left.pack(
            side="left",
            fill="y",
        )

        left.pack_propagate(False)

        core = Panel(
            left,
            "CORE",
        )

        core.pack(
            fill="x",
        )

        Reactor(
            core.content,
            self,
        ).pack(
            pady=8,
        )

        tele = Panel(
            left,
            "TELEMETRY",
        )

        tele.pack(
            fill="x",
            pady=(8, 0),
        )

        self.telemetry = Telemetry(
            tele.content
        )

        self.telemetry.pack(
            padx=8,
            pady=8,
        )

        ctrl = Panel(
            left,
            "CONTROLS",
        )

        ctrl.pack(
            fill="both",
            expand=True,
            pady=(8, 0),
        )

        self.build_controls(
            ctrl.content
        )

        right = tk.Frame(
            main,
            bg=BG,
        )

        right.pack(
            side="left",
            fill="both",
            expand=True,
            padx=(8, 0),
        )

        self.build_chat(right)
        self.build_input(right)

    def build_topbar(self):

        top = tk.Frame(
            self,
            bg=BG,
        )

        top.pack(
            fill="x",
            padx=18,
            pady=(14, 0),
        )

        left = tk.Frame(
            top,
            bg=BG,
        )

        left.pack(
            side="left",
        )

        tk.Label(
            left,
            text="J.A.R.V.I.S",
            fg=CYAN,
            bg=BG,
            font=(MONO, 24, "bold"),
        ).pack(
            anchor="w"
        )

        tk.Label(
            left,
            text="JUST A RATHER VERY INTELLIGENT SYSTEM",
            fg=GRAY,
            bg=BG,
            font=(MONO, 8),
        ).pack(
            anchor="w"
        )

        right = tk.Frame(
            top,
            bg=BG,
        )

        right.pack(
            side="right"
        )

        status = tk.Frame(
            right,
            bg=BG,
        )

        status.pack(
            anchor="e"
        )

        self.status_dot = tk.Label(
            status,
            text="●",
            fg=CYAN,
            bg=BG,
            font=(UI, 11),
        )

        self.status_dot.pack(
            side="left",
            padx=(0, 6),
        )

        self.status_label = tk.Label(
            status,
            text="BOOTING",
            fg=CYAN,
            bg=BG,
            font=(MONO, 10, "bold"),
        )

        self.status_label.pack(
            side="left"
        )

        clock = tk.Frame(
            right,
            bg=BG,
        )

        clock.pack(
            anchor="e",
            pady=(2, 0),
        )

        self.clock_label = tk.Label(
            clock,
            fg=WHITE,
            bg=BG,
            font=(MONO, 12, "bold"),
        )

        self.clock_label.pack(
            side="left"
        )

        self.date_label = tk.Label(
            clock,
            fg=GRAY,
            bg=BG,
            font=(MONO, 9),
        )

        self.date_label.pack(
            side="left",
            padx=(10, 0),
        )

        tk.Label(
            top,
            text=f"{greeting_for_hour().upper()}, {ADDRESS.upper()}",
            fg=WHITE,
            bg=BG,
            font=(MONO, 11),
        ).pack(
            side="left",
            expand=True,
        )

        tk.Frame(
            self,
            bg=CYAN_DARK,
            height=2,
        ).pack(
            fill="x",
            padx=18,
            pady=(10, 0),
        )

    def build_controls(self, parent):

        self.voice_button = button(
            parent,
            "🎙  ACTIVATE VOICE",
            self.start_voice,
            CYAN_DARK,
            CYAN,
            CYAN,
            BG,
            (UI, 12, "bold"),
            height=2,
        )

        self.voice_button.pack(
            fill="x",
            padx=14,
            pady=(14, 8),
        )

        row = tk.Frame(
            parent,
            bg=PANEL,
        )

        row.pack(
            fill="x",
            padx=14,
        )

        self.mute_button = button(
            row,
            "🔊  SOUND ON",
            self.toggle_mute,
            PANEL_2,
            WHITE,
            BORDER,
            WHITE,
            (UI, 9, "bold"),
            height=2,
        )

        self.mute_button.pack(
            side="left",
            fill="x",
            expand=True,
            padx=(0, 4),
        )

        button(
            row,
            "⌫  CLEAR",
            self.clear_chat,
            PANEL_2,
            WHITE,
            BORDER,
            WHITE,
            (UI, 9, "bold"),
            height=2,
        ).pack(
            side="left",
            fill="x",
            expand=True,
            padx=(4, 0),
        )

        tk.Label(
            parent,
            text="ESC  stop speaking",
            fg=GRAY,
            bg=PANEL,
            font=(MONO, 8),
        ).pack(
            pady=(10, 0)
        )

    def build_chat(self, parent):

        panel = Panel(
            parent,
            "COMMS LOG",
        )

        panel.pack(
            fill="both",
            expand=True,
        )

        self.chat = tk.Text(
            panel.content,
            bg=PANEL,
            fg=WHITE,
            insertbackground=CYAN,
            selectbackground=CYAN_DARK,
            selectforeground=WHITE,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            wrap="word",
            font=(UI, 11),
            padx=20,
            pady=14,
            state="disabled",
        )

        self.chat.pack(
            fill="both",
            expand=True,
        )

        for kind, color in KINDS.items():

            self.chat.tag_configure(
                f"{kind}_name",
                foreground=color,
                font=(MONO, 10, "bold"),
                spacing1=6,
            )

            msg_color = (
                "#ff8a80"
                if kind == "error"
                else WHITE
            )

            self.chat.tag_configure(
                f"{kind}_msg",
                foreground=msg_color,
                font=(UI, 11),
                lmargin1=22,
                lmargin2=22,
                spacing3=4,
            )

        self.chat.tag_configure(
            "time",
            foreground=GRAY,
            font=(MONO, 8),
        )

    def build_input(self, parent):

        chips = tk.Frame(
            parent,
            bg=BG,
        )

        chips.pack(
            fill="x",
            pady=(8, 0),
        )

        for name, command in QUICK_COMMANDS:

            button(
                chips,
                name,
                lambda c=command: self.send_message(c),
                PANEL_2,
                CYAN,
                CYAN_DARK,
                CYAN,
                (MONO, 9, "bold"),
                padx=14,
                pady=4,
                highlightthickness=1,
                highlightbackground=BORDER,
            ).pack(
                side="left",
                padx=(0, 6),
            )

        panel = Panel(parent)

        panel.pack(
            fill="x",
            pady=(4, 0),
        )

        row = tk.Frame(
            panel.content,
            bg=PANEL,
        )

        row.pack(
            fill="x",
            padx=8,
            pady=8,
        )

        tk.Label(
            row,
            text="›",
            fg=CYAN,
            bg=PANEL,
            font=(MONO, 18, "bold"),
        ).pack(
            side="left",
            padx=(8, 4),
        )

        self.entry = tk.Entry(
            row,
            bg=PANEL_2,
            fg=WHITE,
            insertbackground=CYAN,
            selectbackground=CYAN_DARK,
            selectforeground=WHITE,
            relief="flat",
            borderwidth=0,
            font=(UI, 12),
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=CYAN,
        )

        self.entry.pack(
            side="left",
            fill="x",
            expand=True,
            padx=(0, 8),
            ipady=9,
        )

        self.entry.bind(
            "<Return>",
            self.on_enter,
        )

        self.send_button = button(
            row,
            "SEND  ➤",
            self.send_message,
            CYAN_DARK,
            CYAN,
            CYAN,
            BG,
            (UI, 10, "bold"),
            padx=20,
            pady=8,
        )

        self.send_button.pack(
            side="right"
        )

    # ------------------------------------------------------------ helpers

    def ui(self, fn, *args):
        """
        Safely run a function on the Tkinter main thread.
        """

        try:
            self.after(
                0,
                fn,
                *args,
            )
        except tk.TclError:
            pass

    def update_clock(self):

        try:
            now = datetime.now()

            self.clock_label.configure(
                text=now.strftime("%H:%M:%S")
            )

            self.date_label.configure(
                text=now.strftime(
                    "%a %d %b %Y"
                ).upper()
            )

            self.after(
                1000,
                self.update_clock,
            )

        except tk.TclError:
            pass

    def update_telemetry(self):

        try:

            cpu = (
                psutil.cpu_percent(None)
                if psutil
                else None
            )

            mem = (
                psutil.virtual_memory().percent
                if psutil
                else None
            )

            up = int(
                time.time() - self.started
            )

            uptime = (
                f"{up // 3600:02d}:"
                f"{up % 3600 // 60:02d}:"
                f"{up % 60:02d}"
            )

            self.telemetry.refresh(
                cpu,
                mem,
                self.last_latency,
                uptime,
            )

            self.after(
                1000,
                self.update_telemetry,
            )

        except tk.TclError:
            pass

    def set_status(self, text):

        self.state = text

        color = STATES.get(
            text,
            STATES["ONLINE"],
        )[0]

        self.status_dot.configure(
            fg=color
        )

        self.status_label.configure(
            text=text,
            fg=color,
        )

    def set_busy(
        self,
        busy,
        status="ONLINE",
    ):

        self.processing = busy

        state = (
            "disabled"
            if busy
            else "normal"
        )

        self.voice_button.configure(
            state=state
        )

        self.send_button.configure(
            state=state
        )

        self.set_status(status)

        if not busy:
            self.entry.focus_force()

    def on_enter(self, event=None):

        self.send_message()

        return "break"

    # --------------------------------------------------------------- chat

    def add_message(
        self,
        sender,
        message,
        kind="jarvis",
    ):

        self._queue.append(
            (
                sender,
                str(message),
                (
                    kind
                    if kind in KINDS
                    else "jarvis"
                ),
            )
        )

        self._pump()

    def _write(self, text, tag):

        self.chat.configure(
            state="normal"
        )

        self.chat.insert(
            "end",
            text,
            tag,
        )

        self.chat.configure(
            state="disabled"
        )

        self.chat.see("end")

    def _pump(self):

        while (
            self._queue
            and not self._typing
        ):

            sender, message, kind = (
                self._queue.pop(0)
            )

            self._write(
                f"◆ {sender}",
                f"{kind}_name",
            )

            self._write(
                f"   {datetime.now():%H:%M:%S}\n",
                "time",
            )

            if (
                kind == "jarvis"
                and len(message) <= 600
            ):

                self._typing = True

                self._type_out(
                    message,
                    0,
                    self._token,
                )

            else:

                self._write(
                    f"{message}\n\n",
                    f"{kind}_msg",
                )

    def _type_out(
        self,
        message,
        i,
        token,
    ):

        if token != self._token:
            return

        step = 3

        self._write(
            message[i:i + step],
            "jarvis_msg",
        )

        i += step

        if i < len(message):

            self.after(
                10,
                self._type_out,
                message,
                i,
                token,
            )

        else:

            self._write(
                "\n\n",
                "jarvis_msg",
            )

            self._typing = False
            self._pump()

    def clear_chat(self):

        self._token += 1
        self._queue.clear()
        self._typing = False

        self.chat.configure(
            state="normal"
        )

        self.chat.delete(
            "1.0",
            "end",
        )

        self.chat.configure(
            state="disabled"
        )

        self.add_message(
            "JARVIS",
            (
                f"Conversation cleared, "
                f"{ADDRESS}. "
                "Systems remain online."
            ),
        )

        self.entry.focus_force()

    # ------------------------------------------------------- text requests

    def send_message(self, text=None):

        if (
            self.processing
            or not self.backend_ready
        ):
            return

        text = (
            self.entry.get()
            if text is None
            else text
        ).strip()

        if not text:
            return

        self.entry.delete(
            0,
            "end",
        )

        self.cancel_speech()

        self.add_message(
            "MR. TYAGI",
            text,
            "user",
        )

        self.set_busy(
            True,
            "THINKING",
        )

        threading.Thread(
            target=self.process_message,
            args=(text,),
            daemon=True,
        ).start()

    def process_message(self, text):

        def run_with_timeout(
            func,
            timeout,
            label,
        ):

            result = []
            errors = []

            def worker():

                try:
                    result.append(
                        func()
                    )

                except Exception as exc:
                    errors.append(exc)

            worker_thread = threading.Thread(
                target=worker,
                daemon=True,
            )

            worker_thread.start()
            worker_thread.join(timeout)

            if worker_thread.is_alive():

                self.logger.error(
                    "%s timed out after %ss",
                    label,
                    timeout,
                )

                raise TimeoutError(
                    f"{label} timed out after "
                    f"{timeout} seconds."
                )

            if errors:
                raise errors[0]

            return (
                result[0]
                if result
                else None
            )

        try:

            t0 = time.perf_counter()

            try:

                answer = run_with_timeout(
                    lambda: handle_command(
                        text,
                        self.registry,
                        self.logger,
                    ),
                    8,
                    "Local command",
                )

            except TimeoutError:

                answer = None

            local = answer is not None

            if not local:

                try:
                    memory = (
                        self.memory.get_context()
                    )
                except Exception:
                    memory = ""

                answer = run_with_timeout(
                    lambda: self.gemini.ask(
                        text,
                        (
                            f"{PERSONA}\n\n"
                            f"{memory}"
                        ).strip(),
                    ),
                    35,
                    "Gemini request",
                )

            answer = self.normalize(
                answer
            )

            if local:

                answer = (
                    f"{random.choice(ACKS)} "
                    f"{answer}"
                )

            latency = (
                time.perf_counter()
                - t0
            )

        except TimeoutError as exc:

            self.logger.exception(
                "Request timeout: %s",
                exc,
            )

            self.ui(
                self.finish_response,
                (
                    "I'm sorry, that request "
                    "took too long. Please try again."
                ),
                True,
                None,
            )

        except Exception as exc:

            self.logger.exception(
                "Command error: %s",
                exc,
            )

            self.ui(
                self.finish_response,
                f"Error: {exc}",
                True,
                None,
            )

        else:

            self.ui(
                self.finish_response,
                answer,
                False,
                latency,
            )

    @staticmethod
    def normalize(answer):

        if isinstance(answer, dict):

            answer = (
                answer.get("message")
                or answer.get("response")
                or answer.get("error")
                or str(answer)
            )

        return (
            str(answer).strip()
            or "I didn't get a response."
        )

    def finish_response(
        self,
        answer,
        error=False,
        latency=None,
    ):

        self.last_latency = latency

        if error:

            self.add_message(
                "JARVIS",
                answer,
                "error",
            )

            self.set_busy(
                False,
                "ERROR",
            )

            return

        self.add_message(
            "JARVIS",
            answer,
        )

        self.set_busy(False)

        generation = self.speech_gen

        threading.Thread(
            target=self.speak_response,
            args=(answer, generation),
            daemon=True,
        ).start()

    # -------------------------------------------------------------- speech

    def toggle_mute(self):

        self.muted = not self.muted

        self.mute_button.configure(
            text=(
                "🔇  MUTED"
                if self.muted
                else "🔊  SOUND ON"
            )
        )

        if self.muted:
            self.cancel_speech()

    def cancel_speech(self):

        # Incrementing this immediately invalidates
        # every currently-running speech generation.
        self.speech_gen += 1

        self.restore_idle_status()

    def restore_idle_status(self):

        if (
            not self.processing
            and self.state
            in (
                "SPEAKING",
                "VOICE ERROR",
            )
        ):
            self.set_status("ONLINE")

    def speak_response(
        self,
        text,
        gen,
    ):
        """
        Reliable TTS worker.

        Important changes:
        - TTS exceptions are captured instead of hanging.
        - Queue uses a sentinel safely.
        - Old/cancelled speech is ignored.
        - Every generated WAV is cleaned up.
        - GUI is never modified directly from this thread.
        """

        if self.muted:
            return

        if gen != self.speech_gen:
            return

        chunks = split_for_speech(text)

        if not chunks:
            return

        TTS_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        ready: queue.Queue = queue.Queue(
            maxsize=2
        )

        SENTINEL = object()

        def synthesise():

            try:

                for i, chunk in enumerate(
                    chunks
                ):

                    if (
                        gen
                        != self.speech_gen
                        or self.muted
                    ):
                        break

                    path = (
                        TTS_DIR
                        / f"speech_{gen}_{i}.wav"
                    )

                    self.logger.info(
                        "Generating speech chunk %s/%s",
                        i + 1,
                        len(chunks),
                    )

                    # GeminiService.speak() must raise
                    # if TTS fails.
                    self.gemini.speak(
                        chunk,
                        path,
                    )

                    if (
                        not path.exists()
                        or path.stat().st_size < 100
                    ):
                        raise RuntimeError(
                            "TTS generated an empty audio file."
                        )

                    ready.put(
                        path
                    )

            except Exception as exc:

                self.logger.exception(
                    "Speech synthesis error: %s",
                    exc,
                )

                # Make sure the playback worker
                # wakes up even when synthesis fails.
                try:
                    ready.put(
                        exc,
                        timeout=2,
                    )
                except queue.Full:
                    pass

            finally:

                try:
                    ready.put(
                        SENTINEL,
                        timeout=2,
                    )
                except queue.Full:
                    pass

        producer = threading.Thread(
            target=synthesise,
            daemon=True,
        )

        producer.start()

        started = False
        error = None

        while True:

            try:
                item = ready.get(
                    timeout=60
                )

            except queue.Empty:

                error = RuntimeError(
                    "Voice output timed out."
                )

                break

            if item is SENTINEL:
                break

            if isinstance(
                item,
                Exception,
            ):

                error = item
                continue

            path = Path(item)

            try:

                if (
                    gen
                    != self.speech_gen
                    or self.muted
                ):
                    continue

                if not started:

                    started = True

                    self.ui(
                        self.set_status,
                        "SPEAKING",
                    )

                self.logger.info(
                    "Playing speech: %s",
                    path.name,
                )

                self.audio.play(
                    path
                )

            except Exception as exc:

                self.logger.exception(
                    "Playback error: %s",
                    exc,
                )

                error = exc

            finally:

                try:
                    path.unlink()
                except OSError:
                    pass

        if (
            error is not None
            and gen == self.speech_gen
            and not self.muted
        ):

            self.logger.error(
                "Voice output failed: %s",
                error,
            )

            self.ui(
                self.set_status,
                "VOICE ERROR",
            )

        elif (
            started
            and gen == self.speech_gen
            and not self.muted
        ):

            self.ui(
                self.restore_idle_status
            )

    # --------------------------------------------------------- voice input

    def start_voice(self):

        if (
            self.processing
            or not self.backend_ready
        ):
            return

        self.cancel_speech()

        self.set_busy(
            True,
            "LISTENING",
        )

        threading.Thread(
            target=self.process_voice,
            daemon=True,
        ).start()

    def process_voice(self):

        """
        Voice pipeline:

        Microphone
            ↓
        WAV recording
            ↓
        Gemini transcription
            ↓
        Normal JARVIS processing
            ↓
        Gemini response
            ↓
        Gemini TTS
            ↓
        Speakers
        """

        temp_path = None

        try:

            self.logger.info(
                "VOICE: starting microphone recording"
            )

            temp_path = self.audio.record()

            if (
                not temp_path
                or not Path(temp_path).exists()
            ):
                raise RuntimeError(
                    "Microphone recording failed."
                )

            self.logger.info(
                "VOICE: recording saved to %s",
                temp_path,
            )

            self.ui(
                self.set_status,
                "TRANSCRIBING",
            )

            self.logger.info(
                "VOICE: sending recording to Gemini"
            )

            transcript = str(
                self.gemini.transcribe(
                    temp_path
                )
                or ""
            ).strip()

            self.logger.info(
                "VOICE: transcript = %r",
                transcript,
            )

            if not transcript:

                raise RuntimeError(
                    "I couldn't understand the audio."
                )

            # Show exactly what Gemini heard.
            self.ui(
                self.add_message,
                "MR. TYAGI",
                transcript,
                "user",
            )

            self.ui(
                self.set_status,
                "THINKING",
            )

            # IMPORTANT:
            # Do not start another process_message thread here.
            # We are already on a worker thread.
            self.process_message(
                transcript
            )

        except Exception as exc:

            self.logger.exception(
                "Voice input error: %s",
                exc,
            )

            self.ui(
                self.add_message,
                "JARVIS",
                f"Voice error: {exc}",
                "error",
            )

            self.ui(
                self.set_busy,
                False,
                "VOICE ERROR",
            )

        finally:

            if temp_path:

                try:
                    Path(
                        temp_path
                    ).unlink()

                except OSError:
                    pass


if __name__ == "__main__":
    JarvisGUI().mainloop()

