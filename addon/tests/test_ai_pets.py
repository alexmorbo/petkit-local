import json
from pathlib import Path

from petkit_local.ai.pets import PetRegistry, _safe_photo_filename
from petkit_local.events.store import MAX_FACES_PER_PET, EventStore

JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"A" * 60


async def test_create_get_update_delete_roundtrip(pet_registry: PetRegistry):
    pet = await pet_registry.create("Mruczek", device_ids=[1, 2], weight=4.2)
    assert pet["name"] == "Mruczek"
    assert (await pet_registry.get(pet["id"]))["name"] == "Mruczek"

    updated = await pet_registry.update(pet["id"], name="Mruczek II")
    assert updated["name"] == "Mruczek II"

    assert await pet_registry.update(99999, name="ghost") is None

    assert await pet_registry.delete(pet["id"]) is True
    assert await pet_registry.get(pet["id"]) is None
    assert await pet_registry.delete(pet["id"]) is False


async def test_for_device_lookup(pet_registry: PetRegistry):
    await pet_registry.create("A", device_ids=[1])
    await pet_registry.create("B", device_ids=[2])
    assert [p["name"] for p in await pet_registry.for_device(1)] == ["A"]
    assert [p["name"] for p in await pet_registry.for_device(2)] == ["B"]


def test_safe_photo_filename_strips_unsafe_chars():
    # '/' never survives (so it's always a single filename component, never a
    # traversal). Since the move to utils.paths.sanitize_filename the leftover
    # '..' runs are collapsed too — the previous sanitizer let them through as
    # "pet_1_face_..-..-etc-passwd.jpg", which was harmless on disk but tripped
    # the defensive '..' rejection in http/handlers/discern.py::handle_faces,
    # so such a photo could be written and then never served.
    name = _safe_photo_filename(1, "../../etc/passwd")
    assert "/" not in name
    assert ".." not in name
    assert name == "pet_1_face_etc-passwd.jpg"
    assert "/" not in _safe_photo_filename(5, "weird name!!")


def test_safe_photo_filename_is_url_and_shell_safe():
    # The name is substituted into the /faces/{name} URL unescaped, so the
    # character set stays a whitelist even though sanitize_filename's own rule
    # is a blacklist.
    for hostile in ("weird name!!", 'x"y', "a&b", "c#d", "e%f", "g\nh", "i\x00j"):
        name = _safe_photo_filename(1, hostile)
        assert all(c.isalnum() or c in "_.-" for c in name), (hostile, name)


def test_safe_photo_filename_never_empty_or_hidden():
    for degenerate in ("", "..", ".", "...", "///", "\n"):
        name = _safe_photo_filename(1, degenerate)
        assert name == "pet_1_face_0.jpg", degenerate


def test_safe_photo_filename_caps_length_and_sanitizes_extension():
    assert len(_safe_photo_filename(1, "a" * 500)) < 100
    assert _safe_photo_filename(1, "f1", ext=".PNG") == "pet_1_face_f1.PNG"
    assert _safe_photo_filename(1, "f1", ext="..") == "pet_1_face_f1.jpg"
    # separators are dropped from the extension, and it is length-capped too
    assert _safe_photo_filename(1, "f1", ext="/etc/passwd") == "pet_1_face_f1.etcpassw"


async def test_add_face_stays_inside_the_faces_dir(pet_registry: PetRegistry, tmp_path: Path):
    pet = await pet_registry.create("Mruczek")
    faces = tmp_path / "faces"
    face = await pet_registry.add_face(pet["id"], JPEG_BYTES)
    assert face is not None
    assert Path(face["photo_path"]).parent == faces.resolve()


async def test_add_face_rejects_non_jpeg(pet_registry: PetRegistry):
    pet = await pet_registry.create("Mruczek")
    assert await pet_registry.add_face(pet["id"], b"not a jpeg at all") is None
    # and leaves no half-registered row behind
    assert await pet_registry.faces(pet["id"]) == []


async def test_add_face_rejects_unknown_pet(pet_registry: PetRegistry):
    assert await pet_registry.add_face(99999, JPEG_BYTES) is None


async def test_add_face_stores_the_file_and_a_row(pet_registry: PetRegistry):
    pet = await pet_registry.create("Mruczek")
    face = await pet_registry.add_face(pet["id"], JPEG_BYTES)
    assert face is not None
    assert Path(face["photo_path"]).read_bytes() == JPEG_BYTES
    # the row id names the file, because it is also what the device is handed
    assert Path(face["photo_path"]).name == f"pet_{pet['id']}_face_{face['id']}.jpg"
    assert [f["id"] for f in await pet_registry.faces(pet["id"])] == [face["id"]]


async def test_a_pet_holds_at_most_six_faces(pet_registry: PetRegistry):
    """The firmware's own "Too many face picture" limit is unknown; six is what
    the official app's upload grid offers, so six is PetKit's own maximum."""
    pet = await pet_registry.create("Mruczek")
    stored = [await pet_registry.add_face(pet["id"], JPEG_BYTES) for _ in range(7)]
    assert stored[6] is None
    assert len(await pet_registry.faces(pet["id"])) == MAX_FACES_PER_PET


