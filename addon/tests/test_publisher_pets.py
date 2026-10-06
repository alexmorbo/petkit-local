import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from petkit_local.devices.registry import DeviceRegistry
from petkit_local.ha import publisher as publisher_mod
from petkit_local.ha.discovery import discovery_topic
from petkit_local.ha.entities.pet import PET_SENSORS
from petkit_local.ha.publisher import HAPublisher
from petkit_local.utils.timeutil import local_day_bounds
from tests._fakes import FakeMqttClient


def _setup(media_root=None):
    reg = DeviceRegistry()
    dev = reg.get_or_create(petkit_id=1, device_type="t5", serial_number="SN")
    pub = HAPublisher(reg, {"media_root": media_root} if media_root else {})
    pub._client = FakeMqttClient()
    pub._connected = True
    return reg, dev, pub


async def test_publish_pet_discovery_uses_distinct_identifiers_and_topics():
    reg, dev, pub = _setup()
    pet = {"id": 1, "name": "Mruczek"}  # same numeric id as device id=1 on purpose

    await pub.publish_pet_discovery(pet)

    configs = [json.loads(p) for t, p, _ in pub._client.published if t.endswith("/config")]
    assert configs, "expected at least one discovery config"
    for cfg in configs:
        assert cfg["device"]["identifiers"] == ["petkit_pet_1"]
        if "image_topic" in cfg:
            # Raw JPEG bytes on a pet topic, never the device namespace
            # (`petkit-local/1/...` would be device id 1's).
            assert cfg["image_topic"].startswith("petkit-local/pet/1/")
            assert cfg["content_type"] == "image/jpeg"
            assert "state_topic" not in cfg
        else:
            assert cfg["state_topic"] == "petkit-local/pet/1/state"
        assert cfg["availability"]["topic"] == "petkit-local/pet/1/availability"
        assert cfg["unique_id"].startswith("petkit_pet_1_")

    avail = [p for t, p, kw in pub._client.published if t == "petkit-local/pet/1/availability"]
    assert avail == ["online"]


async def test_publish_pet_state_builds_stats_and_resolves_device_name(event_store):
    reg, dev, pub = _setup()
    await event_store.upsert_event({"device_id": 1, "event_type": "pet_out",
                                    "event_kind": "toilet_visit", "pet_id": 7, "ts": 1000.0,
                                    "content_json": '{"pet_weight": 2200}'})

    await pub.publish_pet_state({"id": 7, "name": "Mruczek"}, event_store)

    state_msgs = [p for t, p, _ in pub._client.published if t == "petkit-local/pet/7/state"]
    assert state_msgs
    state = json.loads(state_msgs[-1])["state"]
    assert state["lastVisitWeight"] == 2200.0
    assert state["lastDeviceUsed"] == "T5 SN"
    assert state["lastVisit"] is not None


async def test_publish_pet_state_noop_without_client(event_store):
    reg = DeviceRegistry()
    pub = HAPublisher(reg, {})
    await pub.publish_pet_state({"id": 1, "name": "X"}, event_store)  # must not raise


async def test_pet_state_carries_weight_and_drinks_and_is_retained(event_store):
    reg, dev, pub = _setup()
    await event_store.upsert_event({"device_id": 1, "device_type": "t5", "event_type": "pet_out",
                                    "event_kind": "toilet_visit", "pet_id": 7, "ts": 1000.0,
                                    "content_json": '{"pet_weight": 4623}'})
    await event_store.upsert_event({"device_id": 1, "device_type": "w7h",
                                    "event_type": "drink_over", "event_kind": "drinking",
                                    "pet_id": 7, "ts": 2000.0})

    await pub.publish_pet_state({"id": 7, "name": "Gami"}, event_store)

    [(_, payload, kw)] = [m for m in pub._client.published if m[0] == "petkit-local/pet/7/state"]
    assert kw["retain"] is True
    state = json.loads(payload)["state"]
    assert state["weight"] == 4623
    assert state["lastDrink"].startswith("1970-01-01T00:33:20")
    assert "drinksToday" in state


async def test_pet_discovery_announces_the_new_sensors():
    reg, dev, pub = _setup()
    await pub.publish_pet_discovery({"id": 3, "name": "Gami"})

    configs = {json.loads(p)["unique_id"]: json.loads(p)
               for t, p, _ in pub._client.published if t.endswith("/config")}
    for key in ("weight", "last_drink", "drinks_today"):
        assert f"petkit_pet_3_{key}" in configs, key
    weight = configs["petkit_pet_3_weight"]
    assert weight["state_class"] == "measurement"
    assert weight["unit_of_measurement"] == "g"
    assert weight["device_class"] == "weight"
    assert configs["petkit_pet_3_last_drink"]["device_class"] == "timestamp"


