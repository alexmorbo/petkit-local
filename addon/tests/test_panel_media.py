"""Panel API tests for the media/events/AI feature set: capabilities, AI
toggle, retention config, media file serving (+ path-traversal safety),
timeline, and pets."""
import asyncio
import os
import tempfile
from datetime import datetime
import time
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from petkit_local.ai.pets import PetRegistry
from petkit_local.devices.registry import DeviceRegistry
from petkit_local.events.store import MAX_FACES_PER_PET, EventStore
from petkit_local.media.retention import RetentionConfig
from petkit_local.web.hub import EventHub
from petkit_local.web.api.media import (
    _generate_video_thumb, _safe_media_path, _session_media_urls,
)
from petkit_local.web.panel import create_panel_app

JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"A" * 60


class FakeHAPublisher:
    def __init__(self):
        self.states = []
        self.pet_discoveries = []
        self.pet_states = []
        self.pet_unpublished = []

    async def unpublish_pet(self, pet_id):
        self.pet_unpublished.append(pet_id)
        return True

    async def publish_state(self, device):
        self.states.append(device.petkit_id)

    async def publish_pet_discovery(self, pet):
        self.pet_discoveries.append(pet["id"])

    async def publish_pet_state(self, pet, store):
        self.pet_states.append(pet["id"])


def _midday() -> float:
    """Today, local noon — a safe anchor for events the timeline groups by day.

    NOT `time.time()`. The timeline cuts days at LOCAL midnight, so a test that
    writes an event at `now` and its follow-up at `now + 9` puts the two on
    different days whenever it runs in the last nine seconds before midnight,
    and the session comes back missing its sub-events. That is exactly how this
    file failed once, at 00:00 on 2026-08-01, and passed on every rerun.
    Anchoring at noon keeps hours of headroom in both directions.
    """
    return datetime.now().replace(hour=12, minute=0, second=0,
                                  microsecond=0).timestamp()


def _panel(tmp, device_type="t5"):
    reg = DeviceRegistry()
    device = reg.get_or_create(petkit_id=1, device_type=device_type, serial_number="SN")
    hub = EventHub()
    store = EventStore(Path(tmp) / "petkit.db")
    retention = RetentionConfig()
    pet_registry = PetRegistry(store, str(Path(tmp) / "faces"))
    ha_publisher = FakeHAPublisher()
    media_root = str(Path(tmp) / "media")
    cfg = {"api_url": "http://x/6/", "capture": False, "capture_dir": "/nope",
           "data_dir": tmp, "media_root": media_root}
    app = create_panel_app(reg, None, hub, cfg, bridge=None,
                           event_store=store, retention_config=retention,
                           pet_registry=pet_registry, ha_publisher=ha_publisher)
    return app, reg, device, store, retention, pet_registry, ha_publisher, media_root


async def _client(app):
    c = TestClient(TestServer(app))
    await c.start_server()
    return c


def _stitched(cat, path):
    return {"category": cat, "status": "ready", "media_path": path,
            "stitch_state": "stitched", "created_at": 0}


def test_unstitched_playback_is_pending_not_a_fragment():
    """A visit's fullVideo arrives as many ~4s chunks; until stitch.py joins
    them the UI must NOT be handed a raw fragment — playback_url stays None
    and video_pending flags that a recording is on the way."""
    media = [
        {"category": "fullVideo", "status": "ready", "media_path": "/root/Playback/a.mp4",
         "stitch_state": None, "created_at": 1000.0},
        {"category": "fullVideo", "status": "ready", "media_path": "/root/Playback/b.mp4",
         "stitch_state": None, "created_at": 1004.0},
        {"category": "wasteCheck", "status": "ready", "media_path": "/root/Waste/1.jpg"},
    ]
    urls = _session_media_urls(media, "/root", now=1010.0)
    assert urls["playback_url"] is None
    assert urls["video_pending"] is True
    # stills are still available immediately
    assert urls["waste"] == ["Waste/1.jpg"]


def test_stitched_playback_is_shown():
    media = [_stitched("fullVideo", "/root/Playback/joined.mp4")]
    urls = _session_media_urls(media, "/root", now=9_999_999.0)
    assert urls["playback_url"] == "Playback/joined.mp4"
    assert urls["video_pending"] is False


def test_lone_settled_chunk_becomes_ready_after_quiet_period():
    from petkit_local.web.api.media import _STITCH_QUIET
    row = {"category": "fullVideo", "status": "ready", "media_path": "/root/Playback/only.mp4",
           "stitch_state": None, "created_at": 1000.0}
    # still fresh -> pending
    assert _session_media_urls([row], "/root", now=1000.0 + 5)["playback_url"] is None
    # settled past the stitch quiet window, still a lone chunk -> shown
    assert _session_media_urls([row], "/root", now=1000.0 + _STITCH_QUIET + 5)["playback_url"] \
        == "Playback/only.mp4"


