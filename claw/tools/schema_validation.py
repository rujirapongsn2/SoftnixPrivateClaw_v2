"""The one JSON-schema checker for tool arguments, shared by claw and sbot tools.

Schemas come from built-in tools, API connector operations, and third-party MCP
servers (`inputSchema`, passed through `_slim_schema` with only descriptions
clipped). MCP servers built on pydantic or zod emit `anyOf: [{...}, {"type":
"null"}]`, `type: ["string", "null"]` and `$ref` into `$defs`, so those are
supported rather than assumed away.

Contract
--------
`check(value, schema) -> Verdict` never raises. A Verdict is one of:

- valid: `errors == ()` and `schema_problem is None`.
- argument errors: `errors` is non-empty and `schema_problem is None`. The
  value does not fit a usable schema; the model should fix its arguments.
- schema problem: `schema_problem` is one message, `"schema for '<path>' is
  unsupported: <reason>"`, and `errors == ()`. The tool definition is at fault,
  so no verdict on the arguments is given (never a silent pass). Causes: `type`
  that is not a string or list of known type names, `properties` not an object,
  `required` not a list of strings, `enum` not a list, `items`/`anyOf`/`oneOf`/
  `allOf` of the wrong shape, a non-local or unresolvable `$ref`, a `$ref`
  cycle that consumes no input, nesting deeper than `MAX_DEPTH`, or more than
  `MAX_STEPS` schema nodes visited. The last two bound adversarial schemas; a
  pathologically deep or huge argument against a recursive schema also lands
  there. Only the parts of a schema the value actually reaches are inspected,
  and `anyOf`/`oneOf` stop at the first matching branch.

`validate(value, schema) -> list[str]` is `check(...).messages`: the argument
errors, or `[schema_problem]`.

Enforced: `type` (string or list; "integer" accepts 3.0; booleans never count
as numbers), OpenAPI `nullable: true`, `enum`, `const`, `properties`,
`required`, `items` (schema, or list for positional items), `prefixItems`,
`anyOf`, `oneOf`, `allOf`, local `$ref` (`#`, `#/$defs/...`,
`#/definitions/...`, any JSON pointer into the root), and boolean schemas.

`oneOf` is checked like `anyOf` (at least one branch). Exactly-one needs every
branch evaluated, and generated schemas often have overlapping branches (a
string branch and a string-with-format branch) that would wrongly reject a
value the server accepts; the server stays the authority on exclusivity.

Ignored, as before: `format`, `pattern`, `minLength`, `maxLength`, `minimum`,
`maximum`, `exclusiveMinimum`, `exclusiveMaximum`, `multipleOf`, `minItems`,
`maxItems`, `uniqueItems`, `contains`, `minProperties`, `maxProperties`,
`additionalProperties`, `patternProperties`, `propertyNames`,
`dependentRequired`, `dependentSchemas`, `dependencies`, `if`/`then`/`else`,
`not`, `unevaluatedProperties`, `unevaluatedItems`, and annotations (`default`,
`title`, `description`, `examples`, ...).

Messages name the parameter path and the expectation. They never include the
argument value; schema content they quote (`enum`, `const`) is truncated.
"""

from dataclasses import dataclass
from typing import Any

MAX_DEPTH = 100
MAX_STEPS = 100_000
_MAX_QUOTE_CHARS = 120

_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: (isinstance(v, int) and not isinstance(v, bool))
    or (isinstance(v, float) and v.is_integer()),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


@dataclass(frozen=True)
class Verdict:
    errors: tuple[str, ...] = ()
    schema_problem: str | None = None

    @property
    def messages(self) -> list[str]:
        return [self.schema_problem] if self.schema_problem else list(self.errors)


class _SchemaProblem(Exception):
    pass


def check(value: Any, schema: Any) -> Verdict:
    walker = _Walker(schema)
    try:
        errors = walker.walk(value, schema, "", 0, frozenset())
    except _SchemaProblem as exc:
        return Verdict(schema_problem=str(exc))
    return Verdict(errors=tuple(errors))


def validate(value: Any, schema: Any) -> list[str]:
    return check(value, schema).messages


def _quote(obj: Any) -> str:
    text = repr(obj)
    return text if len(text) <= _MAX_QUOTE_CHARS else text[: _MAX_QUOTE_CHARS - 3] + "..."


def _json_equal(a: Any, b: Any) -> bool:
    # Python's 1 == True would let a boolean satisfy a numeric enum.
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_json_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_json_equal(a[k], b[k]) for k in a)
    return a == b


