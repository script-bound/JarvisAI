from __future__ import annotations
import platform
import subprocess
import webbrowser
from datetime import datetime
import json
from pathlib import Path
from .registry import Tool

def register_safe_tools(registry):
    security = registry.security

    def list_files(path="."):
        target = security.resolve_workspace_path(path)
        if not target.exists():
            return {"ok": False, "message": "Path does not exist."}
        if not target.is_dir():
            return {"ok": False, "message": "Path is not a directory."}
        items = []
        for p in sorted(target.iterdir(), key=lambda x: x.name.lower())[:100]:
            items.append({"name": p.name, "directory": p.is_dir()})
        return {"ok": True, "path": str(target), "items": items}

    def open_application(name):
        # Only applications explicitly allowlisted here are accepted.
        apps = {
            "calculator": {
                "Windows": ["calc.exe"],
                "Darwin": ["open", "-a", "Calculator"],
                "Linux": ["gnome-calculator"],
            },
            "notepad": {
                "Windows": ["notepad.exe"],
                "Darwin": ["open", "-a", "TextEdit"],
                "Linux": ["gedit"],
            },
        }
        system = platform.system()
        command = apps.get(name.lower(), {}).get(system)
        if not command:
            return {"ok": False, "message": "Application is not allowlisted on this operating system."}
        subprocess.Popen(command, shell=False)
        return {"ok": True, "message": f"Opened {name}."}

    def open_url(url):
        if not (url.startswith("https://") or url.startswith("http://")):
            return {"ok": False, "message": "Only http(s) URLs are allowed."}
        webbrowser.open(url)
        return {"ok": True, "message": "Opened URL in the default browser."}

    def create_reminder(text, when):
        reminders_path = security.safe_workspace().parent / "reminders.json"
        try:
            reminders = json.loads(reminders_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            reminders = []
        reminders.append({
            "text": text,
            "when": when,
            "created_at": datetime.now().isoformat(),
            "status": "pending",
        })
        reminders_path.write_text(json.dumps(reminders, indent=2), encoding="utf-8")
        return {"ok": True, "message": "Reminder saved locally. A scheduler is not included yet.", "when": when}

    registry.register(Tool("list_files", "List files inside the configured JARVIS workspace.", list_files, "list_files", False))
    registry.register(Tool("open_application", "Open an explicitly allowlisted local application.", open_application, "open_application", True))
    registry.register(Tool("open_url", "Open an http(s) URL in the default browser.", open_url, "open_url", True))
    registry.register(Tool("create_reminder", "Save a local reminder to JSON.", create_reminder, "create_reminder", True))
