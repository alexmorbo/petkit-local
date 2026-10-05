"""Pets: CRUD, the reference photos a device's NPU matches against, and import.

The photos are the point. A pet row on its own does nothing; what makes
recognition work is the JPEGs `dev_discern_pic` serves to the device, so this
module owns storing them, serving them back to the panel, and fetching the ones
the PetKit account already holds.

Import is a button, never a poller. A pet arrives under a NEW local id with
PetKit's own id bound as an alias, because that foreign id is what a box still
matching against cloud-cached faces reports.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from typing import Any

import aiohttp
from aiohttp import web

from petkit_local.ai.pets import cloud_pets, weight_conflicts
from petkit_local.events.store import MAX_FACES_PER_PET
from petkit_local.http.cloud_fetch import CLOUD_TIMEOUT, CloudRefused, fetch_as_device
from petkit_local.utils.coerce import to_bool, to_int
from petkit_local.utils.paths import UnsafePathError, safe_join
from petkit_local.web.api._common import (
    _cloud_upstream, _face_summaries, _json_body, _path_id, _pets_by_id,
)

log = logging.getLogger(__name__)


def _reference_weight(value: Any) -> tuple[bool, float | None]:
    """Parse a reference weight from the panel: `(usable, grams or None)`.

    None or "" clears the reference (usable, None); zero or negative clears it
    too, since no pet weighs that. Anything that will not parse as a finite
    number is NOT usable and the caller leaves the stored value alone -- the
    same skip-on-garbage rule `device_ids` follows. Whole grams, as the scale
    reports them.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return True, None
    if isinstance(value, bool):
        return False, None
    try:
        grams = float(value)
    except (TypeError, ValueError):
        return False, None
    if not math.isfinite(grams):
        return False, None
    if grams <= 0:
        return True, None
    return True, float(round(grams))


async def _reattribute(request: web.Request) -> int:
    """Re-run weight attribution over history and republish the pets it moved.

    Returns how many event rows changed. A pet that no longer exists (it was
    just deleted) is skipped -- there is no device left to publish to.
    """
    pet_registry = request.app.get("pet_registry")
    if pet_registry is None:
        return 0
    changed, affected = await pet_registry.reattribute_by_weight()
    ha_publisher = request.app.get("ha_publisher")
    store = request.app.get("event_store")
    if ha_publisher is not None and store is not None:
        for pid in sorted(affected):
            pet = await pet_registry.get(pid)
            if pet is not None:
                await ha_publisher.publish_pet_state(pet, store)
    return changed


async def _weight_conflicts(pet_registry: Any, pet_id: int | None = None) -> list[dict[str, Any]]:
    """`weight_conflicts` as the panel shows them, optionally only those naming `pet_id`.

    A warning, never a refusal: the reference is saved either way.
    """
    names = {p["id"]: p.get("name") or f"#{p['id']}" for p in await pet_registry.all()}
    refs = await pet_registry.weight_references()
    return [{"pet_ids": [a, b], "names": [names.get(a), names.get(b)],
             "weights": [refs[a], refs[b]], "diff_g": round(d)}
            for a, b, d in weight_conflicts(refs)
            if pet_id is None or pet_id in (a, b)]


