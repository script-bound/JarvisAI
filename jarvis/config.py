from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import os
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"

@dataclass
class Settings:
    assistant: dict
    models: dict
    audio: dict
    security: dict
    permissions: dict
    integrations: dict
    ui: dict

def load_settings() -> Settings:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    models = raw.get("models", {})
    models["reasoning"] = os.getenv("JARVIS_REASONING_MODEL", models.get("reasoning"))
    models["transcribe"] = os.getenv("JARVIS_TRANSCRIBE_MODEL", models.get("transcribe"))
    models["tts"] = os.getenv("JARVIS_TTS_MODEL", models.get("tts"))
    return Settings(
        assistant=raw.get("assistant", {}),
        models=models,
        audio=raw.get("audio", {}),
        security=raw.get("security", {}),
        permissions=raw.get("permissions", {}),
        integrations=raw.get("integrations", {}),
        ui=raw.get("ui", {}),
    )
