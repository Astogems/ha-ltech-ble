"""
lhome_client.py — minimal client for the L Home ("ltech smarthome") cloud,
for logging into YOUR OWN account and reading YOUR OWN devices.

Design:
  * Request bodies (appid/method/data/timestamp/session/sign/...) are built by the
    app's OWN native library via the local signer sidecar (SignerServer, unidbg).
    We never reimplement the signing or request-side AES.
  * This module only (a) POSTs the signed body and (b) DECRYPTS responses so you
    can read them. Response decryption is AES-128-CBC, key used as IV, zero-padded
    (the scheme already verified against real payloads).

Flow:
  1. Start the signer sidecar:   java -jar lhome-signer-jar-with-dependencies.jar
     (with LHOME_SO pointing at libmedia_pipeline.so)
  2. c = LHome(); c.login("you@example.com", "yourpassword")
  3. c.call("ysnetwork.base.com.place.list", {...})   # authenticated calls

Nothing here contacts the cloud until *you* run it. Use it only for your own account.
"""

from __future__ import annotations
import base64
import json
import hashlib
from dataclasses import dataclass, field
from typing import Any, Optional

import requests
from Crypto.Cipher import AES  # pip install pycryptodome


# ---- constants pulled from the app -----------------------------------------
BASE_URL         = "https://apie.ltsys.com.cn:2443/openapi/rest"   # your capture used apie.*; both are the same platform
APP_ID           = 119011416470103
STATIC_KEY       = b"ce8bd1ab2d2a41b5"          # AES_DEFAULT_KEY (pre-login data + login response)
DEFAULT_SESSION  = "s81glrjfvesoyx24rxvu9kmtmjmen4of"
METHOD_LOGIN     = "ysnetwork.base.com.user.app.login"
SIGNER_URL       = "http://127.0.0.1:8777/sign"


# ---- AES (decrypt-only; matches the verified scheme) -----------------------
def aes_decrypt(data_b64: str, key: bytes) -> bytes:
    """AES-128-CBC, IV == key, strip trailing zero padding."""
    ct = base64.b64decode(data_b64)
    pt = AES.new(key, AES.MODE_CBC, iv=key).decrypt(ct)
    return pt.rstrip(b"\x00")


def aes_decrypt_json(data_b64: str, key: bytes) -> Any:
    raw = aes_decrypt(data_b64, key)
    return json.loads(raw.decode("utf-8", "replace"))


