"""dev_feed_get — the feeder's scheduled meals.

A feeder dispenses on its own clock, from the schedule it fetches here; the
server is not in the loop at feeding time. So the answer must always be a
well-formed schedule — an unidentified device gets an empty-but-valid one rather
than an error, which leaves it dispensing nothing instead of retrying forever.

Everything below matches 21 real cloud responses captured from a D4SH on
2026-08-12 (proxy capture, `proxy_http.jsonl`):

* Meal times — ``t`` in ``schedule[].it[]`` — are SECONDS since local midnight
  (``n_46560`` fires at 12:56:00), not the minutes every other schedule on
  these devices counts. The ``n_`` id suffix is that same seconds value.
* ``latest[]`` lists the concrete feeds firing TODAY OR TOMORROW (local days),
  nothing further out: a Sunday meal polled on Wednesday is in ``schedule``
  but never in ``latest``. Each entry's ``t`` is a live countdown — seconds
  from now, floored, recomputed on every poll. Ids: ``s_YYYYMMDD_SSSSS`` is a
  concrete instance of a recurring meal (suffix = the meal's ``t``),
  ``d_YYYYMMDD_SSSSS`` a one-off deferred feed.
* ``nextTick`` is the countdown of the LAST ``latest`` entry — re-poll when
  the final listed feed has fired — and the constant 86340 when ``latest`` is
  empty (all five empty-schedule captures), i.e. try again in about a day.
"""
from __future__ import annotations

import json
import time

from aiohttp import web

from petkit_local.http.handlers._common import request_device
from petkit_local.utils.coerce import to_int

#: What the cloud returns for ``nextTick`` when ``latest`` is empty — the
#: constant 86340 (23h59m) in every such capture, never a computed value.
_EMPTY_TICK = 86340
_EMPTY_GROUP = {"re": "1,2,3,4,5,6,7", "it": [], "itemJsonString": "[]"}

#: ``itemJsonString`` serialization matching the cloud byte-for-byte: keys in
#: alphabetical order (``a1``, ``a2``, ``id``, ``t``), no whitespace.
_ITEM_JSON = dict(separators=(",", ":"), sort_keys=True)

#: LOCAL PATCH (morbo): the single-hopper D4H schedule item carries a single
#: `a` (amount), not the D4SH's `a1`/`a2` pair. Confirmed against a live
#: api-ru.petkit.cn D4H capture 2026-09-16: {"id":"n_55140","t":55140,"a":20},
#: same `a` in latest[]. `a` is portions x10 (a=10 -> 1 portion; the device
#: divides by its own constant, as the manual feed path does with `amount`).
#: Live-confirmed on the hardware: a=10 dispensed one portion (~10 g).
#: Storage stays in `a1` (portions); the translation happens only on the wire,
#: and `render_feed` below is the ONE place it happens — every emitter of a
#: feed schedule (HTTP `dev_feed_get`, the MQTT `data_get` answer, and both
#: `property.set{feed}` pushes) goes through it, so the shapes cannot drift.
_SINGLE_HOPPER_FEEDERS = {"d4h"}
_D4H_AMOUNT_PER_PORTION = 10
#: The firmware keeps feed amounts in a single byte (`sb`, see
#: `ha/commands.py::_feed`); anything above wraps on the device.
_AMOUNT_BYTE_MAX = 255


def _local_midnight(now: float, day_offset: int) -> float:
    """Local midnight ``day_offset`` days after the day containing ``now``.

    Re-localized through ``mktime`` rather than ``+ 86400`` — DST makes days
    23 and 25 hours long.
    """
    lt = time.localtime(now)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + day_offset,
                        0, 0, 0, 0, 0, -1))


