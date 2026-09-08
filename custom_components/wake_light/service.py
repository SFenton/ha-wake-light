"""Registration for the wake_light.command service."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse

from .const import COMMAND_OPERATIONS, DOMAIN, SERVICE_COMMAND

COMMAND_SCHEMA = vol.Schema(
    {
        vol.Required("profile_id"): vol.All(str, vol.Length(min=1, max=64)),
        vol.Required("expected_revision"): vol.All(
            vol.Coerce(int),
            vol.Range(min=0),
        ),
        vol.Required("request_id"): vol.All(str, vol.Length(min=1, max=128)),
        vol.Required("operation"): vol.In(COMMAND_OPERATIONS),
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_register_command_service(hass: HomeAssistant) -> None:
    """Register one domain service shared by every profile entry."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get("service_registered"):
        return

    async def _handle_command(call: ServiceCall) -> dict[str, Any]:
        entries = hass.data.get(DOMAIN, {}).get("entries", {})
        coordinator = next(
            (
                item
                for item in entries.values()
                if item.profile.profile_id == call.data["profile_id"]
            ),
            None,
        )
        if coordinator is None:
            return {
                "outcome": "invalid_request",
                "error": "unknown_profile",
                "profile_id": call.data["profile_id"],
                "request_id": call.data["request_id"],
            }
        return dict(await coordinator.async_handle_command(call.data))

    hass.services.async_register(
        DOMAIN,
        SERVICE_COMMAND,
        _handle_command,
        schema=COMMAND_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    domain_data["service_registered"] = True


def async_unregister_command_service_if_unused(hass: HomeAssistant) -> None:
    """Remove the service after the last config entry unloads."""
    domain_data = hass.data.get(DOMAIN, {})
    if domain_data.get("entries"):
        return
    if domain_data.get("service_registered"):
        hass.services.async_remove(DOMAIN, SERVICE_COMMAND)
        domain_data["service_registered"] = False
