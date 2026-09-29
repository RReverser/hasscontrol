using Toybox.WatchUi as Ui;
using Toybox.Application as App;
using Toybox.Graphics as Gfx;

// Progress screens keep the menu reachable; Back closes the screen while the
// connection keeps trying in the background.
class ProgressDelegate extends Ui.BehaviorDelegate {
    function initialize() {
        BehaviorDelegate.initialize();
    }

    function onMenu() {
        App.getApp().resetInactivityTimer();
        App.getApp().viewController.removeLoaderImmediate();
        App.getApp().menu.showRootMenu();
        return true;
    }

    function onHold(clickEvent) {
        return onMenu();
    }
}

// Own progress screen (the system ProgressBar cannot show button hints): the
// step text, a turning arc on the rim, and Back / Menu hints.
class ProgressView extends Ui.View {
    hidden var _isActive;
    hidden var _text = "";
    hidden var _angle = 0;

    function initialize() {
      View.initialize();
      _isActive = false;
    }

    function isActive() {
        return _isActive;
    }

    function setDisplayString(text) {
        _isActive = true;
        _text = text;
        Ui.requestUpdate();
    }

    // called by the view controller's loader timer
    function step() {
        _angle = (_angle + 20) % 360;
        Ui.requestUpdate();
    }

    function onShow() {
        _isActive = true;
    }

    function onHide() {
        _isActive = false;
    }

    function onUpdate(dc) {
        var w = dc.getWidth();
        var h = dc.getHeight();
        var r = (w < h ? w : h) / 2;
        dc.setColor(Gfx.COLOR_WHITE, Gfx.COLOR_BLACK);
        dc.clear();
        dc.drawText(w / 2, h / 2, Gfx.FONT_MEDIUM, _text,
                    Gfx.TEXT_JUSTIFY_CENTER | Gfx.TEXT_JUSTIFY_VCENTER);
        dc.setColor(Gfx.COLOR_BLUE, Gfx.COLOR_TRANSPARENT);
        dc.setPenWidth(6);
        dc.drawArc(w / 2, h / 2, r - 12, Gfx.ARC_CLOCKWISE, 90 - _angle, 90 - _angle - 70);
        dc.setPenWidth(1);
        Hints.draw(dc, Hints.NONE, Hints.CLOSE, true);
    }
}
