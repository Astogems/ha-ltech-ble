# L Home Signer

Runs the L Home app's own native signing library so the **CtrlSpace X LTech BLE
Integration** can log in to your L Home account **once** and fetch your mesh keys
and light list. After setup the integration works fully offline, so you can stop
this add-on.

Needs HA OS or HA Supervised (add-ons need the Supervisor).

## 1. Provide the native library (one time)

The library belongs to LTECH and is **not included** in this add-on. Take it from
the L Home app's APK (it's a zip): `lib/arm64-v8a/libmedia_pipeline.so`.

Copy it to HA's `share` folder (Samba or File editor add-on):

```
/share/lhome_signer/libmedia_pipeline.so
```

## 2. Start the add-on

Start it and open the **Log**. You should see:

```
[signer] listening on http://0.0.0.0:8777/sign
```

If you see `native lib not found`, the file isn't at the path above.

## 3. Set up the integration

Add or reconfigure *CtrlSpace X LTech BLE Integration*, choose **Log in with my L Home
account** and keep the signer URL `http://127.0.0.1:8777/sign` (HA OS). On other
installs use `http://<HA-host-LAN-IP>:8777/sign`.

When setup finishes you can stop the add-on. Start it again only when you
**Reconfigure** the integration after adding or renaming lights.

## Security

This service builds signed requests for your account. Run it only on a trusted
LAN, ideally firewall port 8777, and stop it when you're not using it.
