"""Visit sessions and the Timeline's filter chips.

Where `events/normalize.py` turns a transport payload into a row, this module
reads those rows back — already stored, already classified — and groups them
into the cards the panel renders: a visit with its cleaning cycle folded
underneath, an "appeared" with its recognition result, a maintenance cycle with
its mechanism steps. Nothing here parses a transport; the only device payload it
touches is the `content`/`state` snapshot a stored row already carries.

Sessions are NOT persisted pre-grouped. `group_sessions` runs at query time, so
the grouping heuristic — which is all it is, and which real captures keep
correcting — stays in readable Python rather than in SQL.
"""
from __future__ import annotations

from petkit_local.events import codes
from petkit_local.events.normalize import _as_dict, is_detail_event, pet_weight_of
from petkit_local.utils.coerce import to_float, to_int

# --- visit-session grouping (Timeline tab) ------------------------------

# How long after a visit a cleaning/deodorizing event still counts as "part
# of it" for the session card, when it isn't already tagged with the same
# related_event.
SUB_EVENT_WINDOW_SEC = 600

# The same idea for a cleaning card, and deliberately much tighter. The only
# known member is the LED illuminator's second report: it fires twice per
# cleaning and the device files the second one as its OWN episode with a fresh
# event_id and no link back (see codes.py "17"/"light_over"). The ~115s in that
# note is the gap between the two episodes' START times; both reports REACH us
# when the cycle ends, 0.1s apart on the T5 that prompted this. So the window
# only has to absorb reporting jitter, and staying far below the visit window
# keeps a maintenance cycle from swallowing a later one. Matched nearest-first
# and symmetric, because which of the two lands first is a race.
CYCLE_TAIL_WINDOW_SEC = 120


def _content_of(event: dict) -> dict:
    """Decode a stored event's `content_json` (empty dict if absent/damaged)."""
    return _as_dict(event.get("content_json"))


def content_of_row(event: dict) -> dict:
    """The device `content` a stored event row carries, decoded.

    Public because the panel's per-event endpoint needs it and reaching into
    `_content_of` from outside is what a leaky abstraction looks like. Never
    raises: a damaged payload decodes to `{}`, including the malformed-JSON
    case the device is known to emit (see `_repair_bare_values`).
    """
    return _content_of(event)


def state_of_row(event: dict) -> dict:
    """The device state snapshot a stored event row carries, decoded."""
    return _as_dict(event.get("state_json"))


def _weight_of(event: dict) -> float | None:
    """The pet weight this event reported, in whatever unit the device sent."""
    return pet_weight_of(_content_of(event))


def _duration_of(anchor: dict, pet_in: dict | None) -> float | None:
    """Visit duration. Preferred source: the anchor's own `time_in`/
    `time_out` (confirmed present on a real T5's event_type "10" visit
    summary — the device already computed the span itself, so no pairing
    heuristic needed). Falls back to a `pet_in`/`pet_out` timestamp pairing
    for MQTT-style semantic event_types, where no such summary field exists."""
    c = _content_of(anchor)
    time_in = to_float(c.get("time_in"), None)
    time_out = to_float(c.get("time_out"), None)
    if time_in is not None and time_out is not None:
        return max(0.0, time_out - time_in)
    if pet_in is not None and pet_in is not anchor and anchor.get("ts") and pet_in.get("ts"):
        return max(0.0, anchor["ts"] - pet_in["ts"])
    return None


def _started_at(anchor: dict, pet_in: dict | None) -> float | None:
    """When the visit BEGAN, for display. Same source order as `_duration_of`.

    A visit is anchored on the `pet_out` summary, which the device can only
    send once the visit is over -- so the anchor's `ts` is the moment the
    report ARRIVED, three to five seconds after the pet had already left.
    Showing that as the event's time put the card's header later than the
    steps listed underneath it, and answered "when did the cat use the box?"
    with the end rather than the beginning.

    `content.time_in` is the device's own entry timestamp and is what the
    official app shows. `start_time` and `mark` are the same idea for every
    OTHER kind of episode: only a toilet visit summary carries `time_in`, so
    looking for it alone meant a `pet_detect` card -- and after the feeding and
    drinking cards were added, every one of those too -- headed itself with the
    moment the report ARRIVED. Measured on a reported T6: `start_time` and
    `mark` both said 14:19:58 and the header said 14:20:19, 21 seconds of
    network and ingest showing as the time the cat was seen.

    The `pet_in` pairing is below them because it is an arrival time as well,
    just a closer one. Returns None when the device named no time at all,
    leaving the caller to fall back to arrival.
    """
    content = _content_of(anchor)
    for key in ("time_in", "start_time", "mark"):
        value = to_float(content.get(key), None)
        if value is not None and value > 0:
            return value
    if pet_in is not None and pet_in is not anchor and pet_in.get("ts"):
        return pet_in["ts"]
    return None


