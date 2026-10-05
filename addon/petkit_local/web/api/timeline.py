"""The Timeline tab: one local day of grouped sessions, and one event's detail.

`api_timeline` groups the day in Python at query time (`events/sessions.py`) and
renders every label through `events/decode.py`, which is why the device codename
has to reach every call — a numeric code means different things on a litter box
and on a feeder.

`api_event_detail` backs the Debug info expander on a card. It is served
per-event rather than inlined above because every event carries a state snapshot
averaging 1.2 kB.
"""
from __future__ import annotations

from typing import Any

from aiohttp import web

from petkit_local.events import codes, decode, ingest
from petkit_local.utils.coerce import to_int
from petkit_local.utils.const import device_display_name
from petkit_local.utils.timeutil import local_day_bounds, local_offset_hours
from petkit_local.web.api._common import _pet_fields, _pets_by_id
from petkit_local.web.api.media import _has_media, _session_media_urls


async def api_timeline(request: web.Request) -> web.Response:
    """Grouped visit sessions for one LOCAL day, with their media.

    `GET /api/timeline?device=&filter=&date=`; grouping is
    events/sessions.py::group_sessions. Answers
    `{"date", "tz_offset", "counts", "sessions"}`: `counts` is per filter chip
    and is computed BEFORE filtering, so the chips keep showing their totals
    while one of them is active. Sessions are grouped in Python at query time
    rather than read pre-grouped from SQL. An unparseable `date` falls back to
    today rather than erroring — the response echoes the day it actually used.

    The day is cut at LOCAL midnight, not UTC. Cutting it at UTC filed every
    event between local midnight and the UTC offset under the previous day —
    50 of the 268 events in the reference corpus, including six complete
    after-midnight toilet visits. `tz_offset` is reported so the panel's date
    picker agrees with the server about which day "today" is.
    """
    store = request.app.get("event_store")
    if store is None:
        return web.json_response({"error": "event store not available"}, status=400)

    device = request.query.get("device")
    did = to_int(device, None) if device else None
    pet = request.query.get("pet")
    pet_id = to_int(pet, None) if pet else None
    filt = request.query.get("filter", "all")

    start_ts, end_ts, day_label = local_day_bounds(request.query.get("date", ""))

    rows = await store.query_timeline(device_id=did, start_ts=start_ts, end_ts=end_ts, limit=2000)
    sessions = ingest.group_sessions(rows)
    # Narrowing by pet happens BEFORE the counts, like the device filter and
    # unlike the chips: picking a cat means "this is her timeline", so the chip
    # badges should count her events, not the household's. Attribution is a
    # property of the grouped session (it can fill down from a detail event), so
    # this cannot move into the SQL query.
    if pet_id is not None:
        sessions = [s for s in sessions if s.get("pet_id") == pet_id]
    counts = ingest.filter_counts(sessions)
    filtered = [s for s in sessions if ingest.matches_filter(s, filt)]

    media_root = request.app["cfg"].get("media_root", "")
    reg = request.app["registry"]
    # Resolved once for the whole day rather than per card: a household has a
    # handful of pets, and a card carrying a bare `pet_id` is what made the
    # Timeline silent about which cat it was showing.
    pets = await _pets_by_id(request)
    payload: list[dict[str, Any]] = []
    for s in filtered:
        dev = reg.get(s.get("device_id"))
        # The codename decides what a numeric code MEANS -- 2 is a cleared
        # fault on a litter box and a completed meal on a feeder -- so it has
        # to reach every label call. The stored row is preferred over the live
        # registry so a re-typed or unregistered device still reads correctly.
        device_type = s.get("device_type") or (dev.device_type if dev else None)
        sub_events: list[dict[str, Any]] = []
        for e in s.get("sub_events", []):
            slots = _session_media_urls(e.get("media") or [], media_root)
            content = e.get("content") or {}
            sub_events.append({
                "id": e.get("id"), "event_type": e.get("event_type"), "ts": e.get("ts"),
                "label": decode.event_label(e.get("event_type"), content, device_type,
                                            e.get("state")),
                # Low-level mechanism/start steps the official app never shows;
                # the UI collapses these behind an expander.
                "detail": e.get("detail", False),
                "media": slots if _has_media(slots) else None,
            })
        content = s.get("content") or {}
        payload.append({
            "kind": s["kind"], "id": s["id"], "related_event": s.get("related_event"),
            "device_id": s.get("device_id"),
            "device_name": device_display_name(dev.device_type) if dev else "",
            "ts": s.get("ts"),
            # What to SHOW. For a visit this is when the pet entered; `ts` is
            # when the closing report reached us, several seconds later.
            "display_ts": s.get("display_ts") or s.get("ts"),
            "pet_id": s.get("pet_id"), "event_type": s.get("event_type"),
            # "weight" when the pet was picked by the scale, not by a face.
            "pet_source": s.get("pet_source"),
            **_pet_fields(pets, s.get("pet_id")),
            # A visit builds its own summary line (duration/weight); every
            # other card is titled by its event's label, now decoded from the
            # content too, so a lone cleaning reads "Manual cleaning completed"
            # rather than a generic "Cleaning done".
            "label": None if s["kind"] == "visit"
                     else decode.event_label(s.get("event_type"), content,
                                             device_type, s.get("state")),
            "event_kind": s.get("event_kind"), "duration_sec": s.get("duration_sec"),
            "weight": s.get("weight"),
            # Confirmed facts worth putting on the card itself: waste weight,
            # litter level, a named fault. Everything else stays in Debug info.
            "bits": decode.summary_bits(s.get("event_type"), content),
            "sub_events": sub_events,
            "media": _session_media_urls(s.get("media") or [], media_root),
        })

    return web.json_response({
        "date": day_label,
        # Seconds east of UTC for the day just returned, so the client's date
        # picker cuts "today" where the server did.
        "tz_offset": int(round(local_offset_hours(start_ts) * 3600)),
        "counts": counts,
        "sessions": payload,
    })


