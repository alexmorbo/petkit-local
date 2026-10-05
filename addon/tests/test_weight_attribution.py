"""Attributing litter-box visits to a pet by weight.

A T6 reports a weight for every visit and no identity at all, so without this
every visit is anonymous. The rule is the operator's: the pet whose reference
weight is NEAREST wins, an exact tie stays unattributed, and a visit more than
`WEIGHT_OUTLIER_GUARD_G` from every reference (litter bag on the scale, a
cleaning cycle) is left alone.

What makes it safe is that an identity always wins. The face matcher, a bound
`pet_ref` and any `pet_id` the scale did not write are never touched — not at
ingest, not in a backfill, not after a reference changes or a pet is deleted.
"""
import json
import urllib.parse
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from petkit_local.ai.pets import (WEIGHT_CONFLICT_G, WEIGHT_OUTLIER_GUARD_G, PetRegistry,
                                  nearest_pet_by_weight, plan_weight_reattribution,
                                  weight_conflicts)
from petkit_local.devices.registry import DeviceRegistry
from petkit_local.events.migrations import backfill_event_rows
from petkit_local.events.store import EventStore
from petkit_local.http.server import create_app
from petkit_local.mqtt.bridge import MQTTBridge
from petkit_local.web.hub import EventHub
from petkit_local.web.panel import create_panel_app

PETS_JS = Path(__file__).resolve().parent.parent / "petkit_local/web/static/js/pets.js"


class FakePublisher:
    def __init__(self):
        self.pet_discoveries = []
        self.pet_states = []
        self.pet_unpublished = []

    async def unpublish_pet(self, pet_id):
        self.pet_unpublished.append(pet_id)
        return True

    async def publish_event(self, device, suffix, event_type, attrs=None):
        pass

    async def publish_state(self, device):
        pass

    async def publish_availability(self, device):
        pass

    async def publish_pet_discovery(self, pet):
        self.pet_discoveries.append(pet["id"])

    async def publish_pet_state(self, pet, store):
        self.pet_states.append(pet["id"])


# --- the rule ---------------------------------------------------------------

def test_the_nearest_reference_wins():
    refs = {2: 5075.0, 3: 4550.0}
    assert nearest_pet_by_weight(5100, refs) == 2
    assert nearest_pet_by_weight(4600, refs) == 3
    # No grey zone: 4813 is 262 from 5075 and 263 from 4550, and still goes.
    assert nearest_pet_by_weight(4813, refs) == 2


def test_an_exact_tie_stays_unattributed():
    assert nearest_pet_by_weight(4800, {2: 5000.0, 3: 4600.0}) is None


def test_the_outlier_guard_leaves_a_far_weight_alone():
    refs = {2: 5075.0, 3: 4550.0}
    assert WEIGHT_OUTLIER_GUARD_G == 1000.0
    assert nearest_pet_by_weight(1300, refs) is None
    assert nearest_pet_by_weight(3000, refs) is None
    assert nearest_pet_by_weight(3551, refs) == 3  # 999 g: inside the guard
    assert nearest_pet_by_weight(3550, refs) == 3  # exactly 1000 g: still inside
    assert nearest_pet_by_weight(3549, refs) is None


def test_no_reference_means_no_attribution():
    assert nearest_pet_by_weight(5000, {}) is None


def test_a_single_pet_with_a_reference_takes_what_is_within_the_guard():
    refs = {2: 5075.0}
    assert nearest_pet_by_weight(4700, refs) == 2
    assert nearest_pet_by_weight(3900, refs) is None


def test_a_margin_makes_a_grey_zone_when_asked_for():
    refs = {2: 5075.0, 3: 4550.0}
    assert nearest_pet_by_weight(4850, refs, margin_g=100) is None  # 225 vs 300
    assert nearest_pet_by_weight(4700, refs, margin_g=100) == 3     # 150 vs 375


def test_a_missing_or_non_positive_weight_is_never_attributed():
    refs = {2: 5075.0}
    for weight in (None, 0, -5):
        assert nearest_pet_by_weight(weight, refs) is None