def _session_from_visit(anchor: dict, pet_in: dict | None, media: list[dict]) -> dict:
    """Build one Timeline session card from the event that anchors it.

    Shape (what web/panel.py and the API consume)::

        {kind, id, related_event, device_id, device_type, ts, display_ts,
         pet_id, event_type, event_kind, duration_sec, weight, content,
         sub_events: [], media: [...]}

    `ts` stays the report's ARRIVAL time -- day bucketing, sort order and the
    sub-event proximity window all key off it, and changing it would move
    events between days. `display_ts` is the time to SHOW: for a visit that is
    when the pet entered, which is several seconds earlier (see `_started_at`).

    `sub_events` starts empty — `group_sessions` fills it — and `media` is
    copied, not aliased, so attaching an episode's media later cannot mutate
    the caller's list. `content` is the anchor's decoded payload, carried so
    the API can title the card from what the device actually reported rather
    than from the bare code.
    """
    duration = _duration_of(anchor, pet_in)
    return {
        "kind": "visit",
        "id": anchor["id"],
        "related_event": anchor.get("related_event"),
        "device_id": anchor.get("device_id"),
        "device_type": anchor.get("device_type"),
        "ts": anchor.get("ts"),
        "display_ts": _started_at(anchor, pet_in) or anchor.get("ts"),
        "pet_id": anchor.get("pet_id"),
        "pet_source": anchor.get("pet_source"),
        "event_type": anchor.get("event_type"),
        "event_kind": codes.KIND_TOILET,
        "duration_sec": duration,
        "weight": _weight_of(anchor),
        "content": _content_of(anchor),
        "state": state_of_row(anchor),
        "sub_events": [],
        "media": list(media),
    }


def _episode_parents(events: list[dict]) -> dict:
    """episode id -> the visit episode id it belongs to, from any of its
    events carrying `parent_event` (`content.relate_event` — see
    `_parent_event_of`). One member establishes it for the whole episode:
    a cleaning cycle reports its link on the "8"/"5" steps but not on the
    "3"/"17" ones, and they all share the episode id."""
    parents = {}
    for e in events:
        rel, parent = e.get("related_event"), e.get("parent_event")
        if rel and parent and rel != parent:
            parents.setdefault(rel, parent)
    return parents


def _assign_episode_media(session: dict, events: list[dict]) -> None:
    """Give each sub-episode's media to exactly one of its lines.

    A cleaning cycle reports several steps under one `related_event`, and the
    episode's media (its recording + the "Check waste" gallery) is keyed by
    that episode, not by an individual step. Hanging it on every step would
    repeat the same gallery 3-4 times; hanging it on none loses it. So it goes
    on the cycle's completion line (type "5", which is the step that actually
    carries the waste report), falling back to the last line of the episode.

    Sub-events belonging to the session's OWN episode are skipped: that media
    is already on the card, so giving it to a line as well showed the visit's
    recording twice -- once at the top and once beside the "Weight check" step
    that shares the anchor's `related_event`.
    """
    own_episode = session.get("related_event")
    by_episode: dict[str, list[dict]] = {}
    for sub in session.get("sub_events", []):
        rel = sub.get("related_event")
        if rel and rel != own_episode:
            by_episode.setdefault(rel, []).append(sub)

    media_by_episode: dict[str, list[dict]] = {}
    for e in events:
        rel = e.get("related_event")
        if rel in by_episode and e.get("media"):
            bucket = media_by_episode.setdefault(rel, [])
            for m in e["media"]:
                if m not in bucket:
                    bucket.append(m)

    for rel, subs in by_episode.items():
        media = media_by_episode.get(rel)
        if not media:
            continue
        carrier = next(
            (s for s in subs if s.get("event_type") in codes.PRIMARY_DONE_CODES),
            subs[-1],
        )
        carrier["media"] = media


