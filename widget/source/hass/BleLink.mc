using Toybox.BluetoothLowEnergy as Ble;
using Toybox.Cryptography as Crypto;
using Toybox.Application as App;
using Toybox.StringUtil;
using Toybox.System;
using Toybox.Lang;
using Utils;

// Direct BLE link to the Home Assistant `garmin_ble` integration.
// See PROTOCOL.md at the repository root for the wire format.
//
// Connect IQ allows one outstanding GATT request at a time and writes of at
// most 20 bytes, so every write goes through _writeQueue and every frame is
// built to fit 20 bytes.
module Hass {
  const BLE_SVC = "6a1e0001-4c7d-4b4e-9a2b-3c8f1d2e5a01";
  const BLE_CMD = "6a1e0002-4c7d-4b4e-9a2b-3c8f1d2e5a01";
  const BLE_EVT = "6a1e0003-4c7d-4b4e-9a2b-3c8f1d2e5a01";

  const OP_HELLO = 0x01;
  const OP_LIST = 0x02;
  const OP_GET = 0x03;
  const OP_ACTION = 0x04;
  const OP_BATTERY = 0x05;
  const OP_BYE = 0x07;

  const MSG_CHALLENGE = 0x81;
  const MSG_ENTITY = 0x82;
  const MSG_LIST_END = 0x83;
  const MSG_RESULT = 0x84;

  const ST_OK = 0;
  const ST_BAD_AUTH = 1;
  const ST_BAD_INDEX = 2;
  const ST_NOT_ALLOWED = 3;
  const ST_SERVICE_ERROR = 4;
  const ST_NO_SESSION = 6;

  // Link states
  enum {
    LINK_IDLE,
    LINK_REGISTERING,
    LINK_SCANNING,
    LINK_CONNECTING,
    LINK_SUBSCRIBING,
    LINK_HELLO,
    LINK_READY,
    LINK_FAILED
  }

  class BleLink extends Ble.BleDelegate {
    hidden var _listener;        // object with onLinkReady(), onLinkError(code), onMessage(msg)
    hidden var _state = LINK_IDLE;
    hidden var _profileRegistered = false;
    hidden var _device = null;
    hidden var _cmd = null;
    hidden var _writeQueue = [];
    hidden var _writing = false;
    hidden var _key = null;
    hidden var _nonce = null;
    hidden var _ctr = 0;
    hidden var _buf = null;
    hidden var _bufSeq = -1;
    hidden var _svcUuid;
    hidden var _cmdUuid;
    hidden var _evtUuid;

    function initialize(listener) {
      BleDelegate.initialize();
      _listener = listener;
      _svcUuid = Ble.stringToUuid(BLE_SVC);
      _cmdUuid = Ble.stringToUuid(BLE_CMD);
      _evtUuid = Ble.stringToUuid(BLE_EVT);
    }

    function getState() {
      return _state;
    }

    function isReady() {
      return _state == LINK_READY;
    }

    // Parses the 32-hex-digit shared secret from the app settings.
    // Returns false when it is missing or malformed.
    function loadKey() {
      var hex = App.Properties.getValue("secret");
      _key = null;
      if (hex == null || hex.length() != 32) {
        return false;
      }
      try {
        _key = StringUtil.convertEncodedString(hex.toLower(), {
          :fromRepresentation => StringUtil.REPRESENTATION_STRING_HEX,
          :toRepresentation => StringUtil.REPRESENTATION_BYTE_ARRAY
        });
      } catch (e) {
        _key = null;
      }
      return _key != null && _key.size() == 16;
    }

    // Starts (or resumes) the connection sequence. Safe to call repeatedly.
    function start() {
      if (_state != LINK_IDLE && _state != LINK_FAILED) {
        return;
      }
      if (!loadKey()) {
        _fail(BleError.BLE_NO_SECRET);
        return;
      }
      Ble.setDelegate(self);
      if (_profileRegistered) {
        _startScan();
        return;
      }
      _state = LINK_REGISTERING;
      try {
        Ble.registerProfile({
          :uuid => _svcUuid,
          :characteristics => [
            { :uuid => _cmdUuid, :descriptors => [] },
            { :uuid => _evtUuid, :descriptors => [Ble.cccdUuid()] }
          ]
        });
      } catch (e) {
        _fail(BleError.BLE_UNSUPPORTED);
      }
    }

    function stop() {
      if (_state == LINK_READY) {
        // best effort: tell HA we are gone so it frees the connection at once
        try {
          _queueRaw(_command(OP_BYE, []b));
        } catch (e) {
        }
      }
      Ble.setScanState(Ble.SCAN_STATE_OFF);
      if (_device != null) {
        try {
          Ble.unpairDevice(_device);
        } catch (e) {
        }
      }
      _reset(LINK_IDLE);
    }

    hidden function _reset(newState) {
      _state = newState;
      _device = null;
      _cmd = null;
      _writeQueue = [];
      _writing = false;
      _nonce = null;
      _ctr = 0;
      _buf = null;
      _bufSeq = -1;
    }

    hidden function _fail(code) {
      Ble.setScanState(Ble.SCAN_STATE_OFF);
      if (_device != null) {
        try {
          Ble.unpairDevice(_device);
        } catch (e) {
        }
      }
      _reset(LINK_FAILED);
      _listener.onLinkError(code);
    }

    hidden function _startScan() {
      _state = LINK_SCANNING;
      Ble.setScanState(Ble.SCAN_STATE_SCANNING);
    }

    // ---- BleDelegate callbacks ------------------------------------------

