import asyncio
import json
import tempfile
from pathlib import Path

from petkit_local.devices.registry import DeviceRegistry
from petkit_local.ha.publisher import HAPublisher
from tests._fakes import FakeMqttClient


def _setup(media_root):
    reg = DeviceRegistry()
    dev = reg.get_or_create(petkit_id=1, device_type="t5", serial_number="SN")
    pub = HAPublisher(reg, {"media_root": media_root})
    pub._client = FakeMqttClient()
    pub._connected = True
    return reg, dev, pub


async def test_publish_media_ready_noop_without_client():
    reg = DeviceRegistry()
    dev = reg.get_or_create(petkit_id=1, device_type="t5", serial_number="SN")
    pub = HAPublisher(reg, {})
    await pub.publish_media_ready(dev, {"status": "ready", "media_path": "/x.jpg", "category": "eventImage"})
    # no client wired -> nothing to assert on, just must not raise


async def test_publish_media_ready_ignores_none_and_not_ready():
    with tempfile.TemporaryDirectory() as tmp:
        reg, dev, pub = _setup(tmp)
        await pub.publish_media_ready(dev, None)
        await pub.publish_media_ready(dev, {"status": "pending", "media_path": "/x.jpg"})
        assert pub._client.published == []


async def test_publish_media_ready_pushes_snapshot_bytes():
    with tempfile.TemporaryDirectory() as tmp:
        reg, dev, pub = _setup(tmp)
        img_path = Path(tmp) / "Waste" / "photo.jpg"
        img_path.parent.mkdir(parents=True, exist_ok=True)
        img_path.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")

        await pub.publish_media_ready(dev, {
            "status": "ready", "media_path": str(img_path), "category": "eventImage",
        })

        topics = [t for t, _, _ in pub._client.published]
        assert "petkit-local/1/last_snapshot" in topics
        payload = next(p for t, p, _ in pub._client.published if t == "petkit-local/1/last_snapshot")
        assert payload == b"\xff\xd8\xff\xe0fakejpeg"


async def test_publish_media_ready_sets_last_clip_relative_path():
    with tempfile.TemporaryDirectory() as tmp:
        reg, dev, pub = _setup(tmp)
        clip_path = Path(tmp) / "Playback" / "2026-07-22" / "clip.mp4"
        clip_path.parent.mkdir(parents=True, exist_ok=True)
        clip_path.write_bytes(b"not-really-video")

        await pub.publish_media_ready(dev, {
            "status": "ready", "media_path": str(clip_path), "category": "fullVideo",
        })

        assert dev.state["lastClipPath"] == "Playback/2026-07-22/clip.mp4"
        state_topics = [t for t, _, _ in pub._client.published if t == "petkit-local/1/state"]
        assert state_topics
        published_state = json.loads(next(p for t, p, _ in pub._client.published
                                          if t == "petkit-local/1/state"))
        assert published_state["state"]["lastClipPath"] == "Playback/2026-07-22/clip.mp4"


async def test_publish_media_ready_reads_the_snapshot_off_the_event_loop():
    """A whole-file read on the loop stalls every device and both MQTT clients.

    Snapshots run to a few MB and arrive in bursts (the waste gallery is ~5
    photos per cleaning), so the read must yield to other tasks.
    """
    with tempfile.TemporaryDirectory() as tmp:
        reg, dev, pub = _setup(tmp)
        img_path = Path(tmp) / "photo.jpg"
        img_path.write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 500_000)

        order = []

        class RecordingClient(FakeMqttClient):
            async def publish(self, topic, payload, **kw):
                order.append("snapshot-published")
                await super().publish(topic, payload, **kw)

        pub._client = RecordingClient()

        async def other_task():
            order.append("other-task-ran")

        await asyncio.gather(
            pub.publish_media_ready(dev, {
                "status": "ready", "media_path": str(img_path), "category": "eventImage",
            }),
            other_task(),
        )

        assert order == ["other-task-ran", "snapshot-published"], \
            "the snapshot read never yielded — it ran on the event loop"


async def test_publish_media_ready_skips_snapshot_for_video_only_category():
    with tempfile.TemporaryDirectory() as tmp:
        reg, dev, pub = _setup(tmp)
        clip_path = Path(tmp) / "Clips" / "clip.mp4"
        clip_path.parent.mkdir(parents=True, exist_ok=True)
        clip_path.write_bytes(b"video-bytes")

        await pub.publish_media_ready(dev, {
            "status": "ready", "media_path": str(clip_path), "category": "dynamicVideo",
        })

        topics = [t for t, _, _ in pub._client.published]
        assert "petkit-local/1/last_snapshot" not in topics


