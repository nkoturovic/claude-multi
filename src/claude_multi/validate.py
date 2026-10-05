"""Closed-vocabulary JSON schema validation for claude-multi v2.

Only the approved keyword set is supported; every unknown keyword or schema
version fails closed. No third-party packages. Supported keywords: type,
required, properties, additionalProperties, items, enum, const, oneOf,
minItems, maxItems, uniqueItems, minLength, maxLength, pattern, minimum,
maximum. A schema document may carry a top-level ``version`` metadata field,
which must equal the supported schema version.
"""

from __future__ import annotations

import re
from typing import Any

from . import errors, strict_json


SUPPORTED_SCHEMA_VERSION = 1

VALIDATION_KEYWORDS = frozenset(
    {
        "type",
        "required",
        "properties",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "oneOf",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
    }
)

TYPE_NAMES = frozenset(
    {"object", "array", "string", "integer", "number", "boolean", "null"}
)


class SchemaError(errors.ClaudeMultiError, ValueError):
    """Raised when a schema document itself is invalid."""


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_schema_node(schema: Any, path: str, errors: list[str]) -> None:
    if not isinstance(schema, dict):
        errors.append(f"{path}: schema must be an object")
        return
    for keyword in sorted(schema):
        if keyword not in VALIDATION_KEYWORDS:
            errors.append(f"{path}: unsupported schema keyword {keyword!r}")

    if "type" in schema:
        declared = schema["type"]
        if isinstance(declared, str):
            if declared not in TYPE_NAMES:
                errors.append(f"{path}.type: unknown type {declared!r}")
        elif isinstance(declared, list):
            if not declared or len(set(declared)) != len(declared):
                errors.append(f"{path}.type: type list must be non-empty and unique")
            for item in declared:
                if item not in TYPE_NAMES:
                    errors.append(f"{path}.type: unknown type {item!r}")
        else:
            errors.append(f"{path}.type: must be a type name or a list of type names")

    if "required" in schema:
        required = schema["required"]
        if not isinstance(required, list) or not all(
            isinstance(item, str) for item in required
        ):
            errors.append(f"{path}.required: must be an array of strings")
        elif len(set(required)) != len(required):
            errors.append(f"{path}.required: entries must be unique")

    if "properties" in schema:
        properties = schema["properties"]
        if not isinstance(properties, dict):
            errors.append(f"{path}.properties: must be an object")
        else:
            for key in sorted(properties):
                _check_schema_node(properties[key], f"{path}.properties.{key}", errors)

    if "additionalProperties" in schema:
        extra = schema["additionalProperties"]
        if not isinstance(extra, bool):
            _check_schema_node(extra, f"{path}.additionalProperties", errors)

    if "items" in schema:
        _check_schema_node(schema["items"], f"{path}.items", errors)

    if "enum" in schema:
        options = schema["enum"]
        if not isinstance(options, list) or not options:
            errors.append(f"{path}.enum: must be a non-empty array")

    if "oneOf" in schema:
        variants = schema["oneOf"]
        if not isinstance(variants, list) or not variants:
            errors.append(f"{path}.oneOf: must be a non-empty array of schemas")
        else:
            for index, variant in enumerate(variants):
                _check_schema_node(variant, f"{path}.oneOf[{index}]", errors)

    for pair in (("minItems", "maxItems"), ("minLength", "maxLength")):
        low_key, high_key = pair
        for key in pair:
            if key in schema and not (
                _is_integer(schema[key]) and schema[key] >= 0
            ):
                errors.append(f"{path}.{key}: must be a non-negative integer")
        if (
            low_key in schema
            and high_key in schema
            and _is_integer(schema[low_key])
            and _is_integer(schema[high_key])
            and schema[low_key] > schema[high_key]
        ):
            errors.append(f"{path}: {low_key} exceeds {high_key}")

    if "uniqueItems" in schema and not isinstance(schema["uniqueItems"], bool):
        errors.append(f"{path}.uniqueItems: must be a boolean")

    if "pattern" in schema:
        pattern = schema["pattern"]
        if not isinstance(pattern, str):
            errors.append(f"{path}.pattern: must be a string")
        else:
            try:
                re.compile(pattern)
            except re.error as exc:
                errors.append(f"{path}.pattern: invalid regular expression: {exc}")

    for key in ("minimum", "maximum"):
        if key in schema and not _is_number(schema[key]):
            errors.append(f"{path}.{key}: must be a number")
    if (
        "minimum" in schema
        and "maximum" in schema
        and _is_number(schema["minimum"])
        and _is_number(schema["maximum"])
        and schema["minimum"] > schema["maximum"]
    ):
        errors.append(f"{path}: minimum exceeds maximum")