def test_weight_conflicts_finds_equal_references():
    assert weight_conflicts({1: 4570.0, 2: 4570.0}) == [(1, 2, 0.0)]


def test_weight_conflicts_is_inclusive_at_100_g_and_quiet_beyond():
    assert WEIGHT_CONFLICT_G == 100.0
    assert weight_conflicts({1: 4500.0, 2: 4600.0}) == [(1, 2, 100.0)]
    assert weight_conflicts({1: 4500.0, 2: 4601.0}) == []


def test_weight_conflicts_reports_every_pair_sorted():
    assert weight_conflicts({3: 4560.0, 1: 4500.0, 2: 4530.0}) == [
        (1, 2, 30.0), (1, 3, 60.0), (2, 3, 30.0)]
    assert weight_conflicts({}) == []
    assert weight_conflicts({1: 4500.0}) == []


def test_planning_skips_pet_in_and_is_idempotent():
    refs = {2: 5070.0, 3: 4570.0}
    rows = [
        {"id": 1, "event_type": "pet_out", "device_type": "t6",
         "content_json": '{"pet_weight": 5089}', "pet_id": None, "pet_source": None},
        {"id": 2, "event_type": "pet_in", "device_type": "t6",
         "content_json": '{"pet_weight": 4600}', "pet_id": None, "pet_source": None},
        {"id": 3, "event_type": "pet_out", "device_type": "t6",
         "content_json": '{"pet_weight": 4588}', "pet_id": 3, "pet_source": "weight"},
        {"id": 4, "event_type": "pet_out", "device_type": "t6",
         "content_json": "not json", "pet_id": None, "pet_source": None},
    ]
    assert plan_weight_reattribution(rows, refs) == [(1, 2)]


def test_planning_clears_a_stale_weight_attribution():
    refs = {2: 5070.0, 3: 4570.0}
    rows = [
        # Not a visit summary, but carries a weight attribution: cleared.
        {"id": 5, "event_type": "pet_in", "device_type": "t6",
         "content_json": '{"pet_weight": 4600}', "pet_id": 3, "pet_source": "weight"},
        # A visit summary with no weight that still carries one: cleared.
        {"id": 6, "event_type": "pet_out", "device_type": "t6",
         "content_json": "{}", "pet_id": 2, "pet_source": "weight"},
        # Not a visit summary and no weight attribution: left alone.
        {"id": 7, "event_type": "pet_in", "device_type": "t6",
         "content_json": '{"pet_weight": 4600}', "pet_id": None, "pet_source": None},
    ]
    assert plan_weight_reattribution(rows, refs) == [(5, None), (6, None)]


# --- at ingest -----------------------------------------------------------------

def _bridge(tmp_path, device_type="t6"):
    reg = DeviceRegistry()
    dev = reg.get_or_create(petkit_id=30007986, device_type=device_type, serial_number="SN")
    pub = FakePublisher()
    store = EventStore(tmp_path / "petkit.db")
    pets = PetRegistry(store, str(tmp_path / "faces"))
    bridge = MQTTBridge(reg, pub, event_store=store, hub=EventHub(), pet_registry=pets)
    return dev, pub, store, pets, bridge


async def _two_cats(pets):
    mia = await pets.create("Mia", device_ids=[30000938], weight=5070)
    gami = await pets.create("Gami", device_ids=[30000938], weight=4570)
    return mia, gami


async def test_a_pet_out_without_identity_is_attributed_by_weight(tmp_path):
    """The pets are linked to the fountain only; a weight is the same on any
    scale, so the T6 visit is attributed all the same."""
    dev, pub, store, pets, bridge = _bridge(tmp_path)
    try:
        mia, gami = await _two_cats(pets)
        await bridge._handle_event(dev, "pet_out", {"params": {
            "event_id": "e1", "content": json.dumps({"pet_weight": 4623})}})
        [row] = await store.all_events()
        assert row["pet_id"] == gami["id"]
        assert row["pet_source"] == "weight"
        assert pub.pet_discoveries == [gami["id"]]
        assert pub.pet_states == [gami["id"]]
    finally:
        await store.close()


