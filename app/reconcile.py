"""Nearest-reported-descendant reconciliation algorithm."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .hours import hour_to_unix


def load_tree(conn: sqlite3.Connection) -> tuple[dict[str, list[str]], dict[str, sqlite3.Row]]:
    rows = conn.execute(
        "SELECT meter_id, parent_id, name, sort_order FROM meters ORDER BY sort_order, meter_id"
    ).fetchall()
    meters = {row["meter_id"]: row for row in rows}
    children: dict[str, list[str]] = {meter_id: [] for meter_id in meters}
    for row in rows:
        if row["parent_id"] is not None:
            children.setdefault(row["parent_id"], []).append(row["meter_id"])
    return children, meters


def load_readings(conn: sqlite3.Connection, hour: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT meter_id, amount FROM readings WHERE hour = ?", (hour,)
    ).fetchall()
    return {row["meter_id"]: int(row["amount"]) for row in rows}


def frontier(
    meter_id: str,
    children: dict[str, list[str]],
    readings: dict[str, int],
) -> tuple[list[str], list[str]]:
    """Return nearest reported frontier IDs and uncovered leaf meter IDs.

    The search starts at each *direct* child. A reported meter stops descent on
    that branch. An unreported leaf is missing.
    """
    used: list[str] = []
    missing: list[str] = []

    def descend(node_id: str) -> None:
        if node_id in readings:
            used.append(node_id)
            return
        node_children = children.get(node_id, [])
        if not node_children:
            missing.append(node_id)
            return
        for child_id in node_children:
            descend(child_id)

    for child_id in children.get(meter_id, []):
        descend(child_id)

    # De-duplicate defensively; a valid tree cannot visit a node twice.
    return list(dict.fromkeys(used)), list(dict.fromkeys(missing))


def recompute_one(
    conn: sqlite3.Connection,
    meter_id: str,
    hour: str,
    *,
    children: dict[str, list[str]] | None = None,
    readings: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    """Recompute and atomically upsert/delete one stored conclusion.

    Returns None when the meter is not eligible (no current own report or no
    children). Any stale conclusion for an ineligible meter is removed.
    """
    meter = conn.execute("SELECT meter_id FROM meters WHERE meter_id = ?", (meter_id,)).fetchone()
    if meter is None:
        return None

    own = conn.execute(
        "SELECT amount FROM readings WHERE meter_id = ? AND hour = ?",
        (meter_id, hour),
    ).fetchone()

    if children is None:
        children, _ = load_tree(conn)
    if readings is None:
        readings = load_readings(conn, hour)

    if own is None or not children.get(meter_id):
        conn.execute(
            "DELETE FROM reconciliations WHERE hour = ? AND meter_id = ?",
            (hour, meter_id),
        )
        return None

    used_ids, missing_ids = frontier(meter_id, children, readings)
    used_readings = [
        {
            "meter_id": used_id,
            "meterId": used_id,
            "meter_no": used_id,
            "amount": readings[used_id],
            "wh": readings[used_id],
            "value": readings[used_id],
        }
        for used_id in used_ids
        if used_id in readings
    ]
    sum_used = sum(item["amount"] for item in used_readings)
    is_complete = not missing_ids
    own_amount = int(own["amount"])
    difference = own_amount - sum_used if is_complete else None

    conn.execute(
        """
        INSERT INTO reconciliations (
            hour, meter_id, own_amount, is_complete, difference, sum_used,
            used_meter_ids, used_readings_json, missing_meter_ids
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(hour, meter_id) DO UPDATE SET
            own_amount = excluded.own_amount,
            is_complete = excluded.is_complete,
            difference = excluded.difference,
            sum_used = excluded.sum_used,
            used_meter_ids = excluded.used_meter_ids,
            used_readings_json = excluded.used_readings_json,
            missing_meter_ids = excluded.missing_meter_ids,
            computed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'),
            version = reconciliations.version + 1
        """,
        (
            hour,
            meter_id,
            own_amount,
            1 if is_complete else 0,
            difference,
            sum_used,
            json.dumps(used_ids, ensure_ascii=False),
            json.dumps(used_readings, ensure_ascii=False),
            json.dumps(missing_ids, ensure_ascii=False),
        ),
    )
    row = conn.execute(
        "SELECT * FROM reconciliations WHERE hour = ? AND meter_id = ?",
        (hour, meter_id),
    ).fetchone()
    return serialize_conclusion(row)


def serialize_conclusion(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    used_ids = json.loads(data.pop("used_meter_ids", "[]"))
    used_readings = json.loads(data.pop("used_readings_json", "[]"))
    missing_ids = json.loads(data.pop("missing_meter_ids", "[]"))
    is_complete = bool(data.pop("is_complete"))
    difference = data.get("difference")
    own_amount = int(data["own_amount"])
    sum_used = int(data["sum_used"])

    result: dict[str, Any] = {
        "meter_id": data["meter_id"],
        "meterId": data["meter_id"],
        "meterNo": data["meter_id"],
        "hour": data["hour"],
        "status": "complete" if is_complete else "incomplete",
        "coverage_status": "complete" if is_complete else "missing_reports",
        "state": "complete" if is_complete else "missing_reports",
        "all_reported": is_complete,
        "has_all_reports": is_complete,
        "complete": is_complete,
        "is_complete": is_complete,
        "isComplete": is_complete,
        "ok": is_complete,
        "own_reading": own_amount,
        "ownReading": own_amount,
        "meter_reading": own_amount,
        "meterReading": own_amount,
        "reported_amount": own_amount,
        "reportedAmount": own_amount,
        "reading": own_amount,
        "amount": own_amount,
        "wh": own_amount,
        "value": own_amount,
        "sum_used": sum_used,
        "sumUsed": sum_used,
        "used_sum": sum_used,
        "usedSum": sum_used,
        "total_used": sum_used,
        "totalUsed": sum_used,
        "children_sum": sum_used,
        "child_sum": sum_used,
        "reported_submeter_sum": sum_used,
        "used_meter_ids": used_ids,
        "usedMeterIds": used_ids,
        "used_meters": used_ids,
        "usedMeters": used_ids,
        "used_meter_numbers": used_ids,
        "meter_ids": used_ids,
        "source_meter_ids": used_ids,
        "source_meters": used_ids,
        "source_ids": used_ids,
        "included_meter_ids": used_ids,
        "covered_by": used_ids,
        "used_readings": used_readings,
        "missing_meter_ids": missing_ids,
        "missingMeterIds": missing_ids,
        "missing_meters": missing_ids,
        "missingMeters": missing_ids,
        "missing_meter_numbers": missing_ids,
        "missing_leaf_ids": missing_ids,
        "missing_leaf_meter_ids": missing_ids,
        "missing_ids": missing_ids,
        "unreported_meter_ids": missing_ids,
        "pending_meter_ids": missing_ids,
        "difference": difference,
        "diff": difference,
        "delta": difference,
        "gap": difference,
        "discrepancy": difference,
        "balanced": bool(is_complete and difference == 0),
        "is_balanced": bool(is_complete and difference == 0),
        "isBalanced": bool(is_complete and difference == 0),
        "version": int(data["version"]),
        "computed_at": data["computed_at"],
        "computedAt": data["computed_at"],
        "hour_unix": hour_to_unix(data["hour"]),
        "hourUnix": hour_to_unix(data["hour"]),
    }
    return result
