"""Importing the account's pets, and their reference photos, out of proxy mode.

`dev_discern_pic` is the account's recognition set for one device: which
animals it matches against, and where their reference photos live. It already
passes through us untouched — the cloud's pets reaching the device is what
proxy mode MEANS — so the photos the device is being told to fetch are sitting
in a reply we were reading anyway and throwing away.

Two things this has to get right, and neither is the download. The identity in
that reply is PetKit's own pet id, which is exactly what a box still matching
against cloud-cached faces reports back — so binding it is what makes events
resolve, and binding it retroactively is what names the history. And nothing
may be fetched without being asked for: a device polls this endpoint by itself,
and reaching out to PetKit for somebody's photographs is not a side effect a
poll gets to have.
"""
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from petkit_local.ai.pets import MAX_CLOUD_FACES, PetRegistry, cloud_pets
from petkit_local.devices.registry import DeviceRegistry
from petkit_local.web.hub import EventHub
from petkit_local.web.panel import create_panel_app

JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"A" * 60


def _reply(*entries):
    return {"result": {"list": list(entries)}}


# --- reading the reply ------------------------------------------------------

def test_a_pet_and_its_photos_come_out_of_the_reply():
    payload = _reply({"id": 101392625, "discern": [
        {"id": 1, "url": "https://img.petkt.com/a.jpg"},
        {"id": 2, "url": "https://img.petkt.com/b.jpg"},
    ]})
    found = cloud_pets(payload, 10)
    assert len(found) == 1
    # Called pet_ref, not pet_id: it is a foreign identity until somebody binds
    # it, and a box matching against cached faces is already reporting it.
    assert found[0]["pet_ref"] == 101392625
    assert found[0]["link_with"] == 10
    assert [f["url"] for f in found[0]["faces"]] == [
        "https://img.petkt.com/a.jpg", "https://img.petkt.com/b.jpg"]
    # No name anywhere in it. The device-facing payload carries ids and URLs;
    # a pet's name lives in the account API, which no device ever asks for.
    assert "name" not in found[0]


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://host/a.jpg", "", "javascript:alert(1)", "/relative.jpg",
])
def test_only_an_http_url_is_ever_followed(url):
    """The bytes land on disk as a JPEG and are never echoed back, so the
    exposure is narrow — but a scheme like `file:` has no business being
    followed from a field the network supplied."""
    found = cloud_pets(_reply({"id": 1, "discern": [{"id": 1, "url": url}]}), 10)
    assert found == []


def test_a_pet_with_no_usable_photo_is_not_offered():
    """Importing it would create a pet the device can never match against."""
    assert cloud_pets(_reply({"id": 1, "discern": []}), 10) == []
    assert cloud_pets(_reply({"id": 1}), 10) == []


def test_the_number_of_photos_is_capped_before_anything_is_fetched():
    """A reply claiming a hundred faces must not cost a hundred fetches to
    discover that the store would refuse most of them."""
    faces = [{"id": i, "url": f"https://img.petkt.com/{i}.jpg"} for i in range(50)]
    found = cloud_pets(_reply({"id": 1, "discern": faces}), 10)
    assert len(found[0]["faces"]) == MAX_CLOUD_FACES


@pytest.mark.parametrize("payload", [
    None, [], "", {}, {"result": None}, {"result": {"list": "nope"}},
    {"result": {"list": ["not an object"]}},
    {"result": {"list": [{"id": 0, "discern": [{"url": "https://x/a.jpg"}]}]}},
])
def test_a_reply_that_is_not_the_shape_we_expected_yields_nothing(payload):
    assert cloud_pets(payload, 10) == []


# --- the one-shot panel action ----------------------------------------------

async def _serve(handler):
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, str(client.make_url("")).rstrip("/")


async def _panel(pet_registry, store, upstream=""):
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=10, device_type="t5", serial_number="SN10")
    cfg = {"api_url": "http://x/6/", "capture": False, "capture_dir": "/nope"}
    app = create_panel_app(reg, None, EventHub(), cfg, store,
                           live_config={"proxy_upstream": upstream})
    app["pet_registry"] = pet_registry
    app["event_store"] = store
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _cloud_with(faces, *, photo=JPEG_BYTES, status=200):
    """A stand-in PetKit answering `dev_discern_pic` and hosting the photos."""
    served = []

    async def handler(request):
        if request.path.endswith("dev_discern_pic"):
            base = str(request.url.origin())
            return web.json_response({"result": {"list": [
                {"id": 500, "discern": [
                    {"id": i, "url": f"{base}/face{i}.jpg"} for i in faces]},
            ]}})
        served.append(request.path)
        if status != 200:
            return web.Response(status=status)
        return web.Response(body=photo, content_type="image/jpeg")

    return handler, served