def migrate_minute_schedule(feed: dict) -> bool:
    """One-time repair of a schedule saved by the 2.0.0/2.0.1 panel.

    That editor stored meal ``t`` in MINUTES since midnight; the wire unit is
    SECONDS (confirmed against the cloud 2026-08-12), so a stored 1082 had the
    device feeding at 00:18 instead of 18:02. Every value the old editor could
    produce is below 1440, so a schedule whose meals ALL are — and that no
    current save has stamped ``v: 2`` — is minute data: convert, and rewrite
    each id to the cloud's ``n_<seconds>`` scheme. A post-fix schedule whose
    meals genuinely all fall before 00:24 carries the stamp and is left alone.
    """
    if feed.get("v") == 2:
        return False
    meals = [it for g in feed.get("schedule") or [] if isinstance(g, dict)
             for it in g.get("it") or [] if isinstance(it, dict)]
    feed["v"] = 2
    if not meals or any(not isinstance(it.get("t"), int)
                        or not 0 <= it["t"] < 1440 for it in meals):
        return False
    for it in meals:
        it["t"] *= 60
        it["id"] = f"n_{it['t']}"
    for g in feed.get("schedule") or []:
        if isinstance(g, dict) and "it" in g:
            g["itemJsonString"] = json.dumps(g["it"], **_ITEM_JSON)
    return True


def _build_latest(feed: dict, now: float) -> list[dict]:
    """Compute ``latest[]``: every feed firing today or tomorrow, local days.

    Both kinds land here — ``s_`` instances of recurring meals and ``d_``
    deferred feeds — sorted by countdown. ``t`` is ``floor(fire - now)``,
    which is why the cloud's countdowns keep landing on :59.

    The two-day window is what 21 captures show: meals four days out never
    appeared, a deferred feed 27 hours out (tomorrow evening) always did. A
    recurring meal can therefore appear twice, once per day, when both days
    match its weekdays — no capture contradicts it and the device re-polls
    after the last entry anyway (``nextTick``).

    Expired deferred feeds are pruned from ``feed`` as a side effect.
    """
    result = []
    cutoff = _local_midnight(now, 2)  # end of tomorrow

    for day_offset in (0, 1):
        day_start = _local_midnight(now, day_offset)
        # PetKit weekday: Sunday=1 .. Saturday=7; tm_wday: Monday=0.
        pk_wd = (time.localtime(day_start + 43200).tm_wday + 2) % 7 or 7
        date_str = time.strftime("%Y%m%d", time.localtime(day_start + 43200))
        for group in feed.get("schedule") or []:
            if not isinstance(group, dict):
                continue
            days = str(group.get("re", "")).split(",")
            if str(pk_wd) not in (d.strip() for d in days):
                continue
            for item in group.get("it") or []:
                if not isinstance(item, dict):
                    continue
                t_secs = item.get("t", 0)
                fire = day_start + t_secs
                if not now < fire < cutoff:
                    continue
                result.append({
                    "id": f"s_{date_str}_{t_secs}",
                    "t": int(fire - now),
                    "a1": item.get("a1", 0),
                    "a2": item.get("a2", 0),
                })

    deferred = feed.get("deferred") or []
    remaining = []
    for d in deferred:
        if not isinstance(d, dict):
            continue
        fire_at = d.get("fire_at", 0)
        if fire_at <= now:
            continue
        remaining.append(d)
        if fire_at >= cutoff:
            continue  # kept for later, but not today-or-tomorrow yet
        result.append({
            "id": d.get("id", ""),
            "t": int(fire_at - now),
            "a1": d.get("a1", 0),
            "a2": d.get("a2", 0),
        })
    if len(remaining) != len(deferred):
        feed["deferred"] = remaining

    result.sort(key=lambda x: x["t"])
    return result


def _compute_next_tick(latest: list[dict]) -> int:
    """The cloud's rule, exact in all 21 captures: the LAST countdown, or the
    86340 constant when nothing is coming up."""
    if not latest:
        return _EMPTY_TICK
    return max(entry["t"] for entry in latest)


def is_single_hopper(device) -> bool:
    """Whether this feeder takes the D4H single-``a`` meal shape on the wire."""
    return (getattr(device, "device_type", "") or "").lower() in _SINGLE_HOPPER_FEEDERS