def test_clip_and_poster_are_ready_immediately():
    """dynamicVideo (the app's 'Highlight') and eventImage are single complete
    files, not chunked, so they show at once — no stitching, no pending."""
    media = [
        {"category": "dynamicVideo", "status": "ready", "media_path": "/root/Clips/short.mp4"},
        {"category": "eventImage", "status": "ready", "media_path": "/root/Snapshots/p.jpg"},
        {"category": "wasteCheck", "status": "ready", "media_path": "/root/Waste/w.jpg"},
        _stitched("cloudDouble", "/root/Timelapse/tl.mp4"),
    ]
    urls = _session_media_urls(media, "/root", now=9_999_999.0)
    assert urls["highlight_url"] == "Clips/short.mp4"   # the clip, ready now
    assert urls["poster_url"] == "Snapshots/p.jpg"
    assert urls["snapshot_url"] == "Snapshots/p.jpg"    # poster preferred as thumb
    assert urls["waste"] == ["Waste/w.jpg"]
    assert urls["preview_url"] == "Timelapse/tl.mp4"    # stitched timelapse
    assert urls["video_pending"] is False               # nothing left to assemble


def test_session_media_urls_exposes_health_gallery():
    media = [
        {"category": "healthPic", "status": "ready", "media_path": "/root/Health/h1.jpg"},
        {"category": "healthPic", "status": "ready", "media_path": "/root/Health/h2.jpg"},
    ]
    urls = _session_media_urls(media, "/root")
    assert urls["health"] == ["Health/h1.jpg", "Health/h2.jpg"]


def test_session_media_urls_skips_not_ready_media():
    media = [{"category": "fullVideo", "status": "pending", "media_path": "/root/x.mp4"}]
    urls = _session_media_urls(media, "/root")
    assert urls["playback_url"] is None


async def test_capabilities_get_defaults_all_on():
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, *_ = _panel(tmp)
        c = await _client(app)
        try:
            r = await c.get("/api/devices/1/capabilities")
            body = await r.json()
            assert body["is_camera"] is True
            assert all(body["capabilities"].values())
        finally:
            await c.close()


async def test_capabilities_post_toggles_and_publishes_state():
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, store, retention, pet_registry, ha_publisher, media_root = _panel(tmp)
        c = await _client(app)
        try:
            r = await c.post("/api/devices/1/capabilities", json={"fullVideo": False})
            body = await r.json()
            assert body["capabilities"]["fullVideo"] is False
            assert device.config["capabilities"]["fullVideo"] is False
            assert ha_publisher.states == [1]
        finally:
            await c.close()


async def test_capabilities_404_for_unknown_device():
    with tempfile.TemporaryDirectory() as tmp:
        app, *_ = _panel(tmp)
        c = await _client(app)
        try:
            r = await c.get("/api/devices/999/capabilities")
            assert r.status == 404
        finally:
            await c.close()


async def test_ai_settings_defaults_on_for_ai_device():
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, *_ = _panel(tmp)
        c = await _client(app)
        try:
            r = await c.get("/api/devices/1/ai")
            body = await r.json()
            assert body == {"supports_ai": True, "ai_enabled": True}
        finally:
            await c.close()


async def test_ai_settings_toggle_off():
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, *_ = _panel(tmp)
        c = await _client(app)
        try:
            r = await c.post("/api/devices/1/ai", json={"ai_enabled": False})
            body = await r.json()
            assert body["ai_enabled"] is False
            assert device.config["ai_enabled"] is False
        finally:
            await c.close()


async def test_retention_get_defaults_and_post_updates():
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, store, retention, *_ = _panel(tmp)
        c = await _client(app)
        try:
            r = await c.get("/api/retention")
            body = await r.json()
            assert body["retention"]["fullVideo"]["max_mb"] == 1024

            r2 = await c.post("/api/retention", json={"fullVideo": {"max_mb": 5}})
            body2 = await r2.json()
            assert body2["retention"]["fullVideo"]["max_mb"] == 5
            assert (Path(tmp) / "retention.json").exists()
        finally:
            await c.close()


async def test_media_file_serves_within_root():
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, store, retention, pet_registry, ha_publisher, media_root = _panel(tmp)
        target = Path(media_root) / "Device" / "Playback" / "clip.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"video-bytes")
        c = await _client(app)
        try:
            r = await c.get("/api/media/Device/Playback/clip.mp4")
            assert r.status == 200
            assert await r.read() == b"video-bytes"
        finally:
            await c.close()