async def test_the_button_imports_the_pets_photos_and_binds_the_identity(
        pet_registry: PetRegistry, event_store):
    """The whole point in one test: photos on disk, and the box's own id bound
    so what it reports resolves to this pet."""
    handler, served = _cloud_with([1, 2])
    up, base = await _serve(handler)
    c = await _panel(pet_registry, event_store, base)
    try:
        body = await (await c.post("/api/pets/import",
                                   data=json.dumps({"device_id": 10}))).json()
        assert body["imported"] == 1
        row = body["results"][0]
        assert row["faces_imported"] == 2
        assert len(served) == 2
        assert len(await pet_registry.faces(row["pet_id"])) == 2
        assert await pet_registry.resolve_pet_ref(500) == row["pet_id"]
    finally:
        await c.close()
        await up.close()


async def test_it_names_the_history_already_recorded(
        pet_registry: PetRegistry, event_store):
    """The events already carry the identity in `pet_ref`, so the past is named
    the moment the import lands rather than only from the next visit on."""
    handler, _ = _cloud_with([1])
    up, base = await _serve(handler)
    for i in range(3):
        await event_store.upsert_event({
            "device_id": 10, "event_type": 5, "timestamp": 1000 + i, "pet_ref": 500,
        })
    c = await _panel(pet_registry, event_store, base)
    try:
        body = await (await c.post("/api/pets/import",
                                   data=json.dumps({"device_id": 10}))).json()
        assert body["results"][0]["bound_events"] == 3
    finally:
        await c.close()
        await up.close()


async def test_an_imported_pet_is_named_after_its_id_not_invented(
        pet_registry: PetRegistry, event_store):
    handler, _ = _cloud_with([1])
    up, base = await _serve(handler)
    c = await _panel(pet_registry, event_store, base)
    try:
        body = await (await c.post("/api/pets/import",
                                   data=json.dumps({"device_id": 10}))).json()
        assert body["results"][0]["name"] == "PetKit pet 500"
    finally:
        await c.close()
        await up.close()


async def test_pressing_it_twice_does_not_make_a_second_pet(
        pet_registry: PetRegistry, event_store):
    """The button is pressable repeatedly. A duplicate pet holding the same
    alias is the state `api_pet_detail` goes out of its way to prevent."""
    handler, _ = _cloud_with([1])
    up, base = await _serve(handler)
    c = await _panel(pet_registry, event_store, base)
    try:
        await c.post("/api/pets/import", data=json.dumps({"device_id": 10}))
        body = await (await c.post("/api/pets/import",
                                   data=json.dumps({"device_id": 10}))).json()
        assert body["imported"] == 0
        assert body["results"][0]["outcome"] == "already yours"
        assert len(await pet_registry.all()) == 1
    finally:
        await c.close()
        await up.close()


async def test_a_photo_that_cannot_be_fetched_is_counted_not_fatal(
        pet_registry: PetRegistry, event_store):
    """One dead URL must not cost the pet its binding."""
    handler, _ = _cloud_with([1], status=404)
    up, base = await _serve(handler)
    c = await _panel(pet_registry, event_store, base)
    try:
        body = await (await c.post("/api/pets/import",
                                   data=json.dumps({"device_id": 10}))).json()
        row = body["results"][0]
        assert row["faces_offered"] == 1 and row["faces_imported"] == 0
        assert await pet_registry.resolve_pet_ref(500) == row["pet_id"]
    finally:
        await c.close()
        await up.close()


async def test_something_that_is_not_a_jpeg_is_refused_after_download(
        pet_registry: PetRegistry, event_store):
    """The magic bytes are the only thing that makes these bytes a face photo,
    and the far end's content type is not evidence."""
    handler, _ = _cloud_with([1], photo=b"<html>not a photo</html>")
    up, base = await _serve(handler)
    c = await _panel(pet_registry, event_store, base)
    try:
        body = await (await c.post("/api/pets/import",
                                   data=json.dumps({"device_id": 10}))).json()
        assert body["results"][0]["faces_imported"] == 0
    finally:
        await c.close()
        await up.close()