async def api_pets_list_create(request: web.Request) -> web.Response:
    """List the pets, or create one.

    GET answers `{"pets": [...], "weight_conflicts": [...]}`, POST
    `{"pet": {...}, "reattributed": n, "weight_conflicts": [...]}` -- the
    conflicts are pairs of references within `WEIGHT_CONFLICT_G` (POST: only
    those naming the new pet), a warning that never blocks the save. A `name`
    is required; unparseable `device_ids` degrade to "no devices" rather than
    failing the create, since the link can be fixed afterwards, and an
    unparseable `weight` to no reference. A new pet is immediately published to
    HA as its own virtual device, and one with a reference weight takes its
    share of the unattributed visits (`ai/pets.py::nearest_pet_by_weight`).

    A name already taken (trimmed, case-insensitive) answers 409 with the
    `existing_id`, unless `allow_duplicate` is set: one pet can be on several
    devices, and a second pet of the same name and weight ties every visit.
    Cloud import calls `PetRegistry.create` directly and bypasses this on
    purpose -- its names are `PetKit pet <ref>`, unique per ref.
    """
    pet_registry = request.app.get("pet_registry")
    if pet_registry is None:
        return web.json_response({"error": "pet registry not available"}, status=400)

    if request.method == "GET":
        return web.json_response({
            "pets": list((await _pets_by_id(request)).values()),
            "weight_conflicts": await _weight_conflicts(pet_registry),
        })

    body = await _json_body(request)

    name = str(body.get("name", "")).strip()
    if not name:
        return web.json_response({"error": "name required"}, status=400)
    if not to_bool(body.get("allow_duplicate"), False):
        wanted = name.casefold()
        for other in await pet_registry.all():
            if str(other.get("name") or "").strip().casefold() == wanted:
                return web.json_response(
                    {"error": f"A pet called {other['name']} already exists — add this "
                              f"device to it from its card instead",
                     "existing_id": other["id"]}, status=409)
    try:
        device_ids = list(dict.fromkeys(int(x) for x in (body.get("device_ids") or [])))
    except (TypeError, ValueError):
        device_ids = []

    _, weight = _reference_weight(body.get("weight"))

    pet = await pet_registry.create(name, device_ids=device_ids, weight=weight)
    ha_publisher = request.app.get("ha_publisher")
    if ha_publisher is not None:
        await ha_publisher.publish_pet_discovery(pet)
    reattributed = await _reattribute(request) if weight is not None else 0
    return web.json_response({"pet": pet, "reattributed": reattributed,
                              "weight_conflicts": await _weight_conflicts(pet_registry,
                                                                          pet["id"])})


