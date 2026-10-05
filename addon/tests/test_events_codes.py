"""Property tests over the protocol tables in events/codes.py.

These are deliberately properties rather than examples. The gap this module
exists to close was not "one code had the wrong label" -- it was that a code's
kind, label, detail-ness and anchor role lived in six separate collections, so
seven codes ended up in none of them and nobody noticed until a real device had
been reporting them for days. An example test would have to be written for each
new code to catch that; a property test catches the next one for free.
"""
from petkit_local.events import codes, ingest
from petkit_local.utils.const import DEVICE_TYPES_ALL

#: Every HTTP code observed at least once in the reference capture (268 events
#: from a real T5, firmware 943). None of these may classify as "other".
OBSERVED_HTTP_CODES = (
    "1", "2", "3", "4", "5", "7", "8", "9", "10",
    "13", "14", "15", "16", "17", "20", "24",
)


def _all_rows():
    """(table name, key, EventCode) for every row in every namespace."""
    for name, table in (("litter", codes.LITTER_HTTP_CODES),
                        ("feeder", codes.FEEDER_HTTP_CODES),
                        ("fountain", codes.FOUNTAIN_HTTP_CODES),
                        ("mqtt", codes.MQTT_EVENT_TOPICS)):
        for key, code in table.items():
            yield name, key, code


def test_every_row_is_well_formed():
    for table, key, code in _all_rows():
        assert code.kind in codes.EVENT_KINDS, f"{table}:{key} has kind {code.kind!r}"
        assert code.label, f"{table}:{key} has no label"
        assert code.grade in codes.GRADES, f"{table}:{key} has grade {code.grade!r}"
        # A completion label is built as "<trigger> <done_word> <outcome>", so
        # a role=done row without the noun would render "Auto  completed".
        if code.role == codes.ROLE_DONE:
            assert code.done_word, f"{table}:{key} completes nothing"


def test_families_only_name_real_device_codenames():
    """A typo'd codename would silently exclude a device from `codes_for`."""
    for table, key, code in _all_rows():
        unknown = set(code.families) - set(DEVICE_TYPES_ALL)
        assert not unknown, f"{table}:{key} names unknown devices {unknown}"


def test_every_observed_code_is_classified_and_labelled():
    """The regression that started this: 23 of 268 events read "Event N"."""
    for code in OBSERVED_HTTP_CODES:
        assert ingest.classify_event_kind(code, device_type="t5") != codes.KIND_OTHER, \
            f"code {code} still falls through to 'other'"
        label = ingest.event_type_label(code, "t5")
        assert not label.startswith("Event "), f"code {code} still reads {label!r}"


def test_unmapped_codes_stay_unmapped():
    """12/19/22/23 are in neither the firmware RE nor any capture.

    Mapping them to a plausible-looking neighbour would hide the moment a
    T6/T7 starts sending one; the warning path is the feature.
    """
    for code in sorted(codes.UNKNOWN_HTTP_CODES):
        assert codes.lookup(code, "t5") is None
        assert ingest.event_type_label(code, "t5") == f"Event {code}"


def test_http_codes_are_resolved_per_device_category():
    """Code 2 is `err_over` on a litter box and `feed_over` on a feeder.

    A flat table would call a feeder's completed meal a cleared fault. This is
    the single most dangerous property of the HTTP namespace.
    """
    litter = codes.lookup("2", "t5")
    feeder = codes.lookup("2", "d4h")
    assert litter.kind == codes.KIND_ERROR and litter.label == "Error cleared"
    assert feeder.kind == codes.KIND_FEEDING and feeder.label == "Feeding done"
    assert ingest.classify_event_kind("2", device_type="d4") == codes.KIND_FEEDING
    assert ingest.classify_event_kind("2", device_type="t6") == codes.KIND_ERROR


def test_unknown_device_type_falls_back_rather_than_dropping():
    """Labelling an unclassified device beats showing its owner a bare code."""
    assert codes.lookup("10", "some-new-model") is not None
    assert codes.lookup("10", None) is not None