class _Walker:
    def __init__(self, root: Any):
        self.root = root
        self.steps = 0

    def problem(self, path: str, reason: str) -> _SchemaProblem:
        return _SchemaProblem(f"schema for '{path or 'parameters'}' is unsupported: {reason}")

    def walk(self, val: Any, schema: Any, path: str, depth: int, refs: frozenset[str]) -> list[str]:
        """`refs` are the $refs already followed at this value position; seeing
        one again means a cycle that would never consume input."""
        self.steps += 1
        if self.steps > MAX_STEPS:
            raise self.problem(path, f"needs more than {MAX_STEPS} steps to check")
        if depth > MAX_DEPTH:
            raise self.problem(path, f"nested deeper than {MAX_DEPTH} levels")
        if schema is True:
            return []
        if schema is False:
            return [f"{path or 'parameter'} is not allowed"]
        if not isinstance(schema, dict):
            raise self.problem(path, f"expected a schema object, got {type(schema).__name__}")

        errors: list[str] = []
        if "$ref" in schema:
            ref = schema["$ref"]
            if ref in refs:
                raise self.problem(path, f"$ref {_quote(ref)} is circular")
            target = self.resolve(ref, path)
            errors.extend(self.walk(val, target, path, depth + 1, refs | {ref}))
        if val is None and schema.get("nullable") is True:
            return errors
        label = path or "parameter"

        type_errors = self.check_type(val, schema, path, label)
        if type_errors:
            return errors + type_errors
        if "enum" in schema:
            options = schema["enum"]
            if not isinstance(options, list):
                raise self.problem(path, "enum must be a list")
            if not any(_json_equal(val, o) for o in options):
                errors.append(f"{label} must be one of {_quote(options)}")
        if "const" in schema and not _json_equal(val, schema["const"]):
            errors.append(f"{label} must equal {_quote(schema['const'])}")

        for keyword in ("anyOf", "oneOf"):
            if keyword in schema:
                errors.extend(self.any_of(val, schema[keyword], keyword, path, label, depth, refs))
        if "allOf" in schema:
            for branch in self.branches(schema["allOf"], "allOf", path):
                errors.extend(self.walk(val, branch, path, depth + 1, refs))

        if isinstance(val, dict):
            errors.extend(self.check_object(val, schema, path, depth))
        elif isinstance(val, list):
            errors.extend(self.check_array(val, schema, path, depth))
        return errors

    def check_type(self, val: Any, schema: dict, path: str, label: str) -> list[str]:
        if "type" not in schema:
            return []
        declared = schema["type"]
        names = [declared] if isinstance(declared, str) else declared
        if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
            raise self.problem(path, f"type must be a type name or a list of them, got {_quote(declared)}")
        unknown = [n for n in names if n not in _TYPE_CHECKS]
        if unknown:
            raise self.problem(path, f"unknown type {_quote(unknown[0])}")
        if any(_TYPE_CHECKS[n](val) for n in names):
            return []
        return [f"{label} should be {' or '.join(names)}"]

    def any_of(self, val, branches, keyword, path, label, depth, refs) -> list[str]:
        failures: list[str] = []
        for branch in self.branches(branches, keyword, path):
            branch_errors = self.walk(val, branch, path, depth + 1, refs)
            if not branch_errors:
                return []
            failures.append(branch_errors[0])
        shown = "; ".join(failures[:3]) + ("; ..." if len(failures) > 3 else "")
        return [f"{label} matches none of the allowed forms ({shown})"]

    def check_object(self, val: dict, schema: dict, path: str, depth: int) -> list[str]:
        errors: list[str] = []
        props = schema.get("properties", {})
        if not isinstance(props, dict):
            raise self.problem(path, "properties must be an object")
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(k, str) for k in required):
            raise self.problem(path, "required must be a list of names")
        for key in required:
            if key not in val:
                errors.append(f"missing required {(path + '.' + key) if path else key}")
        for key, item in val.items():
            if key in props:
                errors.extend(self.walk(item, props[key], f"{path}.{key}" if path else key, depth + 1, frozenset()))
        return errors

    def check_array(self, val: list, schema: dict, path: str, depth: int) -> list[str]:
        items = schema.get("items")
        positional = schema.get("prefixItems")
        if isinstance(items, list):
            positional, items = items, None
        if positional is not None and not isinstance(positional, list):
            raise self.problem(path, "prefixItems must be a list")
        if items is not None and not isinstance(items, (dict, bool)):
            raise self.problem(path, "items must be a schema or a list of schemas")
        positional = positional or []
        errors: list[str] = []
        for i, item in enumerate(val):
            item_schema = positional[i] if i < len(positional) else items
            if item_schema is not None:
                errors.extend(self.walk(item, item_schema, f"{path or 'parameter'}[{i}]", depth + 1, frozenset()))
        return errors

    def branches(self, branches: Any, keyword: str, path: str) -> list:
        if not isinstance(branches, list) or not branches:
            raise self.problem(path, f"{keyword} must be a non-empty list")
        return branches

    def resolve(self, ref: Any, path: str) -> Any:
        if not isinstance(ref, str) or not (ref == "#" or ref.startswith("#/")):
            raise self.problem(path, f"$ref {_quote(ref)} is not a local reference")
        node = self.root
        for token in ref[2:].split("/") if ref != "#" else []:
            token = token.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and token in node:
                node = node[token]
            elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
                node = node[int(token)]
            else:
                raise self.problem(path, f"$ref {_quote(ref)} does not resolve")
        return node
