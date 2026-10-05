"""Strict JSON loading and canonical serialization for claude-multi v2.

All trusted and user data is parsed through this module: duplicate keys,
trailing data, invalid UTF-8, non-finite numbers, and oversized structures are
rejected with deterministic errors. Canonical bytes (sorted keys, compact
separators, UTF-8, newline-terminated on disk) are the only hashed form.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import errors


class StrictJSONError(errors.ClaudeMultiError, ValueError):
    """Raised when JSON input violates strictness or safety limits."""


@dataclass(frozen=True)
class JSONLimits:
    """Configurable safety limits for parsed JSON structures."""

    max_bytes: int = 4 * 1024 * 1024
    max_depth: int = 64
    max_collection: int = 4096
    max_string: int = 65536


DEFAULT_LIMITS = JSONLimits()


def _reject_constant(value: str) -> Any:
    raise StrictJSONError(f"non-finite number literal {value!r} is forbidden")


def _parse_finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise StrictJSONError(f"non-finite number literal {value!r} is forbidden")
    return result


def _check_limits(node: Any, limits: JSONLimits, depth: int, path: str) -> None:
    if depth > limits.max_depth:
        raise StrictJSONError(f"{path}: nesting depth exceeds {limits.max_depth}")
    if isinstance(node, dict):
        if len(node) > limits.max_collection:
            raise StrictJSONError(
                f"{path}: object has {len(node)} keys, limit is {limits.max_collection}"
            )
        for key in sorted(node):
            _check_limits(node[key], limits, depth + 1, f"{path}.{key}")
    elif isinstance(node, list):
        if len(node) > limits.max_collection:
            raise StrictJSONError(
                f"{path}: array has {len(node)} items, limit is {limits.max_collection}"
            )
        for index, item in enumerate(node):
            _check_limits(item, limits, depth + 1, f"{path}[{index}]")
    elif isinstance(node, str):
        if len(node) > limits.max_string:
            raise StrictJSONError(
                f"{path}: string length {len(node)} exceeds {limits.max_string}"
            )


def loads(data: bytes | str, limits: JSONLimits | None = None) -> Any:
    """Parse JSON with duplicate-key, trailing-data, and safety enforcement."""

    limits = limits or DEFAULT_LIMITS
    if isinstance(data, bytes):
        if len(data) > limits.max_bytes:
            raise StrictJSONError(
                f"$: input is {len(data)} bytes, limit is {limits.max_bytes}"
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StrictJSONError(f"$: invalid UTF-8 at byte {exc.start}") from exc
    else:
        encoded = data.encode("utf-8")
        if len(encoded) > limits.max_bytes:
            raise StrictJSONError(
                f"$: input is {len(encoded)} bytes, limit is {limits.max_bytes}"
            )
        text = data

    def _object_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise StrictJSONError(f"duplicate key {key!r}")
            result[key] = value
        return result

    decoder = json.JSONDecoder(
        object_pairs_hook=_object_hook,
        parse_constant=_reject_constant,
        parse_float=_parse_finite_float,
    )
    try:
        document, end = decoder.raw_decode(text)
    except StrictJSONError:
        raise
    except json.JSONDecodeError as exc:
        raise StrictJSONError(
            f"$: invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}"
        ) from exc
    trailing = text[end:]
    if trailing.strip():
        raise StrictJSONError(f"$: trailing data after JSON document at char {end}")
    _check_limits(document, limits, 1, "$")
    return document


def load(path: Path | str, limits: JSONLimits | None = None) -> Any:
    """Load a JSON file strictly. The path must be a regular file."""

    limits = limits or DEFAULT_LIMITS
    candidate = Path(path)
    if not candidate.is_file():
        raise StrictJSONError(f"{candidate}: not a regular file")
    size = candidate.stat().st_size
    if size > limits.max_bytes:
        raise StrictJSONError(
            f"{candidate}: file is {size} bytes, limit is {limits.max_bytes}"
        )
    return loads(candidate.read_bytes(), limits)


def canonical_bytes(document: Any) -> bytes:
    """Deterministic canonical form: sorted keys, compact separators, UTF-8."""

    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_file_bytes(document: Any) -> bytes:
    """Canonical bytes as stored on disk: newline-terminated."""

    return canonical_bytes(document) + b"\n"


def pretty_file_bytes(document: Any) -> bytes:
    """Human-editable JSON for user-facing documents (compositions).

    Indent-2 with sorted keys (stable, diff-friendly) and a trailing
    newline. Hashes never depend on file formatting — digests use
    :func:`canonical_bytes` — so this is safe for every store-write of a
    document a user may open in an editor.
    """

    return (
        json.dumps(
            document,
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """Lowercase hex SHA-256 of the given bytes."""

    return hashlib.sha256(data).hexdigest()


def bundle_digest(document: Any) -> str:
    """Prefixed SHA-256 digest over the canonical bytes of a document."""

    return "sha256:" + sha256_hex(canonical_bytes(document))
