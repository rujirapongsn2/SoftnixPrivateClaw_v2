"""Tool argument validation against the JSON schemas real tools carry, run
through both the claw and the sbot Tool bases so they cannot drift apart."""

from types import SimpleNamespace
from typing import Any

import pytest

import claw.core.connectors as claw_connectors
import claw.tools.base as claw_base
import claw.tools.registry as claw_registry
import sbot.core.connectors as sbot_connectors
import sbot.tools.base as sbot_base
import sbot.tools.registry as sbot_registry
from claw.tools import schema_validation

FLAVORS = {
    "claw": (claw_base.Tool, claw_connectors.McpToolProxy, claw_registry.ToolRegistry),
    "sbot": (sbot_base.Tool, sbot_connectors.McpToolProxy, sbot_registry.ToolRegistry),
}


@pytest.fixture(params=sorted(FLAVORS))
def flavor(request):
    return FLAVORS[request.param]


@pytest.fixture
def make_tool(flavor):
    base = flavor[0]

    def make(properties: dict[str, Any], **extra: Any):
        class SchemaTool(base):
            name = "t"
            description = "t"
            parameters = {"type": "object", "properties": properties, **extra}

            async def execute(self, **kwargs: Any) -> str:
                return "ran"

        return SchemaTool()

    return make


def _errors(make_tool, schema: dict, value: Any, **extra: Any) -> list[str]:
    return make_tool({"x": schema}, **extra).validate_params({"x": value})


# --- types --------------------------------------------------------------------

TYPE_CASES = [
    ({"type": "string"}, "a", True),
    ({"type": "string"}, 1, False),
    ({"type": "integer"}, 3, True),
    ({"type": "integer"}, 3.0, True),
    ({"type": "integer"}, 3.5, False),
    ({"type": "integer"}, True, False),
    ({"type": "integer"}, False, False),
    ({"type": "number"}, 2.5, True),
    ({"type": "number"}, 2, True),
    ({"type": "number"}, True, False),
    ({"type": "boolean"}, False, True),
    ({"type": "boolean"}, 0, False),
    ({"type": "null"}, None, True),
    ({"type": "null"}, "", False),
    ({"type": ["string", "null"]}, "x", True),
    ({"type": ["string", "null"]}, None, True),
    ({"type": ["string", "null"]}, 5, False),
    ({"type": ["integer", "string"]}, 5, True),
    ({"type": "string", "nullable": True}, None, True),
    ({"type": "string"}, None, False),
    ({"enum": ["a", "b"]}, "b", True),
    ({"enum": ["a", "b"]}, "c", False),
    ({"enum": [1, 2]}, True, False),
    ({"const": "fixed"}, "fixed", True),
    ({"const": "fixed"}, "other", False),
    ({"const": 0}, False, False),
    (True, object(), True),
    (False, "anything", False),
]


@pytest.mark.parametrize(("schema", "value", "ok"), TYPE_CASES)
def test_type_enum_const(make_tool, schema, value, ok):
    errors = _errors(make_tool, schema, value)
    assert (errors == []) is ok, errors


def test_union_type_error_names_every_allowed_type(make_tool):
    assert _errors(make_tool, {"type": ["string", "null"]}, 5) == ["x should be string or null"]


def test_incident_shape_accepts_string_and_null_and_never_raises(make_tool):
    tool = make_tool({"team": {"type": ["string", "null"]}})
    assert tool.validate_params({"team": "x"}) == []
    assert tool.validate_params({"team": None}) == []
    assert tool.validate_params({}) == []


def test_nested_object_and_array(make_tool):
    schema = {
        "type": "object",
        "required": ["ids"],
        "properties": {
            "ids": {"type": "array", "items": {"type": "integer"}},
            "inner": {"type": "object", "properties": {"flag": {"type": "boolean"}}},
        },
    }
    assert _errors(make_tool, schema, {"ids": [1, 2], "inner": {"flag": True}}) == []
    assert _errors(make_tool, schema, {"ids": [1, "2"], "inner": {"flag": 1}}) == [
        "x.ids[1] should be integer",
        "x.inner.flag should be boolean",
    ]
    assert _errors(make_tool, schema, {}) == ["missing required x.ids"]


def test_required_at_top_level(make_tool):
    tool = make_tool({"a": {"type": "string"}}, required=["a"])
    assert tool.validate_params({}) == ["missing required a"]
    assert tool.validate_params({"a": "v"}) == []


