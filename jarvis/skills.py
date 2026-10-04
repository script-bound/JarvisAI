"""Everything JARVIS can do without asking Gemini.

`Skills.route(text)` returns a `Reply` when a local skill handles the request
and `None` when it should fall through to the next layer (app.handle_command,
then Gemini). Local skills are instant and work offline (except weather).
"""

from __future__ import annotations

import ast
import io
import json
import operator
import os
import platform
import random
import re
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .scheduler import Scheduler, clock_text, extract_when, parse_duration, spoken_duration
from .tools.registry import Tool

try:
    import psutil
except ImportError:  # optional
    psutil = None

ADDRESS = "Mr. Tyagi"

# Capabilities that are switched on unless config.yaml says otherwise.
# `screen_capture` sends pixels to Google, so it is OFF until you enable it.
PERMISSION_DEFAULTS = {
    "create_reminder": True,
    "timers": True,
    "notes": True,
    "memory_write": True,
    "system_info": True,
    "weather": True,
    "clipboard": True,
    "screen_capture": False,
}


@dataclass
class Reply:
    text: str = ""
    ask: str | None = None       # hand this prompt to Gemini instead of answering locally
    image: bytes | None = None   # optional PNG attached to `ask`
    action: str | None = None    # something only the GUI can do (hide, mute, quit, ...)