# ---- client ----------------------------------------------------------------
@dataclass
class LHome:
    signer_url: str = SIGNER_URL
    base_url: str = BASE_URL
    session_id: str = DEFAULT_SESSION
    secret_key: Optional[bytes] = None          # per-session AES key, set after login
    userid: Optional[int] = None                # set after login
    http: requests.Session = field(default_factory=requests.Session)

    # --- talk to the sidecar to get a fully signed request body ---
    def _sign(self, method: str, data_obj: Any, *, secret_key: str, session: str) -> str:
        payload = {
            "appid": str(APP_ID),
            "method": method,
            "data": json.dumps(data_obj, separators=(",", ":")),
            "secretKey": secret_key,
            "session": session,
        }
        r = self.http.post(self.signer_url, json=payload, timeout=10)
        r.raise_for_status()
        return r.json()["body"]

    def _post(self, body: str) -> dict:
        # body is the full JSON envelope string produced by the native lib
        r = self.http.post(self.base_url, data=body.encode("utf-8"),
                           headers={"Content-Type": "application/json"}, timeout=15)
        r.raise_for_status()
        return r.json()

    # --- login (pre-auth: static key, default session) ---
    def login(self, loginname: str, pwd: str, *, devicesn: str = "", devicetype: str = "3",
              memberid: int = APP_ID, pwd_is_md5: bool = False) -> dict:
        """
        Log into your own account.
        NOTE on `pwd`: confirm from your own captured login whether the app sends the
        raw password or md5(password). If it's md5, pass pwd_is_md5=True (or hash it
        yourself). Decrypt your captured login `data` with STATIC_KEY to check.
        """
        if pwd_is_md5:
            pwd = hashlib.md5(pwd.encode()).hexdigest()
        data_obj = {
            "memberid": memberid,
            "loginname": loginname,
            "pwd": pwd,
            "devicetype": devicetype,
            "devicesn": devicesn or _gen_devicesn(),
        }
        body = self._sign(METHOD_LOGIN, data_obj, secret_key="", session=DEFAULT_SESSION)
        resp = self._post(body)
        info = _result_data(resp, STATIC_KEY)   # on this platform responses are plaintext JSON
        self.secret_key = info["secretkey"].encode()
        self.session_id = info["session"]
        self.userid = int(info["userid"])
        return info

    # --- authenticated call (uses per-session secret key + session) ---
    def call(self, method: str, data_obj: Any) -> Any:
        if self.secret_key is None:
            raise RuntimeError("not logged in")
        body = self._sign(method, data_obj,
                           secret_key=self.secret_key.decode(), session=self.session_id)
        resp = self._post(body)
        return _result_data(resp, self.secret_key)

    # ---- convenience wrappers ------------------------------------------------
    def place_list(self) -> Any:
        """List your homes. Each row includes placeid, netkey, ivindex, provisioneraddress."""
        return self.call("ysnetwork.base.com.area.place.user.list",
                         {"pagenum": 0, "pagesize": 0, "placeid": 0, "userid": self.userid})

    def place_info(self, placeid: int) -> Any:
        """Full place dump: devices, floors, groups, rooms, users, and info (mesh keys)."""
        return self.call("ysnetwork.base.com.area.place.user.placeinfo",
                         {"placeid": placeid, "userid": self.userid})

    def place_detail(self, placeid: int) -> Any:
        """Place detail: netkey, ivindex, provisioneraddress."""
        return self.call("ysnetwork.base.com.area.place.user.detail",
                         {"placeid": placeid, "userid": self.userid})

    def mesh_keys(self, placeid: int) -> dict:
        """Pull just the Bluetooth-Mesh material needed for local BLE control:
        netkey, appkey, ivindex, meshuuid, provisioneraddress, and a
        {deviceid/name -> unicastAddress} map."""
        info = self.place_info(placeid)
        meta = info.get("info", {}) if isinstance(info, dict) else {}
        detail = {}
        try:
            detail = self.place_detail(placeid) or {}
        except Exception:
            pass

        def pick(d, *names):
            for n in names:
                if isinstance(d, dict) and d.get(n) not in (None, ""):
                    return d[n]
            return None

        # devices can be under info["devices"]["rows"] or a list
        dev_container = info.get("devices") if isinstance(info, dict) else None
        rows = []
        if isinstance(dev_container, dict):
            rows = dev_container.get("rows", []) or []
        elif isinstance(dev_container, list):
            rows = dev_container

        devices = []
        for d in rows:
            if not isinstance(d, dict):
                continue
            # mesh fields live in the device's `param` JSON string:
            # {"unicastAddress": 2001 (decimal int), "deviceKey": hex, "deviceUUID": hex, ...}
            try:
                param = json.loads(d.get("param") or "{}")
            except ValueError:
                param = {}
            devices.append({
                "deviceid": d.get("deviceid"),
                "name": d.get("devicename") or d.get("name"),
                "unicastAddress": pick(param, "unicastAddress")
                                  or pick(d, "unicastAddress", "unicastaddress", "unicast", "meshaddress"),
                "deviceKey": pick(param, "deviceKey"),
                "deviceUUID": pick(param, "deviceUUID"),
                "mac": d.get("mac"),
                "productid": d.get("productid"),
                "productname": d.get("productname"),
                "minkelvin": d.get("minkelvin"),
                "maxkelvin": d.get("maxkelvin"),
            })

        return {
            "placeid": placeid,
            "netkey": pick(meta, "netkey") or pick(detail, "netkey"),
            "appkey": pick(meta, "applicationkey", "appkey"),
            "ivindex": pick(detail, "ivindex") or pick(meta, "ivindex"),
            "meshuuid": pick(meta, "meshuuid"),
            "provisioneraddress": pick(detail, "provisioneraddress"),
            "devices": devices,
            "groups": parse_groups(info.get("groups") if isinstance(info, dict) else None),
        }


