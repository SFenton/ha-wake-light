"""Wake Light summary sensor."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import DOMAIN
from .coordinator import WakeLightCoordinator


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Set up one summary sensor per profile config entry."""
    coordinator = hass.data[DOMAIN]["entries"][entry.entry_id]
    async_add_entities([WakeLightSummarySensor(coordinator)])


class WakeLightSummarySensor(
    CoordinatorEntity[WakeLightCoordinator],
    SensorEntity,
):
    """Expose the exact React read contract."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:weather-sunset-up"
    _attr_should_poll = False

    def __init__(self, coordinator: WakeLightCoordinator) -> None:
        super().__init__(coordinator)
        profile = coordinator.profile
        self._attr_name = f"{profile.name} Wake Light"
        self._attr_unique_id = f"{DOMAIN}_{profile.profile_id}"
        self._attr_suggested_object_id = (
            f"{slugify(profile.name)}_wake_light"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, profile.profile_id)},
            name=profile.name,
            manufacturer="Wake Light",
            model="Room profile",
        )

    @property
    def native_value(self) -> str:
        """Return the current state-machine phase."""
        return self.coordinator.sensor_model().state

    @property
    def extra_state_attributes(self):
        """Return the exact versioned dashboard attributes."""
        return dict(self.coordinator.sensor_model().attributes)

    @property
    def available(self) -> bool:
        """Keep the entity present so unavailable is an explicit phase."""
        return True