async def api_pet_detail(request: web.Request) -> web.Response:
    """Read, update or delete one pet.

    GET/POST answer `{"pet": {...}}`, DELETE `{"ok": bool}`.
    POST is a partial update: only the fields present in the body are touched,
    and a `device_ids` that will not parse is skipped rather than clearing the
    links. Same for `weight` (the reference visits are attributed against):
    garbage is skipped, null/""/0 clears it. A POST or DELETE that can change
    weight attribution re-runs it over history and adds `reattributed`; a POST
    carrying `weight` also adds `weight_conflicts` (a warning, see the list
    endpoint). DELETE also removes the pet's HA device -- empty retained
    configs, state and availability, queued for the next connect if HA's broker
    is down. Best effort: it never fails the delete.
    """
    pet_registry = request.app.get("pet_registry")
    if pet_registry is None:
        return web.json_response({"error": "pet registry not available"}, status=400)
    pid = _path_id(request)

    if request.method == "DELETE":
        ok = await pet_registry.delete(pid)
        ha_publisher = request.app.get("ha_publisher")
        if ok and ha_publisher is not None:
            # Before the reattribution, so the republish of the pets it moved
            # follows the removal rather than racing it.
            try:
                await ha_publisher.unpublish_pet(pid)
            except Exception:
                log.warning("Removing pet %d from Home Assistant failed", pid, exc_info=True)
        reattributed = await _reattribute(request) if ok else 0
        return web.json_response({"ok": ok, "reattributed": reattributed})

    if request.method == "GET":
        pet = await pet_registry.get(pid)
        if pet is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({"pet": pet})

    body = await _json_body(request)

    # Read before the update: the alias bookkeeping below needs to know which
    # ids are being REMOVED, and after the write that information is gone.
    before = await pet_registry.get(pid)
    if before is None:
        return web.json_response({"error": "not found"}, status=404)

    fields: dict[str, Any] = {}
    if "name" in body:
        fields["name"] = str(body["name"]).strip()
    if "device_ids" in body:
        try:
            # Nothing else to do on a membership change: `discern_pic_payload`
            # reads this live, so the device gets it at its next
            # `dev_discern_pic` poll (boot / about hourly), like a new mugshot,
            # and the pet's HA device does not depend on it.
            fields["device_ids_json"] = list(dict.fromkeys(int(x) for x in body["device_ids"]))
        except (TypeError, ValueError):
            pass
    if "device_pet_ids" in body:
        # Identities the device reports that are not ours — see
        # ai/pets.py::resolve_pet_ref. Same skip-on-garbage rule as above:
        # dropping an existing binding by accident would silently un-attribute
        # every event it covers.
        try:
            fields["device_pet_ids_json"] = json.dumps(
                [int(x) for x in body["device_pet_ids"]])
        except (TypeError, ValueError):
            pass
    if "weight" in body:
        usable, weight = _reference_weight(body["weight"])
        if usable:
            fields["weight"] = weight

    pet = await pet_registry.update(pid, **fields)
    if pet is None:
        return web.json_response({"error": "not found"}, status=404)

    # Binding a foreign identity is retroactive: the events already carry it in
    # `pet_ref`, so attributing them is a single UPDATE and the user sees their
    # history named the moment they confirm the mapping.
    #
    # Three cases, and the last two are why this is not just a loop over the
    # new list. An id REMOVED from the list has to have its events cleared, or
    # history stays on this pet while new events resolve to nobody. An id taken
    # from ANOTHER pet has to be removed from that pet's list, or
    # `resolve_pet_ref` keeps handing future events to whichever id is lower
    # while the history has already moved — the two would diverge permanently,
    # with no way back through the UI.
    bound = 0
    store = request.app.get("event_store")
    if "device_pet_ids_json" in fields and store is not None:
        wanted = set(json.loads(fields["device_pet_ids_json"]))
        try:
            had = set(json.loads(before.get("device_pet_ids_json") or "[]"))
        except (json.JSONDecodeError, TypeError):
            had = set()

        for ref in had - wanted:
            bound += await store.bind_pet_ref(ref, None)

        for ref in wanted:
            for other in await store.pets_claiming_ref(ref):
                if other == pid:
                    continue
                other_pet = await pet_registry.get(other)
                try:
                    aliases = json.loads((other_pet or {}).get("device_pet_ids_json") or "[]")
                except (json.JSONDecodeError, TypeError):
                    aliases = []
                await pet_registry.update(
                    other, device_pet_ids_json=json.dumps([a for a in aliases if a != ref]))
            bound += await store.bind_pet_ref(ref, pid)

    reattributed = 0
    if "weight" in fields and fields["weight"] != before.get("weight"):
        reattributed = await _reattribute(request)

    ha_publisher = request.app.get("ha_publisher")
    if ha_publisher is not None:
        await ha_publisher.publish_pet_discovery(pet)
        if store is not None:
            await ha_publisher.publish_pet_state(pet, store)
    out: dict[str, Any] = {"pet": pet, "bound_events": bound, "reattributed": reattributed}
    if "weight" in fields:
        out["weight_conflicts"] = await _weight_conflicts(pet_registry, pid)
    return web.json_response(out)


async def api_pet_faces(request: web.Request) -> web.Response:
    """List a pet's reference photos, or add one.

    GET answers `{"faces": [{id, url}]}`. POST takes the raw image bytes (not a
    multipart form) and answers `{"face": {id, url}}`; only a real JPEG is
    accepted, since `dev_discern_pic` only ever serves JPEGs to the device.

    A pet holds at most `MAX_FACES_PER_PET` photos — the firmware has its own
    "Too many face picture" guard whose limit we could not recover, so we stop
    at the largest count the real cloud was seen to serve.
    """
    pet_registry = request.app.get("pet_registry")
    if pet_registry is None:
        return web.json_response({"error": "pet registry not available"}, status=400)
    pid = _path_id(request)
    if await pet_registry.get(pid) is None:
        return web.json_response({"error": "not found"}, status=404)

    if request.method == "GET":
        return web.json_response({"faces": await _face_summaries(pet_registry, pid)})

    data = await request.read()
    if not data:
        return web.json_response({"error": "empty body"}, status=400)
    if data[:3] != b"\xff\xd8\xff":
        return web.json_response({"error": "not a valid JPEG"}, status=400)
    if len(await pet_registry.faces(pid)) >= MAX_FACES_PER_PET:
        return web.json_response(
            {"error": f"at most {MAX_FACES_PER_PET} photos per pet"}, status=400)

    face = await pet_registry.add_face(pid, data)
    if face is None:
        return web.json_response({"error": "could not store photo"}, status=400)
    return web.json_response(
        {"face": {"id": face["id"], "url": f"api/pets/{pid}/faces/{face['id']}/photo"}})


