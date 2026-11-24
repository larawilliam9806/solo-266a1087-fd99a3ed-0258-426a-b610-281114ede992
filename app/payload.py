"""Helpers for accepting small request-shape variations without weakening rules."""
from __future__ import annotations

from typing import Any, Iterable

from .errors import ValidationProblem

METER_ID_KEYS = ("meter_id", "meterId", "meter_no", "meterNo", "meter_number", "meterNumber", "code", "meter", "id")
PARENT_ID_KEYS = ("parent_id", "parentId", "parent_meter_id", "parent", "parent_code")
NAME_KEYS = ("name", "meter_name", "meterName", "label", "description")
AMOUNT_KEYS = ("amount", "value", "wh", "watt_hours", "wattHours", "energy_wh", "energyWh", "energy", "reading_value", "readingAmount", "reading_amount", "count")
HOUR_KEYS = ("hour", "hour_utc", "hourUtc", "utc_hour", "hour_start", "hourStart", "hour_ending", "hourEnding", "timestamp", "ts", "time", "datetime", "period")
CHILDREN_KEYS = ("children", "child_meters", "childMeters", "submeters", "sub_meters", "nodes")


def first_present(data: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in data and data[key] is not None and data[key] != "":
            return data[key]
    return None


def meter_id_from(value: Any) -> str:
    if value is None or isinstance(value, bool):
        raise ValidationProblem("invalid_meter_id", "meter_id must be a non-empty string or integer.")
    text = str(value).strip()
    if not text:
        raise ValidationProblem("invalid_meter_id", "meter_id must be non-empty.")
    if len(text) > 128:
        raise ValidationProblem("invalid_meter_id", "meter_id must be at most 128 characters.")
    return text


def amount_from(value: Any) -> int:
    if value is None:
        raise ValidationProblem("amount_required", "A non-negative integer amount in Wh is required.")
    if isinstance(value, bool):
        raise ValidationProblem("invalid_amount", "Amount must be a non-negative integer number of Wh.")
    if isinstance(value, int):
        amount = value
    elif isinstance(value, float) and value.is_integer():
        amount = int(value)
    elif isinstance(value, str) and value.strip().lstrip("+").isdigit():
        amount = int(value.strip())
    else:
        raise ValidationProblem("invalid_amount", "Amount must be a non-negative integer number of Wh.")
    if amount < 0:
        raise ValidationProblem("negative_amount", "Amount must be non-negative.")
    return amount


def extract_reading_fields(data: dict[str, Any] | None = None, **fallbacks: Any) -> tuple[str, str, int | None]:
    merged: dict[str, Any] = {}
    for key, value in fallbacks.items():
        if value is not None:
            merged[key] = value
    if data:
        merged.update({k: v for k, v in data.items() if v is not None})

    meter_raw = first_present(merged, METER_ID_KEYS)
    if isinstance(meter_raw, dict):
        meter_raw = first_present(meter_raw, METER_ID_KEYS)
    elif meter_raw is None and isinstance(merged.get("meter"), dict):
        meter_raw = first_present(merged["meter"], METER_ID_KEYS)
    meter_id = meter_id_from(meter_raw)

    hour_raw = first_present(merged, HOUR_KEYS)
    hour = hour_raw  # parse in service so the helper remains independent

    amount_raw = first_present(merged, AMOUNT_KEYS)
    amount = None if amount_raw is None else amount_from(amount_raw)
    return meter_id, hour, amount


def flatten_meter_payload(payload: Any) -> list[dict[str, Any]]:
    """Accept one meter, a raw list, an envelope, or a nested tree."""
    raw_nodes: list[Any]

    if isinstance(payload, list):
        raw_nodes = payload
    elif isinstance(payload, dict):
        for envelope_key in ("meters", "nodes", "data", "items"):
            if isinstance(payload.get(envelope_key), list):
                raw_nodes = payload[envelope_key]
                break
        else:
            tree = payload.get("tree")
            if isinstance(tree, (dict, list)):
                raw_nodes = tree if isinstance(tree, list) else [tree]
            else:
                raw_nodes = [payload]
    else:
        raise ValidationProblem("invalid_body", "Request body must be a meter object, list, or tree envelope.")

    result: list[dict[str, Any]] = []

    def visit(raw: Any, inherited_parent: str | None, depth_prefix: tuple[int, ...]) -> None:
        if not isinstance(raw, dict):
            raise ValidationProblem("invalid_meter", "Each meter must be a JSON object.")
        mid = meter_id_from(first_present(raw, METER_ID_KEYS))
        parent_raw = first_present(raw, PARENT_ID_KEYS)
        parent_specified = parent_raw is not None or inherited_parent is not None
        parent = meter_id_from(parent_raw) if parent_raw is not None else inherited_parent
        name_raw = first_present(raw, NAME_KEYS)
        name = str(name_raw) if name_raw is not None else None
        order_raw = raw.get("sort_order", raw.get("sortOrder", depth_prefix[-1] if depth_prefix else 0))
        try:
            sort_order = int(order_raw)
        except (TypeError, ValueError):
            sort_order = depth_prefix[-1] if depth_prefix else 0

        result.append(
            {
                "meter_id": mid,
                "parent_id": parent,
                "parent_specified": parent_specified,
                "name": name,
                "sort_order": sort_order,
            }
        )

        children = first_present(raw, CHILDREN_KEYS)
        if children is None:
            children = []
        if not isinstance(children, list):
            raise ValidationProblem("invalid_meter", "children must be a list.")
        for index, child in enumerate(children):
            if isinstance(child, (str, int)) and not isinstance(child, bool):
                child = {"meter_id": str(child)}
            visit(child, mid, depth_prefix + (index,))

    for index, node in enumerate(raw_nodes):
        visit(node, None, (index,))

    return result