async def test_a_reconnect_republishes_every_pet(event_store, pet_registry):
    """A broker that lost its retained messages left every pet sensor unknown
    until the cat next used the box: the reconnect has to replay pets too."""
    reg, dev, pub = _setup()
    pet = await pet_registry.create("Mia")
    pub.set_pet_source(pet_registry, event_store)

    await pub._publish_all_discovery()

    topics = [t for t, _, _ in pub._client.published]
    assert f"petkit-local/pet/{pet['id']}/state" in topics
    assert any(t.endswith("/config") and f"petkit_pet_{pet['id']}" in t for t in topics)
    retained = {t: kw["retain"] for t, _, kw in pub._client.published}
    assert retained[f"petkit-local/pet/{pet['id']}/state"] is True


async def test_republishing_pets_swallows_a_store_failure():
    reg, dev, pub = _setup()

    class Broken:
        async def all(self):
            raise RuntimeError("database is locked")

    pub.set_pet_source(Broken(), object())
    await pub.publish_all_pets()  # must not raise into the reconnect loop


async def test_republishing_pets_is_a_noop_without_a_source():
    reg, dev, pub = _setup()
    await pub.publish_all_pets()
    assert pub._client.published == []


async def test_pet_day_rollover_sleeps_until_just_after_local_midnight(monkeypatch):
    reg, dev, pub = _setup()
    now = 1_700_000_000.0
    monkeypatch.setattr(publisher_mod, "time", SimpleNamespace(time=lambda: now))
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)
        if len(delays) > 1:
            raise asyncio.CancelledError

    published = []

    async def fake_publish_all_pets():
        published.append(True)

    monkeypatch.setattr(publisher_mod, "asyncio",
                        SimpleNamespace(sleep=fake_sleep, CancelledError=asyncio.CancelledError))
    monkeypatch.setattr(pub, "publish_all_pets", fake_publish_all_pets)

    with pytest.raises(asyncio.CancelledError):
        await pub.pet_day_rollover()

    _, end, _ = local_day_bounds(now=now)
    assert delays[0] == max(1.0, end - now + 1)
    assert published == [True]


# --- removing a deleted pet from Home Assistant ------------------------------

def _pet_clear_topics(pid):
    return {discovery_topic(e, pid, "homeassistant", identifiers=[f"petkit_pet_{pid}"])
            for e in PET_SENSORS} | {f"petkit-local/pet/{pid}/state",
                                     f"petkit-local/pet/{pid}/availability",
                                     f"petkit-local/pet/{pid}/last_visit_image",
                                     f"petkit-local/pet/{pid}/last_drink_image"}


async def test_unpublish_pet_empties_every_config_and_its_state():
    reg, dev, pub = _setup()
    await pub.publish_pet_discovery({"id": 7, "name": "Mia"})
    announced = {t for t, p, _ in pub._client.published if t.endswith("/config")}
    pub._client.published.clear()

    assert await pub.unpublish_pet(7) is True

    sent = pub._client.published
    topics = {t for t, _, _ in sent}
    assert topics == _pet_clear_topics(7)
    # Exactly what was announced, plus the retained runtime topics.
    assert {t for t in topics if t.endswith("/config")} == announced
    # Configs first, so HA drops each image entity before its bytes go.
    order = [t for t, _, _ in sent]
    last_config = max(i for i, t in enumerate(order) if t.endswith("/config"))
    assert order.index("petkit-local/pet/7/last_visit_image") > last_config
    assert order.index("petkit-local/pet/7/last_drink_image") > last_config
    assert "homeassistant/sensor/petkit_pet_7_last_visit/config" in topics
    assert "petkit-local/pet/7/state" in topics
    assert "petkit-local/pet/7/availability" in topics
    assert all(p == "" and kw == {"retain": True} for _, p, kw in sent)
    assert pub._pending_clears == {}