# =============================================================== small stores
class JsonList:
    """A tiny persistent list of dicts (used for notes)."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> list[dict]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return []

    def save(self, rows: list[dict]):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)


class Prefs:
    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")


# =============================================================== calculator
_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Mod: operator.mod, ast.Pow: operator.pow,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def safe_eval(expr: str) -> float:
    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Pow) and (abs(right) > 100 or abs(left) > 1e6):
                raise ValueError("That power is too large.")
            return _OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](walk(node.operand))
        raise ValueError("Unsupported expression.")

    return walk(ast.parse(expr, mode="eval"))


def fmt_number(x: float) -> str:
    if isinstance(x, float) and x.is_integer() and abs(x) < 1e15:
        return f"{int(x):,}" if abs(x) >= 10000 else str(int(x))
    if isinstance(x, int):
        return f"{x:,}" if abs(x) >= 10000 else str(x)
    return f"{x:,.6g}" if abs(x) >= 1e6 else f"{x:.6g}"


_WORD_OPS = [
    (r"\bto the power of\b", "**"), (r"\bsquared\b", "**2"), (r"\bcubed\b", "**3"),
    (r"\bmultiplied by\b", "*"), (r"\btimes\b", "*"), (r"\bplus\b", "+"),
    (r"\bminus\b", "-"), (r"\bdivided by\b", "/"), (r"\bover\b", "/"),
    (r"\bmod(?:ulo)?\b", "%"), (r"(?<=\d)\s*[x\u00d7]\s*(?=\d)", "*"), (r"\u00f7", "/"), (r"\^", "**"),
]

# (dimension, factor to the base unit)
_UNITS = {
    "mm": ("len", 0.001), "millimeter": ("len", 0.001), "millimeters": ("len", 0.001),
    "cm": ("len", 0.01), "centimeter": ("len", 0.01), "centimeters": ("len", 0.01),
    "m": ("len", 1), "meter": ("len", 1), "meters": ("len", 1), "metre": ("len", 1), "metres": ("len", 1),
    "km": ("len", 1000), "kilometer": ("len", 1000), "kilometers": ("len", 1000),
    "kilometre": ("len", 1000), "kilometres": ("len", 1000),
    "in": ("len", 0.0254), "inch": ("len", 0.0254), "inches": ("len", 0.0254),
    "ft": ("len", 0.3048), "foot": ("len", 0.3048), "feet": ("len", 0.3048),
    "yd": ("len", 0.9144), "yard": ("len", 0.9144), "yards": ("len", 0.9144),
    "mi": ("len", 1609.344), "mile": ("len", 1609.344), "miles": ("len", 1609.344),
    "g": ("mass", 0.001), "gram": ("mass", 0.001), "grams": ("mass", 0.001),
    "kg": ("mass", 1), "kilogram": ("mass", 1), "kilograms": ("mass", 1), "kilo": ("mass", 1), "kilos": ("mass", 1),
    "lb": ("mass", 0.45359237), "lbs": ("mass", 0.45359237), "pound": ("mass", 0.45359237), "pounds": ("mass", 0.45359237),
    "oz": ("mass", 0.0283495), "ounce": ("mass", 0.0283495), "ounces": ("mass", 0.0283495),
    "ml": ("vol", 0.001), "milliliter": ("vol", 0.001), "milliliters": ("vol", 0.001),
    "l": ("vol", 1), "liter": ("vol", 1), "liters": ("vol", 1), "litre": ("vol", 1), "litres": ("vol", 1),
    "gal": ("vol", 3.785411784), "gallon": ("vol", 3.785411784), "gallons": ("vol", 3.785411784),
    "cup": ("vol", 0.236588), "cups": ("vol", 0.236588),
    "c": ("temp", "c"), "celsius": ("temp", "c"), "centigrade": ("temp", "c"),
    "f": ("temp", "f"), "fahrenheit": ("temp", "f"),
    "k": ("temp", "k"), "kelvin": ("temp", "k"),
}
_UNIT_LABEL = {"len": "", "mass": "", "vol": "", "temp": ""}


def convert_units(value: float, src: str, dst: str) -> str | None:
    a, b = _UNITS.get(src.lower().strip(" .")), _UNITS.get(dst.lower().strip(" ."))
    if not a or not b or a[0] != b[0]:
        return None
    if a[0] == "temp":
        to_c = {"c": lambda v: v, "f": lambda v: (v - 32) * 5 / 9, "k": lambda v: v - 273.15}[a[1]](value)
        out = {"c": lambda v: v, "f": lambda v: v * 9 / 5 + 32, "k": lambda v: v + 273.15}[b[1]](to_c)
    else:
        out = value * a[1] / b[1]
    return f"{fmt_number(round(out, 4))} {dst.strip(' .')}"


# ================================================================== weather
_WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "foggy",
    51: "light drizzle", 53: "drizzling", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "raining", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snowing", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "heavy showers", 85: "snow showers", 86: "snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}


def _get_json(url: str, timeout: float = 6.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "JARVIS-desktop/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_weather(city: str, cache: dict) -> str:
    if city.lower() not in cache:
        geo = _get_json("https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
            {"name": city, "count": 1, "language": "en"}))
        results = geo.get("results") or []
        if not results:
            raise LookupError(f"I couldn't find a place called {city}.")
        cache[city.lower()] = results[0]
    place = cache[city.lower()]

    data = _get_json("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
        "latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto",
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "forecast_days": 1,
    }))
    cur, day = data["current"], data["daily"]
    sky = _WMO.get(int(cur.get("weather_code", 0)), "unsettled")
    text = (
        f"In {place['name']} it is {round(cur['temperature_2m'])} degrees and {sky}, feeling like "
        f"{round(cur['apparent_temperature'])}. Today's high is {round(day['temperature_2m_max'][0])} and the "
        f"low is {round(day['temperature_2m_min'][0])}"
    )
    rain = (day.get("precipitation_probability_max") or [None])[0]
    if rain is not None:
        text += f", with a {round(rain)} percent chance of rain"
    return text + "."


# ================================================================ apps/sites
SITES = {
    "youtube": "https://www.youtube.com", "github": "https://github.com", "gmail": "https://mail.google.com",
    "google": "https://www.google.com", "maps": "https://maps.google.com", "google maps": "https://maps.google.com",
    "whatsapp": "https://web.whatsapp.com", "wikipedia": "https://www.wikipedia.org",
    "reddit": "https://www.reddit.com", "stack overflow": "https://stackoverflow.com",
    "calendar": "https://calendar.google.com", "drive": "https://drive.google.com",
    "google drive": "https://drive.google.com", "linkedin": "https://www.linkedin.com",
}

# name -> (how, target).  how: exe | start | uri
WINDOWS_APPS = {
    "calculator": ("exe", "calc.exe"), "notepad": ("exe", "notepad.exe"), "paint": ("exe", "mspaint.exe"),
    "file explorer": ("exe", "explorer.exe"), "explorer": ("exe", "explorer.exe"),
    "task manager": ("exe", "taskmgr.exe"), "snipping tool": ("exe", "snippingtool.exe"),
    "settings": ("uri", "ms-settings:"), "chrome": ("start", "chrome"), "google chrome": ("start", "chrome"),
    "edge": ("start", "msedge"), "firefox": ("start", "firefox"), "vs code": ("start", "code"),
    "vscode": ("start", "code"), "visual studio code": ("start", "code"), "spotify": ("start", "spotify:"),
}
MAC_APPS = {"calculator": "Calculator", "notepad": "TextEdit", "chrome": "Google Chrome", "spotify": "Spotify"}
LINUX_APPS = {"calculator": "gnome-calculator", "notepad": "gedit", "chrome": "google-chrome", "firefox": "firefox"}


def register_extra_tools(registry, settings):
    """Adds `launch_app`, a superset of the stock open_application tool.

    It reuses the existing `open_application` permission and confirmation rule,
    so your config.yaml keeps full control. Extra apps/sites can be added under
    `integrations.apps` and `integrations.sites` in config.yaml.
    """
    custom_apps = (settings.integrations or {}).get("apps", {}) or {}
    custom_sites = (settings.integrations or {}).get("sites", {}) or {}
    SITES.update({k.lower(): v for k, v in custom_sites.items()})
    system = platform.system()

    def launch_app(name):
        key = name.lower().strip()
        if key in {k.lower() for k in custom_apps}:
            path = next(v for k, v in custom_apps.items() if k.lower() == key)
            subprocess.Popen([path] if isinstance(path, str) else list(path), shell=False)
            return {"ok": True, "message": f"Launching {name}."}
        if system == "Windows" and key in WINDOWS_APPS:
            how, target = WINDOWS_APPS[key]
            if how == "exe":
                subprocess.Popen([target], shell=False)
            elif how == "uri":
                os.startfile(target)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["cmd", "/c", "start", "", target], shell=False)
        elif system == "Darwin" and key in MAC_APPS:
            subprocess.Popen(["open", "-a", MAC_APPS[key]], shell=False)
        elif system == "Linux" and key in LINUX_APPS:
            subprocess.Popen([LINUX_APPS[key]], shell=False)
        else:
            return {"ok": False, "message": f"{name} is not on my approved list."}
        return {"ok": True, "message": f"Opening {name}."}

    registry.register(Tool("launch_app", "Launch an allowlisted application.", launch_app, "open_application", True))


def known_apps(settings) -> set[str]:
    custom = {k.lower() for k in ((settings.integrations or {}).get("apps", {}) or {})}
    system = platform.system()
    base = {"Windows": WINDOWS_APPS, "Darwin": MAC_APPS, "Linux": LINUX_APPS}.get(system, {})
    return custom | {k.lower() for k in base}


# ==================================================================== skills
class Skills:
    HELP = (
        "Here is what I can do, {a}. Timers, alarms and reminders. Notes and memory. Weather and a daily briefing. "
        "System status. Calculations and unit conversions. Opening apps and websites, and web searches. "
        "I can read your clipboard, and with permission, look at your screen. Anything else, I will think about. "
        "Say minimise to send me to the background."
    )

    def __init__(self, settings, registry, security, memory, scheduler: Scheduler, data_dir: Path, logger):
        self.settings = settings
        self.registry = registry
        self.security = security
        self.memory = memory
        self.scheduler = scheduler
        self.logger = logger
        self.data_dir = Path(data_dir)
        self.notes = JsonList(self.data_dir / "notes.json")
        self.prefs = Prefs(self.data_dir / "prefs.json")
        self._geo_cache: dict = {}
        self.apps = known_apps(settings)
        self.started = time.time()
        register_extra_tools(registry, settings)

    # ------------------------------------------------------------ helpers
    def allowed(self, key: str) -> bool:
        return bool(self.settings.permissions.get(key, PERMISSION_DEFAULTS.get(key, False)))

    def denied(self, key: str) -> Reply:
        return Reply(f"That is switched off in config.yaml, {ADDRESS}. Set {key}: true under permissions to enable it.")

    @staticmethod
    def normalise(text: str) -> str:
        t = text.lower().strip()
        t = re.sub(r"^(?:hey |ok |okay )?jarvis[,.!]?\s*", "", t)
        t = re.sub(r"^(?:please\s+)?(?:(?:can|could|would|will) you(?: please)?\s+)?", "", t)
        t = re.sub(r"\s+please$", "", t)
        return t.strip(" ?!.,")

    def city(self) -> str | None:
        return self.prefs.get("city") or (self.settings.assistant or {}).get("city")

    # -------------------------------------------------------------- route
    def route(self, text: str, clipboard: str | None = None) -> Reply | None:
        t = self.normalise(text)
        if not t:
            return None

        # ---- window / assistant control
        if re.fullmatch(r"(?:minimi[sz]e|hide|hide yourself|go (?:to the )?background|run in the background|go quiet)(?: now)?", t):
            return Reply(f"Going to the background, {ADDRESS}. Use the hotkey or the tray icon to bring me back.", action="hide")
        if re.fullmatch(r"(?:mini|compact)(?: mode| hud| window)?|(?:switch to |show )?mini (?:mode|hud)", t):
            return Reply("Switching to the compact display.", action="mini")
        if re.fullmatch(r"(?:show|open)(?: yourself| the window| main window)?|come back|maxi[mn]i[sz]e|full screen|expand", t):
            return Reply("", action="show")
        if re.fullmatch(r"clear(?: the)?(?: chat| screen| conversation| log)?", t):
            return Reply("", action="clear")
        if re.fullmatch(r"(?:stop|quiet|stop talking|be quiet|shut up|silence|enough|that'?s enough)", t):
            return Reply("", action="stop_speech")
        if re.fullmatch(r"mute(?: yourself| voice| the voice| sound)?|turn (?:the )?(?:voice|sound) off", t):
            return Reply(f"Voice muted, {ADDRESS}.", action="mute")
        if re.fullmatch(r"unmute(?: yourself| voice| the voice| sound)?|speak again|turn (?:the )?(?:voice|sound) on", t):
            return Reply(f"Voice restored, {ADDRESS}.", action="unmute")
        if re.fullmatch(r"(?:repeat that|say that again|what did you say|repeat)", t):
            return Reply("", action="repeat")
        if re.fullmatch(r"(?:copy that|copy (?:your|the) (?:last )?(?:reply|response|answer)|copy to clipboard)", t):
            return Reply(f"Copied to your clipboard, {ADDRESS}.", action="copy_last")
        m = re.fullmatch(r"(?:turn |switch )?(?:conversation|continuous|chat) mode (on|off)|(?:start|stop) (?:conversation|continuous) mode", t)
        if m:
            on = (m.group(1) == "on") if m.group(1) else t.startswith("start")
            return Reply("Conversation mode on. I will keep listening after each reply." if on
                         else "Conversation mode off.", action="convo_on" if on else "convo_off")
        if re.fullmatch(r"(?:quit|exit|shut down|power off|goodbye|good bye|bye|bye bye|close jarvis)", t):
            return Reply(f"Goodbye, {ADDRESS}.", action="quit")

        if re.fullmatch(r"(?:test|check)(?: your| the)?(?: voice| speakers?| audio| sound)|voice (?:test|check)|say something", t):
            return Reply(f"Voice check, {ADDRESS}. If you can hear this, my speakers are working.", action="voice_test")

        # ---- small talk that doesn't need the network
        if re.fullmatch(r"(?:hello|hi|hey|hey there|are you there|you there|jarvis|wake up)", t):
            return Reply(random.choice([f"At your service, {ADDRESS}.", f"Here, {ADDRESS}. How can I help?", f"Always, {ADDRESS}."]))
        if re.fullmatch(r"(?:thanks|thank you|thanks a lot|cheers|thank you jarvis)", t):
            return Reply(random.choice([f"My pleasure, {ADDRESS}.", f"Of course, {ADDRESS}.", f"Anytime, {ADDRESS}."]))

        # ---- time / date / help
        if re.fullmatch(r"(?:what(?:'s| is) the time|what time is it|current time|tell me the time|time(?: now)?|the time)", t):
            return Reply(f"It is {datetime.now().strftime('%I:%M %p').lstrip('0')}, {ADDRESS}.")
        if re.fullmatch(r"(?:what(?:'s| is)(?: the)? (?:date|day)(?: today)?|what day is it|today'?s date|date(?: today)?|what is today)", t):
            now = datetime.now()
            return Reply(f"Today is {now:%A}, {now.day} {now:%B %Y}.")
        if re.fullmatch(r"(?:what can you do|help|help me|what are your (?:commands|capabilities)|show commands|commands)", t):
            return Reply(self.HELP.format(a=ADDRESS))

        # ---- city
        m = re.fullmatch(r"(?:my city is|i live in|i'?m in|set my (?:city|location) to|set city to)\s+(.+)", t)
        if m:
            city = m.group(1).strip().title()
            self.prefs.set("city", city)
            return Reply(f"Noted, {ADDRESS}. I will use {city} for the weather.")

        # ---- timers / alarms / reminders
        r = self._schedule_skills(t, text)
        if r:
            return r

        # ---- notes & memory
        r = self._notes_and_memory(t, text)
        if r:
            return r

        # ---- clipboard
        if re.search(r"\b(?:clipboard|what i (?:just )?copied|what i have copied)\b", t):
            if not self.allowed("clipboard"):
                return self.denied("clipboard")
            if not clipboard:
                return Reply(f"Your clipboard is empty, {ADDRESS}.")
            if re.match(r"(?:read|show|what(?:'s| is) in|what does)", t) and not re.search(r"summari|explain|translate|fix|rewrite|improve|proofread", t):
                shown = clipboard if len(clipboard) <= 600 else clipboard[:600] + "..."
                return Reply(f"Your clipboard says: {shown}")
            return Reply(ask=f"{text}\n\nThe text on the user's clipboard:\n\"\"\"\n{clipboard[:8000]}\n\"\"\"")

        # ---- screen
        if re.search(r"\bscreenshot\b", t) and re.match(r"(?:take|capture|grab|save|make)", t):
            return self._screenshot(save_only=True)
        if re.search(r"\b(?:my screen|the screen|my display|what am i (?:looking at|doing|working on))\b", t) and \
                re.match(r"(?:what|look|describe|read|analy[sz]e|explain|tell|can|summari)", t):
            return self._screenshot(save_only=False, question=text)

        # ---- system, weather, briefing
        if re.search(r"\b(?:system|computer|pc|laptop)\s+(?:status|info|health|stats|report)\b|\bhow(?:'s| is) my (?:computer|pc|laptop|system)\b|"
                     r"\bbattery\b|\bcpu\b|\bram\b|\bmemory usage\b|\bdisk (?:space|usage)\b|\bstorage\b|\bsystem status\b|^uptime$", t):
            return self._system(t)
        if re.search(r"\b(?:weather|forecast|temperature)\b|\b(?:is it|will it) (?:rain|raining|hot|cold|sunny)\b", t):
            return self._weather(t)
        if re.search(r"\b(?:briefing|brief me)\b|^good morning$|^(?:what'?s|how'?s) my day|^start my day$", t):
            return self._briefing()

        # ---- math & units
        r = self._math(t)
        if r:
            return r

        # ---- web / sites / apps
        r = self._web_and_apps(t)
        if r:
            return r

        return None

    # ---------------------------------------------------- timers & reminders
    def _schedule_skills(self, t: str, original: str) -> Reply | None:
        # cancel
        m = re.fullmatch(r"(?:cancel|stop|delete|clear|remove|dismiss)(?: all| my| the| every)*\s*(timers?|reminders?|alarms?)(?: (\d+))?", t)
        if m:
            kind = "timer" if m.group(1).startswith("timer") else "reminder"
            if not self.allowed("timers" if kind == "timer" else "create_reminder"):
                return self.denied("timers" if kind == "timer" else "create_reminder")
            if m.group(2):
                items = self.scheduler.pending(kind)
                idx = int(m.group(2)) - 1
                if 0 <= idx < len(items):
                    self.scheduler.cancel(items[idx].id)
                    return Reply(f"Cancelled, {ADDRESS}.", action="refresh")
                return Reply(f"I don't have a {kind} number {m.group(2)}.")
            n = self.scheduler.clear(kind)
            return Reply(f"Cleared {n} {kind}{'s' if n != 1 else ''}, {ADDRESS}." if n else f"There are no {kind}s to clear.", action="refresh")

        # list
        if re.fullmatch(r"(?:(?:show|list|what(?:'s| are| is)|check)(?: me)?(?: my| the| all)?\s*)?(?:timers?|reminders?|alarms?|schedule|what do i have scheduled|what(?:'s| is) coming up)"
                        r"|how (?:much time|long)(?: is)? (?:left|remaining)(?: on (?:my|the) timer)?|time (?:left|remaining)|what timers do i have", t):
            return self._list_schedule()

        # set a timer
        if re.search(r"\btimer\b", t) and not re.search(r"\b(?:cancel|stop|how|left|remaining|show|list)\b", t):
            if not self.allowed("timers"):
                return self.denied("timers")
            seconds = parse_duration(t)
            if not seconds:
                return Reply(f"For how long, {ADDRESS}? For example, set a timer for ten minutes.")
            label = ""
            m = re.search(r"\b(?:called|named|labell?ed|for the)\s+(.+)$", t)
            if m:
                label = m.group(1).strip()
            item = self.scheduler.add("timer", label or "Timer", time.time() + seconds)
            name = f" {label}" if label else ""
            return Reply(f"Your{name} timer is set for {spoken_duration(seconds)}, {ADDRESS}.", action="refresh")

        # alarm / reminder
        is_alarm = bool(re.match(r"(?:set |create )?(?:an? )?alarm\b", t))
        if is_alarm or re.match(r"(?:remind me|set (?:a )?reminder|add (?:a )?reminder|create (?:a )?reminder)", t):
            if not self.allowed("create_reminder"):
                return self.denied("create_reminder")
            body = re.sub(r"^(?:remind me|set (?:a )?reminder|add (?:a )?reminder|create (?:a )?reminder|set (?:an? )?alarm|create (?:an? )?alarm)\s*", "", t)
            task, due = extract_when(body)
            if due is None:
                return Reply(f"When shall I remind you, {ADDRESS}? For example, remind me to call mum at 5 pm.")
            task = task or ("Alarm" if is_alarm else "Reminder")
            self.scheduler.add("reminder", task[:1].upper() + task[1:], due.timestamp())
            return Reply(f"Reminder set for {clock_text(due)}, {ADDRESS}: {task}.", action="refresh")
        return None

    def _list_schedule(self) -> Reply:
        items = self.scheduler.pending()
        if not items:
            return Reply(f"You have no active timers or reminders, {ADDRESS}.")
        now = time.time()
        parts = []
        for it in items[:6]:
            if it.kind == "timer":
                label = "timer" if it.text == "Timer" else f"{it.text} timer"
                parts.append(f"{label}, {spoken_duration(it.due - now)} left")
            else:
                parts.append(f"{it.text} at {clock_text(datetime.fromtimestamp(it.due))}")
        more = f", and {len(items) - 6} more" if len(items) > 6 else ""
        return Reply(f"You have {len(items)} scheduled: " + "; ".join(parts) + more + ".")

    # ----------------------------------------------------- notes and memory
    def _notes_and_memory(self, t: str, original: str) -> Reply | None:
        m = (re.fullmatch(r"(?:take|make|add|save) (?:a )?note(?: that| to self)?[:,]?\s+(.+)", t)
             or re.fullmatch(r"note(?: down| that)?[:,]?\s+(.+)", t)
             or re.fullmatch(r"write down\s+(.+)", t))
        if m:
            if not self.allowed("notes"):
                return self.denied("notes")
            body = re.sub(r"^(?:take|make|add|save) (?:a )?note(?: that| to self)?[:,]?\s+|^note(?: down| that)?[:,]?\s+|^write down\s+", "", original.strip(), flags=re.I)
            rows = self.notes.load()
            rows.append({"text": body.strip(), "time": datetime.now().isoformat(timespec="seconds")})
            self.notes.save(rows)
            return Reply(f"Noted, {ADDRESS}.", action="refresh")

        if re.fullmatch(r"(?:(?:show|read|list|what are)(?: me)?(?: my| the| all)?\s*)?notes", t) or t in {"what did i note", "read my notes"}:
            rows = self.notes.load()
            if not rows:
                return Reply(f"You have no saved notes, {ADDRESS}.")
            body = " ".join(f"Note {i}: {r['text']}." for i, r in enumerate(rows[-5:], start=max(1, len(rows) - 4)))
            more = f" That is the latest five of {len(rows)}." if len(rows) > 5 else ""
            return Reply(body + more)

        m = re.fullmatch(r"(?:delete|remove) note (\d+)", t)
        if m:
            rows = self.notes.load()
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(rows):
                gone = rows.pop(idx)
                self.notes.save(rows)
                return Reply(f"Deleted note {m.group(1)}: {gone['text']}.", action="refresh")
            return Reply(f"I don't have a note number {m.group(1)}.")
        if re.fullmatch(r"(?:clear|delete|erase)(?: all| my)*\s*notes", t):
            n = len(self.notes.load())
            self.notes.save([])
            return Reply(f"Deleted {n} note{'s' if n != 1 else ''}, {ADDRESS}.", action="refresh")

        # long-term memory (facts that go into every Gemini prompt)
        m = re.fullmatch(r"remember (?:that )?(.+)", t)
        if m:
            if not self.allowed("memory_write"):
                return self.denied("memory_write")
            fact = re.sub(r"^remember (?:that )?", "", original.strip(), flags=re.I).strip()
            self.memory.add_fact(fact)
            return Reply(f"I will remember that, {ADDRESS}.")
        if re.fullmatch(r"what do you (?:remember|know)(?: about me)?|what have i told you|what do you remember about me", t):
            facts = self.memory._load().get("facts", [])
            if not facts:
                return Reply(f"I haven't been asked to remember anything yet, {ADDRESS}.")
            return Reply("I remember: " + "; ".join(facts[-10:]) + ".")
        m = re.fullmatch(r"forget (?:that )?(.+)", t)
        if m:
            if m.group(1) in {"everything", "all", "it all", "everything you know"}:
                data = self.memory._load()
                n = len(data["facts"])
                data["facts"] = []
                self.memory._save(data)
                return Reply(f"Forgotten all {n} facts, {ADDRESS}.")
            needle = m.group(1)
            data = self.memory._load()
            keep = [f for f in data["facts"] if needle not in f.lower()]
            n = len(data["facts"]) - len(keep)
            if n:
                data["facts"] = keep
                self.memory._save(data)
                return Reply(f"Forgotten, {ADDRESS}.")
            return Reply(f"I have nothing stored matching that, {ADDRESS}.")
        return None

    # ------------------------------------------------------- screen / system
    def _screenshot(self, save_only: bool, question: str = "") -> Reply:
        if not self.allowed("screen_capture"):
            return self.denied("screen_capture")
        try:
            from PIL import Image, ImageGrab
        except ImportError:
            return Reply(f"Screen capture needs Pillow. Run: pip install pillow")
        try:
            img = ImageGrab.grab()
        except Exception as exc:
            return Reply(f"I couldn't capture the screen: {exc}")

        if save_only:
            folder = self.data_dir / "screenshots"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"screen_{datetime.now():%Y%m%d_%H%M%S}.png"
            img.save(path)
            return Reply(f"Screenshot saved, {ADDRESS}: {path}")

        img.thumbnail((1600, 1600))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG", optimize=True)
        prompt = (f"{question}\n\nThe attached image is a screenshot of the user's screen right now. "
                  "Answer the question about it directly and briefly.")
        return Reply(ask=prompt, image=buf.getvalue())

    def _system(self, t: str) -> Reply:
        if not self.allowed("system_info"):
            return self.denied("system_info")
        if psutil is None:
            return Reply("System monitoring needs psutil. Run: pip install psutil")
        bits = []
        want_all = bool(re.search(r"status|health|stats|report|how.*(?:computer|pc|laptop|system)", t))
        if want_all or "cpu" in t:
            bits.append(f"CPU at {psutil.cpu_percent(interval=0.3):.0f} percent")
        if want_all or "ram" in t or "memory" in t:
            bits.append(f"memory at {psutil.virtual_memory().percent:.0f} percent")
        if want_all or "disk" in t or "storage" in t:
            root = Path.home().anchor or "/"
            bits.append(f"disk {psutil.disk_usage(root).percent:.0f} percent full")
        if want_all or "battery" in t:
            batt = psutil.sensors_battery()
            if batt is None:
                bits.append("no battery detected")
            else:
                state = "charging" if batt.power_plugged else "on battery"
                bits.append(f"battery at {batt.percent:.0f} percent, {state}")
        if "uptime" in t or want_all:
            up = time.time() - psutil.boot_time()
            bits.append(f"up for {spoken_duration(up)}")
        return Reply(f"{ADDRESS}, " + ", ".join(bits) + ".")

    def _weather(self, t: str) -> Reply:
        if not self.allowed("weather"):
            return self.denied("weather")
        m = re.search(r"\b(?:in|for|at)\s+([a-z][a-z .'-]+?)(?:\s+(?:today|now|tomorrow|right now))?$", t)
        city = m.group(1).strip().title() if m else self.city()
        if not city:
            return Reply(f"Which city, {ADDRESS}? Say, my city is Nairobi, and I will remember it.")
        try:
            return Reply(fetch_weather(city, self._geo_cache))
        except LookupError as exc:
            return Reply(str(exc))
        except Exception as exc:
            self.logger.warning("Weather failed: %s", exc)
            return Reply(f"I couldn't reach the weather service, {ADDRESS}.")

    def _briefing(self) -> Reply:
        now = datetime.now()
        hour = now.hour
        greet = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
        parts = [f"{greet}, {ADDRESS}. It is {now.strftime('%I:%M %p').lstrip('0')} on {now:%A}, {now.day} {now:%B}."]
        if self.city() and self.allowed("weather"):
            try:
                parts.append(fetch_weather(self.city(), self._geo_cache))
            except Exception:
                pass
        pend = self.scheduler.pending()
        if pend:
            nxt = pend[0]
            when = (f"in {spoken_duration(nxt.due - time.time())}" if nxt.kind == "timer"
                    else f"at {clock_text(datetime.fromtimestamp(nxt.due))}")
            parts.append(f"You have {len(pend)} item{'s' if len(pend) != 1 else ''} scheduled. Next is {nxt.text} {when}.")
        else:
            parts.append("Your schedule is clear.")
        n = len(self.notes.load())
        if n:
            parts.append(f"You have {n} saved note{'s' if n != 1 else ''}.")
        if psutil:
            batt = psutil.sensors_battery()
            if batt and batt.percent < 30 and not batt.power_plugged:
                parts.append(f"Battery is at {batt.percent:.0f} percent. You may want to plug in.")
        return Reply(" ".join(parts))

    # --------------------------------------------------------------- math
    def _math(self, t: str) -> Reply | None:
        m = re.fullmatch(r"(?:what is |what's |how much is |calculate |compute )?(\d+(?:\.\d+)?)\s*(?:%|percent) of (\d+(?:\.\d+)?)", t)
        if m:
            return Reply(f"{fmt_number(float(m.group(1)) * float(m.group(2)) / 100)}, {ADDRESS}.")
        m = re.fullmatch(r"(?:what is |what's |calculate |compute )?(?:the )?(?:square root|sqrt)(?: of)? (\d+(?:\.\d+)?)", t)
        if m:
            return Reply(f"{fmt_number(round(float(m.group(1)) ** 0.5, 6))}, {ADDRESS}.")

        m = (re.fullmatch(r"(?:convert )?(-?\d+(?:\.\d+)?)\s*(?:degrees? )?([a-z]+)\s+(?:to|into|in)\s+(?:degrees? )?([a-z]+)", t)
             or re.fullmatch(r"how many ([a-z]+) (?:are )?in (-?\d+(?:\.\d+)?)\s*([a-z]+)", t))
        if m:
            if t.startswith("how many"):
                dst, value, src = m.group(1), float(m.group(2)), m.group(3)
            else:
                value, src, dst = float(m.group(1)), m.group(2), m.group(3)
            out = convert_units(value, src, dst)
            if out:
                return Reply(f"{fmt_number(value)} {src} is {out}.")
            if src.lower() in _UNITS and dst.lower() in _UNITS:
                return Reply(f"I can't convert {src} to {dst}, {ADDRESS}. They measure different things.")
            return None

        expr = re.sub(r"^(?:what is|what's|how much is|calculate|compute|evaluate|work out|solve)\s+", "", t)
        for pattern, repl in _WORD_OPS:
            expr = re.sub(pattern, repl, expr)
        expr = expr.replace(",", "")
        if re.fullmatch(r"[\d\s.+\-*/()%]+", expr) and re.search(r"\d\s*[-+*/%]\s*[\d(]|\*\*", expr):
            try:
                value = safe_eval(expr.replace("%", "%"))
            except ZeroDivisionError:
                return Reply("That would be dividing by zero.")
            except Exception:
                return None
            return Reply(f"That comes to {fmt_number(value)}, {ADDRESS}.")
        return None

    # ----------------------------------------------------------- web / apps
    def _web_and_apps(self, t: str) -> Reply | None:
        m = re.fullmatch(r"(?:play|search youtube for|youtube)\s+(.+?)\s+on youtube|search youtube for\s+(.+)", t)
        if m:
            query = (m.group(1) or m.group(2)).strip()
            return self._open_url("https://www.youtube.com/results?" + urllib.parse.urlencode({"search_query": query}),
                                  f"Searching YouTube for {query}")

        m = re.fullmatch(r"(?:search(?: the web| google| online)? for|google|look up|search(?! my\b))\s+(.+)", t)
        if m:
            query = m.group(1).strip()
            return self._open_url("https://www.google.com/search?" + urllib.parse.urlencode({"q": query}),
                                  f"Searching for {query}")

        m = re.fullmatch(r"(open|launch|start|run)\s+(?:up\s+)?(?:the\s+)?(.+)", t)
        if m and not m.group(2).startswith("url "):
            verb, name = m.group(1), m.group(2).strip()
            if verb in {"start", "run"} and name not in self.apps and name not in SITES:
                return None  # "start counting", "run a test": not an app launch
            site = SITES.get(name) or SITES.get(name.replace(" website", "").replace(" site", ""))
            if site:
                return self._open_url(site, f"Opening {name}")
            if name in self.apps:
                try:
                    result = self.registry.execute("launch_app", name=name)
                except PermissionError:
                    return self.denied("open_application")
                msg = result.get("message", "") if isinstance(result, dict) else str(result)
                if isinstance(result, dict) and result.get("ok"):
                    return Reply(f"{msg.rstrip('.')}, {ADDRESS}.")
                return Reply(msg or f"I couldn't open {name}.")
            if re.fullmatch(r"[a-z0-9-]+\.[a-z.]{2,}(?:/\S*)?", name):
                return self._open_url("https://" + name, f"Opening {name}")
            return Reply(f"I don't have {name} on my approved list, {ADDRESS}. You can add it under integrations in config.yaml.")
        return None

    def _open_url(self, url: str, spoken: str) -> Reply:
        try:
            result = self.registry.execute("open_url", url=url)
        except PermissionError:
            return self.denied("open_url")
        if isinstance(result, dict) and not result.get("ok", True):
            return Reply(result.get("message", "I wasn't allowed to do that."))
        return Reply(f"{spoken}, {ADDRESS}.")
