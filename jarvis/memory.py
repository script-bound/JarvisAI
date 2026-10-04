from __future__ import annotations
import json
from pathlib import Path
from datetime import datetime, timezone

class MemoryStore:
    def __init__(self, root: Path):
        self.path = root / "memory.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(json.dumps({"facts": [], "conversation": []}, indent=2), encoding="utf-8")

    def _load(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, data):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def add_turn(self, role: str, content: str):
        data = self._load()
        data["conversation"].append({
            "role": role,
            "content": content,
            "time": datetime.now(timezone.utc).isoformat()
        })
        data["conversation"] = data["conversation"][-40:]
        self._save(data)

    def add_fact(self, fact: str):
        data = self._load()
        if fact not in data["facts"]:
            data["facts"].append(fact)
        data["facts"] = data["facts"][-100:]
        self._save(data)

    def context(self) -> str:
        data = self._load()
        facts = "\n".join(f"- {x}" for x in data["facts"][-20:])
        conversation = "\n".join(
            f"{x['role']}: {x['content']}" for x in data["conversation"][-12:]
        )
        return f"MEMORY FACTS:\n{facts or '(none)'}\n\nRECENT CONVERSATION:\n{conversation or '(none)'}"
