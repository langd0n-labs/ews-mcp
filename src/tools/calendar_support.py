"""Calendar input validation and Exchange recurrence conversion."""
from datetime import date, datetime
import os
import pytz
from zoneinfo import ZoneInfo
from exchangelib import EWSDate, EWSDateTime, EWSTimeZone
from exchangelib.recurrence import Recurrence, DailyPattern, WeeklyPattern
from ..exceptions import ValidationError

TIMEZONE_SCHEMA = {
    "type": "string",
    "description": "IANA timezone, e.g. America/New_York. Numeric offsets are not zones. Naive times use this zone; ambiguous times require a matching ISO offset. Nonexistent local times are rejected.",
}
RECURRENCE_SCHEMA = {
    "type": ["object", "null"],
    "description": "Daily or weekly recurrence. null removes recurrence (series scope only on update). Omit to preserve. Other patterns are unsupported.",
    "properties": {
        "frequency": {"type": "string", "enum": ["daily", "weekly"]},
        "interval": {"type": "integer", "minimum": 1, "maximum": 99},
        "weekdays": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 7}, "minItems": 1},
        "start": {"type": "string", "description": "First local date, YYYY-MM-DD"},
        "end": {"type": "string", "description": "Last local date; mutually exclusive with count"},
        "count": {"type": "integer", "minimum": 1, "maximum": 999},
    },
    "required": ["frequency", "start"],
    "additionalProperties": False,
}


def calendar_datetime(value, timezone=None, *, match_offset=True):
    """Parse local time without losing its named zone or silently choosing DST."""
    try:
        canonical = EWSTimeZone(timezone or os.environ.get("TIMEZONE", os.environ.get("TZ", "UTC")))
        # CalendarItem.clean compares start/end directly. Separate zone instances
        # make Python compare UTC instants across folds, not identical-zone wall times.
        # no_cache bypasses EWSTimeZone.__new__, so copy its validated EWS mapping.
        zone = EWSTimeZone.no_cache(canonical.key)
        zone.ms_id, zone.ms_name = canonical.ms_id, canonical.ms_name
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            # pytz's strict localization rejects both folds and gaps.
            localized = pytz.timezone(zone.key).localize(parsed, is_dst=None)
            parsed = localized.astimezone(ZoneInfo(zone.key))
        else:
            converted = parsed.astimezone(ZoneInfo(zone.key))
            if timezone and match_offset and (converted.replace(tzinfo=None) != parsed.replace(tzinfo=None)
                             or converted.utcoffset() != parsed.utcoffset()):
                raise ValueError("ISO offset does not match local time in the named timezone")
            parsed = converted
        return EWSDateTime(parsed.year, parsed.month, parsed.day, parsed.hour, parsed.minute,
                           parsed.second, parsed.microsecond, tzinfo=zone, fold=parsed.fold)
    except Exception as exc:
        raise ValidationError(f"Invalid calendar time or timezone: {exc}") from exc


def recurrence_from_input(value):
    if value is None:
        return None
    try:
        if not isinstance(value, dict) or set(value) - set(RECURRENCE_SCHEMA["properties"]):
            raise ValueError("Unknown recurrence fields")
        frequency = value["frequency"]
        interval = value.get("interval", 1)
        if type(interval) is not int or not 1 <= interval <= 99:
            raise ValueError("interval must be an integer from 1 to 99")
        if frequency == "daily":
            if "weekdays" in value:
                raise ValueError("weekdays requires weekly frequency")
            pattern = DailyPattern(interval=interval)
        elif frequency == "weekly":
            days = value.get("weekdays")
            if not days or any(type(day) is not int or not 1 <= day <= 7 for day in days):
                raise ValueError("weekly recurrence requires weekdays (1=Monday to 7=Sunday)")
            pattern = WeeklyPattern(interval=interval, weekdays=days)
        else:
            raise ValueError("Only daily and weekly recurrence are supported")
        start = EWSDate.from_date(date.fromisoformat(value["start"]))
        end = EWSDate.from_date(date.fromisoformat(value["end"])) if "end" in value else None
        count = value.get("count")
        if "count" in value and (type(count) is not int or not 1 <= count <= 999):
            raise ValueError("count must be an integer from 1 to 999")
        if end and (count is not None or end < start):
            raise ValueError("end must be on or after start and cannot be combined with count")
        result = Recurrence(pattern=pattern, start=start, end=end, number=count)
        result.clean()
        return result
    except Exception as exc:
        raise ValidationError(f"Invalid recurrence: {exc}") from exc


def calendar_metadata(item):
    kind = item.type
    master_id = item.id if kind == "RecurringMaster" else None
    if kind in ("Occurrence", "Exception"):
        master = item.recurring_master()
        master.refresh()
        master_id = master.id
    return {
        "calendar_item_type": kind,
        "series_master_id": master_id,
        "uid": item.uid,
        "original_start": item.original_start.isoformat() if item.original_start else None,
        "timezone": getattr(item._start_timezone or getattr(item.start, "tzinfo", None), "key", None),
    }
