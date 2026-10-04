"""Local-time helpers: what "today" means, and what offset to tell a device.

Everything in this codebase stores UTC epochs, and that is correct -- the
devices report correct UTC and nothing here changes that. The bug this module
exists to fix is one layer up: a *day* was being cut at UTC midnight while the
box sits in someone's house. On a UTC+2 install that files every event between
local midnight and 02:00 under the previous day, which measured out at 50 of
268 events in the reference corpus, including six complete after-midnight
toilet visits.

Where "local" comes from
------------------------
The Supervisor sets the add-on container's timezone from Home Assistant's own
configuration (`TZ=Europe/Warsaw` on the reference install), so the process
already knows. `datetime.astimezone()` on a NAIVE datetime interprets it as
local wall-clock time and attaches the offset that was in force *on that date*
-- which is what makes the DST handling below work without a tz database
dependency or a configured zone name.
"""
from __future__ import annotations

import math
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: The wire format the panel's date picker and `?date=` parameter both use.
DATE_FORMAT = "%Y-%m-%d"


def fixed_offset_tz(offset_hours: float | None) -> timezone:
    """A fixed-offset tzinfo for `offset_hours` east of UTC, UTC if unusable.

    Rounded to whole minutes, because that is all `%z` can say without
    growing a seconds field the wire format does not have (`5.75` -> `+0545`).
    Never raises: the value can be a panel override or a device report, and a
    NaN, an infinity or anything a `timezone` refuses (|offset| >= 24h) falls
    back to UTC rather than taking a device reply down with it.
    """
    if offset_hours is None or not math.isfinite(offset_hours):
        return timezone.utc
    minutes = round(offset_hours * 60)
    if not -24 * 60 < minutes < 24 * 60:
        return timezone.utc
    return timezone(timedelta(minutes=minutes))


def cloud_timestamp(when: float | None = None,
                    offset_hours: float | None = None) -> str:
    """An instant in the format PetKit's cloud puts on the wire.

    `2026-07-15T05:19:42.000+0000` — milliseconds, and a `±HHMM` offset with no
    colon. NOT `datetime.isoformat()`, which produces `+00:00` and would be a
    different string to whatever parses it on the device. That SHAPE was
    confirmed against captured `dev_state_report` and `dev_schedule_get`
    replies; their `+0000` most likely came from a UTC account.

    `offset_hours` renders the same instant in that fixed offset instead of
    UTC (`3` -> `...T18:26:53.278+0300`). The cloud does this: for a
    Europe/Moscow account all 205 logged `time` fields it sent in state-report
    replies (MQTT `user/get`, `{"msgType":0,...,"type":"<type>_state_report"}`,
    W7H and D4H, 2026-09-16..10-04) carry `+0300`, never `+0000`.
    It matters if the firmware takes its runtime UTC offset from this string's
    `%z` — a HYPOTHESIS, not proven: the device's reported `timezone` drifted
    3.0 -> 0.0 within hours of being served `+0000` and back to 3.0 once proxy
    mode relayed the cloud's `+0300`, and the camera watermark followed.
    The evidence is the cloud's MQTT push, which we do not send; the HTTP
    `dev_state_report` reply carries the same field and is matched for
    parity. Confirm on a live device — which channel, if either — before
    treating it as settled. Absent, the default stays UTC.
    """
    tz = fixed_offset_tz(offset_hours)
    now = datetime.fromtimestamp(when, tz) if when is not None \
        else datetime.now(tz)
    return (now.strftime("%Y-%m-%dT%H:%M:%S.")
            + f"{now.microsecond // 1000:03d}"
            + now.strftime("%z"))


def local_offset_hours(when: float | None = None) -> float:
    """Hours east of UTC in force locally, e.g. 2.0 for Warsaw in summer.

    This is the unit PetKit's own API uses for its `timezone` field, which is
    what we report to a device. Returns 0.0 if the platform will not tell us.
    """
    moment = datetime.fromtimestamp(when) if when is not None else datetime.now()
    offset = moment.astimezone().utcoffset()
    return offset.total_seconds() / 3600 if offset is not None else 0.0


def zone_for_locale(locale: str | None) -> ZoneInfo | None:
    """The IANA zone a device's `locale` names, or None (rules as below)."""
    if not isinstance(locale, str) or "/" not in locale:
        return None
    try:
        return ZoneInfo(locale)
    except (ZoneInfoNotFoundError, ValueError):
        # Unknown zone, or a name the OS tz database does not carry.
        return None


def offset_hours_for_locale(locale: str | None,
                            when: float | None = None) -> float | None:
    """Current UTC offset in hours for an IANA zone name, or None.

    A device reports its own zone as a NAME (`locale: "Europe/Warsaw"`), and a
    name is a better answer than a number for the `timezone` field two ways: it
    is unambiguous, and — unlike an offset frozen at provisioning — it moves
    with DST, so it is right in both halves of the year. A device that only ever
    got a locale and never a numeric offset reports the offset as 0, which is
    how a box plainly in `Europe/Warsaw` ends up telling us it is in UTC; this
    recovers the real offset from the name it did send.

    Returns None for an absent, non-string, or unknown zone (and for a bare name
    with no `/`, which no real zone this matters for lacks — `UTC` included,
    whose 0 the numeric path already yields) so the caller can fall back.
    """
    tz = zone_for_locale(locale)
    if tz is None:
        return None
    moment = datetime.fromtimestamp(when, tz) if when is not None \
        else datetime.now(tz)
    offset = moment.utcoffset()
    return offset.total_seconds() / 3600 if offset is not None else None


def parse_date(date_str: str | None) -> date | None:
    """Parse a `YYYY-MM-DD` string, or None if it is absent or malformed.

    Never raises: the value reaches us from a query string.
    """
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, DATE_FORMAT).date()
    except (TypeError, ValueError):
        return None


def local_day_bounds(date_str: str | None = None,
                     now: float | None = None) -> tuple[float, float, str]:
    """The UTC epoch span of one LOCAL calendar day.

    Args:
        date_str: The day to bound, `YYYY-MM-DD`. Absent or unparseable falls
            back to today, matching the panel's existing contract.
        now: Epoch to treat as "now" when falling back. For tests.

    Returns:
        `(start_ts, end_ts, day_label)` -- start inclusive, end exclusive, and
        the `YYYY-MM-DD` actually used so a caller can echo it back.

    The end is midnight of the NEXT day re-localized, never `start + 86400`.
    Those differ twice a year: Europe/Warsaw has a 23-hour day when DST starts
    and a 25-hour one when it ends, so the fixed-86400 form would silently drop
    an hour of events in spring and double-count one in autumn.
    """
    day = parse_date(date_str)
    if day is None:
        moment = datetime.fromtimestamp(now) if now is not None else datetime.now()
        day = moment.date()

    midnight = datetime(day.year, day.month, day.day)
    start = midnight.astimezone()
    end = (midnight + timedelta(days=1)).astimezone()
    return start.timestamp(), end.timestamp(), day.strftime(DATE_FORMAT)


def local_day_start(now: float | None = None) -> float:
    """Epoch of the most recent local midnight -- the "today" of daily counts."""
    start, _, _ = local_day_bounds(now=now if now is not None else time.time())
    return start
