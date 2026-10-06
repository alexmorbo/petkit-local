"""Cached ffmpeg frame grabs of videos in the friendly media tree.

One cache shared by the panel's `/api/media/thumb/...` route and the HA pet
`image` entities (a visit with no poster still), so the same video is grabbed
once whoever asks first. Lives in `media/` rather than `web/` because `ha` must
not import `web`.
"""
from __future__ import annotations

import hashlib
import logging
import os
import tempfile

from petkit_local.media.transcode import THUMB_TIMEOUT, have_ffmpeg, run_ffmpeg

log = logging.getLogger(__name__)


def thumb_cache_path(data_dir: str, video_path: str) -> str:
    """Where the frame grab of the absolute `video_path` is cached:
    `{data_dir}/thumbs/<sha1 of the path>.jpg`."""
    return os.path.join(data_dir, "thumbs", hashlib.sha1(video_path.encode()).hexdigest() + ".jpg")


async def generate_video_thumb(video_path: str, thumb_path: str) -> bool:
    """Grab one frame from `video_path` into `thumb_path`; True if it worked.

    ffmpeg writes to a temp file in the destination directory which is then
    renamed into place, so `thumb_path` only ever exists complete. The Timeline
    renders a whole day at once, so several requests for the same not-yet-made
    thumbnail arrive together; letting them all write the final path directly
    means one request can serve a half-written JPEG. Renaming is preferred over
    a per-path asyncio.Lock because it needs no shared state to keep correct
    (the runs are equivalent, so last-writer-wins is fine) and it also survives
    the process being killed mid-encode.
    """
    # The temp file must share a filesystem with the target for os.replace to
    # be atomic, so it goes in the same directory.
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(thumb_path) or ".",
                                    prefix=".thumb.", suffix=".jpg")
    os.close(fd)  # ffmpeg opens the path itself; the extension picks the muxer
    try:
        rc, _, _ = await run_ffmpeg(
            ["ffmpeg", "-y", "-i", video_path, "-frames:v", "1", "-vf", "thumbnail", tmp_path],
            timeout=THUMB_TIMEOUT, what=video_path)
        if rc != 0 or not os.path.getsize(tmp_path):
            return False
        os.replace(tmp_path, thumb_path)
        return True
    except OSError as e:
        log.warning("thumbnail generation failed for %s: %s", video_path, e)
        return False
    finally:
        # os.replace consumed the temp file on success; on every other path
        # (including cancellation) it is still there.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def cached_video_thumb(data_dir: str, video_path: str) -> str | None:
    """The cached frame grab of `video_path`, made on first use; None when
    ffmpeg is missing or the grab failed (nothing is cached then)."""
    if not have_ffmpeg():
        return None
    thumb_path = thumb_cache_path(data_dir, video_path)
    if os.path.isfile(thumb_path):
        return thumb_path
    os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
    if not await generate_video_thumb(video_path, thumb_path):
        return None
    return thumb_path