def test_positional_items(make_tool):
    schema = {"type": "array", "items": [{"type": "string"}, {"type": "integer"}]}
    assert _errors(make_tool, schema, ["a", 1, {"extra": True}]) == []
    assert _errors(make_tool, schema, [1, "a"]) == ["x[0] should be string", "x[1] should be integer"]


# --- combinators and $ref --------------------------------------------------------


def test_any_of_nullable_pydantic_shape(make_tool):
    schema = {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}
    assert _errors(make_tool, schema, "v") == []
    assert _errors(make_tool, schema, None) == []
    assert _errors(make_tool, schema, 3) == [
        "x matches none of the allowed forms (x should be string; x should be null)"
    ]


def test_one_of_needs_at_least_one_branch(make_tool):
    schema = {"oneOf": [{"type": "string"}, {"type": "string", "format": "date"}]}
    assert _errors(make_tool, schema, "2026-01-01") == []
    assert _errors(make_tool, schema, 1) != []


def test_all_of_needs_every_branch(make_tool):
    schema = {
        "allOf": [
            {"type": "object", "required": ["a"]},
            {"properties": {"b": {"type": "integer"}}},
        ]
    }
    assert _errors(make_tool, schema, {"a": 1, "b": 2}) == []
    assert _errors(make_tool, schema, {"b": "no"}) == ["missing required x.a", "x.b should be integer"]


@pytest.mark.parametrize("defs_key", ["$defs", "definitions"])
def test_local_ref(make_tool, defs_key):
    tool = make_tool(
        {"x": {"$ref": f"#/{defs_key}/State"}},
        **{defs_key: {"State": {"type": "string", "enum": ["open", "done"]}}},
    )
    assert tool.validate_params({"x": "open"}) == []
    assert tool.validate_params({"x": "nope"}) == ["x must be one of ['open', 'done']"]


def test_recursive_ref_follows_the_value(make_tool):
    node = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "children": {"type": "array", "items": {"$ref": "#/$defs/Node"}},
        },
    }
    tool = make_tool({"x": {"$ref": "#/$defs/Node"}}, **{"$defs": {"Node": node}})
    deep = {"name": "a", "children": [{"name": "b", "children": [{"name": "c", "children": []}]}]}
    assert tool.validate_params({"x": deep}) == []
    deep["children"][0]["children"][0]["name"] = 7
    assert tool.validate_params({"x": deep}) == ["x.children[0].children[0].name should be string"]


# --- malformed or unsupported schemas -------------------------------------------

MALFORMED = [
    ({"type": 5}, "a", "type must be a type name or a list of them"),
    ({"type": []}, "a", "type must be a type name or a list of them"),
    ({"type": "float"}, 1.0, "unknown type 'float'"),
    ({"type": ["string", "date"]}, "a", "unknown type 'date'"),
    ({"type": "object", "properties": ["a"]}, {}, "properties must be an object"),
    ({"type": "object", "required": "a"}, {}, "required must be a list of names"),
    ({"enum": "ab"}, "a", "enum must be a list"),
    ({"anyOf": []}, "a", "anyOf must be a non-empty list"),
    ({"oneOf": {"type": "string"}}, "a", "oneOf must be a non-empty list"),
    ({"type": "array", "items": "string"}, ["a"], "items must be a schema or a list of schemas"),
    ({"$ref": "https://example.com/schema.json"}, "a", "is not a local reference"),
    ({"$ref": "#/$defs/Missing"}, "a", "does not resolve"),
    ("string", "a", "expected a schema object"),
]


@pytest.mark.parametrize(("schema", "value", "reason"), MALFORMED)
def test_malformed_schema_is_a_schema_problem(make_tool, schema, value, reason):
    verdict = make_tool({"x": schema}).check_params({"x": value})
    assert verdict.errors == ()
    assert verdict.schema_problem is not None
    assert verdict.schema_problem.startswith("schema for 'x' is unsupported: ")
    assert reason in verdict.schema_problem
    assert make_tool({"x": schema}).validate_params({"x": value}) == [verdict.schema_problem]


def test_circular_ref_is_a_schema_problem(make_tool):
    tool = make_tool(
        {"x": {"$ref": "#/$defs/A"}},
        **{"$defs": {"A": {"anyOf": [{"$ref": "#/$defs/B"}]}, "B": {"$ref": "#/$defs/A"}}},
    )
    problem = tool.check_params({"x": "v"}).schema_problem
    assert problem is not None and "circular" in problem


