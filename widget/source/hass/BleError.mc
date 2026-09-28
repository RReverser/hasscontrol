using Toybox.WatchUi as Ui;

module Hass {
  class BleError extends Error {
    static const BLE_UNSUPPORTED = 101;
    static const BLE_NOT_FOUND = 102;
    static const BLE_CONNECT_FAILED = 103;
    static const BLE_WRITE_FAILED = 104;
    static const BLE_TIMEOUT = 105;
    static const BLE_PROTOCOL = 106;
    static const BLE_BAD_AUTH = 107;
    static const BLE_NOT_ALLOWED = 108;
    static const BLE_SERVICE_ERROR = 109;
    static const BLE_UNKNOWN_ENTITY = 110;
    static const BLE_NOT_PAIRED = 111;
    static const BLE_PAIR_FAILED = 112;
    static const BLE_NOT_APPROVED = 113;

    function initialize(bleCode) {
      Error.initialize(Error.ERROR_UNKNOWN);
      code = bleCode;
      if (bleCode == BLE_UNSUPPORTED) {
        message = Rez.Strings.Error_Ble_Unsupported;
      } else if (bleCode == BLE_NOT_FOUND || bleCode == BLE_CONNECT_FAILED) {
        message = Rez.Strings.Error_Ble_NotFound;
      } else if (bleCode == BLE_TIMEOUT || bleCode == BLE_WRITE_FAILED) {
        message = Rez.Strings.Error_Ble_Timeout;
      } else if (bleCode == BLE_BAD_AUTH) {
        message = Rez.Strings.Error_Ble_BadAuth;
      } else if (bleCode == BLE_NOT_ALLOWED) {
        message = Rez.Strings.Error_Ble_NotAllowed;
      } else if (bleCode == BLE_SERVICE_ERROR) {
        message = Rez.Strings.Error_Ble_Service;
      } else if (bleCode == BLE_UNKNOWN_ENTITY) {
        message = Rez.Strings.Error_Ble_UnknownEntity;
      } else if (bleCode == BLE_NOT_PAIRED) {
        message = Rez.Strings.Error_Ble_NotPaired;
      } else if (bleCode == BLE_PAIR_FAILED) {
        message = Rez.Strings.Error_Ble_PairFailed;
      } else if (bleCode == BLE_NOT_APPROVED) {
        message = Rez.Strings.Error_Ble_NotApproved;
      } else {
        message = Rez.Strings.Error_Unknown;
      }
    }

    // RESULT status (see PROTOCOL.md) -> error code
    static function fromStatus(status) {
      if (status == ST_BAD_AUTH || status == ST_NO_SESSION) {
        return new BleError(BLE_BAD_AUTH);
      }
      if (status == ST_NOT_ALLOWED) {
        return new BleError(BLE_NOT_ALLOWED);
      }
      if (status == ST_SERVICE_ERROR) {
        return new BleError(BLE_SERVICE_ERROR);
      }
      if (status == ST_NOT_PAIRED) {
        return new BleError(BLE_NOT_PAIRED);
      }
      if (status == ST_BAD_INDEX) {
        return new BleError(BLE_UNKNOWN_ENTITY);
      }
      return new BleError(BLE_PROTOCOL);
    }

    function toString() {
      return "BleError " + code + ": " + Ui.loadResource(message);
    }

    function toShortString() {
      return Ui.loadResource(message);
    }
  }
}