# ---- groups -----------------------------------------------------------------
# A group made in the L Home app is a Bluetooth-Mesh group address (0xC000-0xFEFF)
# that the member lights subscribe to: one message, every member reacts at once.
# Seen in placeinfo (2026-11): the address is `index` as a hex string ("c002") and
# the members are `devices: [{deviceid, devicename}]`.  Other likely names are kept
# as fallbacks in case another app version differs.  (`groupindex` is a sort order,
# not the address.)

GROUP_ADDR_MIN, GROUP_ADDR_MAX = 0xC000, 0xFEFF

_GROUP_ADDR_NAMES = ("index", "groupAddress", "groupaddress", "groupAddr", "groupaddr",
                     "meshGroupAddress", "meshgroupaddress", "meshaddress", "meshAddress",
                     "address", "groupMeshAddress", "subscriptionAddress")
_GROUP_MEMBER_NAMES = ("devices", "devicelist", "deviceList", "groupdevices", "groupDevices",
                       "deviceids", "deviceIds", "deviceidlist", "children", "rows")


def _as_group_addr(v: Any) -> Optional[int]:
    """2-byte group address from a decimal int/str (cloud style) or a hex str ('C001', '0xC001')."""
    cand = []
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        cand = [v]
    elif isinstance(v, str) and v.strip():
        s = v.strip().lower()
        if s.startswith("0x"):
            cand = [_int_or_none(s[2:], 16)]
        else:
            cand = [_int_or_none(s, 10), _int_or_none(s, 16)]
    for c in cand:
        if c is not None and GROUP_ADDR_MIN <= c <= GROUP_ADDR_MAX:
            return c
    return None


def _int_or_none(s: str, base: int) -> Optional[int]:
    try:
        return int(s, base)
    except ValueError:
        return None


def _json_obj(v: Any) -> dict:
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip().startswith("{"):
        try:
            obj = json.loads(v)
        except ValueError:
            return {}
        return obj if isinstance(obj, dict) else {}
    return {}


def _find_group_addr(*objs: dict) -> Optional[int]:
    for obj in objs:                       # 1) known names
        for n in _GROUP_ADDR_NAMES:
            if (a := _as_group_addr(obj.get(n))) is not None:
                return a
    for obj in objs:                       # 2) any other *addr* field holding a group address
        for k, v in obj.items():
            if "addr" in k.lower() and (a := _as_group_addr(v)) is not None:
                return a
    return None


def _member_ids(*objs: dict) -> list:
    for obj in objs:
        for n in _GROUP_MEMBER_NAMES:
            v = obj.get(n)
            if isinstance(v, dict):
                v = v.get("rows")
            if isinstance(v, str):
                v = [x for x in v.replace(";", ",").split(",") if x.strip()]
            if not isinstance(v, list) or not v:
                continue
            ids = []
            for m in v:
                if isinstance(m, dict):
                    m = m.get("deviceid") or m.get("deviceId") or m.get("areadeviceid")
                if m not in (None, ""):
                    ids.append(str(m).strip())
            if ids:
                return ids
    return []


