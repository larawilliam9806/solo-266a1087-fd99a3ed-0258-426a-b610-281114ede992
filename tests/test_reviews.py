from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TEST_DIR = tempfile.mkdtemp(prefix="meter-review-test-")
os.environ["DB_PATH"] = str(Path(_TEST_DIR) / "review-ledger.db")
os.environ["SEED_DEMO_TREE"] = "false"
os.environ["ALLOW_RESET"] = "true"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db, reset_database_file  # noqa: E402
from app.main import app  # noqa: E402

HOUR = "2026-02-02T08:00:00Z"


@pytest.fixture()
def client():
    init_db()
    reset_database_file()
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _tree(client: TestClient) -> None:
    response = client.post(
        "/meters",
        json=[
            {"meter_id": "P"},
            {"meter_id": "A", "parent_id": "P"},
            {"meter_id": "A1", "parent_id": "A"},
            {"meter_id": "A2", "parent_id": "A"},
            {"meter_id": "B", "parent_id": "P"},
            {"meter_id": "B1", "parent_id": "B"},
        ],
    )
    assert response.status_code == 200, response.text


def _report(client: TestClient, meter_id: str, amount: int, hour: str = HOUR):
    r = client.post("/readings", json={"meter_id": meter_id, "hour": hour, "amount": amount})
    assert r.status_code == 200, r.text
    return r.json()


def _review(client: TestClient, meter_id: str, version: int, decision: str, reason=None, reviewer=None):
    payload = {
        "meter_id": meter_id,
        "hour": HOUR,
        "reading_version": version,
        "decision": decision,
    }
    if reason is not None:
        payload["reason"] = reason
    if reviewer is not None:
        payload["reviewer"] = reviewer
    return client.post("/readings/reviews", json=payload)


def test_new_readings_default_to_auto_approved_and_enter_ledger(client: TestClient):
    _tree(client)
    for mid, amount in [("A1", 100), ("A2", 200), ("B", 300), ("P", 600)]:
        body = _report(client, mid, amount)
        assert body["review"]["status"] == "auto_approved"
        assert body["review"]["admitted"] is True

    review = client.get("/readings/A1/review", params={"hour": HOUR}).json()
    assert review["status"] == "auto_approved"
    assert review["reading_version"] == 1
    assert review["reason"] is None

    p = client.get("/reconciliations/P", params={"hour": HOUR}).json()
    assert p["complete"] is True
    assert p["difference"] == 0
    assert p["suspended_meter_ids"] == []


def test_suspended_reading_is_treated_as_missing_and_reapproved_reading_returns(client: TestClient):
    _tree(client)
    for mid, amount in [("A1", 100), ("A2", 200), ("B1", 50), ("B", 300), ("P", 650)]:
        _report(client, mid, amount)

    version = client.get("/readings/A1/review", params={"hour": HOUR}).json()["reading_version"]
    held = _review(client, "A1", version, "pending", reason="spike looks impossible", reviewer="qa-7")
    assert held.status_code == 200, held.text
    assert held.json()["review"]["status"] == "pending"
    assert held.json()["review"]["suspended"] is True

    # Frontier descends past A1; A1 is a leaf so it appears as missing.
    p = client.get("/reconciliations/P", params={"hour": HOUR}).json()
    assert p["complete"] is False
    assert p["missing_meter_ids"] == ["A1"]
    assert p["difference"] is None
    assert p["suspended_meter_ids"] == ["A1"]
    # No used reading references the held value.
    assert all(item["meter_id"] != "A1" for item in p["used_readings"])

    # A has no own report, so it never stores its own conclusion (existing
    # rule); the held value is still visible through P's conclusion.
    assert client.get("/reconciliations/A", params={"hour": HOUR}).status_code == 404

    # Audit event was written.
    events = client.get("/readings/review-events", params={"hour": HOUR}).json()
    assert len(events) == 1
    assert events[0]["decision"] == "pending"
    assert events[0]["reason"] == "spike looks impossible"
    assert events[0]["reviewer"] == "qa-7"
    assert events[0]["reading_version"] == version

    # Approval re-admits the value; conclusions come back in the same call.
    approved = _review(client, "A1", version, "approved", reason="checked on site", reviewer="qa-7")
    assert approved.status_code == 200
    p = client.get("/reconciliations/P", params={"hour": HOUR}).json()
    assert p["complete"] is True
    assert p["difference"] == 650 - (100 + 200 + 300)
    assert p["suspended_meter_ids"] == []

    events = client.get("/readings/review-events", params={"hour": HOUR}).json()
    assert [event["decision"] for event in events] == ["pending", "approved"]


