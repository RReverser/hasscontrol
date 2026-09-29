using Toybox.Application as App;
using Toybox.Communications;
using Toybox.StringUtil;
using Toybox.System;
using Toybox.Timer;
using Toybox.Lang;
using Utils;

// Drop-in replacement for the original HTTP/OAuth client: same public methods
// and the same callback data shapes (HA REST-style dictionaries), but the
// transport is a direct BLE link to the `garmin_ble` Home Assistant
// integration. The rest of the app is unchanged.
module Hass {
  // Pseudo entity id the app asks for when importing: answered with the list
  // of entities HA exposes (those carrying the configured label).
  const LIST_ID = "ble.exposed";

  const REQUEST_TIMEOUT_MS = 10000;
  const CONNECT_TIMEOUT_MS = 40000;
  const TICK_MS = 50;
  const RETRY_DELAY_MS = 2000;
  const KEEPALIVE_MS = 20000;  // under HA's 30 s idle timeout
  // Set once a session completed a LIST with HA; cleared by logout.
  const STORAGE_PAIRED = "ble/paired";

  class Client {
    static enum {
      ENTITY_ACTION_TURN_ON,
      ENTITY_ACTION_TURN_OFF,
      ENTITY_ACTION_LOCK,
      ENTITY_ACTION_UNLOCK,
      ENTITY_ACTION_CLOSE,
      ENTITY_ACTION_OPEN,
      ENTITY_ACTION_COVER_TOGGLE,
      ENTITY_ACTION_PRESS
    }

    hidden var _link;
    hidden var _ready = false;
    hidden var _listing = false;
    hidden var _listFresh = false; // a LIST completed on this connection and was not consumed yet
    hidden var _cache = {};      // entity_id -> REST-style body
    hidden var _index = {};      // entity_id -> idx for this connection
    hidden var _ids = [];        // exposed ids in HA order
    hidden var _ops = [];        // requests waiting for the link / for LIST
    hidden var _pendingCtr = {}; // ctr8 -> { :cb, :data, :deadline }
    hidden var _pendingGet = {}; // idx -> [ { :cb, :ctx, :deadline } ]
    hidden var _deferred = [];   // [cb, err, data] invoked from the timer
    hidden var _timer = null;
    hidden var _timerRunning = false;
    hidden var _connectDeadline = null;
    hidden var _statusText = null;
    hidden var _wasConnected = false; // reached HA during the current wait
    hidden var _approvalLinkSent = false;
    hidden var _retryAt = null;
    hidden var _live = false;      // app in use: keep a session for live states
    hidden var _keepArmed = false; // _timer currently runs the keep-alive
    hidden var _bgList = false;    // the running LIST was not asked for by a request    // connection retry scheduled (reconnect loop)

    function initialize() {
      _link = new BleLink(self);
      _timer = new Timer.Timer();
    }

    // ---- API used by the rest of the app ------------------------------------

    function onSettingsChanged() {
      shutdown();
    }

    // Nothing to configure: the watch gets its key from HA after pairing.
    function validateSettings(errorCallback) {
      return null;
    }

    // "Logged in" = paired: a session with HA completed at least once since
    // the last logout.
    function isLoggedIn() {
      return App.Storage.getValue(STORAGE_PAIRED) == true;
    }

    // Connect (pairing first if needed) and complete a session with HA.
    function login(callback) {
      _enqueue({ :k => :login, :cb => callback });
    }

    // Forget the pairing on the watch side. The system keeps its LE bond
    // keys (Connect IQ has no API to delete them), so this returns the app
    // to its unpaired start state; HA keeps its side of the bond too.
    function logout() {
      App.Storage.deleteValue(STORAGE_PAIRED);
      _link.forgetKey();
      shutdown();
    }

    // Runs `cb` from the client timer, i.e. outside the current Ui callback.
    function later(cb) {
      _defer(cb, null, null);
      _ensureTick();
    }