def _anchor_events(events: list[dict]) -> list[dict]:
    """The events eligible to head a card of their own."""
    # Two groups, and the difference is whether a *detail* code is admitted.
    #
    # An EPISODE kind brings its details with it, because grouping starts here:
    # `feed_start` is a detail and shares its `event_id` with the `feed_over`
    # that closes the meal, so leaving it out would leave it with no card to be
    # folded under. `_cards_from_episodes` then picks the real head out of the
    # group via `codes.ANCHOR_CODES` and files the rest as steps.
    #
    # An ANCHOR kind admits only its non-details: a "24" detection result
    # belongs under the "20" it reports on, and an "Error cleared" under the
    # error it clears.
    #
    # Feeding and drinking used to be in neither, so on a feeder and a fountain
    # `by_related` was ALWAYS empty and every event fell through to a flat
    # standalone row — while `codes.py` had marked `feed_over`, `eat_over` and
    # `drink_over` as anchors all along, and the rows already carried the right
    # `related_event`. Only this function disagreed.
    episode_kinds = (codes.KIND_TOILET, codes.KIND_FEEDING, codes.KIND_DRINKING)
    anchor_kinds = (codes.KIND_PET, codes.KIND_MOTION, codes.KIND_ERROR)
    return [e for e in events
            if e.get("event_kind") in episode_kinds
            or (e.get("event_kind") in anchor_kinds
                and not is_detail_event(e.get("event_type"),
                                        e.get("device_type")))]


def _episodes_of(anchors: list[dict]) -> tuple[dict[str, list[dict]], list[dict]]:
    """Anchors grouped by the episode they belong to, plus the toilet visits
    that carry no episode id at all and can therefore only stand alone."""
    by_related: dict[str, list[dict]] = {}
    loose_toilet = []
    for e in anchors:
        rel = e.get("related_event")
        if rel:
            by_related.setdefault(rel, []).append(e)
        elif e.get("event_kind") == codes.KIND_TOILET:
            loose_toilet.append(e)
    return by_related, loose_toilet


def _attach(session: dict, e: dict, used_ids: set) -> None:
    """Hang event `e` off `session` as a sub-event line, and mark it used.

    The line is `{id, event_type, ts, related_event, detail, content,
    media}` and starts with `media: []` on purpose. Media stays with the
    episode that produced it — a cleaning cycle's waste photos and
    recording belong to the cleaning, NOT to the visit (merging them was
    why photos appeared to "mix" between the two) — so exactly one line per
    cleaning episode is given that episode's media afterwards, by
    `_assign_episode_media`. Filling it in here would repeat the same
    gallery on every step of the cycle.

    Pet identity fills UP, not down: an "appeared" card is anchored by code
    20, which carries no identity, while the recognition result arrives on
    code 24 — a `detail` code that can never head a card and is attached
    here. Without this the cat is named on visits and anonymous on every
    sighting. The line itself stays identity-free; only the card gets it.
    """
    used_ids.add(e["id"])
    if session.get("pet_id") is None and e.get("pet_id") is not None:
        session["pet_id"] = e["pet_id"]
        session["pet_source"] = e.get("pet_source")
    session["sub_events"].append({
        "id": e["id"], "event_type": e.get("event_type"), "ts": e.get("ts"),
        "related_event": e.get("related_event"),
        "detail": is_detail_event(e.get("event_type"), e.get("device_type")),
        # Carry the decoded content so the API can build a specific label
        # (e.g. "Auto cleaning completed") from result/start_reason, and
        # the state snapshot for the codes whose content cannot say which
        # way they went -- the light reports the same payload on and off.
        "content": _content_of(e),
        "state": state_of_row(e),
        "media": [],
    })


