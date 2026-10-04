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

    # `tool_name` is positional-only. The original signature was execute(self, name, **kwargs),
    # so any tool that takes a `name` argument (open_application does) raised
    # "got multiple values for argument 'name'" before it ever ran.
    def execute(self, tool_name: str, /, **kwargs):
        tool = self.tools.get(tool_name)
        if not tool:
            raise ValueError(f"Unknown tool: {tool_name}")
        if not self.security.permitted(tool.permission):
            raise PermissionError(f"Permission disabled for {tool_name}")
        if tool.confirmation and self.security.needs_confirmation(tool.permission):
            summary = f"{tool_name} with arguments {kwargs}"
            if not self.security.confirm(summary):
                return {"ok": False, "message": "User denied the action."}
        self.logger.info("Executing tool=%s", tool_name)
        return tool.handler(**kwargs)