async def test_unpublish_pet_while_disconnected_is_cleared_on_reconnect(event_store):
    reg, dev, pub = _setup()
    pub._connected = False

    assert await pub.unpublish_pet(7) is False
    assert pub._client.published == []
    assert ("pet", 7) in pub._pending_clears

    # What start() does on (re)connect: tombstones, then the live registries
    # -- which no longer hold pet 7.
    remaining = SimpleNamespace(all=_async_value([{"id": 8, "name": "Gami"}]))
    pub.set_pet_source(remaining, event_store)
    pub._connected = True
    assert await pub._flush_pending_clears() is True
    await pub._publish_all_discovery()

    sent = pub._client.published
    cleared = {t for t, p, _ in sent if p == ""}
    assert _pet_clear_topics(7) <= cleared
    # Nothing re-announced the deleted pet.
    assert not [t for t, p, _ in sent if "petkit_pet_7_" in t and p != ""]
    assert not [t for t, p, _ in sent if t.startswith("petkit-local/pet/7/") and p != ""]
    assert any("petkit_pet_8_" in t and p for t, p, _ in sent)

    pub._client.published.clear()
    assert await pub._flush_pending_clears() is True
    assert pub._client.published == []


def _async_value(value):
    async def f():
        return value
    return f


async def test_a_failed_clear_stays_pending():
    class Refusing:
        def __init__(self):
            self.calls = 0

        async def publish(self, topic, payload, **kw):
            self.calls += 1
            raise RuntimeError("connection lost")

    reg, dev, pub = _setup()
    pub._client = Refusing()

    assert await pub.unpublish_pet(7) is False
    assert pub._client.calls == 1  # the rest waits for the reconnect
    assert len(pub._pending_clears[("pet", 7)]) == len(_pet_clear_topics(7))
    assert pub._connected is False


async def test_republishing_a_pet_cancels_its_tombstone():
    reg, dev, pub = _setup()
    pub._connected = False
    await pub.unpublish_pet(7)
    assert ("pet", 7) in pub._pending_clears

    await pub.publish_pet_discovery({"id": 7, "name": "Mia"})
    assert pub._pending_clears == {}


async def test_start_flushes_queued_removals_before_any_discovery(monkeypatch, event_store):
    aiomqtt = pytest.importorskip("aiomqtt")
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=1, device_type="t5", serial_number="SN")
    pub = HAPublisher(reg, {"ha_mqtt_host": "broker"})
    # Deleted while HA's broker was down: nothing to send it on yet.
    assert await pub.unpublish_pet(7) is False
    pub.set_pet_source(SimpleNamespace(all=_async_value([{"id": 8, "name": "Gami"}])),
                       event_store)

    connects = []

    class Messages:
        def __aiter__(self):
            return self

        async def __anext__(self):
            # Stop start()'s forever-loop right after the first connect.
            raise asyncio.CancelledError

    class FakeClient(FakeMqttClient):
        def __init__(self, **kw):
            super().__init__()
            self.kw = kw
            self.messages = Messages()
            connects.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def subscribe(self, topic):
            pass

    monkeypatch.setattr(aiomqtt, "Client", FakeClient)
    with pytest.raises(asyncio.CancelledError):
        await pub.start()

    assert len(connects) == 1 and connects[0].kw["hostname"] == "broker"
    sent = connects[0].published
    clears = [i for i, (t, p, _) in enumerate(sent) if p == "" and t in _pet_clear_topics(7)]
    announces = [i for i, (t, p, _) in enumerate(sent) if t.endswith("/config") and p]
    assert {sent[i][0] for i in clears} == _pet_clear_topics(7)
    assert announces, "the live device and pet must still be announced"
    assert max(clears) < min(announces)
    assert any("petkit_pet_8_" in sent[i][0] for i in announces)
    assert pub._pending_clears == {}


async def test_without_a_broker_nothing_is_queued():
    reg, dev, pub = _setup()
    pub = HAPublisher(reg, {"ha_mqtt_host": ""})
    assert pub.enabled is False
    assert await pub.unpublish_pet(7) is None
    assert await pub.unpublish_discovery(dev) is None
    assert pub._pending_clears == {}


async def test_a_republish_during_a_flush_is_not_wiped():
    reg, dev, pub = _setup()
    pub._connected = False
    await pub.unpublish_pet(7)
    topics = list(pub._pending_clears[("pet", 7)])

    class Republishing(FakeMqttClient):
        async def publish(self, topic, payload, **kw):
            await super().publish(topic, payload, **kw)
            if len(self.published) == 1:
                # The pet comes back while the first clear is in flight.
                pub._cancel_pending_clear(("pet", 7), topics[2:])

    pub._client = Republishing()
    pub._connected = True
    assert await pub._flush_pending_clears() is True
    assert [t for t, _, _ in pub._client.published] == topics[:2]
    assert pub._pending_clears == {}


