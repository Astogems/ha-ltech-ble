"""Config flow: get the mesh keys + device map ONCE, then everything runs over BLE.

Two sources, offered as a menu:
- login:     email + password through the signer add-on -> cloud login + placeinfo
- keys_file: a keys.json export (lhome_client.py --export) in the HA config folder

Only the mesh keys, device map, signer URL and email are stored; never the password.
Reconfigure (Settings -> the integration -> Reconfigure) re-fetches the device map
after lights are added/renamed in the L Home app, keeping the same source address.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import requests
import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback

from .const import (
    CONF_EMAIL,
    CONF_IDLE_DISCONNECT,
    CONF_KEYS_PATH,
    CONF_PASSWORD,
    CONF_PLACE,
    CONF_PWD_IS_MD5,
    CONF_SIGNER_URL,
    CONF_SRC,
    DEFAULT_KEYS_PATH,
    DEFAULT_SIGNER_URL,
    DEFAULT_SRC,
    DOMAIN,
    IDLE_DISCONNECT_MAX,
    IDLE_DISCONNECT_MIN,
    IDLE_DISCONNECT_SECONDS,
    PHONE_SRC,
)

_LOGGER = logging.getLogger(__name__)

# fields kept per device (device keys are not needed for control, so not stored)
_DEVICE_FIELDS = ("deviceid", "name", "unicastAddress", "mac",
                  "productname", "minkelvin", "maxkelvin")


class InvalidKeys(Exception):
    """No usable place (netkey/appkey) in the export / cloud response."""


class CannotConnect(Exception):
    """Signer add-on or cloud unreachable."""


class InvalidAuth(Exception):
    """The cloud rejected the login."""


def _is_key(v: Any) -> bool:
    try:
        return isinstance(v, str) and len(bytes.fromhex(v)) == 16
    except ValueError:
        return False


def _normalize_places(raw_places: list[dict]) -> list[dict]:
    """lhome_client mesh_keys() rows -> the slim form stored in the config entry."""
    places = []
    for p in raw_places or []:
        if not (_is_key(p.get("netkey")) and _is_key(p.get("appkey"))):
            continue
        devices = []
        for d in p.get("devices") or []:
            if d.get("unicastAddress") in (None, ""):
                _LOGGER.warning("skipping %s: no unicastAddress", d.get("name"))
                continue
            dev = {k: d.get(k) for k in _DEVICE_FIELDS}
            dev["unicastAddress"] = int(dev["unicastAddress"])  # cloud gives a decimal int
            devices.append(dev)
        places.append({
            "placeid": p.get("placeid"),
            "placename": p.get("placename"),
            "netkey": p["netkey"].upper(),
            "appkey": p["appkey"].upper(),
            "ivindex": int(p.get("ivindex") or 0),
            "devices": devices,
        })
    if not places:
        raise InvalidKeys("no place with netkey/appkey")
    return places


def _load_places(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        try:
            export = json.load(f)
        except ValueError as err:
            raise InvalidKeys(str(err)) from err
    return _normalize_places(export.get("places"))


def _fetch_places(signer_url: str, email: str, password: str, pwd_is_md5: bool) -> list[dict]:
    """Blocking: cloud login via the signer, then placeinfo for every place."""
    from .lhome_client import LHome, _rows_of

    client = LHome(signer_url=signer_url)
    try:
        client.login(email, password, pwd_is_md5=pwd_is_md5)
    except requests.RequestException as err:
        raise CannotConnect(str(err)) from err
    except (RuntimeError, KeyError, ValueError) as err:
        raise InvalidAuth(str(err)) from err
    try:
        raw = []
        for p in _rows_of(client.place_list()):
            keys = client.mesh_keys(p.get("placeid"))
            keys["placename"] = p.get("placename")
            raw.append(keys)
    except requests.RequestException as err:
        raise CannotConnect(str(err)) from err
    return _normalize_places(raw)


def _parse_src(text: str) -> int:
    src = int(text.strip().lower().removeprefix("0x"), 16)
    if not 0x0001 <= src <= 0x7FFF:
        raise ValueError("not a unicast address")
    return src


class LHomeBleOptionsFlow(OptionsFlow):
    """How long HA keeps the light's single BLE connection after a command."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(CONF_IDLE_DISCONNECT, IDLE_DISCONNECT_SECONDS)
        schema = vol.Schema({
            vol.Required(CONF_IDLE_DISCONNECT, default=current): vol.All(
                vol.Coerce(int), vol.Range(min=IDLE_DISCONNECT_MIN, max=IDLE_DISCONNECT_MAX)),
        })
        return self.async_show_form(step_id="init", data_schema=schema)


class LHomeBleConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> LHomeBleOptionsFlow:
        return LHomeBleOptionsFlow()

    def __init__(self) -> None:
        self._places: list[dict] = []
        self._src = DEFAULT_SRC
        self._extra: dict[str, Any] = {}

    @property
    def _reconfiguring(self) -> bool:
        return self.source == SOURCE_RECONFIGURE

    # ---- entry points ---------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["login", "keys_file"])

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._src = self._get_reconfigure_entry().data[CONF_SRC]
        return self.async_show_menu(step_id="reconfigure", menu_options=["login", "keys_file"])

    # ---- sources ----------------------------------------------------------------

    async def async_step_login(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = self._check_src(user_input)
            if not errors:
                try:
                    places = await self.hass.async_add_executor_job(
                        _fetch_places, user_input[CONF_SIGNER_URL], user_input[CONF_EMAIL],
                        user_input[CONF_PASSWORD], user_input.get(CONF_PWD_IS_MD5, False))
                except CannotConnect as err:
                    _LOGGER.error("lhome_ble: signer/cloud unreachable: %s", err)
                    errors["base"] = "cannot_connect"
                except InvalidAuth as err:
                    _LOGGER.error("lhome_ble: login rejected: %s", err)
                    errors["base"] = "invalid_auth"
                except InvalidKeys:
                    errors["base"] = "no_places"
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("lhome_ble: unexpected error fetching keys")
                    errors["base"] = "unknown"
                else:
                    self._extra = {CONF_EMAIL: user_input[CONF_EMAIL],
                                   CONF_SIGNER_URL: user_input[CONF_SIGNER_URL]}
                    return await self._async_places_loaded(places, errors)

        prev = self._get_reconfigure_entry().data if self._reconfiguring else {}
        fields: dict[Any, Any] = {
            vol.Required(CONF_EMAIL, default=prev.get(CONF_EMAIL, vol.UNDEFINED)): str,
            vol.Required(CONF_PASSWORD): str,
            vol.Required(CONF_SIGNER_URL,
                         default=prev.get(CONF_SIGNER_URL, DEFAULT_SIGNER_URL)): str,
            vol.Optional(CONF_PWD_IS_MD5, default=False): bool,
        }
        return self.async_show_form(
            step_id="login", data_schema=self._with_src(fields), errors=errors)

    async def async_step_keys_file(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = self._check_src(user_input)
            if not errors:
                path = self.hass.config.path(user_input[CONF_KEYS_PATH])
                try:
                    places = await self.hass.async_add_executor_job(_load_places, path)
                except FileNotFoundError:
                    errors[CONF_KEYS_PATH] = "file_not_found"
                except InvalidKeys as err:
                    _LOGGER.error("lhome_ble: %s is not a usable keys.json: %s", path, err)
                    errors[CONF_KEYS_PATH] = "invalid_keys"
                else:
                    self._extra = {}
                    return await self._async_places_loaded(places, errors)

        fields = {vol.Required(CONF_KEYS_PATH, default=DEFAULT_KEYS_PATH): str}
        return self.async_show_form(
            step_id="keys_file", data_schema=self._with_src(fields), errors=errors)

    # ---- shared ---------------------------------------------------------------

    def _with_src(self, fields: dict) -> vol.Schema:
        """Ask for the source address on first setup only; reconfigure keeps it."""
        if not self._reconfiguring:
            fields[vol.Required(CONF_SRC, default=DEFAULT_SRC)] = str
        return vol.Schema(fields)

    def _check_src(self, user_input: dict[str, Any]) -> dict[str, str]:
        if self._reconfiguring:
            return {}
        try:
            self._src = f"{_parse_src(user_input[CONF_SRC]):04X}"
        except ValueError:
            return {CONF_SRC: "invalid_src"}
        return {}

    async def _async_places_loaded(self, places: list[dict], errors: dict[str, str]) -> ConfigFlowResult:
        src = int(self._src, 16)
        taken = {d["unicastAddress"] for p in places for d in p["devices"]}
        if src == PHONE_SRC or src in taken:
            # only reachable on first setup; the form is shown again with the error
            errors[CONF_SRC] = "src_conflict"
            step = "login" if self._extra else "keys_file"
            return self.async_show_form(
                step_id=step, data_schema=self._last_schema(step), errors=errors)
        self._places = places
        if len(places) == 1:
            return await self._async_finish(places[0])
        return await self.async_step_place()

    def _last_schema(self, step: str) -> vol.Schema:
        if step == "login":
            return self._with_src({
                vol.Required(CONF_EMAIL, default=self._extra.get(CONF_EMAIL, vol.UNDEFINED)): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required(CONF_SIGNER_URL,
                             default=self._extra.get(CONF_SIGNER_URL, DEFAULT_SIGNER_URL)): str,
                vol.Optional(CONF_PWD_IS_MD5, default=False): bool,
            })
        return self._with_src({vol.Required(CONF_KEYS_PATH, default=DEFAULT_KEYS_PATH): str})

    async def async_step_place(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            place = next(p for p in self._places if str(p["placeid"]) == user_input[CONF_PLACE])
            return await self._async_finish(place)
        options = {str(p["placeid"]): p.get("placename") or str(p["placeid"]) for p in self._places}
        return self.async_show_form(
            step_id="place", data_schema=vol.Schema({vol.Required(CONF_PLACE): vol.In(options)}))

    async def _async_finish(self, place: dict) -> ConfigFlowResult:
        await self.async_set_unique_id(str(place["placeid"]))
        if self._reconfiguring:
            self._abort_if_unique_id_mismatch(reason="wrong_place")
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry(), data_updates={CONF_PLACE: place, **self._extra})
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"CtrlSpace X LTech {place.get('placename') or place['placeid']}",
            data={CONF_SRC: self._src, CONF_PLACE: place, **self._extra},
        )
