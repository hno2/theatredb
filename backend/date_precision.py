"""Calendar-domain dates and their half-open intervals."""

import datetime as dt
import logging
import re
from collections.abc import Mapping
from enum import StrEnum

LOG = logging.getLogger(__name__)


class DatePrecision(StrEnum):
    """Resolution of a calendar-domain date."""

    YEAR = "year"
    MONTH = "month"
    DAY = "day"
    MINUTE = "minute"


_PATTERN = re.compile(
    r"(?P<year>\d{4})(?:-(?P<month>\d{2})(?:-(?P<day>\d{2})"
    r"(?:[Tt ](?P<hour>\d{2})(?::(?P<minute>\d{2})"
    r"(?::(?P<second>\d{2})(?:\.\d{1,6})?)?)?"
    r"(?P<offset>[Zz]|[+-]\d{2}:\d{2})?)?)?)?"
)


def parse_date(value: object) -> tuple[dt.datetime, DatePrecision]:
    """Parse ISO dates, retaining source wall time and truncating seconds."""
    if isinstance(value, dt.datetime):
        if value.utcoffset() is not None:
            LOG.warning("Calendar date timezone offset discarded; retaining source-local wall time: %s", value)
        return value.replace(second=0, microsecond=0, tzinfo=None), DatePrecision.MINUTE
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time()), DatePrecision.DAY
    if not isinstance(value, str) or not (match := _PATTERN.fullmatch(value)):
        raise ValueError(f"Invalid calendar date: {value!r}")
    parts = match.groupdict()
    precision = next(
        (
            precision
            for key, precision in (
                ("hour", DatePrecision.MINUTE),
                ("day", DatePrecision.DAY),
                ("month", DatePrecision.MONTH),
            )
            if parts[key] is not None
        ),
        DatePrecision.YEAR,
    )
    if parts["second"] is not None and int(parts["second"]) > 59:
        raise ValueError(f"Invalid calendar date: {value!r}")
    if parts["offset"] and parts["offset"].upper() != "Z":
        hours, minutes = (int(part) for part in parts["offset"][1:].split(":"))
        if hours > 23 or minutes > 59:
            raise ValueError(f"Invalid UTC offset: {parts['offset']}")
    if parts["offset"]:
        LOG.warning("Calendar date timezone offset discarded; retaining source-local wall time: %s", value)
    parsed = dt.datetime(
        int(parts["year"]),
        int(parts["month"] or 1),
        int(parts["day"] or 1),
        int(parts["hour"] or 0),
        int(parts["minute"] or 0),
    )
    return parsed, precision


def date_interval(value: dt.datetime, precision: DatePrecision) -> tuple[dt.datetime, dt.datetime | None]:
    """Return [start, end); None means the end exceeds datetime.max."""
    precision = DatePrecision(precision)
    if precision == DatePrecision.YEAR:
        start = value.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year + 1) if start.year < 9999 else None
    elif precision == DatePrecision.MONTH:
        start = value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1) if start.year < 9999 else None
        else:
            end = start.replace(month=start.month + 1)
    else:
        start = value.replace(second=0, microsecond=0)
        if precision == DatePrecision.DAY:
            start = start.replace(hour=0, minute=0)
            delta = dt.timedelta(days=1)
        else:
            delta = dt.timedelta(minutes=1)
        try:
            end = start + delta
        except OverflowError:
            end = None
    return start, end


def normalize_dates(value: object, fields: tuple[str, ...]) -> object:
    """Validate and normalize date/precision pairs in input mappings."""
    if not isinstance(value, Mapping):
        return value
    data = dict(value)
    for field in fields:
        raw = data.get(field)
        precision_name = f"{field}_precision"
        if raw is None:
            if data.get(precision_name) is not None:
                raise ValueError(f"{precision_name} requires {field}")
            data[field] = None
            data[precision_name] = None
            continue
        parsed, inferred = parse_date(raw)
        precision = DatePrecision(data.get(precision_name) or inferred)
        if list(DatePrecision).index(precision) > list(DatePrecision).index(inferred):
            raise ValueError(f"{field} lacks components required for {precision.value} precision")
        data[field] = date_interval(parsed, precision)[0]
        data[precision_name] = precision
    return data


def format_date(value: dt.datetime, precision: DatePrecision) -> str:
    """Format a date without exposing the filled-in interval start."""
    precision = DatePrecision(precision)
    return {
        DatePrecision.YEAR: f"{value.year:04d}",
        DatePrecision.MONTH: f"{value.month:02d}.{value.year:04d}",
        DatePrecision.DAY: f"{value.day:02d}.{value.month:02d}.{value.year:04d}",
        DatePrecision.MINUTE: f"{value.day:02d}.{value.month:02d}.{value.year:04d} {value.hour:02d}:{value.minute:02d}",
    }[precision]


def iso_date(value: dt.datetime, precision: DatePrecision) -> str:
    """Format a domain date as a precision-preserving ISO input."""
    return value.isoformat(timespec="minutes")[
        : {
            DatePrecision.YEAR: 4,
            DatePrecision.MONTH: 7,
            DatePrecision.DAY: 10,
            DatePrecision.MINUTE: 16,
        }[DatePrecision(precision)]
    ]


def intervals_overlap(
    left: tuple[dt.datetime, dt.datetime | None], right: tuple[dt.datetime, dt.datetime | None]
) -> bool:
    """Test overlap of two half-open intervals."""
    return (right[1] is None or left[0] < right[1]) and (left[1] is None or right[0] < left[1])