async def test_delete_face_removes_the_row_and_the_file(pet_registry: PetRegistry):
    pet = await pet_registry.create("Mruczek")
    face = await pet_registry.add_face(pet["id"], JPEG_BYTES)
    assert await pet_registry.delete_face(face["id"]) is True
    assert not Path(face["photo_path"]).exists()
    assert await pet_registry.faces(pet["id"]) == []
    assert await pet_registry.delete_face(face["id"]) is False


async def test_deleting_a_pet_removes_every_face_file(pet_registry: PetRegistry):
    pet = await pet_registry.create("Mruczek")
    faces = [await pet_registry.add_face(pet["id"], JPEG_BYTES) for _ in range(2)]
    assert await pet_registry.delete(pet["id"]) is True
    assert not any(Path(f["photo_path"]).exists() for f in faces)


async def test_face_ids_are_never_reused(pet_registry: PetRegistry):
    """A recycled id would look to the device like a photo it already has
    cached, so it would never re-fetch the replacement."""
    pet = await pet_registry.create("Mruczek")
    first = await pet_registry.add_face(pet["id"], JPEG_BYTES)
    await pet_registry.delete_face(first["id"])
    second = await pet_registry.add_face(pet["id"], JPEG_BYTES)
    assert second["id"] != first["id"]


async def test_discern_pic_payload_matches_the_real_cloud_shape(pet_registry: PetRegistry):
    """Keys and types are the captured cloud's: integer `id` at both levels,
    no `petId`/`faceId`, and no `area` inside a list entry."""
    with_photo = await pet_registry.create("A", device_ids=[1])
    face = await pet_registry.add_face(with_photo["id"], JPEG_BYTES)
    await pet_registry.create("B", device_ids=[1])  # no photo

    entries = (await pet_registry.discern_pic_payload(1, "192.0.2.50:8080"))["result"]["list"]
    assert entries == [{
        "id": with_photo["id"],
        "discern": [{
            "id": face["id"],
            "url": f"http://192.0.2.50:8080/faces/pet_{with_photo['id']}_face_{face['id']}.jpg",
        }],
    }]
    assert isinstance(entries[0]["id"], int)
    assert isinstance(entries[0]["discern"][0]["id"], int)


async def test_discern_pic_payload_serves_every_face_of_a_pet(pet_registry: PetRegistry):
    pet = await pet_registry.create("A", device_ids=[1])
    ids = [(await pet_registry.add_face(pet["id"], JPEG_BYTES))["id"] for _ in range(3)]
    entries = (await pet_registry.discern_pic_payload(1, "h"))["result"]["list"]
    assert [d["id"] for d in entries[0]["discern"]] == ids


async def test_resolve_pet_ref_maps_our_own_id_and_a_bound_alias(pet_registry: PetRegistry):
    pet = await pet_registry.create("Mruczek")
    assert await pet_registry.resolve_pet_ref(pet["id"]) == pet["id"]
    # an identity the device cached from PetKit's cloud resolves only once bound
    assert await pet_registry.resolve_pet_ref(101392625) is None
    await pet_registry.update(pet["id"], device_pet_ids_json=json.dumps([101392625]))
    assert await pet_registry.resolve_pet_ref(101392625) == pet["id"]
    assert await pet_registry.resolve_pet_ref(None) is None


async def test_resolve_pet_ref_survives_unparseable_aliases(pet_registry: PetRegistry):
    pet = await pet_registry.create("Mruczek")
    await pet_registry.update(pet["id"], device_pet_ids_json="{not json")
    assert await pet_registry.resolve_pet_ref(999) is None
    assert await pet_registry.resolve_pet_ref(pet["id"]) == pet["id"]


async def test_a_legacy_single_photo_is_migrated_into_pet_faces(tmp_path: Path):
    """Installs predating `pet_faces` kept the one photo on the pet row; it has
    to survive, or their device is told to recognise nobody."""
    from petkit_local.events.store import EventStore

    store = EventStore(tmp_path / "petkit.db")
    await store.connect()
    legacy = tmp_path / "faces" / "pet_1_face_1.jpg"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(JPEG_BYTES)
    pid = await store.upsert_pet({"name": "Mruczek", "photo_path": str(legacy), "face_id": "1"})
    await store.close()

    reopened = EventStore(tmp_path / "petkit.db")
    await reopened.connect()
    assert [f["photo_path"] for f in await reopened.pet_faces(pid)] == [str(legacy)]
    # idempotent: a second open must not duplicate it
    await reopened.close()
    await reopened.connect()
    assert len(await reopened.pet_faces(pid)) == 1
    await reopened.close()


