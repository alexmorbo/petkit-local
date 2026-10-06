"""Serving the friendly media tree.

The routes hand out one file or one thumbnail, with every path built from
request input going through `utils/paths.safe_join` and every rejection
answering 404. The role -> slot mapping the Timeline renders from (which of an
episode's uploads is the playable recording, which is the poster, and whether a
chunked video is still assembling) now lives in `media/slots.py`, because the
HA pet entities need the same answer; it is re-exported here under its old
private names for `timeline.py` and the tests. So does the video frame grab
(`media/thumbs.py`, shared with the HA pet images).
"""
from __future__ import annotations

import logging
import os

from aiohttp import web

from petkit_local.media.slots import chunked_video_slot as _pick_chunked_video  # noqa: F401
from petkit_local.media.slots import has_media as _has_media  # noqa: F401
from petkit_local.media.slots import rel_media as _rel_media  # noqa: F401
from petkit_local.media.slots import session_media_urls as _session_media_urls  # noqa: F401
from petkit_local.media.slots import single_slot as _pick_single_video  # noqa: F401
from petkit_local.media.stitch import QUIET_SECONDS as _STITCH_QUIET  # noqa: F401
from petkit_local.media.thumbs import cached_video_thumb
from petkit_local.media.thumbs import generate_video_thumb as _generate_video_thumb  # noqa: F401
from petkit_local.media.transcode import have_ffmpeg
from petkit_local.utils.paths import UnsafePathError, safe_join

log = logging.getLogger(__name__)


def _safe_media_path(request: web.Request, rel_path: str) -> str | None:
    """Resolve `rel_path` under the friendly media root, or None.

    Containment (`..`, absolute paths, symlink escapes) is `safe_join`'s job;
    what stays here is this endpoint's own contract: an unconfigured media root
    serves nothing (`safe_join` would happily resolve against the process CWD),
    the result must be an existing regular file, and a rejection is a 404 rather
    than an exception.
    """
    media_root = request.app["cfg"].get("media_root", "")
    if not media_root or not rel_path:
        return None
    try:
        candidate = safe_join(media_root, rel_path)
    except UnsafePathError:
        return None
    return candidate if os.path.isfile(candidate) else None


async def api_media_file(request: web.Request) -> web.StreamResponse:
    """Serve one file from the friendly media tree.

    The path is relative to the media root — the same form the timeline hands
    out. 404 covers both "missing" and "rejected", so a probe cannot tell the
    two apart.
    """
    p = _safe_media_path(request, request.match_info["path"])
    if not p:
        return web.json_response({"error": "not found"}, status=404)
    return web.FileResponse(p)


async def api_media_thumb(request: web.Request) -> web.StreamResponse:
    """Serve a thumbnail for a media path: the image itself for stills, or a
    cached frame grab for video.

    Grabs are cached under `{data_dir}/thumbs` keyed by a hash of the source
    path, so the Timeline re-rendering a day costs one ffmpeg run per clip
    total, not per request. Every "no thumbnail" outcome answers 404, including
    a failed grab: that is the only status the UI has a fallback for
    (`hide-on-error`), and a 500 leaves a broken tile where the poster still
    would have gone.
    """
    p = _safe_media_path(request, request.match_info["path"])
    if not p:
        return web.json_response({"error": "not found"}, status=404)

    if p.lower().endswith((".jpg", ".jpeg", ".png")):
        return web.FileResponse(p)  # the browser downsizes via CSS — no separate pipeline needed

    if not have_ffmpeg():
        return web.json_response({"error": "ffmpeg not available for video thumbnails"}, status=404)

    thumb_path = await cached_video_thumb(request.app["cfg"].get("data_dir", "/data"), p)
    if thumb_path is None:
        return web.json_response({"error": "thumbnail generation failed"}, status=404)

    return web.FileResponse(thumb_path)