def test_mqtt_topics_resolve_for_every_category():
    """MQTT names are global, so a sparse HTTP table must not hide them."""
    for device_type in ("t5", "d4h", "w7h"):
        assert codes.lookup("pet_detect", device_type) is not None


def test_every_topic_the_bridge_dispatches_on_is_in_the_table():
    """`mqtt/bridge.py` reacting to a topic the table does not know means the
    event is stored with a label the table cannot produce."""
    from petkit_local.ha.categories import CATEGORY_SPECS
    for spec in CATEGORY_SPECS.values():
        for topic in spec.state_topics_for(True):
            if topic.endswith("/post"):      # transport, handled separately
                continue
            assert codes.lookup(topic) is not None, f"{topic} is dispatched but unmapped"


def test_an_http_code_fires_the_same_ha_event_entity_as_its_mqtt_twin():
    """The bug this replaced: the HTTP handler looked its numeric `event_type`
    up in a table keyed by MQTT NAMES, so it always missed and the four `event`
    entities never fired for any device reporting over HTTP."""
    from petkit_local.events.ingest import entity_for_event

    # (mqtt name, http code, device type, entity)
    for name, code, device_type, entity in [
        ("pet_out", "10", "t5", "toilet_event"),
        ("clean_over", "5", "t5", "cleaning_event"),
        ("dump_over", "6", "t5", "cleaning_event"),
        ("feed_over", "2", "d4h", "feeding_event"),
        ("feed_start", "3", "d4sh", "feeding_event"),
        ("feed_over", "4", "d4sh", "feeding_event"),
        ("drink_over", "6", "w7h", "drinking_event"),
    ]:
        assert entity_for_event(name) == entity
        assert entity_for_event(code, device_type) == entity


def test_every_declared_ha_event_entity_can_actually_be_fired():
    """An `event` entity nothing maps to is published to HA and stays silent
    forever — indistinguishable from a device that never does the thing."""
    from petkit_local.ha.categories import CATEGORY_SPECS
    from petkit_local.events.ingest import KIND_TO_ENTITY

    declared = {e.key for spec in CATEGORY_SPECS.values()
                for e in spec.entities_for(True) if e.component == "event"}
    assert declared and declared <= set(KIND_TO_ENTITY.values())


def test_transport_topics_are_not_timeline_events():
    """Property updates and BLE plumbing must never become Timeline rows."""
    for topic in codes.MQTT_TRANSPORT_TOPICS:
        code = codes.lookup(topic)
        assert code is not None and code.kind == codes.KIND_SYSTEM
        assert code.detail, f"{topic} would render as a card"


def test_primary_done_codes_exclude_detail_steps():
    """A cycle emits several completions; only the non-detail one heads a card
    and carries the episode's media."""
    assert codes.PRIMARY_DONE_CODES <= codes.DONE_CODES
    assert "5" in codes.PRIMARY_DONE_CODES     # cleaning done
    assert "17" not in codes.PRIMARY_DONE_CODES  # light cycle, folded away


def test_anchor_codes_cover_every_card_heading_kind():
    for code in ("10", "20", "1"):
        assert code in codes.ANCHOR_CODES, f"{code} can no longer head a card"


def test_codes_for_filters_by_family():
    """Hardware one model lacks must not be advertised on it."""
    t4, t5, t6 = (codes.codes_for(d) for d in ("t4", "t5", "t6"))
    assert "melt_over" in t6 and "melt_over" not in t5        # T6+ cycle
    assert "feed_over" in codes.codes_for("d4h")

    # The N60 is built into the Purobot Max Pro/Pro 2, Ultra and Crystal Duo,
    # so its codes belong to t5/t6/t7 alike. This used to assert the opposite
    # -- "spray_over in t5 and not in t6" -- which withheld the deodorizer
    # codes from two thirds of the models that ship the hardware.
    for name in ("spray_over", "liquid_reset_over"):
        assert name in t5 and name in t6, name
        # The T4 has no built-in N60; where it sprays at all it is via an
        # optional BLE K3, whose consumables arrive as levels on the parent's
        # report rather than through these codes.
        assert name not in t4, name