async def test_pet_for_related_event_returns_none_without_attribution(pet_registry: PetRegistry,
                                                                      event_store: EventStore):
    await event_store.upsert_event({"device_id": 1, "event_type": "pet_out",
                                    "related_event": "r1", "ts": 1.0})
    assert await pet_registry.pet_for_related_event("r1") is None


async def test_pet_for_related_event_resolves_attributed_pet(pet_registry: PetRegistry,
                                                             event_store: EventStore):
    pet = await pet_registry.create("Mruczek")
    await event_store.upsert_event({"device_id": 1, "event_type": "pet_out", "related_event": "r1",
                                    "pet_id": pet["id"], "ts": 1.0})
    found = await pet_registry.pet_for_related_event("r1")
    assert found is not None
    assert found["id"] == pet["id"]


def test_jpeg_dimensions_reads_the_frame_header():
    from petkit_local.media.transcode import jpeg_dimensions
    # SOI, a APP0 segment to skip over, then SOF0 carrying 224x224
    jpeg = (b"\xff\xd8"
            + b"\xff\xe0" + (16).to_bytes(2, "big") + b"\x00" * 14
            + b"\xff\xc0" + (17).to_bytes(2, "big") + b"\x08"
            + (224).to_bytes(2, "big") + (224).to_bytes(2, "big") + b"\x00" * 8)
    assert jpeg_dimensions(jpeg) == (224, 224)
    assert jpeg_dimensions(b"not a jpeg") is None
    assert jpeg_dimensions(b"\xff\xd8") is None          # truncated, no frame
    assert jpeg_dimensions(b"\xff\xd8\xff\xc0\x00\x01") is None  # bogus length


async def test_a_face_photo_already_the_right_size_is_stored_untouched():
    """The cloud's own reference photos are 224x224 face crops; re-encoding one
    would cost a JPEG generation for nothing."""
    from petkit_local.media.transcode import FACE_PHOTO_SIZE, normalize_face_photo
    jpeg = (b"\xff\xd8"
            + b"\xff\xc0" + (17).to_bytes(2, "big") + b"\x08"
            + (FACE_PHOTO_SIZE).to_bytes(2, "big") + (FACE_PHOTO_SIZE).to_bytes(2, "big")
            + b"\x00" * 8 + b"\xff\xd9")
    assert await normalize_face_photo(jpeg) is jpeg


async def test_an_unconvertible_photo_is_still_stored(pet_registry: PetRegistry):
    """A filter graph that fails must not lose the upload — worse recognition,
    not a dropped photo. JPEG_BYTES is valid magic but not a decodable image."""
    pet = await pet_registry.create("Mruczek")
    face = await pet_registry.add_face(pet["id"], JPEG_BYTES)
    assert face is not None
    assert Path(face["photo_path"]).read_bytes() == JPEG_BYTES


def test_jpeg_dimensions_skips_legal_fill_bytes():
    """0xFF padding before a marker is legal JPEG. Treating one as the marker
    read a length out of the real marker and lost the frame header."""
    from petkit_local.media.transcode import jpeg_dimensions
    jpeg = (b"\xff\xd8"
            + b"\xff\xff\xff"                                   # fill
            + b"\xff\xc0" + (17).to_bytes(2, "big") + b"\x08"
            + (224).to_bytes(2, "big") + (224).to_bytes(2, "big") + b"\x00" * 8)
    assert jpeg_dimensions(jpeg) == (224, 224)
    # end-of-image before any frame header is "unknown", not a bogus length
    assert jpeg_dimensions(b"\xff\xd8" + b"\xff\xd9" + b"\x00" * 8) is None


async def test_add_face_returns_none_when_the_file_cannot_be_written(pet_registry: PetRegistry,
                                                                     monkeypatch):
    """Documented contract is "or None". A full disk must reach the panel as an
    error message, not a 500 with a traceback."""
    pet = await pet_registry.create("Mruczek")

    def boom(*a, **kw):
        raise OSError("no space left on device")
    monkeypatch.setattr("builtins.open", boom)

    assert await pet_registry.add_face(pet["id"], JPEG_BYTES) is None
    monkeypatch.undo()
    # and no row survives pointing at a file that was never written
    assert await pet_registry.faces(pet["id"]) == []


def test_best_match_still_falls_back_when_score_info_is_empty():
    """`score_info` is an empty list on 31 of 33 captured detection results, so
    an `is None` check would swallow a sibling bare `score`."""
    from petkit_local.events.ingest import _extract_pet_ref, _extract_score
    assert _extract_score({"score_info": [], "score": 131}) == 131
    assert _extract_score({"score_info": [131]}) == 131
    assert _extract_score({"score_info": []}) is None
    assert _extract_pet_ref({"score_info": [], "petId": 7}) == 7


async def test_weight_references_skip_unset_and_non_positive(pet_registry: PetRegistry):
    a = await pet_registry.create("A", weight=5075)
    await pet_registry.create("B")
    await pet_registry.create("C", weight=0)
    await pet_registry.create("D", weight=-3)
    assert await pet_registry.weight_references() == {a["id"]: 5075.0}