async def test_media_file_rejects_traversal_outside_root():
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, store, retention, pet_registry, ha_publisher, media_root = _panel(tmp)
        secret = Path(tmp) / "secret.txt"
        secret.write_text("top secret")
        c = await _client(app)
        try:
            r = await c.get("/api/media/../secret.txt")
            assert r.status == 404
        finally:
            await c.close()


async def test_media_thumb_returns_original_for_images():
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, store, retention, pet_registry, ha_publisher, media_root = _panel(tmp)
        target = Path(media_root) / "Device" / "Waste" / "photo.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(JPEG_BYTES)
        c = await _client(app)
        try:
            r = await c.get("/api/media/thumb/Device/Waste/photo.jpg")
            assert r.status == 200
            assert await r.read() == JPEG_BYTES
        finally:
            await c.close()


async def test_media_file_rejects_symlink_escape():
    """Containment is checked after resolving symlinks, so a link planted in
    the media tree cannot read the rest of the filesystem."""
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, store, retention, pet_registry, ha_publisher, media_root = _panel(tmp)
        secret = Path(tmp) / "secret.txt"
        secret.write_text("top secret")
        Path(media_root).mkdir(parents=True, exist_ok=True)
        (Path(media_root) / "link.txt").symlink_to(secret)
        c = await _client(app)
        try:
            r = await c.get("/api/media/link.txt")
            assert r.status == 404
        finally:
            await c.close()


def test_safe_media_path_contains_untrusted_input():
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, store, retention, pet_registry, ha_publisher, media_root = _panel(tmp)
        target = Path(media_root) / "Device" / "Waste" / "photo.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(JPEG_BYTES)
        # a sibling directory that merely shares the root's name prefix
        outside = Path(tmp) / "media-evil"
        outside.mkdir()
        (outside / "x.txt").write_text("nope")

        def resolve(rel):
            return _safe_media_path(
                make_mocked_request("GET", "/api/media/x", app=app), rel)

        assert resolve("Device/Waste/photo.jpg") == os.path.realpath(str(target))
        assert resolve("../media-evil/x.txt") is None
        assert resolve("/etc/hosts") is None       # absolute input is read as relative
        assert resolve("Device/Waste") is None     # directories are not served
        assert resolve("") is None


async def test_video_thumb_failure_leaves_nothing_behind():
    """A failed frame-grab must not leave a partial thumbnail (which would be
    cached forever) nor a stray temp file in the thumbs directory."""
    with tempfile.TemporaryDirectory() as tmp:
        thumbs = Path(tmp) / "thumbs"
        thumbs.mkdir()
        not_a_video = Path(tmp) / "broken.mp4"
        not_a_video.write_bytes(b"definitely not a video")
        thumb = thumbs / "out.jpg"

        assert await _generate_video_thumb(str(not_a_video), str(thumb)) is False
        assert not thumb.exists()
        assert list(thumbs.iterdir()) == []


async def test_concurrent_video_thumbs_never_expose_a_partial_file():
    """Two Timeline cards asking for the same missing thumbnail used to run two
    ffmpeg processes writing the same output path, so one request could serve a
    half-written JPEG. Each run now writes a temp file and renames it in."""
    from petkit_local.media.transcode import have_ffmpeg
    if not have_ffmpeg():
        return  # nothing to assert without ffmpeg; the failure path is covered above

    with tempfile.TemporaryDirectory() as tmp:
        video = Path(tmp) / "clip.mp4"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=5:duration=1",
            str(video), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        assert video.exists()

        thumbs = Path(tmp) / "thumbs"
        thumbs.mkdir()
        thumb = thumbs / "shared.jpg"
        results = await asyncio.gather(*[
            _generate_video_thumb(str(video), str(thumb)) for _ in range(3)
        ])
        assert all(results)
        assert thumb.read_bytes()[:2] == b"\xff\xd8"       # a complete JPEG
        assert [p.name for p in thumbs.iterdir()] == ["shared.jpg"]  # no temp leftovers


async def test_timeline_returns_sessions_for_today():
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, store, retention, pet_registry, ha_publisher, media_root = _panel(tmp)
        now = _midday()
        await store.upsert_event({"device_id": 1, "event_type": "pet_out", "event_kind": "toilet_visit",
                                  "ts": now, "related_event": "r1"})
        c = await _client(app)
        try:
            r = await c.get("/api/timeline")
            body = await r.json()
            assert body["counts"]["all"] == 1
            assert len(body["sessions"]) == 1
            assert body["sessions"][0]["event_kind"] == "toilet_visit"
            assert body["sessions"][0]["device_name"]
        finally:
            await c.close()


