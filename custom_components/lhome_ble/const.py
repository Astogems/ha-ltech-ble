DOMAIN = "lhome_ble"

CONF_KEYS_PATH = "keys_path"
CONF_SRC = "src_address"
CONF_PLACE = "place"
CONF_EMAIL = "email"
CONF_PASSWORD = "password"      # used once during setup, never stored
CONF_PWD_IS_MD5 = "pwd_is_md5"
CONF_SIGNER_URL = "signer_url"

# The signer add-on publishes 8777 on the HA host; HA OS core shares the host
# network, so localhost reaches it.  Otherwise use http://<HA-host-LAN-IP>:8777/sign
DEFAULT_SIGNER_URL = "http://127.0.0.1:8777/sign"

# keys.json exported by lhome_client.py --export, copied into HA's /config folder
DEFAULT_KEYS_PATH = "lhome_keys.json"

# HA's own mesh source address (hex).  Must not be the L Home phone (0x0001), any
# light, nRF Mesh (0x7F00-0x7FFF in our test files) or the PC test script (0x7E10).
DEFAULT_SRC = "7E00"
PHONE_SRC = 0x0001

# Sequence numbers are reserved in blocks and the block end is saved, so a restart
# never reuses a seq (the lights drop any seq <= the last one seen from our src).
SEQ_RESERVE = 256

# Drop the GATT connection after this long without commands.  A light accepts ONE
# connection at a time, so while HA holds it the L Home app cannot connect to THAT
# light.  Short = app stays usable; long = faster HA commands (no reconnect).
# 0 = never release: HA stays connected to one light and reconnects by itself when
# the link drops.  Only sensible with 2+ lights, so the app can use another one.
CONF_IDLE_DISCONNECT = "idle_disconnect"
IDLE_DISCONNECT_SECONDS = 10
IDLE_DISCONNECT_MIN = 10
IDLE_DISCONNECT_MAX = 600
KEEP_CONNECTED = 0

# Keep-connected mode: retry a failed (re)connect after this long, doubling up to
# the maximum.  A light of this mesh starting to advertise again cuts the wait short,
# but never below the minimum (a light that advertises yet refuses connections, or a
# proxy with no free slot, must not cause a tight retry loop).
RECONNECT_MIN_SECONDS = 5
RECONNECT_MAX_SECONDS = 60


def idle_seconds(options) -> int:
    """The idle-release option: 0 (keep connected) or MIN..MAX seconds.

    Older versions allowed 2-9 s; those are raised to the new minimum.
    """
    value = int(options.get(CONF_IDLE_DISCONNECT, IDLE_DISCONNECT_SECONDS))
    if value == KEEP_CONNECTED:
        return KEEP_CONNECTED
    return min(max(value, IDLE_DISCONNECT_MIN), IDLE_DISCONNECT_MAX)

# After power-on the light fades to its stored level; brightness/CT sent during
# that fade is overridden, so wait this long before sending them.
POWER_ON_SETTLE_SECONDS = 0.6

# On the first connection of a run, wait this long for the proxy's secure network
# beacon (it arrives ~1.3 s after connecting) to learn the real IV index.
BEACON_WAIT_SECONDS = 2.5

# Upper bound for connect + write of one command; a light that is busy (e.g. still
# connected to a phone) must fail the action, not leave it hanging.
SEND_TIMEOUT_SECONDS = 30