def check_schema(schema: Any) -> None:
    """Validate a schema document; raise SchemaError listing every problem."""

    errors: list[str] = []
    if not isinstance(schema, dict):
        raise SchemaError("$: schema must be an object")
    version = schema.get("version")
    if version != SUPPORTED_SCHEMA_VERSION:
        errors.append(
            f"$.version: unsupported schema version {version!r}; "
            f"supported is {SUPPORTED_SCHEMA_VERSION}"
        )
    body = {key: value for key, value in schema.items() if key != "version"}
    _check_schema_node(body, "$", errors)
    if errors:
        raise SchemaError("; ".join(errors))


def _type_matches(instance: Any, type_name: str) -> bool:
    if type_name == "object":
        return isinstance(instance, dict)
    if type_name == "array":
        return isinstance(instance, list)
    if type_name == "string":
        return isinstance(instance, str)
    if type_name == "integer":
        return _is_integer(instance)
    if type_name == "number":
        return _is_number(instance)
    if type_name == "boolean":
        return isinstance(instance, bool)
    if type_name == "null":
        return instance is None
    return False


def _type_label(declared: Any) -> str:
    if isinstance(declared, list):
        return " or ".join(declared)
    return str(declared)


def validate(instance: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Validate an instance against a schema; return deterministic errors."""

    errors: list[str] = []
    body = {key: value for key, value in schema.items() if key != "version"}

    if "type" in body:
        declared = body["type"]
        names = declared if isinstance(declared, list) else [declared]
        if not any(_type_matches(instance, name) for name in names):
            errors.append(
                f"{path}: expected {_type_label(declared)}, "
                f"got {type(instance).__name__}"
            )
            return errors

    if "const" in body:
        if strict_json.canonical_bytes(instance) != strict_json.canonical_bytes(
            body["const"]
        ):
            errors.append(f"{path}: must equal {body['const']!r}")

    if "enum" in body:
        options = {strict_json.canonical_bytes(option) for option in body["enum"]}
        if strict_json.canonical_bytes(instance) not in options:
            errors.append(f"{path}: value {instance!r} is not in the allowed set")

    if "oneOf" in body:
        matches = sum(
            1 for variant in body["oneOf"] if not validate(instance, variant, path)
        )
        if matches != 1:
            errors.append(
                f"{path}: must match exactly one oneOf variant, matched {matches}"
            )

    if isinstance(instance, dict):
        for key in body.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required key {key!r}")
        properties = body.get("properties", {})
        for key in sorted(properties):
            if key in instance:
                errors.extend(validate(instance[key], properties[key], f"{path}.{key}"))
        extra_keys = sorted(key for key in instance if key not in properties)
        extra_rule = body.get("additionalProperties", True)
        if extra_rule is False:
            for key in extra_keys:
                errors.append(f"{path}: unexpected key {key!r}")
        elif isinstance(extra_rule, dict):
            for key in extra_keys:
                errors.extend(validate(instance[key], extra_rule, f"{path}.{key}"))

    if isinstance(instance, list):
        if "minItems" in body and len(instance) < body["minItems"]:
            errors.append(
                f"{path}: array has {len(instance)} items, minimum is {body['minItems']}"
            )
        if "maxItems" in body and len(instance) > body["maxItems"]:
            errors.append(
                f"{path}: array has {len(instance)} items, maximum is {body['maxItems']}"
            )
        if body.get("uniqueItems"):
            seen: set[bytes] = set()
            for index, item in enumerate(instance):
                marker = strict_json.canonical_bytes(item)
                if marker in seen:
                    errors.append(f"{path}[{index}]: duplicate array item")
                seen.add(marker)
        if "items" in body:
            for index, item in enumerate(instance):
                errors.extend(validate(item, body["items"], f"{path}[{index}]"))

    if isinstance(instance, str):
        if "minLength" in body and len(instance) < body["minLength"]:
            errors.append(
                f"{path}: string shorter than {body['minLength']} characters"
            )
        if "maxLength" in body and len(instance) > body["maxLength"]:
            errors.append(
                f"{path}: string longer than {body['maxLength']} characters"
            )
        if "pattern" in body and not re.search(body["pattern"], instance):
            errors.append(f"{path}: string does not match pattern {body['pattern']!r}")

    if _is_number(instance) and not isinstance(instance, bool):
        if "minimum" in body and instance < body["minimum"]:
            errors.append(f"{path}: value {instance!r} below minimum {body['minimum']}")
        if "maximum" in body and instance > body["maximum"]:
            errors.append(f"{path}: value {instance!r} above maximum {body['maximum']}")

    return errors
