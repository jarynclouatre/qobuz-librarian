"""Shared validation for persisted recovery carriers."""

import json
import re

_OWNER_FIELDS = frozenset({"operation_id", "item_id"})
_HEX_ID = re.compile(r"[0-9a-f]{64}\Z")
_MAX_RECOVERY_JSON_BYTES = 1024 * 1024
_MAX_RECOVERY_JSON_DEPTH = 64


def _strict_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"recovery JSON contains duplicate key {key!r}")
        value[key] = item
    return value


def _reject_json_constant(value):
    raise ValueError(f"recovery JSON contains invalid constant {value}")


def decode_recovery_json(
        raw, *, max_bytes=_MAX_RECOVERY_JSON_BYTES,
        max_depth=_MAX_RECOVERY_JSON_DEPTH):
    """Decode bounded JSON while rejecting duplicate keys at every level."""
    try:
        if type(raw) is bytes:
            if len(raw) > max_bytes:
                raise ValueError("recovery JSON exceeds size limit")
            text = raw.decode("utf-8")
        elif type(raw) is str:
            if len(raw) > max_bytes or len(raw.encode("utf-8")) > max_bytes:
                raise ValueError("recovery JSON exceeds size limit")
            text = raw
        else:
            raise ValueError("recovery JSON must be text or bytes")
        value = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, UnicodeError) as exc:
        raise ValueError("invalid recovery JSON") from exc

    pending = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        if depth > max_depth:
            raise ValueError("recovery JSON nesting exceeds limit")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return value


def is_hex64(value) -> bool:
    return isinstance(value, str) and _HEX_ID.fullmatch(value) is not None


def normalise_recovery_owner(value):
    """Return an isolated strict owner record, or raise for malformed input."""
    if value is None:
        return None
    if type(value) is not dict or set(value) != _OWNER_FIELDS:
        raise ValueError("recovery owner must contain operation_id and item_id")
    if any(
        type(value[field]) is not str or not is_hex64(value[field])
        for field in _OWNER_FIELDS
    ):
        raise ValueError("recovery owner IDs must be 64 lowercase hex characters")
    return {
        "operation_id": value["operation_id"],
        "item_id": value["item_id"],
    }


def recovery_owner_matches(value, expected):
    """Compare two strict, non-null owner records without trusting malformed data."""
    try:
        value = normalise_recovery_owner(value)
        expected = normalise_recovery_owner(expected)
    except ValueError:
        return False
    return value is not None and expected is not None and value == expected