def _cards_from_episodes(by_related: dict[str, list[dict]],
                         used_ids: set) -> tuple[list[dict], dict[str, dict]]:
    """One card per episode, with the episode's other events as its steps.

    Returns the cards in episode-discovery order, and an index of them by
    episode id — that index is what a follow-up's parent link resolves against.
    """
    sessions = []
    sessions_by_episode = {}
    for rel, group in by_related.items():
        group.sort(key=lambda e: e.get("ts") or 0)
        is_visit = any(e.get("event_kind") == codes.KIND_TOILET for e in group)
        anchor = next((e for e in group
                       if e.get("event_type") in codes.ANCHOR_CODES), group[-1])
        pet_in = next((e for e in group if e.get("event_type") == "pet_in"), None)
        used_ids.add(anchor["id"])
        session = _session_from_visit(anchor, pet_in, anchor.get("media") or [])
        if not is_visit:
            # An "appeared" or error card: no duration and no weighing, just
            # the event and whatever it recorded.
            kind = anchor.get("event_kind") or codes.KIND_PET
            session.update(kind=kind, event_kind=kind,
                           duration_sec=None, weight=None)
        # Every OTHER member of the episode is a step of this card. Marking
        # them used without rendering them — which is what this did — silently
        # discarded all 23 mid-visit weight samples in the reference corpus,
        # and would swallow every "Error cleared" now that errors anchor.
        for e in group:
            if e is not anchor:
                _attach(session, e, used_ids)
        sessions.append(session)
        sessions_by_episode[rel] = session
    return sessions, sessions_by_episode


def _cards_from_loose_visits(loose_toilet: list[dict], used_ids: set) -> list[dict]:
    """A card for each toilet visit that reported no episode id, and so has
    nothing it could group with."""
    sessions = []
    for e in loose_toilet:
        used_ids.add(e["id"])
        sessions.append(_session_from_visit(e, None, e.get("media") or []))
    return sessions


def _attach_linked_followups(events: list[dict], parents: dict,
                             sessions_by_episode: dict[str, dict],
                             used_ids: set) -> None:
    """The follow-ups the device linked itself, matched to the card their
    `parent_event` names and confined to the device that reported them."""
    # Pass 1 — the device told us which card each follow-up belongs to
    # (a cleaning cycle -> its visit, a detection result -> its "appeared").
    attachable = (codes.KIND_CLEANING, codes.KIND_PET, codes.KIND_MOTION,
                  codes.KIND_ERROR)
    for e in events:
        if e["id"] in used_ids or e.get("event_kind") not in attachable:
            continue
        parent = parents.get(e.get("related_event")) or e.get("parent_event")
        session = sessions_by_episode.get(parent) if parent else None
        if session is not None and session.get("device_id") == e.get("device_id"):
            _attach(session, e, used_ids)


def _attach_nearby_cleanings(events: list[dict], sessions: list[dict],
                             used_ids: set) -> None:
    """Timestamp proximity, for the cleaning steps the explicit link missed."""
    # Pass 2 — last resort for a cleaning step that reported no link and whose
    # episode had no completion of its own (the solo light-cycle reports).
    #
    # Two guards, both learned from real data. It only offers steps to a
    # TOILET VISIT: a cleaning cycle follows a visit, whereas an error or an
    # "appeared" card has no reason to own one, and without this an unrelated
    # error card 600s away swallowed a whole maintenance session. And it picks
    # the CLOSEST candidate rather than the first in list order, which was
    # never meaningful — `sessions` is in episode-discovery order.
    visit_sessions = [s for s in sessions if s.get("kind") == "visit"]
    for e in events:
        if e["id"] in used_ids or e.get("event_kind") != codes.KIND_CLEANING:
            continue
        if e.get("ts") is None:
            continue
        candidates = [
            s for s in visit_sessions
            if s.get("device_id") == e.get("device_id") and s.get("ts") is not None
            and 0 <= e["ts"] - s["ts"] <= SUB_EVENT_WINDOW_SEC
        ]
        if candidates:
            _attach(min(candidates, key=lambda s: e["ts"] - s["ts"]), e, used_ids)