    // Progress text, one step per thing the user can relate to:
    // Searching (finding HA) -> Pairing (only when this link has no bond)
    // or Connecting (bonded) -> Approve in HA (only while HA waits for it).
    function onLinkStatus(state) {
      var text = null;
      if (!_ready && _ops.size() > 0) {
        if (state == LINK_REGISTERING || state == LINK_SCANNING || state == LINK_CONNECTING) {
          // HA was already reached and the link dropped mid-setup: this is a
          // reconnect, not a search for an unknown HA
          text = _wasConnected ? "Connecting" : "Searching";
        } else if (state == LINK_APPROVAL) {
          text = "Approve in HA";
          _openApprovalOnPhone();
        } else if (state == LINK_BONDING || state == LINK_DISCOVERING || state == LINK_ENCRYPTING
                   || state == LINK_SUBSCRIBING || state == LINK_HELLO) {
          text = _link.isPairing() ? "Pairing" : "Connecting";
          _wasConnected = true;
        } else if (_retryAt != null || _connectDeadline != null) {
          text = _wasConnected ? "Connecting" : "Searching";  // between attempts
        }
      }
      if (text == null) {
        _wasConnected = false;  // wait over (ready, failed or nothing pending)
      }
      if (text == null ? _statusText == null : text.equals(_statusText)) {
        return;  // same step: no redraw
      }
      _statusText = text;
      Hass.onLinkStatus(text);
    }

    // Once per app run: ask Garmin Connect on the phone for a notification
    // that opens HA's integrations page (where the approval card is). Garmin
    // Connect opens it in the browser, so custom schemes such as the
    // companion app's homeassistant:// do not work; the My Home Assistant
    // link redirects to the user's own instance. Needs the phone connected
    // to the watch; without it nothing happens and the watch keeps waiting.
    hidden function _openApprovalOnPhone() {
      if (_approvalLinkSent) {
        return;
      }
      _approvalLinkSent = true;
      try {
        Communications.openWebPage("https://my.home-assistant.io/redirect/integrations/", null, null);
        Utils.debugLog("BLE: approval link sent to phone", null, null);
      } catch (e) {
        Utils.debugLog("BLE: approval link failed", null, null);
      }
    }

    function shutdown() {
      _live = false;
      _link.stop();
      _onDown(new BleError(BleError.BLE_CONNECT_FAILED));
    }

    function getEntity(entityId, context, callback) {
      if (validateSettings(callback) != null) {
        return;
      }
      if (context == null) {
        context = {};
      }
      _enqueue({ :k => :get, :id => entityId, :ctx => context, :cb => callback });
    }

    function setEntityState(entityId, entityType, action, callback) {
      if (validateSettings(callback) != null) {
        return;
      }
      var newState = null;
      if (action == null) {
        action = ENTITY_ACTION_TURN_ON; // scenes
      } else if (action == ENTITY_ACTION_TURN_ON) {
        newState = "on";
      } else if (action == ENTITY_ACTION_TURN_OFF) {
        newState = "off";
      } else if (action == ENTITY_ACTION_CLOSE) {
        newState = "closed";
      } else if (action == ENTITY_ACTION_OPEN) {
        newState = "open";
      } else if (action == ENTITY_ACTION_LOCK) {
        newState = "locked";
      } else if (action == ENTITY_ACTION_UNLOCK) {
        newState = "unlocked";
      } else if (action == ENTITY_ACTION_PRESS) {
        newState = "timestamp";
      }
      _enqueue({
        :k => :action, :id => entityId, :action => action, :arg => []b, :cb => callback,
        :data => { :context => { :entityId => entityId, :state => newState } }
      });
    }

