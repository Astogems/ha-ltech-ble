# CtrlSpace X LTech BLE Integration — Home Assistant

Internal domain: `lhome_ble` (kept unchanged so existing setups, entities and the saved mesh sequence number carry over).

Local control of L Home / Lete Bluetooth-Mesh lights: **on/off, brightness, colour
temperature**. No cloud and no signer add-on at runtime. The mesh keys and device list
are fetched **once** at setup, either by logging in through the signer add-on or from a
`keys.json` file.

```
HA (lhome_ble) ──Wi-Fi──► ESPHome BT proxy ──BLE GATT 0x2ADD──► any light (mesh proxy) ──mesh──► all lights
```

## 1. ESPHome Bluetooth proxy

Flash `esphome/lhome-bt-proxy.yaml` to an ESP32 placed within a few metres of a
light (ESPHome add-on → New device → paste the YAML; set the board and secrets).
Add it to HA when discovered. An existing ESPHome Bluetooth proxy works too, as long
as it has `bluetooth_proxy: active: true`. A Bluetooth adapter on the HA host also works.

## 2. Install the integration

**With HACS (recommended)**

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/Astogems/ha-ltech-ble`, type **Integration**.
2. Search HACS for *CtrlSpace X LTech BLE Integration* → **Download**.
3. Restart HA → Settings → Devices & services → **Add integration** → *CtrlSpace X LTech BLE Integration*.

**Manually:** copy `custom_components/lhome_ble/` to HA's `/config/custom_components/lhome_ble/`, then do step 3 above.

## 3. L Home Signer add-on (only for the account login)

Skip this if you set up from a `keys.json` file. HACS can't install add-ons; use the
Add-on Store instead:

1. Settings → Add-ons → **Add-on Store** → ⋮ → **Repositories** → add
   `https://github.com/Astogems/ha-ltech-ble`.
2. Install **L Home Signer** (the first build takes a few minutes).
3. The add-on does **not** include LTECH's native library. Take
   `lib/arm64-v8a/libmedia_pipeline.so` from the L Home APK and copy it to
   `/share/lhome_signer/libmedia_pipeline.so`.
4. Start the add-on. Its log should show `[signer] listening on ...`.

Details: [`lhome_signer/DOCS.md`](lhome_signer/DOCS.md).

## 4. Set up

1. Choose how to get the keys (one time):
   - **Log in with my L Home account.** Start the *L Home Signer* add-on, then enter your
     email, password and the signer URL. The default `http://127.0.0.1:8777/sign` works
     on HA OS; otherwise use `http://<HA-host-LAN-IP>:8777/sign`. The password is only
     used for this login and is **not stored**.
   - **Use a keys.json file.** Copy `keys.json` to `/config/lhome_keys.json` first.
2. Keep the source address `7E00` → each light becomes a `light.*` entity with
   brightness and a 2700–6500 K colour-temperature slider.

After setup, the signer add-on and `keys.json` are no longer needed; you can stop the
add-on. **When you add or rename lights** in the L Home app, go to the integration and
choose ⋮ → **Reconfigure**. Log in again (start the add-on first) or point it at a fresh
`keys.json`. The device list is refreshed, and the source address and sequence number
are kept.

## Source addresses — each sender needs its own

| Sender | Address |
|---|---|
| L Home phone app | `0x0001` |
| Lights | from the cloud (CT light 1 = `0x07D1`) |
| Home Assistant | `0x7E00` (default) |
| PC test script (not in this repo) | `0x7E10` |
| nRF Mesh test files | `0x7F00`–`0x7FFF` |

Each light ignores any message whose sequence number isn't higher than the last one it
accepted from that address, so two senders must never share an address.

## Limits (v0.1)

- **Assumed state.** The lights' status replies aren't decoded yet, so HA shows what
  it last sent. Changes from the L Home app or a wall switch aren't seen.
- **IV index** is taken from `keys.json` (0). If the mesh ever performs an IV update,
  re-export keys (and update `ivindex`) or commands will be ignored.
- **CT lights only** are mapped (colour temperature `D5 <cool><warm>`). Lights
  without `minkelvin/maxkelvin` get on/off + brightness only; RGB is not mapped.
- The GATT connection to the proxy is dropped after 120 s idle, to free the ESPHome
  slot. The first command after that takes about 1–3 s while it reconnects.

## Troubleshooting

Enable debug logging:

```yaml
logger:
  logs:
    custom_components.lhome_ble: debug
```

- *"No L Home light is visible…"*: the proxy doesn't see a light. Check Settings →
  Bluetooth → advertisement monitor for the light's MAC address.
- Commands send but the light doesn't react: make sure no other sender uses `0x7E00`,
  and that the `ivindex` in `keys.json` is still current.