def test_suspending_parent_own_reading_removes_its_conclusion(client: TestClient):
    _tree(client)
    for mid, amount in [("A1", 100), ("A2", 200), ("P", 300)]:
        _report(client, mid, amount)
    assert client.get("/reconciliations/P", params={"hour": HOUR}).status_code == 200

    version = client.get("/readings/P/review", params={"hour": HOUR}).json()["reading_version"]
    assert _review(client, "P", version, "pending", reason="park meter seal broken").status_code == 200

    # The park total itself is held: it is ineligible for a stored conclusion.
    assert client.get("/reconciliations/P", params={"hour": HOUR}).status_code == 404

    _review(client, "P", version, "approved", reason="seal verified after inspection")
    assert client.get("/reconciliations/P", params={"hour": HOUR}).status_code == 200


def test_correction_resets_review_to_pending(client: TestClient):
    _tree(client)
    _report(client, "A1", 100)
    assert client.get("/readings/A1/review", params={"hour": HOUR}).json()["status"] == "auto_approved"

    corrected = client.put(
        "/readings", json={"meter_id": "A1", "hour": HOUR, "amount": 120}
    ).json()
    assert corrected["version"] == 2
    assert corrected["review"]["status"] == "pending"
    assert corrected["review"]["reading_version"] == 2

    # The old version-1 decision can no longer approve the current value.
    stale = _review(client, "A1", 1, "approved", reason="late approval")
    assert stale.status_code == 409
    body = stale.json()
    assert body["error"]["code"] == "reading_version_conflict"
    assert body["current_version"] == 2
    assert body["current_status"] == "pending"

    # State is preserved on conflict.
    review = client.get("/readings/A1/review", params={"hour": HOUR}).json()
    assert review["status"] == "pending"
    assert review["reason"] is None
    assert client.get("/readings/review-events", params={"hour": HOUR}).json() == []


def test_withdraw_and_rereport_revision_never_matches_stale_review(client: TestClient):
    _tree(client)
    _report(client, "A1", 100)

    # Withdraw: review row becomes pending re-review, reading is gone.
    withdrawn = client.request(
        "DELETE", "/readings", json={"meter_id": "A1", "hour": HOUR}
    )
    assert withdrawn.status_code == 200
    review = client.get("/readings/A1/review", params={"hour": HOUR}).json()
    assert review["status"] == "pending"
    assert review["withdrawn"] is True

    # Reviewing a withdrawn reading is a 404 and leaves state untouched.
    no_reading = _review(client, "A1", 1, "approved", reason="too late")
    assert no_reading.status_code == 404
    assert no_reading.json()["error"]["code"] == "reading_not_found"

    # Re-report starts a new incarnation that reuses human-facing version 1,
    # but its revision token advanced.
    rereport = _report(client, "A1", 110)
    assert rereport["version"] == 1
    assert rereport["review"]["status"] == "pending"
    assert rereport["revision"] == 2

    # A stale review that only knew version 1 / revision 1 must still
    # conflict, even though the new incarnation reuses human-facing version 1.
    stale = client.post(
        "/readings/reviews",
        json={
            "meter_id": "A1",
            "hour": HOUR,
            "reading_version": 1,
            "reading_revision": 1,
            "decision": "approved",
            "reason": "stale",
        },
    )
    assert stale.status_code == 409
    assert stale.json()["current_revision"] == 2
    assert stale.json()["submitted_revision"] == 1

    # Omitting revision defaults to the first incarnation and also conflicts.
    assert _review(client, "A1", 1, "approved", reason="stale-default").status_code == 409

    current = client.get("/readings/A1/review", params={"hour": HOUR}).json()
    ok = client.post(
        "/readings/reviews",
        json={
            "meter_id": "A1",
            "hour": HOUR,
            "reading_version": current["reading_version"],
            "reading_revision": current["reading_revision"],
            "decision": "approved",
            "reason": "new value ok",
        },
    )
    assert ok.status_code == 200
    assert ok.json()["review"]["status"] == "approved"


