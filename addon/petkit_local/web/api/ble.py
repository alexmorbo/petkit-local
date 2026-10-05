"""BLE accessories: pairing them, importing them, and writing to them.

Pairing lives here because it lives in the cloud, and we are the cloud — see the
comment over `ACCESSORY_ID_BASE` for why a form and not a discovery scan. The
delivery rules differ from a real device's on purpose: an accessory has no
heartbeat queue, so a command is either relayed by a parent that is on MQTT
right now, or refused.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web

from petkit_local.devices.ble import (
    BLE_TYPES, CLOUD_BINDING_ENDPOINTS, ble_command_for, cloud_bindings, normalize_mac,
)
from petkit_local.ha.commands import PROPERTY_SET_SUFFIX, Refused, make_mqtt_property_set
from petkit_local.ha.entities.ble import get_ble_entities
from petkit_local.http.cloud_fetch import CLOUD_TIMEOUT, CloudRefused, fetch_as_device
from petkit_local.utils.coerce import to_int
from petkit_local.utils.const import device_display_name
from petkit_local.utils.dicts import dig_path
from petkit_local.web.api._common import _cloud_upstream, _json_body, _path_id

if TYPE_CHECKING:
    from petkit_local.devices.ble import BLEDevice, BLERegistry
    from petkit_local.devices.registry import DeviceRegistry

log = logging.getLogger(__name__)


# --- BLE accessories ---------------------------------------------------------
# A K3 Pura Air spray or W5 fountain has no network identity: it is reached over BLE
# by a mains-powered neighbour that relays for it. The device does NOT discover
# accessories — it pulls a list from the cloud (`dev_ble_device`) and scans for
# exactly those MACs, and no firmware in any of the three we have examined has
# an endpoint for reporting a newly-found one upward. Pairing happens in
# PetKit's app, which is to say in the cloud; since we ARE the cloud, pairing
# has to be entered here. That is why this is a form and not a discovery scan.


#: Accessory ids are allocated from here up. Deliberately far above the 8-digit
#: ids PetKit issues real devices, so a generated one cannot be mistaken for a
#: real device id and cannot collide with one that registers later.
ACCESSORY_ID_BASE = 900001


def _next_accessory_id(reg: DeviceRegistry, ble: BLERegistry) -> int:
    """An unused id for a new accessory.

    The id is ours to choose. The firmware stores whatever we send in its relay
    list, but every report it sends back identifies the accessory by
    `{"mac", "type"}` — the id appears in no report format string in any of the
    three firmwares. So it is a handle for our side (it becomes the Home
    Assistant device identity and the MQTT topic), not something the user has
    to go and look up.
    """
    taken = {d.petkit_id for d in ble.all()} | {d.petkit_id for d in reg.all()}
    candidate = ACCESSORY_ID_BASE
    while candidate in taken:
        candidate += 1
    return candidate


def _ble_view(dev: BLEDevice, reg: DeviceRegistry | None = None) -> dict[str, Any]:
    """One accessory as the panel shows it: identity, wire entry, and its state.

    Carries the same `entities` block a real device's detail does — resolved
    values included — because the panel renders an accessory as its own device
    panel and reuses the very same table and control renderers. Without it the
    accessory was three cells in its parent's card while its decoded state, its
    entities and its controls existed only in Home Assistant.

    The state document needs no adapter: an accessory's `value_path` is already
    `states.x`/`consumables.x` and `dev.state` has exactly those sections, so
    `dig_path` reads it directly. A button has no path and no value — it is an
    action, and `None` is the honest answer rather than the whole document.
    """
    entities = get_ble_entities(dev.ble_type)
    parent = reg.get(dev.link_with) if (reg and dev.link_with) else None
    return {
        "petkit_id": dev.petkit_id,
        "ble_type": dev.ble_type,
        "name": device_display_name(dev.ble_type),
        # Who relays for it. An accessory with no reachable parent is not
        # merely offline, it is unaddressable, and the panel says which.
        "parent_name": device_display_name(parent.device_type) if parent else "",
        "parent_type": parent.device_type if parent else "",
        "parent_online": bool(parent and parent.online),
        "last_seen": dev.last_seen,
        "entities": [{
            "component": e.component, "key": e.key, "name": e.name,
            "value_path": e.value_path, "unit": e.unit, "device_class": e.device_class,
            "icon": e.icon, "options": e.options, "option_values": e.option_values,
            "settable": e.is_settable,
            "entity_category": e.entity_category,
            "min": e.min_value, "max": e.max_value, "step": e.step,
            "value": dig_path(dev.state, e.value_path) if e.value_path else None,
        } for e in entities],
        "mac": dev.mac,
        "secret": dev.secret,
        "interval": dev.interval,
        "link_with": dev.link_with,
        "serial_number": dev.serial_number,
        "scan_type": dev.scan_type,
        # True when the `type` in the wire entry below is a working assumption
        # rather than a value anybody has captured. Surfaced because the person
        # who can settle it is the one whose fountain either pairs or does not.
        "scan_type_is_guessed": dev.scan_type_is_guessed,
        # Exactly what `dev_ble_device` will hand the parent, so a user can see
        # whether what they typed is what the device will be told to scan for.
        "wire_entry": dev.to_ble_list_entry(),
        "state": dev.state,
    }


async def _send_k3_link(request: web.Request, parent_id: int, k3_id: int) -> str:
    """Tell a parent litter box which K3 it owns. Returns the transport used.

    A K3 is the one accessory NOT served through `dev_ble_device` — the relay
    list deliberately excludes it — so pairing one means writing `k3Id` on the
    parent instead. `autoRefresh` rides along on a link, and `k3Id: 0` unlinks.
    Both come from localkit's `PetkitPuraMax::link/unlink`, which is the only
    source for this; no capture of ours has ever contained a linked K3.

    Best-effort by design: the accessory is registered either way, because a
    parent that is asleep must not fail the pairing the user just entered. It
    picks the property up from `dev_device_info` on its next poll regardless.
    """
    reg = request.app["registry"]
    bridge = request.app.get("bridge")
    parent = reg.get(parent_id)
    if parent is None:
        return "no parent"

    params = {"k3Id": k3_id, "autoRefresh": 1} if k3_id else {"k3Id": 0}
    envelope = make_mqtt_property_set(params)
    if parent.mqtt_connected and bridge is not None and getattr(bridge, "_client", None):
        try:
            await bridge.publish_to_device(parent, PROPERTY_SET_SUFFIX, envelope)
            return "mqtt"
        except Exception as e:
            log.warning("panel: K3 link publish failed for device %d, queueing: %s", parent_id, e)
    envelope["_service_suffix"] = PROPERTY_SET_SUFFIX
    parent.command_queue.append(envelope)
    return "heartbeat-queue"


async def _nudge_relay_list(request: web.Request, parent_id: int) -> None:
    """Tell a parent to refetch `dev_ble_device` now, after its list changed.

    Without it a pairing takes effect on the parent's own schedule, and that
    schedule is `nextTick: 3600` — an hour in which the accessory is in the
    panel, in Home Assistant, and invisible to the device meant to scan for it.
    The poll meanwhile pushes `connect` for a MAC the parent has never been
    told about, which fails exactly like a wrong scan type does.

    Best effort: a parent that is off MQTT picks the change up on its next
    fetch, which is the behaviour we had for everything.
    """
    bridge = request.app.get("bridge")
    reg = request.app["registry"]
    parent = reg.get(parent_id) if parent_id else None
    if bridge is None or parent is None:
        return
    try:
        await bridge.publish_relay_update(parent)
    except Exception as exc:  # a refresh is a convenience, never a failure
        log.warning("Could not nudge parent %d to refresh its relay list: %s",
                    parent_id, exc)


async def api_ble_accessories(request: web.Request) -> web.Response:
    """List (GET) or pair/update (POST) a BLE accessory.

    POST body: `{ble_type, petkit_id, link_with, mac, secret, interval,
    serial_number}`. The five wire fields are the ones the firmware's own parse
    logs name (`id`, `mac`, `secret`, `interval`, `type` in
    `ble_relay_network.c`), so this form is the protocol, not a UI convenience.

    Answers `{"accessories": [...]}` either way, so the caller never has to
    re-fetch after a write.
    """
    ble = request.app["ble_registry"]
    reg = request.app["registry"]
    if ble is None:
        return web.json_response({"error": "no BLE registry"}, status=503)

    if request.method == "POST":
        body = await _json_body(request)
        if not isinstance(body, dict):
            return web.json_response({"error": "expected an object"}, status=400)

        ble_type = str(body.get("ble_type", "")).lower().strip()
        if ble_type not in BLE_TYPES:
            return web.json_response(
                {"error": f"ble_type must be one of {', '.join(BLE_TYPES)}"}, status=400)

        petkit_id = to_int(body.get("petkit_id"), 0) or 0
        if petkit_id < 0:
            return web.json_response({"error": "petkit_id cannot be negative"}, status=400)
        if not petkit_id:
            petkit_id = _next_accessory_id(reg, ble)
        # An accessory shares the `petkit_{id}` HA identity and the
        # `petkit-local/{id}/state` topic with real devices, so a collision does
        # not merely confuse the panel — it makes two devices fight over one
        # Home Assistant entity set.
        if reg.get(petkit_id) is not None:
            return web.json_response(
                {"error": f"id {petkit_id} is already a real device"}, status=409)

        mac = normalize_mac(str(body.get("mac", "")))
        if not mac:
            return web.json_response(
                {"error": "mac must be 12 hex digits, e.g. AA:BB:CC:DD:EE:FF"}, status=400)
        clash = ble.get_by_mac(mac)
        if clash is not None and clash.petkit_id != petkit_id:
            return web.json_response(
                {"error": f"mac already paired to id {clash.petkit_id}"}, status=409)

        link_with = to_int(body.get("link_with"), 0) or 0
        if link_with and reg.get(link_with) is None:
            return web.json_response({"error": f"no device with id {link_with}"}, status=400)

        ble.register(
            ble_type=ble_type,
            petkit_id=petkit_id,
            mac=mac,
            secret=str(body.get("secret", "")),
            interval=to_int(body.get("interval"), 240) or 240,
            link_with=link_with,
            serial_number=str(body.get("serial_number", "")),
            # Optional: overrides the `type` the parent is told to scan for.
            # Only the W5's is a captured value, so for the other fountains this
            # is the field that turns a guess into something a user can correct.
            scan_type=max(to_int(body.get("scan_type"), 0) or 0, 0),
        )
        await _announce_pairing(request, ble.get(petkit_id))

    return web.json_response({"accessories": [_ble_view(d, reg) for d in ble.all()]})


async def _announce_pairing(request: web.Request, dev: BLEDevice | None) -> None:
    """Make a just-paired accessory real to Home Assistant and to its parent.

    Shared by hand-pairing and by cloud import so the two cannot drift: an
    accessory that reaches the registry through one path and not the other is
    the kind of difference nobody notices until a device is silent.
    """
    if dev is None:
        return

    # Publish immediately rather than waiting for the next HA reconnect:
    # an accessory that appears in the panel but not in HA reads as broken.
    publisher = request.app.get("ha_publisher")
    if publisher is not None:
        await publisher.publish_ble_discovery(dev)
        await publisher.publish_ble_state(dev)

    # A W5 is picked up from the relay list the parent already polls; a K3
    # is not in that list at all and has to be named on the parent.
    if dev.ble_type == "k3" and dev.link_with:
        await _send_k3_link(request, dev.link_with, dev.petkit_id)
    else:
        await _nudge_relay_list(request, dev.link_with)


async def api_ble_import(request: web.Request) -> web.Response:
    """Ask PetKit which accessories this device owns, and pair them here.

    POST body: `{device_id}`.

    One request out and one answer back, on a button — nothing runs in the
    background and nothing is staged. That is the whole design: the account is
    asked when somebody wants to know, not on a schedule of ours.

    Three endpoints are read, because a K3 is in none of the same places a
    fountain is: `dev_ble_device` lists the relayed accessories, and the spray's
    binding and parameters live in `dev_device_info` and `dev_k3_device_info`
    (issue #6, and the half of #17 that needs the account's `secret`).

    Answers `{imported, results, accessories}` where `results` names every
    accessory and what happened to it, refusals included — an import that
    quietly covers four of five is worse than one that says so.
    """
    ble = request.app["ble_registry"]
    reg = request.app["registry"]
    if ble is None:
        return web.json_response({"error": "no BLE registry"}, status=503)

    body = await _json_body(request)
    if not isinstance(body, dict):
        return web.json_response({"error": "expected an object"}, status=400)

    device = reg.get(to_int(body.get("device_id"), 0) or 0)
    if device is None:
        return web.json_response({"error": "no such device"}, status=404)

    # `dev_k3_device_info` is a litter box's endpoint — PetKit answers 404 to a
    # feeder or a camera asking, and the firmware string behind it is
    # `t4/dev_k3_device_info`. Skipped rather than asked-and-reported, so the
    # answer for a device that can never have a spray is not a line of noise.
    endpoints = sorted(
        e for e in CLOUD_BINDING_ENDPOINTS
        if e != "dev_k3_device_info" or device.is_litter)

    results: list[dict[str, Any]] = []
    paired: list[Any] = []
    async with aiohttp.ClientSession(timeout=CLOUD_TIMEOUT) as session:
        for endpoint in endpoints:
            try:
                payload = await fetch_as_device(
                    session, _cloud_upstream(request), device, endpoint)
            except CloudRefused as e:
                # Not fatal on its own: one endpoint refusing must not cost the
                # others. A 404 is not even reported — confirmed against the
                # real cloud on a T5, PetKit answers 404 to
                # `dev_k3_device_info`, so that endpoint is a T4's (its firmware
                # string is `t4/dev_k3_device_info`) and its absence elsewhere
                # is normal rather than a problem somebody should read about.
                if e.status == 404:
                    log.debug("PetKit has no %s for a %s", endpoint, device.device_type)
                else:
                    results.append({"endpoint": endpoint, "outcome": str(e)})
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                return web.json_response(
                    {"error": f"could not reach PetKit: {e}"}, status=502)

            for fields in cloud_bindings(endpoint, payload, device.petkit_id):
                if reg.get(fields["petkit_id"]) is not None:
                    results.append({"petkit_id": fields["petkit_id"],
                                    "outcome": "that id is already a real device"})
                    continue
                dev, outcome = ble.apply_cloud_binding(fields)
                results.append({"petkit_id": fields["petkit_id"],
                                "ble_type": fields.get("ble_type"),
                                "mac": fields.get("mac"), "outcome": outcome})
                if dev is not None and outcome != "unchanged":
                    paired.append(dev)

    for dev in paired:
        await _announce_pairing(request, dev)

    return web.json_response({
        "imported": len(paired),
        "results": results,
        "accessories": [_ble_view(d, reg) for d in ble.all()],
    })


def _ble_entity_value(entity: Any, payload: str) -> int | None:
    """A panel control's payload as the integer an accessory frame carries.

    Same three shapes Home Assistant sends — ON/OFF, a select label, a decimal
    — because `controlRow` in the panel emits exactly what the HA entity would.
    None for anything else: a write to a fountain is not worth guessing at.

    A button carries no value; 0 stands in for one, and the command it builds
    never reads it.
    """
    text = payload.strip()
    if entity.component == "button":
        return 0
    if entity.component == "switch":
        upper = text.upper()
        if upper in ("ON", "1", "TRUE"):
            return 1
        if upper in ("OFF", "0", "FALSE"):
            return 0
        return None
    if entity.component == "select":
        options = list(entity.options or [])
        if text in options:
            values = entity.option_values or list(range(len(options)))
            # Twin of `ha/publisher.py::_ble_command_value` — `option_values`
            # may be strings (the W7H's voice language) and an accessory frame
            # carries a byte, so this coerces rather than casting.
            return to_int(values[options.index(text)], None)
        return None
    return to_int(text, None)


async def api_ble_command(request: web.Request) -> web.Response:
    """Set one entity on a BLE accessory, from the panel.

    The accessory twin of `api_send_command`, and it has to be a twin rather
    than a branch: the delivery rules are different in a way that matters.

    There is no `transport` here. A real device that is off MQTT still has a
    heartbeat queue to hold a command until it polls; an accessory has neither
    — it is reachable only while its parent is on MQTT, because the command is
    a `thing/service/ble` publish to that parent. So the honest answers are
    "sent" or "cannot reach it", and queueing into nothing is not one of them.

    Returns 400 with the reason when the write is refused — both CTW3 frames
    restate every field they carry, so a setting cannot be changed before the
    accessory has reported the rest of them.
    """
    ble = request.app["ble_registry"]
    reg = request.app["registry"]
    bridge = request.app.get("bridge")
    hub = request.app["hub"]

    ble_id = to_int(request.match_info.get("id"), 0) or 0
    dev = ble.get(ble_id)
    if dev is None:
        return web.json_response({"error": "not found"}, status=404)

    body = await _json_body(request)

    entity_key = body.get("entity")
    entity = next((e for e in get_ble_entities(dev.ble_type) if e.key == entity_key), None)
    if entity is None:
        return web.json_response({"error": f"unknown entity {entity_key}"}, status=400)

    value = _ble_entity_value(entity, str(body.get("value", "")))
    if value is None:
        return web.json_response(
            {"error": f"unusable value for {entity.key}"}, status=400)

    parent = reg.get(dev.link_with) if dev.link_with else None
    if parent is None:
        return web.json_response(
            {"error": "no registered parent to relay through"}, status=409)
    if bridge is None or not getattr(bridge, "_client", None):
        return web.json_response({"error": "MQTT bridge is not running"}, status=409)
    if not parent.mqtt_connected:
        return web.json_response(
            {"error": f"{device_display_name(parent.device_type)} is not on MQTT — "
                      f"an accessory can only be reached while its parent is"},
            status=409)

    try:
        cmd, payload = ble_command_for(dev, entity.key, value)
    except Refused as exc:
        return web.json_response({"error": str(exc)}, status=400)

    if not await bridge.publish_ble_command(parent, dev, cmd, payload):
        return web.json_response({"error": "nothing was sent"}, status=502)

    # Optimistic, exactly as the HA path is: the accessory acknowledges the
    # write, but only its next status proves it, and that is a poll away. A
    # button has no state and no `value_path` to file one under.
    if entity.value_path:
        dev.state.setdefault("states", {})[entity.value_path.split(".")[-1]] = value
    ble.mark_dirty()
    hub.record_command(ble_id, "ble", f"{entity.key}={value} (cmd {cmd})")
    return web.json_response({"ok": True, "delivered": "ble", "entity": entity.key,
                              "cmd": cmd, "via": parent.petkit_id})


async def api_ble_poll(request: web.Request) -> web.Response:
    """Ask an accessory's parent to fetch a reading now.

    The one action a BLE accessory has, and it is not the accessory's: nothing
    in the CTW3 protocol is shaped like "do X now" — its four writes are all
    settings. What IS worth a button is the relay itself, because an accessory
    speaks only when its parent is told to open a session, and otherwise that
    happens on a timer up to `interval` seconds away. When the scan type is a
    guess, "has it ever answered" is the only question, and waiting four
    minutes to ask it is not a workflow.
    """
    ble = request.app["ble_registry"]
    reg = request.app["registry"]
    bridge = request.app.get("bridge")
    hub = request.app["hub"]

    dev = ble.get(to_int(request.match_info.get("id"), 0) or 0)
    if dev is None:
        return web.json_response({"error": "not found"}, status=404)
    parent = reg.get(dev.link_with) if dev.link_with else None
    if parent is None:
        return web.json_response(
            {"error": "no registered parent to relay through"}, status=409)
    if bridge is None or not getattr(bridge, "_client", None):
        return web.json_response({"error": "MQTT bridge is not running"}, status=409)
    if not parent.mqtt_connected:
        return web.json_response(
            {"error": f"{device_display_name(parent.device_type)} is not on MQTT — "
                      f"an accessory can only be reached while its parent is"},
            status=409)

    if not await bridge.request_ble_reading(parent, dev):
        return web.json_response({"error": "nothing was sent"}, status=502)
    hub.record_command(dev.petkit_id, "ble", "read now")
    return web.json_response({"ok": True, "via": parent.petkit_id})


async def _unpublish_ble_note(publisher: Any, dev: Any, did: int) -> str:
    """Remove an unpaired accessory from HA, and say which of the outcomes happened."""
    reached = None
    if publisher is not None:
        try:
            reached = await publisher.unpublish_ble(dev)
        except Exception:
            log.warning("Removing BLE %d from Home Assistant failed", did, exc_info=True)
            return "Removing it from Home Assistant failed; delete the device there by hand."
    if reached is None:  # no publisher, or no HA broker configured
        return "Home Assistant publishing is off, so there was nothing to remove there."
    if reached:
        return "Removed from Home Assistant too."
    return ("Home Assistant is not connected right now; the accessory is removed there "
            "as soon as it reconnects.")


async def api_ble_delete(request: web.Request) -> web.Response:
    """Unpair an accessory.

    The parent simply stops being told to scan for it on its next
    `dev_ble_device`; there is no revoke command. Its Home Assistant device is
    removed: an empty retained payload on every discovery config topic
    (`get_ble_entities` for its type, the same list it was published from),
    plus its state and availability. Best effort -- it never fails the unpair;
    with HA's broker down it is queued and done on reconnect, and the `note`
    says which of these happened.
    """
    ble = request.app["ble_registry"]
    if ble is None:
        return web.json_response({"error": "no BLE registry"}, status=503)
    did = _path_id(request)
    dev = ble.get(did)
    was_k3 = dev is not None and dev.ble_type == "k3"
    parent_id = dev.link_with if dev is not None else 0
    if not ble.remove(did):
        return web.json_response({"error": "not found"}, status=404)
    if was_k3 and parent_id:
        await _send_k3_link(request, parent_id, 0)
    elif parent_id:
        # Unpairing is the same event from the parent's side: its list is one
        # shorter, and until it refetches it keeps scanning for a MAC we have
        # stopped serving.
        await _nudge_relay_list(request, parent_id)
    note = await _unpublish_ble_note(request.app.get("ha_publisher"), dev, did)
    return web.json_response({
        "ok": True,
        "accessories": [_ble_view(d, request.app["registry"]) for d in ble.all()],
        "note": note,
    })
