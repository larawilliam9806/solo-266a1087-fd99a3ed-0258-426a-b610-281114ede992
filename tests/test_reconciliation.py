from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Isolate the test ledger before importing the application configuration.
_TEST_DIR = tempfile.mkdtemp(prefix="meter-recon-test-")
os.environ["DB_PATH"] = str(Path(_TEST_DIR) / "test-ledger.db")
os.environ["SEED_DEMO_TREE"] = "false"
os.environ["ALLOW_RESET"] = "true"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db, reset_database_file  # noqa: E402
from app.main import app  # noqa: E402

HOUR = "2026-01-01T00:00:00Z"


@pytest.fixture()
def client():
    init_db()
    reset_database_file()
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def register_basic_tree(client: TestClient) -> None:
    response = client.post(
        "/meters",
        json={
            "meters": [
                {"meter_id": "P", "children": [
                    {"meter_id": "A", "children": ["A1", "A2"]},
                    {"meter_id": "B", "children": [
                        {"meter_id": "B1"},
                        {"meter_id": "B2"},
                    ]},
                ]}
            ]
        },
    )
    # String children are not objects; use explicit payload below if needed.
    if response.status_code >= 400:
        response = client.post(
            "/meters",
            json=[
                {"meter_id": "P"},
                {"meter_id": "A", "parent_id": "P"},
                {"meter_id": "A1", "parent_id": "A"},
                {"meter_id": "A2", "parent_id": "A"},
                {"meter_id": "B", "parent_id": "P"},
                {"meter_id": "B1", "parent_id": "B"},
                {"meter_id": "B2", "parent_id": "B"},
            ],
        )
    assert response.status_code == 200, response.text


def report(client: TestClient, meter_id: str, amount: int, hour: str = HOUR) -> None:
    response = client.post("/readings", json={"meter_id": meter_id, "hour": hour, "amount": amount})
    assert response.status_code == 201, response.text if response.status_code == 201 else response.text
    # Our API currently returns 200 from the route because no status_code set.
    # Accept either 200/201 below in callers? This helper expects success only.


def put_reading(client: TestClient, meter_id: str, amount: int, hour: str = HOUR):
    return client.put("/readings", json={"meter_id": meter_id, "hour": hour, "amount": amount})


def test_nearest_reports_stop_and_difference(client: TestClient):
    register_basic_tree(client)
    for mid, amount in [("A1", 100), ("A2", 200), ("B", 650), ("P", 1000)]:
        r = client.post("/readings", json={"meter_id": mid, "hour": HOUR, "amount": amount})
        assert r.status_code == 200, r.text

    rows = client.get("/reconciliations", params={"hour": HOUR}).json()
    by_meter = {row["meter_id"]: row for row in rows}

    assert set(by_meter) == {"P", "B"}  # A has no report; leaves have no children.
    p = by_meter["P"]
    assert p["complete"] is True
    assert p["used_meter_ids"] == ["A1", "A2", "B"]
    assert p["sum_used"] == 950
    assert p["difference"] == 50
    assert p["balanced"] is False

    b = by_meter["B"]
    assert b["complete"] is False
    assert b["missing_meter_ids"] == ["B1", "B2"]
    assert b["difference"] is None


def test_zero_parent_difference_still_lists_lower_levels(client: TestClient):
    register_basic_tree(client)
    for mid, amount in [("A1", 100), ("A2", 200), ("B", 650), ("P", 950)]:
        r = client.post("/readings", json={"meter_id": mid, "hour": HOUR, "amount": amount})
        assert r.status_code == 200, r.text

    data = client.get("/reconcile", params={"hour": HOUR}).json()
    by_meter = {row["meter_id"]: row for row in data["results"]}
    assert by_meter["P"]["difference"] == 0
    assert by_meter["P"]["balanced"] is True
    assert "B" in by_meter
    assert by_meter["B"]["complete"] is False

    tree = client.get("/meters/tree", params={"hour": HOUR}).json()["tree"]
    assert tree[0]["conclusion"]["difference"] == 0
    assert tree[0]["children"][1]["conclusion"]["meter_id"] == "B"


def test_skip_unreported_levels_and_list_terminal_missing(client: TestClient):
    client.post("/meters", json=[
        {"meter_id": "R"},
        {"meter_id": "C", "parent_id": "R"},
        {"meter_id": "D1", "parent_id": "C"},
        {"meter_id": "D2", "parent_id": "C"},
        {"meter_id": "L1", "parent_id": "D2"},
        {"meter_id": "L2", "parent_id": "D2"},
    ])
    for mid, amount in [("D1", 30), ("R", 100)]:
        r = client.post("/readings", json={"meter_id": mid, "hour": HOUR, "amount": amount})
        assert r.status_code == 200, r.text

    row = client.get("/reconciliations/R", params={"hour": HOUR}).json()
    assert row["used_meter_ids"] == ["D1"]
    assert row["missing_meter_ids"] == ["L1", "L2"]
    assert row["complete"] is False


