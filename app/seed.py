"""Fixed park meter tree registered on first startup."""
from __future__ import annotations

from .db import write_transaction

# The topology is the accounting boundary: sibling meters are non-overlapping
# and their leaves cover the parent's measurement range.
FIXED_METER_TREE: list[dict[str, object]] = [
    {
        "meter_id": "PARK-001",
        "parent_id": None,
        "name": "园区总表",
        "sort_order": 0,
    },
    {"meter_id": "BLDG-A", "parent_id": "PARK-001", "name": "A 栋总表", "sort_order": 0},
    {"meter_id": "A-1F", "parent_id": "BLDG-A", "name": "A 栋 1 层", "sort_order": 0},
    {"meter_id": "A-2F", "parent_id": "BLDG-A", "name": "A 栋 2 层", "sort_order": 1},
    {"meter_id": "BLDG-B", "parent_id": "PARK-001", "name": "B 栋总表", "sort_order": 1},
    {"meter_id": "B-1F", "parent_id": "BLDG-B", "name": "B 栋 1 层", "sort_order": 0},
    {"meter_id": "B-2F", "parent_id": "BLDG-B", "name": "B 栋 2 层", "sort_order": 1},
    {"meter_id": "EV-01", "parent_id": "PARK-001", "name": "充电站直属表", "sort_order": 2},
]


def seed_fixed_tree() -> int:
    with write_transaction() as conn:
        for meter in FIXED_METER_TREE:
            conn.execute(
                """
                INSERT INTO meters (meter_id, parent_id, name, sort_order)
                VALUES (:meter_id, :parent_id, :name, :sort_order)
                ON CONFLICT(meter_id) DO NOTHING
                """,
                meter,
            )
        return conn.execute("SELECT COUNT(*) AS n FROM meters").fetchone()["n"]