# --- removing a deleted device from Home Assistant ---------------------------

def _device_clear_topics(dev):
    from petkit_local.ha.categories import get_entities_for_device  # noqa: PLC0415
    entities = get_entities_for_device(dev)
    return ({discovery_topic(e, dev.petkit_id) for e in entities}
            | {f"petkit-local/{dev.petkit_id}/{e.unique_id_suffix}"
               for e in entities if e.component == "image"}
            | {f"petkit-local/{dev.petkit_id}/state",
               f"petkit-local/{dev.petkit_id}/availability"})


async def test_unpublish_discovery_clears_what_publish_discovery_announced():
    reg, dev, pub = _setup()
    await pub.publish_discovery(dev)
    announced = {t for t, p, _ in pub._client.published if t.endswith("/config") and p}
    assert 1 in pub._commands._entity_index
    pub._client.published.clear()

    assert await pub.unpublish_discovery(dev) is True

    sent = pub._client.published
    topics = {t for t, _, _ in sent}
    assert topics == _device_clear_topics(dev)
    assert {t for t in topics if t.endswith("/config")} == announced
    # The T5 camera's retained snapshot bytes go too, not only its config.
    assert "petkit-local/1/last_snapshot" in topics
    assert {"petkit-local/1/state", "petkit-local/1/availability"} <= topics
    assert all(p == "" and kw == {"retain": True} for _, p, kw in sent)
    assert 1 not in pub._commands._entity_index
    assert pub._pending_clears == {}


async def test_unpublish_discovery_while_disconnected_is_flushed_on_reconnect():
    reg, dev, pub = _setup()
    pub._connected = False

    assert await pub.unpublish_discovery(dev) is False
    assert pub._client.published == []
    assert ("device", 1) in pub._pending_clears

    pub._connected = True
    assert await pub._flush_pending_clears() is True
    sent = pub._client.published
    assert {t for t, _, _ in sent} == _device_clear_topics(dev)
    assert all(p == "" and kw == {"retain": True} for _, p, kw in sent)
    assert pub._pending_clears == {}


async def test_publish_discovery_cancels_a_queued_device_removal():
    reg, dev, pub = _setup()
    pub._connected = False
    await pub.unpublish_discovery(dev)
    assert ("device", 1) in pub._pending_clears

    await pub.publish_discovery(dev)  # the same device registers again
    assert pub._pending_clears == {}


# --- last visit / last drink media -------------------------------------------

def _image_msgs(pub, pid, key="last_visit_image"):
    return [(p, kw) for t, p, kw in pub._client.published
            if t == f"petkit-local/pet/{pid}/{key}"]


def _pet_state(pub, pid):
    return json.loads([p for t, p, _ in pub._client.published
                       if t == f"petkit-local/pet/{pid}/state"][-1])["state"]


async def test_pet_image_discovery_uses_the_pet_image_topic():
    reg, dev, pub = _setup()
    await pub.publish_pet_discovery({"id": 1, "name": "Mia"})
    configs = {json.loads(p)["unique_id"]: json.loads(p)
               for t, p, _ in pub._client.published if t.endswith("/config")}
    visit = configs["petkit_pet_1_last_visit_image"]
    assert visit["image_topic"] == "petkit-local/pet/1/last_visit_image"
    assert visit["content_type"] == "image/jpeg"
    assert "state_topic" not in visit
    assert configs["petkit_pet_1_last_drink_image"]["image_topic"] \
        == "petkit-local/pet/1/last_drink_image"
    assert configs["petkit_pet_1_last_visit_video"]["state_topic"] == "petkit-local/pet/1/state"
    assert "homeassistant/image/petkit_pet_1_last_visit_image/config" in \
        {t for t, _, _ in pub._client.published}


async def test_pet_state_pushes_the_poster_once_and_names_the_video(event_store, tmp_path):
    from tests._pet_media import JPEG, add_episode, media
    root = str(tmp_path / "media")
    reg, dev, pub = _setup(root)
    await add_episode(event_store, root, "r1", pet_id=7, items=(
        media("cloudDouble", "T5 (1)/Timelapse/v 1.mp4", stitch_state="stitched"),
        media("eventImage", "T5 (1)/Snapshots/p.jpg"),
    ))
    pet = {"id": 7, "name": "Mia"}

    await pub.publish_pet_state(pet, event_store)
    [(data, kw)] = _image_msgs(pub, 7)
    assert data == JPEG and kw["retain"] is True
    state = _pet_state(pub, 7)
    assert state["lastVisitVideo"] == "api/media/T5%20%281%29/Timelapse/v%201.mp4"
    assert state["lastDrinkVideo"] is None
    assert _image_msgs(pub, 7, "last_drink_image") == []

    # Twenty weight samples per visit republish the state; the JPEG is not resent.
    pub._client.published.clear()
    await pub.publish_pet_state(pet, event_store)
    assert _image_msgs(pub, 7) == []
    assert _pet_state(pub, 7)["lastVisitVideo"] == state["lastVisitVideo"]


