"""Which of an episode's uploads is ready to play, and which is its poster.

The role -> slot mapping the Timeline renders from, moved out of
`web/api/media.py` so the same answer serves the panel and the HA pet
entities (`media/pet_media.py`): two copies of "is this chunked video ready
yet" would drift, and a pet image would then show a visit the Timeline still
calls "assembling".

Pure functions over media rows (dicts as `EventStore` returns them); every
path they return is relative to the friendly media root.
"""
from __future__ import annotations

import os
import time
from typing import Any

from petkit_local.media.stitch import QUIET_SECONDS


def rel_media(path: str | None, media_root: str) -> str | None:
    """An absolute stored media path as the media-root-relative URL part."""
    if not path:
        return None
    try:
        return os.path.relpath(path, media_root)
    except ValueError:
        # Different drives on Windows — no relative form exists.
        return None


def group_by_role(rows: list[dict[str, Any]]) -> dict[str | None, list[dict[str, Any]]]:
    """Media rows bucketed by `category`, in their original order.

    Read with `.get(cat, [])`: a role with no rows has no key.
    """
    by: dict[str | None, list[dict[str, Any]]] = {}
    for m in rows:
        by.setdefault(m.get("category"), []).append(m)
    return by


def chunked_video_slot(rows: list[dict[str, Any]], media_root: str,
                       now: float) -> tuple[str | None, bool]:
    """`(url, pending)` for a CHUNKED video role (fullVideo / cloudDouble).

    A real T5 uploads these as many ~4s chunks that media/stitch.py joins into
    one clip once the episode goes quiet. Until that's done we must NOT hand
    the UI a raw chunk — playing a 4-second fragment of a visit is exactly the
    rough edge to avoid. So a chunked video is "ready" only when the stitched
    result exists, or a lone chunk has clearly settled (the stitcher looked
    and left it because there was nothing to join). Otherwise it's `pending`
    and the UI shows a still / "processing" state instead."""
    ready = [m for m in rows if m.get("status") == "ready" and m.get("media_path")]
    stitched = next((m for m in ready if m.get("stitch_state") == "stitched"), None)
    if stitched:
        return rel_media(stitched["media_path"], media_root), False
    expecting = bool(rows)  # any row (even pending) means a recording is coming
    if len(ready) == 1 and not any(m.get("status") == "pending" for m in rows):
        newest = ready[0].get("created_at") or 0
        if now - newest > QUIET_SECONDS:
            return rel_media(ready[0]["media_path"], media_root), False
    return None, expecting


def single_slot(rows: list[dict[str, Any]], media_root: str) -> str | None:
    """A SINGLE-file clip role (dynamicVideo / highLight = the app's short
    'Highlight'): the device uploads it as one complete ~4s file, not chunks,
    so it's ready the moment it's processed — no stitching, never a fragment."""
    for m in rows:
        if m.get("status") == "ready" and m.get("media_path"):
            r = rel_media(m["media_path"], media_root)
            if r:
                return r
    return None


def session_media_urls(sessions_media: list[dict[str, Any]], media_root: str,
                       now: float | None = None) -> dict[str, Any]:
    """Media slots for one episode, keyed by role (see events/ingest.py).

    - `highlight_url` — the short ~4s event clip (`dynamicVideo`); ready at once.
    - `playback_url`  — the long continuous recording (`fullVideo`); ready only
      once media/stitch.py has joined its chunks (else `video_pending`).
    - `preview_url`   — the `cloudDouble` timelapse; same chunked/stitched gate
      (else `preview_pending`).
    - `poster_url` / `waste` / `health` — stills.
    - `snapshot_url`  — best still to use as a poster/thumbnail fallback.
    - `video_pending` — a playable recording is expected but still assembling,
      so the UI shows a still or a "processing" placeholder, not a fragment.
    - `preview_pending` — the same for the timelapse.
    """
    now = now if now is not None else time.time()
    by = group_by_role(sessions_media)

    playback_url, pb_pending = chunked_video_slot(by.get("fullVideo", []), media_root, now)
    preview_url, pv_pending = chunked_video_slot(by.get("cloudDouble", []), media_root, now)
    highlight_url = single_slot(by.get("dynamicVideo", []) + by.get("highLight", []), media_root)
    poster_url = single_slot(by.get("eventImage", []), media_root)

    def _ready_rels(cat: str) -> list[str]:
        """Every ready file of one still-image role, as relative URLs."""
        return [r for r in (rel_media(m["media_path"], media_root)
                            for m in by.get(cat, [])
                            if m.get("status") == "ready" and m.get("media_path")) if r]

    out: dict[str, Any] = {
        "highlight_url": highlight_url, "playback_url": playback_url,
        "preview_url": preview_url, "poster_url": poster_url,
        "waste": _ready_rels("wasteCheck"), "health": _ready_rels("healthPic"),
        "snapshot_url": None, "video_pending": bool(pb_pending),
        "preview_pending": bool(pv_pending),
    }

    # Prefer the purpose-made poster, then a waste shot, as the thumbnail.
    out["snapshot_url"] = out["poster_url"] or (out["waste"][0] if out["waste"] else None)
    return out


def has_media(slots: dict[str, Any]) -> bool:
    """True if a `session_media_urls` result is worth rendering at all.

    `video_pending` counts: the placeholder is the point of that flag.
    """
    return bool(slots.get("playback_url") or slots.get("highlight_url")
                or slots.get("waste") or slots.get("health")
                or slots.get("snapshot_url") or slots.get("preview_url")
                or slots.get("video_pending"))
