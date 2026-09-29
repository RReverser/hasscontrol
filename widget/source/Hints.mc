using Toybox.Graphics as Gfx;
using Toybox.Math;
using Toybox.System;

// Button hints on round 5-button watches (Fenix, Epix, Forerunner...): a
// short arc on the rim next to the physical button plus a small glyph saying
// what it does. Positions follow those watches' layout, as clock angles:
// START/select at 2 o'clock, BACK at 4, UP (hold: menu) at about 9:30.
// Rectangular or touch-only devices get no hints.
module Hints {
  const SELECT_DEG = 60;
  const BACK_DEG = 120;
  const MENU_DEG = 285;

  enum {
    NONE,
    OK,       // check mark: confirm / pair
    RETRY,    // circular arrow: try again / refresh
    CLOSE,    // cross: back / cancel
    MENU      // three lines: menu
  }

  function available() {
    var s = System.getDeviceSettings();
    if ((s has :screenShape) && s.screenShape != System.SCREEN_SHAPE_ROUND) {
      return false;
    }
    if (s has :inputButtons) {
      return (s.inputButtons & System.BUTTON_INPUT_SELECT) != 0
        && (s.inputButtons & System.BUTTON_INPUT_UP) != 0;
    }
    return true;
  }

  function draw(dc, select, back, menu) {
    if (!available()) {
      return;
    }
    if (select != NONE) {
      _hint(dc, SELECT_DEG, select);
    }
    if (back != NONE) {
      _hint(dc, BACK_DEG, back);
    }
    if (menu) {
      _hint(dc, MENU_DEG, MENU);
    }
  }

  function _hint(dc, clockDeg, glyph) {
    var cx = dc.getWidth() / 2;
    var cy = dc.getHeight() / 2;
    var r = (cx < cy ? cx : cy);
    // rim arc (dc angles: degrees counterclockwise from 3 o'clock)
    var a = 90 - clockDeg;
    dc.setColor(Gfx.COLOR_BLUE, Gfx.COLOR_TRANSPARENT);
    dc.setPenWidth(4);
    dc.drawArc(cx, cy, r - 3, Gfx.ARC_COUNTER_CLOCKWISE, a - 12, a + 12);
    // glyph just inside the arc
    var rad = Math.toRadians(clockDeg);
    var gr = r - 20;
    var gx = cx + gr * Math.sin(rad);
    var gy = cy - gr * Math.cos(rad);
    var s = 7;
    dc.setColor(Gfx.COLOR_WHITE, Gfx.COLOR_TRANSPARENT);
    dc.setPenWidth(3);
    if (glyph == OK) {
      dc.drawLine(gx - s, gy, gx - s / 3, gy + s * 2 / 3);
      dc.drawLine(gx - s / 3, gy + s * 2 / 3, gx + s, gy - s * 2 / 3);
    } else if (glyph == RETRY) {
      dc.setPenWidth(2);
      dc.drawArc(gx, gy, s, Gfx.ARC_COUNTER_CLOCKWISE, 100, 40);
      // arrow head at the arc's open end (top)
      dc.drawLine(gx + 2, gy - s, gx - 3, gy - s - 4);
      dc.drawLine(gx + 2, gy - s, gx - 3, gy - s + 4);
    } else if (glyph == CLOSE) {
      dc.drawLine(gx - s * 2 / 3, gy - s * 2 / 3, gx + s * 2 / 3, gy + s * 2 / 3);
      dc.drawLine(gx - s * 2 / 3, gy + s * 2 / 3, gx + s * 2 / 3, gy - s * 2 / 3);
    } else if (glyph == MENU) {
      dc.setPenWidth(2);
      dc.drawLine(gx - s, gy - 5, gx + s, gy - 5);
      dc.drawLine(gx - s, gy, gx + s, gy);
      dc.drawLine(gx - s, gy + 5, gx + s, gy + 5);
    }
    dc.setPenWidth(1);
  }
}
