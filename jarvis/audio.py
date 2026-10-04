from __future__ import annotations

import math
import os
import queue
import tempfile
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd


class NoSpeechError(RuntimeError):
    """Raised when the mic was open but nobody said anything."""


class Endpointer:
    """Decides when the speaker has finished talking (simple energy-based VAD).

    It measures the room's background noise for the first moment, then treats
    anything clearly louder than that as speech. Recording stops once speech has
    been heard and is followed by `silence_s` of quiet.
    """

    def __init__(
        self,
        block_s: float,
        silence_s: float = 0.8,
        max_s: float = 10.0,
        no_speech_s: float = 4.0,
        calibrate_s: float = 0.3,
        min_threshold: float = 0.012,
        margin: float = 3.0,
    ):
        self.block_s = block_s
        self.silence_s = silence_s
        self.max_s = max_s
        self.no_speech_s = no_speech_s
        self.calibrate_s = calibrate_s
        self.min_threshold = min_threshold
        self.margin = margin

        self.elapsed = 0.0
        self.quiet = 0.0
        self.speech = False
        self.threshold = min_threshold
        self._ambient: list[float] = []

    @staticmethod
    def level(block: np.ndarray) -> float:
        """RMS loudness of an int16 block, 0.0 to 1.0."""
        if block.size == 0:
            return 0.0
        x = block.astype(np.float32) / 32768.0
        return float(math.sqrt(float(np.mean(x * x))))

    def feed(self, block: np.ndarray) -> str | None:
        """Returns None to keep recording, or 'done' / 'no_speech' to stop."""
        level = self.level(block)
        self.elapsed += self.block_s

        if self.elapsed <= self.calibrate_s:
            self._ambient.append(level)
            if self.elapsed + self.block_s > self.calibrate_s:
                ambient = sum(self._ambient) / len(self._ambient)
                self.threshold = max(self.min_threshold, ambient * self.margin)
            return None

        if level > self.threshold:
            self.speech = True
            self.quiet = 0.0
        else:
            self.quiet += self.block_s

        if self.speech and self.quiet >= self.silence_s:
            return "done"
        if not self.speech and self.elapsed >= self.no_speech_s:
            return "no_speech"
        if self.elapsed >= self.max_s:
            return "done" if self.speech else "no_speech"
        return None


class AudioService:
    BLOCK_S = 0.03  # 30 ms per block

    def __init__(self, settings):
        audio = settings.audio
        self.sample_rate = int(audio.get("sample_rate", 16000))
        self.channels = int(audio.get("channels", 1))
        # `record_seconds` is now only the upper limit; recording stops on silence.
        self.max_seconds = float(audio.get("record_seconds", 10))
        self.silence_seconds = float(audio.get("silence_seconds", 0.8))
        self.no_speech_seconds = float(audio.get("no_speech_seconds", 4.0))

        self.level = 0.0  # live mic loudness (0..1), read by the GUI

    def record(self, output_path: Path | None = None) -> Path:
        """Record until the speaker stops talking, then save a WAV file."""
        try:
            if not sd.query_devices(kind="input"):
                raise RuntimeError("No microphone was detected.")

            if output_path is None:
                fd, name = tempfile.mkstemp(suffix=".wav")
                os.close(fd)
                output_path = Path(name)

            block = int(self.sample_rate * self.BLOCK_S)
            blocks: queue.Queue = queue.Queue()

            def callback(indata, frames, time_info, status):
                blocks.put(indata.copy())

            endpointer = Endpointer(
                self.BLOCK_S,
                silence_s=self.silence_seconds,
                max_s=self.max_seconds,
                no_speech_s=self.no_speech_seconds,
            )

            chunks = []
            result = None
            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                blocksize=block,
                callback=callback,
            ):
                while result is None:
                    data = blocks.get(timeout=2)
                    chunks.append(data)
                    self.level = min(1.0, Endpointer.level(data) * 8)
                    result = endpointer.feed(data)

            self.level = 0.0

            if result == "no_speech":
                raise NoSpeechError("I didn't hear anything.")

            audio = np.concatenate(chunks)
            with wave.open(str(output_path), "wb") as wf:
                wf.setnchannels(self.channels)
                wf.setsampwidth(2)
                wf.setframerate(self.sample_rate)
                wf.writeframes(audio.tobytes())

            return output_path

        except NoSpeechError:
            self.level = 0.0
            raise
        except Exception as exc:
            self.level = 0.0
            raise RuntimeError(f"Microphone error: {exc}") from exc

    @staticmethod
    def play(wav_path: Path):
        """Play a WAV file. Blocks until finished, or until stop() is called."""
        try:
            wav_path = Path(wav_path)
            if not wav_path.exists():
                raise RuntimeError(f"Audio file does not exist: {wav_path}")

            with wave.open(str(wav_path), "rb") as wf:
                sample_width = wf.getsampwidth()
                channels = wf.getnchannels()
                sample_rate = wf.getframerate()
                frames = wf.readframes(wf.getnframes())

            if not frames:
                raise RuntimeError("Audio file contains no audio.")

            if sample_width == 2:
                audio = np.frombuffer(frames, dtype=np.int16)
            elif sample_width == 4:
                audio = np.frombuffer(frames, dtype=np.int32)
            else:
                raise RuntimeError(f"Unsupported WAV sample width: {sample_width * 8}-bit")

            if channels > 1:
                audio = audio.reshape(-1, channels)

            sd.play(audio, sample_rate, blocking=True)
            sd.stop()

        except Exception as exc:
            raise RuntimeError(f"Speaker error: {exc}") from exc

    @staticmethod
    def stop():
        """Cut off whatever is currently playing."""
        try:
            sd.stop()
        except Exception:
            pass