    (:fullmem)
    function callService(domain, service, entityId, extraParams, callback) {
      if (validateSettings(callback) != null) {
        return;
      }
      var op = { :k => :action, :id => entityId, :cb => callback,
                 :data => { :context => { :entityId => entityId, :extraParams => extraParams } } };
      if (service.equals("select_option")) {
        op[:action] = 0x10;
        op[:option] = extraParams["option"];
      } else if (service.equals("set_value")) {
        var b = new [4]b;
        b.encodeNumber(extraParams["value"].toFloat(), Lang.NUMBER_FORMAT_FLOAT,
                       { :offset => 0, :endianness => Lang.ENDIAN_BIG });
        op[:action] = 0x11;
        op[:arg] = b;
      } else {
        callback.invoke(new BleError(BleError.BLE_NOT_ALLOWED), null);
        return;
      }
      _enqueue(op);
    }

    function reportBatteryValue(entityId, callback) {
      var stats = System.getSystemStats();
      var pct = stats.battery.toNumber();
      var charging = (stats has :charging) && stats.charging ? 1 : 0;
      _enqueue({ :k => :battery, :arg => [pct & 0xff, charging]b, :cb => callback,
                 :data => { :context => { :entityId => entityId, :state => pct } } });
    }

    // ---- queueing ------------------------------------------------------------

    hidden function _enqueue(op) {
      _live = true;
      op[:deadline] = System.getTimer() + CONNECT_TIMEOUT_MS + REQUEST_TIMEOUT_MS;
      _ops.add(op);
      if (_ready) {
        _drain();
      } else {
        if (_connectDeadline == null) {
          _connectDeadline = System.getTimer() + CONNECT_TIMEOUT_MS;
        }
        _link.start();
      }
      _ensureTick();
    }

    hidden function _drain() {
      while (_ready && !_listing && _ops.size() > 0) {
        var op = _ops[0];
        _ops = _ops.slice(1, null);
        _run(op);
      }
    }

    hidden function _run(op) {
      var k = op[:k];
      if (k == :login) {
        _defer(op[:cb], null, null);
      } else if (k == :get) {
        _runGet(op);
      } else if (k == :action) {
        var idx = _index[op[:id]];
        if (idx == null) {
          _defer(op[:cb], new BleError(BleError.BLE_UNKNOWN_ENTITY), null);
          return;
        }
        var payload = [idx, op[:action]]b;
        if (op[:option] != null) {
          var oi = _optionIndex(op[:id], op[:option]);
          if (oi < 0) {
            _defer(op[:cb], new BleError(BleError.BLE_UNKNOWN_ENTITY), null);
            return;
          }
          payload.add(oi);
        } else if (op[:arg] != null) {
          payload.addAll(op[:arg]);
        }
        _await(_link.send(OP_ACTION, payload), op);
      } else if (k == :battery) {
        _await(_link.send(OP_BATTERY, op[:arg]), op);
      }
    }

    hidden function _runGet(op) {
      var id = op[:id];
      if (id.equals(LIST_ID)) {
        if (op[:listed] == null && _listFresh) {
          // the list fetched right after connecting is still fresh: use it
          op[:listed] = true;
        }
        _listFresh = false;
        if (op[:listed] == null) {
          // fetch a fresh list, then answer this op from it
          op[:listed] = true;
          _ops = [op].addAll(_ops);
          _startList();
          return;
        }
        _defer(op[:cb], null, {
          :body => { "entity_id" => LIST_ID, "attributes" => { "entity_id" => _ids } },
          :context => op[:ctx]
        });
        return;
      }
      var body = _cache[id];
      if (body != null) {
        _defer(op[:cb], null, { :body => body, :context => op[:ctx] });
        return;
      }
      var idx = _index[id];
      if (idx == null) {
        // not exposed over BLE (e.g. a scene only listed in the settings)
        _defer(op[:cb], null, {
          :body => { "entity_id" => id, "state" => "unavailable", "attributes" => {} },
          :context => op[:ctx]
        });
        return;
      }
      var waiters = _pendingGet[idx];
      if (waiters == null) {
        waiters = [];
        _pendingGet[idx] = waiters;
        _link.send(OP_GET, [idx]b);
      }
      waiters.add({ :cb => op[:cb], :ctx => op[:ctx], :deadline => System.getTimer() + REQUEST_TIMEOUT_MS });
    }

