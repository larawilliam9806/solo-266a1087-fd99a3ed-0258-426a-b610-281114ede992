"""Ledger use cases: registration, mutations, incremental checks and queries."""
from __future__ import annotations

import sqlite3
from typing import Any

from .db import read_connection, write_transaction
from .errors import ConflictProblem, NotFoundProblem, ValidationProblem
from .hours import parse_hour
from .payload import flatten_meter_payload
from .reconcile import (
    load_readings,
    load_review_statuses,
    load_tree,
    recompute_one,
    serialize_conclusion,
)

# Reviewer-facing decision aliases. ``pending`` means the value is held out of
# the park account until a later approval.
_DECISION_ALIASES = {
    "approved": "approved",
    "approve": "approved",
    "pass": "approved",
    "passed": "approved",
    "accept": "approved",
    "accepted": "approved",
    "admit": "approved",
    "ok": "approved",
    "pending": "pending",
    "suspend": "pending",
    "suspended": "pending",
    "hold": "pending",
    "held": "pending",
    "defer": "pending",
    "deferred": "pending",
    "reject": "pending",
    "rejected": "pending",
    "fail": "pending",
    "failed": "pending",
}
_STATUS_LABELS = {
    "auto_approved": "auto_approved",
    "approved": "approved",
    "pending": "pending",
}


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
            conn.execute("DELETE FROM reading_review_events")
            conn.execute("DELETE FROM reading_reviews")
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
        "SELECT meter_id, hour, amount, version, revision, created_at, updated_at "
        "FROM readings WHERE meter_id = ? AND hour = ?",
        (meter_id, hour),
    ).fetchone()
    return dict(row) if row else None


def _review_dict(conn: sqlite3.Connection, meter_id: str, hour: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM reading_reviews WHERE meter_id = ? AND hour = ?",
        (meter_id, hour),
    ).fetchone()
    return dict(row) if row else None


