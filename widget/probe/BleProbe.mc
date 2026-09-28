using Toybox.Application as App;
using Toybox.WatchUi as Ui;
using Toybox.Graphics;
using Toybox.System;
using Toybox.Timer;
using Hass;

// Scripted end-to-end run of the real BLE client:
//   LIST -> toggle TEST_ENTITY on -> off -> GET -> battery -> done.
// Every step prints "PROBE <step> ok|FAIL ..." with elapsed milliseconds.
const TEST_ENTITY = "input_boolean.garmin_ble_test";

class BleProbeApp extends App.AppBase {
  var steps = 0;
  var t0 = 0;
  var tStep = 0;
  var failed = 0;

  function initialize() {
    AppBase.initialize();
  }

  function getInitialView() {
    Hass.initClient();
    t0 = System.getTimer();
    tStep = t0;
    System.println("PROBE start");
    Hass.client.getEntity(Hass.LIST_ID, null, method(:onList));
    return [new BleProbeView()];
  }

  function onStop(state) {
    Hass.client.shutdown();
  }

  function mark(step, err, extra) {
    var now = System.getTimer();
    var ok = err == null;
    if (!ok) {
      failed++;
    }
    System.println("PROBE " + step + " " + (ok ? "ok" : "FAIL " + err.toString()) + " ms=" + (now - tStep)
      + " total=" + (now - t0) + (extra != null ? " " + extra : ""));
    tStep = now;
  }

  function onList(err, data) {
    var ids = null;
    if (err == null) {
      ids = data[:body]["attributes"]["entity_id"];
    }
    mark("list", err, "ids=" + ids);
    if (err != null) {
      finish();
      return;
    }
    Hass.client.setEntityState(TEST_ENTITY, "input_boolean", Hass.Client.ENTITY_ACTION_TURN_ON, method(:onOn));
  }

  function onOn(err, data) {
    mark("turn_on", err, null);
    Hass.client.setEntityState(TEST_ENTITY, "input_boolean", Hass.Client.ENTITY_ACTION_TURN_OFF, method(:onOff));
  }

  function onOff(err, data) {
    mark("turn_off", err, null);
    Hass.client.getEntity(TEST_ENTITY, null, method(:onGet));
  }

  function onGet(err, data) {
    mark("get", err, err == null ? "state=" + data[:body]["state"] : null);
    Hass.client.reportBatteryValue(null, method(:onBattery));
  }

  function onBattery(err, data) {
    mark("battery", err, null);
    finish();
  }

  function finish() {
    System.println("PROBE done failed=" + failed + " total=" + (System.getTimer() - t0));
    Hass.client.shutdown();
  }
}

class BleProbeView extends Ui.View {
  function initialize() {
    View.initialize();
  }

  // No text: the simulator device bundle here has no fonts.
  function onUpdate(dc) {
    dc.setColor(Graphics.COLOR_BLACK, Graphics.COLOR_BLACK);
    dc.clear();
    dc.setColor(Graphics.COLOR_BLUE, Graphics.COLOR_BLACK);
    dc.fillCircle(dc.getWidth() / 2, dc.getHeight() / 2, 20);
  }
}
