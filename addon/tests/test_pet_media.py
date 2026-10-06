"""The per-pet "last visit / last drink" resolver (media/pet_media.py).

Real EventStore, real files under `tmp_path`: the resolver's whole job is the
interplay of event attribution, media readiness and what is actually on disk.
"""
from __future__ import annotations

import os

from petkit_local.events.codes import KIND_DRINKING, KIND_TOILET
from petkit_local.media.pet_media import (
    CLIP_SETTLE_SECONDS,
    PENDING_GRACE_SECONDS,
    SCAN_EPISODES,
    media_url_path,
    resolve_pet_media,
)
from tests._pet_media import add_episode, media

NOW = 10_000_000.0


def _root(tmp_path) -> str:
    root = tmp_path / "media"
    root.mkdir(exist_ok=True)
    return str(root)


async def test_timelapse_is_preferred_over_playback_and_clip(event_store, tmp_path):
    root = _root(tmp_path)
    await add_episode(event_store, root, "r1", pet_id=2, items=(
        media("cloudDouble", "T5/Timelapse/v.mp4", stitch_state="stitched"),
        media("fullVideo", "T5/Playback/v.mp4", stitch_state="stitched"),
        media("dynamicVideo", "T5/Clips/v.mp4"),
        media("eventImage", "T5/Snapshots/v.jpg"),
    ))
    res = await resolve_pet_media(event_store, 2, KIND_TOILET, root, now=NOW)
    assert res is not None
    assert (res.tier, res.video, res.poster) == ("timelapse", "T5/Timelapse/v.mp4",
                                                 "T5/Snapshots/v.jpg")
    assert (res.pet_id, res.kind, res.related_event, res.device_id) == (2, KIND_TOILET, "r1", 1)


async def test_no_timelapse_falls_back_to_playback(event_store, tmp_path):
    """A W7H before Timelapse was enabled uploads no cloudDouble at all."""
    root = _root(tmp_path)
    await add_episode(event_store, root, "r1", pet_id=2, items=(
        media("fullVideo", "W7H/Playback/v.mp4", stitch_state="stitched"),
        media("dynamicVideo", "W7H/Clips/v.mp4"),
    ))
    res = await resolve_pet_media(event_store, 2, KIND_TOILET, root, now=NOW)
    assert (res.tier, res.video) == ("playback", "W7H/Playback/v.mp4")


async def test_only_a_clip_falls_back_to_clip(event_store, tmp_path):
    root = _root(tmp_path)
    await add_episode(event_store, root, "r1", pet_id=2, items=(
        media("highLight", "T5/Clips/h.mp4"),))
    res = await resolve_pet_media(event_store, 2, KIND_TOILET, root, now=NOW)
    assert (res.tier, res.video, res.poster) == ("clip", "T5/Clips/h.mp4", None)


async def test_an_assembling_timelapse_skips_to_the_previous_visit(event_store, tmp_path):
    root = _root(tmp_path)
    await add_episode(event_store, root, "old", pet_id=2, ts=1000.0, items=(
        media("cloudDouble", "T5/Timelapse/old.mp4", stitch_state="stitched"),))
    await add_episode(event_store, root, "new", pet_id=2, ts=2000.0, items=(
        media("cloudDouble", "T5/.raw/c1.mp4", created_at=NOW - 10),
        media("cloudDouble", "T5/.raw/c2.mp4", created_at=NOW - 10),
        media("fullVideo", "T5/Playback/new.mp4", stitch_state="stitched"),
    ))
    res = await resolve_pet_media(event_store, 2, KIND_TOILET, root, now=NOW)
    assert (res.related_event, res.video) == ("old", "T5/Timelapse/old.mp4")


async def test_a_stale_pending_tier_falls_through_within_the_same_visit(event_store, tmp_path):
    """A failed stitch leaves ready, unstitched chunks that are "pending"
    forever; past the grace window they must not hide the visit."""
    root = _root(tmp_path)
    stale = NOW - PENDING_GRACE_SECONDS - 1
    await add_episode(event_store, root, "old", pet_id=2, ts=1000.0, items=(
        media("cloudDouble", "T5/Timelapse/old.mp4", stitch_state="stitched"),))
    await add_episode(event_store, root, "new", pet_id=2, ts=2000.0, items=(
        media("cloudDouble", "T5/.raw/c1.mp4", stitch_state="failed", created_at=stale),
        media("cloudDouble", "T5/.raw/c2.mp4", stitch_state="failed", created_at=stale),
        media("fullVideo", "T5/Playback/new.mp4", stitch_state="stitched"),
    ))
    res = await resolve_pet_media(event_store, 2, KIND_TOILET, root, now=NOW)
    assert (res.related_event, res.tier, res.video) == ("new", "playback", "T5/Playback/new.mp4")