def test_review_validation_rules(client: TestClient):
    _tree(client)
    _report(client, "A1", 100)

    # Suspend requires a reason.
    no_reason = _review(client, "A1", 1, "pending")
    assert no_reason.status_code == 422
    assert no_reason.json()["error"]["code"] == "reason_required"

    # Unknown decision is rejected.
    bad = _review(client, "A1", 1, "maybe", reason="?")
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "invalid_decision"

    # Missing version.
    missing_version = client.post(
        "/readings/reviews",
        json={"meter_id": "A1", "hour": HOUR, "decision": "approved"},
    )
    assert missing_version.status_code == 422
    assert missing_version.json()["error"]["code"] == "reading_version_required"

    # Unknown meter and never-reported reading are 404s.
    unknown_meter = _review(client, "GHOST", 1, "approved")
    assert unknown_meter.status_code == 404
    never = _review(client, "A2", 1, "approved")
    assert never.status_code == 404


def test_review_listing_and_status_filter(client: TestClient):
    _tree(client)
    _report(client, "A1", 100)
    _report(client, "A2", 200)
    _review(client, "A1", 1, "pending", reason="held")

    all_rows = client.get("/readings/reviews", params={"hour": HOUR}).json()
    assert {row["meter_id"] for row in all_rows} == {"A1", "A2"}
    statuses = {row["meter_id"]: row["status"] for row in all_rows}
    assert statuses == {"A1": "pending", "A2": "auto_approved"}

    pending_only = client.get(
        "/readings/reviews", params={"hour": HOUR, "status": "pending"}
    ).json()
    assert [row["meter_id"] for row in pending_only] == ["A1"]

    approved_only = client.get(
        "/readings/reviews", params={"hour": HOUR, "status": "approved"}
    ).json()
    assert approved_only == []


def test_failed_review_rolls_back_review_event_and_conclusions(client: TestClient, monkeypatch):
    _tree(client)
    for mid, amount in [("A1", 100), ("A2", 200), ("P", 300)]:
        _report(client, mid, amount)
    before_p = client.get("/reconciliations/P", params={"hour": HOUR}).json()

    import app.service as service

    def fail_recompute(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "recompute_one", fail_recompute)
    response = _review(client, "A1", 1, "pending", reason="will roll back")
    assert response.status_code == 500
    monkeypatch.undo()

    # Review row unchanged, no audit event, conclusions unchanged.
    review = client.get("/readings/A1/review", params={"hour": HOUR}).json()
    assert review["status"] == "auto_approved"
    assert client.get("/readings/review-events", params={"hour": HOUR}).json() == []
    assert client.get("/reconciliations/P", params={"hour": HOUR}).json() == before_p

    # A valid retry still works.
    ok = _review(client, "A1", 1, "pending", reason="now it works")
    assert ok.status_code == 200


def test_concurrent_reviews_are_serializable(client: TestClient):
    from concurrent.futures import ThreadPoolExecutor

    _tree(client)
    _report(client, "A1", 100)

    def decide(i: int):
        from app.service import submit_reading_review

        return submit_reading_review(
            meter_id="A1",
            hour_value=HOUR,
            observed_version=1,
            decision_value="pending" if i % 2 == 0 else "approved",
            reason_value=f"check {i}",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(decide, range(12)))
    assert all(r["ok"] for r in results)

    review = client.get("/readings/A1/review", params={"hour": HOUR}).json()
    assert review["decision_count"] == 12
    events = client.get("/readings/review-events", params={"hour": HOUR}).json()
    assert len(events) == 12

    # Final conclusion state matches the last committed decision. While A1 is
    # held it stays out of the ledger; once approved it is admitted again.
    if review["status"] == "pending":
        pending_rows = client.get(
            "/readings/reviews", params={"hour": HOUR, "status": "pending"}
        ).json()
        assert [row["meter_id"] for row in pending_rows] == ["A1"]
    else:
        assert review["admitted"] is True
        assert client.get(
            "/readings/reviews", params={"hour": HOUR, "status": "approved"}
        ).json()[0]["meter_id"] == "A1"