def parse_groups(container: Any) -> list[dict]:
    """placeinfo["groups"] ({"rows": [...]} or a list) -> [{groupid, name, address, members}].

    `address` is the mesh group address (int) or None if it could not be found;
    `members` is a list of deviceid strings (may be empty if the cloud row has none).
    `raw_fields` lists the row's field names, to help map the format if needed.
    """
    rows = container.get("rows", []) if isinstance(container, dict) else container
    groups = []
    for g in rows or []:
        if not isinstance(g, dict):
            continue
        objs = [g] + [_json_obj(g.get(k)) for k in ("param", "paramext", "ext", "extend")]
        objs = [o for o in objs if o]
        groups.append({
            "groupid": g.get("groupid") or g.get("groupId") or g.get("id"),
            "name": g.get("groupname") or g.get("groupName") or g.get("name"),
            "address": _find_group_addr(*objs),
            "members": _member_ids(*objs),
            "raw_fields": sorted({k for o in objs for k in o}),
        })
    return groups


def _result_data(resp: dict, key: bytes) -> Any:
    """Return the response payload.

    This platform returns plaintext JSON: {"ret":0,"data":{...},"message":"..."}.
    If an endpoint ever returns an encrypted string under `data`, we decrypt it
    with the given key (AES-128-CBC, key-as-IV, zero-pad).
    """
    ret = resp.get("ret")
    if ret not in (0, None):
        raise RuntimeError(f"API error ret={ret}: {resp.get('message')}")
    data = resp.get("data")
    if isinstance(data, (dict, list)):
        return data
    if isinstance(data, str) and data:
        return aes_decrypt_json(data, key)
    return resp  # status-only response, no data body


def _gen_devicesn() -> str:
    import uuid
    return uuid.uuid4().hex  # 32 lowercase hex, same shape as the app's devicesn


def _rows_of(result: Any) -> list:
    if isinstance(result, dict):
        return result.get("rows", []) or []
    return result if isinstance(result, list) else []


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="L Home client — login, placeinfo, and mesh-key export.")
    ap.add_argument("loginname")
    ap.add_argument("password")
    ap.add_argument("--placeid", type=int, default=None, help="limit to one place")
    ap.add_argument("--export", metavar="FILE",
                    help="write mesh keys + device map to a JSON file for the local BLE integration")
    ap.add_argument("--pwd-md5", action="store_true", help="send md5(password) instead of raw")
    ap.add_argument("--dump-raw", metavar="FILE",
                    help="write the raw placeinfo + detail responses to a JSON file (to locate device fields)")
    args = ap.parse_args()

    c = LHome()
    info = c.login(args.loginname, args.password, pwd_is_md5=args.pwd_md5)
    print("logged in as:", info.get("username"), "| userid:", info.get("userid"))
    print("session   :", c.session_id)
    print("secretkey :", c.secret_key.decode())

    places = _rows_of(c.place_list())
    if args.placeid:
        places = [p for p in places if p.get("placeid") == args.placeid]
    if not places:
        print("\n(no places found)")
        raise SystemExit(0)

    export = {
        "endpoint": c.base_url,
        "appid": APP_ID,
        "account": info.get("username"),
        "userid": c.userid,
        "places": [],
    }

    raw = {}
    for p in places:
        pid = p.get("placeid")
        if args.dump_raw:
            raw[str(pid)] = {"placelist_row": p,
                             "placeinfo": c.place_info(pid),
                             "detail": c.place_detail(pid)}
        keys = c.mesh_keys(pid)
        keys["placename"] = p.get("placename")
        export["places"].append(keys)
        print("\n=== BLE mesh keys :: %s (placeid %s) ===" % (p.get("placename"), pid))
        print(json.dumps(keys, indent=2, ensure_ascii=False))

    if args.dump_raw:
        with open(args.dump_raw, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2, ensure_ascii=False)
        print("\nraw responses ->", args.dump_raw)

    if args.export:
        with open(args.export, "w", encoding="utf-8") as f:
            json.dump(export, f, indent=2, ensure_ascii=False)
        print("\nexported ->", args.export,
              "(mesh keys + device map; the local BLE integration needs ONLY this — no cloud/add-on)")