async def test_an_oversized_photo_is_refused_without_being_kept(
        pet_registry: PetRegistry, event_store, monkeypatch):
    """A Content-Length is not a promise, so the cap is enforced while reading."""
    import petkit_local.web.api.pets as pets
    monkeypatch.setattr(pets, "MAX_FACE_DOWNLOAD_BYTES", 1024)
    handler, _ = _cloud_with([1], photo=JPEG_BYTES + b"B" * 5000)
    up, base = await _serve(handler)
    c = await _panel(pet_registry, event_store, base)
    try:
        body = await (await c.post("/api/pets/import",
                                   data=json.dumps({"device_id": 10}))).json()
        row = body["results"][0]
        assert row["faces_imported"] == 0
        assert await pet_registry.faces(row["pet_id"]) == []
    finally:
        await c.close()
        await up.close()


async def test_a_refusal_from_petkit_is_reported_not_swallowed(
        pet_registry: PetRegistry, event_store):
    async def handler(request):
        return web.json_response({"error": {"code": 704, "msg": "bad sign"}})

    up, base = await _serve(handler)
    c = await _panel(pet_registry, event_store, base)
    try:
        r = await c.post("/api/pets/import", data=json.dumps({"device_id": 10}))
        assert r.status == 502
        # 704 gets its own words: it means the credential is ours, not PetKit's.
        assert "704" in (await r.json())["error"]
        assert await pet_registry.all() == []
    finally:
        await c.close()
        await up.close()


async def test_an_unreachable_cloud_creates_nothing(
        pet_registry: PetRegistry, event_store):
    c = await _panel(pet_registry, event_store, "http://127.0.0.1:1")
    try:
        r = await c.post("/api/pets/import", data=json.dumps({"device_id": 10}))
        assert r.status == 502
        assert await pet_registry.all() == []
    finally:
        await c.close()


async def test_importing_for_a_device_we_do_not_have_is_a_404(
        pet_registry: PetRegistry, event_store):
    c = await _panel(pet_registry, event_store)
    try:
        r = await c.post("/api/pets/import", data=json.dumps({"device_id": 999}))
        assert r.status == 404
        assert await pet_registry.all() == []
    finally:
        await c.close()


@pytest.mark.parametrize("body", ["not json", '["a list"]'])
async def test_a_malformed_request_is_refused(
        pet_registry: PetRegistry, event_store, body):
    c = await _panel(pet_registry, event_store)
    try:
        assert (await c.post("/api/pets/import", data=body)).status == 400
    finally:
        await c.close()


async def test_an_imported_pet_can_be_renamed(pet_registry: PetRegistry, event_store):
    """The import can only ever produce `PetKit pet <id>` — the name is in no
    payload a device receives — so renaming is not a nicety, it is the second
    half of the feature."""
    handler, _ = _cloud_with([1])
    up, base = await _serve(handler)
    c = await _panel(pet_registry, event_store, base)
    try:
        body = await (await c.post("/api/pets/import",
                                   data=json.dumps({"device_id": 10}))).json()
        pet_id = body["results"][0]["pet_id"]

        renamed = await (await c.post(f"/api/pets/{pet_id}",
                                      data=json.dumps({"name": "Mruczek"}))).json()
        assert renamed["pet"]["name"] == "Mruczek"
        # The binding to PetKit's id survives the rename — losing it would
        # un-attribute every event the box reports under its cached identity.
        assert await pet_registry.resolve_pet_ref(500) == pet_id
    finally:
        await c.close()
        await up.close()


async def test_import_is_not_blocked_by_the_duplicate_guard(
        pet_registry: PetRegistry, event_store):
    """The panel's create refuses a taken name (409); import creates through the
    registry and must not. A pet already called `PetKit pet 500` that does NOT
    hold the alias is a different pet as far as import can tell."""
    await pet_registry.create("PetKit pet 500")
    handler, _ = _cloud_with([1])
    up, base = await _serve(handler)
    c = await _panel(pet_registry, event_store, base)
    try:
        body = await (await c.post("/api/pets/import",
                                   data=json.dumps({"device_id": 10}))).json()
        assert body["results"][0]["outcome"] == "imported"
        assert len(await pet_registry.all()) == 2
    finally:
        await c.close()
        await up.close()
