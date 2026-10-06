"""A pet's last toilet visit / last drink as one playable recording.

The single resolver behind both the panel's stable redirect URLs
(`web/api/pet_media.py`) and the HA pet entities (`ha/publisher.py`), so the
picture HA shows and the video a tap opens are always the same visit.

Readiness reuses `media/slots.py` — the Timeline's own gate — and adds the one
thing a "latest" lookup needs on top of it: a chunked tier that is still
assembling makes the resolver skip to the previous visit rather than hand out a
fragment, but a tier that has been "assembling" for longer than
`PENDING_GRACE_SECONDS` (failed stitch, chunks stuck pending) is treated as
never coming and falls through to the next tier of the SAME visit. Without that
a single failed stitch would hide a pet's newest visit forever. A Clip-only
visit likewise waits until its media has settled (`CLIP_SETTLE_SECONDS`), so the
chunks that may still follow a clip cannot move the answer back to an older
visit once the newer one was shown.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from petkit_local.events import codes
from petkit_local.media.slots import (
    chunked_video_slot,
    group_by_role,
    session_media_urls,
    single_slot,
)
from petkit_local.utils.paths import UnsafePathError, safe_join

if TYPE_CHECKING:
    from petkit_local.events.store import EventStore

# How many of a pet's newest episodes are inspected before giving up. Bounds
# the cost when the newest visits have no media (retention, a device without a
# camera, rows that landed before their uploads).
SCAN_EPISODES = 20

# Well above the stitcher's QUIET_SECONDS (90) + its sweep interval (60) +
# ffmpeg time. Past it, a chunked tier that is still not ready is assumed to
# never become ready.
PENDING_GRACE_SECONDS = 600.0

# How long an episode's media must have been silent before its lone Clip is
# accepted. The single-file clip can land BEFORE the first cloudDouble/fullVideo
# chunk; accepting it at once would show the new visit as "clip", then skip it
# while the chunks assemble (back to the previous visit), then show it again as
# "timelapse" - the picture would jump back in time. Chunks of one episode
# arrive every ~4 s, and the stitcher already treats QUIET_SECONDS (90) of
# silence as "the episode is over"; 180 s is twice that, which also covers one
# full stitch sweep (every 60 s) passing over the episode in between.
CLIP_SETTLE_SECONDS = 180.0

PET_MEDIA_KINDS = (codes.KIND_TOILET, codes.KIND_DRINKING)


@dataclass(frozen=True)
class PetMedia:
    """The recording chosen for one pet and kind; paths are media-root-relative."""

    pet_id: int
    kind: str
    event_id: int
    related_event: str
    device_id: int
    ts: float
    video: str
    tier: str  # "timelapse" | "playback" | "clip"
    poster: str | None  # eventImage, else a waste shot - whichever is on disk


def media_url_path(rel: str) -> str:
    """`api/media/<rel>` with every path segment percent-encoded.

    Mirrors the panel's `static/js/media.js` URL builder: real folder names
    carry spaces and parentheses (`Purobot Ultra (T6 30007986)/...`). Relative
    on purpose — the panel may sit behind an Ingress prefix.
    """
    return "api/media/" + "/".join(quote(seg, safe="") for seg in rel.split("/"))


def _fresh(rows: list[dict[str, Any]], now: float) -> bool:
    """True while the newest upload of a role is inside the pending grace."""
    newest = max((float(m.get("created_at") or 0) for m in rows), default=0.0)
    return now - newest < PENDING_GRACE_SECONDS


def _settled(rows: list[dict[str, Any]], now: float) -> bool:
    """True once the newest media row of an episode (any role) is older than
    `CLIP_SETTLE_SECONDS` - nothing more is coming that could outrank a clip."""
    newest = max((float(m.get("created_at") or 0) for m in rows), default=0.0)
    return now - newest >= CLIP_SETTLE_SECONDS


def _exists(media_root: str, rel: str | None) -> bool:
    """True if `rel` is a regular file under `media_root` (retention may have
    removed it from under its row)."""
    if not rel:
        return False
    try:
        return os.path.isfile(safe_join(media_root, rel))
    except UnsafePathError:
        return False


def _pick(episodes: list[dict[str, Any]], media: dict[str, list[dict[str, Any]]],
          media_root: str, now: float) -> tuple[dict[str, Any], str, str, str | None] | None:
    """The first episode with a playable recording, as `(episode, video, tier,
    poster)`. Sync on purpose: its `isfile` checks run in one worker thread."""
    for ep in episodes:
        rows = [m for m in media.get(ep["related_event"], [])
                if m.get("device_id") == ep["device_id"]]
        by = group_by_role(rows)
        tl_url, tl_pending = chunked_video_slot(by.get("cloudDouble", []), media_root, now)
        pb_url, pb_pending = chunked_video_slot(by.get("fullVideo", []), media_root, now)
        clip = single_slot(by.get("dynamicVideo", []) + by.get("highLight", []), media_root)
        if tl_url:
            video, tier = tl_url, "timelapse"
        elif tl_pending and _fresh(by.get("cloudDouble", []), now):
            continue  # the timelapse is still assembling: show the previous visit
        elif pb_url:
            video, tier = pb_url, "playback"
        elif pb_pending and _fresh(by.get("fullVideo", []), now):
            continue
        elif clip and _settled(rows, now):
            video, tier = clip, "clip"
        else:
            # Nothing playable (no media yet, or retention took it), or a clip
            # whose episode may still be followed by chunks: previous visit.
            continue
        if not _exists(media_root, video):
            continue
        slots = session_media_urls(rows, media_root, now)
        # The poster is checked on disk like the video: retention may have
        # taken it, and a dangling poster would leave the HA picture stale.
        stills = [slots["poster_url"], *slots["waste"]]
        poster = next((p for p in stills if _exists(media_root, p)), None)
        return ep, video, tier, poster
    return None


async def resolve_pet_media(store: EventStore, pet_id: int, kind: str, media_root: str,
                            now: float | None = None) -> PetMedia | None:
    """The newest episode of `kind` for `pet_id` that has a playable recording.

    Tier order is Timelapse (`cloudDouble`) -> Playback (`fullVideo`) -> Clip
    (`dynamicVideo`/`highLight`); a Clip only once the episode's media has been
    silent for `CLIP_SETTLE_SECONDS`. None when no media root is configured or
    none of the newest `SCAN_EPISODES` episodes has one.
    """
    if not media_root:
        return None
    now = now if now is not None else time.time()
    episodes = await store.pet_episodes(pet_id, kind, limit=SCAN_EPISODES)
    if not episodes:
        return None
    media = await store.media_for_related_events([e["related_event"] for e in episodes])
    picked = await asyncio.to_thread(_pick, episodes, media, media_root, now)
    if picked is None:
        return None
    ep, video, tier, poster = picked
    return PetMedia(
        pet_id=pet_id, kind=kind, event_id=int(ep["event_id"]),
        related_event=ep["related_event"], device_id=int(ep["device_id"]),
        ts=float(ep["ts"]), video=video, tier=tier, poster=poster)
