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

  const CONNECT_ATTEMPT_MS = 15000;
  const DISCOVERY_WAIT_MS = 8000;
  const STORAGE_BOND_TRY = "ble/bondTry";
  const BOND_WAIT_MS = 40000;  // user confirms the code on the watch and in HA
  const ENCRYPTION_WAIT_MS = 5000;  // bonded link: LTK encryption after connect
  const APPROVAL_WAIT_MS = 600000;  // how long the app waits for approval in HA
  const APPROVAL_POLL_MS = 3000;    // HELLO retry while waiting for approval
  const STORAGE_BLE_KEY = "ble/key";    // per-watch command key from HA, hex
  const PROTOCOL_VERSION = 2;

  const MSG_CHALLENGE = 0x81;
  const MSG_ENTITY = 0x82;
  const MSG_LIST_END = 0x83;
  const MSG_RESULT = 0x84;
  const MSG_KEY = 0x85;

  const ST_OK = 0;
  const ST_BAD_AUTH = 1;
  const ST_BAD_INDEX = 2;
  const ST_NOT_ALLOWED = 3;
  const ST_SERVICE_ERROR = 4;
  const ST_NO_SESSION = 6;
  const ST_NOT_PAIRED = 7;
  const ST_NOT_APPROVED = 8;

  // Link states
  enum {
    LINK_IDLE,
    LINK_REGISTERING,
    LINK_SCANNING,
    LINK_CONNECTING,
    LINK_BONDING,
    LINK_DISCOVERING,
    LINK_ENCRYPTING,
    LINK_SUBSCRIBING,
    LINK_HELLO,
    LINK_APPROVAL,
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
    hidden var _reasm = new Reassembler();
    hidden var _connectStarted = 0;
    hidden var _connectTries = 0;
    hidden var _cccd = null;
    hidden var _discoveryStarted = 0;
    hidden var _rebonded = false;
    hidden var _unbonded = false;    // the connected HA had no bond: this link pairs    // stale bond already dropped once this attempt
    hidden var _encWaitStarted = 0;
    hidden var _cccdRetried = false;
    hidden var _approvalStarted = 0;
    hidden var _helloAt = 0;
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

    hidden function _setState(s) {
      if (s != _state) {
        _state = s;
        _listener.onLinkStatus(s);
      }
    }

    // true while the current link has to pair first (for the progress text)
    function isPairing() {
      return _unbonded;
    }

    function isReady() {
      return _state == LINK_READY;
    }

    // Loads the per-watch key HA issued (MSG_KEY); null until approved.
    function loadKey() {
      var hex = App.Storage.getValue(STORAGE_BLE_KEY);
      _key = null;
      if (hex instanceof Lang.String && hex.length() == 32) {
        try {
          _key = StringUtil.convertEncodedString(hex, {
            :fromRepresentation => StringUtil.REPRESENTATION_STRING_HEX,
            :toRepresentation => StringUtil.REPRESENTATION_BYTE_ARRAY
          });
        } catch (e) {
          _key = null;
        }
      }
      return _key != null;
    }

    hidden function _storeKey(key) {
      _key = key;
      App.Storage.setValue(STORAGE_BLE_KEY, StringUtil.convertEncodedString(key, {
        :fromRepresentation => StringUtil.REPRESENTATION_BYTE_ARRAY,
        :toRepresentation => StringUtil.REPRESENTATION_STRING_HEX
      }));
    }

    function forgetKey() {
      _key = null;
      App.Storage.deleteValue(STORAGE_BLE_KEY);
    }

    // Starts (or resumes) the connection sequence. Safe to call repeatedly.
    // An unbonded link pairs right away: HA accepts every bond and asks for
    // approval separately.
    function start() {
      if (_state != LINK_IDLE && _state != LINK_FAILED) {
        return;
      }
      loadKey();
      Ble.setDelegate(self);
      _setSecureStrategy();
      _logSystemDevices();
      if (_profileRegistered) {
        _connect();
        return;
      }
      _setState(LINK_REGISTERING);
      Utils.debugLog("BLE: registering profile", null, null);
      try {
        Ble.registerProfile({
          :uuid => _svcUuid,
          :characteristics => [
            { :uuid => _cmdUuid },  // write-only: no descriptors (an empty list is rejected)
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
      _release();
      _reset(LINK_IDLE);
    }

    // Drops our hold on the device. Ble.unpairDevice() also deletes the
    // system bond (Fenix 7: bonded=1 before, 0 right after a failure path
    // called it), so a bonded HA is left alone; HA drops the idle link.
    hidden function _release() {
      if (_device != null && !((_device has :isBonded) && _device.isBonded())) {
        try {
          Ble.unpairDevice(_device);
        } catch (e) {
        }
      }
    }

    // The watch's bond is stale (HA forgot this watch or lost its keys):
    // delete it and pair again, once per attempt. HA then asks for approval.
    hidden function _repair() {
      if (_rebonded || _device == null) {
        return false;
      }
      _rebonded = true;
      Utils.debugLog("BLE: stale bond, pairing again", null, null);
      forgetKey();
      try {
        Ble.unpairDevice(_device);
      } catch (e) {
      }
      _reset(LINK_IDLE);
      start();
      return true;
    }

    hidden function _reset(newState) {
      _setState(newState);
      _device = null;
      _cmd = null;
      _cccd = null;
      _writeQueue = [];
      _writing = false;
      _nonce = null;
      _ctr = 0;
      _reasm = new Reassembler();
      _cccdRetried = false;
      _unbonded = false;
      if (newState == LINK_READY || newState == LINK_FAILED) {
        _rebonded = false;
      }
    }

    hidden function _fail(code) {
      Utils.debugLog("BLE: fail code=", code, null);
      Utils.saveLog();
      Ble.setScanState(Ble.SCAN_STATE_OFF);
      _release();
      _reset(LINK_FAILED);
      _listener.onLinkError(code);
    }

    // A bonded HA is reconnected directly (getBondedDevices() returns
    // ScanResults usable with pairDevice()); otherwise scan for its service.
    hidden function _connect() {
      if (Ble has :getBondedDevices) {
        var it = Ble.getBondedDevices();
        var r = it.next();
        if (r != null) {
          Utils.debugLog("BLE: connecting to bonded HA", null, null);
          _pairWith(r);
          return;
        }
      }
      _startScan();
    }

    hidden function _pairWith(r) {
      Ble.setScanState(Ble.SCAN_STATE_OFF);
      _setState(LINK_CONNECTING);
      _connectStarted = System.getTimer();
      _connectTries += 1;
      try {
        _device = Ble.pairDevice(r);
      } catch (e) {
        _fail(BleError.BLE_CONNECT_FAILED);
      }
    }

    hidden function _startScan() {
      _setState(LINK_SCANNING);
      Utils.debugLog("BLE: scanning", null, null);
      Ble.setScanState(Ble.SCAN_STATE_SCANNING);
    }

    // Called periodically by the client while a connection is pending; a
    // stalled attempt is abandoned after CONNECT_ATTEMPT_MS and rescanned.
    // Keep this long: the central scans with a low duty cycle while
    // connecting, so against BlueZ's default 1.28 s advertising interval a
    // connection took ~10 s to form (HCI trace), and a shorter timeout
    // cancelled attempts that were about to succeed. The integration now
    // advertises every 60-100 ms to shorten that.
    function checkTimeout(now) {
      if (_state == LINK_DISCOVERING && _device != null) {
        if (now - _discoveryStarted > DISCOVERY_WAIT_MS) {
          Utils.debugLog("BLE: service never listed", null, null);
          _findServiceElsewhere(_device, true);
          _fail(BleError.BLE_CONNECT_FAILED);
        } else {
          _subscribe(_device);
        }
        return;
      }
      if (_state == LINK_BONDING && now - _connectStarted > BOND_WAIT_MS) {
        Utils.debugLog("BLE: pairing timed out", null, null);
        _fail(BleError.BLE_PAIR_FAILED);
        return;
      }
      if (_state == LINK_APPROVAL) {
        if (now - _approvalStarted > APPROVAL_WAIT_MS) {
          _fail(BleError.BLE_NOT_APPROVED);
        } else if (now - _helloAt > APPROVAL_POLL_MS) {
          _hello();
        }
        return;
      }
      if (_state == LINK_ENCRYPTING && now - _encWaitStarted > ENCRYPTION_WAIT_MS) {
        // no encryption status arrived: retry the subscription once anyway
        _enableNotify();
        return;
      }
      if (_state == LINK_CONNECTING && now - _connectStarted > CONNECT_ATTEMPT_MS) {
        Utils.debugLog("BLE: connect attempt timed out, rescanning; tries=", _connectTries, null);
        _release();
        _device = null;
        _startScan();
      }
    }

    // ---- BleDelegate callbacks ------------------------------------------

    function onProfileRegister(uuid, status) {
      Utils.debugLog("BLE: profile registered status=", status, null);
      if (status != Ble.STATUS_SUCCESS) {
        _fail(BleError.BLE_UNSUPPORTED);
        return;
      }
      _profileRegistered = true;
      if (_state == LINK_REGISTERING) {
        _connect();
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
            Utils.debugLog("BLE: found HA, rssi=", r.getRssi(), ", connecting");
            _pairWith(r);
            return;
          }
        }
      }
    }

    function onConnectedStateChanged(device, state) {
      Utils.debugLog("BLE: connected state=", state, " link=" + _state);
      if (state == Ble.CONNECTION_STATE_CONNECTED) {
        // Besides the connection we asked for, the system reconnects on its
        // own to a device this app instance has paired (e.g. after HA drops
        // an idle link). Adopt those too: HA stops advertising while a
        // central is connected, so scanning for it again would never succeed.
        if (_state != LINK_CONNECTING && _state != LINK_SCANNING
            && _state != LINK_IDLE && _state != LINK_FAILED) {
          return;
        }
        Ble.setScanState(Ble.SCAN_STATE_OFF);
        _device = device;
        _unbonded = !((device has :isBonded) && device.isBonded());
        Utils.debugLog("BLE: connected after tries=", _connectTries, null);
        _connectTries = 0;
        _secure(device);
      } else if (_state == LINK_SCANNING || _state == LINK_REGISTERING) {
        // late disconnect of a device already let go (e.g. after _repair())
        return;
      } else if (_state != LINK_IDLE) {
        // HA dropped us (idle timeout) or radio lost: next request reconnects
        _reset(LINK_IDLE);
        _listener.onLinkDown();
      }
    }

    // Order after connecting: find the service, then (if not bonded yet)
    // bond, then enable notifications.
    //
    // Findings on a Fenix 7 (fw 27.18, CIQ 6.0.2):
    // - Under CONNECTION_STRATEGY_SECURE_PAIR_BOND the system pairs by
    //   itself (LE Secure Connections, Numeric Comparison), but getService()
    //   then stays null for good, even after encryption succeeds and although
    //   the watch discovered HA's service. So the default strategy is used and
    //   the service is looked up before bonding.
    // - Device.requestBond() while that strategy was active crashed the app
    //   with an uncatchable System Error. A Storage flag set around the call
    //   stops a crash from repeating: if it is still set on the next attempt,
    //   bonding is skipped and the link stays unencrypted (every command is
    //   authenticated by the protocol either way).
    hidden function _secure(device) {
      _subscribe(device);
    }

    hidden function _subscribe(device) {
      var svc = device.getService(_svcUuid);
      if (svc == null) {
        // another Device object for the same peer may carry the services
        var alt = _findServiceElsewhere(device, _state != LINK_DISCOVERING);
        if (alt != null) {
          Utils.debugLog("BLE: using service from another Device object", null, null);
          _device = alt;
          device = alt;
          svc = alt.getService(_svcUuid);
        }
      }
      if (svc == null) {
        if (_state != LINK_DISCOVERING) {
          Utils.debugLog("BLE: service not listed yet, waiting", null, null);
          _setState(LINK_DISCOVERING);
          _discoveryStarted = System.getTimer();
        }
        return;
      }
      _cmd = svc.getCharacteristic(_cmdUuid);
      var evt = svc.getCharacteristic(_evtUuid);
      _cccd = evt != null ? evt.getDescriptor(Ble.cccdUuid()) : null;
      if (_cmd == null || _cccd == null) {
        Utils.debugLog("BLE: characteristic missing", null, null);
        _fail(BleError.BLE_CONNECT_FAILED);
        return;
      }
      var bonded = (device has :isBonded) ? device.isBonded() : false;
      var guard = App.Storage.getValue(STORAGE_BOND_TRY) == true;
      Utils.debugLog("BLE: bonded=", bonded, " bondGuard=" + guard);
      if (bonded) {
        // HA's characteristics need an encrypted link; the system encrypts
        // bonded links with the stored key (HA also sends a security
        // request). A subscription that races ahead of it is retried from
        // onDescriptorWrite.
        _enableNotify();
        return;
      }
      if (guard || !(device has :requestBond)) {
        // the last requestBond() did not come back (app crash) or the API is
        // missing: let the protected subscription trigger pairing instead
        Utils.debugLog("BLE: no requestBond, subscribing to trigger pairing", null, null);
        _enableNotify();
        return;
      }
      _setState(LINK_BONDING);
      _connectStarted = System.getTimer();
      App.Storage.setValue(STORAGE_BOND_TRY, true);
      Utils.saveLog();
      Utils.debugLog("BLE: requesting bond", null, null);
      device.requestBond();
    }

    function onEncryptionStatus(device, status) {
      Utils.debugLog("BLE: encryption status=", status, " link=" + _state);
      App.Storage.deleteValue(STORAGE_BOND_TRY);
      if (_state == LINK_BONDING) {
        if (status == Ble.STATUS_SUCCESS) {
          _enableNotify();
        } else {
          _fail(BleError.BLE_PAIR_FAILED);
        }
      } else if (_state == LINK_ENCRYPTING && status == Ble.STATUS_SUCCESS) {
        _enableNotify();
      }
    }

    hidden function _logSystemDevices() {
      var paired = 0;
      var it = Ble.getPairedDevices();
      for (var d = it.next(); d != null; d = it.next()) {
        paired += 1;
      }
      var bonded = -1;
      if (Ble has :getBondedDevices) {
        bonded = 0;
        it = Ble.getBondedDevices();
        for (var d = it.next(); d != null; d = it.next()) {
          bonded += 1;
        }
      }
      Utils.debugLog("BLE: system paired=", paired, " bonded=" + bonded);
    }

    // Diagnostics for "service never listed" (Fenix 7, bonded link): logs
    // what the Device objects the system hands out actually contain, and
    // returns one that has HA's service if the callback's object does not.
    hidden function _findServiceElsewhere(device, verbose) {
      if (verbose) {
        _logDevice("cb", device);
      }
      // getBondedDevices() yields ScanResults, not Devices (a Device call on
      // one crashed the app), so only the paired list is searched
      var it = Ble.getPairedDevices();
      for (var d = it.next(); d != null; d = it.next()) {
        if (verbose) {
          _logDevice("paired", d);
        }
        if (d.getService(_svcUuid) != null) {
          return d;
        }
      }
      return null;
    }

    hidden function _logDevice(tag, d) {
      var n = 0;
      var names = "";
      var it = d.getServices();
      for (var sv = it.next(); sv != null; sv = it.next()) {
        n += 1;
        names += " " + sv.getUuid().toString().substring(0, 8);
      }
      Utils.debugLog("BLE: dev[" + tag + "] conn=" + d.isConnected()
        + " bond=" + ((d has :isBonded) ? d.isBonded() : "?")
        + " name=" + d.getName(), " svcs=" + n, names);
    }

    // Always the default strategy. Under CONNECTION_STRATEGY_SECURE_PAIR_BOND
    // a Fenix 7 (fw 27.18, CIQ 6.0.2) pairs and encrypts, but every Device
    // object then reports zero services (getServices() empty, getService()
    // null) although the watch discovered HA's GATT database. Bonding is
    // requested explicitly instead, after the service lookup (_subscribe).
    hidden function _setSecureStrategy() {
      if ((Ble has :setConnectionStrategy) && (Ble has :CONNECTION_STRATEGY_DEFAULT)) {
        Ble.setConnectionStrategy(Ble.CONNECTION_STRATEGY_DEFAULT);
        Utils.debugLog("BLE: strategy=default", null, null);
      }
    }

    hidden function _securityStatus(status) {
      return status == Ble.STATUS_GATT_INSUFFICIENT_ENCRYPTION_FAIL
        || status == Ble.STATUS_ENCRYPTION_BOND_FAIL
        || status == Ble.STATUS_ENCRYPTION_SECURITY_INSUFFICIENT
        || status == Ble.STATUS_ENCRYPTION_PEER_KEYS_LOST;
    }

    hidden function _enableNotify() {
      _setState(LINK_SUBSCRIBING);
      Utils.debugLog("BLE: subscribing", null, null);
      _cccd.requestWrite([0x01, 0x00]b);
    }

    function onDescriptorWrite(descriptor, status) {
      Utils.debugLog("BLE: descriptor write status=", status, null);
      if (_state != LINK_SUBSCRIBING) {
        return;
      }
      if (status != Ble.STATUS_SUCCESS) {
        // HA's CCCD needs an LE Secure Connections encrypted link. On a bonded
        // link the write may run before encryption is up: wait for the
        // encryption status (or ENCRYPTION_WAIT_MS) and retry once.
        if (!_cccdRetried && _device != null && (_device has :isBonded) && _device.isBonded()) {
          _cccdRetried = true;
          _setState(LINK_ENCRYPTING);
          _encWaitStarted = System.getTimer();
          Utils.debugLog("BLE: subscription refused, waiting for encryption", null, null);
          return;
        }
        if (_securityStatus(status)) {
          if (!_repair()) {
            _fail(BleError.BLE_NOT_PAIRED);
          }
          return;
        }
        _fail(BleError.BLE_CONNECT_FAILED);
        return;
      }
      Utils.debugLog("BLE: subscribed, HELLO key=", _key != null, null);
      _approvalStarted = System.getTimer();
      _hello();
    }

    // HELLO: version, flags (bit 0: this watch already has its key)
    hidden function _hello() {
      if (_state != LINK_APPROVAL) {
        _setState(LINK_HELLO);
      }
      _helloAt = System.getTimer();
      _queueRaw([OP_HELLO, PROTOCOL_VERSION, _key != null ? 1 : 0]b);
    }

    function onCharacteristicWrite(characteristic, status) {
      if (status != Ble.STATUS_SUCCESS) {
        Utils.debugLog("BLE: write status=", status, null);
      }
      _writing = false;
      if (status != Ble.STATUS_SUCCESS) {
        _fail(BleError.BLE_WRITE_FAILED);
        return;
      }
      _pump();
    }

    function onCharacteristicChanged(characteristic, value) {
      var msg = _reasm.feed(value);
      if (msg != null) {
        _onMessage(msg);
      }
    }

    // ---- messages ----------------------------------------------------------

    hidden function _onMessage(msg) {
      if (msg.size() == 0) {
        return;
      }
      var hello = _state == LINK_HELLO || _state == LINK_APPROVAL;
      if (msg[0] == MSG_RESULT && hello && msg.size() >= 3) {
        if (msg[2] == ST_NOT_APPROVED) {
          // bonded, waiting for the user to approve this watch in HA
          if (_state != LINK_APPROVAL) {
            Utils.debugLog("BLE: waiting for approval in HA", null, null);
            _setState(LINK_APPROVAL);
          }
          return;
        }
        // NOT_PAIRED: HA does not know this watch (bond missing on HA's
        // side, or the watch was forgotten there); anything else: protocol
        forgetKey();
        if (msg[2] == ST_NOT_PAIRED && _repair()) {
          return;
        }
        _fail(msg[2] == ST_NOT_PAIRED ? BleError.BLE_NOT_PAIRED : BleError.BLE_PROTOCOL);
        return;
      }
      if (msg[0] == MSG_KEY && hello) {
        if (msg.size() >= 17) {
          Utils.debugLog("BLE: key received", null, null);
          _storeKey(msg.slice(1, 17));
        }
        return;
      }
      if (msg[0] == MSG_RESULT && _state == LINK_READY && msg.size() >= 3 && msg[2] == ST_BAD_AUTH) {
        // key out of date (watch forgotten and approved again in HA): drop
        // it and ask again; HA only hands it out over this bonded link
        Utils.debugLog("BLE: bad auth, renewing key", null, null);
        forgetKey();
        _listener.onMessage(msg);
        _nonce = null;
        _hello();
        _listener.onLinkDown();
        return;
      }
      if (msg[0] == MSG_CHALLENGE && hello) {
        if (_key == null) {
          _fail(BleError.BLE_PROTOCOL);
          return;
        }
        if (msg.size() < 11) {
          _fail(BleError.BLE_PROTOCOL);
          return;
        }
        _nonce = msg.slice(1, 9);
        _ctr = 0;
        _setState(LINK_READY);
        Utils.debugLog("BLE: session ready, entities=", msg[10], null);
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
      return buildCommand(_key, _nonce, _ctr, op, payload);
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

  // Authenticated watch->HA frame: op, ctr8, payload, 4-byte truncated
  // HMAC-SHA256(key, nonce || ctr_be32 || op || payload). See PROTOCOL.md.
  function buildCommand(key, nonce, ctr, op, payload) {
    var msg = []b;
    msg.addAll(nonce);
    msg.addAll([(ctr >> 24) & 0xff, (ctr >> 16) & 0xff, (ctr >> 8) & 0xff, ctr & 0xff]b);
    msg.add(op);
    msg.addAll(payload);
    var frame = [op, ctr & 0xff]b;
    frame.addAll(payload);
    frame.addAll(hmacSha256(key, msg).slice(0, 4));
    return frame;
  }

  // Rebuilds HA->watch messages from <=20-byte notifications:
  // byte 0 = bit 7 last fragment, bits 0..6 message sequence.
  class Reassembler {
    hidden var _buf = null;
    hidden var _seq = -1;

    function initialize() {
    }

    function feed(value) {
      if (value == null || value.size() < 1) {
        return null;
      }
      var hdr = value[0];
      var seq = hdr & 0x7f;
      if (_buf == null || seq != _seq) {
        _buf = []b;   // new message, or a fragment was lost: start over
        _seq = seq;
      }
      _buf.addAll(value.slice(1, null));
      if ((hdr & 0x80) != 0) {
        var msg = _buf;
        _buf = null;
        _seq = -1;
        return msg;
      }
      return null;
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