def _cards_from_orphan_cleanings(events: list[dict],
                                 sessions_by_episode: dict[str, dict],
                                 used_ids: set) -> list[dict]:
    """A card per cleaning episode that no visit claimed, headed by that
    episode's own completion step."""
    # Whatever pass 2 could not place: a cleaning or maintenance cycle that
    # neither linked to a visit nor happened near one. Left ungrouped it is
    # one bare row per step — seven of them for a single maintenance session
    # — so its completion step heads a card instead, with the mechanism steps
    # folded underneath like any other cycle.
    orphan_episodes: dict[str, list[dict]] = {}
    for e in events:
        rel = e.get("related_event")
        if rel and e["id"] not in used_ids and e.get("event_kind") == codes.KIND_CLEANING:
            orphan_episodes.setdefault(rel, []).append(e)

    sessions = []
    for rel, group in orphan_episodes.items():
        group.sort(key=lambda e: e.get("ts") or 0)
        anchor = next((e for e in group
                       if e.get("event_type") in codes.PRIMARY_DONE_CODES), None)
        if anchor is None:
            # No completion step, so nothing here claims to summarise the
            # cycle; the steps stay standalone rather than one being promoted
            # arbitrarily.
            continue
        used_ids.add(anchor["id"])
        session = _session_from_visit(anchor, None, anchor.get("media") or [])
        session.update(kind=codes.KIND_CLEANING, event_kind=codes.KIND_CLEANING,
                       duration_sec=None, weight=None)
        sessions.append(session)
        sessions_by_episode.setdefault(rel, session)
        for e in group:
            if e is not anchor:
                _attach(session, e, used_ids)
    return sessions


def _attach_cycle_tails(events: list[dict], sessions: list[dict],
                        used_ids: set) -> None:
    """The second half of a cleaning that the device filed as its own episode."""
    # Pass 3 — the tail of a cycle that the device filed as its own episode.
    # A cleaning turns the illuminator on and off, and reports the "off" under
    # a fresh event_id with no link back, so nothing in the data ties it to the
    # cycle it belongs to. When the cleaning followed a VISIT, pass 2 already
    # swept that report into the visit card -- 26 of the 30 in the reference
    # corpus. A MANUAL clean has no visit, so the report stranded into a card
    # of its own: one press, two cards, the second one just "light off".
    #
    # Offered only to a CLEANING card, so this cannot revive the failure pass 2
    # is guarded against (an unrelated error or "appeared" card swallowing a
    # maintenance session), and only to a card that is not the event's own.
    cleaning_cards = [s for s in sessions
                      if s.get("kind") == codes.KIND_CLEANING
                      and s.get("ts") is not None]
    if cleaning_cards:
        for e in events:
            if e["id"] in used_ids or e.get("event_kind") != codes.KIND_CLEANING:
                continue
            if e.get("ts") is None:
                continue
            candidates = [s for s in cleaning_cards
                          if s.get("device_id") == e.get("device_id")
                          and s.get("related_event") != e.get("related_event")
                          and abs(e["ts"] - s["ts"]) <= CYCLE_TAIL_WINDOW_SEC]
            if candidates:
                _attach(min(candidates, key=lambda s: abs(e["ts"] - s["ts"])), e, used_ids)


def _standalone_rows(events: list[dict], used_ids: set) -> list[dict]:
    """A lighter card for every event no session claimed."""
    sessions = []
    for e in events:
        if e["id"] in used_ids:
            continue
        sessions.append({
            "kind": "event",
            "id": e["id"],
            "related_event": e.get("related_event"),
            "device_id": e.get("device_id"),
            "device_type": e.get("device_type"),
            "ts": e.get("ts"),
            # A standalone step has no span of its own, so its arrival time is
            # the only time it has. Emitted anyway so every card has one shape.
            "display_ts": e.get("ts"),
            "pet_id": e.get("pet_id"),
            "pet_source": e.get("pet_source"),
            "event_type": e.get("event_type"),
            "event_kind": e.get("event_kind"),
            "duration_sec": None,
            "weight": None,
            "content": _content_of(e),
            "state": state_of_row(e),
            "sub_events": [],
            "media": e.get("media") or [],
        })
    return sessions


