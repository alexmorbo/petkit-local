"""HA entities for a per-pet virtual device — the pet, not the hardware.

A household with two cats and one litter box wants per-cat history, which no
device entity can express. `ai/pets.py::PetRegistry` owns the pets and
`ha/publisher.py` publishes each as its own HA device (identifiers
`petkit_pet_{id}`, a namespace of its own so pet ids cannot collide with device
ids) using the list below. Modeled on Jezza34000/homeassistant_petkit's pet
sensors.

Unlike every other list here these do NOT go through `ha/categories.py`:
they belong to no device type, and unlike a device the values are not reported
by anything — `publish_pet_state` recomputes all of them from the event store.

The list keeps its historical name although it now holds images too: the two
`image` entities carry no `value_path`, because they are fed raw JPEG bytes on
their own retained topic (`ha/publisher.py::_publish_pet_images`) rather than
read out of the state document. The poster and the `Last * Video` path come
from the same resolver (`media/pet_media.py`), so they always show one visit.
"""
from petkit_local.events import codes
from petkit_local.ha.discovery import EntityDef

LAST_VISIT_IMAGE = EntityDef(component="image", key="last_visit_image",
                             name="Last Visit Snapshot", icon="mdi:cat")
LAST_DRINK_IMAGE = EntityDef(component="image", key="last_drink_image",
                             name="Last Drink Snapshot", icon="mdi:cup-water")

PET_SENSORS = [
    EntityDef(component="sensor", key="last_visit", name="Last Visit",
              value_path="state.lastVisit", device_class="timestamp", icon="mdi:cat"),
    EntityDef(component="sensor", key="visits_today", name="Visits Today",
              value_path="state.visitsToday", icon="mdi:counter"),
    EntityDef(component="sensor", key="last_visit_weight", name="Last Visit Weight",
              value_path="state.lastVisitWeight", device_class="weight", unit="g",
              icon="mdi:scale-bathroom"),
    EntityDef(component="sensor", key="last_visit_duration", name="Last Visit Duration",
              value_path="state.lastVisitDuration", unit="s", device_class="duration",
              icon="mdi:timer-outline"),
    EntityDef(component="sensor", key="last_device_used", name="Last Device Used",
              value_path="state.lastDeviceUsed", icon="mdi:home-floor-a"),
    # The OBSERVED weight: median of the newest weighed visits. The pet's
    # reference weight (what visits are attributed against) is user-set in the
    # panel and never follows this, so a misattribution cannot feed back.
    EntityDef(component="sensor", key="weight", name="Weight",
              value_path="state.weight", device_class="weight", unit="g",
              state_class="measurement", icon="mdi:scale-bathroom"),
    EntityDef(component="sensor", key="last_drink", name="Last Drink",
              value_path="state.lastDrink", device_class="timestamp", icon="mdi:cup-water"),
    EntityDef(component="sensor", key="drinks_today", name="Drinks Today",
              value_path="state.drinksToday", icon="mdi:counter"),
    LAST_VISIT_IMAGE,
    LAST_DRINK_IMAGE,
    # The panel path (`api/media/<encoded rel>`) the stable
    # `/api/pets/{id}/last-visit|last-drink` URL currently redirects to. It
    # changes exactly when a new playable recording becomes the latest, which
    # `last_visit` (set when the event row lands, minutes before the video
    # exists) cannot signal.
    EntityDef(component="sensor", key="last_visit_video", name="Last Visit Video",
              value_path="state.lastVisitVideo", icon="mdi:filmstrip"),
    EntityDef(component="sensor", key="last_drink_video", name="Last Drink Video",
              value_path="state.lastDrinkVideo", icon="mdi:filmstrip"),
]

PET_IMAGE_BY_KIND = {codes.KIND_TOILET: LAST_VISIT_IMAGE, codes.KIND_DRINKING: LAST_DRINK_IMAGE}
