"""Light entities for L Home mesh lights and groups (on/off, brightness, colour temperature).

The lights' status replies are not decoded yet, so state is assumed: HA shows what
it last sent, and changes made in the L Home app or at the wall switch are not seen.

Groups made in the L Home app become their own light entities.  A group is sent ONE
message to its mesh group address and every member reacts at the same time; if the
cloud gave no group address, the group messages its members one by one instead.
After a group command the member lights' assumed state is updated to match.
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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity

from . import format_mac
from .const import CONF_PLACE, DOMAIN, POWER_ON_SETTLE_SECONDS
from .mesh import brightness_payload, ct_payload, power_payload
from .proxy import MeshProxyLink


def _group_signal(entry: ConfigEntry) -> str:
    return f"{DOMAIN}_{entry.entry_id}_group_command"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    link: MeshProxyLink = entry.runtime_data
    place = entry.data[CONF_PLACE]
    devices = place["devices"]
    entities: list[LightEntity] = [LHomeLight(link, entry, dev) for dev in devices]
    entities += [LHomeGroupLight(link, entry, grp, devices) for grp in place.get("groups") or []]
    async_add_entities(entities)


@dataclass
class _LightExtra(ExtraStoredData):
    """Brightness / CT survive restarts even while the light is off."""
    brightness: int | None
    color_temp_kelvin: int | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class _MeshLightBase(LightEntity, RestoreEntity):
    """Shared assumed-state logic; subclasses say where the messages go."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_assumed_state = True
    _attr_should_poll = False

    def __init__(self, link: MeshProxyLink, entry: ConfigEntry,
                 k_min: int | None, k_max: int | None) -> None:
        self._link = link
        self._entry = entry
        self._k_min = k_min
        self._k_max = k_max
        if k_min and k_max:
            self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_min_color_temp_kelvin = k_min
            self._attr_max_color_temp_kelvin = k_max
            self._attr_color_temp_kelvin = round((k_min + k_max) / 2)
        else:
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
            self._attr_color_mode = ColorMode.BRIGHTNESS
        self._attr_is_on = None
        self._attr_brightness = 255

    async def _async_send(self, payloads: list[bytes]) -> None:
        raise NotImplementedError

    @callback
    def _after_command(self, is_on: bool, brightness: int | None, kelvin: int | None) -> None:
        """Hook for groups to tell their members what was sent."""

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

    @callback
    def _apply(self, is_on: bool, brightness: int | None, kelvin: int | None) -> None:
        self._attr_is_on = is_on
        if brightness is not None:
            self._attr_brightness = brightness
        if kelvin is not None and self._k_min:
            self._attr_color_temp_kelvin = max(self._k_min, min(self._k_max, kelvin))

    async def async_turn_on(self, **kwargs: Any) -> None:
        settings = []
        brightness = kelvin = None
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
            await self._async_send([power_payload(True)])
            if settings:
                await asyncio.sleep(POWER_ON_SETTLE_SECONDS)
        if settings:
            await self._async_send(settings)

        self._apply(True, brightness, kelvin)
        self.async_write_ha_state()
        self._after_command(True, brightness, kelvin)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_send([power_payload(False)])
        self._attr_is_on = False
        self.async_write_ha_state()
        self._after_command(False, None, None)


class LHomeLight(_MeshLightBase):
    """One light, addressed by its unicast address."""

    def __init__(self, link: MeshProxyLink, entry: ConfigEntry, dev: dict) -> None:
        super().__init__(link, entry, dev.get("minkelvin"), dev.get("maxkelvin"))
        self._addr = int(dev["unicastAddress"])
        self._deviceid = str(dev["deviceid"])
        self._attr_unique_id = f"{entry.unique_id}_{dev['deviceid']}"
        mac = dev.get("mac")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._deviceid)},
            connections={(CONNECTION_BLUETOOTH, format_mac(mac))} if mac else set(),
            name=dev.get("name") or f"L Home light 0x{self._addr:04X}",
            manufacturer="LTECH",
            model=dev.get("productname"),
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(
            self.hass, _group_signal(self._entry), self._on_group_command))

    @callback
    def _on_group_command(self, members: frozenset[str], is_on: bool,
                          brightness: int | None, kelvin: int | None) -> None:
        if self._deviceid in members:
            self._apply(is_on, brightness, kelvin)
            self.async_write_ha_state()

    async def _async_send(self, payloads: list[bytes]) -> None:
        await self._link.async_send(self._addr, payloads)


class LHomeGroupLight(_MeshLightBase):
    """An L Home group: one message to its mesh group address (0xC000-0xFEFF)."""

    def __init__(self, link: MeshProxyLink, entry: ConfigEntry, grp: dict,
                 devices: list[dict]) -> None:
        by_id = {str(d["deviceid"]): d for d in devices}
        self._members = frozenset(m for m in grp.get("members") or [] if m in by_id)
        self._address: int | None = grp.get("address")
        self._member_addrs = [int(by_id[m]["unicastAddress"]) for m in sorted(self._members)]

        # CT only if every light the group can reach has a CT range (members if
        # known, otherwise every light in the home); use the widest range.
        reach = [by_id[m] for m in self._members] or devices
        if reach and all(d.get("minkelvin") and d.get("maxkelvin") for d in reach):
            k_min = min(int(d["minkelvin"]) for d in reach)
            k_max = max(int(d["maxkelvin"]) for d in reach)
        else:
            k_min = k_max = None
        super().__init__(link, entry, k_min, k_max)

        gid = str(grp["groupid"])
        self._attr_unique_id = f"{entry.unique_id}_group_{gid}"
        how = (f"Mesh group 0x{self._address:04X}" if self._address is not None
               else "Group (per light)")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"group_{gid}")},
            name=grp.get("name") or f"L Home group {gid}",
            manufacturer="LTECH",
            model=how,
        )
        self._attr_extra_state_attributes = {
            "group_address": f"0x{self._address:04X}" if self._address is not None else None,
            "member_count": len(self._members),
        }

    async def _async_send(self, payloads: list[bytes]) -> None:
        if self._address is not None:
            await self._link.async_send(self._address, payloads)
            return
        for addr in self._member_addrs:
            await self._link.async_send(addr, payloads)

    @callback
    def _after_command(self, is_on: bool, brightness: int | None, kelvin: int | None) -> None:
        if self._members:
            async_dispatcher_send(self.hass, _group_signal(self._entry),
                                  self._members, is_on, brightness, kelvin)
