"""Timers, alarms and reminders that actually fire.

Items are stored in a small JSON file so they survive a restart. The GUI polls
`pop_due()` once a second and announces whatever has come due.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

_WORD_NUMS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
    "forty five": 45, "fifty": 50, "sixty": 60,
}
_NUM = r"(?:\d+(?:\.\d+)?|" + "|".join(sorted(map(re.escape, _WORD_NUMS), key=len, reverse=True)) + r")"
_UNIT = r"(?:hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)"
_DUR_PART = rf"{_NUM}\s*{_UNIT}\b"
_DURATION = rf"(?:half\s+(?:an?\s+)?hour|{_DUR_PART}(?:\s*(?:,|and)?\s*{_DUR_PART})*(?:\s+and\s+a\s+half)?)"

_CLOCK = (
    r"(?:noon|midnight|"
    r"\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)|"
    r"\d{1,2}:\d{2}|"
    r"(?<=at\s)\d{1,2})"
)


def _to_number(token: str) -> float:
    token = token.strip().lower()
    return float(_WORD_NUMS[token]) if token in _WORD_NUMS else float(token)


def parse_duration(text: str) -> float | None:
    """'10 minutes', 'an hour and a half', '2h 15m' -> seconds."""
    t = text.lower()
    if re.search(r"\bhalf\s+(?:an?\s+)?hour\b", t):
        return 1800.0
    total = 0.0
    last_unit = ""
    for num, unit in re.findall(rf"({_NUM})\s*({_UNIT})\b", t):
        n = _to_number(num)
        u = unit[0]
        total += n * {"h": 3600, "m": 60, "s": 1}[u]
        last_unit = u
    if last_unit == "h" and re.search(r"\band\s+a\s+half\b", t):
        total += 1800
    return total or None


def _parse_clock(token: str, now: datetime) -> tuple[int, int] | None:
    token = token.strip().lower().replace(".", "")
    if token == "noon":
        return 12, 0
    if token == "midnight":
        return 0, 0
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", token)
    if not m:
        return None
    hour, minute, meridiem = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if minute > 59 or hour > 24:
        return None
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    return hour % 24, minute


def extract_when(text: str, now: datetime | None = None) -> tuple[str, datetime | None]:
    """Find a time expression inside `text`.

    Returns (text with the time expression removed, due datetime or None).
    """
    now = now or datetime.now()
    t = text.strip()

    # 1. relative: "in 20 minutes"
    m = re.search(rf"\b(?:in|after|for)\s+({_DURATION})", t, re.I)
    if m:
        seconds = parse_duration(m.group(1))
        if seconds:
            rest = (t[: m.start()] + " " + t[m.end():]).strip()
            return _tidy(rest), now + timedelta(seconds=seconds)

    # 2. absolute: "at 5pm", "tomorrow at 9", "17:30", "tonight"
    day = re.search(r"\b(tomorrow|tonight|today)\b", t, re.I)
    clock = re.search(rf"(?:\bat\s+)?\b({_CLOCK})", t, re.I)
    if clock or day:
        hm = _parse_clock(clock.group(1), now) if clock else None
        base = now.replace(second=0, microsecond=0)
        word = day.group(1).lower() if day else ""

        if hm is None:
            hm = (20, 0) if word == "tonight" else (9, 0)
            if word == "today":
                return _tidy(_cut(t, day, None)), None  # "today" alone is not a time

        due = base.replace(hour=hm[0], minute=hm[1])
        if word == "tomorrow":
            due += timedelta(days=1)
        else:
            # bare "at 5" with no am/pm: pick the next 5 o'clock, am or pm
            if clock and not re.search(r"[ap]\.?m|:|noon|midnight", clock.group(1), re.I) and hm[0] <= 12:
                options = [due, due + timedelta(hours=12)]
                due = next((d for d in options if d > now), options[-1] + timedelta(days=1))
            if due <= now:
                due += timedelta(days=1)
        return _tidy(_cut(t, day, clock)), due

    return _tidy(t), None


def _cut(text: str, *matches) -> str:
    spans = sorted((m.span() for m in matches if m), reverse=True)
    for start, end in spans:
        text = text[:start] + " " + text[end:]
    return text


def _tidy(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip(" ,.-")
    text = re.sub(r"^(?:to|that|about|for)(?:\s+|$)", "", text, flags=re.I)
    text = re.sub(r"\s+(?:at|in|on|by)$", "", text, flags=re.I)
    return text.strip()


def spoken_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    parts = []
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if secs and not hours:
        parts.append(f"{secs} second{'s' if secs != 1 else ''}")
    return " ".join(parts)


def clock_text(dt: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now()
    stamp = dt.strftime("%I:%M %p").lstrip("0")
    if dt.date() == now.date():
        return stamp
    if dt.date() == (now + timedelta(days=1)).date():
        return f"tomorrow at {stamp}"
    return f"{dt:%A} at {stamp}"


@dataclass
class Item:
    id: str
    kind: str  # "timer" | "reminder"
    text: str
    due: float
    created: float
    done: bool = False


class Scheduler:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._items: list[Item] = []
        self._load()

    # ---------------------------------------------------------- storage
    def _load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._items = [Item(**x) for x in raw if not x.get("done")]
        except (FileNotFoundError, ValueError, TypeError):
            self._items = []

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(i) for i in self._items], indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # -------------------------------------------------------------- API
    def add(self, kind: str, text: str, due_epoch: float) -> Item:
        with self._lock:
            item = Item(uuid.uuid4().hex[:8], kind, text, due_epoch, time.time())
            self._items.append(item)
            self._save()
            return item

    def pending(self, kind: str | None = None) -> list[Item]:
        with self._lock:
            items = [i for i in self._items if not i.done and (kind is None or i.kind == kind)]
        return sorted(items, key=lambda i: i.due)

    def pop_due(self, now: float | None = None) -> list[Item]:
        now = now or time.time()
        with self._lock:
            due = [i for i in self._items if not i.done and i.due <= now]
            if due:
                for i in due:
                    i.done = True
                self._items = [i for i in self._items if not i.done]
                self._save()
        return sorted(due, key=lambda i: i.due)

    def cancel(self, item_id: str) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [i for i in self._items if i.id != item_id]
            changed = len(self._items) != before
            if changed:
                self._save()
            return changed

    def clear(self, kind: str | None = None) -> int:
        with self._lock:
            before = len(self._items)
            self._items = [i for i in self._items if kind is not None and i.kind != kind]
            if before != len(self._items):
                self._save()
            return before - len(self._items)