async def test_timeline_names_the_recognised_pet():
    """The whole point of the AI chain: a card has to say WHICH cat. It used to
    carry a bare `pet_id` that nothing resolved, so the Timeline never did."""
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, store, retention, pet_registry, ha_publisher, media_root = _panel(tmp)
        import time
        pet = await pet_registry.create("Appka", device_ids=[1])
        face = await pet_registry.add_face(pet["id"], JPEG_BYTES)
        await store.upsert_event({"device_id": 1, "event_type": "pet_out",
                                  "event_kind": "toilet_visit", "ts": time.time(),
                                  "related_event": "r1", "pet_ref": pet["id"],
                                  "pet_id": pet["id"]})
        c = await _client(app)
        try:
            s = (await (await c.get("/api/timeline")).json())["sessions"][0]
            assert s["pet_name"] == "Appka"
            assert s["pet_photo_url"] == f"api/pets/{pet['id']}/faces/{face['id']}/photo"
        finally:
            await c.close()


async def test_timeline_leaves_an_unattributed_card_unnamed():
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, store, retention, pet_registry, ha_publisher, media_root = _panel(tmp)
        import time
        # reported by the device but bound to nobody — the normal state for a
        # box still matching against PetKit's cached faces
        await store.upsert_event({"device_id": 1, "event_type": "pet_out",
                                  "event_kind": "toilet_visit", "ts": time.time(),
                                  "related_event": "r1", "pet_ref": 101392625})
        c = await _client(app)
        try:
            s = (await (await c.get("/api/timeline")).json())["sessions"][0]
            assert s["pet_name"] is None
            assert s["pet_photo_url"] is None
        finally:
            await c.close()


async def test_timeline_filters_by_query_param():
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, store, retention, pet_registry, ha_publisher, media_root = _panel(tmp)
        now = _midday()
        await store.upsert_event({"device_id": 1, "event_type": "pet_out", "event_kind": "toilet_visit", "ts": now})
        await store.upsert_event({"device_id": 1, "event_type": "error_start", "event_kind": "error", "ts": now})
        c = await _client(app)
        try:
            r = await c.get("/api/timeline?filter=fault")
            body = await r.json()
            assert body["counts"]["all"] == 2  # counts are over the whole day, unfiltered
            assert len(body["sessions"]) == 1
            assert body["sessions"][0]["event_kind"] == "error"
        finally:
            await c.close()


async def test_pets_crud_via_api():
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, store, retention, pet_registry, ha_publisher, media_root = _panel(tmp)
        c = await _client(app)
        try:
            r = await c.post("/api/pets", json={"name": "Mruczek", "device_ids": [1]})
            pet = (await r.json())["pet"]
            assert pet["name"] == "Mruczek"
            assert ha_publisher.pet_discoveries == [pet["id"]]

            r2 = await c.get("/api/pets")
            assert len(( await r2.json())["pets"]) == 1

            r3 = await c.post(f"/api/pets/{pet['id']}", json={"name": "Mruczek II"})
            assert (await r3.json())["pet"]["name"] == "Mruczek II"

            r4 = await c.delete(f"/api/pets/{pet['id']}")
            assert (await r4.json())["ok"] is True
            assert (await (await c.get("/api/pets")).json())["pets"] == []
            assert ha_publisher.pet_unpublished == [pet["id"]]
        finally:
            await c.close()


async def test_adding_and_removing_a_device_on_a_pet():
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, store, retention, pet_registry, ha_publisher, media_root = _panel(tmp)
        c = await _client(app)
        try:
            pet = (await (await c.post("/api/pets", json={"name": "Mia", "device_ids": [1]}))
                   .json())["pet"]
            await pet_registry.add_face(pet["id"], JPEG_BYTES)
            pid = pet["id"]

            def served(payload):
                return [e["id"] for e in payload["result"]["list"]]

            assert served(await pet_registry.discern_pic_payload(2, "h")) == []

            r = await c.post(f"/api/pets/{pid}", json={"device_ids": [1, 2, 2]})
            assert r.status == 200
            assert (await r.json())["pet"]["device_ids_json"] == "[1, 2]"
            # Served on device 2's next dev_discern_pic, with nothing pushed.
            assert served(await pet_registry.discern_pic_payload(2, "h")) == [pid]
            assert served(await pet_registry.discern_pic_payload(1, "h")) == [pid]

            r = await c.post(f"/api/pets/{pid}", json={"device_ids": [1]})
            assert served(await pet_registry.discern_pic_payload(2, "h")) == []

            # The last device can go too: the pet just stops being recognised.
            r = await c.post(f"/api/pets/{pid}", json={"device_ids": []})
            assert r.status == 200
            assert (await r.json())["pet"]["device_ids_json"] == "[]"
            assert served(await pet_registry.discern_pic_payload(1, "h")) == []

            # The pet's HA device is republished, never removed, by a change.
            assert ha_publisher.pet_discoveries.count(pid) == 4
            assert ha_publisher.pet_unpublished == []
        finally:
            await c.close()