async def test_pet_in_is_never_attributed_by_weight(tmp_path):
    dev, pub, store, pets, bridge = _bridge(tmp_path)
    try:
        await _two_cats(pets)
        await bridge._handle_event(dev, "pet_in", {"params": {
            "event_id": "e1", "content": json.dumps({"pet_weight": 4600})}})
        [row] = await store.all_events()
        assert row["pet_id"] is None
        assert row["pet_source"] is None
        assert pub.pet_states == []
    finally:
        await store.close()


async def test_an_identity_beats_the_scale(tmp_path):
    """Gami's face on a visit that weighs exactly Mia's reference: Gami."""
    dev, pub, store, pets, bridge = _bridge(tmp_path, device_type="t5")
    try:
        mia, gami = await _two_cats(pets)
        await bridge._handle_event(dev, "pet_out", {"params": {
            "event_id": "e1",
            "content": json.dumps({"pet_weight": 5070,
                                   "score_info": [{"id": gami["id"], "score": 900}]})}})
        await bridge._handle_event(dev, "pet_out", {"params": {
            "event_id": "e2", "content": json.dumps({"pet_weight": 5070, "petId": gami["id"]})}})
        rows = await store.all_events()
        assert [(r["pet_id"], r["pet_source"]) for r in rows] == [(gami["id"], None)] * 2
    finally:
        await store.close()


async def test_an_unresolved_identity_is_not_overridden_by_weight(tmp_path):
    """A box still matching cloud-cached faces names someone we cannot resolve.
    That is still a claim, and binding it later must be what names the row."""
    dev, pub, store, pets, bridge = _bridge(tmp_path, device_type="t5")
    try:
        await _two_cats(pets)
        await bridge._handle_event(dev, "pet_out", {"params": {
            "event_id": "e1",
            "content": json.dumps({"pet_weight": 5070,
                                   "score_info": [{"id": 101392625, "score": 900}]})}})
        [row] = await store.all_events()
        assert row["pet_ref"] == 101392625
        assert row["pet_id"] is None
        assert row["pet_source"] is None
    finally:
        await store.close()


async def test_an_http_visit_summary_is_attributed_by_weight(tmp_path):
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=100, device_type="t5", serial_number="SN100")
    store = EventStore(tmp_path / "petkit.db")
    pets = PetRegistry(store, str(tmp_path / "faces"))
    mia, gami = await _two_cats(pets)
    pub = FakePublisher()
    app = create_app(reg, {"api_url": "http://server/6/", "mqtt_port": 1883,
                           "proxy_mode": False, "proxy_upstream": "",
                           "proxy_block_run_cmd": True, "capture": False})
    app["event_store"] = store
    app["pet_registry"] = pets
    app["ha_publisher"] = pub
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        content = urllib.parse.quote(json.dumps({"pet_weight": 5157}))
        r = await client.post("/6/t5/dev_event_report", headers={"X-Device": "id=100&sn=SN100"},
                              data=f"eventType=10&eventId=e1&content={content}")
        assert r.status == 200
        [row] = await store.all_events()
        assert (row["pet_id"], row["pet_source"]) == (mia["id"], "weight")
        assert pub.pet_states == [mia["id"]]
    finally:
        await client.close()
        await store.close()


# --- over history ------------------------------------------------------------

async def _visit(store, weight, *, event_type="pet_out", pet_id=None, pet_ref=None,
                 pet_source=None, content=None):
    body = {"pet_weight": weight} if content is None else content
    return await store.upsert_event({
        "device_id": 30007986, "device_type": "t6", "event_type": event_type,
        "event_kind": "toilet_visit", "ts": 1000.0, "pet_id": pet_id, "pet_ref": pet_ref,
        "pet_source": pet_source, "content_json": json.dumps(body)})


async def _attribution(store):
    return {r["id"]: (r["pet_id"], r["pet_source"]) for r in await store.all_events()}