def test_excessive_depth_is_a_schema_problem(make_tool):
    schema: dict[str, Any] = {"type": "string"}
    value: Any = "leaf"
    for _ in range(schema_validation.MAX_DEPTH + 5):
        schema = {"type": "object", "properties": {"n": schema}}
        value = {"n": value}
    problem = make_tool({"x": schema}).check_params({"x": value}).schema_problem
    assert problem is not None and "nested deeper than" in problem


def test_exponential_ref_fanout_is_bounded(make_tool):
    defs = {f"D{i}": {"allOf": [{"$ref": f"#/$defs/D{i + 1}"}] * 2} for i in range(40)}
    defs["D40"] = {"type": "string"}
    tool = make_tool({"x": {"$ref": "#/$defs/D0"}}, **{"$defs": defs})
    problem = tool.check_params({"x": "v"}).schema_problem
    assert problem is not None and "steps" in problem


def test_messages_never_echo_the_argument_and_truncate_schema_quotes(make_tool):
    secret = "sk-live-" + "s" * 200
    enum = [f"option-{i}" for i in range(100)]
    errors = _errors(make_tool, {"enum": enum}, secret)
    assert len(errors) == 1
    assert secret not in errors[0] and "sk-live" not in errors[0]
    assert len(errors[0]) < 200


# --- end to end through the registry ---------------------------------------------

LINEAR_LIST_ISSUES = {
    "type": "object",
    "properties": {
        "query": {"type": ["string", "null"], "description": "Free-text search"},
        "team": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
        "assignee": {"type": "string", "nullable": True},
        "state": {"anyOf": [{"$ref": "#/$defs/IssueState"}, {"type": "null"}]},
        "orderBy": {"type": "string", "enum": ["createdAt", "updatedAt"], "default": "updatedAt"},
        "includeArchived": {"type": "boolean", "default": False},
        "limit": {"type": "integer", "minimum": 1, "maximum": 250, "default": 50},
        "labels": {"type": ["array", "null"], "items": {"type": "string"}},
        "filter": {
            "type": "object",
            "properties": {
                "priority": {"anyOf": [{"type": "integer", "enum": [0, 1, 2, 3, 4]}, {"type": "null"}]},
                "createdAt": {
                    "type": "object",
                    "properties": {"gte": {"type": "string", "format": "date-time"}},
                },
            },
            "additionalProperties": False,
        },
    },
    "required": [],
    "additionalProperties": False,
    "$defs": {"IssueState": {"type": "string", "enum": ["backlog", "todo", "in_progress", "done"]}},
    "$schema": "http://json-schema.org/draft-07/schema#",
}


class RecordingSession:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments, read_timeout_seconds=None):
        self.calls.append((name, arguments))
        return SimpleNamespace(content=[SimpleNamespace(text="[]")], isError=False)


def _linear_registry(flavor):
    _, proxy_cls, registry_cls = flavor
    session = RecordingSession()
    registry = registry_cls()
    registry.register(proxy_cls(session, "linear", "list_issues", "List issues", LINEAR_LIST_ISSUES))
    return registry, session


async def test_linear_list_issues_valid_arguments_reach_the_session(flavor):
    registry, session = _linear_registry(flavor)
    args = {
        "query": None,
        "team": "ENG",
        "assignee": None,
        "state": "in_progress",
        "orderBy": "createdAt",
        "includeArchived": False,
        "limit": 25,
        "labels": ["bug"],
        "filter": {"priority": 2, "createdAt": {"gte": "2026-10-01T00:00:00Z"}},
    }
    result = await registry.execute("mcp_linear_list_issues", args)
    assert result == "[]"
    assert session.calls == [("list_issues", args)]


async def test_linear_list_issues_bad_arguments_never_reach_the_session(flavor):
    registry, session = _linear_registry(flavor)
    result = await registry.execute(
        "mcp_linear_list_issues", {"limit": True, "state": "closed", "filter": {"priority": "high"}}
    )
    assert result.startswith("Error: invalid parameters for 'mcp_linear_list_issues'")
    assert "limit should be integer" in result
    assert "state matches none of the allowed forms" in result
    assert "filter.priority matches none of the allowed forms" in result
    assert session.calls == []
