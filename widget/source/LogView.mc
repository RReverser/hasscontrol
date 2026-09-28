using Toybox.WatchUi as Ui;
using Toybox.Graphics;
using Utils;

// Read-only view of the in-memory debug log (Utils.logLines()), newest at
// the bottom. UP/DOWN scroll by a page.
class LogView extends Ui.View {
  hidden var _offset = 0;  // lines scrolled up from the newest

  function initialize() {
    View.initialize();
  }

  function scroll(pages) {
    _offset += pages * 8;
    if (_offset < 0) {
      _offset = 0;
    }
    var n = Utils.logLines().size();
    if (_offset > n - 1) {
      _offset = n > 0 ? n - 1 : 0;
    }
    Ui.requestUpdate();
  }

  function onUpdate(dc) {
    dc.setColor(Graphics.COLOR_WHITE, Graphics.COLOR_BLACK);
    dc.clear();
    var lines = Utils.logLines();
    var font = Graphics.FONT_XTINY;
    var lh = dc.getFontHeight(font);
    var h = dc.getHeight();
    var w = dc.getWidth();
    if (lines.size() == 0) {
      dc.drawText(w / 2, h / 2, font, "log empty", Graphics.TEXT_JUSTIFY_CENTER);
      return;
    }
    // walk back from the newest shown line, wrapping each to the screen
    // width, until the usable height (inset for the round bezel) is full
    var avail = h - 2 * lh;
    var picked = [];
    for (var i = lines.size() - 1 - _offset; i >= 0; i--) {
      var t = Graphics.fitTextToArea(lines[i], font, w * 0.84, lh * 3, true);
      var rows = 1;
      var chars = t.toCharArray();
      for (var c = 0; c < chars.size(); c++) {
        if (chars[c] == '\n') {
          rows += 1;
        }
      }
      if (rows * lh > avail) {
        break;
      }
      avail -= rows * lh;
      picked.add([t, rows]);
    }
    var y = lh + avail;
    for (var j = picked.size() - 1; j >= 0; j--) {
      dc.drawText(w / 2, y, font, picked[j][0], Graphics.TEXT_JUSTIFY_CENTER);
      y += picked[j][1] * lh;
    }
  }
}

class LogDelegate extends Ui.BehaviorDelegate {
  hidden var _view;

  function initialize(view) {
    BehaviorDelegate.initialize();
    _view = view;
  }

  function onPreviousPage() {
    _view.scroll(1);
    return true;
  }

  function onNextPage() {
    _view.scroll(-1);
    return true;
  }
}
