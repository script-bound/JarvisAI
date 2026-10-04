"""Speech output that keeps working when Gemini TTS doesn't.

Order of play:
  1. Gemini TTS (the nice voice), in short chunks so audio starts early.
  2. If a Gemini call fails, times out or is rate limited, that chunk is spoken
     by the operating system's own voice instead, and Gemini is skipped for a
     short cool-down so replies stay fast. You are told why, once, in the chat.

The system voice is Windows SAPI (built in, offline, no install), `say` on
macOS, or espeak on Linux.

Config (config.yaml, all optional):
  audio:
    tts_engine: auto        # auto | gemini | windows   ("windows" = system voice only)
    local_voice: gemini     # voice for instant local replies: gemini | windows
"""

from __future__ import annotations

import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

MAX_SPOKEN_CHARS = 700  # long answers are shown in full but only the start is read aloud


def split_for_speech(text: str, first_max: int = 140, max_chunks: int = 2) -> list[str]:
    """Split text for TTS. First chunk is short so audio starts sooner.

    Capped at `max_chunks` because every chunk is one Gemini TTS request, and TTS
    models have tight rate limits.
    """
    text = re.sub(r"[*_`#>]+", "", text)
    text = " ".join(text.split())
    if len(text) > MAX_SPOKEN_CHARS:
        cut = text[:MAX_SPOKEN_CHARS]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        text = cut[: end + 1] if end > 80 else cut

    sentences = re.split(r"(?<=[.!?])\s+", text)
    first, rest = "", []
    for i, sentence in enumerate(sentences):
        if not first or len(first) + len(sentence) + 1 <= first_max:
            first = f"{first} {sentence}".strip()
        else:
            rest = sentences[i:]
            break
    chunks = [first] if first else []
    if rest:
        chunks.append(" ".join(rest))
    return chunks[:max_chunks]


def system_voice_command(text: str, platform: str | None = None):
    """(argv, stdin_bytes, creationflags) for the OS speech command, or None if unavailable."""
    platform = platform or sys.platform
    if platform == "win32":
        script = (
            "[Console]::InputEncoding = [System.Text.Encoding]::UTF8; "
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "try { $v = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Gender -eq 'Male' } "
            "| Select-Object -First 1; if ($v) { $s.SelectVoice($v.VoiceInfo.Name) } } catch {} "
            "$s.Speak([Console]::In.ReadToEnd())"
        )
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script], text.encode("utf-8"), flags
    if platform == "darwin" and shutil.which("say"):
        return ["say", text], None, 0
    for exe in ("espeak-ng", "espeak"):
        if shutil.which(exe):
            return [exe, text], None, 0
    if shutil.which("spd-say"):
        return ["spd-say", "--wait", text], None, 0
    return None