async def test_creating_a_duplicate_name_is_refused_with_the_existing_id():
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, store, retention, pet_registry, ha_publisher, media_root = _panel(tmp)
        c = await _client(app)
        try:
            mia = (await (await c.post("/api/pets", json={"name": "Mia", "device_ids": [1]}))
                   .json())["pet"]

            r = await c.post("/api/pets", json={"name": "  mIA ", "device_ids": [2]})
            assert r.status == 409
            body = await r.json()
            assert body["existing_id"] == mia["id"]
            assert body["error"] == ("A pet called Mia already exists — add this device "
                                     "to it from its card instead")
            assert len(await pet_registry.all()) == 1

            r = await c.post("/api/pets", json={"name": "Mia", "device_ids": [2],
                                                "allow_duplicate": True})
            assert r.status == 200
            assert len(await pet_registry.all()) == 2

            r = await c.post("/api/pets", json={"name": "Mia Two"})
            assert r.status == 200
        finally:
            await c.close()


async def test_pet_face_upload_rejects_non_jpeg():
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, store, retention, pet_registry, ha_publisher, media_root = _panel(tmp)
        pet = await pet_registry.create("Mruczek")
        c = await _client(app)
        try:
            r = await c.post(f"/api/pets/{pet['id']}/faces", data=b"not a jpeg")
            assert r.status == 400
            assert await pet_registry.faces(pet["id"]) == []
        finally:
            await c.close()


async def test_pet_face_upload_accepts_jpeg_and_serves_it_back():
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, store, retention, pet_registry, ha_publisher, media_root = _panel(tmp)
        pet = await pet_registry.create("Mruczek")
        c = await _client(app)
        try:
            r = await c.post(f"/api/pets/{pet['id']}/faces", data=JPEG_BYTES)
            assert r.status == 200
            face = (await r.json())["face"]

            # the panel renders thumbnails from its own app; the device's
            # /faces/{name} route lives on a different app on another port
            r = await c.get(f"/api/pets/{pet['id']}/faces/{face['id']}/photo")
            assert r.status == 200
            assert await r.read() == JPEG_BYTES

            r = await c.get(f"/api/pets/{pet['id']}/faces")
            assert [f["id"] for f in (await r.json())["faces"]] == [face["id"]]

            r = await c.delete(f"/api/pets/{pet['id']}/faces/{face['id']}")
            assert (await r.json())["ok"] is True
            assert await pet_registry.faces(pet["id"]) == []
        finally:
            await c.close()


async def test_pet_face_upload_stops_at_the_cap():
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, store, retention, pet_registry, ha_publisher, media_root = _panel(tmp)
        pet = await pet_registry.create("Mruczek")
        c = await _client(app)
        try:
            for _ in range(MAX_FACES_PER_PET):
                assert (await c.post(f"/api/pets/{pet['id']}/faces", data=JPEG_BYTES)).status == 200
            r = await c.post(f"/api/pets/{pet['id']}/faces", data=JPEG_BYTES)
            assert r.status == 400
            assert str(MAX_FACES_PER_PET) in (await r.json())["error"]
        finally:
            await c.close()


async def test_unbound_pet_refs_are_offered_and_binding_backfills_history():
    """The device reports PetKit's cached pet id until it re-syncs. We never
    guess the mapping — the user binds it, and history is attributed at once."""
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, store, retention, pet_registry, ha_publisher, media_root = _panel(tmp)
        pet = await pet_registry.create("Mruczek")
        for i in range(3):
            await store.upsert_event({"device_id": 100, "event_type": "10", "ts": float(i),
                                      "event_uid": f"u{i}", "pet_ref": 101392625})
        c = await _client(app)
        try:
            r = await c.get("/api/pets/unbound")
            assert (await r.json())["unbound"] == [
                {"pet_ref": 101392625, "count": 3, "last_ts": 2.0}]

            r = await c.post(f"/api/pets/{pet['id']}", json={"device_pet_ids": [101392625]})
            assert (await r.json())["bound_events"] == 3

            # bound ids stop being offered, and the past events now name the pet
            assert (await (await c.get("/api/pets/unbound")).json())["unbound"] == []
            assert await pet_registry.resolve_pet_ref(101392625) == pet["id"]
        finally:
            await c.close()