def group_sessions(events: list[dict]) -> list[dict]:
    """Group raw event rows (each already carrying its `media` list — see
    EventStore.query_timeline) into **sessions**.

    Two kinds of episode anchor a card, mirroring the official app:
      * a toilet visit (its cleaning cycle attaches as sub-events), and
      * a motion/"appeared" detection, whose follow-up result attaches to it.
    Anything else passes through as a lighter standalone row.

    Sub-events attach by the device's OWN explicit link (`parent_event`),
    falling back to timestamp proximity only for episodes that never reported
    one.

    `events` order doesn't matter; the result is sorted newest-first.
    """
    by_related, loose_toilet = _episodes_of(_anchor_events(events))
    used_ids: set = set()

    sessions, sessions_by_episode = _cards_from_episodes(by_related, used_ids)
    sessions.extend(_cards_from_loose_visits(loose_toilet, used_ids))

    parents = _episode_parents(events)
    _attach_linked_followups(events, parents, sessions_by_episode, used_ids)
    _attach_nearby_cleanings(events, sessions, used_ids)
    sessions.extend(_cards_from_orphan_cleanings(events, sessions_by_episode, used_ids))
    _attach_cycle_tails(events, sessions, used_ids)

    for session in sessions:
        session["sub_events"].sort(key=lambda s: s.get("ts") or 0)
        _assign_episode_media(session, events)

    sessions.extend(_standalone_rows(events, used_ids))
    sessions.sort(key=lambda s: s.get("ts") or 0, reverse=True)
    return sessions


def _filter_buckets(session: dict) -> set[str]:
    """Which Timeline filter chips a session belongs to. May be more than one.

    "Pet" and "Toileting" are DISJOINT, matching the official app (its own
    chips read e.g. "All 25 / Pet 5 / Toileting 7" — Pet is smaller than
    Toileting, which it couldn't be if visits were also counted as Pet):
    "Toileting" is a real visit, "Pet" is an episode where the pet was only
    seen ("appeared").

    "Health alert" is about the CAT, not the box. A hall-sensor fault or a
    jammed mechanism is a device problem and belongs under "Faults"; filing it
    as a health alert made the chip mean two unrelated things at once and buried
    the pet-health signal it exists for.

    It is also the reason this returns a set: a toilet visit where the cat
    YOWLED is both a visit and a health alert. PetKit's own description of
    Yowling Detection is that meowing in the box "may be caused by the cat's
    physical discomfort" and exists so you "spot potential health problems
    promptly" — so such a visit belongs under the health chip without ceasing
    to be a visit.

    Urine pH is deliberately NOT wired in yet. It would belong here — the app
    says an abnormal reading is surfaced in the timeline — but `ph_reason`'s
    values are undecoded: the feature was off for the whole reference capture,
    so 5 and 4 are its "not measured" codes and we cannot tell an abnormal
    reading from a missing one. Guessing would put a health warning on a
    healthy cat.

    Feeding, drinking and cleaning get chips of their own. Without them a
    feeder's and a fountain's cards belonged to no chip at all: they showed
    under "All" and were unreachable from every other filter, which is how a
    water refill came to be findable only by scrolling past it.
    """
    kind = session.get("event_kind")
    buckets: set[str] = set()
    if kind == codes.KIND_TOILET:
        buckets.add("toileting")
    elif kind in (codes.KIND_PET, codes.KIND_MOTION):
        buckets.add("pet")
    elif kind == codes.KIND_ERROR:
        buckets.add("fault")
    elif kind == codes.KIND_FEEDING:
        buckets.add("feeding")
    elif kind == codes.KIND_DRINKING:
        buckets.add("drinking")
    elif kind == codes.KIND_CLEANING:
        buckets.add("cleaning")

    if to_int((session.get("content") or {}).get("petVoice"), 0):
        buckets.add("health_alert")
    return buckets


def filter_counts(sessions: list[dict]) -> dict:
    """Chip badge numbers, one key per chip the panel offers.

    `all` is the total, so it is NOT the sum of the others — a session in no
    bucket (an unrecognised code) is counted only there, and a yowling visit is
    counted twice on purpose.

    Every key is always present, so a chip renders `0` rather than `undefined`
    on a device family that never fills it.
    """
    counts = {"all": len(sessions), "pet": 0, "toileting": 0,
              "drinking": 0, "feeding": 0, "cleaning": 0,
              "health_alert": 0, "fault": 0}
    for s in sessions:
        for bucket in _filter_buckets(s):
            counts[bucket] += 1
    return counts


def matches_filter(session: dict, filt: str) -> bool:
    """Whether a session survives the chip `filt` (empty/"all" keeps every one)."""
    if not filt or filt == "all":
        return True
    return filt in _filter_buckets(session)