def serialize_review(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    status = data["status"]
    result: dict[str, Any] = {
        "meter_id": data["meter_id"],
        "meterId": data["meter_id"],
        "meter_no": data["meter_id"],
        "hour": data["hour"],
        "status": _STATUS_LABELS.get(status, status),
        "review_status": _STATUS_LABELS.get(status, status),
        "quality_status": _STATUS_LABELS.get(status, status),
        "decision": status,
        # Auto-approved readings and human approvals both enter the park
        # account; only pending readings are held out.
        "admitted": status != "pending",
        "enters_ledger": status != "pending",
        "in_ledger": status != "pending",
        "suspended": status == "pending",
        "held": status == "pending",
        "needs_review": status == "pending",
        "reading_version": int(data["reading_version"]),
        "readingVersion": int(data["reading_version"]),
        "version": int(data["reading_version"]),
        "reading_revision": int(data["reading_revision"]),
        "readingRevision": int(data["reading_revision"]),
        "revision": int(data["reading_revision"]),
        "reason": data.get("reason"),
        "reviewer": data.get("reviewer"),
        "reviewer_id": data.get("reviewer"),
        "decision_count": int(data["decision_count"]),
        "decisionCount": int(data["decision_count"]),
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
        "reviewed_at": data["updated_at"],
        "createdAt": data["created_at"],
        "updatedAt": data["updated_at"],
        "reviewedAt": data["updated_at"],
    }
    return result


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
        existing_review = _review_dict(conn, meter_id, hour)

        if action == "report":
            if existing is not None:
                raise ConflictProblem(
                    "reading_exists",
                    "A reading already exists; use correction to change it.",
                    {"meter_id": meter_id, "hour": hour, "current": existing},
                )
            if amount is None:
                raise ValidationProblem("amount_required", "Amount is required for a report.")
            # A re-report after withdrawal must not reuse the old revision: a
            # review submitted against the withdrawn incarnation has to
            # conflict instead of silently attaching to the new value.
            next_revision = (
                int(existing_review["reading_revision"]) + 1 if existing_review is not None else 1
            )
            conn.execute(
                "INSERT INTO readings (meter_id, hour, amount, version, revision) "
                "VALUES (?, ?, ?, 1, ?)",
                (meter_id, hour, amount, next_revision),
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
            next_revision = (
                int(existing["revision"]) + 1
                if existing is not None
                else (int(existing_review["reading_revision"]) + 1 if existing_review is not None else 1)
            )
            conn.execute(
                """
                INSERT INTO readings (meter_id, hour, amount, version, revision)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(meter_id, hour) DO UPDATE SET
                    amount = excluded.amount,
                    version = readings.version + 1,
                    revision = excluded.revision,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                """,
                (meter_id, hour, amount, next_revision),
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

        # Maintain the quality-review gate in the same transaction as the
        # ledger value. New values default to admitted; any new incarnation
        # (correction / re-report / withdraw) resets a prior decision so the
        # value must pass quality review again before it can enter the account.
        after_write = _reading_dict(conn, meter_id, hour)
        if after_write is not None:
            if response_action == "reported" and existing_review is None:
                # First-ever report: existing readings default to pass.
                conn.execute(
                    """
                    INSERT INTO reading_reviews (
                        meter_id, hour, reading_version, reading_revision,
                        status, reason, decision_count
                    ) VALUES (?, ?, ?, ?, 'auto_approved', NULL, 0)
                    """,
                    (meter_id, hour, after_write["version"], after_write["revision"]),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO reading_reviews (
                        meter_id, hour, reading_version, reading_revision,
                        status, reason, reviewer, decision_count, created_at
                    ) VALUES (?, ?, ?, ?, 'pending', NULL, NULL, 0,
                              strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                    ON CONFLICT(meter_id, hour) DO UPDATE SET
                        reading_version = excluded.reading_version,
                        reading_revision = excluded.reading_revision,
                        status = 'pending',
                        reason = NULL,
                        reviewer = NULL,
                        decision_count = 0,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                    """,
                    (meter_id, hour, after_write["version"], after_write["revision"]),
                )
        elif response_action == "withdrawn":
            # Keep the gate row so a stale review still conflicts; the
            # withdrawn incarnation is marked pending re-review.
            conn.execute(
                """
                UPDATE reading_reviews
                SET status = 'pending',
                    reason = NULL,
                    reviewer = NULL,
                    decision_count = 0,
                    reading_version = ?,
                    reading_revision = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE meter_id = ? AND hour = ?
                """,
                (existing["version"], existing["revision"], meter_id, hour),
            )

        # Append an audit event in the same transaction as the current ledger
        # value and the recomputed conclusions.
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
        statuses = load_review_statuses(conn, hour)
        affected_ids = _self_and_ancestors(conn, meter_id)
        conclusions: list[dict[str, Any]] = []
        for affected_id in affected_ids:  # bottom-up order
            conclusion = recompute_one(
                conn,
                affected_id,
                hour,
                children=children,
                readings=readings,
                statuses=statuses,
            )
            if conclusion is not None:
                conclusions.append(conclusion)

        reading = _reading_dict(conn, meter_id, hour)
        review = _review_dict(conn, meter_id, hour)
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
            "revision": reading["revision"] if reading else (existing["revision"] if existing else 0),
            "withdrawn": reading is None,
            "review": serialize_review(review) if review else None,
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


# ----------------------------- Quality review ------------------------------

def _normalize_decision(value: Any) -> str:
    if value is None:
        raise ValidationProblem(
            "decision_required",
            "A review decision is required: 'approved' or 'pending' (suspend).",
        )
    key = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    decision = _DECISION_ALIASES.get(key)
    if decision is None:
        raise ValidationProblem(
            "invalid_decision",
            "Decision must be 'approved' or 'pending' (suspend/hold).",
            {"received": value},
        )
    return decision


def _normalize_reason(value: Any, decision: str) -> str | None:
    if value is None:
        text = None
    else:
        text = str(value).strip()
        if not text:
            text = None
    if text is not None and len(text) > 1000:
        raise ValidationProblem("reason_too_long", "Reason must be at most 1000 characters.")
    if decision == "pending" and not text:
        raise ValidationProblem(
            "reason_required",
            "A non-empty reason is required when suspending (holding) a reading.",
        )
    return text


def submit_reading_review(
    *,
    meter_id: str,
    hour_value: Any,
    observed_version: Any,
    decision_value: Any,
    reason_value: Any = None,
    reviewer: str | None = None,
    observed_revision: Any = None,
) -> dict[str, Any]:
    """Review one reading version for one UTC hour.

    The reviewer must state the reading ``version`` (and, because a withdraw
    followed by a re-report reuses version 1, the ``revision`` incarnation
    token) they actually saw. A mismatch with the current incarnation
    (correction, withdrawal, re-report) returns 409 and preserves the stored
    state. The review row, the audit event and every affected reconciliation
    commit in one transaction.
    """
    hour = parse_hour(hour_value)
    decision = _normalize_decision(decision_value)
    reason = _normalize_reason(reason_value, decision)

    if observed_version is None or isinstance(observed_version, bool):
        raise ValidationProblem(
            "reading_version_required",
            "observed reading version (the version of the reading under review) is required.",
        )
    try:
        observed = int(observed_version)
    except (TypeError, ValueError) as exc:
        raise ValidationProblem(
            "invalid_reading_version", "Reading version must be a positive integer."
        ) from exc
    if observed <= 0:
        raise ValidationProblem(
            "invalid_reading_version", "Reading version must be a positive integer."
        )

    # Version alone is not unique across incarnations: withdraw + re-report
    # starts again at version 1. ``revision`` is the monotonic incarnation
    # token returned by every reading response. Defaulting to 1 keeps simple
    # first-version clients working while still rejecting stale tokens.
    if observed_revision is None or isinstance(observed_revision, bool):
        observed_rev = 1
    else:
        try:
            observed_rev = int(observed_revision)
        except (TypeError, ValueError) as exc:
            raise ValidationProblem(
                "invalid_reading_revision", "Reading revision must be a positive integer."
            ) from exc
        if observed_rev <= 0:
            raise ValidationProblem(
                "invalid_reading_revision", "Reading revision must be a positive integer."
            )

    reviewer_text = None
    if reviewer is not None and str(reviewer).strip():
        reviewer_text = str(reviewer).strip()[:128]

    with write_transaction() as conn:
        if _meter_exists(conn, meter_id) is None:
            raise NotFoundProblem(
                "meter_not_found",
                f"Meter {meter_id!r} is not registered.",
                {"meter_id": meter_id},
            )

        reading = _reading_dict(conn, meter_id, hour)
        review = _review_dict(conn, meter_id, hour)
        if reading is None or review is None:
            # Either never reported, or withdrawn. The current incarnation is
            # not reviewable; keep whatever state exists untouched.
            raise NotFoundProblem(
                "reading_not_found",
                "No active reading to review for this meter and hour; it may have been withdrawn.",
                {
                    "meter_id": meter_id,
                    "hour": hour,
                    "withdrawn": reading is None and review is not None,
                    "current_status": review["status"] if review else None,
                },
            )

        if observed != int(reading["version"]) or observed_rev != int(reading["revision"]):
            raise ConflictProblem(
                "reading_version_conflict",
                "The reviewed reading version is stale; the reading was corrected, withdrawn or re-reported. "
                "Fetch the current version/revision and review again.",
                {
                    "meter_id": meter_id,
                    "hour": hour,
                    "submitted_version": observed,
                    "submitted_revision": observed_rev,
                    "current_version": int(reading["version"]),
                    "current_revision": int(reading["revision"]),
                    "current_status": review["status"],
                },
            )

        new_status = decision  # 'approved' or 'pending'
        conn.execute(
            """
            UPDATE reading_reviews
            SET status = ?,
                reason = ?,
                reviewer = COALESCE(?, reviewer),
                reading_version = ?,
                reading_revision = ?,
                decision_count = decision_count + 1,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE meter_id = ? AND hour = ?
            """,
            (
                new_status,
                reason,
                reviewer_text,
                reading["version"],
                reading["revision"],
                meter_id,
                hour,
            ),
        )
        conn.execute(
            """
            INSERT INTO reading_review_events (
                meter_id, hour, reading_version, reading_revision,
                decision, reason, reviewer
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                meter_id,
                hour,
                reading["version"],
                reading["revision"],
                decision,
                reason if reason is not None else "",
                reviewer_text,
            ),
        )

        # Recompute the meter and its ancestors inside the same transaction.
        children, _ = load_tree(conn)
        readings = load_readings(conn, hour)
        statuses = load_review_statuses(conn, hour)
        affected_ids = _self_and_ancestors(conn, meter_id)
        conclusions: list[dict[str, Any]] = []
        for affected_id in affected_ids:
            conclusion = recompute_one(
                conn,
                affected_id,
                hour,
                children=children,
                readings=readings,
                statuses=statuses,
            )
            if conclusion is not None:
                conclusions.append(conclusion)

        stored_review = _review_dict(conn, meter_id, hour)
        return {
            "ok": True,
            "success": True,
            "meter_id": meter_id,
            "meterId": meter_id,
            "hour": hour,
            "decision": decision,
            "review": serialize_review(stored_review),
            "affected_meter_ids": affected_ids,
            "recomputed_meter_ids": [item["meter_id"] for item in conclusions],
            "reconciliations": conclusions,
            "conclusions": conclusions,
        }


def get_reading_review(meter_id: str, hour_value: Any) -> dict[str, Any]:
    hour = parse_hour(hour_value)
    with read_connection() as conn:
        if _meter_exists(conn, meter_id) is None:
            raise NotFoundProblem("meter_not_found", f"Meter {meter_id!r} is not registered.")
        review = _review_dict(conn, meter_id, hour)
        if review is None:
            raise NotFoundProblem(
                "review_not_found",
                "No reading (and therefore no review) exists for this meter and hour.",
                {"meter_id": meter_id, "hour": hour},
            )
        result = serialize_review(review)
        reading = _reading_dict(conn, meter_id, hour)
        result["amount"] = reading["amount"] if reading else None
        result["withdrawn"] = reading is None
        return result


def list_reading_reviews(hour_value: Any, status_filter: str | None = None) -> list[dict[str, Any]]:
    hour = parse_hour(hour_value)
    with read_connection() as conn:
        if status_filter:
            normalized = _normalize_decision(status_filter) if status_filter not in {"auto_approved"} else status_filter
            rows = conn.execute(
                "SELECT * FROM reading_reviews WHERE hour = ? AND status = ? ORDER BY meter_id",
                (hour, normalized),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM reading_reviews WHERE hour = ? ORDER BY meter_id",
                (hour,),
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = serialize_review(row)
            reading = _reading_dict(conn, row["meter_id"], hour)
            item["amount"] = reading["amount"] if reading else None
            item["withdrawn"] = reading is None
            results.append(item)
        return results


def list_reading_review_events(hour_value: Any, meter_id: str | None = None) -> list[dict[str, Any]]:
    hour = parse_hour(hour_value)
    query = "SELECT * FROM reading_review_events WHERE hour = ?"
    params: list[Any] = [hour]
    if meter_id:
        query += " AND meter_id = ?"
        params.append(meter_id)
    query += " ORDER BY event_id"
    with read_connection() as conn:
        return [dict(row) for row in conn.execute(query, params).fetchall()]


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
        statuses = load_review_statuses(conn, hour)
        stored: list[dict[str, Any]] = []
        for meter_id in meters:
            result = recompute_one(
                conn,
                meter_id,
                hour,
                children=children,
                readings=readings,
                statuses=statuses,
            )
            if result is not None:
                stored.append(result)
    return {"hour": hour, "count": len(stored), "results": stored, "conclusions": stored}
