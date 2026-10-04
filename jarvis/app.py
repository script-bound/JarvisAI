from __future__ import annotations

from pathlib import Path
import os
import re

from dotenv import load_dotenv

from .config import load_settings, ROOT
from .logger import setup_logger
from .security import SecurityManager
from .memory import MemoryStore
from .gemini import GeminiService
from .audio import AudioService
from .tools.registry import ToolRegistry
from .tools.safe_tools import register_safe_tools


def check_environment():
    if not os.getenv("GEMINI_API_KEY"):
        raise RuntimeError(
            "GEMINI_API_KEY is missing. Copy .env.example to .env and add your key."
        )


def handle_command(text, registry, logger):
    """
    Handle commands that can be completed locally.

    Local commands are much faster because they don't need Gemini.
    Anything that isn't recognized here is sent to Gemini.
    """

    lower = text.lower().strip()

    # ==========================================
    # FAST MATH
    # ==========================================

    # Multiplication
    match = re.fullmatch(
        r"(?:what is\s+)?(\d+(?:\.\d+)?)\s*(?:times|x|\*)\s*(\d+(?:\.\d+)?)\??",
        lower,
    )

    if match:
        a = float(match.group(1))
        b = float(match.group(2))
        result = a * b

        if result.is_integer():
            result = int(result)

        return {
            "ok": True,
            "message": str(result),
        }

    # Addition
    match = re.fullmatch(
        r"(?:what is\s+)?(\d+(?:\.\d+)?)\s*(?:plus|\+)\s*(\d+(?:\.\d+)?)\??",
        lower,
    )

    if match:
        a = float(match.group(1))
        b = float(match.group(2))
        result = a + b

        if result.is_integer():
            result = int(result)

        return {
            "ok": True,
            "message": str(result),
        }

    # Subtraction
    match = re.fullmatch(
        r"(?:what is\s+)?(\d+(?:\.\d+)?)\s*(?:minus|-)\s*(\d+(?:\.\d+)?)\??",
        lower,
    )

    if match:
        a = float(match.group(1))
        b = float(match.group(2))
        result = a - b

        if result.is_integer():
            result = int(result)

        return {
            "ok": True,
            "message": str(result),
        }

    # Division
    match = re.fullmatch(
        r"(?:what is\s+)?(\d+(?:\.\d+)?)\s*(?:divided by|/)\s*(\d+(?:\.\d+)?)\??",
        lower,
    )

    if match:
        a = float(match.group(1))
        b = float(match.group(2))

        if b == 0:
            return {
                "ok": False,
                "message": "You can't divide by zero.",
            }

        result = a / b

        if result.is_integer():
            result = int(result)

        return {
            "ok": True,
            "message": str(result),
        }

    # ==========================================
    # APPLICATION COMMANDS
    # ==========================================

    if lower.startswith("open calculator"):
        return registry.execute(
            "open_application",
            name="calculator",
        )

    if lower.startswith("open notepad"):
        return registry.execute(
            "open_application",
            name="notepad",
        )

    # ==========================================
    # FILE COMMANDS
    # ==========================================

    if lower.startswith("list files"):
        remainder = text[len("list files"):].strip() or "."

        return registry.execute(
            "list_files",
            path=remainder,
        )

    # ==========================================
    # URL COMMAND
    # ==========================================

    if lower.startswith("open url "):
        url = text[len("open url "):].strip()

        return registry.execute(
            "open_url",
            url=url,
        )

    # ==========================================
    # REMINDER COMMAND
    # ==========================================

    if lower.startswith("remind me "):
        marker = " at "

        if marker not in lower:
            return {
                "ok": False,
                "message": "Use: remind me <text> at <time>.",
            }

        idx = lower.rfind(marker)

        reminder_text = text[len("remind me "):idx].strip()
        when = text[idx + len(marker):].strip()

        return registry.execute(
            "create_reminder",
            text=reminder_text,
            when=when,
        )

    # ==========================================
    # NOTHING MATCHED
    # ==========================================

    # Send unknown requests to Gemini.
    return None