async def test_an_assembling_playback_without_timelapse_skips_the_visit(event_store, tmp_path):
    root = _root(tmp_path)
    await add_episode(event_store, root, "old", pet_id=2, ts=1000.0, items=(
        media("fullVideo", "W7H/Playback/old.mp4", stitch_state="stitched"),))
    await add_episode(event_store, root, "new", pet_id=2, ts=2000.0, items=(
        media("fullVideo", "W7H/.raw/c1.mp4", created_at=NOW - 10),
        media("fullVideo", "W7H/.raw/c2.mp4", created_at=NOW - 10),
        media("dynamicVideo", "W7H/Clips/new.mp4"),
    ))
    res = await resolve_pet_media(event_store, 2, KIND_TOILET, root, now=NOW)
    assert res.related_event == "old"


async def test_each_pet_resolves_to_its_own_visits(event_store, tmp_path):
    root = _root(tmp_path)
    for i, pid in enumerate((2, 3, 2, 3)):
        await add_episode(event_store, root, f"r{i}", pet_id=pid, ts=1000.0 + i, items=(
            media("cloudDouble", f"T5/Timelapse/{i}.mp4", stitch_state="stitched"),))
    assert (await resolve_pet_media(event_store, 2, KIND_TOILET, root, now=NOW)).related_event == "r2"
    assert (await resolve_pet_media(event_store, 3, KIND_TOILET, root, now=NOW)).related_event == "r3"


async def test_a_drink_is_never_a_toilet_visit(event_store, tmp_path):
    root = _root(tmp_path)
    await add_episode(event_store, root, "visit", pet_id=2, ts=1000.0, items=(
        media("fullVideo", "T5/Playback/v.mp4", stitch_state="stitched"),))
    await add_episode(event_store, root, "drink", pet_id=2, ts=2000.0, kind=KIND_DRINKING,
                      device_id=4, event_type="drink_over", items=(
                          media("fullVideo", "W7H/Playback/d.mp4", stitch_state="stitched"),))
    visit = await resolve_pet_media(event_store, 2, KIND_TOILET, root, now=NOW)
    drink = await resolve_pet_media(event_store, 2, KIND_DRINKING, root, now=NOW)
    assert (visit.related_event, drink.related_event, drink.device_id) == ("visit", "drink", 4)


async def test_a_visit_whose_media_has_not_arrived_yet_is_skipped(event_store, tmp_path):
    root = _root(tmp_path)
    await add_episode(event_store, root, "old", pet_id=2, ts=1000.0, items=(
        media("dynamicVideo", "T5/Clips/old.mp4"),))
    await add_episode(event_store, root, "new", pet_id=2, ts=2000.0)
    assert (await resolve_pet_media(event_store, 2, KIND_TOILET, root, now=NOW)).related_event == "old"


async def test_a_file_removed_by_retention_skips_the_visit(event_store, tmp_path):
    root = _root(tmp_path)
    await add_episode(event_store, root, "old", pet_id=2, ts=1000.0, items=(
        media("dynamicVideo", "T5/Clips/old.mp4"),))
    await add_episode(event_store, root, "new", pet_id=2, ts=2000.0, items=(
        media("cloudDouble", "T5/Timelapse/new.mp4", stitch_state="stitched", on_disk=False),))
    assert (await resolve_pet_media(event_store, 2, KIND_TOILET, root, now=NOW)).related_event == "old"


async def test_the_poster_falls_back_to_a_waste_shot(event_store, tmp_path):
    root = _root(tmp_path)
    await add_episode(event_store, root, "r1", pet_id=2, items=(
        media("dynamicVideo", "T5/Clips/v.mp4"),
        media("wasteCheck", "T5/Waste/w.jpg"),
    ))
    assert (await resolve_pet_media(event_store, 2, KIND_TOILET, root, now=NOW)).poster \
        == "T5/Waste/w.jpg"


