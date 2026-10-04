from __future__ import annotations

import base64
import logging
import wave
from pathlib import Path

from google import genai


class GeminiService:
    def __init__(self, settings, logger):
        self.settings = settings
        self.logger = logger

        self.client = genai.Client()

        self.model = "gemini-3.8-flash"
        self.tts_model = "gemini-3.8-flash-tts"

        # Dedicated transcription model
        self.transcribe_model = "gemini-3.5-transcribe"

        self.timeout = 45.0
        self.tts_timeout = 15.0  # fail fast; voice.py falls back to the system voice

    def ask(self, user_text: str, memory_context: str) -> str:
        assistant_name = self.settings.assistant.get("name", "Jarvis")
        personality = self.settings.assistant.get(
            "personality",
            "calm, intelligent, helpful and concise",
        )

        system = f"""
You are {assistant_name}, a desktop AI assistant.

Personality:
{personality}

Always be helpful and concise.
Address the user as Mr. Tyagi when appropriate.

Memory:
{memory_context}
""".strip()

        prompt = f"""
{system}

User:
{user_text}
""".strip()

        try:
            self.logger.info("Sending request to Gemini...")

            interaction = self.client.interactions.create(
                model=self.model,
                input=prompt,
                generation_config={
                    "thinking_level": "low",
                },
                timeout=self.timeout,
            )

            answer = interaction.output_text

            if not answer:
                return "I didn't get a response."

            return answer.strip()

        except Exception:
            self.logger.exception("Gemini request failed.")
            return "Sorry Mr. Tyagi, I couldn't connect to the AI service."

    def ask_with_image(
        self,
        user_text: str,
        memory_context: str,
        image_bytes: bytes,
        mime_type: str = "image/png",
    ) -> str:
        """Same as ask(), but with an image attached (used for 'what's on my screen')."""
        assistant_name = self.settings.assistant.get("name", "Jarvis")
        system = (
            f"You are {assistant_name}, a desktop AI assistant. "
            "Be concise. Address the user as Mr. Tyagi when appropriate.\n\n"
            f"Memory:\n{memory_context}"
        )
        try:
            self.logger.info("Sending image request to Gemini...")

            interaction = self.client.interactions.create(
                model=self.model,
                input=[
                    {"type": "text", "text": f"{system}\n\nUser:\n{user_text}"},
                    {
                        "type": "image",
                        "data": base64.b64encode(image_bytes).decode("utf-8"),
                        "mime_type": mime_type,
                    },
                ],
                generation_config={"thinking_level": "low"},
                timeout=self.timeout,
            )

            answer = interaction.output_text
            return answer.strip() if answer else "I couldn't make anything out of that image."

        except Exception:
            self.logger.exception("Gemini image request failed.")
            return "Sorry Mr. Tyagi, I couldn't analyse that image."

    def transcribe(self, audio_path: Path) -> str:
        try:
            self.logger.info("Sending audio to transcription model...")

            # Send the audio inline instead of uploading it to the Files API first.
            # That removes a whole network round trip; short voice clips are tiny.
            audio_b64 = base64.b64encode(Path(audio_path).read_bytes()).decode("utf-8")

            interaction = self.client.interactions.create(
                model=self.transcribe_model,
                input=[
                    {
                        "type": "audio",
                        "data": audio_b64,
                        "mime_type": "audio/wav",
                    }
                ],
                timeout=self.timeout,
            )

            answer = interaction.output_text

            if not answer:
                self.logger.warning("Transcription returned no text.")
                return ""

            self.logger.info("Transcription successful.")

            return answer.strip()

        except Exception:
            self.logger.exception("Audio transcription failed.")
            return ""

    def speak(self, text: str, output_path: Path):
        try:
            self.logger.info("Generating speech...")

            speech_style = (
                "Deep, calm, confident male AI assistant voice. "
                "Warm, natural, clear and professional. "
                "Speak at a normal conversational speed."
            )

            interaction = self.client.interactions.create(
                model=self.tts_model,
                input=[
                    {
                        "type": "user_input",
                        "content": [
                            {
                                "type": "text",
                                "text": text,
                                "annotations": [
                                    {
                                        "type": "speech_metadata",
                                        "style": speech_style,
                                    }
                                ],
                            }
                        ],
                    }
                ],
                response_format={
                    "type": "audio"
                },
                generation_config={
                    "speech_config": [
                        {
                            "voice": "Kore"
                        }
                    ]
                },
                timeout=self.tts_timeout,
            )

            if not interaction.output_audio:
                raise RuntimeError("Gemini returned no audio.")

            audio_data = interaction.output_audio.data

            if isinstance(audio_data, str):
                audio_data = base64.b64decode(audio_data)

            if not audio_data:
                raise RuntimeError("Gemini returned empty audio data.")

            output_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            # Gemini may return either a complete WAV file
            # or raw PCM audio. Handle both safely.
            if (
                audio_data.startswith(b"RIFF")
                and b"WAVE" in audio_data[:20]
            ):
                output_path.write_bytes(audio_data)

            else:
                # Raw Gemini PCM:
                # 24,000 Hz / 16-bit / mono
                with wave.open(str(output_path), "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(24000)
                    wf.writeframes(audio_data)

            self.logger.info(
                "Speech saved to %s",
                output_path,
            )

        except Exception:
            self.logger.exception("Text-to-speech failed.")
            raise