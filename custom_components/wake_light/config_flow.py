"""Config and options flows for Wake Light."""

from __future__ import annotations

from typing import Any, Mapping

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_AREA_ID,
    CONF_BLOCKER_ENTITY_IDS,
    CONF_DEFAULT_POST_WAKE_HOLD_MINUTES,
    CONF_DEFAULT_RAMP_MINUTES,
    CONF_LEGACY_BRIGHTNESS_LIFECYCLE_SAFE,
    CONF_OCCUPANCY_ENTITY_ID,
    CONF_PBL_SWITCH_ENTITY_ID,
    CONF_PROFILE_ID,
    CONF_PROFILE_NAME,
    CONF_ROOT_LIGHT_ENTITY_ID,
    CONF_SLEEPYPOD_LEFT_STATE_ENTITY_ID,
    CONF_SLEEPYPOD_RIGHT_STATE_ENTITY_ID,
    CONF_SLEEPYPOD_SCHEDULE_ENTITY_ID,
    CONF_SLEEPYPOD_SOURCE_SIDES,
    CONF_TARGET_LIGHT_ENTITY_IDS,
    CONF_VACATION_ENTITY_ID,
    DEFAULT_POST_WAKE_HOLD_MINUTES,
    DEFAULT_RAMP_MINUTES,
    DOMAIN,
    RAMP_MINUTE_OPTIONS,
    SOURCE_SIDE_LEFT,
    SOURCE_SIDE_RIGHT,
)
from .model import WakeLightProfile


def _optional_marker(
    key: str,
    defaults: Mapping[str, Any],
) -> vol.Optional:
    value = defaults.get(key)
    return (
        vol.Optional(key, default=value)
        if value not in (None, "")
        else vol.Optional(key)
    )


def _required_marker(
    key: str,
    defaults: Mapping[str, Any],
) -> vol.Required:
    value = defaults.get(key)
    return (
        vol.Required(key, default=value)
        if value not in (None, "")
        else vol.Required(key)
    )


def _allowed_ramp_minutes(defaults: Mapping[str, Any]) -> tuple[int, ...]:
    values = list(RAMP_MINUTE_OPTIONS)
    try:
        current = int(defaults.get(CONF_DEFAULT_RAMP_MINUTES))
    except (TypeError, ValueError):
        return RAMP_MINUTE_OPTIONS
    if 0 <= current <= 60 and current not in values:
        values.append(current)
    return tuple(values)