async def test_the_scan_is_bounded(event_store, tmp_path):
    root = _root(tmp_path)
    await add_episode(event_store, root, "ancient", pet_id=2, ts=1.0, items=(
        media("dynamicVideo", "T5/Clips/a.mp4"),))
    for i in range(SCAN_EPISODES):
        await add_episode(event_store, root, f"bare{i}", pet_id=2, ts=1000.0 + i)
    assert await resolve_pet_media(event_store, 2, KIND_TOILET, root, now=NOW) is None


async def test_no_media_root_resolves_nothing(event_store, tmp_path):
    root = _root(tmp_path)
    await add_episode(event_store, root, "r1", pet_id=2, items=(
        media("dynamicVideo", "T5/Clips/v.mp4"),))
    assert await resolve_pet_media(event_store, 2, KIND_TOILET, "", now=NOW) is None


def test_media_url_path_encodes_each_segment():
    assert media_url_path("Purobot Ultra (T6 1)/Timelapse/x y.mp4") \
        == "api/media/Purobot%20Ultra%20%28T6%201%29/Timelapse/x%20y.mp4"


# --- the store reads behind it ----------------------------------------------

async def test_pet_episodes_groups_by_episode_newest_first(event_store, tmp_path):
    root = _root(tmp_path)
    await add_episode(event_store, root, "a", pet_id=2, ts=1000.0)
    await add_episode(event_store, root, "a", pet_id=2, ts=1005.0, event_type="pet_in")
    await add_episode(event_store, root, "b", pet_id=2, ts=2000.0)
    await add_episode(event_store, root, "c", pet_id=3, ts=3000.0)
    await add_episode(event_store, root, "d", pet_id=2, ts=4000.0, kind=KIND_DRINKING)
    eps = await event_store.pet_episodes(2, KIND_TOILET)
    assert [(e["related_event"], e["ts"]) for e in eps] == [("b", 2000.0), ("a", 1005.0)]
    assert all(isinstance(e["event_id"], int) and e["device_id"] == 1 for e in eps)
    assert len(await event_store.pet_episodes(2, KIND_TOILET, limit=1)) == 1


async def test_media_for_related_events_batches_by_episode(event_store, tmp_path):
    root = _root(tmp_path)
    await add_episode(event_store, root, "a", pet_id=2, items=(
        media("eventImage", "x/a2.jpg", created_at=20.0),
        media("eventImage", "x/a1.jpg", created_at=10.0)))
    await add_episode(event_store, root, "b", pet_id=2, items=(media("eventImage", "x/b.jpg"),))
    got = await event_store.media_for_related_events(["a", "zzz"])
    assert list(got) == ["a"]
    assert [os.path.basename(m["media_path"]) for m in got["a"]] == ["a1.jpg", "a2.jpg"]
    assert await event_store.media_for_related_events([]) == {}


async def test_pet_ids_for_related_event_reads_the_whole_episode(event_store, tmp_path):
    root = _root(tmp_path)
    await add_episode(event_store, root, "a", pet_id=3, ts=1000.0)
    await add_episode(event_store, root, "a", pet_id=None, ts=2000.0, event_type="pet_in")
    await add_episode(event_store, root, "b", pet_id=4)
    assert await event_store.pet_ids_for_related_event("a") == [3]
    assert await event_store.pet_ids_for_related_event("nope") == []


async def test_a_fresh_clip_waits_for_its_episode_to_settle(event_store, tmp_path):
    """The clip can land before the first timelapse chunk. Accepting it at once
    would show the new visit, then skip it while the chunks assemble (back to
    the previous visit), then show it again: the picture would jump back."""
    root = _root(tmp_path)
    await add_episode(event_store, root, "old", pet_id=2, ts=1000.0, items=(
        media("dynamicVideo", "T5/Clips/old.mp4", created_at=NOW - 3600),))
    await add_episode(event_store, root, "new", pet_id=2, ts=2000.0, items=(
        media("dynamicVideo", "T5/Clips/new.mp4", created_at=NOW - 5),))
    res = await resolve_pet_media(event_store, 2, KIND_TOILET, root, now=NOW)
    assert res.related_event == "old"

    later = NOW + CLIP_SETTLE_SECONDS
    res = await resolve_pet_media(event_store, 2, KIND_TOILET, root, now=later)
    assert (res.related_event, res.tier, res.video) == ("new", "clip", "T5/Clips/new.mp4")


