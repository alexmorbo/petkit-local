"""Repairs run over rows that are already stored.

Every derived field on an `events` row — `event_kind`, `parent_event`,
`related_event`, `pet_ref` — is computed once at ingest time and persisted, so a
row written before a field existed, or before its event code was recognised,
keeps a wrong value that no amount of later reading fixes. A pass here
recomputes those fields from the untouched device `content` and writes back
only what actually changed, which is what makes it safe to run on every start.

Kept out of `events/store.py` on purpose: this is not persistence, it is the
normalizers in `events/normalize.py` applied a second time — to stored rows
instead of to payloads. The store arrives as an argument, and is imported here
for typing only.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from petkit_local.events.models import PET_SOURCE_WEIGHT
from petkit_local.events.normalize import (_as_dict, _extract_pet_ref, _parent_event_of,
                                           classify_event_kind)

if TYPE_CHECKING:
    from petkit_local.events.store import EventStore

log = logging.getLogger(__name__)


async def backfill_event_rows(store: EventStore) -> int:
    """Recompute derived fields on already-stored events.

    Classification happens at ingest time and is persisted, so rows written
    before a field existed (`parent_event`) or before an event_type code was
    recognised keep their stale values forever — a cleaning report saved as
    `event_kind="other"` never attaches to its visit, and shows up as a bare
    standalone card. Recomputing from the untouched `content_json` repairs
    them in place. Safe to run on every start: it only writes rows whose
    derived values actually change.

    The rewrites go in one `store.transaction()`: every repaired row would
    otherwise be its own commit, and a start-up interrupted halfway would
    leave the timeline partly regrouped.
    """
    rows = await store.all_events()
    fixed = 0

    async with store.transaction():
        for row in rows:
            content = _as_dict(row.get("content_json"))
            updates = {}

            kind = classify_event_kind(row.get("event_type"), content,
                                       row.get("device_type"))
            if kind != row.get("event_kind"):
                updates["event_kind"] = kind

            parent = _parent_event_of(content)
            if parent and parent != row.get("parent_event"):
                updates["parent_event"] = parent

            # Rows from before `event_id` was understood as the session key
            # stored it only as event_uid; recover it so they group instead of
            # floating.
            if not row.get("related_event") and row.get("event_uid"):
                uid = str(row["event_uid"])
                updates["related_event"] = uid.rsplit(":", 1)[0] if ":" in uid else uid

            # Every row stored before `score_info[].id` was understood carries
            # the identity in its content and nothing in `pet_ref`. Recovering
            # it is what makes an existing install's history bindable at all.
            ref = _extract_pet_ref(content)
            if ref is not None and ref != row.get("pet_ref"):
                updates["pet_ref"] = ref
                # Identity outranks the scale: a row the weight matcher filled
                # is handed back to the reported identity, which
                # `resolve_pet_ref` / `bind_pet_ref` own from here.
                if row.get("pet_source") == PET_SOURCE_WEIGHT:
                    updates["pet_id"] = None
                    updates["pet_source"] = None

            if updates:
                await store.update_event_fields(row["id"], **updates)
                fixed += 1

    if fixed:
        log.info("Backfilled %d event rows (event_kind/parent_event/related_event/pet_ref)", fixed)
    return fixed