async def _owned_face(request: web.Request) -> dict[str, Any] | None:
    """The face named by `{id}/{face_id}`, or None unless that pet owns it.

    Both ids are in the path and BOTH have to be checked. Matching only
    `face_id` made `DELETE /api/pets/999/faces/3` delete face 3 whoever owned
    it, unlink its file and answer `{"ok": true}` — a destructive route acting
    on an ownership assumption it never verified. A stale panel tab is enough
    to trigger that.
    """
    pet_registry = request.app.get("pet_registry")
    if pet_registry is None:
        return None
    try:
        pid = int(request.match_info["id"])
        face_id = int(request.match_info["face_id"])
    except ValueError:
        return None
    face = await pet_registry.face(face_id)
    return face if face and face.get("pet_id") == pid else None


async def api_pet_face_detail(request: web.Request) -> web.Response:
    """DELETE one reference photo; answers `{"ok": bool}`."""
    pet_registry = request.app.get("pet_registry")
    if pet_registry is None:
        return web.json_response({"error": "pet registry not available"}, status=400)
    face = await _owned_face(request)
    if face is None:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response({"ok": await pet_registry.delete_face(face["id"])})


async def api_pet_face_photo(request: web.Request) -> web.Response:
    """Serve one reference photo to the PANEL.

    The device fetches these from `/faces/{filename}` on the device-facing app;
    that is a different aiohttp application on a different port, so the panel
    cannot link to it and needs its own route to show a thumbnail.
    """
    face = await _owned_face(request)
    if face is None or not face.get("photo_path"):
        return web.Response(status=404, text="not found")

    # The stored path is re-resolved against the faces directory rather than
    # trusted. Every writer today goes through `safe_join`, so this is
    # unreachable — but `photo_path` is a writable column and this route is on
    # an unauthenticated panel, so it must not depend on that staying true.
    # Same reasoning as handlers/discern.py::handle_faces.
    faces_dir = os.path.join(request.app["cfg"].get("data_dir", "/data"), "faces")
    try:
        path = safe_join(faces_dir, os.path.basename(face["photo_path"]))
    except UnsafePathError:
        return web.Response(status=404, text="not found")
    if not os.path.isfile(path):
        return web.Response(status=404, text="not found")
    return web.FileResponse(path)


async def api_pets_unbound(request: web.Request) -> web.Response:
    """Identities the device reported that match no pet row.

    Answers `{"unbound": [{pet_ref, count, last_ts}]}`. These are normally ids
    a box cached from PetKit's cloud before the takeover; the user binds one to
    a pet via `POST /api/pets/{id}` `device_pet_ids`, and that backfills every
    past event carrying it. We never guess the mapping — with one pet in the
    table the guess would even look right, and be wrong the day a second
    animal appears.
    """
    store = request.app.get("event_store")
    if store is None:
        return web.json_response({"error": "event store not available"}, status=400)
    return web.json_response({"unbound": await store.unbound_pet_refs()})


#: Cap on one downloaded reference photo. PetKit's are 224x224 face crops of a
#: few KB; the cap is loose enough not to matter and tight enough that a reply
#: pointing at something enormous cannot fill the disk before it is rejected.
MAX_FACE_DOWNLOAD_BYTES = 4 * 1024 * 1024

#: Per-photo, not per-import: one unreachable URL must not hold up the rest.
FACE_DOWNLOAD_TIMEOUT = 20