async def test_a_reconnect_resends_the_pet_image(event_store, pet_registry, tmp_path):
    from tests._pet_media import add_episode, media
    root = str(tmp_path / "media")
    reg, dev, pub = _setup(root)
    pet = await pet_registry.create("Mia")
    pub.set_pet_source(pet_registry, event_store)
    await add_episode(event_store, root, "r1", pet_id=pet["id"], items=(
        media("dynamicVideo", "T5/Clips/c.mp4"), media("eventImage", "T5/Snapshots/p.jpg")))
    await pub.publish_all_pets()
    assert len(_image_msgs(pub, pet["id"])) == 1

    pub._client.published.clear()
    await pub.publish_all_pets()
    assert _image_msgs(pub, pet["id"]) == []

    pub._pet_images_sent.clear()  # what start() does on every (re)connect
    await pub.publish_all_pets()
    assert len(_image_msgs(pub, pet["id"])) == 1


async def test_a_new_visit_moves_the_image_to_it(event_store, tmp_path):
    from tests._pet_media import add_episode, media
    root = str(tmp_path / "media")
    reg, dev, pub = _setup(root)
    pet = {"id": 7, "name": "Mia"}
    await add_episode(event_store, root, "old", pet_id=7, ts=1000.0, items=(
        media("dynamicVideo", "T5/Clips/old.mp4"), media("eventImage", "T5/Snapshots/old.jpg")))
    await pub.publish_pet_state(pet, event_store)
    assert pub._pet_images_sent[(7, "toilet_visit")].endswith("old.jpg")

    await add_episode(event_store, root, "new", pet_id=7, ts=2000.0, items=(
        media("dynamicVideo", "T5/Clips/new.mp4"), media("eventImage", "T5/Snapshots/new.jpg")))
    pub._client.published.clear()
    await pub.publish_pet_state(pet, event_store)  # as bridge/stubs do for a new event
    assert len(_image_msgs(pub, 7)) == 1
    assert pub._pet_images_sent[(7, "toilet_visit")].endswith("new.jpg")
    assert _pet_state(pub, 7)["lastVisitVideo"] == "api/media/T5/Clips/new.mp4"


async def test_no_media_publishes_no_image(event_store):
    reg, dev, pub = _setup()
    await event_store.upsert_event({"device_id": 1, "event_type": "pet_out",
                                    "event_kind": "toilet_visit", "pet_id": 7, "ts": 1000.0,
                                    "related_event": "r1"})
    await pub.publish_pet_state({"id": 7, "name": "Mia"}, event_store)
    assert _image_msgs(pub, 7) == []
    assert _pet_state(pub, 7)["lastVisitVideo"] is None