    hidden function _startList() {
      _listing = true;
      _ids = [];
      _link.send(OP_LIST, []b);
    }

    hidden function _await(ctr8, op) {
      _pendingCtr[ctr8] = { :cb => op[:cb], :data => op[:data], :deadline => System.getTimer() + REQUEST_TIMEOUT_MS };
    }

    hidden function _optionIndex(id, option) {
      var body = _cache[id];
      if (body == null || body["attributes"]["options"] == null) {
        return -1;
      }
      var opts = body["attributes"]["options"];
      for (var i = 0; i < opts.size(); i++) {
        if (opts[i].equals(option)) {
          return i;
        }
      }
      return -1;
    }

    // ---- BleLink listener ------------------------------------------------------

    function onLinkReady(count) {
      _ready = true;
      _listFresh = false;
      _connectDeadline = null;
      _cache = {};
      _index = {};
      // the index map is per connection: always list first; a reconnect
      // made only to stay live pushes what it lists to the views
      _bgList = _ops.size() == 0;
      _startList();
    }

    // Radio-level failures (HA out of range, connection dropped) never end
    // on an error screen: the link retries while requests wait, showing
    // "Searching". Only failures the user must act on (pairing refused, not
    // approved, ...) fail the waiting requests.
    function onLinkError(code) {
      if (_ops.size() > 0 && _isRadioError(code)) {
        _failPending(new BleError(code));
        _retryAt = System.getTimer() + RETRY_DELAY_MS;
        _connectDeadline = null;
        _ensureTick();
        return;
      }
      _onDown(new BleError(code));
    }

    hidden function _isRadioError(code) {
      return code == BleError.BLE_NOT_FOUND || code == BleError.BLE_CONNECT_FAILED
        || code == BleError.BLE_TIMEOUT || code == BleError.BLE_WRITE_FAILED;
    }

    function onLinkDown() {
      _ready = false;
      _listing = false;
      // Requests in flight are lost with the link; queued ones reconnect.
      _failPending(new BleError(BleError.BLE_TIMEOUT));
      if (_ops.size() > 0) {
        _connectDeadline = System.getTimer() + CONNECT_TIMEOUT_MS;
        _link.start();
      }
    }

    function onMessage(msg) {
      var t = msg[0];
      Utils.debugLog("BLE: msg type=", t, " len=" + msg.size());
      if (t == MSG_ENTITY) {
        _onEntity(msg);
      } else if (t == MSG_LIST_END) {
        App.Storage.setValue(STORAGE_PAIRED, true);
        _listing = false;
        _listFresh = !_bgList;
        _bgList = false;
        _drain();
      } else if (t == MSG_RESULT && msg.size() >= 3) {
        var p = _pendingCtr[msg[1]];
        if (p != null) {
          _pendingCtr.remove(msg[1]);
          if (msg[2] == ST_OK) {
            _defer(p[:cb], null, p[:data]);
          } else {
            _defer(p[:cb], BleError.fromStatus(msg[2]), null);
          }
        }
      }
    }

    hidden function _onEntity(msg) {
      Utils.debugLog("BLE: entity ", msg.size() > 1 ? msg[1] : -1, null);
      if (msg.size() < 2) {
        return;
      }
      var idx = msg[1];
      var body = decodeEntity(msg);
      var id = body["entity_id"];
      if (id == null) {
        return;
      }
      _cache[id] = body;
      _index[id] = idx;
      if (_listing) {
        _ids.add(id);
        if (_bgList) {
          _defer(Utils.method(Hass, :onEntityPushed), null, { :body => body, :context => null });
        }
        return;
      }
      var waiters = _pendingGet[idx];
      if (waiters != null) {
        _pendingGet.remove(idx);
        for (var i = 0; i < waiters.size(); i++) {
          _defer(waiters[i][:cb], null, { :body => body, :context => waiters[i][:ctx] });
        }
      } else {
        // unsolicited: HA pushed a state change
        _defer(Utils.method(Hass, :onEntityPushed), null, { :body => body, :context => null });
      }
    }

