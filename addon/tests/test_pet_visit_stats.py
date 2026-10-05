from petkit_local.utils.timeutil import local_day_bounds


async def test_stats_empty_for_pet_with_no_visits(event_store):
    store = event_store
    stats = await store.pet_visit_stats(1, now=1000.0)
    assert stats == {
        "last_visit_ts": None, "visits_today": 0,
        "last_visit_weight": None, "last_visit_duration": None,
        "last_device_id": None,
        "weight": None, "last_drink_ts": None, "drinks_today": 0,
    }


async def test_stats_pick_latest_visit_and_weight(event_store):
    store = event_store
    await store.upsert_event({"device_id": 5, "event_type": "pet_out", "event_kind": "toilet_visit",
                              "pet_id": 1, "ts": 100.0, "content_json": '{"pet_weight": 2200}'})
    await store.upsert_event({"device_id": 5, "event_type": "pet_out", "event_kind": "toilet_visit",
                              "pet_id": 1, "ts": 200.0, "content_json": '{"pet_weight": 2300}'})
    stats = await store.pet_visit_stats(1, now=300.0)
    assert stats["last_visit_ts"] == 200.0
    assert stats["last_visit_weight"] == 2300.0
    assert stats["last_device_id"] == 5


async def test_stats_computes_duration_from_paired_pet_in(event_store):
    store = event_store
    await store.upsert_event({"device_id": 5, "event_type": "pet_in", "event_kind": "toilet_visit",
                              "related_event": "r1", "ts": 100.0})
    await store.upsert_event({"device_id": 5, "event_type": "pet_out", "event_kind": "toilet_visit",
                              "pet_id": 1, "related_event": "r1", "ts": 156.0})
    stats = await store.pet_visit_stats(1, now=200.0)
    assert stats["last_visit_duration"] == 56.0


async def test_a_visit_is_reported_in_whole_seconds_and_whole_grams(event_store):
    """Both stamps are report ARRIVAL times, so the sub-second part of their
    difference is transport latency rather than time the cat spent in the box —
    `codes.py` measures a 3.8s median for `ts - start_time` on this kind of
    pair. Subtracting them raw published "57.317591428756714 s" to Home
    Assistant: fifteen digits of precision the inputs cannot support.

    The weight beside it had the same shape in miniature. All 58 `pet_weight`
    values in the captures are integers, so `float()` was inventing the `.0`.
    """
    store = event_store
    await store.upsert_event({"device_id": 5, "event_type": "pet_in", "event_kind": "toilet_visit",
                              "related_event": "r1", "ts": 1000.0})
    await store.upsert_event({"device_id": 5, "event_type": "pet_out", "event_kind": "toilet_visit",
                              "pet_id": 1, "related_event": "r1", "ts": 1057.317591428756714,
                              "content_json": '{"pet_weight": 2223}'})
    stats = await store.pet_visit_stats(1, now=2000.0)

    assert stats["last_visit_duration"] == 57
    assert isinstance(stats["last_visit_duration"], int)
    assert stats["last_visit_weight"] == 2223
    assert isinstance(stats["last_visit_weight"], int)


async def test_stats_visits_today_counts_only_same_local_day(event_store):
    """The boundary is LOCAL midnight, not UTC.

    Timestamps are derived from `local_day_bounds` rather than written as
    multiples of 86400, so this passes in any developer's timezone. Hardcoding
    them assumed a UTC boundary, and on a UTC+2 machine the "previous day"
    event landed inside the same local day and the count came out at 3.
    """
    store = event_store
    now = 86400.0 * 100 + 43200  # midday of an arbitrary day
    day_start, _, _ = local_day_bounds(now=now)
    for ts in (day_start + 10, day_start + 20, day_start - 100):
        await store.upsert_event({"device_id": 5, "event_type": "pet_out",
                                  "event_kind": "toilet_visit", "pet_id": 1, "ts": ts})
    stats = await store.pet_visit_stats(1, now=now)
    assert stats["visits_today"] == 2