async def test_timeline_puts_visit_media_on_the_card_and_cleaning_media_on_its_line():
    """The layout the official app uses: the visit's own video on the card,
    the cleaning cycle's waste gallery on its 'Cleaning done' line — never
    mixed together."""
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, store, retention, pet_registry, ha_publisher, media_root = _panel(tmp)
        now = _midday()

        await store.upsert_event({"event_uid": "v:10", "related_event": "visit", "device_id": 1,
                                  "event_type": "10", "event_kind": "toilet_visit", "ts": now})
        await store.upsert_event({"event_uid": "c:3", "related_event": "clean", "parent_event": "visit",
                                  "device_id": 1, "event_type": "3", "event_kind": "cleaning", "ts": now + 5})
        await store.upsert_event({"event_uid": "c:5", "related_event": "clean", "parent_event": "visit",
                                  "device_id": 1, "event_type": "5", "event_kind": "cleaning", "ts": now + 9})

        # a stitched (finished-processing) recording, so playback is shown
        await store.upsert_media({"file_id": "vid", "device_id": 1, "related_event": "visit",
                                  "category": "fullVideo", "status": "ready", "stitch_state": "stitched",
                                  "media_path": f"{media_root}/Playback/visit.mp4"})
        await store.upsert_media({"file_id": "clip", "device_id": 1, "related_event": "visit",
                                  "category": "dynamicVideo", "status": "ready",
                                  "media_path": f"{media_root}/Clips/visit.mp4"})
        for i in range(5):
            await store.upsert_media({"file_id": f"w{i}", "device_id": 1, "related_event": "clean",
                                      "category": "wasteCheck", "status": "ready",
                                      "media_path": f"{media_root}/Waste/w{i}.jpg"})

        c = await _client(app)
        try:
            body = await (await c.get("/api/timeline")).json()
            visit = next(s for s in body["sessions"] if s["kind"] == "visit")

            # visit card: both videos -> the Highlight/Playback toggle, no waste
            assert visit["media"]["playback_url"] == "Playback/visit.mp4"
            assert visit["media"]["highlight_url"] == "Clips/visit.mp4"
            assert visit["media"]["waste"] == []

            subs = {s["event_type"]: s for s in visit["sub_events"]}
            # all 5 waste photos, on the completion line only
            assert len(subs["5"]["media"]["waste"]) == 5
            assert subs["3"]["media"] is None
            # and the mechanism step is marked as collapsible detail
            assert subs["3"]["detail"] is True and subs["5"]["detail"] is False
        finally:
            await c.close()


# --- per-event detail endpoint + decoded timeline payload ------------------

async def test_event_detail_returns_the_whole_record():
    """`GET /api/timeline/{id}` is what the Debug info expander opens.

    Before it existed, `content_json` and `state_json` were written on every
    event and read by nothing, so a wrong label could only be diagnosed by
    opening the database.
    """
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, store, *_ = _panel(tmp)
        content = {"is_shit": 1, "shit_weight": 10, "pet_weight": 2320,
                   "time_in": 1784745533, "time_out": 1784745547,
                   "score_info": [{"id": 101392625, "score": 131}]}
        event_id = await store.upsert_event({
            "device_id": 1, "device_type": "t5", "event_type": "10",
            "event_kind": "toilet_visit", "ts": time.time(), "source": "http",
            "related_event": "r1", "event_uid": "r1:10",
            "content_json": content, "state_json": {"power": 1},
        })
        c = await _client(app)
        try:
            body = await (await c.get(f"/api/timeline/{event_id}")).json()

            assert body["event"]["event_uid"] == "r1:10"
            assert body["event"]["source"] == "http"
            assert body["event"]["label"] == "Toilet visit"
            # What the table knows, including its provenance.
            assert body["code"]["label"] == "Toilet visit"
            assert body["code"]["grade"] == "confirmed"
            assert body["code"]["firmware"]
            # Every content key decoded, none dropped.
            assert {f["key"] for f in body["decoded"]} == set(content)
            decoded = {f["key"]: f for f in body["decoded"]}
            assert decoded["shit_weight"]["text"] == "10 g"
            assert decoded["pet_weight"]["text"] == "2.32 kg"
            # ...and the raw payloads, so our reading can be checked.
            assert body["content"] == content
            assert body["state"] == {"power": 1}
        finally:
            await c.close()


async def test_event_detail_rejects_bad_input_without_raising():
    with tempfile.TemporaryDirectory() as tmp:
        app, *_ = _panel(tmp)
        c = await _client(app)
        try:
            assert (await c.get("/api/timeline/999999")).status == 404
            assert (await c.get("/api/timeline/not-a-number")).status == 400
            # The hub's event list must not be shadowed by the dynamic route.
            assert (await c.get("/api/events")).status == 200
        finally:
            await c.close()