    // ---- failure handling, deferral and timeouts ----------------------------------

    // Actions (and battery reports) must not fire long after the user asked
    // for them: they fail once their deadline passes without a connection.
    // Reads (the entity list, states) keep waiting for the link.
    hidden function _expireActions(now) {
      if (_ready) {
        return;
      }
      var keep = [];
      for (var i = 0; i < _ops.size(); i++) {
        var op = _ops[i];
        if ((op[:k] == :action || op[:k] == :battery) && now > op[:deadline]) {
          _defer(op[:cb], new BleError(BleError.BLE_NOT_FOUND), null);
        } else {
          keep.add(op);
        }
      }
      var expired = keep.size() < _ops.size();
      _ops = keep;
      if (expired && _ops.size() == 0) {
        _retryAt = null;
        _connectDeadline = null;
        _link.stop();
      }
    }

    hidden function _onDown(err) {
      _retryAt = null;
      _ready = false;
      _listing = false;
      _connectDeadline = null;
      var ops = _ops;
      _ops = [];
      for (var i = 0; i < ops.size(); i++) {
        _defer(ops[i][:cb], err, null);
      }
      _failPending(err);
    }

    hidden function _failPending(err) {
      var ks = _pendingCtr.keys();
      for (var i = 0; i < ks.size(); i++) {
        _defer(_pendingCtr[ks[i]][:cb], err, null);
      }
      _pendingCtr = {};
      ks = _pendingGet.keys();
      for (var i = 0; i < ks.size(); i++) {
        var w = _pendingGet[ks[i]];
        for (var j = 0; j < w.size(); j++) {
          _defer(w[j][:cb], err, null);
        }
      }
      _pendingGet = {};
    }

    // Callbacks run from the timer, never from inside a BLE callback or the
    // caller's stack: the app's refresh chain would otherwise recurse once per
    // entity when answers come from the cache.
    hidden function _defer(cb, err, data) {
      if (cb == null) {
        return;
      }
      _deferred.add([cb, err, data]);
      _ensureTick();
    }

    hidden function _ensureTick() {
      if (!_timerRunning) {
        if (_keepArmed) {
          _timer.stop();
          _keepArmed = false;
        }
        _timerRunning = true;
        _timer.start(method(:_tick), TICK_MS, true);
      }
    }

