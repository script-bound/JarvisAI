from __future__ import annotations

import argparse
import logging
import queue
import random
import re
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

from dotenv import load_dotenv

from .app import handle_command
from .audio import AudioService, NoSpeechError
from .config import ROOT, load_settings
from .gemini import GeminiService
from .hud import (
    AMBER, BG, BORDER, CYAN, GRAY, GREEN, PANEL, PANEL_2, RED, WHITE,
    Beam, HudButton, ListCanvas, MiniHUD, Panel, Reactor, StatusDot, Telemetry,
    blend, fnt, icon_photo, init_fonts, state_color, style_dark_titlebar, style_scrollbar,
)
from .logger import setup_logger
from .memory import MemoryStore
from .scheduler import Scheduler
from .security import SecurityManager
from .skills import ADDRESS, Reply, Skills
from .tools.registry import ToolRegistry
from .tools.safe_tools import register_safe_tools
from .tray import Hotkeys, SingleInstance, Tray
from .voice import Speaker

try:
    import psutil
except ImportError:
    psutil = None

DATA_DIR = ROOT / "data"
TTS_DIR = DATA_DIR / "tts"

load_dotenv(ROOT / ".env")
load_dotenv(Path.home() / ".env")

SPEAK_GREETING = True

PERSONA = (
    f"You are JARVIS, a polished, dry-witted AI assistant. The user is {ADDRESS}; "
    f"address him as '{ADDRESS}' naturally, the way JARVIS addresses Tony Stark. "
    "Keep replies to one to three sentences unless he explicitly asks for detail. "
    "Never use emoji, markdown, bullet points or special symbols, because replies are read aloud."
)
ACKS = [f"Of course, {ADDRESS}.", f"Right away, {ADDRESS}.", f"Certainly, {ADDRESS}."]

CLIP_RE = re.compile(r"\b(?:clipboard|what i (?:just )?copied|what i have copied)\b", re.I)
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u200d\ufe0f\u20e3]+")

KINDS = {"user": CYAN, "jarvis": GREEN, "system": AMBER, "error": RED, "alert": AMBER, "boot": GRAY}

QUICK = [
    ("WEATHER", "what's the weather"), ("BRIEFING", "give me my briefing"),
    ("SYSTEM", "system status"), ("NOTES", "show my notes"),
    ("TIMER 5M", "set a timer for 5 minutes"), ("CLIPBOARD", "explain my clipboard"),
    ("SCREEN", "what's on my screen"), ("TEST VOICE", "test voice"),
]

BUSY_STATES = ("LISTENING", "THINKING", "TRANSCRIBING", "SPEAKING", "CONFIRM")


