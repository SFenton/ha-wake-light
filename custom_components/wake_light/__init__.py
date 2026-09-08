"""Wake Light custom integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .const import DOMAIN, PLATFORMS

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


async def async_setup(hass: HomeAssistant, _config: dict[str, Any]) -> bool:
    """Initialize domain storage."""
    hass.data.setdefault(DOMAIN, {"entries": {}, "service_registered": False})
    return True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Set up one generic room profile."""
    from homeassistant.exceptions import ConfigEntryError

    from .coordinator import WakeLightCoordinator
    from .model import WakeLightProfile
    from .service import (
        async_register_command_service,
        async_unregister_command_service_if_unused,
    )

    domain_data = hass.data.setdefault(
        DOMAIN,
        {"entries": {}, "service_registered": False},
    )
    try:
        profile = WakeLightProfile.from_mapping(
            {**entry.data, **entry.options}
        )
    except ValueError as err:
        raise ConfigEntryError(str(err)) from err
    if any(
        coordinator.profile.profile_id == profile.profile_id
        for coordinator in domain_data["entries"].values()
    ):
        raise ConfigEntryError("duplicate_profile_id")

    coordinator = WakeLightCoordinator(hass, entry, profile)
    domain_data["entries"][entry.entry_id] = coordinator
    try:
        await async_register_command_service(hass)
        await coordinator.async_start()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        domain_data["entries"].pop(entry.entry_id, None)
        await coordinator.async_shutdown()
        async_unregister_command_service_if_unused(hass)
        raise
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Unload one room profile."""
    from .service import async_unregister_command_service_if_unused

    domain_data = hass.data.get(DOMAIN, {})
    coordinator = domain_data.get("entries", {}).get(entry.entry_id)
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry,
        PLATFORMS,
    )
    if not unload_ok:
        return False
    if coordinator is not None:
        await coordinator.async_shutdown()
    domain_data.get("entries", {}).pop(entry.entry_id, None)
    async_unregister_command_service_if_unused(hass)
    return True


async def _async_reload_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Reload after config-entry options change."""
    await hass.config_entries.async_reload(entry.entry_id)