async def test_reattribution_fills_history_and_is_idempotent(event_store, pet_registry):
    mia, gami = await _two_cats(pet_registry)
    heavy = await _visit(event_store, 5089)
    light = await _visit(event_store, 4588)
    junk = await _visit(event_store, 1935)
    partial = await _visit(event_store, 4600, event_type="pet_in")

    changed, affected = await pet_registry.reattribute_by_weight()
    assert changed == 2
    assert affected == {mia["id"], gami["id"]}
    got = await _attribution(event_store)
    assert got[heavy] == (mia["id"], "weight")
    assert got[light] == (gami["id"], "weight")
    assert got[junk] == (None, None)
    assert got[partial] == (None, None)

    assert await pet_registry.reattribute_by_weight() == (0, set())


async def test_a_reference_change_moves_weight_rows(event_store, pet_registry):
    mia, gami = await _two_cats(pet_registry)
    heavy = await _visit(event_store, 5089)
    light = await _visit(event_store, 4588)
    await pet_registry.reattribute_by_weight()

    # Swap the references: every weight-attributed row has to follow.
    await pet_registry.update(mia["id"], weight=4570)
    await pet_registry.update(gami["id"], weight=5070)
    changed, affected = await pet_registry.reattribute_by_weight()
    assert changed == 2
    assert affected == {mia["id"], gami["id"]}
    got = await _attribution(event_store)
    assert got[heavy] == (gami["id"], "weight")
    assert got[light] == (mia["id"], "weight")


async def test_identity_rows_are_never_touched(event_store, pet_registry):
    mia, gami = await _two_cats(pet_registry)
    # A pet_id the scale did not write, weighing Gami: stays Mia.
    explicit = await _visit(event_store, 4570, pet_id=mia["id"])
    # A resolved identity, weighing Gami: stays Mia.
    bound = await _visit(event_store, 4570, pet_id=mia["id"], pet_ref=mia["id"])
    # An unresolved identity: stays anonymous.
    claimed = await _visit(event_store, 4570, pet_ref=101392625)
    before = await _attribution(event_store)

    assert await pet_registry.reattribute_by_weight() == (0, set())
    await pet_registry.update(mia["id"], weight=9000)
    await pet_registry.reattribute_by_weight()
    await pet_registry.delete(gami["id"])
    await pet_registry.reattribute_by_weight()

    after = await _attribution(event_store)
    for row_id in (explicit, bound, claimed):
        assert after[row_id] == before[row_id]


async def test_removing_a_reference_moves_or_clears_its_rows(event_store, pet_registry):
    mia, gami = await _two_cats(pet_registry)
    near_gami = await _visit(event_store, 4588)    # 482 from Mia: moves to her
    far_from_mia = await _visit(event_store, 3700)  # 1370 from Mia: cleared
    await pet_registry.reattribute_by_weight()
    assert (await _attribution(event_store))[far_from_mia] == (gami["id"], "weight")

    await pet_registry.update(gami["id"], weight=None)
    changed, affected = await pet_registry.reattribute_by_weight()
    assert changed == 2
    assert affected == {mia["id"], gami["id"]}
    got = await _attribution(event_store)
    assert got[near_gami] == (mia["id"], "weight")
    assert got[far_from_mia] == (None, None)


async def test_deleting_a_pet_reassigns_only_its_weight_rows(event_store, pet_registry):
    mia, gami = await _two_cats(pet_registry)
    weighed = await _visit(event_store, 4588)
    seen = await _visit(event_store, 4588, pet_id=gami["id"], pet_ref=gami["id"])
    await pet_registry.reattribute_by_weight()

    await pet_registry.delete(gami["id"])
    changed, affected = await pet_registry.reattribute_by_weight()
    assert changed == 1
    assert affected == {mia["id"], gami["id"]}
    got = await _attribution(event_store)
    assert got[weighed] == (mia["id"], "weight")
    assert got[seen] == (gami["id"], None)


async def test_apply_rechecks_ownership_against_a_racing_identity(event_store, pet_registry):
    mia, gami = await _two_cats(pet_registry)
    row_id = await _visit(event_store, 5089)
    plan = plan_weight_reattribution(await event_store.weight_attribution_candidates(),
                                     await pet_registry.weight_references())
    assert plan == [(row_id, mia["id"])]

    # The face matcher's answer lands between planning and applying.
    await event_store.update_event_fields(row_id, pet_ref=gami["id"], pet_id=gami["id"])
    assert await event_store.apply_weight_attribution(plan) == 0
    assert (await _attribution(event_store))[row_id] == (gami["id"], None)


