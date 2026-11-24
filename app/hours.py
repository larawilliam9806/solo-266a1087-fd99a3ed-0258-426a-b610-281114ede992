"""UTC whole-hour parsing and formatting."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .errors import ValidationProblem

_HOUR_KEY_RE = re.compile(r"^(\d{4})-?(\d{2})-?(\d{2})[T ](\d{2})(?::00(?::00(?:\.\d+)?)?)?(?:Z|[+-]\d{2}:?\d{2})?$")
_COMPACT_HOUR_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(\d{2})$")
_DATE_HOUR_RE = re.compile(r"^(\d{4})-?(\d{2})-?(\d{2})$")


def _require_whole_hour(dt: datetime) -> datetime:
    if dt.minute or dt.second or dt.microsecond:
        raise ValidationProblem(
            "hour_not_whole",
            "Hour must be a UTC whole hour (minutes, seconds and microseconds must be zero).",
        )
    return dt


def parse_hour(value: Any) -> str:
    """Parse many reasonable hour representations and return canonical UTC text.

    Canonical form is ``YYYY-MM-DDTHH:00:00Z``, which sorts chronologically.
    Naive ISO strings are interpreted as UTC.
    """
    if value is None:
        raise ValidationProblem("hour_required", "hour is required.")

    # datetime objects (including those produced by lenient callers).
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return format_hour(_require_whole_hour(dt))

    # Integers are normally Unix time, but a 10-digit valid YYYYMMDDHH key is
    # accepted as a compact hour. For 2020s dates these ranges do not overlap.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = int(value)
        digits = str(abs(number))
        if len(digits) == 10:
            compact = _COMPACT_HOUR_RE.fullmatch(digits)
            if compact:
                year, month, day, hour = map(int, compact.groups())
                try:
                    return format_hour(datetime(year, month, day, hour, tzinfo=timezone.utc))
                except ValueError:
                    pass
        elif len(digits) == 8:
            date_match = _DATE_HOUR_RE.fullmatch(digits)
            if date_match:
                year, month, day = map(int, date_match.groups())
                try:
                    return format_hour(datetime(year, month, day, tzinfo=timezone.utc))
                except ValueError:
                    pass

        if abs(value) >= 10_000_000_000:  # milliseconds
            number = number // 1000
        if number % 3600 != 0:
            raise ValidationProblem("hour_not_whole", "Unix timestamp must be on a UTC hour boundary.")
        return format_hour(datetime.fromtimestamp(number, tz=timezone.utc))

    text = str(value).strip()
    if not text:
        raise ValidationProblem("hour_required", "hour is required.")

    # Compact YYYYMMDDHH.
    compact = _COMPACT_HOUR_RE.fullmatch(text)
    if compact:
        year, month, day, hour = map(int, compact.groups())
        try:
            dt = datetime(year, month, day, hour, tzinfo=timezone.utc)
        except ValueError as exc:
            raise ValidationProblem("invalid_hour", f"Invalid UTC hour {text!r}.") from exc
        return format_hour(dt)

    # Date only means midnight UTC.
    date_only = _DATE_HOUR_RE.fullmatch(text)
    if date_only:
        year, month, day = map(int, date_only.groups())
        try:
            dt = datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError as exc:
            raise ValidationProblem("invalid_hour", f"Invalid UTC date {text!r}.") from exc
        return format_hour(dt)

    candidate = text
    if candidate.endswith(("Z", "z")):
        candidate = candidate[:-1] + "+00:00"
    else:
        # An unencoded '+' in a query string becomes a space. Restore it as a
        # positive UTC offset when a trailing HH:MM offset is recognizable.
        offset_match = re.search(r"\s(\d{2}:?\d{2})$", candidate)
        if offset_match and re.search(r"[T ]\d{2}:\d{2}", candidate[: offset_match.start()]):
            offset = offset_match.group(1)
            if ":" not in offset:
                offset = offset[:2] + ":" + offset[2:]
            candidate = candidate[: offset_match.start()] + "+" + offset

    # Tolerate compact offsets and other ISO forms via fromisoformat.
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        match = _HOUR_KEY_RE.fullmatch(text)
        if not match:
            raise ValidationProblem("invalid_hour", f"Invalid UTC hour {text!r}.") from None
        year, month, day, hour = map(int, match.groups())
        try:
            dt = datetime(year, month, day, hour, tzinfo=timezone.utc)
        except ValueError as exc:
            raise ValidationProblem("invalid_hour", f"Invalid UTC hour {text!r}.") from exc
        return format_hour(dt)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return format_hour(_require_whole_hour(dt))


def format_hour(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")


def hour_to_unix(hour: str) -> int:
    dt = datetime.strptime(hour, "%Y-%m-%dT%H:00:00Z").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())