async def test_a_clip_settles_from_the_episodes_newest_upload_of_any_role(event_store, tmp_path):
    root = _root(tmp_path)
    await add_episode(event_store, root, "r1", pet_id=2, items=(
        media("dynamicVideo", "T5/Clips/c.mp4", created_at=NOW - 3600),
        media("eventImage", "T5/Snapshots/p.jpg", created_at=NOW - 5)))
    assert await resolve_pet_media(event_store, 2, KIND_TOILET, root, now=NOW) is None
    res = await resolve_pet_media(event_store, 2, KIND_TOILET, root,
                                  now=NOW + CLIP_SETTLE_SECONDS)
    assert res.tier == "clip"


async def test_the_resolved_visit_never_moves_back_while_chunks_arrive(event_store, tmp_path):
    """Clip, then the timelapse's chunks: old -> (assembling: still old) -> new."""
    root = _root(tmp_path)
    await add_episode(event_store, root, "old", pet_id=2, ts=1000.0, items=(
        media("dynamicVideo", "T5/Clips/old.mp4", created_at=NOW - 3600),))
    await add_episode(event_store, root, "new", pet_id=2, ts=2000.0, items=(
        media("dynamicVideo", "T5/Clips/new.mp4", created_at=NOW),))
    seen = [(await resolve_pet_media(event_store, 2, KIND_TOILET, root, now=NOW)).related_event]
    await add_episode(event_store, root, "new", pet_id=2, ts=2000.0, items=(
        media("cloudDouble", "T5/.raw/c1.mp4", created_at=NOW + 20),
        media("cloudDouble", "T5/.raw/c2.mp4", created_at=NOW + 24)))
    seen.append((await resolve_pet_media(event_store, 2, KIND_TOILET, root,
                                         now=NOW + 30)).related_event)
    await add_episode(event_store, root, "new", pet_id=2, ts=2000.0, items=(
        media("cloudDouble", "T5/Timelapse/new.mp4", stitch_state="stitched",
              created_at=NOW + 150),))
    res = await resolve_pet_media(event_store, 2, KIND_TOILET, root, now=NOW + 160)
    seen.append(res.related_event)
    assert seen == ["old", "old", "new"] and res.tier == "timelapse"


async def test_a_poster_removed_from_disk_falls_back_to_a_waste_shot(event_store, tmp_path):
    root = _root(tmp_path)
    await add_episode(event_store, root, "r1", pet_id=2, items=(
        media("dynamicVideo", "T5/Clips/c.mp4"),
        media("eventImage", "T5/Snapshots/p.jpg", on_disk=False),
        media("wasteCheck", "T5/Waste/w.jpg")))
    res = await resolve_pet_media(event_store, 2, KIND_TOILET, root, now=NOW)
    assert res.poster == "T5/Waste/w.jpg"


async def test_no_still_on_disk_resolves_no_poster(event_store, tmp_path):
    root = _root(tmp_path)
    await add_episode(event_store, root, "r1", pet_id=2, items=(
        media("dynamicVideo", "T5/Clips/c.mp4"),
        media("eventImage", "T5/Snapshots/p.jpg", on_disk=False),
        media("wasteCheck", "T5/Waste/w.jpg", on_disk=False)))
    res = await resolve_pet_media(event_store, 2, KIND_TOILET, root, now=NOW)
    assert (res.video, res.poster) == ("T5/Clips/c.mp4", None)


async def test_another_devices_media_of_the_same_episode_is_ignored(event_store, tmp_path):
    root = _root(tmp_path)
    await add_episode(event_store, root, "r1", pet_id=2, device_id=1)
    await add_episode(event_store, root, "r1", pet_id=None, device_id=9, items=(
        media("dynamicVideo", "T6/Clips/c.mp4"),))
    assert await resolve_pet_media(event_store, 2, KIND_TOILET, root, now=NOW) is None


async def test_pet_ids_for_related_event_narrows_to_one_device(event_store, tmp_path):
    root = _root(tmp_path)
    await add_episode(event_store, root, "r1", pet_id=3, device_id=1)
    await add_episode(event_store, root, "r1", pet_id=4, device_id=2)
    assert await event_store.pet_ids_for_related_event("r1") == [3, 4]
    assert await event_store.pet_ids_for_related_event("r1", 2) == [4]
