"""Declarative models for the event/media/pet store (SQLite, `{data_dir}/petkit.db`).

The schema is deliberately denormalized and JSON-loose (`content_json`,
`state_json`): the device payload shape is still being reverse-engineered, so
only the fields we have confirmed get a column and the untouched payload is
kept beside them — the same philosophy `devices/state_parsers.py` applies to
state_report parsing. Recomputing a derived column from the raw JSON later is
then a backfill (`events/migrations.py::backfill_event_rows`), not a data loss.

What the rows MEAN on the wire — `event_uid` versus `related_event` as a
SESSION key, the numeric `dev_event_report` `event_type` codes, the
`moduleType` -> `category` mapping — is documented once, in
`events/normalize.py`'s module docstring. It is the authoritative reference and
is not repeated here.

These models describe a table set that is already deployed, so column names,
types, nullability, defaults and index names are fixed: `create_all` only ever
creates a table that does not exist yet, and a live database is upgraded in
place by `events/store.py::EventStore._migrate`.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import REAL, Index, Integer, Text, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


#: `events.pet_source` for a row attributed by the scale rather than by face.
PET_SOURCE_WEIGHT = "weight"


class Base(DeclarativeBase):
    """Declarative base that can render a row as a plain dict.

    `EventStore` returns dicts rather than ORM instances everywhere: call sites
    index rows with `row["key"]` / `row.get("key")`, and `query_timeline`
    synthesises an extra `"media"` key onto each event row — neither of which
    an entity supports without surprising the reader.
    """

    def as_dict(self) -> dict[str, Any]:
        """Column values only, keyed by column name, in schema order."""
        return {column.name: getattr(self, column.name) for column in self.__table__.columns}


class Event(Base):
    """One report from a device.

    Written from two transports that `events/normalize.py` normalizes to the same
    shape: a `dev_event_report` POST (HTTP, from the device's `cloud` process,
    whose `event_type` is a numeric code string) and an MQTT `thing/event/*`
    message (from `ctrl`, whose `event_type` is a semantic name).

    Three id-ish columns, all distinct:

    * `event_uid` — OUR dedup key, `event_id` + `event_type`. The device's
      `event_id` alone is not unique: several reports of one visit share it.
    * `related_event` — the device's SESSION id, what groups a visit with its
      cleaning sub-events and with the media uploaded for it.
    * `parent_event` — a follow-up report pointing back at the episode it
      closes (e.g. a recognition result closing a pet-appeared episode).
    """

    __tablename__ = "events"
    __table_args__ = (
        # Timeline queries filter by device and window; the session grouping
        # and media pairing both look rows up by related_event.
        Index("idx_events_device_ts", "device_id", "ts"),
        Index("idx_events_related", "related_event"),
        Index("idx_events_ts", "ts"),
        # AUTOINCREMENT rather than a bare rowid alias, matching the deployed
        # schema: ids of deleted rows are never handed out again, so a pruned
        # event cannot be confused with a newer one.
        {"sqlite_autoincrement": True},
    )

    # `nullable=True` on a primary key looks wrong and is not: it makes
    # SQLAlchemy emit the deployed schema's plain `INTEGER PRIMARY KEY
    # AUTOINCREMENT` instead of adding NOT NULL, so a fresh install and a
    # database upgraded in place come out identical. SQLite assigns the value;
    # it is never actually NULL. Same on every table below.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, nullable=True)
    event_uid: Mapped[str | None] = mapped_column(Text)
    related_event: Mapped[str | None] = mapped_column(Text)
    parent_event: Mapped[str | None] = mapped_column(Text)
    device_id: Mapped[int] = mapped_column(Integer, nullable=False)
    device_type: Mapped[str | None] = mapped_column(Text)
    event_type: Mapped[str | None] = mapped_column(Text)
    event_kind: Mapped[str | None] = mapped_column(Text)
    ts: Mapped[float | None] = mapped_column(REAL)
    source: Mapped[str | None] = mapped_column(Text)
    #: The identity the DEVICE reported, verbatim — `content.score_info[].id`
    #: of the best-scoring match. Kept separate from `pet_id` because it is not
    #: necessarily ours: a box still holding faces cached from PetKit's cloud
    #: reports that cloud's id, and writing it into `pet_id` would fabricate an
    #: HA pet device for a row that does not exist. `pet_id` is set only once
    #: `ai/pets.py::PetRegistry.resolve_pet_ref` maps this to a real pet.
    pet_ref: Mapped[int | None] = mapped_column(Integer)
    pet_id: Mapped[int | None] = mapped_column(Integer)
    #: HOW `pet_id` was decided. NULL means identity (a resolved `pet_ref`) or
    #: nothing; `PET_SOURCE_WEIGHT` means `ai/pets.py` picked the pet whose
    #: reference weight is nearest the visit's. Only weight-sourced rows are
    #: ever re-attributed when a reference changes -- an identity is never
    #: overwritten by a guess from the scale.
    pet_source: Mapped[str | None] = mapped_column(Text)
    score: Mapped[float | None] = mapped_column(REAL)
    content_json: Mapped[str | None] = mapped_column(Text)
    state_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)


class Media(Base):
    """One file the device uploaded, from `dev_upload_file_info_v2`.

    A row is written twice: once when the metadata arrives (encrypted, no
    `media_path`, `status='pending'`) and again once `media/pipeline.py` has
    decrypted, remuxed and filed it (`status='ready'`). `file_id` is the
    device's own id for the upload and is what makes that second write an
    update rather than a duplicate.

    `category` is derived from `module_type`, never stored as sent — see
    `events/normalize.py::_MODULE_TYPE_TO_CATEGORY`. It matters more than it
    looks: video arrives as many ~4s chunks sharing one `related_event`, and
    two different streams (the main recording and its low-res timelapse) cover
    the same span, so `media/stitch.py` keys an episode on category as well.
    `stitch_state` is NULL for a chunk nothing has done anything with,
    `'stitched'` on the merged row, and a reason string on chunks a failed or
    skipped join must not be retried on forever.
    """

    __tablename__ = "media"
    __table_args__ = (
        Index("idx_media_related", "related_event"),
        Index("idx_media_device_created", "device_id", "created_at"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, nullable=True)
    file_id: Mapped[str | None] = mapped_column(Text, unique=True)
    device_id: Mapped[int] = mapped_column(Integer, nullable=False)
    related_event: Mapped[str | None] = mapped_column(Text)
    module_type: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    file_type: Mapped[str | None] = mapped_column(Text)
    media_path: Mapped[str | None] = mapped_column(Text)
    encrypted: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    aes_iv: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    start_ts: Mapped[float | None] = mapped_column(REAL)
    end_ts: Mapped[float | None] = mapped_column(REAL)
    pet_score: Mapped[float | None] = mapped_column(REAL)
    pet_event: Mapped[str | None] = mapped_column(Text)
    pet_id: Mapped[int | None] = mapped_column(Integer)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(Text, server_default=text("'pending'"))
    stitch_state: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)


class BlockedAttempt(Base):
    """Something the real cloud tried to do to a device, and did not get to do.

    Written only in proxy mode, by the redaction layer's blocking rules
    (`http/redact/rules.py::BLOCKING_RULES`): a shell command, a firmware push, or an
    attempt to re-credential the device. Deliberately NOT the routine address
    substitutions: the device re-polls `dev_serverinfo`, `dev_device_info` and
    its STS block on their own timers, so recording each `apiServers`,
    `mqttHost` or timezone rewrite would file entirely expected traffic
    alongside the handful of rows that matter, which is what this table is for.
    Those go to the panel's live log and the capture files instead.

    It is a table of its own rather than a new `event_kind` on `events` because
    `events` is the pet timeline: every row there feeds `query_timeline` and
    `group_sessions` and is pruned as pet history, so a blocked shell command
    would surface between a toilet visit and a cleaning cycle.

    `payload_json` holds what was blocked, verbatim and unmasked — it is the
    whole point of the record. Anything serving these outside `/data` is
    responsible for masking it (`web/api/settings.py`'s `/api/blocked`), because they
    can contain a real device secret or a media AES key.
    """

    __tablename__ = "blocked_attempts"
    __table_args__ = (
        # The panel reads "most recent N", optionally for one device.
        Index("idx_blocked_created", "created_at"),
        Index("idx_blocked_device_created", "device_id", "created_at"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, nullable=True)
    # Nullable, unlike `Event.device_id`, though nothing writes NULL today: the
    # HTTP path never forwards an unidentified request (there would be no
    # credentials to substitute into the reply) and the MQTT path is per-device
    # by construction. Left open so a future source that cannot attribute a
    # blocked attempt records it rather than dropping it.
    device_id: Mapped[int | None] = mapped_column(Integer)
    #: One of `http/redact/`'s blocking rule names ("rce", "ota", "secret").
    kind: Mapped[str | None] = mapped_column(Text)
    #: "http" or "mqtt" — which side of the proxy caught it.
    transport: Mapped[str | None] = mapped_column(Text)
    #: The request path, or the MQTT topic.
    endpoint: Mapped[str | None] = mapped_column(Text)
    #: Which upstream the answer came from, so a record stays readable after
    #: the panel's server selection is changed.
    upstream: Mapped[str | None] = mapped_column(Text)
    #: Where in the decoded body the rule matched.
    field_path: Mapped[str | None] = mapped_column(Text)
    payload_json: Mapped[str | None] = mapped_column(Text)
    detail_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)


class Pet(Base):
    """A pet the user configured, and the source of every per-pet HA entity.

    `id` is handed to the device as the `id` of a `dev_discern_pic` list entry
    and comes back verbatim as `content.score_info[].id` once the device's own
    NPU recognises that pet — confirmed in the firmware, which builds that
    field from the outer list id (`get_update_face_score_info`). So this
    primary key doubles as the device-side identity.

    `device_ids_json` is a JSON array because a pet can use several litter
    boxes and nothing queries across it. `device_pet_ids_json` is a second,
    unrelated array: identities some device already reports that are NOT ours
    — a box still carrying faces cached from PetKit's cloud keeps sending that
    cloud's pet id. Binding one here is a deliberate user action, never a guess.

    `face_id`/`photo_path` are LEGACY, from when a pet had exactly one photo.
    `PetFace` replaced them; `EventStore.connect` migrates any surviving value
    across and nothing reads these two afterwards.
    """

    __tablename__ = "pets"
    __table_args__ = ({"sqlite_autoincrement": True},)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    device_ids_json: Mapped[str | None] = mapped_column(Text, server_default=text("'[]'"))
    device_pet_ids_json: Mapped[str | None] = mapped_column(Text, server_default=text("'[]'"))
    face_id: Mapped[str | None] = mapped_column(Text)
    photo_path: Mapped[str | None] = mapped_column(Text)
    weight: Mapped[float | None] = mapped_column(REAL)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)


class PetFace(Base):
    """One reference face photo the device's NPU matches against.

    Several per pet — the real cloud served four for a single cat, and more
    angles is the main lever on whether recognition succeeds at all.

    `id` IS the identity handed to the device as `discern[].id`, and it comes
    back in the state report's `discernPic` array listing the photos the device
    has downloaded and feature-extracted (features live on in
    `/system/feature.bin` there, the JPEG only passes through `/tmp`). That
    readback is the one signal that proves a payload was accepted, which is why
    the table is `sqlite_autoincrement`: a reused id after a delete would look
    to the device like a photo it already holds, and it would never re-fetch.

    Confirmed worse than that, from a T5's own log: the fetch is
    `wget -O /tmp/pet_face_pic.jpg -c <url>`, and `-c` RESUMES a partial
    download. A recycled id means a URL whose bytes changed under a name the
    device may already hold a prefix of, which splices two images together
    rather than replacing one. Never reusing an id is what makes that
    impossible.
    """

    __tablename__ = "pet_faces"
    __table_args__ = (
        Index("idx_pet_faces_pet", "pet_id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, nullable=True)
    pet_id: Mapped[int] = mapped_column(Integer, nullable=False)
    photo_path: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