async def test_a_resolver_failure_still_publishes_the_counters(event_store, monkeypatch):
    reg, dev, pub = _setup()

    async def broken(*a, **kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(publisher_mod, "resolve_pet_media", broken)
    await pub.publish_pet_state({"id": 7, "name": "Mia"}, event_store)
    state = _pet_state(pub, 7)
    assert state["lastVisitVideo"] is None and "visitsToday" in state


async def test_unpublish_pet_forgets_its_sent_images(event_store, tmp_path):
    from tests._pet_media import add_episode, media
    root = str(tmp_path / "media")
    reg, dev, pub = _setup(root)
    await add_episode(event_store, root, "r1", pet_id=7, items=(
        media("dynamicVideo", "T5/Clips/c.mp4"), media("eventImage", "T5/Snapshots/p.jpg")))
    await pub.publish_pet_state({"id": 7, "name": "Mia"}, event_store)
    pub._pet_images_sent[(8, "toilet_visit")] = "/other"
    pub._client.published.clear()

    await pub.unpublish_pet(7)
    cleared = {t for t, p, _ in pub._client.published if p == ""}
    assert {"petkit-local/pet/7/last_visit_image", "petkit-local/pet/7/last_drink_image"} <= cleared
    assert pub._pet_images_sent == {(8, "toilet_visit"): "/other"}


def _pub_with(root, data_dir):
    reg = DeviceRegistry()
    pub = HAPublisher(reg, {"media_root": root, "data_dir": data_dir})
    pub._client = FakeMqttClient()
    pub._connected = True
    return pub


async def test_a_visit_without_a_still_publishes_a_frame_of_its_video(event_store, tmp_path,
                                                                      monkeypatch):
    from tests._pet_media import add_episode, media
    root = str(tmp_path / "media")
    pub = _pub_with(root, str(tmp_path / "data"))
    await add_episode(event_store, root, "r1", pet_id=7, items=(
        media("cloudDouble", "T5/Timelapse/v.mp4", stitch_state="stitched"),))
    grabs = []

    async def fake_thumb(data_dir, video_path):
        grabs.append(video_path)
        out = os.path.join(data_dir, "thumbs", "grab.jpg")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as f:
            f.write(b"\xff\xd8frame")
        return out

    monkeypatch.setattr(publisher_mod, "cached_video_thumb", fake_thumb)
    monkeypatch.setattr(publisher_mod, "thumb_cache_path",
                        lambda d, v: os.path.join(d, "thumbs", "grab.jpg"))
    pet = {"id": 7, "name": "Mia"}
    await pub.publish_pet_state(pet, event_store)
    [(data, kw)] = _image_msgs(pub, 7)
    assert data == b"\xff\xd8frame" and kw["retain"] is True
    assert grabs == [os.path.join(root, "T5/Timelapse/v.mp4")]
    assert pub._pet_images_sent[(7, "toilet_visit")].endswith("grab.jpg")

    # The next weight sample neither re-grabs nor re-sends.
    pub._client.published.clear()
    await pub.publish_pet_state(pet, event_store)
    assert _image_msgs(pub, 7) == [] and len(grabs) == 1


async def test_a_failed_frame_grab_publishes_nothing_and_is_not_retried(event_store, tmp_path,
                                                                       monkeypatch, caplog):
    from tests._pet_media import add_episode, media
    root = str(tmp_path / "media")
    pub = _pub_with(root, str(tmp_path / "data"))
    await add_episode(event_store, root, "r1", pet_id=7, items=(
        media("dynamicVideo", "T5/Clips/c.mp4"),))
    calls = []

    async def no_ffmpeg(data_dir, video_path):
        calls.append(video_path)
        return None

    monkeypatch.setattr(publisher_mod, "cached_video_thumb", no_ffmpeg)
    caplog.set_level("INFO", logger="petkit_local.ha.publisher")
    pet = {"id": 7, "name": "Mia"}
    for _ in range(3):
        await pub.publish_pet_state(pet, event_store)
    assert _image_msgs(pub, 7) == []
    assert _pet_state(pub, 7)["lastVisitVideo"] == "api/media/T5/Clips/c.mp4"
    assert len(calls) == 1
    assert len([r for r in caplog.records if "No picture for pet 7" in r.message]) == 1


async def test_an_unreadable_poster_is_logged_once(event_store, tmp_path, monkeypatch, caplog):
    from tests._pet_media import add_episode, media
    root = str(tmp_path / "media")
    pub = _pub_with(root, str(tmp_path / "data"))
    await add_episode(event_store, root, "r1", pet_id=7, items=(
        media("dynamicVideo", "T5/Clips/c.mp4"), media("eventImage", "T5/Snapshots/p.jpg")))

    def unreadable(path):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(publisher_mod, "read_bytes", unreadable)
    pet = {"id": 7, "name": "Mia"}
    for _ in range(3):
        await pub.publish_pet_state(pet, event_store)
    assert _image_msgs(pub, 7) == []
    assert len([r for r in caplog.records if "Could not read pet 7" in r.message]) == 1


def test_a_video_path_over_the_state_cap_reads_as_unknown():
    from petkit_local.media.pet_media import PetMedia
    long = PetMedia(pet_id=1, kind="toilet_visit", event_id=1, related_event="r",
                    device_id=1, ts=0.0, video="T5/" + "x" * 300 + ".mp4", tier="clip",
                    poster=None)
    assert HAPublisher._video_state(long) is None
    short = PetMedia(pet_id=1, kind="toilet_visit", event_id=1, related_event="r",
                     device_id=1, ts=0.0, video="T5/c.mp4", tier="clip", poster=None)
    assert HAPublisher._video_state(short) == "api/media/T5/c.mp4"