def _profile_schema(
    defaults: Mapping[str, Any],
    *,
    include_profile_id: bool,
) -> vol.Schema:
    fields: dict[Any, Any] = {}
    if include_profile_id:
        fields[
            vol.Required(
                CONF_PROFILE_ID,
                default=defaults.get(CONF_PROFILE_ID, ""),
            )
        ] = selector.TextSelector()
    fields[
        vol.Required(
            CONF_PROFILE_NAME,
            default=defaults.get(CONF_PROFILE_NAME, ""),
        )
    ] = selector.TextSelector()
    fields[_optional_marker(CONF_AREA_ID, defaults)] = selector.AreaSelector()
    fields[
        _required_marker(CONF_ROOT_LIGHT_ENTITY_ID, defaults)
    ] = selector.EntitySelector(
        selector.EntitySelectorConfig(domain="light")
    )
    fields[
        vol.Required(
            CONF_TARGET_LIGHT_ENTITY_IDS,
            default=defaults.get(CONF_TARGET_LIGHT_ENTITY_IDS, []),
        )
    ] = selector.EntitySelector(
        selector.EntitySelectorConfig(domain="light", multiple=True)
    )
    fields[
        _optional_marker(CONF_OCCUPANCY_ENTITY_ID, defaults)
    ] = selector.EntitySelector(selector.EntitySelectorConfig())
    fields[
        _required_marker(CONF_PBL_SWITCH_ENTITY_ID, defaults)
    ] = selector.EntitySelector(
        selector.EntitySelectorConfig(domain="switch")
    )
    fields[
        _required_marker(CONF_VACATION_ENTITY_ID, defaults)
    ] = selector.EntitySelector(selector.EntitySelectorConfig())
    fields[
        vol.Optional(
            CONF_BLOCKER_ENTITY_IDS,
            default=defaults.get(CONF_BLOCKER_ENTITY_IDS, []),
        )
    ] = selector.EntitySelector(
        selector.EntitySelectorConfig(multiple=True)
    )
    fields[
        _optional_marker(CONF_SLEEPYPOD_SCHEDULE_ENTITY_ID, defaults)
    ] = selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor")
    )
    fields[
        vol.Optional(
            CONF_SLEEPYPOD_SOURCE_SIDES,
            default=defaults.get(CONF_SLEEPYPOD_SOURCE_SIDES, []),
        )
    ] = selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(
                    value=SOURCE_SIDE_LEFT,
                    label="Left",
                ),
                selector.SelectOptionDict(
                    value=SOURCE_SIDE_RIGHT,
                    label="Right",
                ),
            ],
            multiple=True,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )
    fields[
        _optional_marker(CONF_SLEEPYPOD_LEFT_STATE_ENTITY_ID, defaults)
    ] = selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor")
    )
    fields[
        _optional_marker(CONF_SLEEPYPOD_RIGHT_STATE_ENTITY_ID, defaults)
    ] = selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor")
    )
    fields[
        vol.Required(
            CONF_DEFAULT_RAMP_MINUTES,
            default=defaults.get(
                CONF_DEFAULT_RAMP_MINUTES,
                DEFAULT_RAMP_MINUTES,
            ),
        )
    ] = vol.All(
        vol.Coerce(int),
        vol.In(_allowed_ramp_minutes(defaults)),
    )
    fields[
        vol.Required(
            CONF_DEFAULT_POST_WAKE_HOLD_MINUTES,
            default=DEFAULT_POST_WAKE_HOLD_MINUTES,
        )
    ] = vol.All(
        vol.Coerce(int),
        vol.In([DEFAULT_POST_WAKE_HOLD_MINUTES]),
    )
    fields[
        vol.Required(
            CONF_LEGACY_BRIGHTNESS_LIFECYCLE_SAFE,
            default=defaults.get(
                CONF_LEGACY_BRIGHTNESS_LIFECYCLE_SAFE,
                False,
            ),
        )
    ] = selector.BooleanSelector()
    return vol.Schema(fields)


class WakeLightConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Create one config entry per room profile."""

    VERSION = 1
    MINOR_VERSION = 2

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ):
        """Configure a room profile."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                profile = WakeLightProfile.from_mapping(user_input)
            except ValueError:
                errors["base"] = "invalid_profile"
            else:
                await self.async_set_unique_id(profile.profile_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=profile.name,
                    data=user_input,
                )
        return self.async_show_form(
            step_id="user",
            data_schema=_profile_schema(
                user_input or {},
                include_profile_id=True,
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the profile options flow."""
        return WakeLightOptionsFlow(config_entry)


class WakeLightOptionsFlow(config_entries.OptionsFlow):
    """Edit a room profile while retaining its stable profile ID."""

    def __init__(self, config_entry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ):
        """Edit profile entities, sources, and defaults."""
        merged = {**self._config_entry.data, **self._config_entry.options}
        errors: dict[str, str] = {}
        if user_input is not None:
            candidate = {
                **user_input,
                CONF_PROFILE_ID: self._config_entry.data[CONF_PROFILE_ID],
            }
            try:
                WakeLightProfile.from_mapping(candidate)
            except ValueError:
                errors["base"] = "invalid_profile"
            else:
                normalized = {
                    CONF_AREA_ID: None,
                    CONF_OCCUPANCY_ENTITY_ID: None,
                    CONF_SLEEPYPOD_SCHEDULE_ENTITY_ID: None,
                    CONF_SLEEPYPOD_LEFT_STATE_ENTITY_ID: None,
                    CONF_SLEEPYPOD_RIGHT_STATE_ENTITY_ID: None,
                    CONF_BLOCKER_ENTITY_IDS: [],
                    CONF_SLEEPYPOD_SOURCE_SIDES: [],
                    **user_input,
                }
                return self.async_create_entry(title="", data=normalized)
        return self.async_show_form(
            step_id="init",
            data_schema=_profile_schema(
                user_input or merged,
                include_profile_id=False,
            ),
            errors=errors,
        )