async def test_backfill_hands_a_weight_row_back_to_a_recovered_identity(event_store,
                                                                        pet_registry):
    mia, gami = await _two_cats(pet_registry)
    content = {"pet_weight": 5089, "score_info": [{"id": gami["id"], "score": 900}]}
    row_id = await _visit(event_store, 5089, pet_id=mia["id"], pet_source="weight",
                          content=content)

    await backfill_event_rows(event_store)
    row = await event_store.get_event(row_id)
    assert row["pet_ref"] == gami["id"]
    assert (row["pet_id"], row["pet_source"]) == (None, None)
    # ...and the scale never claims it again.
    assert await pet_registry.reattribute_by_weight() == (0, set())


# --- the panel ---------------------------------------------------------------

async def _panel(store, pets, publisher=None):
    reg = DeviceRegistry()
    cfg = {"api_url": "http://x/6/", "capture": False, "capture_dir": "/nope"}
    app = create_panel_app(reg, None, EventHub(), cfg, event_store=store,
                           pet_registry=pets, ha_publisher=publisher)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def _post(client, path, body):
    r = await client.post(path, data=json.dumps(body),
                          headers={"Content-Type": "application/json"})
    assert r.status == 200
    return await r.json()


async def test_setting_a_weight_from_the_panel_reattributes(event_store, pet_registry):
    mia = await pet_registry.create("Mia")
    row_id = await _visit(event_store, 5089)
    pub = FakePublisher()
    c = await _panel(event_store, pet_registry, pub)
    try:
        body = await _post(c, f"/api/pets/{mia['id']}", {"weight": "5075"})
        assert body["pet"]["weight"] == 5075.0
        assert body["reattributed"] == 1
        assert (await _attribution(event_store))[row_id] == (mia["id"], "weight")
        assert mia["id"] in pub.pet_states

        # Garbage leaves the reference alone.
        body = await _post(c, f"/api/pets/{mia['id']}", {"weight": "heavy"})
        assert body["pet"]["weight"] == 5075.0
        assert body["reattributed"] == 0

        # An empty field clears it, and the visit with it.
        body = await _post(c, f"/api/pets/{mia['id']}", {"weight": ""})
        assert body["pet"]["weight"] is None
        assert body["reattributed"] == 1
        assert (await _attribution(event_store))[row_id] == (None, None)

        body = await _post(c, f"/api/pets/{mia['id']}", {"weight": 5075})
        body = await _post(c, f"/api/pets/{mia['id']}", {"weight": None})
        assert body["pet"]["weight"] is None
    finally:
        await c.close()


async def test_creating_and_deleting_a_pet_with_a_weight_reattributes(event_store,
                                                                      pet_registry):
    row_id = await _visit(event_store, 4588)
    c = await _panel(event_store, pet_registry)
    try:
        body = await _post(c, "/api/pets", {"name": "Gami", "weight": 4570})
        assert body["pet"]["weight"] == 4570.0
        assert body["reattributed"] == 1
        gid = body["pet"]["id"]
        assert (await _attribution(event_store))[row_id] == (gid, "weight")

        r = await c.delete(f"/api/pets/{gid}")
        body = await r.json()
        assert body == {"ok": True, "reattributed": 1}
        assert (await _attribution(event_store))[row_id] == (None, None)
    finally:
        await c.close()


def test_the_panel_can_edit_a_reference_weight():
    js = PETS_JS.read_text()
    assert 'data-action="edit-pet-weight"' in js
    assert "onAction('edit-pet-weight'" in js
    assert "JSON.stringify({ weight })" in js
    assert "newPetWeight" in js


def test_the_panel_shows_weight_conflicts():
    js = PETS_JS.read_text()
    assert "weight_conflicts" in js
    assert "function weightConflictCard" in js


