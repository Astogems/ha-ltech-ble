"""CtrlSpace X LTech BLE Integration — local control of L Home / Lete mesh lights, no cloud.

Keys come from keys.json (lhome_client.py --export), copied into the config entry
at setup; commands go out through any HA Bluetooth adapter or ESPHome proxy.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_IDLE_DISCONNECT, CONF_PLACE, CONF_SRC, IDLE_DISCONNECT_SECONDS
from .mesh import MeshNetwork
from .proxy import MeshProxyLink

PLATFORMS: list[Platform] = [Platform.LIGHT]


def format_mac(mac: str) -> str:
    """'DC0D30D9CC47' -> 'DC:0D:30:D9:CC:47' (HA Bluetooth address format)."""
    m = mac.replace(":", "").replace("-", "").upper()
    return ":".join(m[i:i + 2] for i in range(0, 12, 2))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    place = entry.data[CONF_PLACE]
    net = MeshNetwork(place["netkey"], place["appkey"],
                      int(place.get("ivindex") or 0), int(entry.data[CONF_SRC], 16))
    macs = [format_mac(d["mac"]) for d in place["devices"] if d.get("mac")]
    idle = entry.options.get(CONF_IDLE_DISCONNECT, IDLE_DISCONNECT_SECONDS)
    link = MeshProxyLink(hass, net, macs, entry.title, idle_seconds=idle)
    await link.async_setup()
    entry.runtime_data = link
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.async_shutdown()
    return ok
