"""Builders for pet-attributed episodes with media on disk.

Shared by the resolver, panel and publisher tests of the per-pet last-visit /
last-drink media, so all three exercise the same row shapes the pipeline and
the stitcher write.
"""
from __future__ import annotations

import itertools
import os
from typing import Any

from petkit_local.events import codes

JPEG = b"\xff\xd8\xff\xe0" + b"J" * 60
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"V" * 200

_ids = itertools.count(1)


def media(category: str, name: str, *, status: str = "ready", stitch_state: str | None = None,
          created_at: float = 0.0, on_disk: bool = True) -> dict[str, Any]:
    """One media spec for `add_episode`; `name` is relative to the media root."""
    return {"category": category, "name": name, "status": status,
            "stitch_state": stitch_state, "created_at": created_at, "on_disk": on_disk}


async def add_episode(store, media_root: str, rel: str, *, pet_id: int | None,
                      kind: str = codes.KIND_TOILET, device_id: int = 1, ts: float = 1000.0,
                      items: tuple[dict[str, Any], ...] = (),
                      event_type: str = "pet_out") -> None:
    """One attributed event row plus its media rows (and files)."""
    await store.upsert_event({
        "device_id": device_id, "event_type": event_type, "event_kind": kind,
        "pet_id": pet_id, "ts": ts, "related_event": rel,
        "event_uid": f"{rel}-{event_type}-{next(_ids)}",
    })
    for m in items:
        path = os.path.join(media_root, m["name"])
        if m["on_disk"]:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(JPEG if path.endswith(".jpg") else MP4)
        await store.upsert_media({
            "file_id": f"f{next(_ids)}", "device_id": device_id, "related_event": rel,
            "category": m["category"], "status": m["status"], "media_path": path,
            "stitch_state": m["stitch_state"], "created_at": m["created_at"],
        })