def test_legacy_database_is_migrated_without_losing_ledger(tmp_path, monkeypatch):
    import sqlite3

    legacy = tmp_path / "legacy.db"
    # Schema as it existed before the quality-review feature.
    conn = sqlite3.connect(legacy)
    conn.executescript(
        """
        CREATE TABLE meters (
            meter_id TEXT PRIMARY KEY, parent_id TEXT, name TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        CREATE TABLE readings (
            meter_id TEXT NOT NULL, hour TEXT NOT NULL, amount INTEGER NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            PRIMARY KEY (meter_id, hour)
        ) WITHOUT ROWID;
        CREATE TABLE reading_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT, meter_id TEXT NOT NULL,
            hour TEXT NOT NULL, action TEXT NOT NULL, amount INTEGER,
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        CREATE TABLE reconciliations (
            hour TEXT NOT NULL, meter_id TEXT NOT NULL, own_amount INTEGER NOT NULL,
            is_complete INTEGER NOT NULL, difference INTEGER, sum_used INTEGER NOT NULL,
            used_meter_ids TEXT NOT NULL, used_readings_json TEXT NOT NULL,
            missing_meter_ids TEXT NOT NULL,
            computed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            version INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (hour, meter_id)
        ) WITHOUT ROWID;
        INSERT INTO meters (meter_id, parent_id) VALUES ('P', NULL), ('L1', 'P');
        INSERT INTO readings (meter_id, hour, amount, version)
            VALUES ('L1', '2026-03-03T03:00:00Z', 42, 1);
        """
    )
    conn.commit()
    conn.close()

    import app.db as db

    monkeypatch.setattr(db, "DB_PATH", str(legacy))
    db.init_db()
    check = db.connect()
    try:
        columns = {row[1] for row in check.execute("PRAGMA table_info(readings)")}
        assert "revision" in columns
        recon_cols = {row[1] for row in check.execute("PRAGMA table_info(reconciliations)")}
        assert "suspended_meter_ids_json" in recon_cols
        tables = {
            row[0]
            for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"reading_reviews", "reading_review_events"} <= tables
        # Existing reading survived and defaults to revision 1.
        row = check.execute(
            "SELECT amount, revision FROM readings WHERE meter_id = 'L1'"
        ).fetchone()
        assert tuple(row) == (42, 1)
    finally:
        check.close()

    # The service layer opens connections through the same db module path.
    import app.service as service

    monkeypatch.setattr(service, "write_transaction", db.write_transaction)
    with db.write_transaction() as write_conn:
        write_conn.execute(
            "INSERT INTO reading_reviews (meter_id, hour, reading_version, reading_revision, status) "
            "VALUES ('L1', '2026-03-03T03:00:00Z', 1, 1, 'auto_approved')"
        )
        write_conn.execute(
            "INSERT INTO readings (meter_id, hour, amount, version, revision) "
            "VALUES ('P', '2026-03-03T03:00:00Z', 42, 1, 1)"
        )
        write_conn.execute(
            "INSERT INTO reading_reviews (meter_id, hour, reading_version, reading_revision, status) "
            "VALUES ('P', '2026-03-03T03:00:00Z', 1, 1, 'auto_approved')"
        )

    result = service.recompute_all_for_hour("2026-03-03T03:00:00Z")
    p = next(item for item in result["results"] if item["meter_id"] == "P")
    assert p["difference"] == 0

    # The reviewer can suspend the backfilled legacy reading.
    held = service.submit_reading_review(
        meter_id="L1",
        hour_value="2026-03-03T03:00:00Z",
        observed_version=1,
        decision_value="pending",
        reason_value="legacy value under question",
    )
    assert held["review"]["status"] == "pending"
    after = service.get_conclusion("P", "2026-03-03T03:00:00Z")
    assert after["suspended_meter_ids"] == ["L1"]
    assert after["complete"] is False


def test_review_alias_routes_and_path_forms(client: TestClient):
    _tree(client)
    _report(client, "A1", 100)

    r1 = client.post(
        "/reviews",
        json={
            "meterId": "A1",
            "hour": HOUR,
            "observedVersion": 1,
            "verdict": "suspend",
            "comment": "alias fields",
        },
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["review"]["status"] == "pending"

    r2 = client.post(
        f"/readings/A1/{HOUR}/review",
        json={"reading_version": 1, "decision": "approve", "reason": "path form"},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["review"]["status"] == "approved"

    single = client.get(f"/reviews/A1/{HOUR}").json()
    assert single["meter_id"] == "A1"
    assert single["status"] == "approved"
