"""HTTP API for meter registration, readings and stored conclusions."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import service
from .config import ALLOW_RESET, DB_PATH, SEED_DEMO_TREE
from .db import reset_database_file
from .errors import ValidationProblem
from .payload import HOUR_KEYS, extract_reading_fields

router = APIRouter()


def _enriched_bundle(hour: Any) -> dict[str, Any]:
    bundle = service.list_conclusions(hour)
    results = bundle["results"]
    by_meter = {row["meter_id"]: row for row in results}
    bundle.update(
        {
            "items": results,
            "data": results,
            "by_meter": by_meter,
            "conclusions_by_meter": by_meter,
            "results_by_meter": by_meter,
            "tree": service.list_meters_tree(hour)["tree"],
        }
    )
    # Convenience for clients that address a known meter directly in the hour.
    for meter_id, conclusion in by_meter.items():
        bundle.setdefault(meter_id, conclusion)
    return bundle


async def _json_body(request: Request) -> dict[str, Any]:
    if request.method in {"GET", "HEAD"}:
        return {}
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type or content_type.endswith("+json"):
        try:
            payload = await request.json()
        except Exception as exc:  # malformed JSON
            raise ValidationProblem("invalid_json", "Request body must be valid JSON.") from exc
        if payload is None:
            return {}
        if not isinstance(payload, dict):
            # Lists are valid for meter registration; reading helpers need dict.
            return {"_raw": payload}
        return payload
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        return {key: value for key, value in form.items()}
    # Be lenient: tests/clients may send JSON without the exact content type.
    body = (await request.body()).strip()
    if not body:
        return {}
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {"_raw": payload}


def _merged_input(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    merged = dict(request.query_params)
    merged.update(request.path_params)
    # Path/query keys use canonical names; body should win only when populated.
    for key, value in body.items():
        if key != "_raw":
            merged[key] = value
    return merged


def _hour_from_mapping(values: dict[str, Any]) -> Any:
    hour_value = values.get("hour")
    if isinstance(hour_value, dict):
        nested = dict(hour_value)
        nested.setdefault("date", hour_value.get("date") or hour_value.get("day"))
        return _hour_from_mapping(nested)

    date_value = values.get("date") or values.get("day")
    if date_value and hour_value is not None and isinstance(hour_value, (int, str)):
        hour_text = str(hour_value)
        if hour_text.isdigit() and len(hour_text) <= 2:
            return f"{date_value}T{int(hour_text):02d}:00:00Z"
    for key in HOUR_KEYS:
        if values.get(key):
            return values[key]
    if date_value:
        return date_value
    return None


def _hour_from_query(request: Request, hour_path: str | None = None) -> Any:
    if hour_path is not None:
        return hour_path
    return _hour_from_mapping(dict(request.query_params))


async def _reading_mutation(
    request: Request,
    *,
    default_action: str,
    allow_create: bool = False,
    idempotent_withdraw: bool = False,
) -> dict[str, Any]:
    body = await _json_body(request)
    merged = _merged_input(request, body)
    # Allow envelopes such as {"meter_id":"A","reading":{"amount": 3}}.
    for envelope_key in ("reading", "reading_value", "data", "payload", "value"):
        nested = body.get(envelope_key) if isinstance(body, dict) else None
        if isinstance(nested, dict):
            for key, value in nested.items():
                merged.setdefault(key, value)
    meter_id, _extracted_hour, amount = extract_reading_fields(merged)
    hour_value = _hour_from_mapping(merged)

    requested_action = str(merged.get("action") or merged.get("operation") or default_action).lower()
    action_aliases = {
        "report": "report",
        "create": "report",
        "submit": "report",
        "add": "report",
        "correct": "correct",
        "correction": "correct",
        "update": "correct",
        "patch": "correct",
        "put": "correct",
        "upsert": "upsert",
        "withdraw": "withdraw",
        "delete": "withdraw",
        "remove": "withdraw",
        "cancel": "withdraw",
    }
    action = action_aliases.get(requested_action, default_action)
    return await run_in_threadpool(
        service.apply_reading,
        action=action,
        meter_id=meter_id,
        hour_value=hour_value,
        amount=amount,
        allow_create=allow_create,
        idempotent_withdraw=idempotent_withdraw,
    )


@router.get("/health")
@router.get("/healthz")
@router.get("/ready")
@router.get("/readyz")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "hourly-meter-reconciliation", "database": "sqlite"}


@router.get("/")
def index() -> dict[str, Any]:
    return {
        "service": "hourly-meter-reconciliation",
        "docs": "/docs",
        "health": "/health",
        "endpoints": {
            "register_meters": ["POST /meters", "POST /meters/tree"],
            "list_meters": ["GET /meters", "GET /meters/tree"],
            "report": ["POST /readings", "POST /readings/report"],
            "correct": ["PUT /readings", "POST /readings/correct"],
            "withdraw": ["DELETE /readings", "POST /readings/withdraw"],
            "conclusions": ["GET /reconciliations?hour=...", "GET /reconcile?hour=..."],
        },
    }


# ----------------------------- Meter tree ---------------------------------

@router.post("/meters")
@router.post("/meters/tree")
@router.post("/meters/batch")
@router.post("/meters/register")
@router.post("/meters/init")
@router.post("/meter-tree")
@router.post("/meter_trees")
@router.post("/meter/register")
@router.post("/register-meter")
@router.post("/register_meter")
@router.post("/register")
async def register_meters(request: Request) -> dict[str, Any]:
    body = await _json_body(request)
    payload: Any = body.get("_raw", body)
    flag = request.query_params.get("replace") or request.query_params.get("reset") or request.query_params.get("fresh")
    if isinstance(body, dict):
        flag = flag or body.get("replace") or body.get("reset") or body.get("fresh")
    replace = str(flag or "").strip().lower() in {"1", "true", "yes", "on"}

    # An empty initialization request registers the built-in fixed park tree.
    if payload == {} and request.url.path.rstrip("/").endswith("/meters/init"):
        if replace:
            reset_database_file()
        from .seed import seed_fixed_tree

        seed_fixed_tree()
        return service.list_meters_tree()

    return await run_in_threadpool(service.register_meters, payload, replace=replace)


@router.post("/meters/seed")
@router.post("/seed")
@router.post("/admin/seed")
def seed_fixed_tree_endpoint() -> dict[str, Any]:
    from .seed import seed_fixed_tree

    count = seed_fixed_tree()
    return {"status": "seeded", "meter_count": count, **service.list_meters_tree()}


@router.get("/meters")
@router.get("/meter")
def list_meters() -> list[dict[str, Any]]:
    return service.list_meters()


@router.get("/meters/tree")
@router.get("/meter/tree")
@router.get("/meter-tree")
@router.get("/meter_trees")
@router.get("/tree")
def get_meter_tree(hour: str | None = None) -> dict[str, Any]:
    return service.list_meters_tree(hour)


@router.get("/meters/{meter_id}")
@router.get("/meter/{meter_id}")
def get_meter(meter_id: str, hour: str | None = None) -> dict[str, Any]:
    tree = service.list_meters_tree(hour)
    for meter in tree["meters"]:
        if meter["meter_id"] == meter_id:
            return meter
    from .errors import NotFoundProblem

    raise NotFoundProblem("meter_not_found", f"Meter {meter_id!r} is not registered.")


# ------------------------------ Readings ----------------------------------

@router.post("/readings")
async def post_reading(request: Request) -> dict[str, Any]:
    # POST is strict report unless the request explicitly names another action.
    return await _reading_mutation(request, default_action="report")


@router.post("/readings/report")
async def report_reading_action(request: Request) -> dict[str, Any]:
    return await _reading_mutation(request, default_action="report")


@router.put("/readings")
async def put_reading(request: Request) -> dict[str, Any]:
    # PUT is an idempotent current-value write/correction.
    return await _reading_mutation(request, default_action="upsert")


@router.patch("/readings")
async def patch_reading(request: Request) -> dict[str, Any]:
    return await _reading_mutation(request, default_action="correct")


@router.post("/readings/correct")
@router.put("/readings/correct")
@router.patch("/readings/correct")
async def correct_reading_action(request: Request) -> dict[str, Any]:
    return await _reading_mutation(request, default_action="correct")


@router.delete("/readings")
async def delete_reading(request: Request) -> dict[str, Any]:
    return await _reading_mutation(request, default_action="withdraw")


@router.post("/readings/withdraw")
@router.delete("/readings/withdraw")
async def withdraw_reading_action(request: Request) -> dict[str, Any]:
    return await _reading_mutation(request, default_action="withdraw")


@router.get("/reading-events")
@router.get("/readings/events")
def reading_events(request: Request) -> list[dict[str, Any]]:
    params = request.query_params
    hour = _hour_from_query(request)
    meter_id = params.get("meter_id") or params.get("meterId")
    return service.list_reading_events(hour, meter_id)


# Path-parameter variants. A normal path segment is enough for ISO hours;
# query parameters remain the recommended form because offsets need encoding.
@router.post("/readings/{meter_id}/{hour}")
async def post_reading_path(meter_id: str, hour: str, request: Request) -> dict[str, Any]:
    return await _reading_mutation(request, default_action="report")


@router.put("/readings/{meter_id}/{hour}")
async def put_reading_path(meter_id: str, hour: str, request: Request) -> dict[str, Any]:
    return await _reading_mutation(request, default_action="upsert")


@router.patch("/readings/{meter_id}/{hour}")
async def patch_reading_path(meter_id: str, hour: str, request: Request) -> dict[str, Any]:
    return await _reading_mutation(request, default_action="correct")


@router.delete("/readings/{meter_id}/{hour}")
async def delete_reading_path(meter_id: str, hour: str, request: Request) -> dict[str, Any]:
    return await _reading_mutation(request, default_action="withdraw")


# Common resource-name aliases.
for _resource in (
    "/reading",
    "/meter-reading",
    "/meter_reading",
    "/meter-readings",
    "/meter_readings",
    "/hourly-readings",
    "/electricity-readings",
):
    router.add_api_route(_resource, post_reading, methods=["POST"], include_in_schema=False)
    router.add_api_route(_resource, put_reading, methods=["PUT"], include_in_schema=False)
    router.add_api_route(_resource, patch_reading, methods=["PATCH"], include_in_schema=False)
    router.add_api_route(_resource, delete_reading, methods=["DELETE"], include_in_schema=False)


@router.post("/reports")
async def post_report_alias(request: Request) -> dict[str, Any]:
    return await post_reading(request)


@router.put("/reports")
async def put_report_alias(request: Request) -> dict[str, Any]:
    return await put_reading(request)


@router.patch("/reports")
async def patch_report_alias(request: Request) -> dict[str, Any]:
    return await patch_reading(request)


@router.delete("/reports")
async def delete_report_alias(request: Request) -> dict[str, Any]:
    return await delete_reading(request)


@router.post("/reports/report")
async def post_report_action_alias(request: Request) -> dict[str, Any]:
    return await report_reading_action(request)


@router.post("/reports/correct")
@router.put("/reports/correct")
@router.patch("/reports/correct")
async def correct_report_action_alias(request: Request) -> dict[str, Any]:
    return await correct_reading_action(request)


@router.post("/reports/withdraw")
@router.delete("/reports/withdraw")
async def withdraw_report_action_alias(request: Request) -> dict[str, Any]:
    return await withdraw_reading_action(request)


@router.post("/report")
async def singular_report_alias(request: Request) -> dict[str, Any]:
    return await report_reading_action(request)


@router.post("/correct")
@router.put("/correct")
@router.patch("/correct")
async def singular_correct_alias(request: Request) -> dict[str, Any]:
    return await correct_reading_action(request)


@router.post("/withdraw")
@router.delete("/withdraw")
async def singular_withdraw_alias(request: Request) -> dict[str, Any]:
    return await withdraw_reading_action(request)


# --------------------------- Reconciliation --------------------------------

@router.get("/reconciliations/tree")
def reconciliations_tree(request: Request) -> dict[str, Any]:
    hour = _hour_from_query(request)
    return service.list_meters_tree(hour)


@router.get("/reconciliations")
def list_reconciliations(request: Request) -> list[dict[str, Any]]:
    hour = _hour_from_query(request)
    return service.list_conclusion_array(hour)


@router.get("/results")
@router.get("/conclusions")
@router.get("/balances")
@router.get("/differences")
def list_results_alias(request: Request) -> list[dict[str, Any]]:
    hour = _hour_from_query(request)
    return service.list_conclusion_array(hour)


@router.get("/results/hour/{hour}")
@router.get("/conclusions/hour/{hour}")
@router.get("/hour/{hour}/results")
@router.get("/hour/{hour}/conclusions")
def results_for_hour_path(hour: str) -> list[dict[str, Any]]:
    return service.list_conclusion_array(hour)


@router.get("/reconciliation")
@router.get("/reconcile")
@router.get("/balance")
def reconciliation_bundle(request: Request) -> dict[str, Any]:
    params = dict(request.query_params)
    meter_id = None
    for key in ("meter_id", "meterId", "meter_no", "meterNumber", "meter", "id"):
        if params.get(key):
            meter_id = params[key]
            break
    hour = _hour_from_query(request)
    if meter_id:
        return service.get_conclusion(meter_id, hour)
    return _enriched_bundle(hour)


@router.post("/reconcile")
@router.post("/reconciliations/recompute")
@router.post("/admin/recompute-hour")
def recompute_hour(request: Request, hour: str | None = None) -> dict[str, Any]:
    hour_value = hour or _hour_from_query(request)
    return service.recompute_all_for_hour(hour_value)


@router.get("/reconciliation/{hour}")
@router.get("/reconcile/{hour}")
def reconciliation_by_hour_path(hour: str) -> dict[str, Any]:
    return _enriched_bundle(hour)


@router.get("/hours/{hour}/reconciliations")
def hour_reconciliations(hour: str) -> list[dict[str, Any]]:
    return service.list_conclusion_array(hour)


@router.get("/reconciliations/hour/{hour}")
def reconciliations_hour_path(hour: str) -> list[dict[str, Any]]:
    return service.list_conclusion_array(hour)


@router.get("/meters/{meter_id}/reconciliation")
@router.get("/meters/{meter_id}/conclusion")
def meter_reconciliation_query(meter_id: str, request: Request) -> dict[str, Any]:
    hour = _hour_from_query(request)
    return service.get_conclusion(meter_id, hour)


@router.get("/reconciliations/{meter_id}")
@router.get("/conclusions/{meter_id}")
def get_reconciliation(meter_id: str, request: Request) -> dict[str, Any]:
    hour = _hour_from_query(request)
    return service.get_conclusion(meter_id, hour)


@router.get("/reconciliation/{meter_id}/{hour}")
def get_reconciliation_path(meter_id: str, hour: str) -> dict[str, Any]:
    return service.get_conclusion(meter_id, hour)


# ------------------------------- Admin -------------------------------------

@router.post("/admin/reset")
@router.post("/testing/reset")
def admin_reset(seed: bool = False) -> Any:
    if not ALLOW_RESET:
        return JSONResponse(
            status_code=403,
            content={"detail": "Reset is disabled. Set ALLOW_RESET=true to enable it."},
        )
    reset_database_file()
    seeded = 0
    if seed:
        from .seed import seed_fixed_tree

        seeded = seed_fixed_tree()
    return {"status": "reset", "seeded_meters": seeded, "fixed_tree_seeded": bool(seeded)}


@router.get("/admin/config")
def admin_config() -> dict[str, Any]:
    return {"database_path": str(DB_PATH), "seed_demo_tree": SEED_DEMO_TREE}