def main():
    # ==========================================
    # LOAD ENVIRONMENT
    # ==========================================

    load_dotenv(ROOT / ".env")
    check_environment()

    # ==========================================
    # INITIALIZE SERVICES
    # ==========================================

    settings = load_settings()

    logger = setup_logger(
        ROOT / "data"
    )

    security = SecurityManager(
        settings
    )

    memory = MemoryStore(
        ROOT / "data"
    )

    gemini = GeminiService(
        settings,
        logger
    )

    audio = AudioService(
        settings
    )

    # ==========================================
    # TOOL REGISTRY
    # ==========================================

    registry = ToolRegistry(
        security,
        logger
    )

    register_safe_tools(
        registry
    )

    # ==========================================
    # STARTUP
    # ==========================================

    assistant_name = settings.assistant.get(
        "name",
        "Jarvis"
    )

    wake_phrase = settings.assistant.get(
        "wake_phrase",
        "jarvis"
    )

    print()
    print("=" * 50)
    print(f"{assistant_name} ready.")
    print("=" * 50)
    print()
    print("Commands: /voice, /text, /quit")
    print(f"Activation phrase: '{wake_phrase}'")
    print()

    mode = "text"

    # ==========================================
    # MAIN LOOP
    # ==========================================

    while True:

        try:

            # --------------------------------------
            # VOICE MODE
            # --------------------------------------

            if mode == "voice":

                wav = audio.record()

                transcript = gemini.transcribe(
                    wav
                )

                print(
                    f"You: {transcript}"
                )

                try:
                    wav.unlink(
                        missing_ok=True
                    )
                except Exception:
                    pass

            # --------------------------------------
            # TEXT MODE
            # --------------------------------------

            else:

                transcript = input(
                    "\nYou: "
                ).strip()

            # --------------------------------------
            # EMPTY INPUT
            # --------------------------------------

            if not transcript:
                continue

            # --------------------------------------
            # QUIT
            # --------------------------------------

            if transcript.lower() in {
                "/quit",
                "quit",
                "exit",
            }:

                print(
                    f"{assistant_name}: Goodbye."
                )

                break

            # --------------------------------------
            # SWITCH TO VOICE
            # --------------------------------------

            if transcript.lower() == "/voice":

                mode = "voice"

                print(
                    f"{assistant_name}: Voice mode enabled."
                )

                continue

            # --------------------------------------
            # SWITCH TO TEXT
            # --------------------------------------

            if transcript.lower() == "/text":

                mode = "text"

                print(
                    f"{assistant_name}: Text mode enabled."
                )

                continue

            # --------------------------------------
            # WAKE PHRASE
            # --------------------------------------

            wake = wake_phrase.lower()

            if (
                mode == "voice"
                and wake not in transcript.lower()
            ):

                print(
                    "Wake phrase not detected."
                )

                continue

            # --------------------------------------
            # REMOVE WAKE PHRASE
            # --------------------------------------

            if wake in transcript.lower():

                pos = transcript.lower().find(
                    wake
                )

                transcript = transcript[
                    pos + len(wake):
                ].strip(
                    " ,:.-"
                )

            # --------------------------------------
            # ONLY WAKE PHRASE
            # --------------------------------------

            if not transcript:

                print(
                    f"{assistant_name}: Yes?"
                )

                continue

            # --------------------------------------
            # LOCAL COMMANDS FIRST
            # --------------------------------------

            action_result = handle_command(
                transcript,
                registry,
                logger,
            )

            # --------------------------------------
            # LOCAL COMMAND RESULT
            # --------------------------------------

            if action_result is not None:

                answer = action_result.get(
                    "message",
                    str(action_result),
                )

            # --------------------------------------
            # GEMINI
            # --------------------------------------

            else:

                memory.add_turn(
                    "user",
                    transcript,
                )

                answer = gemini.ask(
                    transcript,
                    memory.context(),
                )

                memory.add_turn(
                    "assistant",
                    answer,
                )

            # --------------------------------------
            # DISPLAY ANSWER
            # --------------------------------------

            print(
                f"{assistant_name}: {answer}"
            )

            # --------------------------------------
            # VOICE RESPONSE
            # --------------------------------------

            if (
                settings.ui.get(
                    "speak_responses",
                    True,
                )
                and mode == "voice"
            ):

                output_path = (
                    ROOT
                    / "data"
                    / "jarvis_reply.wav"
                )

                gemini.speak(
                    answer,
                    output_path,
                )

                audio.play(
                    output_path
                )

        # ==========================================
        # KEYBOARD INTERRUPT
        # ==========================================

        except KeyboardInterrupt:

            print(
                f"\n{assistant_name}: Goodbye."
            )

            break

        # ==========================================
        # ERROR HANDLING
        # ==========================================

        except Exception as exc:

            logger.exception(
                "Unhandled error: %s",
                exc,
            )

            print(
                f"{assistant_name}: "
                f"I couldn't complete that request safely. "
                f"{type(exc).__name__}: {exc}"
            )


# ==============================================
# PROGRAM ENTRY POINT
# ==============================================

if __name__ == "__main__":
    main()