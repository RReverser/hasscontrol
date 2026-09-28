DOMAIN = "garmin_ble"

CONF_SECRET = "secret"
CONF_LABEL = "label"
CONF_ADAPTER = "adapter"
CONF_IDLE_TIMEOUT = "idle_timeout"

DEFAULT_LABEL = "garmin"
DEFAULT_ADAPTER = "hci0"
DEFAULT_IDLE_TIMEOUT = 30

SIGNAL_BATTERY = f"{DOMAIN}_battery_{{}}"

PAIRING_MODE_SECONDS = 120
# LE Secure Connections pairing times out after 30 s on the watch side; HA's
# confirmation has to arrive before that.
PAIRING_CONFIRM_SECONDS = 25
SIGNAL_PAIRING = f"{DOMAIN}_pairing_{{}}"