    function onProfileRegister(uuid, status) {
      if (status != Ble.STATUS_SUCCESS) {
        _fail(BleError.BLE_UNSUPPORTED);
        return;
      }
      _profileRegistered = true;
      if (_state == LINK_REGISTERING) {
        _startScan();
      }
    }

    function onScanResults(scanResults) {
      if (_state != LINK_SCANNING) {
        return;
      }
      for (var r = scanResults.next() as Ble.ScanResult?; r != null; r = scanResults.next() as Ble.ScanResult?) {
        var uuids = r.getServiceUuids();
        for (var u = uuids.next(); u != null; u = uuids.next()) {
          if (u.equals(_svcUuid)) {
            Ble.setScanState(Ble.SCAN_STATE_OFF);
            _state = LINK_CONNECTING;
            try {
              _device = Ble.pairDevice(r);
            } catch (e) {
              _fail(BleError.BLE_CONNECT_FAILED);
            }
            return;
          }
        }
      }
    }

    function onConnectedStateChanged(device, state) {
      if (state == Ble.CONNECTION_STATE_CONNECTED) {
        if (_state != LINK_CONNECTING) {
          return;
        }
        _device = device;
        var svc = device.getService(_svcUuid);
        if (svc == null) {
          _fail(BleError.BLE_CONNECT_FAILED);
          return;
        }
        _cmd = svc.getCharacteristic(_cmdUuid);
        var evt = svc.getCharacteristic(_evtUuid);
        var cccd = evt != null ? evt.getDescriptor(Ble.cccdUuid()) : null;
        if (_cmd == null || cccd == null) {
          _fail(BleError.BLE_CONNECT_FAILED);
          return;
        }
        _state = LINK_SUBSCRIBING;
        cccd.requestWrite([0x01, 0x00]b);
      } else if (_state != LINK_IDLE) {
        // HA dropped us (idle timeout) or radio lost: next request reconnects
        _reset(LINK_IDLE);
        _listener.onLinkDown();
      }
    }

    function onDescriptorWrite(descriptor, status) {
      if (_state != LINK_SUBSCRIBING) {
        return;
      }
      if (status != Ble.STATUS_SUCCESS) {
        _fail(BleError.BLE_CONNECT_FAILED);
        return;
      }
      _state = LINK_HELLO;
      _queueRaw([OP_HELLO, 1]b);
    }

    function onCharacteristicWrite(characteristic, status) {
      _writing = false;
      if (status != Ble.STATUS_SUCCESS) {
        _fail(BleError.BLE_WRITE_FAILED);
        return;
      }
      _pump();
    }

    function onCharacteristicChanged(characteristic, value) {
      if (value == null || value.size() < 1) {
        return;
      }
      var hdr = value[0];
      var seq = hdr & 0x7f;
      if (_buf == null || seq != _bufSeq) {
        _buf = []b;
        _bufSeq = seq;
      }
      _buf.addAll(value.slice(1, null));
      if ((hdr & 0x80) != 0) {
        var msg = _buf;
        _buf = null;
        _bufSeq = -1;
        _onMessage(msg);
      }
    }

    // ---- messages ----------------------------------------------------------

    hidden function _onMessage(msg) {
      if (msg.size() == 0) {
        return;
      }
      if (msg[0] == MSG_CHALLENGE && _state == LINK_HELLO) {
        if (msg.size() < 11) {
          _fail(BleError.BLE_PROTOCOL);
          return;
        }
        _nonce = msg.slice(1, 9);
        _ctr = 0;
        _state = LINK_READY;
        _listener.onLinkReady(msg[10]);
        return;
      }
      _listener.onMessage(msg);
    }

    // ---- sending -----------------------------------------------------------

    // Sends an authenticated command; returns the low byte of its counter,
    // which HA echoes in the RESULT message.
    function send(op, payload) {
      var frame = _command(op, payload);
      _queueRaw(frame);
      return frame[1];
    }

    hidden function _command(op, payload) {
      _ctr += 1;
      var msg = []b;
      msg.addAll(_nonce);
      msg.addAll([(_ctr >> 24) & 0xff, (_ctr >> 16) & 0xff, (_ctr >> 8) & 0xff, _ctr & 0xff]b);
      msg.add(op);
      msg.addAll(payload);
      var tag = hmacSha256(_key, msg).slice(0, 4);
      var frame = [op, _ctr & 0xff]b;
      frame.addAll(payload);
      frame.addAll(tag);
      return frame;
    }

    hidden function _queueRaw(frame) {
      _writeQueue.add(frame);
      _pump();
    }

    hidden function _pump() {
      if (_writing || _cmd == null || _writeQueue.size() == 0) {
        return;
      }
      var frame = _writeQueue[0];
      _writeQueue = _writeQueue.slice(1, null);
      _writing = true;
      try {
        _cmd.requestWrite(frame, { :writeType => Ble.WRITE_TYPE_WITH_RESPONSE });
      } catch (e) {
        _writing = false;
        _fail(BleError.BLE_WRITE_FAILED);
      }
    }
  }

  // HMAC-SHA256 (RFC 2104) on top of Cryptography.Hash; key must be <= 64 bytes.
  function hmacSha256(key, msg) {
    var ipad = new [64]b;
    var opad = new [64]b;
    for (var i = 0; i < 64; i++) {
      var k = i < key.size() ? key[i] : 0;
      ipad[i] = k ^ 0x36;
      opad[i] = k ^ 0x5c;
    }
    var h = new Crypto.Hash({ :algorithm => Crypto.HASH_SHA256 });
    h.update(ipad);
    h.update(msg);
    var inner = h.digest();
    h.update(opad);
    h.update(inner);
    return h.digest();
  }
}
