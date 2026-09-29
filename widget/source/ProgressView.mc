using Toybox.WatchUi as Ui;
using Toybox.Application as App;

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

    // unpaired, the app has nothing else to show: Back closes it
    function onBack() {
        if (!App.getApp().isLoggedIn()) {
            App.getApp().exitUnpaired();
            return true;
        }
        return false;
    }
}

class ProgressView extends Ui.ProgressBar {
    hidden var _isActive;

    function initialize() {
      ProgressBar.initialize("", null);
      _isActive = false;
    }

    function isActive() {
        return _isActive;
    }

    function setDisplayString(text) {
        _isActive = true;
        ProgressBar.setDisplayString(text);
    }

    function onShow() {
        _isActive = true;
    }

    function onHide() {
        _isActive = false;
    }
}