def clean_text(text: str) -> str:
    """No emoji, no markdown noise, in anything JARVIS shows or says."""
    text = EMOJI_RE.sub("", str(text))
    text = re.sub(r"\*\*|__|`", "", text)
    text = re.sub(r"(?m)^\s*[*\u2022]\s+", "- ", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def greeting_for_hour() -> str:
    h = datetime.now().hour
    return "Working late" if h < 5 else "Good morning" if h < 12 else "Good afternoon" if h < 18 else "Good evening"


def countdown(seconds: float) -> str:
    s = max(0, int(seconds + 0.5))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def short_clock(dt: datetime) -> str:
    stamp = dt.strftime("%I:%M %p").lstrip("0")
    return stamp if dt.date() == datetime.now().date() else f"{dt:%a} {stamp}"


class JarvisGUI(tk.Tk):
    def __init__(self, start_hidden: bool = False, start_mini: bool = False, use_tray: bool = True):
        super().__init__()
        init_fonts(self)
        style_scrollbar(self)
        self.title("J.A.R.V.I.S")
        self.geometry("1380x860")
        self.minsize(1180, 780)
        self.configure(bg=BG)
        self._icon = icon_photo()
        if self._icon is not None:
            self.iconphoto(True, self._icon)

        # ---- state  (NB: self.state is JARVIS's state string, not tk's window state)
        self.state = "BOOTING"
        self.processing = False
        self.muted = False
        self.convo_mode = False
        self.last_input_voice = False
        self.last_reply = ""
        self.last_latency = None
        self.started = time.time()
        self.main_visible = True
        self.mini: MiniHUD | None = None
        self.instance: SingleInstance | None = None
        self.backend_ready = False
        self.startup_error = ""
        self.logger = logging.getLogger("jarvis")
        self.listen_combo = "ctrl+alt+j"
        self.toggle_combo = "ctrl+alt+h"
        self.history: list[str] = []
        self.hist_pos = 0
        self._queue: list[tuple[str, str, str]] = []
        self._typing = False
        self._token = 0
        self._ui_q: queue.Queue = queue.Queue()
        self._quitting = False
        self._told_tray = False
        self._last_voice_error = ""
        self._sched_ids: list[str] = []
        self._note_idx: list[int] = []

        self.after(15, self._drain)

        self.init_backend()

        self.tray = Tray(self)
        self.tray_ok = self.tray.start() if use_tray else False
        self.hotkeys = Hotkeys()

        self.build_ui()
        self.mini = MiniHUD(self)

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind("<Map>", self._on_map)
        self.bind("<Unmap>", self._on_unmap)
        self.bind("<Escape>", lambda e: self.cancel_speech())
        self.after(150, lambda: style_dark_titlebar(self))

        self.start_hotkeys()
        self.update_clock()
        self.update_telemetry()
        self.tick_schedule()
        self.boot(start_hidden, start_mini)

    # ------------------------------------------------------ thread helpers
    def ui(self, fn, *args):
        """Queue fn for the Tk thread. Safe from any thread (workers, tray, hotkeys)."""
        self._ui_q.put((fn, args))

    def _drain(self):
        try:
            for _ in range(60):
                fn, args = self._ui_q.get_nowait()
                try:
                    fn(*args)
                except Exception:
                    self.logger.exception("UI callback failed")
        except queue.Empty:
            pass
        if not self._quitting:
            self.after(15, self._drain)

    def ui_call(self, fn, *args, timeout: float = 300):
        """Run fn on the Tk thread and wait for its result. Only call from a worker thread."""
        done, box = threading.Event(), {}

        def run():
            try:
                box["v"] = fn(*args)
            except Exception as exc:
                box["e"] = exc
            finally:
                done.set()

        self.ui(run)
        done.wait(timeout)
        if "e" in box:
            raise box["e"]
        return box.get("v")

    @property
    def animating(self) -> bool:
        return self.main_visible or (self.mini is not None and self.mini.visible)

    # ------------------------------------------------------------- backend
    def init_backend(self):
        try:
            self.settings = load_settings()
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            self.logger = setup_logger(DATA_DIR)
            self.security = SecurityManager(self.settings)
            # stock confirm() uses input() on the console, which would freeze the GUI
            self.security.confirm = self.confirm_action
            self.registry = ToolRegistry(self.security, self.logger)
            register_safe_tools(self.registry)
            self.audio = AudioService(self.settings)
            self.gemini = GeminiService(self.settings, self.logger)
            self.memory = MemoryStore(DATA_DIR)
            self.scheduler = Scheduler(DATA_DIR / "schedule.json")
            self.skills = Skills(self.settings, self.registry, self.security, self.memory,
                                 self.scheduler, DATA_DIR, self.logger)
            self.speaker = Speaker(self.gemini, self.audio, self.logger, TTS_DIR, self.settings.audio,
                                   notify=lambda m: self.ui(self.add_message, "SYSTEM", m, "system"))
            self.muted = bool(self.skills.prefs.get("muted", False))
            self.backend_ready = True
        except Exception as exc:
            self.logger.exception("Backend init failed")
            self.startup_error = str(exc)

    def confirm_action(self, description: str) -> bool:
        """Called on a worker thread by the tool registry; asks with a dialog on the Tk thread."""
        return bool(self.ui_call(self._ask_confirm, description))

    def _ask_confirm(self, description: str) -> bool:
        previous = self.state
        self.set_status("CONFIRM")
        if not self.main_visible:
            self.show_main()
        ok = messagebox.askyesno("JARVIS - confirm action", f"{description}\n\nProceed, {ADDRESS}?", parent=self)
        self.set_status(previous if previous in BUSY_STATES else "THINKING")
        return ok

    # ------------------------------------------------------------------ UI
    def build_ui(self):
        self.build_topbar()
        main = tk.Frame(self, bg=BG)
        main.pack(fill="both", expand=True, padx=12, pady=(8, 12))

        left = tk.Frame(main, bg=BG, width=338)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        core = Panel(left, "CORE", tag="ARC REACTOR")
        core.pack(fill="x")
        self.reactor = Reactor(core.content, self, size=264)
        self.reactor.pack(pady=(6, 10))
        ctrl = Panel(left, "CONTROLS", tag="HOTKEY CTRL+ALT+J")
        ctrl.pack(fill="both", expand=True, pady=(8, 0))
        self.build_controls(ctrl.content)

        right = tk.Frame(main, bg=BG, width=318)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        sched = Panel(right, "SCHEDULE", tag="DOUBLE-CLICK TO CANCEL")
        sched.pack(fill="x")
        self.schedule_view = ListCanvas(sched.content, rows=4, empty="NO ACTIVE TIMERS OR REMINDERS",
                                        on_activate=self.dismiss_schedule)
        self.schedule_view.pack(fill="x", padx=10, pady=8)
        notes = Panel(right, "NOTES", tag="DOUBLE-CLICK TO DELETE")
        notes.pack(fill="x", pady=(8, 0))
        self.notes_view = ListCanvas(notes.content, rows=5, empty="NO SAVED NOTES", on_activate=self.delete_note)
        self.notes_view.pack(fill="x", padx=10, pady=8)
        tele = Panel(right, "TELEMETRY", tag="LIVE")
        tele.pack(fill="both", expand=True, pady=(8, 0))
        self.telemetry = Telemetry(tele.content, width=268)
        self.telemetry.pack(padx=10, pady=10, anchor="w")

        center = tk.Frame(main, bg=BG)
        center.pack(side="left", fill="both", expand=True, padx=8)
        self.build_chat(center)
        self.build_input(center)

    def build_topbar(self):
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=22, pady=(14, 0))

        left = tk.Frame(top, bg=BG)
        left.pack(side="left")
        tk.Label(left, text="J.A.R.V.I.S", fg=CYAN, bg=BG, font=fnt("hud", 26, True)).pack(anchor="w")
        tk.Label(left, text="JUST A RATHER VERY INTELLIGENT SYSTEM", fg=GRAY, bg=BG, font=fnt("hud", 8)).pack(anchor="w")

        right = tk.Frame(top, bg=BG)
        right.pack(side="right")
        status = tk.Frame(right, bg=BG)
        status.pack(anchor="e")
        self.status_dot = StatusDot(status, bg=BG)
        self.status_dot.pack(side="left", padx=(0, 8))
        self.status_label = tk.Label(status, text="BOOTING", fg=CYAN, bg=BG, font=fnt("hud", 11, True))
        self.status_label.pack(side="left")
        clock = tk.Frame(right, bg=BG)
        clock.pack(anchor="e", pady=(3, 0))
        self.clock_label = tk.Label(clock, fg=WHITE, bg=BG, font=fnt("hud", 13, True))
        self.clock_label.pack(side="left")
        self.date_label = tk.Label(clock, fg=GRAY, bg=BG, font=fnt("hud", 9))
        self.date_label.pack(side="left", padx=(10, 0))

        self.greeting_label = tk.Label(top, fg=WHITE, bg=BG, font=fnt("hud", 12))
        self.greeting_label.pack(side="left", expand=True)

        Beam(self, self).pack(fill="x", padx=22, pady=(10, 0))

    def build_controls(self, parent):
        self.voice_button = HudButton(parent, "ACTIVATE VOICE", self.start_voice, "primary",
                                      font=fnt("hud", 12, True), pady=13)
        self.voice_button.pack(fill="x", padx=12, pady=(14, 8))

        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x", padx=12)
        self.mute_button = HudButton(row, "VOICE ON", self.toggle_mute, "ghost", pady=7)
        self.mute_button.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.convo_button = HudButton(row, "CONVO OFF", self.toggle_convo, "ghost", pady=7)
        self.convo_button.pack(side="left", fill="x", expand=True, padx=(4, 0))

        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x", padx=12, pady=(8, 0))
        HudButton(row, "MINI HUD", self.toggle_mini, "ghost", pady=7).pack(side="left", fill="x", expand=True, padx=(0, 4))
        HudButton(row, "BACKGROUND", self.hide_main, "ghost", pady=7).pack(side="left", fill="x", expand=True, padx=(4, 0))

        tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", padx=12, pady=(14, 10))
        tk.Label(parent, text="QUICK ACTIONS", fg=GRAY, bg=PANEL, font=fnt("hud", 8, True)).pack(anchor="w", padx=14, pady=(0, 6))

        grid = tk.Frame(parent, bg=PANEL)
        grid.pack(fill="x", padx=12)
        grid.columnconfigure((0, 1), weight=1, uniform="q")
        for i, (name, command) in enumerate(QUICK):
            HudButton(grid, name, lambda c=command: self.send_message(c), "chip", pady=6,
                      font=fnt("hud", 8, True)).grid(row=i // 2, column=i % 2, sticky="ew",
                                                     padx=(0 if i % 2 == 0 else 3, 3 if i % 2 == 0 else 0), pady=3)

        tk.Label(parent, text="ESC  stop speaking     UP/DOWN  history", fg=blend(GRAY, PANEL, 0.7),
                 bg=PANEL, font=fnt("hud", 8)).pack(side="bottom", pady=(0, 10))

    def build_chat(self, parent):
        panel = Panel(parent, "COMMS LOG", tag="SECURE CHANNEL")
        panel.pack(fill="both", expand=True)
        wrap = tk.Frame(panel.content, bg=PANEL)
        wrap.pack(fill="both", expand=True)

        self.chat = tk.Text(wrap, bg=PANEL, fg=WHITE, insertbackground=CYAN, selectbackground=blend(CYAN, PANEL, 0.3),
                            selectforeground=WHITE, relief="flat", borderwidth=0, highlightthickness=0, wrap="word",
                            font=fnt("ui", 11), padx=22, pady=14, state="disabled", cursor="arrow")
        bar = ttk.Scrollbar(wrap, orient="vertical", style="Hud.Vertical.TScrollbar", command=self.chat.yview)
        self.chat.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y", padx=(0, 3), pady=8)
        self.chat.pack(side="left", fill="both", expand=True)

        for kind, color in KINDS.items():
            self.chat.tag_configure(f"{kind}_name", foreground=color, font=fnt("hud", 9, True), spacing1=14)
            self.chat.tag_configure(f"{kind}_msg", foreground=WHITE, font=fnt("ui", 11), spacing3=2)
        self.chat.tag_configure("user_msg", foreground=blend(CYAN, WHITE, 0.3), lmargin1=150, lmargin2=150)
        self.chat.tag_configure("error_msg", foreground="#ff9aa9")
        self.chat.tag_configure("alert_msg", foreground=AMBER, font=fnt("ui", 12, True))
        self.chat.tag_configure("boot_msg", foreground=GRAY, font=fnt("mono", 9))
        self.chat.tag_configure("right", justify="right", rmargin=4)
        self.chat.tag_configure("time", foreground=GRAY, font=fnt("hud", 8))

    def build_input(self, parent):
        panel = Panel(parent)
        panel.pack(fill="x", pady=(8, 0))
        row = tk.Frame(panel.content, bg=PANEL)
        row.pack(fill="x", padx=10, pady=10)
        tk.Label(row, text=">", fg=CYAN, bg=PANEL, font=fnt("hud", 16, True)).pack(side="left", padx=(6, 6))
        self.entry = tk.Entry(row, bg=PANEL_2, fg=WHITE, insertbackground=CYAN, selectbackground=blend(CYAN, PANEL, 0.3),
                              selectforeground=WHITE, relief="flat", borderwidth=0, font=fnt("ui", 12),
                              highlightthickness=1, highlightbackground=BORDER, highlightcolor=CYAN)
        self.entry.pack(side="left", fill="x", expand=True, padx=(0, 10), ipady=10)
        self.entry.bind("<Return>", self.on_enter)
        self.entry.bind("<Up>", lambda e: self.recall(-1))
        self.entry.bind("<Down>", lambda e: self.recall(1))
        self.send_button = HudButton(row, "SEND", self.send_message, "primary", padx=22, pady=9,
                                     font=fnt("hud", 10, True))
        self.send_button.pack(side="right")

    # --------------------------------------------------------------- status
    def set_status(self, text: str):
        self.state = text
        color = state_color(text)
        self.status_dot.set_color(color, BG)
        self.status_label.configure(text=text, fg=color)
        self.tray.set_state(color, text.title())

    def set_busy(self, busy: bool, status: str = "ONLINE"):
        self.processing = busy
        self.voice_button.set_enabled(not busy)
        self.send_button.set_enabled(not busy)
        self.set_status(status)
        if not busy and self.main_visible:
            self.entry.focus_force()

    def restore_idle_status(self):
        if not self.processing and self.state in ("SPEAKING", "VOICE ERROR", "ALERT"):
            self.set_status("ONLINE")

    def update_clock(self):
        now = datetime.now()
        self.clock_label.configure(text=now.strftime("%H:%M:%S"))
        self.date_label.configure(text=now.strftime("%a %d %b %Y").upper())
        self.greeting_label.configure(text=f"{greeting_for_hour().upper()}, {ADDRESS.upper()}")
        self.after(1000, self.update_clock)

    def update_telemetry(self):
        if self.main_visible:
            cpu = psutil.cpu_percent(None) if psutil else None
            mem = psutil.virtual_memory().percent if psutil else None
            batt = psutil.sensors_battery() if psutil else None
            bars = {
                "CPU": (cpu, None if cpu is None else f"{cpu:.0f}%"),
                "MEM": (mem, None if mem is None else f"{mem:.0f}%"),
                "BAT": (batt.percent if batt else None,
                        (f"{batt.percent:.0f}%{' AC' if batt.power_plugged else ''}") if batt else "N/A"),
            }
            up = int(time.time() - self.started)
            rows = {"LAST REPLY": "--" if self.last_latency is None else f"{self.last_latency:.1f}s",
                    "UPTIME": f"{up // 3600:02d}:{up % 3600 // 60:02d}:{up % 60:02d}"}
            self.telemetry.refresh(bars, rows)
        self.after(1000, self.update_telemetry)

    # ----------------------------------------------------------------- chat
    def add_message(self, sender: str, message: str, kind: str = "jarvis"):
        kind = kind if kind in KINDS else "jarvis"
        text = clean_text(message) if kind in ("jarvis", "alert") else str(message)
        self._queue.append((sender, text, kind))
        self._pump()

    def _write(self, text: str, *tags: str):
        self.chat.configure(state="normal")
        self.chat.insert("end", text, tags)
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def _pump(self):
        while self._queue and not self._typing:
            sender, message, kind = self._queue.pop(0)
            extra = ("right",) if kind == "user" else ()
            self._write(sender, f"{kind}_name", *extra)
            self._write(f"   {datetime.now():%H:%M:%S}\n", "time", *extra)
            if kind == "jarvis" and len(message) <= 600:
                self._typing = True
                self._type_out(message, 0, self._token)
            else:
                self._write(f"{message}\n\n", f"{kind}_msg", *extra)

    def _type_out(self, message: str, i: int, token: int):
        if token != self._token:
            return
        self._write(message[i:i + 3], "jarvis_msg")
        i += 3
        if i < len(message):
            self.after(9, self._type_out, message, i, token)
        else:
            self._write("\n\n", "jarvis_msg")
            self._typing = False
            self._pump()

    def clear_chat(self):
        self._token += 1
        self._queue.clear()
        self._typing = False
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.configure(state="disabled")
        self.add_message("JARVIS", f"Conversation cleared, {ADDRESS}. Systems remain online.")

    # --------------------------------------------------------- text requests
    def on_enter(self, event=None):
        self.send_message()
        return "break"

    def recall(self, step: int):
        if not self.history:
            return "break"
        self.hist_pos = max(0, min(len(self.history), self.hist_pos + step))
        self.entry.delete(0, "end")
        if self.hist_pos < len(self.history):
            self.entry.insert(0, self.history[self.hist_pos])
        return "break"

    def send_message(self, text: str | None = None):
        if self.processing or not self.backend_ready:
            return
        text = (self.entry.get() if text is None else text).strip()
        if not text:
            return
        self.entry.delete(0, "end")
        self.history.append(text)
        self.hist_pos = len(self.history)
        self.cancel_speech()
        self.last_input_voice = False
        self.add_message("MR. TYAGI", text, "user")
        self.set_busy(True, "THINKING")
        threading.Thread(target=self.process_message, args=(text,), daemon=True).start()

    def read_clipboard(self) -> str:
        try:
            return self.clipboard_get()
        except tk.TclError:
            return ""

    def build_context(self) -> str:
        try:
            memory = self.memory.context()
        except Exception:
            memory = ""
        now = datetime.now()
        return f"{PERSONA}\nCurrent local date and time: {now:%A %d %B %Y, %H:%M}.\n\n{memory}".strip()

    def process_message(self, text: str):
        """Worker thread. Never touch widgets directly in here; use self.ui / self.ui_call."""
        try:
            t0 = time.perf_counter()
            clip = self.ui_call(self.read_clipboard) if CLIP_RE.search(text) else None

            reply = self.skills.route(text, clipboard=clip)  # 1. local skills
            local = reply is not None

            if reply is None:  # 2. the original local commands in app.py
                raw = handle_command(text, self.registry, self.logger)
                if raw is not None:
                    local = True
                    failed = isinstance(raw, dict) and raw.get("ok") is False
                    msg = self.normalize(raw)
                    sep = "\n" if "\n" in msg else " "
                    reply = Reply(text=msg if failed else f"{random.choice(ACKS)}{sep}{msg}")

            if reply is None or reply.ask:  # 3. Gemini
                local = False
                prompt = reply.ask if reply else text
                if reply and reply.image:
                    answer = self.gemini.ask_with_image(prompt, self.build_context(), reply.image)
                else:
                    answer = self.gemini.ask(prompt, self.build_context())
                reply = Reply(text=self.normalize(answer))

            latency = time.perf_counter() - t0
            if reply.text:
                self.remember(text, reply.text)
            self.ui(self.finish_response, reply.text, False, latency, reply.action, local)

        except PermissionError as exc:
            self.ui(self.finish_response, f"That is switched off in config.yaml, {ADDRESS}. ({exc})", True, None, None, False)
        except Exception as exc:
            self.logger.exception("Command error: %s", exc)
            self.ui(self.finish_response, f"Error: {exc}", True, None, None, False)

    @staticmethod
    def normalize(answer) -> str:
        if isinstance(answer, dict):
            if answer.get("items") is not None:  # list_files
                names = [i["name"] + ("/" if i.get("directory") else "") for i in answer["items"]]
                answer = "\n".join(names) or "That folder is empty."
            else:
                answer = answer.get("message") or answer.get("response") or answer.get("error") or str(answer)
        return clean_text(answer) or "I didn't get a response."

    def remember(self, user_text: str, answer: str):
        try:
            self.memory.add_turn("user", user_text)
            self.memory.add_turn("assistant", answer)
        except Exception:
            self.logger.exception("Could not save memory")

    def finish_response(self, answer: str, error: bool, latency, action, local: bool):
        self.last_latency = latency
        if error:
            self.add_message("JARVIS", answer, "error")
            self.mini.set_reply(answer)
            self.set_busy(False, "ERROR")
            return

        if answer:
            self.last_reply = answer
            self.add_message("JARVIS", answer)
            self.mini.set_reply(answer)
            if not self.main_visible and not self.mini.visible:
                self.mini.show()
            self.mini.touch()
        self.set_busy(False)

        if action:
            self.run_action(action)
        if answer and not self.muted and action not in ("stop_speech", "repeat"):
            prefer = "windows" if (local and self.speaker.local_voice == "windows") else None
            self.speak_async(answer, prefer=prefer, report=(action == "voice_test"))

    # ------------------------------------------------------------- actions
    def run_action(self, action: str):
        if action == "hide":
            self.after(1200, self.hide_main)
        elif action == "mini":
            self.after(400, self.toggle_mini)
        elif action == "show":
            self.show_main()
        elif action == "clear":
            self.clear_chat()
        elif action == "stop_speech":
            self.cancel_speech()
        elif action == "mute":
            self.set_muted(True)
        elif action == "unmute":
            self.set_muted(False)
        elif action == "repeat":
            if self.last_reply:
                self.speak_async(self.last_reply)
        elif action == "copy_last":
            self.clipboard_clear()
            self.clipboard_append(self.last_reply)
        elif action == "convo_on":
            self.set_convo(True)
        elif action == "convo_off":
            self.set_convo(False)
        elif action == "refresh":
            self.refresh_lists()
        elif action == "quit":
            self.after(300, self.quit_when_silent, 0)

    def quit_when_silent(self, waited: int):
        if self.state == "SPEAKING" and waited < 25:
            self.after(250, self.quit_when_silent, waited + 1)
        else:
            self.quit_app()

    def set_muted(self, muted: bool):
        self.muted = muted
        self.mute_button.set_text("VOICE OFF" if muted else "VOICE ON")
        self.mute_button.set_style("danger" if muted else "ghost")
        if muted:
            self.cancel_speech()
        if self.backend_ready:
            self.skills.prefs.set("muted", muted)
        if self.tray.icon:
            try:
                self.tray.icon.update_menu()
            except Exception:
                pass

    def toggle_mute(self):
        self.set_muted(not self.muted)

    def set_convo(self, on: bool):
        self.convo_mode = on
        self.convo_button.set_text("CONVO ON" if on else "CONVO OFF")
        self.convo_button.set_style("on" if on else "ghost")

    def toggle_convo(self):
        self.set_convo(not self.convo_mode)

    # -------------------------------------------------------------- speech
    def cancel_speech(self):
        if self.backend_ready:
            self.speaker.cancel()
        self.restore_idle_status()

    def speak_async(self, text: str, prefer: str | None = None, report: bool = False):
        if not self.backend_ready:
            return
        threading.Thread(target=self._speak_worker, args=(text, self.speaker.gen, prefer, report), daemon=True).start()

    def _speak_worker(self, text: str, gen: int, prefer: str | None, report: bool):
        try:
            result = self.speaker.speak(text, gen, on_start=lambda: self.ui(self.set_status, "SPEAKING"), prefer=prefer)
        except Exception as exc:
            self.logger.exception("Speech failed: %s", exc)
            result = {"spoke": False, "error": str(exc)}
        self.ui(self.after_speech, gen, result, report)

    def after_speech(self, gen: int, result: dict, report: bool):
        if gen != self.speaker.gen:
            return
        error = result.get("error")
        if error:
            self.set_status("VOICE ERROR")
            if error != self._last_voice_error:  # say it once, in the chat, so silence is never a mystery
                self.add_message("SYSTEM", f"Voice problem: {error}", "error")
            self._last_voice_error = error
        else:
            self._last_voice_error = ""
            self.restore_idle_status()
        if report:
            detail = f"Voice output: {self.speaker.describe()}."
            if self.speaker.last_error:
                detail += f" Last Gemini voice error: {self.speaker.last_error}"
            self.add_message("SYSTEM", detail, "system")
        if not error and self.convo_mode and self.last_input_voice and not self.processing:
            self.start_voice()

    # --------------------------------------------------------- voice input
    def start_voice(self):
        if self.processing or not self.backend_ready:
            return
        if not self.main_visible and not self.mini.visible:
            self.mini.show()
        self.cancel_speech()
        self.set_busy(True, "LISTENING")
        threading.Thread(target=self.process_voice, daemon=True).start()

    def process_voice(self):
        """Worker thread: record, transcribe, then run the normal pipeline."""
        temp_path = None
        try:
            temp_path = self.audio.record()
            self.ui(self.set_status, "TRANSCRIBING")
            transcript = str(self.gemini.transcribe(temp_path) or "").strip()
            if not transcript:
                raise NoSpeechError("I couldn't understand the audio.")

            self.last_input_voice = True
            self.ui(self.add_message, "MR. TYAGI", transcript, "user")
            self.ui(self.set_status, "THINKING")
            self.process_message(transcript)

        except NoSpeechError:
            self.ui(self.on_no_speech)
        except Exception as exc:
            self.logger.exception("Voice error: %s", exc)
            self.ui(self.add_message, "JARVIS", f"Voice error: {exc}", "error")
            self.ui(self.set_busy, False)
        finally:
            if temp_path:
                try:
                    Path(temp_path).unlink()
                except OSError:
                    pass

    def on_no_speech(self):
        if self.convo_mode:
            self.set_convo(False)
            self.add_message("JARVIS", f"Standing by, {ADDRESS}.")
        else:
            self.add_message("JARVIS", f"I didn't catch anything, {ADDRESS}.", "system")
        self.set_busy(False)

    # ------------------------------------------------------- timers/alerts
    def tick_schedule(self):
        if self.backend_ready:
            for item in self.scheduler.pop_due():
                self.fire(item)
            if self.main_visible:
                self.refresh_lists()
        if not self._quitting:
            self.after(1000, self.tick_schedule)

    def fire(self, item):
        late = time.time() - item.due
        if item.kind == "timer":
            label = "" if item.text == "Timer" else f"{item.text} "
            spoken = f"{ADDRESS}, your {label}timer is up."
            title, body = "Timer finished", item.text
        else:
            spoken = f"{ADDRESS}, reminder: {item.text}."
            title, body = "Reminder", item.text
        if late > 90:
            when = datetime.fromtimestamp(item.due).strftime("%I:%M %p").lstrip("0")
            spoken += f" This was due at {when}."
        self.alert(spoken, title, body)

    def alert(self, spoken: str, title: str, body: str):
        self.add_message("REMINDER", spoken, "alert")
        self.last_reply = spoken
        self.mini.set_reply(spoken)
        if not self.main_visible:
            self.mini.show()
        self.mini.touch()
        self.tray.notify(title, body)
        self.set_status("ALERT")
        self.after(5000, self.restore_idle_status)
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            self.bell()
        if not self.muted:
            self.speak_async(spoken)
        self.refresh_lists()

    def refresh_lists(self):
        if not self.backend_ready:
            return
        now = time.time()
        items = self.scheduler.pending()[:4]
        self._sched_ids = [i.id for i in items]
        rows = []
        for it in items:
            if it.kind == "timer":
                remaining = max(0.0, it.due - now)
                total = max(1.0, it.due - it.created)
                rows.append(dict(badge="TIMER", text=it.text, right=countdown(remaining),
                                 progress=1 - remaining / total, color=AMBER))
            else:
                rows.append(dict(badge="ALARM", text=it.text, right=short_clock(datetime.fromtimestamp(it.due)),
                                 progress=None, color=CYAN))
        self.schedule_view.set_rows(rows)

        notes = self.skills.notes.load()
        latest = list(range(len(notes) - 1, max(-1, len(notes) - 6), -1))
        self._note_idx = latest
        self.notes_view.set_rows([dict(badge=f"#{i + 1}", text=notes[i]["text"], right="", color=GREEN) for i in latest])

    def dismiss_schedule(self, idx: int):
        if idx < len(self._sched_ids) and messagebox.askyesno("JARVIS", "Cancel this item?", parent=self):
            self.scheduler.cancel(self._sched_ids[idx])
            self.refresh_lists()

    def delete_note(self, idx: int):
        if idx < len(self._note_idx) and messagebox.askyesno("JARVIS", "Delete this note?", parent=self):
            rows = self.skills.notes.load()
            real = self._note_idx[idx]
            if 0 <= real < len(rows):
                rows.pop(real)
                self.skills.notes.save(rows)
            self.refresh_lists()

    # ------------------------------------------------------ window handling
    def _on_map(self, event):
        if event.widget is self:
            self.main_visible = True

    def _on_unmap(self, event):
        if event.widget is self:
            self.main_visible = False

    def show_main(self):
        self.deiconify()
        self.wm_state("normal")
        self.lift()
        self.attributes("-topmost", True)
        self.after(250, lambda: self.attributes("-topmost", False))
        self.focus_force()
        if self.mini is not None and self.mini.visible:
            self.mini.hide()
        self.entry.focus_force()
        self.refresh_lists()

    def hide_main(self, announce: bool = False):
        if self.tray_ok:
            self.withdraw()
            if announce and not self._told_tray:
                self._told_tray = True
                self.tray.notify("JARVIS is still running", f"Press {self.listen_combo.upper()} or click the tray icon.")
        else:
            self.iconify()  # without a tray there would be no way back from withdraw()

    def toggle_main(self):
        if self.main_visible:
            self.hide_main()
        else:
            self.show_main()

    def toggle_mini(self):
        if self.mini.visible and self.mini.pinned:
            self.mini.hide()
            self.show_main()
        else:
            self.hide_main()
            self.mini.show(pin=True, focus=True)

    def on_close(self):
        if self.tray_ok:
            self.hide_main(announce=True)
        else:
            self.quit_app()

    def summon(self):
        """Hotkey / tray 'Listen now': pop up the mini HUD if hidden, then listen."""
        if not self.backend_ready:
            self.show_main()
            return
        if self.processing:
            return
        if not self.main_visible:
            self.mini.show()
        self.start_voice()

    def quit_app(self):
        if self._quitting:
            return
        self._quitting = True
        for step in (lambda: self.speaker.cancel(), self.tray.stop, self.hotkeys.stop,
                     lambda: self.instance and self.instance.close()):
            try:
                step()
            except Exception:
                pass
        self.destroy()

    # ------------------------------------------------------------- hotkeys
    def start_hotkeys(self):
        ui_cfg = (self.settings.ui if self.backend_ready else {}) or {}
        self.listen_combo = ui_cfg.get("hotkey_listen", "ctrl+alt+j")
        self.toggle_combo = ui_cfg.get("hotkey_toggle", "ctrl+alt+h")

        def hotkey_listen():
            self.ui(self.summon)

        def hotkey_toggle():
            self.ui(self.toggle_main)

        self.hotkeys.start({self.listen_combo: hotkey_listen, self.toggle_combo: hotkey_toggle})

    # ---------------------------------------------------------------- boot
    @staticmethod
    def _row(label: str, value: str) -> str:
        return f"{(label + ' ').ljust(17, '.')} {value}"

    def _mic_ok(self) -> bool:
        try:
            import sounddevice as sd
            return bool(sd.query_devices(kind="input"))
        except Exception:
            return False

    def boot(self, start_hidden: bool, start_mini: bool):
        self.set_status("BOOTING")
        if not self.backend_ready:
            self.add_message("SYSTEM", f"Backend initialization failed:\n{self.startup_error}", "error")
            self.set_status("ERROR")
            return

        for old in TTS_DIR.glob("*.wav"):
            try:
                old.unlink()
            except OSError:
                pass

        hk = ", ".join(k.upper() for k in self.hotkeys.active) or f"OFF ({self.hotkeys.error[:48]})"
        tray = "ACTIVE" if self.tray_ok else f"OFF ({self.tray.error[:48] or 'disabled'})"
        lines = [
            self._row("CORE", "ONLINE"),
            self._row("VOICE OUT", self.speaker.describe().upper()),
            self._row("MICROPHONE", "READY" if self._mic_ok() else "NOT FOUND"),
            self._row("TRAY", tray),
            self._row("HOTKEYS", hk),
            self._row("SCHEDULER", f"{len(self.scheduler.pending())} PENDING"),
        ]
        self.add_message("SYSTEM", "\n".join(lines), "boot")
        self.set_muted(self.muted)

        if start_mini:
            self.hide_main()
            self.mini.show(pin=True)
        elif start_hidden:
            self.hide_main()
        self.after(1400, self.finish_boot, start_hidden or start_mini)

    def finish_boot(self, quiet: bool):
        greeting = f"{greeting_for_hour()}, {ADDRESS}. All systems are online and standing by."
        self.set_status("ONLINE")
        self.add_message("JARVIS", greeting)
        self.refresh_lists()
        if self.main_visible:
            self.entry.focus_force()
        if SPEAK_GREETING and not quiet and not self.muted:
            self.speak_async(greeting)


def main(argv=None):
    parser = argparse.ArgumentParser(description="JARVIS desktop assistant")
    parser.add_argument("--minimized", action="store_true", help="start hidden in the tray")
    parser.add_argument("--mini", action="store_true", help="start in the compact always-on-top window")
    parser.add_argument("--no-tray", action="store_true", help="do not create a tray icon")
    args = parser.parse_args(argv)

    guard = SingleInstance()
    if not guard.acquire():
        print("JARVIS is already running; asked it to show itself.")
        return

    app = JarvisGUI(start_hidden=args.minimized, start_mini=args.mini, use_tray=not args.no_tray)
    app.instance = guard
    guard.listen(lambda: app.ui(app.show_main))
    app.mainloop()


if __name__ == "__main__":
    main()