    function _tick() {
      var now = System.getTimer();
      _link.checkTimeout(now);
      var ls = _link.getState();
      if (_connectDeadline != null && (ls == LINK_BONDING || ls == LINK_APPROVAL)) {
        // pairing waits for the user to confirm the code on the watch, then
        // for approval in HA (BleLink bounds both)
        _connectDeadline = now + CONNECT_TIMEOUT_MS;
      }

      if (_connectDeadline != null && !_ready && now > _connectDeadline) {
        // no connection yet: start over (keeps "Searching" on screen)
        Utils.debugLog("BLE: still no connection, retrying", null, null);
        _link.stop();
        _retryAt = now;
        _connectDeadline = null;
      }
      if (_retryAt != null && now >= _retryAt) {
        _retryAt = null;
        if (!_ready && _ops.size() > 0) {
          _connectDeadline = now + CONNECT_TIMEOUT_MS;
          _link.start();
        }
      }
      _expireActions(now);

      var ks = _pendingCtr.keys();
      for (var i = 0; i < ks.size(); i++) {
        var p = _pendingCtr[ks[i]];
        if (now > p[:deadline]) {
          _pendingCtr.remove(ks[i]);
          _deferred.add([p[:cb], new BleError(BleError.BLE_TIMEOUT), null]);
        }
      }
      ks = _pendingGet.keys();
      for (var i = 0; i < ks.size(); i++) {
        var w = _pendingGet[ks[i]];
        if (w.size() > 0 && now > w[0][:deadline]) {
          _pendingGet.remove(ks[i]);
          for (var j = 0; j < w.size(); j++) {
            _deferred.add([w[j][:cb], new BleError(BleError.BLE_TIMEOUT), null]);
          }
        }
      }

      // run only what was queued before this tick; new work waits for the next
      var batch = _deferred;
      _deferred = [];
      for (var i = 0; i < batch.size(); i++) {
        batch[i][0].invoke(batch[i][1], batch[i][2]);
      }

      var ls2 = _link.getState();
      var linkBusy = !_ready && ls2 != LINK_IDLE && ls2 != LINK_FAILED;
      if (_deferred.size() == 0 && _pendingCtr.size() == 0 && _pendingGet.size() == 0
          && (_ops.size() == 0 || _ready) && !linkBusy && !_listing) {
        _timer.stop();
        _timerRunning = false;
        if (_live) {
          // one timer only (Connect IQ limits them): it doubles as the
          // keep-alive clock while nothing else is pending
          _keepArmed = true;
          _timer.start(method(:_keepAlive), KEEPALIVE_MS, false);
        }
      }
    }

    // While the app is open: keep the session (HA pushes every state change
    // over it), or quietly re-open it after HA or the radio dropped it.
    function _keepAlive() {
      _keepArmed = false;
      if (!_live) {
        return;
      }
      if (_ready) {
        _link.send(OP_PING, []b);
      } else if (isLoggedIn()) {
        // never pairs in the background: only a paired watch reconnects
        var st = _link.getState();
        if (st == LINK_IDLE || st == LINK_FAILED) {
          _link.start();
        }
      }
      _ensureTick();
    }
  }

  // ENTITY message (PROTOCOL.md) -> HA REST-style state body.
  function decodeEntity(msg) {
    var attrs = {};
    var body = { "attributes" => attrs };
    var i = 2;
    while (i + 2 <= msg.size()) {
      var tag = msg[i];
      var len = msg[i + 1];
      var s = _utf8(msg.slice(i + 2, i + 2 + len));
      if (tag == 1) {
        body["entity_id"] = s;
      } else if (tag == 2) {
        body["state"] = s;
      } else if (tag == 3) {
        attrs["friendly_name"] = s;
      } else if (tag == 4) {
        attrs["unit_of_measurement"] = s;
      } else if (tag == 5) {
        attrs["device_class"] = s;
      } else if (tag == 6) {
        attrs["icon"] = s;
      } else if (tag == 7) {
        attrs["options"] = _split(s, (0x1f).toChar());
      } else if (tag == 8) {
        attrs["min"] = s.toFloat();
      } else if (tag == 9) {
        attrs["max"] = s.toFloat();
      } else if (tag == 10) {
        attrs["step"] = s.toFloat();
      }
      i += 2 + len;
    }
    return body;
  }

  function _utf8(bytes) {
    if (bytes.size() == 0) {
      return "";
    }
    return StringUtil.convertEncodedString(bytes, {
      :fromRepresentation => StringUtil.REPRESENTATION_BYTE_ARRAY,
      :toRepresentation => StringUtil.REPRESENTATION_STRING_PLAIN_TEXT,
      :encoding => StringUtil.CHAR_ENCODING_UTF8
    });
  }

  function _split(s, sep) {
    var out = [];
    var chars = s.toCharArray();
    var start = 0;
    for (var i = 0; i <= chars.size(); i++) {
      if (i == chars.size() || chars[i] == sep) {
        out.add(s.substring(start, i));
        start = i + 1;
      }
    }
    return out;
  }
}