async def test_timeline_labels_are_decoded_from_content():
    """A lone cleaning used to read a generic "Cleaning done" because session
    cards carried no content at all."""
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, store, *_ = _panel(tmp)
        now = _midday()
        await store.upsert_event({
            "device_id": 1, "device_type": "t5", "event_type": "5",
            "event_kind": "cleaning", "ts": now, "related_event": "c1",
            "content_json": {"start_reason": 2, "result": 0, "err": "NULL",
                             "litter_percent": 100},
        })
        c = await _client(app)
        try:
            body = await (await c.get("/api/timeline")).json()
            card = body["sessions"][0]
            assert card["label"] == "Manual cleaning completed"
            # err="NULL" means NO error -- it must not become a cause.
            assert "NULL" not in card["label"]
            assert card["bits"] == ["litter 100%"]
        finally:
            await c.close()


async def test_timeline_reports_the_timezone_it_bucketed_by():
    """The date picker has to cut "today" where the server did."""
    with tempfile.TemporaryDirectory() as tmp:
        app, *_ = _panel(tmp)
        c = await _client(app)
        try:
            body = await (await c.get("/api/timeline")).json()
            assert isinstance(body["tz_offset"], int)
            assert -12 * 3600 <= body["tz_offset"] <= 14 * 3600
        finally:
            await c.close()


async def test_an_error_pair_is_one_card_and_counts_as_a_fault():
    """Codes 1 and 2 used to classify as "other", so a real hall-sensor fault
    rendered as two anonymous "Event 1"/"Event 2" cards and no filter caught
    them. It is a FAULT, not a health alert: the box is broken, not the cat."""
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, store, *_ = _panel(tmp)
        now = _midday()
        for event_type, extra in (("1", {}), ("2", {"start_time": 1784827076})):
            await store.upsert_event({
                "device_id": 1, "device_type": "t5", "event_type": event_type,
                "event_kind": "error", "ts": now, "related_event": "e1",
                "event_uid": f"e1:{event_type}",
                "content_json": {"err": "hallB", "msg": "", "detail": "", **extra},
            })
        c = await _client(app)
        try:
            body = await (await c.get("/api/timeline")).json()
            assert body["counts"]["fault"] == 1
            assert body["counts"]["health_alert"] == 0
            assert len(body["sessions"]) == 1
            card = body["sessions"][0]
            assert card["label"] == "Error - hall sensor (B)"
            # The "cleared" half is a step of the same card, not a lost row.
            assert [s["label"] for s in card["sub_events"]] == \
                ["Error cleared - hall sensor (B)"]
        finally:
            await c.close()


async def test_a_real_phone_photo_is_not_rejected_by_the_body_cap():
    """aiohttp's 1 MiB default 413'd every real photo, and the panel then
    failed to parse the text/plain error as JSON — so the button silently did
    nothing. Typical phone JPEGs are 2-8 MB."""
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, store, retention, pet_registry, ha_publisher, media_root = _panel(tmp)
        pet = await pet_registry.create("Mruczek")
        big = JPEG_BYTES + b"\x00" * (3 * 1024 * 1024)
        c = await _client(app)
        try:
            r = await c.post(f"/api/pets/{pet['id']}/faces", data=big)
            assert r.status == 200, await r.text()
            assert (await r.json())["face"]["id"]
        finally:
            await c.close()


async def test_a_face_route_will_not_touch_another_pets_photo():
    """Both ids are in the path and both must be checked — matching only
    face_id made DELETE /api/pets/999/faces/N delete face N whoever owned it,
    unlink its file, and answer ok."""
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, store, retention, pet_registry, ha_publisher, media_root = _panel(tmp)
        owner = await pet_registry.create("Owner")
        other = await pet_registry.create("Other")
        face = await pet_registry.add_face(owner["id"], JPEG_BYTES)
        c = await _client(app)
        try:
            assert (await c.get(f"/api/pets/{other['id']}/faces/{face['id']}/photo")).status == 404
            assert (await c.delete(f"/api/pets/{other['id']}/faces/{face['id']}")).status == 404
            assert (await c.delete(f"/api/pets/9999/faces/{face['id']}")).status == 404
            # the real owner is unharmed
            assert len(await pet_registry.faces(owner["id"])) == 1
            assert (await c.delete(f"/api/pets/{owner['id']}/faces/{face['id']}")).status == 200
            assert await pet_registry.faces(owner["id"]) == []
        finally:
            await c.close()


async def test_unbinding_a_ref_clears_the_history_it_attributed():
    """Otherwise history stays on this pet while new events resolve to nobody."""
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, store, retention, pet_registry, ha_publisher, media_root = _panel(tmp)
        pet = await pet_registry.create("Mruczek")
        await store.upsert_event({"device_id": 1, "event_type": "10", "ts": 1.0,
                                  "event_uid": "u1", "pet_ref": 555})
        c = await _client(app)
        try:
            await c.post(f"/api/pets/{pet['id']}", json={"device_pet_ids": [555]})
            r = await c.post(f"/api/pets/{pet['id']}", json={"device_pet_ids": []})
            assert (await r.json())["bound_events"] == 1

            assert await pet_registry.resolve_pet_ref(555) is None
            # and it is offered for binding again rather than vanishing
            assert [u["pet_ref"] for u in (await (await c.get("/api/pets/unbound")).json())["unbound"]] == [555]
        finally:
            await c.close()