async def test_stats_ignore_other_pets_and_non_visit_events(event_store):
    store = event_store
    await store.upsert_event({"device_id": 5, "event_type": "pet_out", "event_kind": "toilet_visit",
                              "pet_id": 2, "ts": 100.0})  # different pet
    await store.upsert_event({"device_id": 5, "event_type": "clean_over", "event_kind": "cleaning",
                              "pet_id": 1, "ts": 150.0})  # not a toilet_visit
    stats = await store.pet_visit_stats(1, now=200.0)
    assert stats["last_visit_ts"] is None
    assert stats["visits_today"] == 0


async def test_weight_is_the_median_of_the_newest_seven_summaries(event_store):
    """`pet_in` carries a PARTIAL weight (the cat is still stepping in), so it
    must never reach the median however recent it is; and only the newest
    seven summaries count, so an old outlier ages out."""
    store = event_store
    weights = [9000, 4000, 4100, 4200, 4300, 4400, 4500, 4600]  # oldest first
    for i, w in enumerate(weights):
        await store.upsert_event({"device_id": 5, "device_type": "t6", "event_type": "pet_out",
                                  "event_kind": "toilet_visit", "pet_id": 1, "ts": 100.0 + i,
                                  "content_json": f'{{"pet_weight": {w}}}'})
    await store.upsert_event({"device_id": 5, "device_type": "t6", "event_type": "pet_in",
                              "event_kind": "toilet_visit", "pet_id": 1, "ts": 500.0,
                              "content_json": '{"pet_weight": 1}'})
    stats = await store.pet_visit_stats(1, now=1000.0)
    # newest seven: 4000..4600 -> median 4300; the 9000 has aged out.
    assert stats["weight"] == 4300
    assert isinstance(stats["weight"], int)


async def test_weight_is_none_without_weighed_visits(event_store):
    store = event_store
    await store.upsert_event({"device_id": 5, "device_type": "t6", "event_type": "pet_out",
                              "event_kind": "toilet_visit", "pet_id": 1, "ts": 100.0})
    await store.upsert_event({"device_id": 5, "device_type": "t6", "event_type": "pet_out",
                              "event_kind": "toilet_visit", "pet_id": 1, "ts": 110.0,
                              "content_json": '{"pet_weight": 0}'})
    stats = await store.pet_visit_stats(1, now=1000.0)
    assert stats["weight"] is None


async def test_drinks_today_counts_from_local_midnight(event_store):
    store = event_store
    now = 86400.0 * 100 + 43200
    day_start, _, _ = local_day_bounds(now=now)
    for ts in (day_start + 10, day_start + 20, day_start - 100):
        await store.upsert_event({"device_id": 7, "device_type": "w7h",
                                  "event_type": "drink_over", "event_kind": "drinking",
                                  "pet_id": 1, "ts": ts})
    stats = await store.pet_visit_stats(1, now=now)
    assert stats["drinks_today"] == 2
    assert stats["last_drink_ts"] == day_start + 20


async def test_drinks_ignore_other_pets_unattributed_and_starts(event_store):
    store = event_store
    now = 86400.0 * 100 + 43200
    day_start, _, _ = local_day_bounds(now=now)
    rows = [
        ("drink_over", 2, day_start + 10),     # another pet
        ("drink_over", None, day_start + 20),  # recognised nobody
        ("drink_start", 1, day_start + 30),    # the start of a drink, not a drink
        ("6", 1, day_start + 40),              # fountain HTTP "Drinking done"
    ]
    for event_type, pet_id, ts in rows:
        await store.upsert_event({"device_id": 7, "device_type": "w7h",
                                  "event_type": event_type, "event_kind": "drinking",
                                  "pet_id": pet_id, "ts": ts})
    stats = await store.pet_visit_stats(1, now=now)
    assert stats["drinks_today"] == 1
    assert stats["last_drink_ts"] == day_start + 40