def _single_hopper_amount(item: dict) -> int:
    """The scalar ``a`` for one D4H meal, from whatever shape is stored.

    Storage normally holds ``a1`` in portions (the panel and cleaner write
    that), so ``a`` is ``a1 x 10``. A stored item may instead already carry
    ``a`` — the device's own shape, which `mqtt/bridge.py` stores verbatim from
    a property post — and that is passed through unscaled rather than read as
    zero: a zero here means a meal that runs and dispenses nothing. Coerced
    with `to_int` so a string or junk value degrades to 0 instead of a 500 on
    the device's poll, and clamped to the byte the firmware stores it in.
    """
    a1 = to_int(item.get("a1"), None)
    if a1 is not None:
        amount = a1 * _D4H_AMOUNT_PER_PORTION
    else:
        amount = to_int(item.get("a"), 0)
    return max(0, min(_AMOUNT_BYTE_MAX, amount))


def _single_hopper_item(item: dict) -> dict:
    return {"id": item.get("id"), "t": item.get("t"),
            "a": _single_hopper_amount(item)}


def render_feed(device, feed: dict, now: float, *, item_json: bool = True) -> dict:
    """The ``{schedule, nextTick, latest}`` body every feed-schedule emitter sends.

    Four call sites send this and MUST agree, because the device reads them
    interchangeably: `handle_feed_get` (HTTP ``dev_feed_get``), the MQTT
    ``data_get`` answer for the same name (`mqtt/bridge.py`), and the two
    ``property.set{feed}`` pushes (`web/api/schedules.py`, `ha/commands.py`).

    ``item_json`` selects the group shape: True is the GET answer, where each
    group carries the cloud's ``itemJsonString`` twin; False is the push, which
    the cloud was captured sending as bare ``{re, it}`` groups.

    Non-D4H feeders get exactly what upstream sent — the stored groups verbatim
    (``itemJsonString`` refreshed in place, as before) or ``{re, it}`` copies —
    so their bytes do not change. A D4H gets each meal rewritten to the
    single-``a`` shape, in ``it``, ``itemJsonString`` and ``latest`` alike.
    """
    latest = _build_latest(feed, now)
    next_tick = _compute_next_tick(latest)

    if not is_single_hopper(device):
        if item_json:
            for group in feed.get("schedule", []):
                if isinstance(group, dict) and "it" in group:
                    group["itemJsonString"] = json.dumps(group["it"], **_ITEM_JSON)
            schedule = feed.get("schedule", [_EMPTY_GROUP])
        else:
            schedule = [{"re": g.get("re", ""), "it": g.get("it", [])}
                        for g in feed.get("schedule", []) if isinstance(g, dict)]
        return {"schedule": schedule, "nextTick": next_tick, "latest": latest}

    schedule = []
    for group in feed.get("schedule", []) or []:
        if not isinstance(group, dict):
            continue
        items = [_single_hopper_item(it) for it in group.get("it") or []
                 if isinstance(it, dict)]
        out = {"re": group.get("re", ""), "it": items}
        if item_json:
            out["itemJsonString"] = json.dumps(items, **_ITEM_JSON)
        schedule.append(out)
    if not schedule and item_json:
        schedule = [_EMPTY_GROUP]
    return {
        "schedule": schedule,
        "nextTick": next_tick,
        "latest": [_single_hopper_item(entry) for entry in latest],
    }


async def handle_feed_get(request: web.Request) -> web.Response:
    """Return the device's stored feeding schedule with live countdowns.

    Returns:
        ``{"result": {"schedule": [...], "nextTick": N, "latest": [...]}}``.
        Structure matches the real cloud's response 1:1.

    LOCAL PATCH: a single-hopper D4H is served a single ``a`` per meal instead
    of the D4SH ``a1``/``a2`` pair — see `render_feed`, which is also what the
    three other feed-schedule emitters send. Non-D4H devices take the original
    path verbatim.
    """
    device = request_device(request)

    if not device or not device.config.get("feed_schedule"):
        return web.json_response({
            "result": {
                "schedule": [_EMPTY_GROUP],
                "nextTick": _EMPTY_TICK,
                "latest": [],
            }
        })

    feed = device.config["feed_schedule"]
    if not isinstance(feed, dict):
        return web.json_response({"result": feed})

    if migrate_minute_schedule(feed):
        request.app["registry"].save()

    return web.json_response({"result": render_feed(device, feed, time.time())})
