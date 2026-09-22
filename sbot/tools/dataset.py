"""Read-only structured queries over Queryable Knowledge."""

from pathlib import Path
from typing import Any

from claw.knowledge.dataset import execute_dataset_query
from sbot.tools.base import Tool


class QueryKnowledgeDatasetTool(Tool):
    name = "query_knowledge_dataset"
    description = (
        "Inspect or query shared Queryable Knowledge datasets. Use inspect first to learn table and "
        "column names. Supports exact filters, grouping, count, distinct count, sum, average, min and max."
    )
    parameters = {
        "type": "object",
        "properties": {
            "knowledge_base": {"type": "string"},
            "action": {"type": "string", "enum": ["inspect", "query"], "default": "inspect"},
            "dataset": {"type": "string"},
            "table": {"type": "string"},
            "columns": {"type": "array", "items": {"type": "string"}},
            "filters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "column": {"type": "string"},
                        "operator": {
                            "type": "string",
                            "enum": ["eq", "ne", "gt", "gte", "lt", "lte", "contains", "starts_with"],
                        },
                        "value": {},
                    },
                    "required": ["column", "operator", "value"],
                },
            },
            "aggregations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "function": {
                            "type": "string",
                            "enum": ["count", "count_distinct", "sum", "avg", "min", "max"],
                        },
                        "column": {"type": "string"},
                        "alias": {"type": "string"},
                    },
                    "required": ["function"],
                },
            },
            "group_by": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        "required": ["knowledge_base"],
    }

    def __init__(self, store, user_id: str, root: Path):
        self.store = store
        self.root = root
        self.user_id = user_id

    async def execute(self, knowledge_base: str, **kwargs: Any) -> str:
        return await execute_dataset_query(
            self.store, self.root, self.user_id, knowledge_base=knowledge_base, **kwargs
        )