async def test_the_pets_list_names_weight_conflicts(event_store, pet_registry):
    mia = await pet_registry.create("Mia", weight=4570)
    gami = await pet_registry.create("Gami", weight=4600)
    await pet_registry.create("Bars", weight=6000)
    c = await _panel(event_store, pet_registry)
    try:
        body = await (await c.get("/api/pets")).json()
        assert len(body["pets"]) == 3
        assert body["weight_conflicts"] == [{
            "pet_ids": [mia["id"], gami["id"]], "names": ["Mia", "Gami"],
            "weights": [4570.0, 4600.0], "diff_g": 30,
        }]
    finally:
        await c.close()


async def test_a_weight_update_warns_but_saves(event_store, pet_registry):
    mia = await pet_registry.create("Mia", weight=4570)
    gami = await pet_registry.create("Gami", weight=5200)
    c = await _panel(event_store, pet_registry)
    try:
        body = await _post(c, f"/api/pets/{gami['id']}", {"weight": 4570})
        assert body["pet"]["weight"] == 4570.0
        assert body["weight_conflicts"] == [{
            "pet_ids": [mia["id"], gami["id"]], "names": ["Mia", "Gami"],
            "weights": [4570.0, 4570.0], "diff_g": 0,
        }]
        body = await _post(c, f"/api/pets/{gami['id']}", {"weight": 5300})
        assert body["pet"]["weight"] == 5300.0
        assert body["weight_conflicts"] == []
        # A POST that does not touch the weight carries no verdict on it.
        body = await _post(c, f"/api/pets/{gami['id']}", {"name": "Gami II"})
        assert "weight_conflicts" not in body
    finally:
        await c.close()


async def test_creating_a_pet_near_another_warns_but_creates(event_store, pet_registry):
    mia = await pet_registry.create("Mia", weight=4570)
    await pet_registry.create("Bars", weight=6000)
    c = await _panel(event_store, pet_registry)
    try:
        body = await _post(c, "/api/pets", {"name": "Gami", "weight": 4650})
        gid = body["pet"]["id"]
        assert [x["pet_ids"] for x in body["weight_conflicts"]] == [[mia["id"], gid]]
        assert body["weight_conflicts"][0]["diff_g"] == 80
    finally:
        await c.close()


async def test_weight_conflicts_ignore_devices(event_store, pet_registry):
    # Attribution matches a visit against EVERY reference, whichever box the
    # pet's mugshots are served to -- so two pets on different devices still
    # steal each other's visits.
    await pet_registry.create("Mia", device_ids=[1], weight=4570)
    await pet_registry.create("Mia", device_ids=[2], weight=4570)
    c = await _panel(event_store, pet_registry)
    try:
        body = await (await c.get("/api/pets")).json()
        assert len(body["weight_conflicts"]) == 1
        assert body["weight_conflicts"][0]["diff_g"] == 0
    finally:
        await c.close()


async def test_deleting_a_pet_removes_it_from_home_assistant(event_store, pet_registry):
    mia = await pet_registry.create("Mia", weight=4570)
    pub = FakePublisher()
    c = await _panel(event_store, pet_registry, pub)
    try:
        body = await (await c.delete(f"/api/pets/{mia['id']}")).json()
        assert body == {"ok": True, "reattributed": 0}
        assert pub.pet_unpublished == [mia["id"]]
        # A pet that is already gone is not unpublished twice.
        await c.delete(f"/api/pets/{mia['id']}")
        assert pub.pet_unpublished == [mia["id"]]
    finally:
        await c.close()


async def test_a_failing_ha_cleanup_never_fails_the_delete(event_store, pet_registry):
    class Exploding(FakePublisher):
        async def unpublish_pet(self, pet_id):
            raise RuntimeError("broker on fire")

    mia = await pet_registry.create("Mia")
    c = await _panel(event_store, pet_registry, Exploding())
    try:
        r = await c.delete(f"/api/pets/{mia['id']}")
        assert r.status == 200
        assert (await r.json())["ok"] is True
        assert await pet_registry.get(mia["id"]) is None
    finally:
        await c.close()
