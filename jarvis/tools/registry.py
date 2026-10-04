from __future__ import annotations
from dataclasses import dataclass
from typing import Callable

@dataclass
class Tool:
    name: str
    description: str
    handler: Callable
    permission: str
    confirmation: bool

class ToolRegistry:
    def __init__(self, security, logger):
        self.security = security
        self.logger = logger
        self.tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        self.tools[tool.name] = tool

    def execute(self, name: str, **kwargs):
        tool = self.tools.get(name)
        if not tool:
            raise ValueError(f"Unknown tool: {name}")
        if not self.security.permitted(tool.permission):
            raise PermissionError(f"Permission disabled for {name}")
        if tool.confirmation and self.security.needs_confirmation(tool.permission):
            summary = f"{name} with arguments {kwargs}"
            if not self.security.confirm(summary):
                return {"ok": False, "message": "User denied the action."}
        self.logger.info("Executing tool=%s", name)
        return tool.handler(**kwargs)
