"""Stable per-pet URLs for the last toilet visit and the last drink.

`GET /api/pets/{id}/last-visit|last-drink[/poster]` never serve bytes
themselves: they resolve the recording (`media/pet_media.py`, the same resolver
the HA pet entities use) and redirect to the ordinary `/api/media/...` route,
which already answers Range requests with a 206 — what iOS Safari and the HA
app need to play an mp4. A bookmark or a dashboard tap action can therefore
point at one URL forever and always open the newest playable visit.
"""
from __future__ import annotations

from aiohttp import web

from petkit_local.events import codes
from petkit_local.media.pet_media import PetMedia, media_url_path, resolve_pet_media
from petkit_local.web.api._common import _path_id, _refuse

_KIND = {"last-visit": codes.KIND_TOILET, "last-drink": codes.KIND_DRINKING}
_NO_MEDIA = {"last-visit": "no toilet visit with media", "last-drink": "no drink with media"}

# The target changes with every new visit, so neither a browser nor the HA app
# may cache the redirect itself.
_NO_STORE = {"Cache-Control": "no-store"}


def _under_api(path: str) -> str:
    """`api/media/...` as seen from `/api/`: the climb ends there, not at the
    panel root, so the leading `api/` must not be repeated."""
    return path.removeprefix("api/")


async def _resolve(request: web.Request) -> PetMedia:
    """The recording `{id}`/`{which}` name, or a JSON refusal.

    Raises:
        web.HTTPBadRequest: a non-numeric id, or no event store / pet registry.
        web.HTTPNotFound: an unknown pet, or one with no playable recording.
    """
    pid = _path_id(request)
    which = request.match_info["which"]
    store = request.app.get("event_store")
    if store is None:
        raise _refuse(web.HTTPBadRequest, "event store not available")
    pet_registry = request.app.get("pet_registry")
    if pet_registry is None:
        raise _refuse(web.HTTPBadRequest, "pet registry not available")
    if await pet_registry.get(pid) is None:
        raise _refuse(web.HTTPNotFound, "not found")
    media_root = request.app["cfg"].get("media_root", "")
    res = await resolve_pet_media(store, pid, _KIND[which], media_root)
    if res is None:
        raise _refuse(web.HTTPNotFound, _NO_MEDIA[which])
    return res


async def api_pet_last_media(request: web.Request) -> web.StreamResponse:
    """302 to the newest visit's/drink's recording: Timelapse, else Playback,
    else Clip.

    The Location is RELATIVE (`../../media/...`: `../../` climbs from
    `/api/pets/{id}/` back to `/api/`), so the same redirect works under HA's Ingress prefix and at the
    root of a reverse-proxied panel. The depth is fixed per route rather than
    derived from `request.path`, which carries no Ingress prefix.
    """
    res = await _resolve(request)
    raise web.HTTPFound(location="../../" + _under_api(media_url_path(res.video)),
                        headers=_NO_STORE)


async def api_pet_last_media_poster(request: web.Request) -> web.StreamResponse:
    """302 to that same recording's poster (one level deeper than the video
    route, hence `../../../`). With no still uploaded, the existing ffmpeg frame
    grab of the video stands in."""
    res = await _resolve(request)
    if res.poster:
        target = _under_api(media_url_path(res.poster))
    else:
        target = "media/thumb/" + media_url_path(res.video).removeprefix("api/media/")
    raise web.HTTPFound(location="../../../" + target, headers=_NO_STORE)