async def api_event_detail(request: web.Request) -> web.Response:
    """Everything the database holds about one stored event.

    `GET /api/timeline/{id}` — backs the Timeline's "Debug info" expander.
    Answers `{event, code, decoded, content, state}`:

    * `event` — the row's own columns, including the ones the timeline drops
      (`event_uid`, `source`, `parent_event`, `score`).
    * `code` — what `events/codes.py` knows about this event type: its
      evidence grade, the firmware function behind it, and any note recording
      where the firmware RE and our captures disagree.
    * `decoded` — every `content` field rendered by `events/decode.py`,
      including ones we do not claim to understand.
    * `content` / `state` — the raw stored payloads, so our interpretation can
      always be checked against the original.

    Served per-event rather than inlined into `/api/timeline` because every
    event carries a state snapshot averaging 1.2 kB; on a busy day that is
    ~100 kB of payload re-fetched on every websocket refresh, to show one row
    the user may never open.
    """
    store = request.app.get("event_store")
    if store is None:
        return web.json_response({"error": "event store not available"}, status=400)

    event_id = to_int(request.match_info.get("id"), None)
    if event_id is None:
        return web.json_response({"error": "invalid event id"}, status=400)

    row = await store.get_event(event_id)
    if row is None:
        return web.json_response({"error": "no such event"}, status=404)

    event_type = row.get("event_type")
    device_type = row.get("device_type")
    content = ingest.content_of_row(row)
    state = ingest.state_of_row(row)
    code = codes.lookup(event_type, device_type)

    return web.json_response({
        "event": {
            "id": row.get("id"), "event_uid": row.get("event_uid"),
            "event_type": event_type, "event_kind": row.get("event_kind"),
            "label": decode.event_label(event_type, content, device_type),
            "ts": row.get("ts"), "created_at": row.get("created_at"),
            "source": row.get("source"), "device_id": row.get("device_id"),
            "device_type": device_type, "pet_id": row.get("pet_id"),
            # Both, and they differ on purpose: `pet_ref` is what the device
            # claimed, `pet_id` is what resolved to one of our rows. A ref with
            # no id is the signal that the box is still matching against faces
            # cached from PetKit's cloud.
            "pet_ref": row.get("pet_ref"),
            "pet_source": row.get("pet_source"),
            **_pet_fields(await _pets_by_id(request), row.get("pet_id")),
            "score": row.get("score"),
            "related_event": row.get("related_event"),
            "parent_event": row.get("parent_event"),
        },
        "code": {
            "code": event_type, "label": code.label, "kind": code.kind,
            "grade": code.grade, "role": code.role, "detail": code.detail,
            "firmware": code.firmware, "note": code.note,
        } if code else None,
        "decoded": [
            # `epoch` tells the panel to render this row's `raw` itself. The
            # server formats in the CONTAINER's timezone and the Timeline
            # headers format in the BROWSER's, so an install whose container
            # runs UTC showed one instant twice, hours apart, in adjacent rows.
            {"key": f.key, "label": f.label, "raw": f.raw,
             "text": f.text, "grade": f.grade, "note": f.note,
             "epoch": f.key in decode.EPOCH_FIELDS}
            for f in decode.decode_content(event_type, content, device_type)
        ],
        "content": content,
        "state": state,
    })