async def _fetch_cloud_face(session: aiohttp.ClientSession, url: str) -> bytes | None:
    """Download one reference photo, or None if it could not be had.

    No new trust is extended by this. These are the exact URLs we hand the
    device in the same reply — `dev_discern_pic` is passed through untouched in
    proxy mode — so the device is already fetching them, from the same host,
    on its own. What is new is only that the owner asked for a copy.

    Read in chunks against a cap rather than with `read()`: the size is
    whatever the far end claims, and a `Content-Length` is not a promise.
    `add_face` then does the real validation, since JPEG magic bytes are the
    only thing that makes these bytes a face photo.
    """
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                log.info("Face photo %s answered %d", url, resp.status)
                return None
            data = bytearray()
            async for chunk in resp.content.iter_chunked(64 * 1024):
                data.extend(chunk)
                if len(data) > MAX_FACE_DOWNLOAD_BYTES:
                    log.warning("Face photo %s is over %d bytes - refused",
                                url, MAX_FACE_DOWNLOAD_BYTES)
                    return None
            return bytes(data)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.info("Could not fetch the face photo at %s", url, exc_info=True)
        return None


async def api_pets_import(request: web.Request) -> web.Response:
    """Ask PetKit which pets this device recognises, and bring them across.

    POST body: `{device_id}`.

    `dev_discern_pic` is the account's recognition set for one device: which
    animals it matches against, and where their reference photos live. This
    asks for it once, on a button, and then downloads those photos.

    Each pet arrives under a NEW local id with PetKit's own id bound to it as a
    `device_pet_ids_json` alias. That binding is worth as much as the photos: a
    box still matching against cloud-cached faces reports THAT id, so the alias
    is what makes its events resolve — and `bind_pet_ref` applies it to the
    history retroactively, naming events already recorded.

    Names are not in that payload — a pet's name lives in the account API,
    which no device ever asks for — so an imported pet is called
    `PetKit pet <id>` until somebody renames it. Nothing is invented.
    """
    pet_registry = request.app.get("pet_registry")
    if pet_registry is None:
        return web.json_response({"error": "pet registry not available"}, status=503)
    reg = request.app["registry"]

    body = await _json_body(request)
    if not isinstance(body, dict):
        return web.json_response({"error": "expected an object"}, status=400)

    device = reg.get(to_int(body.get("device_id"), 0) or 0)
    if device is None:
        return web.json_response({"error": "no such device"}, status=404)

    async with aiohttp.ClientSession(timeout=CLOUD_TIMEOUT) as session:
        try:
            payload = await fetch_as_device(
                session, _cloud_upstream(request), device, "dev_discern_pic")
        except CloudRefused as e:
            return web.json_response({"error": str(e)}, status=502)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return web.json_response(
                {"error": f"could not reach PetKit: {e}"}, status=502)

        store = request.app.get("event_store")
        results = []
        for entry in cloud_pets(payload, device.petkit_id):
            pet_ref = entry["pet_ref"]
            if await pet_registry.resolve_pet_ref(pet_ref) is not None:
                results.append({"pet_ref": pet_ref, "outcome": "already yours"})
                continue

            pet = await pet_registry.create(f"PetKit pet {pet_ref}",
                                            device_ids=[device.petkit_id])
            await pet_registry.update(pet["id"],
                                      device_pet_ids_json=json.dumps([pet_ref]))
            bound = await store.bind_pet_ref(pet_ref, pet["id"]) if store else 0

            faces = 0
            for face in entry["faces"]:
                data = await _fetch_cloud_face(session, face.get("url", ""))
                if data and await pet_registry.add_face(pet["id"], data):
                    faces += 1

            pet = await pet_registry.get(pet["id"])
            ha_publisher = request.app.get("ha_publisher")
            if ha_publisher is not None:
                await ha_publisher.publish_pet_discovery(pet)

            results.append({
                "pet_ref": pet_ref, "pet_id": pet["id"], "name": pet["name"],
                "faces_imported": faces, "faces_offered": len(entry["faces"]),
                "bound_events": bound, "outcome": "imported",
            })

    return web.json_response({
        "imported": sum(1 for r in results if r.get("outcome") == "imported"),
        "results": results,
    })
