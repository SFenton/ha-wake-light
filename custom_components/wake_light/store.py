"""Versioned Home Assistant Store wrapper."""

from __future__ import annotations

from typing import Any, Mapping

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    STORE_KEY_PREFIX,
    STORE_MINOR_VERSION,
    STORE_VERSION,
)
from .model import ProfileState, WakeLightProfile


class WakeLightStore:
    """Persist one config entry's mutable profile state."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        profile: WakeLightProfile,
    ) -> None:
        self._profile = profile
        self._store: Store[dict[str, Any]] = Store(
            hass,
            STORE_VERSION,
            f"{STORE_KEY_PREFIX}.{entry_id}",
            minor_version=STORE_MINOR_VERSION,
        )

    async def async_load(self) -> ProfileState:
        """Load and validate Store data."""
        data = await self._store.async_load()
        if not isinstance(data, Mapping):
            return ProfileState.initial(self._profile)
        return ProfileState.from_dict(data, self._profile)

    async def async_save(self, state: ProfileState) -> None:
        """Persist current state."""
        await self._store.async_save(state.to_dict())