class Speaker:
    def __init__(self, gemini, audio, logger, tts_dir: Path, cfg: dict | None = None, notify=None):
        cfg = cfg or {}
        self.gemini = gemini
        self.audio = audio
        self.logger = logger
        self.tts_dir = Path(tts_dir)
        self.tts_dir.mkdir(parents=True, exist_ok=True)
        self.engine = str(cfg.get("tts_engine", "auto")).lower()
        self.local_voice = str(cfg.get("local_voice", "gemini")).lower()
        self.notify = notify  # callable(str), may be called from any thread

        self.gen = 0  # bump to cancel whatever is speaking or queued
        self.last_error = ""
        self._lock = threading.Lock()  # one voice at a time
        self._proc = None
        self._proc_lock = threading.Lock()
        self._cooldown_until = 0.0
        self._told_fallback = False
        self.fallback_ok = system_voice_command("test") is not None

    # ---------------------------------------------------------- control
    def cancel(self):
        self.gen += 1
        try:
            self.audio.stop()
        except Exception:
            pass
        self._kill_proc()

    def _kill_proc(self):
        with self._proc_lock:
            proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    def describe(self) -> str:
        if self.engine == "windows":
            return "system voice"
        if self.engine == "gemini":
            return "Gemini voice"
        if time.time() < self._cooldown_until:
            return "system voice (Gemini cooling down)"
        return "Gemini voice" + (" with system fallback" if self.fallback_ok else "")

    # ------------------------------------------------------------ gemini
    def _gemini_blocked(self) -> bool:
        return self.engine == "auto" and time.time() < self._cooldown_until

    def _gemini_failed(self, exc: Exception):
        msg = str(exc) or type(exc).__name__
        self.last_error = msg[:300]
        self.logger.warning("Gemini TTS failed: %s", msg[:300])
        rate_limited = bool(re.search(r"429|RESOURCE_EXHAUSTED|rate.?limit|quota", msg, re.I))
        if self.engine == "auto" and self.fallback_ok:
            self._cooldown_until = time.time() + (90 if rate_limited else 45)
            if not self._told_fallback and self.notify:
                self._told_fallback = True
                why = "rate limited" if rate_limited else "unavailable"
                self.notify(f"Gemini voice is {why}. Using the system voice for now; I'll retry shortly.")
        elif self.notify and not self._told_fallback:
            self._told_fallback = True
            self.notify(f"Voice failed: {msg[:160]}")

    # ------------------------------------------------------ system voice
    def _say_system(self, text: str):
        spec = system_voice_command(text)
        if spec is None:
            raise RuntimeError("No system voice is available on this machine.")
        argv, stdin, flags = spec
        kwargs = {"stdin": subprocess.PIPE if stdin is not None else None,
                  "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if flags:
            kwargs["creationflags"] = flags
        proc = subprocess.Popen(argv, **kwargs)
        with self._proc_lock:
            self._proc = proc
        try:
            proc.communicate(input=stdin, timeout=180)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise RuntimeError("System voice timed out.")
        finally:
            with self._proc_lock:
                if self._proc is proc:
                    self._proc = None
        if proc.returncode not in (0, None) and proc.returncode > 0:
            raise RuntimeError(f"System voice exited with code {proc.returncode}.")

    # -------------------------------------------------------------- speak
    def speak(self, text: str, gen: int | None = None, on_start=None, prefer: str | None = None) -> dict:
        """Blocking. Run it on a worker thread.

        Returns {"spoke": bool, "error": str | None}.
        """
        gen = self.gen if gen is None else gen
        engine = (prefer or self.engine).lower()
        chunks = split_for_speech(text)
        if not chunks:
            return {"spoke": False, "error": None}

        with self._lock:
            if gen != self.gen:
                return {"spoke": False, "error": None}

            started, error = False, None

            def begin():
                nonlocal started
                if not started:
                    started = True
                    if on_start:
                        on_start()

            if engine == "windows":
                begin()
                try:
                    self._say_system(" ".join(chunks))
                except Exception as exc:
                    error = str(exc)
                return {"spoke": started and error is None, "error": error}

            ready: queue.Queue = queue.Queue(maxsize=2)

            def synth():
                try:
                    for i, chunk in enumerate(chunks):
                        if gen != self.gen:
                            break
                        if self._gemini_blocked():
                            ready.put(("sys", chunk))
                            continue
                        path = self.tts_dir / f"{gen}_{i}.wav"
                        try:
                            self.gemini.speak(chunk, path)
                            ready.put(("wav", path))
                        except Exception as exc:
                            self._gemini_failed(exc)
                            if self.engine == "auto" and self.fallback_ok:
                                ready.put(("sys", chunk))
                            else:
                                ready.put(("err", exc))
                finally:
                    ready.put(None)

            threading.Thread(target=synth, daemon=True).start()

            while True:
                item = ready.get()
                if item is None:
                    break
                kind, payload = item
                if kind == "err":
                    error = str(payload)
                    continue
                if gen != self.gen:
                    if kind == "wav":
                        self._discard(payload)
                    continue
                begin()
                try:
                    if kind == "wav":
                        self.audio.play(payload)
                    else:
                        self._say_system(payload)
                except Exception as exc:
                    self.logger.exception("Playback error: %s", exc)
                    error = str(exc)
                if kind == "wav":
                    self._discard(payload)

            return {"spoke": started and error is None, "error": error}

    @staticmethod
    def _discard(path):
        try:
            Path(path).unlink()
        except OSError:
            pass
