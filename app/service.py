"""Ledger use cases: registration, mutations, incremental checks and queries."""
from __future__ import annotations

import sqlite3
from typing import Any

from .db import read_connection, write_transaction
from .errors import ConflictProblem, NotFoundProblem, ValidationProblem
from .hours import parse_hour
from .payload import flatten_meter_payload
from .reconcile import load_readings, load_tree, recompute_one, serialize_conclusion


def _meter_exists(conn: sqlite3.Connection, meter_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM meters WHERE meter_id = ?", (meter_id,)).fetchone()


def _parent_map(conn: sqlite3.Connection) -> dict[str, str | None]:
    rows = conn.execute("SELECT meter_id, parent_id FROM meters").fetchall()
    return {row["meter_id"]: row["parent_id"] for row in rows}


def _self_and_ancestors(conn: sqlite3.Connection, meter_id: str) -> list[str]:
    parents = _parent_map(conn)
    chain: list[str] = []
    current: str | None = meter_id
    seen: set[str] = set()
    while current is not None:
        if current in seen:
            raise ConflictProblem("meter_cycle", "Meter parent relationships contain a cycle.")
        seen.add(current)
        chain.append(current)
        current = parents.get(current)
    return chain


def register_meters(payload: Any, *, replace: bool = False) -> dict[str, Any]:
    meters = flatten_meter_payload(payload)
    payload_ids = [item["meter_id"] for item in meters]
    if len(payload_ids) != len(set(payload_ids)):
        raise ConflictProblem("duplicate_meter", "A meter_id appears more than once in the registration payload.")

    with write_transaction() as conn:
        if replace:
            conn.execute("DELETE FROM reconciliations")
            conn.execute("DELETE FROM reading_events")
            conn.execute("DELETE FROM readings")
            conn.execute("DELETE FROM meters")

        existing_rows = conn.execute("SELECT meter_id, parent_id FROM meters").fetchall()
        existing = {row["meter_id"]: row["parent_id"] for row in existing_rows}
        all_parents = dict(existing)

        for item in meters:
            mid = item["meter_id"]
            parent = item["parent_id"]
            if parent is not None and parent == mid:
                raise ValidationProblem("meter_cycle", f"Meter {mid!r} cannot be its own parent.")
            if parent is not None and parent not in payload_ids and parent not in existing:
                raise ValidationProblem(
                    "parent_not_found",
                    f"Parent meter {parent!r} must be registered before or in the same request.",
                    {"meter_id": mid, "parent_id": parent},
                )
            if mid in existing and item["parent_specified"] and existing[mid] != parent:
                raise ConflictProblem(
                    "fixed_tree_conflict",
                    f"Meter {mid!r} is fixed with parent {existing[mid]!r}; parent cannot change.",
                    {"meter_id": mid, "existing_parent_id": existing[mid], "requested_parent_id": parent},
                )
            all_parents[mid] = parent

        # Cycle detection over the combined graph.
        for start in all_parents:
            seen: list[str] = []
            current: str | None = start
            while current is not None:
                if current in seen:
                    raise ValidationProblem("meter_cycle", "Meter tree cannot contain a cycle.")
                seen.append(current)
                current = all_parents.get(current)

        for item in meters:
            conn.execute(
                """
                INSERT INTO meters (meter_id, parent_id, name, sort_order)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(meter_id) DO UPDATE SET
                    parent_id = CASE
                        WHEN ? THEN excluded.parent_id
                        ELSE meters.parent_id
                    END,
                    name = COALESCE(excluded.name, meters.name),
                    sort_order = excluded.sort_order
                """,
                (
                    item["meter_id"],
                    item["parent_id"],
                    item["name"],
                    item["sort_order"],
                    1 if item["parent_specified"] else 0,
                ),
            )

    return list_meters_tree()


def list_meters() -> list[dict[str, Any]]:
    with read_connection() as conn:
        rows = conn.execute(
            "SELECT meter_id, parent_id, name, sort_order, created_at "
            "FROM meters ORDER BY sort_order, meter_id"
        ).fetchall()
        meters = [dict(row) for row in rows]
        for meter in meters:
            meter["meterId"] = meter["meter_id"]
            meter["parentId"] = meter["parent_id"]
            meter["sortOrder"] = meter["sort_order"]
            meter["createdAt"] = meter["created_at"]
        return meters


def list_meters_tree(hour: str | None = None) -> dict[str, Any]:
    canonical_hour = parse_hour(hour) if hour else None
    with read_connection() as conn:
        rows = conn.execute(
            "SELECT meter_id, parent_id, name, sort_order, created_at "
            "FROM meters ORDER BY sort_order, meter_id"
        ).fetchall()
        conclusion_rows: dict[str, sqlite3.Row] = {}
        if canonical_hour:
            for row in conn.execute(
                "SELECT * FROM reconciliations WHERE hour = ?", (canonical_hour,)
            ):
                conclusion_rows[row["meter_id"]] = row

    nodes: dict[str, dict[str, Any]] = {}
    for row in rows:
        mid = row["meter_id"]
        nodes[mid] = {
            "meter_id": mid,
            "meterId": mid,
            "parent_id": row["parent_id"],
            "parentId": row["parent_id"],
            "name": row["name"],
            "sort_order": row["sort_order"],
            "sortOrder": row["sort_order"],
            "children": [],
        }
        if canonical_hour and mid in conclusion_rows:
            nodes[mid]["conclusion"] = serialize_conclusion(conclusion_rows[mid])
        elif canonical_hour:
            nodes[mid]["conclusion"] = None

    roots: list[dict[str, Any]] = []
    for mid, node in nodes.items():
        parent = node["parent_id"]
        if parent and parent in nodes:
            nodes[parent]["children"].append(node)
        else:
            roots.append(node)

    def sort_children(items: list[dict[str, Any]]) -> None:
        items.sort(key=lambda item: (item.get("sort_order") or 0, item["meter_id"]))
        for item in items:
            sort_children(item["children"])

    sort_children(roots)
    return {"hour": canonical_hour, "meters": list(nodes.values()), "tree": roots}


def _reading_dict(conn: sqlite3.Connection, meter_id: str, hour: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT meter_id, hour, amount, version, created_at, updated_at "
        "FROM readings WHERE meter_id = ? AND hour = ?",
        (meter_id, hour),
    ).fetchone()
    return dict(row) if row else None


def apply_reading(
    *,
    action: str,
    meter_id: str,
    hour_value: Any,
    amount: int | None = None,
    allow_create: bool = False,
    idempotent_withdraw: bool = False,
) -> dict[str, Any]:
    hour = parse_hour(hour_value)

    with write_transaction() as conn:
        if _meter_exists(conn, meter_id) is None:
            raise NotFoundProblem(
                "meter_not_found",
                f"Meter {meter_id!r} is not registered.",
                {"meter_id": meter_id},
            )

        existing = _reading_dict(conn, meter_id, hour)

        if action == "report":
            if existing is not None:
                raise ConflictProblem(
                    "reading_exists",
                    "A reading already exists; use correction to change it.",
                    {"meter_id": meter_id, "hour": hour, "current": existing},
                )
            if amount is None:
                raise ValidationProblem("amount_required", "Amount is required for a report.")
            conn.execute(
                "INSERT INTO readings (meter_id, hour, amount) VALUES (?, ?, ?)",
                (meter_id, hour, amount),
            )
            response_action = "reported"

        elif action in {"correct", "upsert"}:
            if amount is None:
                raise ValidationProblem("amount_required", "Amount is required for a correction.")
            if existing is None and action == "correct" and not allow_create:
                raise NotFoundProblem(
                    "reading_not_found",
                    "No active reading to correct; report it first.",
                    {"meter_id": meter_id, "hour": hour},
                )
            conn.execute(
                """
                INSERT INTO readings (meter_id, hour, amount, version)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(meter_id, hour) DO UPDATE SET
                    amount = excluded.amount,
                    version = readings.version + 1,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                """,
                (meter_id, hour, amount),
            )
            response_action = "corrected" if existing is not None else "reported"

        elif action == "withdraw":
            if existing is None:
                if idempotent_withdraw:
                    response_action = "already_withdrawn"
                else:
                    raise NotFoundProblem(
                        "reading_not_found",
                        "No active reading to withdraw.",
                        {"meter_id": meter_id, "hour": hour},
                    )
            else:
                conn.execute(
                    "DELETE FROM readings WHERE meter_id = ? AND hour = ?",
                    (meter_id, hour),
                )
                response_action = "withdrawn"
        else:
            raise ValidationProblem("unknown_action", f"Unknown reading action {action!r}.")

        # Append an audit event in the same transaction as the current ledger
        # value and the recomputed conclusions.
        after_write = _reading_dict(conn, meter_id, hour)
        if response_action in {"reported", "corrected"} and after_write is not None:
            event_action = "report" if response_action == "reported" else "correct"
            conn.execute(
                """
                INSERT INTO reading_events (meter_id, hour, action, amount, version)
                VALUES (?, ?, ?, ?, ?)
                """,
                (meter_id, hour, event_action, after_write["amount"], after_write["version"]),
            )
        elif response_action == "withdrawn" and existing is not None:
            conn.execute(
                """
                INSERT INTO reading_events (meter_id, hour, action, amount, version)
                VALUES (?, ?, 'withdraw', ?, ?)
                """,
                (meter_id, hour, existing["amount"], existing["version"]),
            )

        # One snapshot of this hour/tree inside the serial transaction gives a
        # consistent recomputation. Only self + ancestors are written.
        children, _ = load_tree(conn)
        readings = load_readings(conn, hour)
        affected_ids = _self_and_ancestors(conn, meter_id)
        conclusions: list[dict[str, Any]] = []
        for affected_id in affected_ids:  # bottom-up order
            conclusion = recompute_one(
                conn,
                affected_id,
                hour,
                children=children,
                readings=readings,
            )
            if conclusion is not None:
                conclusions.append(conclusion)

        reading = _reading_dict(conn, meter_id, hour)
        response: dict[str, Any] = {
            "ok": True,
            "success": True,
            "action": response_action,
            "meter_id": meter_id,
            "meterId": meter_id,
            "hour": hour,
            "amount": reading["amount"] if reading else None,
            "wh": reading["amount"] if reading else None,
            "value": reading["amount"] if reading else None,
            "version": reading["version"] if reading else 0,
            "withdrawn": reading is None,
            "affected_meter_ids": affected_ids,
            "affected_meter_ids_top_down": list(reversed(affected_ids)),
            "checked_meter_ids": affected_ids,
            "recomputed_meter_ids": [item["meter_id"] for item in conclusions],
            "reconciliations": conclusions,
            "conclusions": conclusions,
        }
        if reading:
            response["created_at"] = reading["created_at"]
            response["updated_at"] = reading["updated_at"]
        return response


def list_reading_events(hour_value: Any, meter_id: str | None = None) -> list[dict[str, Any]]:
    hour = parse_hour(hour_value)
    query = "SELECT * FROM reading_events WHERE hour = ?"
    params: list[Any] = [hour]
    if meter_id:
        query += " AND meter_id = ?"
        params.append(meter_id)
    query += " ORDER BY event_id"
    with read_connection() as conn:
        return [dict(row) for row in conn.execute(query, params).fetchall()]


def list_conclusions(hour_value: Any) -> dict[str, Any]:
    hour = parse_hour(hour_value)
    with read_connection() as conn:
        rows = conn.execute(
            """
            SELECT r.*
            FROM reconciliations r
            JOIN meters m ON m.meter_id = r.meter_id
            WHERE r.hour = ?
            ORDER BY m.sort_order, m.meter_id
            """,
            (hour,),
        ).fetchall()
        results = [serialize_conclusion(row) for row in rows]
    return {"hour": hour, "count": len(results), "results": results, "conclusions": results}


def list_conclusion_array(hour_value: Any) -> list[dict[str, Any]]:
    return list_conclusions(hour_value)["results"]


def get_conclusion(meter_id: str, hour_value: Any) -> dict[str, Any]:
    hour = parse_hour(hour_value)
    with read_connection() as conn:
        meter = _meter_exists(conn, meter_id)
        if meter is None:
            raise NotFoundProblem("meter_not_found", f"Meter {meter_id!r} is not registered.")
        row = conn.execute(
            "SELECT * FROM reconciliations WHERE meter_id = ? AND hour = ?",
            (meter_id, hour),
        ).fetchone()
    if row is None:
        raise NotFoundProblem(
            "conclusion_not_found",
            "No stored conclusion for this meter and hour. It may have no report or no submeters.",
            {"meter_id": meter_id, "hour": hour},
        )
    return serialize_conclusion(row)


def recompute_all_for_hour(hour_value: Any) -> dict[str, Any]:
    """Maintenance/recovery endpoint; normal writes already recompute affected chains."""
    hour = parse_hour(hour_value)
    with write_transaction() as conn:
        children, meters = load_tree(conn)
        readings = load_readings(conn, hour)
        stored: list[dict[str, Any]] = []
        for meter_id in meters:
            result = recompute_one(
                conn, meter_id, hour, children=children, readings=readings
            )
            if result is not None:
                stored.append(result)
    return {"hour": hour, "count": len(stored), "results": stored, "conclusions": stored}
