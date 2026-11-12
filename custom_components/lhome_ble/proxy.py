"""GATT connection to one mesh proxy light, shared by every light in the place.

Any light that is a Mesh Proxy node relays our network PDUs to the rest of the
mesh, so one connection (through an ESPHome Bluetooth proxy or a local adapter)
controls all lights.
"""
from __future__ import annotations

import asyncio
import logging

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from .const import (
    BEACON_WAIT_SECONDS,
    DOMAIN,
    IDLE_DISCONNECT_SECONDS,
    SEND_TIMEOUT_SECONDS,
    SEQ_RESERVE,
)
from .mesh import (
    MESH_PROXY_DATA_IN_UUID,
    MESH_PROXY_DATA_OUT_UUID,
    MESH_PROXY_SERVICE_UUID,
    SEQ_MAX,
    MeshNetwork,
    proxy_segments,
)

_LOGGER = logging.getLogger(__name__)

# small gap between back-to-back messages so the proxy can relay each one
_INTER_MESSAGE_DELAY = 0.05


class MeshProxyLink:
    """Owns the sequence number and the (lazy, idle-timed) proxy connection."""

    def __init__(self, hass: HomeAssistant, net: MeshNetwork, macs: list[str], name: str,
                 idle_seconds: float = IDLE_DISCONNECT_SECONDS) -> None:
        self.hass = hass
        self.net = net
        self._macs = {m.upper() for m in macs}
        self._name = name
        self._idle_seconds = idle_seconds
        self._lock = asyncio.Lock()
        self._client: BleakClientWithServiceCache | None = None
        self._idle_unsub: CALLBACK_TYPE | None = None
        # keyed by network + src, not by config entry: re-adding the integration
        # with the same src must continue the sequence, never restart it
        self._store: Store[dict] = Store(
            hass, 1, f"{DOMAIN}.seq_{net.network_id.hex()}_{net.src:04x}")
        self._seq = 0
        self._reserved = 0
        # The IV index from the cloud/keys.json can be wrong; the lights' own secure
        # network beacon (sent on every proxy connect) is the truth.  Until one has
        # been seen this run, a fresh connection waits briefly for it before sending.
        self._iv_confirmed = asyncio.Event()

    async def async_setup(self) -> None:
        data = await self._store.async_load() or {}
        self._seq = self._reserved = int(data.get("next", 0))
        if "ivindex" in data and data["ivindex"] != self.net.ivindex:
            _LOGGER.info("%s: using IV index %d learned from the lights (keys say %d)",
                         self._name, data["ivindex"], self.net.ivindex)
            self.net.ivindex = int(data["ivindex"])
        _LOGGER.info("%s: mesh src 0x%04X, next seq %d, IV index %d, Network ID %s", self._name,
                     self.net.src, self._seq, self.net.ivindex, self.net.network_id.hex().upper())

    async def _async_save(self) -> None:
        await self._store.async_save({"next": self._reserved, "ivindex": self.net.ivindex})

    async def async_shutdown(self) -> None:
        self._cancel_idle()
        await self._disconnect()

    # ---- sequence numbers --------------------------------------------------

    async def _next_seq(self) -> int:
        if self._seq > SEQ_MAX:
            raise HomeAssistantError(
                f"Mesh sequence numbers for src 0x{self.net.src:04X} are exhausted; "
                "re-add the integration with a different source address")
        if self._seq >= self._reserved:
            self._reserved = min(self._seq + SEQ_RESERVE, SEQ_MAX + 1)
            await self._async_save()
        seq = self._seq
        self._seq += 1
        return seq

    # ---- finding and connecting to a proxy ---------------------------------

    @callback
    def _find_device(self) -> BLEDevice | None:
        """Strongest connectable light: a known MAC, or any proxy advertising our Network ID."""
        netid_adv = b"\x00" + self.net.network_id
        best = None
        for info in bluetooth.async_discovered_service_info(self.hass, connectable=True):
            known = info.address.upper() in self._macs
            ours = info.service_data.get(MESH_PROXY_SERVICE_UUID, b"").startswith(netid_adv)
            if (known or ours) and (best is None or info.rssi > best.rssi):
                best = info
        return best.device if best else None

    async def _ensure_connected(self) -> BleakClientWithServiceCache:
        if self._client is not None and self._client.is_connected:
            return self._client
        device = self._find_device()
        if device is None:
            _LOGGER.warning("%s: no light of this mesh is visible to any Bluetooth adapter "
                            "or proxy (looking for %s or Network ID %s)", self._name,
                            ", ".join(sorted(self._macs)) or "-", self.net.network_id.hex().upper())
            raise HomeAssistantError(
                "No L Home light is visible to any Bluetooth adapter or ESPHome proxy "
                "(is the proxy in range and are the lights powered?)")
        _LOGGER.debug("%s: connecting to mesh proxy %s", self._name, device.address)
        client = await establish_connection(
            BleakClientWithServiceCache,
            device,
            self._name,
            disconnected_callback=self._on_disconnected,
            ble_device_callback=lambda: self._find_device() or device,
            max_attempts=3,
        )
        # Like nRF Mesh: subscribe to Mesh Proxy Data Out first.  Telink-based
        # proxies drop Data In writes until the client has enabled notifications.
        try:
            await client.start_notify(MESH_PROXY_DATA_OUT_UUID, self._on_notify)
        except BleakError as err:
            _LOGGER.warning("%s: could not enable 0x2ADE notifications: %s", self._name, err)
        _LOGGER.debug("%s: connected to %s, mtu %s", self._name, device.address, client.mtu_size)
        self._client = client
        if not self._iv_confirmed.is_set():
            # the proxy sends its beacon ~1.3 s after connecting; commands sent before it
            # with a wrong IV index would be silently dropped by the lights
            try:
                async with asyncio.timeout(BEACON_WAIT_SECONDS):
                    await self._iv_confirmed.wait()
            except TimeoutError:
                _LOGGER.debug("%s: no network beacon within %ss; using IV index %d",
                              self._name, BEACON_WAIT_SECONDS, self.net.ivindex)
        return client

    @callback
    def _on_notify(self, _char, data: bytearray) -> None:
        data = bytes(data)
        _LOGGER.debug("%s: proxy -> HA %s", self._name, data.hex())
        iv = self.net.iv_from_beacon(data)
        if iv is None:
            return
        if iv != self.net.ivindex:
            _LOGGER.warning("%s: the lights use IV index %d, not %d (from the cloud/keys); "
                            "switching to %d", self._name, iv, self.net.ivindex, iv)
            self.net.ivindex = iv
            self.hass.async_create_task(self._async_save())
        self._iv_confirmed.set()

    @callback
    def _on_disconnected(self, _client) -> None:
        _LOGGER.debug("%s: mesh proxy disconnected", self._name)
        self._client = None

    async def _disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except BleakError:
                pass

    # ---- idle disconnect ----------------------------------------------------

    @callback
    def _cancel_idle(self) -> None:
        if self._idle_unsub is not None:
            self._idle_unsub()
            self._idle_unsub = None

    @callback
    def _schedule_idle(self) -> None:
        self._cancel_idle()

        @callback
        def _idle(_now) -> None:
            self._idle_unsub = None
            self.hass.async_create_task(self._disconnect_if_idle())

        self._idle_unsub = async_call_later(self.hass, self._idle_seconds, _idle)

    async def _disconnect_if_idle(self) -> None:
        async with self._lock:
            if self._client is not None:
                _LOGGER.debug("%s: idle %ss, releasing the light for other apps",
                              self._name, self._idle_seconds)
            await self._disconnect()

    # ---- sending -------------------------------------------------------------

    async def _write(self, client: BleakClientWithServiceCache, pdu: bytes) -> None:
        max_len = max(20, (client.mtu_size or 23) - 3)
        for seg in proxy_segments(pdu, max_len):
            await client.write_gatt_char(MESH_PROXY_DATA_IN_UUID, seg, response=False)

    async def async_send(self, dst: int, payloads: list[bytes]) -> None:
        """Send access payloads (opcode + params) to one unicast address."""
        async with self._lock:
            self._cancel_idle()
            for attempt in (1, 2):
                # retry only when an existing connection went stale; a fresh connect
                # that fails (light busy / out of range) fails the action right away
                reused = self._client is not None and self._client.is_connected
                try:
                    async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
                        client = await self._ensure_connected()
                        for i, access in enumerate(payloads):
                            if i:
                                await asyncio.sleep(_INTER_MESSAGE_DELAY)
                            seq = await self._next_seq()
                            pdu = self.net.proxy_pdu(access, seq, dst)
                            _LOGGER.debug("%s: HA -> 0x%04X seq %d access %s pdu %s",
                                          self._name, dst, seq, access.hex(), pdu.hex())
                            await self._write(client, pdu)
                    break
                except (BleakError, TimeoutError) as err:
                    await self._disconnect()
                    if attempt == 1 and reused:
                        _LOGGER.debug("%s: stale connection (%r), reconnecting", self._name, err)
                        continue
                    reason = (f"no answer within {SEND_TIMEOUT_SECONDS}s"
                              if isinstance(err, TimeoutError) else repr(err))
                    _LOGGER.warning(
                        "%s: sending to 0x%04X failed: %s. Is the light still connected to a "
                        "phone (nRF Mesh / L Home app)? It accepts one connection at a time",
                        self._name, dst, reason)
                    raise HomeAssistantError(
                        f"Could not send to L Home light 0x{dst:04X}: {reason}") from err
            self._schedule_idle()