async def test_rebinding_a_ref_moves_history_and_future_together():
    """Binding a ref already held by another pet used to move only the history:
    resolve_pet_ref kept handing future events to the lower pet id, so past and
    future ended up on different animals with no way back."""
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, store, retention, pet_registry, ha_publisher, media_root = _panel(tmp)
        first = await pet_registry.create("First")
        second = await pet_registry.create("Second")
        await store.upsert_event({"device_id": 1, "event_type": "10", "ts": 1.0,
                                  "event_uid": "u1", "pet_ref": 555})
        c = await _client(app)
        try:
            await c.post(f"/api/pets/{first['id']}", json={"device_pet_ids": [555]})
            await c.post(f"/api/pets/{second['id']}", json={"device_pet_ids": [555]})

            # future events go to Second...
            assert await pet_registry.resolve_pet_ref(555) == second["id"]
            # ...only Second claims it, so nothing can diverge...
            assert await store.pets_claiming_ref(555) == [second["id"]]
            # ...and the history moved with it
            rows = await store.all_events()
            assert [r["pet_id"] for r in rows] == [second["id"]]
        finally:
            await c.close()


async def test_an_unresolved_ref_for_one_of_our_own_pets_is_still_offered():
    """A row can carry our own pet id with a NULL pet_id — ingested with no pet
    registry, or recovered by backfill. Excluding those left them unattributed
    forever with nothing offering to fix them."""
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, store, retention, pet_registry, ha_publisher, media_root = _panel(tmp)
        pet = await pet_registry.create("Mruczek")
        await store.upsert_event({"device_id": 1, "event_type": "10", "ts": 1.0,
                                  "event_uid": "u1", "pet_ref": pet["id"]})
        c = await _client(app)
        try:
            offered = (await (await c.get("/api/pets/unbound")).json())["unbound"]
            assert [u["pet_ref"] for u in offered] == [pet["id"]]
        finally:
            await c.close()


async def test_timeline_filters_by_pet():
    """Picking a cat means "this is her timeline" — so the chip counts narrow
    with her too, the way the device filter behaves and unlike the chips
    themselves."""
    with tempfile.TemporaryDirectory() as tmp:
        app, reg, device, store, retention, pet_registry, ha_publisher, media_root = _panel(tmp)
        now = _midday()
        appka = await pet_registry.create("Appka", device_ids=[1])
        other = await pet_registry.create("Other", device_ids=[1])
        await store.upsert_event({"device_id": 1, "event_type": "pet_out", "ts": now,
                                  "event_kind": "toilet_visit", "event_uid": "a",
                                  "related_event": "ra", "pet_id": appka["id"]})
        await store.upsert_event({"device_id": 1, "event_type": "pet_out", "ts": now,
                                  "event_kind": "toilet_visit", "event_uid": "b",
                                  "related_event": "rb", "pet_id": other["id"]})
        # Unattributed — a cleaning belongs to nobody. Dated well away from the
        # visits so `group_sessions` cannot adopt it as one of their sub-events
        # (it attaches an unplaced cleaning to a visit within 600s).
        await store.upsert_event({"device_id": 1, "event_type": "5", "ts": now - 4000,
                                  "event_kind": "cleaning", "event_uid": "c",
                                  "related_event": "rc"})
        c = await _client(app)
        try:
            everything = await (await c.get("/api/timeline")).json()
            assert everything["counts"]["toileting"] == 2

            mine = await (await c.get(f"/api/timeline?pet={appka['id']}")).json()
            assert [s["pet_name"] for s in mine["sessions"]] == ["Appka"]
            # counts narrow with the selection, and the unattributed cleaning is
            # not hers
            assert mine["counts"] == {"all": 1, "pet": 0, "toileting": 1,
                                      "drinking": 0, "feeding": 0, "cleaning": 0,
                                      "health_alert": 0, "fault": 0}

            # a pet id nobody matches empties the view rather than ignoring it
            none = await (await c.get("/api/timeline?pet=99999")).json()
            assert none["sessions"] == [] and none["counts"]["all"] == 0
            # garbage is ignored, not a 500 — the view is exactly the unfiltered one
            bad = await (await c.get("/api/timeline?pet=notanid")).json()
            assert bad["counts"] == everything["counts"]
        finally:
            await c.close()