def test_conflicts_and_caveats_are_recorded_not_silent():
    """A row whose sources disagree has to say so -- the Debug view renders
    `note`, and an unexplained `conflicted` badge is worse than none."""
    for table, key, code in _all_rows():
        if code.grade == codes.CONFLICTED:
            assert code.note, f"{table}:{key} is conflicted but says nothing"


def test_code_17_is_the_led_light_and_no_longer_conflicted():
    """Resolved from the device's own firmware: `ctrl` drives white and IR
    LEDs over PWM, raises an event carrying a `light_open_reason`, and closes
    it on `pk_toilet_over_judge_light_off`. LBCommand.LIGHT is action 7,
    matching NS5 workMode 7.
    """
    code = codes.lookup("17", "t5")
    assert code.grade == codes.CONFIRMED
    assert code.role == codes.ROLE_DONE
    # Noise the official app never surfaces: it stays folded away.
    assert code.detail is True
    # It fires on the way on AND on the way off with an identical payload, so
    # the direction can only come from the attached state.
    assert code.state_label == ("lightState", "light on", "light off")


# --- the settings-side tables (capture-derived, 2026-08-09) ------------------

def test_the_litter_start_actions_are_graded_and_agree_where_they_should():
    """This table records taps, `WORK_MODES` decodes reports, and they meet on
    five values. The one that disagrees must stay visible as a disagreement:
    pypetkitapi calls 8 RESET_N50_DEODOR, and the tap that produced it was
    Pack. One reading is bookkeeping, the other moves the mechanism."""
    valid = {codes.CONFIRMED, codes.INFERRED, codes.UNVERIFIED, codes.CONFLICTED}
    for value, (label, grade) in codes.LITTER_START_ACTIONS.items():
        assert isinstance(value, int) and label and grade in valid, value

    assert codes.LITTER_START_ACTIONS[8][1] == codes.CONFLICTED
    for agrees in (0, 1, 3, 4):
        assert codes.LITTER_START_ACTIONS[agrees][1] == codes.CONFIRMED


def test_the_start_action_table_is_not_a_whitelist():
    """`FOUNTAIN_W7H_START_ACTIONS` is a literal `cmp` chain out of firmware, so
    a value outside it is known to be ignored. This one is a list of taps
    somebody made on one model, and 2/9/10 are real codes it does not contain —
    10 confirmed against the real cloud. Nothing may validate against it."""
    for real_but_absent in (2, 9, 10):
        assert real_but_absent not in codes.LITTER_START_ACTIONS

    from petkit_local.ha import commands
    for key in ("deodorize", "maintenance_start", "reset_n60"):
        assert key in commands.LITTER_ACTIONS, (
            f"{key} still has to work, so nothing gates on the tap table")


def test_a_schedule_array_carries_two_jobs():
    """One `schedule[]` holds a box's cleaning AND deodorizing times, and `type`
    is the only thing separating them — captured on a T5 whose array gained a
    `type: 1` entry when a periodic deodorizing time was added. Every source
    before that recorded `type` as "observed as 0, semantics unknown"."""
    assert codes.SCHEDULE_TYPES == {0: "cleaning", 1: "deodorizing"}


def test_sunday_is_the_first_weekday():
    """Confirmed on two models. Every schedule this add-on serves by default
    repeats on all seven days, so the convention has never mattered — and would
    have been got wrong the first time a UI let somebody pick one day."""
    assert codes.WEEKDAY_NAMES[1] == "Sunday"
    assert codes.WEEKDAY_NAMES[7] == "Saturday"
    assert sorted(codes.WEEKDAY_NAMES) == list(range(1, 8))


