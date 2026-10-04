from __future__ import annotations
import getpass
import re
from pathlib import Path

SECRET_PATTERNS = [
    re.compile(r"(AIza[0-9A-Za-z_-]{20,})"),
    re.compile(r"(GEMINI_API_KEY\s*=\s*)(\S+)", re.I),
    re.compile(r"(api[_-]?key\s*[:=]\s*)(\S+)", re.I),
]

def redact(text: str) -> str:
    value = text
    for pattern in SECRET_PATTERNS:
        value = pattern.sub(lambda m: m.group(1) + "[REDACTED]" if m.lastindex and m.lastindex >= 2 else "[REDACTED]", value)
    return value

class SecurityManager:
    def __init__(self, settings):
        self.settings = settings

    def permitted(self, tool_name: str) -> bool:
        return bool(self.settings.permissions.get(tool_name, False))

    def needs_confirmation(self, tool_name: str) -> bool:
        return tool_name in self.settings.security.get("require_confirmation", [])

    def confirm(self, description: str) -> bool:
        answer = input(f"\nCONFIRM ACTION: {description}\nType 'yes' to continue: ").strip().lower()
        return answer == "yes"

    def safe_workspace(self) -> Path:
        workspace = Path(self.settings.security.get("workspace", "./data/workspace"))
        if not workspace.is_absolute():
            workspace = Path(__file__).resolve().parent.parent / workspace
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace.resolve()

    def resolve_workspace_path(self, user_path: str) -> Path:
        root = self.safe_workspace()
        candidate = (root / user_path).resolve()
        if candidate != root and root not in candidate.parents:
            raise PermissionError("Path escapes the configured JARVIS workspace.")
        return candidate
