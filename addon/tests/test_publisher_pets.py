import asyncio
import json
from types import SimpleNamespace

import pytest

from petkit_local.devices.registry import DeviceRegistry
from petkit_local.ha import publisher as publisher_mod
from petkit_local.ha.publisher import HAPublisher
from petkit_local.utils.timeutil import local_day_bounds
from tests._fakes import FakeMqttClient


def _setup():
    reg = DeviceRegistry()
    dev = reg.get_or_create(petkit_id=1, device_type="t5", serial_number="SN")
    pub = HAPublisher(reg, {})
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
