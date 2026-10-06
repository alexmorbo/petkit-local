"""Episode stitcher — concatenate a visit's video chunks into one continuous
clip.

A real T5 does NOT record one file per visit: it uploads a continuous stream
of small ~4s chunks (plus a separate low-res substream), all sharing one
`related_event`. Confirmed on-device 2026-07-22: a single toilet visit
produced 20+ separate `.ts` files. Playing "the visit" therefore means
concatenating its chunks in order.

Chunks of one category come from the same encoder with identical parameters
(verified: every `CLOUD_STORAGE` chunk is h264 1056x1056 @25fps + AAC), so
ffmpeg's concat demuxer with `-c copy` joins them losslessly and fast — no
re-encode. Chunks of DIFFERENT categories must never be mixed (the substream
is 528x528 @20fps with no audio), which is why the episode key includes the
category.

Because a successful stitch **deletes the source chunks**, the output is
verified before anything is removed: probed for a real video stream and a
plausible duration, then fully decoded (`-xerror`) to prove no frame is
corrupt. A failed verification leaves the chunks untouched and marks the
episode so it isn't retried forever.

Blocking-I/O rule for this module: everything here runs as a background task
on the single event loop shared with the HTTP server and the MQTT bridge, so
work whose cost scales with the number of chunks — stat'ing an episode's
chunk list, writing the concat list, unlinking the sources — is handed to
`asyncio.to_thread` in ONE batch per step, never one hop per file. Fixed-cost
single syscalls (one `unlink`, one `mkdir -p`) stay inline: the hop would cost
more than the call.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import time
from typing import TYPE_CHECKING, Awaitable, Callable, Iterable

from petkit_local.media import layout, transcode
from petkit_local.media.transcode import STITCH_TIMEOUT, run_ffmpeg
from petkit_local.utils.coerce import to_int
from petkit_local.utils.jsonio import atomic_write_text
from petkit_local.utils.paths import sanitize_filename

if TYPE_CHECKING:
    from petkit_local.devices.registry import DeviceRegistry
    from petkit_local.events.store import EventStore
    from petkit_local.web.hub import EventHub

log = logging.getLogger(__name__)

# Categories worth concatenating (photo categories obviously aren't).
STITCH_CATEGORIES = ("fullVideo", "dynamicVideo", "highLight", "cloudDouble")

# How long an episode must be quiet before it's considered finished. The
# device uploads a chunk every ~4s during a visit and keeps going through
# the cleaning cycle that follows, so this needs to comfortably exceed the
# gap between chunks, not just the visit length. Kept well above that gap but
# low enough that the media tree converges within a couple of minutes —
# until it fires, the folder legitimately shows the individual chunks.
QUIET_SECONDS = 90.0

# A stitched clip should be roughly the sum of its parts; a much shorter
# result means ffmpeg silently dropped input, so treat it as a failure.
MIN_DURATION_RATIO = 0.8

# Characters that cannot survive in a concat-list entry no matter how it is
# quoted, because they terminate the unit the parser works in: the demuxer
# reads the file line by line before it ever looks at quotes, and NUL ends the
# path as far as every syscall below is concerned. Quotes and backslashes are
# NOT here — those are escapable, and are escaped in `_concat_list_entry`.
_CONCAT_FATAL_CHARS = re.compile(r"[\x00\r\n]")


class UnsafeConcatPath(ValueError):
    """A media path could not be represented safely in an ffmpeg concat list."""


def _concat_list_entry(path: str) -> str:
    """Render one `file '<path>'` line for ffmpeg's concat demuxer.

    Deliberately independent of `media/layout.py`'s sanitizer: paths reach the
    stitcher from the media table, which has rows written by older builds and
    by any other producer, so the writer must not assume its input was ever
    sanitized.

    Escaping follows ffmpeg's own tokenizer (`av_get_token`): inside single
    quotes every byte is literal, so a literal `'` is written by closing the
    quote, emitting a backslash-escaped quote and reopening — the classic
    `'\\''` sequence. Backslashes need nothing, being literal inside quotes.

    Raises:
        UnsafeConcatPath: The path contains a byte that cannot be escaped and
            would let the rest of the name be parsed as its own directive.
    """
    if _CONCAT_FATAL_CHARS.search(path):
        raise UnsafeConcatPath(f"path contains a line terminator or NUL: {path!r}")
    return "file '%s'\n" % path.replace("'", "'\\''")


def _write_concat_list(list_path: str, paths: Iterable[str]) -> None:
    """Write the concat list atomically (blocking — call via a thread)."""
    atomic_write_text(list_path, "".join(_concat_list_entry(p) for p in paths))


def _expected_duration_sec(chunks: list[dict]) -> float | None:
    """How long the joined clip should be, or None if no chunk reported a
    duration (in which case `verify_video` skips the length check rather than
    comparing against a bogus zero).

    Each `duration_ms` is coerced individually and a bad one is skipped, not
    raised on: this number only gates a verification, so losing one chunk's
    contribution is far better than aborting a join that is otherwise fine.
    """
    total_ms = 0
    seen = False
    for c in chunks:
        d = c.get("duration_ms")
        if d:
            try:
                total_ms += float(d)
                seen = True
            except (TypeError, ValueError):
                continue
    return total_ms / 1000.0 if seen else None


async def stream_key(path: str) -> str | None:
    """A signature of the file's video stream (`WxH/codec`). Chunks must
    agree on this to be joinable with `-c copy`; ffmpeg silently stops at the
    first mismatch, producing a short file rather than an error."""
    info = await transcode.probe(path)
    for s in (info.get("streams") or []):
        if s.get("codec_type") == "video":
            return f"{s.get('width')}x{s.get('height')}/{s.get('codec_name')}"
    return None


async def split_by_stream(paths: list[str]) -> tuple[list[str], list[str]]:
    """Split chunks into the majority stream shape and everything else.

    Real-world data isn't always uniform: a single foreign chunk among 26
    good ones (seen on a live device, where the low-res substream had
    overwritten a main-stream file of the same name) is enough to truncate
    the whole join. Rather than discard the episode,
    concatenate the dominant stream and leave the odd ones out — reported by
    the caller, never deleted."""
    by_key: dict[str, list[str]] = {}
    unreadable: list[str] = []
    for p in paths:
        key = await stream_key(p)
        if key is None:
            unreadable.append(p)
        else:
            by_key.setdefault(key, []).append(p)

    if not by_key:
        return [], list(paths)
    best = max(by_key.values(), key=len)
    best_set = set(best)
    outliers = [p for p in paths if p not in best_set]
    return [p for p in paths if p in best_set], outliers


async def is_decodable(path: str) -> bool:
    """Whether every frame of `path` decodes. Used only on the retry path —
    it costs a full decode per chunk, so it isn't run unless a join already
    came out wrong."""
    if not shutil.which("ffmpeg"):
        return True
    rc, _, _ = await run_ffmpeg(
        ["ffmpeg", "-v", "error", "-xerror", "-i", path, "-f", "null", "-"],
        timeout=STITCH_TIMEOUT, what=path)
    if rc == -1:
        return True  # can't check — don't block on a missing binary
    return rc == 0


async def concat_videos(paths: list[str], dst: str, work_dir: str) -> bool:
    """Losslessly join `paths` (in order) into `dst`. Returns False rather
    than raising — the caller keeps the chunks when this fails."""
    if not transcode.have_ffmpeg():
        log.warning("ffmpeg not installed - cannot stitch %d chunks", len(paths))
        return False

    # The list file must live somewhere the ffmpeg process can read. Its name
    # is derived from `dst` rather than from the clock so that a run killed
    # mid-concat leaves a file the next run for the same episode overwrites,
    # instead of an orphan nobody can attribute.
    list_path = os.path.join(
        work_dir, f"concat_{sanitize_filename(os.path.splitext(os.path.basename(dst))[0])}.txt")
    try:
        # Writing N quoted lines is real blocking I/O and must not run on the
        # event loop; `atomic_write_text` also creates the work dir.
        await asyncio.to_thread(_write_concat_list, list_path, paths)
    except UnsafeConcatPath as e:
        log.error("Refusing to stitch: %s", e)
        return False
    except OSError as e:
        log.warning("Could not write concat list %s: %s", list_path, e)
        return False

    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        rc, _, stderr = await run_ffmpeg(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
             "-c", "copy", "-movflags", "+faststart", dst],
            timeout=STITCH_TIMEOUT, what=dst)
    except OSError as e:
        log.warning("ffmpeg concat could not start: %s", e)
        return False
    finally:
        # A single unlink — not worth a thread hop; see the module's I/O rule.
        try:
            os.remove(list_path)
        except OSError:
            pass

    if rc != 0:
        log.warning("ffmpeg concat failed: %s", stderr.decode(errors="replace")[-500:])
        return False
    return True


async def verify_video(path: str, expected_duration_sec: float | None = None) -> bool:
    """Prove a stitched file is actually usable before its sources are
    deleted: container has a video stream, duration is plausible, and every
    frame decodes without error."""
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        log.warning("Stitch verify: %s missing or empty", path)
        return False

    info = await transcode.probe(path)
    streams = info.get("streams") or []
    if not any(s.get("codec_type") == "video" for s in streams):
        log.warning("Stitch verify: %s has no video stream", path)
        return False

    duration = None
    try:
        duration = float((info.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        duration = None
    if duration is None or duration <= 0:
        log.warning("Stitch verify: %s has no usable duration", path)
        return False
    if expected_duration_sec and duration < expected_duration_sec * MIN_DURATION_RATIO:
        log.warning("Stitch verify: %s is %.1fs, expected ~%.1fs - input was dropped",
                    path, duration, expected_duration_sec)
        return False

    # Strongest check: decode everything. `-xerror` makes ffmpeg exit non-zero
    # on the first decode error instead of logging and carrying on.
    if not shutil.which("ffmpeg"):
        return True
    rc, _, stderr = await run_ffmpeg(
        ["ffmpeg", "-v", "error", "-xerror", "-i", path, "-f", "null", "-"],
        timeout=STITCH_TIMEOUT, what=path)
    if rc == -1:
        return True  # can't check — don't block on a missing binary
    if rc != 0:
        log.warning("Stitch verify: %s failed full decode: %s",
                    path, stderr.decode(errors="replace")[-300:])
        return False
    return True


def _existing_chunk_paths(chunks: list[dict]) -> list[str]:
    """The chunks that are still on disk, in order (blocking — one stat each)."""
    return [c["media_path"] for c in chunks
            if c.get("media_path") and os.path.isfile(c["media_path"])]


def _remove_files(paths: Iterable[str]) -> None:
    """Unlink each path, logging but not raising (blocking — call via a thread)."""
    for p in paths:
        try:
            os.remove(p)
        except OSError as e:
            log.warning("Stitch: could not remove chunk %s: %s", p, e)


def _move_into_place(tmp_path: str, final_path: str) -> int:
    """Move the verified clip to its final name and return its size in bytes.

    The final directory may not exist yet: `concat_videos` only created the
    work dir, and the merged file's date folder derives from the episode's
    first chunk, which can differ from where the chunks themselves landed.

    Raises:
        OSError: the directory could not be created or the rename failed.
    """
    os.makedirs(os.path.dirname(final_path), exist_ok=True)
    os.replace(tmp_path, final_path)
    return os.path.getsize(final_path)


def _episode_slug(episode: dict) -> str:
    """A stable short id for an episode's work-dir temp file.

    Derived from the episode key (related_event + category) and NOT from
    `hash()`, whose string seed is randomized per process: a temp file left by
    a crashed run would then be unrecognisable to the next one and linger in
    the work dir forever. Hashed rather than used verbatim because
    `related_event` is a device-supplied string.
    """
    key = f"{episode.get('related_event')}\x00{episode.get('category')}"
    return hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:16]


async def _merged_path(store: EventStore, episode: dict, media_root: str,
                       device_type: str) -> str:
    """Where the joined clip belongs in the friendly tree.

    Named from the episode's FIRST chunk, so the stitched file lands at the
    moment the recording started and usually replaces that chunk in place —
    which is also why `stitch_episode` builds into a work dir first (ffmpeg
    would otherwise read a file it is writing).
    """
    chunks = episode["chunks"]
    first = chunks[0]
    event = await store.event_by_related(episode["related_event"])
    label = layout.media_filename_label(None, event.get("event_kind") if event else None)
    return layout.build_media_path(
        media_root, device_type, episode["device_id"], episode["category"],
        first.get("start_ts") or first.get("created_at"), label, "mp4",
    )


async def stitch_episode(store: EventStore, episode: dict, media_root: str, work_dir: str,
                         device_type: str = "") -> str | None:
    """Concatenate one episode's chunks. On success the chunk rows+files are
    replaced by the single stitched file; on failure everything is left
    exactly as it was (and the episode is marked so it isn't retried).

    `episode` is one `EventStore.stitch_candidates` row::

        {device_id, related_event, category, chunks: [<media row>, ...]}

    with `chunks` already in play order. Returns the joined file's path, or
    None if nothing was written — every failure path is non-destructive, so
    None always means "the chunks are still there".
    """
    chunks = episode["chunks"]
    # One thread hop for the whole chunk list: a real visit produces 25+.
    paths = await asyncio.to_thread(_existing_chunk_paths, chunks)
    if len(paths) < 2:
        log.info("Stitch: episode %s/%s has <2 readable chunks on disk, skipping",
                 episode["related_event"], episode["category"])
        await store.mark_stitch_failed([c["id"] for c in chunks], "missing-files")
        return None

    paths, outliers = await split_by_stream(paths)
    if outliers:
        log.warning("Stitch: episode %s/%s - %d of %d chunks don't match the dominant "
                    "video stream and are being left out (kept on disk): %s",
                    episode["related_event"], episode["category"], len(outliers),
                    len(outliers) + len(paths), ", ".join(os.path.basename(p) for p in outliers[:5]))
    if len(paths) < 2:
        log.info("Stitch: episode %s/%s has <2 consistent chunks, skipping",
                 episode["related_event"], episode["category"])
        await store.mark_stitch_failed([c["id"] for c in chunks], "inconsistent-streams")
        return None

    # Only the chunks actually going into the join may be replaced/removed;
    # excluded ones keep their row and file so nothing is silently lost.
    included = {p for p in paths}
    chunks = [c for c in chunks if c.get("media_path") in included]
    excluded_ids = [c["id"] for c in episode["chunks"] if c.get("media_path") not in included]

    final_path = await _merged_path(store, episode, media_root, device_type)
    # Build in the hidden work dir, not next to the final file: the final
    # name usually collides with one of the inputs (both derive from the
    # first chunk's timestamp) and ffmpeg reading a file it's writing would
    # corrupt the output — and a half-written file must never show up in the
    # HA media browser. Same filesystem, so the move at the end is a rename.
    os.makedirs(work_dir, exist_ok=True)
    tmp_path = os.path.join(
        work_dir,
        f"stitching_{sanitize_filename(str(episode['category']))}_{_episode_slug(episode)}.mp4")

    ok = await concat_videos(paths, tmp_path, work_dir)
    if ok:
        ok = await verify_video(tmp_path, _expected_duration_sec(chunks))

    if not ok and len(paths) > 2:
        # A chunk can be individually well-formed enough to probe yet still
        # break the join (seen on a real device: a file containing two moov
        # atoms and invalid NAL sizes, left behind by two writers racing on
        # one filename). Find the unreadable ones the expensive way, once,
        # and retry without them rather than losing the whole episode.
        log.info("Stitch: episode %s/%s came out wrong - checking chunks individually",
                 episode["related_event"], episode["category"])
        good = [p for p in paths if await is_decodable(p)]
        broken = [p for p in paths if p not in set(good)]
        if broken and len(good) >= 2:
            log.warning("Stitch: episode %s/%s - dropping %d undecodable chunk(s): %s",
                        episode["related_event"], episode["category"], len(broken),
                        ", ".join(os.path.basename(p) for p in broken[:5]))
            chunks = [c for c in chunks if c.get("media_path") in set(good)]
            excluded_ids += [c["id"] for c in episode["chunks"]
                             if c.get("media_path") in set(broken)]
            paths = good
            ok = await concat_videos(paths, tmp_path, work_dir)
            if ok:
                ok = await verify_video(tmp_path, _expected_duration_sec(chunks))

    if not ok:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        await store.mark_stitch_failed([c["id"] for c in chunks], "failed")
        log.warning("Stitch failed for episode %s/%s - keeping %d original chunks",
                    episode["related_event"], episode["category"], len(chunks))
        return None

    # Verified good: now it's safe to drop the sources. One thread hop for all
    # of them — an episode can carry a hundred chunks. `final_path` is skipped
    # because the rename below is about to overwrite it anyway.
    await asyncio.to_thread(_remove_files, [p for p in paths if p != final_path])
    try:
        size = await asyncio.to_thread(_move_into_place, tmp_path, final_path)
    except OSError as e:
        log.error("Stitch: could not move %s into place: %s", tmp_path, e)
        await store.mark_stitch_failed([c["id"] for c in chunks], "move-failed")
        return None

    first, last = chunks[0], chunks[-1]
    merged_row = {
        "file_id": f"stitched:{episode['related_event']}:{episode['category']}",
        "device_id": episode["device_id"],
        "related_event": episode["related_event"],
        "module_type": first.get("module_type"),
        "category": episode["category"],
        "file_type": first.get("file_type"),
        "media_path": final_path,
        "encrypted": 0,
        # `to_int`, not `int()`: this row is built AFTER the source chunks have
        # been deleted, so a raw device-supplied duration ("4000ms", a row
        # written before ingest coerced it) raising here would lose the whole
        # episode — the joined file would exist with no media row pointing at
        # it. `_expected_duration_sec` guards the same field for the same
        # reason; this one was missed.
        "duration_ms": sum(to_int(c.get("duration_ms"), 0) for c in chunks) or None,
        "start_ts": first.get("start_ts"),
        "end_ts": last.get("end_ts"),
        "size_bytes": size,
        "status": "ready",
        "stitch_state": "stitched",
    }
    await store.replace_chunks_with_stitched([c["id"] for c in chunks], merged_row)
    if excluded_ids:
        # Keep them visible and playable, but flagged so the sweeper doesn't
        # keep reconsidering them every pass.
        await store.mark_stitch_failed(excluded_ids, "excluded-stream-mismatch")
    log.info("Stitched %d chunks -> %s (%.1f MB)", len(chunks), final_path, size / 1048576)
    return final_path


class EpisodeStitcher:
    """Background sweeper that joins finished episodes' chunks.

    An episode is only picked up once it has been quiet for `quiet_seconds`,
    because the device keeps uploading chunks throughout a visit AND the
    cleaning cycle that follows: stitching early would produce a clip that is
    missing its tail and delete the sources that could have completed it.
    Until then the media tree legitimately shows the individual chunks.
    """

    def __init__(self, store: EventStore, registry: DeviceRegistry | None,
                 media_root: str, work_dir: str,
                 interval: float = 60.0, quiet_seconds: float = QUIET_SECONDS,
                 hub: EventHub | None = None,
                 on_stitched: Callable[[dict], Awaitable[None]] | None = None) -> None:
        """`interval` and `quiet_seconds` are both in seconds and independent:
        the first is how often a pass runs, the second how long an episode
        must have been idle before that pass will touch it.

        `registry` is only consulted for a device's `device_type` (which names
        its folder in the friendly tree), so None degrades to an empty type
        rather than skipping the episode.

        `on_stitched` is awaited with the episode after each successful join —
        how HA's pet entities (`HAPublisher.on_episode_stitched`) learn a visit
        became playable without this module importing `ha`.
        """
        self._store = store
        self._registry = registry
        self._media_root = media_root
        self._work_dir = work_dir
        self._interval = interval
        self._quiet = quiet_seconds
        self._hub = hub
        self._on_stitched = on_stitched

    async def run_once(self) -> int:
        """Stitch every episode that has gone quiet; returns how many joined.

        One failing episode never stops the pass — the exception is logged and
        the remaining candidates are still attempted, since a single
        undecodable episode would otherwise block the media tree from ever
        converging.
        """
        episodes = await self._store.stitch_candidates(
            STITCH_CATEGORIES, time.time() - self._quiet)
        done = 0
        for ep in episodes:
            device = self._registry.get(ep["device_id"]) if self._registry else None
            device_type = device.device_type if device else ""
            try:
                path = await stitch_episode(self._store, ep, self._media_root,
                                            self._work_dir, device_type)
                if path:
                    done += 1
                    # Tell the panel a video finished assembling, so an open
                    # Timeline flips its "processing" placeholder to the real
                    # clip (see web/panel.py's scheduleTimeline on "media").
                    if self._hub is not None:
                        self._hub.publish("media", ep.get("device_id"),
                                          f"video ready: {os.path.basename(path)}",
                                          detail={"stitched": True, "category": ep.get("category")})
                    if self._on_stitched is not None:
                        await self._notify_stitched(ep)
            except Exception:
                log.exception("Stitching episode %s/%s blew up",
                              ep.get("related_event"), ep.get("category"))
        return done

    async def _notify_stitched(self, ep: dict) -> None:
        """Await `on_stitched`, logging its failure as its own: the join itself
        succeeded and must not be reported as a failed stitch."""
        try:
            await self._on_stitched(ep)
        except Exception:
            log.exception("Episode %s/%s stitched, but the on_stitched callback failed",
                          ep.get("related_event"), ep.get("category"))

    async def run(self) -> None:
        """Sweep every `interval` seconds until cancelled.

        Sleeps FIRST: at start-up the event store may still be opening and the
        freshly-restored episodes haven't had time to go quiet, so an
        immediate pass would only ever be wasted work.
        """
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.run_once()
            except Exception:
                log.exception("Stitch sweep failed")
