"""Light entities for L Home mesh lights (on/off, brightness, colour temperature).

The lights' status replies are not decoded yet, so state is assumed: HA shows what
it last sent, and changes made in the L Home app or at the wall switch are not seen.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity

from . import format_mac
from .const import CONF_PLACE, DOMAIN, POWER_ON_SETTLE_SECONDS
from .mesh import brightness_payload, ct_payload, power_payload
from .proxy import MeshProxyLink


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    link: MeshProxyLink = entry.runtime_data
    async_add_entities(
        LHomeLight(link, entry, dev) for dev in entry.data[CONF_PLACE]["devices"])


@dataclass
class _LightExtra(ExtraStoredData):
    """Brightness / CT survive restarts even while the light is off."""
    brightness: int | None
    color_temp_kelvin: int | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class LHomeLight(LightEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_assumed_state = True
    _attr_should_poll = False

    def __init__(self, link: MeshProxyLink, entry: ConfigEntry, dev: dict) -> None:
        self._link = link
        self._addr = int(dev["unicastAddress"])
        self._k_min = dev.get("minkelvin")
        self._k_max = dev.get("maxkelvin")
        self._attr_unique_id = f"{entry.unique_id}_{dev['deviceid']}"

        if self._k_min and self._k_max:
            self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_min_color_temp_kelvin = self._k_min
            self._attr_max_color_temp_kelvin = self._k_max
            self._attr_color_temp_kelvin = round((self._k_min + self._k_max) / 2)
        else:
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
            self._attr_color_mode = ColorMode.BRIGHTNESS
        self._attr_is_on = None
        self._attr_brightness = 255

        mac = dev.get("mac")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(dev["deviceid"]))},
            connections={(CONNECTION_BLUETOOTH, format_mac(mac))} if mac else set(),
            name=dev.get("name") or f"L Home light 0x{self._addr:04X}",
            manufacturer="LTECH",
            model=dev.get("productname"),
        )

    @property
    def extra_restore_state_data(self) -> _LightExtra:
        return _LightExtra(self._attr_brightness, self._attr_color_temp_kelvin)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) and last.state in (STATE_ON, STATE_OFF):
            self._attr_is_on = last.state == STATE_ON
        if extra := await self.async_get_last_extra_data():
            data = extra.as_dict()
            if data.get("brightness") is not None:
                self._attr_brightness = data["brightness"]
            if data.get("color_temp_kelvin") is not None and self._k_min:
                self._attr_color_temp_kelvin = data["color_temp_kelvin"]

    async def async_turn_on(self, **kwargs: Any) -> None:
        settings = []
        if ATTR_BRIGHTNESS in kwargs:
            brightness = max(1, int(kwargs[ATTR_BRIGHTNESS]))
            settings.append(brightness_payload(brightness))
        if ATTR_COLOR_TEMP_KELVIN in kwargs and self._k_min:
            kelvin = int(kwargs[ATTR_COLOR_TEMP_KELVIN])
            settings.append(ct_payload(kelvin, self._k_min, self._k_max))

        # Power on only when needed: an ON makes the light fade to its stored level,
        # which overrides brightness/CT sent during the fade (slider moves while on
        # therefore send the setting alone).  Unknown state counts as off.
        if not self._attr_is_on or not settings:
            await self._link.async_send(self._addr, [power_payload(True)])
            if settings:
                await asyncio.sleep(POWER_ON_SETTLE_SECONDS)
        if settings:
            await self._link.async_send(self._addr, settings)

        self._attr_is_on = True
        if ATTR_BRIGHTNESS in kwargs:
            self._attr_brightness = brightness
        if ATTR_COLOR_TEMP_KELVIN in kwargs and self._k_min:
            self._attr_color_temp_kelvin = max(self._k_min, min(self._k_max, kelvin))
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._link.async_send(self._addr, [power_payload(False)])
        self._attr_is_on = False
        self.async_write_ha_state()
