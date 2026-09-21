"""Rule 3: output is schema-validated.

A deliberately small JSON-Schema subset — object/array/string/number/boolean,
`required`, `enum`, `minItems`, `additionalProperties: false`. That is the
whole of what the prompts in this package declare, and the same dict is sent
to the API as `output_config.format`, so the server-side constraint and the
client-side check can never describe different shapes.

Validating locally as well as server-side is not belt-and-braces paranoia: the
provider falls back to an unstructured request when a model does not support
structured output (see `providers.py`), and that path has no server-side
guarantee at all.
"""

from __future__ import annotations

from typing import Any

from api.ai.numbers import unsupported_figures


def schema_errors(value: Any, schema: dict[str, Any], path: str = "") -> list[str]:
    where = path or "output"
    expected = schema.get("type")

    if expected == "object":
        if not isinstance(value, dict):
            return [f"{where}: expected an object"]
        errors: list[str] = []
        properties: dict[str, Any] = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{where}: missing required field '{name}'")
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    errors.append(f"{where}: unexpected field '{name}'")
        for name, sub in properties.items():
            if name in value:
                errors.extend(schema_errors(value[name], sub, f"{where}.{name}"))
        return errors

    if expected == "array":
        if not isinstance(value, list):
            return [f"{where}: expected an array"]
        errors = []
        minimum = schema.get("minItems")
        if minimum is not None and len(value) < minimum:
            errors.append(f"{where}: expected at least {minimum} items")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                errors.extend(schema_errors(item, item_schema, f"{where}[{index}]"))
        return errors

    if expected == "string":
        if not isinstance(value, str):
            return [f"{where}: expected a string"]
        if "enum" in schema and value not in schema["enum"]:
            return [f"{where}: '{value}' is not one of {schema['enum']}"]
        return []

    if expected == "integer":
        # bool is an int in Python and is never what an integer field means.
        if isinstance(value, bool) or not isinstance(value, int):
            return [f"{where}: expected an integer"]
        return []

    if expected == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return [f"{where}: expected a number"]
        return []

    if expected == "boolean":
        return [] if isinstance(value, bool) else [f"{where}: expected a boolean"]

    return []


def validate(
    data: Any, schema: dict[str, Any], evidence: Any
) -> tuple[list[str], list[str]]:
    """Shape errors and unsupported figures, in that order.

    Returned rather than raised: the caller records a refusal and renders the
    template, and both halves of that are normal operation.
    """
    return schema_errors(data, schema), unsupported_figures(data, evidence)
