from __future__ import annotations

from pathlib import Path
import os
import tempfile
import wave

import numpy as np
import sounddevice as sd


class AudioService:
    def __init__(self, settings):
        self.sample_rate = int(
            settings.audio.get("sample_rate", 16000)
        )

        self.channels = int(
            settings.audio.get("channels", 1)
        )

        self.seconds = int(
            settings.audio.get("record_seconds", 5)
        )

    def record(
        self,
        output_path: Path | None = None,
    ) -> Path:

        try:
            # Check that Windows has an input device.
            input_device = sd.query_devices(
                kind="input"
            )

            if not input_device:
                raise RuntimeError(
                    "No microphone was detected."
                )

            if output_path is None:
                fd, name = tempfile.mkstemp(
                    suffix=".wav"
                )
                os.close(fd)
                output_path = Path(name)

            frames = int(
                self.seconds * self.sample_rate
            )

            print(
                f"Listening for {self.seconds} seconds..."
            )

            audio = sd.rec(
                frames,
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                blocking=True,
            )

            if audio is None:
                raise RuntimeError(
                    "Microphone returned no audio."
                )

            with wave.open(
                str(output_path),
                "wb",
            ) as wf:

                wf.setnchannels(
                    self.channels
                )

                wf.setsampwidth(2)

                wf.setframerate(
                    self.sample_rate
                )

                wf.writeframes(
                    audio.tobytes()
                )

            print(
                f"Recording saved to: {output_path}"
            )

            return output_path

        except Exception as exc:
            raise RuntimeError(
                f"Microphone error: {exc}"
            ) from exc

    @staticmethod
    def play(wav_path: Path):

        try:
            if not wav_path.exists():
                raise RuntimeError(
                    f"Audio file does not exist: {wav_path}"
                )

            with wave.open(
                str(wav_path),
                "rb",
            ) as wf:

                sample_width = wf.getsampwidth()
                channels = wf.getnchannels()
                sample_rate = wf.getframerate()
                frames = wf.readframes(
                    wf.getnframes()
                )

            if not frames:
                raise RuntimeError(
                    "Audio file contains no audio."
                )

            if sample_width == 2:
                audio = np.frombuffer(
                    frames,
                    dtype=np.int16,
                )

            elif sample_width == 4:
                audio = np.frombuffer(
                    frames,
                    dtype=np.int32,
                )

            else:
                raise RuntimeError(
                    f"Unsupported WAV sample width: "
                    f"{sample_width * 8}-bit"
                )

            if channels > 1:
                audio = audio.reshape(
                    -1,
                    channels,
                )

            sd.play(
                audio,
                sample_rate,
                blocking=True,
            )

            sd.stop()

        except Exception as exc:
            raise RuntimeError(
                f"Speaker error: {exc}"
            ) from exc
```
