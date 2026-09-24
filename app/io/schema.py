"""Convert a Pydantic JSON schema into the subset providers accept.

Pydantic emits full JSON Schema: `$defs` + `$ref` for nested models, `anyOf`
for optionals, `prefixItems` for tuples, plus `title` and `default` everywhere.
Structured-output APIs (Gemini's `response_schema`, and most OpenAI-compatible
`json_schema` modes) accept a narrower OpenAPI-flavoured subset and either
error or silently ignore the rest.

Getting this wrong is a quiet failure: the model returns *something*, Pydantic
rejects it, and the retry burns another call. So the conversion happens once,
here, with tests.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

# Keys the providers understand. Everything else is dropped.
_KEEP = frozenset({
    "type", "properties", "items", "required", "enum", "description",
    "nullable", "format",
})

_MAX_DEPTH = 12          # guards against a self-referential model


def to_provider_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Return `model`'s schema, dereferenced and reduced to the safe subset."""
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})
    return _convert(raw, defs, depth=0)


def _convert(node: Any, defs: dict[str, Any], depth: int) -> Any:
    if depth > _MAX_DEPTH or not isinstance(node, dict):
        return {"type": "string"} if depth > _MAX_DEPTH else node

    # $ref -> inline the definition
    if "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        target = defs.get(name)
        if target is None:
            return {"type": "string"}
        merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
        return _convert(merged, defs, depth + 1)

    # Optional[X] is anyOf[X, null] -> X with nullable
    if "anyOf" in node:
        variants = [v for v in node["anyOf"] if v.get("type") != "null"]
        nullable = len(variants) != len(node["anyOf"])
        if not variants:
            return {"type": "string", "nullable": True}
        # A genuine multi-type union has no clean representation; take the
        # first branch rather than emitting something the provider rejects.
        out = _convert(variants[0], defs, depth + 1)
        if nullable and isinstance(out, dict):
            out["nullable"] = True
        return out

    out: dict[str, Any] = {}

    # tuple[int, int] -> prefixItems; providers only understand a plain array
    if "prefixItems" in node:
        first = node["prefixItems"][0] if node["prefixItems"] else {"type": "string"}
        out["type"] = "array"
        out["items"] = _convert(first, defs, depth + 1)
        if desc := node.get("description"):
            out["description"] = desc
        return out

    for key, value in node.items():
        if key not in _KEEP:
            continue
        if key == "properties" and isinstance(value, dict):
            out["properties"] = {
                k: _convert(v, defs, depth + 1) for k, v in value.items()
            }
        elif key == "items":
            out["items"] = _convert(value, defs, depth + 1)
        else:
            out[key] = value

    # A Literal with one option becomes `const`, which is not in the subset.
    if "const" in node and "enum" not in out:
        out["enum"] = [node["const"]]
        out.setdefault("type", "string")

    # An object with no declared properties confuses some providers.
    if out.get("type") == "object" and not out.get("properties"):
        out["properties"] = {}

    return out


def required_paths(schema: dict[str, Any], prefix: str = "") -> list[str]:
    """Dotted paths of every required field. Useful in tests and prompts."""
    found: list[str] = []
    for name in schema.get("required", []):
        path = f"{prefix}{name}"
        found.append(path)
        child = (schema.get("properties") or {}).get(name, {})
        if child.get("type") == "object":
            found.extend(required_paths(child, f"{path}."))
    return found