def test_correction_and_withdrawal_recompute_chain(client: TestClient):
    register_basic_tree(client)
    for mid, amount in [("A1", 100), ("A2", 200), ("B", 100), ("P", 500)]:
        client.post("/readings", json={"meter_id": mid, "hour": HOUR, "amount": amount})

    row = client.get("/reconciliations/P", params={"hour": HOUR}).json()
    assert row["difference"] == 100

    r = client.put("/readings", json={"meter_id": "A1", "hour": HOUR, "amount": 150})
    assert r.status_code == 200
    row = client.get("/reconciliations/P", params={"hour": HOUR}).json()
    assert row["difference"] == 50
    assert row["used_readings"][0]["amount"] == 150

    r = client.request("DELETE", "/readings", json={"meter_id": "A1", "hour": HOUR})
    assert r.status_code == 200, r.text
    row = client.get("/reconciliations/P", params={"hour": HOUR}).json()
    assert row["complete"] is False
    assert row["missing_meter_ids"] == ["A1"]

    client.request("DELETE", "/readings", json={"meter_id": "P", "hour": HOUR})
    assert client.get("/reconciliations/P", params={"hour": HOUR}).status_code == 404


def test_only_self_and_ancestors_are_recomputed(client: TestClient):
    register_basic_tree(client)
    for mid, amount in [("A1", 1), ("A2", 2), ("B", 3), ("P", 10)]:
        client.post("/readings", json={"meter_id": mid, "hour": HOUR, "amount": amount})

    before_p = client.get("/reconciliations/P", params={"hour": HOUR}).json()
    before_b = client.get("/reconciliations/B", params={"hour": HOUR}).json()

    client.put("/readings", json={"meter_id": "A1", "hour": HOUR, "amount": 5})

    after_p = client.get("/reconciliations/P", params={"hour": HOUR}).json()
    after_b = client.get("/reconciliations/B", params={"hour": HOUR}).json()
    assert after_p["version"] == before_p["version"] + 1
    assert after_b["version"] == before_b["version"]
    assert after_b["computed_at"] == before_b["computed_at"]


def test_invalid_hour_and_negative_amount_do_not_write(client: TestClient):
    register_basic_tree(client)
    bad_hour = client.post("/readings", json={"meter_id": "A1", "hour": "2026-01-01T00:30:00Z", "amount": 1})
    assert bad_hour.status_code == 422
    negative = client.post("/readings", json={"meter_id": "A1", "hour": HOUR, "amount": -1})
    assert negative.status_code == 422
    assert client.get("/reconciliations", params={"hour": HOUR}).json() == []


def test_failed_write_keeps_previous_ledger(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    register_basic_tree(client)
    client.post("/readings", json={"meter_id": "A1", "hour": HOUR, "amount": 100})
    client.post("/readings", json={"meter_id": "B", "hour": HOUR, "amount": 0})
    client.post("/readings", json={"meter_id": "P", "hour": HOUR, "amount": 150})
    before = client.get("/reconciliations/P", params={"hour": HOUR}).json()

    import app.service as service

    def fail_recompute(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "recompute_one", fail_recompute)
    response = client.post("/readings", json={"meter_id": "A2", "hour": HOUR, "amount": 999})
    assert response.status_code == 500

    monkeypatch.undo()
    # The inserted reading was rolled back.
    rows = client.get("/reconciliations", params={"hour": HOUR}).json()
    p = next(row for row in rows if row["meter_id"] == "P")
    assert p == before
    # A valid retry works from the preserved ledger.
    ok = client.post("/readings", json={"meter_id": "A2", "hour": HOUR, "amount": 50})
    assert ok.status_code == 200
    p = client.get("/reconciliations/P", params={"hour": HOUR}).json()
    assert p["difference"] == 0


def test_concurrent_mutations_are_serializable(client: TestClient):
    from concurrent.futures import ThreadPoolExecutor

    client.post("/meters", json=[{"meter_id": "P"}] + [{"meter_id": f"L{i}", "parent_id": "P"} for i in range(20)])
    client.post("/readings", json={"meter_id": "P", "hour": HOUR, "amount": 2000})

    def report_leaf(i: int):
        from app.service import apply_reading

        return apply_reading(action="report", meter_id=f"L{i}", hour_value=HOUR, amount=100)

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(report_leaf, range(20)))
    assert all(r["action"] == "reported" for r in results)

    p = client.get("/reconciliations/P", params={"hour": HOUR}).json()
    assert p["complete"] is True
    assert p["sum_used"] == 2000
    assert p["difference"] == 0

    # Concurrent corrections all succeed as upserts; ledger/conclusion agree.
    from app.service import apply_reading

    apply_reading(action="upsert", meter_id="L0", hour_value=HOUR, amount=100)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: apply_reading(action="upsert", meter_id="L0", hour_value=HOUR, amount=i), range(100, 108)))

    row = client.get("/reconciliations/P", params={"hour": HOUR}).json()
    current = client.get("/meters/L0", params={"hour": HOUR}).json()
    assert current["conclusion"] is None  # leaf has no conclusion
    assert row["used_readings"][0]["amount"] if row["used_meter_ids"][0] == "L0" else True
    l0_amount = next(item["amount"] for item in row["used_readings"] if item["meter_id"] == "L0")
    assert 100 <= l0_amount <= 107
    assert row["difference"] == 2000 - (1900 + l0_amount)
