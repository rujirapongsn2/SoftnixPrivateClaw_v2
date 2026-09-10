"""Blackboard tool: how a mission node hands a large result to later steps.

A node's final message is what its dependents receive, and it is summarized on
the way. Anything bigger than a summary — a dataset, a draft, a table of
findings — goes here under a key, and downstream nodes read the keys they
actually need. That is what keeps a mission's prompt cost proportional to the
task rather than to the size of everything produced so far (PRD §3.5).
"""

import json
from typing import Any

from sbot.db.stores import MissionStore
from sbot.tools.base import Tool

# Values are replayed into a later node's prompt, so a single write cannot be
# allowed to consume that node's whole context window.
MAX_VALUE_CHARS = 100_000
MAX_KEY_CHARS = 64


class BlackboardTool(Tool):
    name = "blackboard"
    description = (
        "Read or write the mission's shared workspace. Use 'write' to publish a result that "
        "later steps will need in full (a draft, dataset, or list of findings) — put it under a "
        "descriptive key and mention the key in your final message. Use 'read' to fetch a key "
        "another step published. Your final message should carry your conclusion; the blackboard "
        "carries the bulk."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read", "write", "list"]},
            "key": {"type": "string", "description": "Key to read or write, e.g. 'competitor_table'"},
            "value": {
                "type": "string",
                "description": "Content to store (required for 'write'). Overwrites the key.",
            },
            "offset": {"type": "integer", "description": "Character offset for reading large results."},
            "limit": {"type": "integer", "description": "Page length, at most 8000 characters."},
        },
        "required": ["action"],
    }

    def __init__(self, missions: MissionStore, mission_id: str, node_id: str):
        self.missions = missions
        # Both are set by the scheduler, never by the model: a node may only
        # touch its own mission's blackboard.
        self.mission_id = mission_id
        self.node_id = node_id

    async def execute(self, action: str, **kwargs: Any) -> str:
        key = str(kwargs.get("key") or "").strip()

        if action == "list":
            manifest = await self.missions.blackboard_manifest(self.mission_id)
            if not manifest:
                return "The blackboard is empty."
            return json.dumps(manifest, ensure_ascii=False, indent=2, default=str)

        if action == "read":
            if not key:
                return "Error: read requires a 'key'."
            value = await self.missions.blackboard_read(self.mission_id, key)
            if value is None and key.startswith('result:'):
                # Compatibility with missions completed before automatic handoff.
                nodes = await self.missions.get_nodes(self.mission_id)
                node = next((node for node in nodes if node.id == key.removeprefix('result:')), None)
                value = node.output if node else None
            if value is None:
                return f"No blackboard entry named '{key}'. Use action 'list' to see what exists."
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
            offset = max(0, int(kwargs.get('offset') or 0))
            limit = min(8000, max(1, int(kwargs.get('limit') or 8000)))
            page = text[offset:offset + limit]
            return page + (f"\n[More: read key {key!r} with offset={offset + limit}; total={len(text)}]" if offset + limit < len(text) else '')

        if action == "write":
            if key.startswith(('result:', 'scope:')):
                return 'Error: result: and scope: keys are reserved for the scheduler.'
            if not key:
                return "Error: write requires a 'key'."
            if len(key) > MAX_KEY_CHARS:
                return f"Error: key must be at most {MAX_KEY_CHARS} characters."
            if key.startswith(("result:", "contract:", "scope:", "delivery:")):
                return "Error: runtime result and scope keys are read-only."
            value = kwargs.get("value")
            if value is None or value == "":
                return "Error: write requires a 'value'."
            value = str(value)
            if len(value) > MAX_VALUE_CHARS:
                return (
                    f"Error: value is {len(value)} characters, over the {MAX_VALUE_CHARS} limit. "
                    "Write a shorter summary, or split it across several keys."
                )
            await self.missions.blackboard_write(self.mission_id, key, value, node_id=self.node_id)
            return f"Wrote {len(value)} characters to blackboard key '{key}'."

        return f"Error: unknown action '{action}'"