def test_the_litter_type_enum_has_no_zero():
    """From a controlled 1 -> 2 -> 3 -> 1 run through the app's own picker.
    `devices/defaults.py` seeded 0 here for a long time and served it back to
    devices as the litter they are filled with."""
    assert 0 not in codes.SAND_TYPES
    assert sorted(codes.SAND_TYPES) == [1, 2, 3]

    from petkit_local.ha.entities.selects import LITTER_SELECTS
    sand = next(e for e in LITTER_SELECTS if e.key == "sand_type")
    assert sand.option_values == sorted(codes.SAND_TYPES)
    assert len(sand.options) == len(codes.SAND_TYPES)


# --- per-category HTTP code coverage (capture-derived, 2026-08-12) ----------

def test_feeder_codes_3_and_4_map_to_feeding():
    """Confirmed on a live D4SH (fw 248, HTTP). Code 3 opens a feed cycle,
    code 4 closes it — matching MQTT feed_start/feed_over content shapes."""
    start = codes.lookup("3", "d4sh")
    assert start.kind == codes.KIND_FEEDING and start.role == codes.ROLE_START

    over = codes.lookup("4", "d4sh")
    assert over.kind == codes.KIND_FEEDING and over.role == codes.ROLE_DONE

    assert ingest.classify_event_kind("3", device_type="d4sh") == codes.KIND_FEEDING
    assert ingest.classify_event_kind("4", device_type="d4sh") == codes.KIND_FEEDING


def test_feeder_code_3_does_not_collide_with_litter_code_3():
    """Code 3 is feed_start on a feeder and mechanism started on a litter box."""
    feeder = codes.lookup("3", "d4sh")
    litter = codes.lookup("3", "t5")
    assert feeder.kind == codes.KIND_FEEDING
    assert litter.kind == codes.KIND_CLEANING


def test_fountain_codes_map_to_drinking_and_pet():
    """Confirmed on a W7H (fw 456, HTTP capture)."""
    assert codes.lookup("5", "w7h").kind == codes.KIND_DRINKING
    assert codes.lookup("6", "w7h").kind == codes.KIND_DRINKING
    assert codes.lookup("20", "w7h").kind == codes.KIND_PET
    assert codes.lookup("24", "w7h").kind == codes.KIND_PET


def test_fountain_code_5_does_not_collide_with_litter_code_5():
    """Code 5 is drink_start on a fountain and cleaning done on a litter box."""
    fountain = codes.lookup("5", "w7h")
    litter = codes.lookup("5", "t5")
    assert fountain.kind == codes.KIND_DRINKING
    assert litter.kind == codes.KIND_CLEANING


def test_fountain_drink_over_fires_drinking_event():
    """The W7H's drink_over must fire the `drinking_event` HA entity."""
    from petkit_local.events.ingest import entity_for_event
    assert entity_for_event("6", "w7h") == "drinking_event"


def test_litter_ble_relay_codes_are_system_detail():
    """Codes 51 and 53 are BLE relay transport, not user-visible events."""
    for code in ("51", "53"):
        ec = codes.lookup(code, "t5")
        assert ec.kind == codes.KIND_SYSTEM
        assert ec.detail is True


def test_feed_result_9_is_mapped():
    """Observed on a live D4SH with empty hoppers."""
    assert 9 in codes.FEED_RESULT


def test_is_visit_summary_is_the_one_visit_report_that_carries_the_result():
    assert codes.is_visit_summary("10", "t5")
    assert codes.is_visit_summary("10", "t6")
    assert codes.is_visit_summary("pet_out", "t6")
    # `pet_in` / HTTP 9 carry a PARTIAL weight, sampled while stepping in.
    assert not codes.is_visit_summary("pet_in", "t6")
    assert not codes.is_visit_summary("9", "t5")
    # Per category: a feeder's "10" is not a litter visit.
    assert not codes.is_visit_summary("10", "d4h")
    assert not codes.is_visit_summary(None)


def test_drink_done_codes_are_one_report_per_drink():
    assert codes.DRINK_DONE_CODES == {"drink_over", "6"}