async def _pet_wired(event_store, pet_registry, tmp):
    from tests._pet_media import add_episode, media
    reg, dev, pub = _setup(tmp)
    pet = await pet_registry.create("Mia")
    pub.set_pet_source(pet_registry, event_store)
    await add_episode(event_store, tmp, "r1", pet_id=pet["id"], items=(
        media("dynamicVideo", "T5/Clips/c.mp4"), media("eventImage", "T5/Snapshots/p.jpg")))
    return pub, dev, pet


def _pet_images(pub, pid):
    return [t for t, _, _ in pub._client.published
            if t == f"petkit-local/pet/{pid}/last_visit_image"]


async def test_a_poster_landing_refreshes_its_pets_image(event_store, pet_registry, tmp_path):
    tmp = str(tmp_path)
    pub, dev, pet = await _pet_wired(event_store, pet_registry, tmp)
    row = {"status": "ready", "category": "eventImage", "related_event": "r1",
           "media_path": str(tmp_path / "T5/Snapshots/p.jpg")}
    await pub.publish_media_ready(dev, row)
    assert len(_pet_images(pub, pet["id"])) == 1
    await pub.stop_pet_media_settles()


async def test_a_chunk_refreshes_only_after_it_settles(event_store, pet_registry, tmp_path):
    tmp = str(tmp_path)
    pub, dev, pet = await _pet_wired(event_store, pet_registry, tmp)
    pub.pet_media_settle_delays = (0,)
    chunk = {"status": "ready", "category": "cloudDouble", "related_event": "r1",
             "device_id": 1, "media_path": str(tmp_path / "T5/.raw/c1.mp4")}
    await pub.publish_media_ready(dev, chunk)
    assert _pet_images(pub, pet["id"]) == []  # nothing changes on a chunk's arrival
    assert (1, "r1") in pub._settle_tasks
    await pub._settle_tasks[(1, "r1")]
    assert len(_pet_images(pub, pet["id"])) == 1
    assert pub._settle_tasks == {}


async def test_a_second_chunk_restarts_the_settle_timer(event_store, pet_registry, tmp_path):
    tmp = str(tmp_path)
    pub, dev, pet = await _pet_wired(event_store, pet_registry, tmp)
    pub.pet_media_settle_delays = (3600,)
    chunk = {"status": "ready", "category": "fullVideo", "related_event": "r1",
             "device_id": 1, "media_path": str(tmp_path / "T5/.raw/c1.mp4")}
    await pub.publish_media_ready(dev, chunk)
    first = pub._settle_tasks[(1, "r1")]
    await pub.publish_media_ready(dev, chunk)
    second = pub._settle_tasks[(1, "r1")]
    await asyncio.sleep(0)
    assert first is not second and first.cancelled()
    await pub.stop_pet_media_settles()
    assert second.cancelled() and pub._settle_tasks == {}


async def test_a_stitched_episode_refreshes_its_pets(event_store, pet_registry, tmp_path):
    tmp = str(tmp_path)
    pub, dev, pet = await _pet_wired(event_store, pet_registry, tmp)
    await pub.on_episode_stitched({"related_event": "r1", "category": "cloudDouble"})
    assert len(_pet_images(pub, pet["id"])) == 1


async def test_a_clip_arms_the_settle_timer_without_refreshing(event_store, pet_registry, tmp_path):
    """A Clip is accepted only once its episode has been silent for
    CLIP_SETTLE_SECONDS, so its arrival changes nothing yet; the settle timer
    must cover that, or a Clip-only visit would wait for the next weight sample."""
    from petkit_local.media.pet_media import CLIP_SETTLE_SECONDS
    tmp = str(tmp_path)
    pub, dev, pet = await _pet_wired(event_store, pet_registry, tmp)
    assert pub.pet_media_settle_delays[0] > CLIP_SETTLE_SECONDS
    pub.pet_media_settle_delays = (3600,)
    clip = {"status": "ready", "category": "dynamicVideo", "related_event": "r1",
            "device_id": 1, "media_path": str(tmp_path / "T5/Clips/c.mp4")}
    await pub.publish_media_ready(dev, clip)
    assert _pet_images(pub, pet["id"]) == []
    assert (1, "r1") in pub._settle_tasks
    await pub.stop_pet_media_settles()


async def test_a_refresh_only_reaches_pets_of_that_devices_episode(event_store, pet_registry,
                                                                   tmp_path):
    from tests._pet_media import add_episode, media
    tmp = str(tmp_path)
    pub, dev, pet = await _pet_wired(event_store, pet_registry, tmp)
    other = await pet_registry.create("Leo")
    await add_episode(event_store, tmp, "r1", pet_id=other["id"], device_id=2, items=(
        media("dynamicVideo", "T6/Clips/c.mp4"), media("eventImage", "T6/Snapshots/p.jpg")))
    await pub.on_episode_stitched({"related_event": "r1", "device_id": 1})
    assert len(_pet_images(pub, pet["id"])) == 1
    assert _pet_images(pub, other["id"]) == []
