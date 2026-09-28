using Toybox.Application as App;
using Toybox.WatchUi as Ui;
using Hass;

class MenuController {
    enum {
        MENU_SWITCH_TO_SCENES,
        MENU_SWITCH_TO_ENTITIES,
        MENU_SWITCH_TO_ENTITIES_SCENES,
        MENU_ENTER_SETTINGS,
        MENU_LOGIN,

        MENU_SELECT_START_VIEW,
        MENU_REFRESH_ENTITIES,
        MENU_LOGOUT,

        MENU_SELECT_START_VIEW_ENTITIES,
        MENU_SELECT_START_VIEW_SCENES,
        MENU_SELECT_START_VIEW_ENTITIES_SCENES,

        MENU_TOGGLE_LIST_VIEW,

        MENU_BACK
    }

    hidden var _delegate;

    (:fullmem)
    hidden var _selectMenuEntityId;

    function initialize() {
            _delegate = new MenuDelegate();
            resetSelectMenuEntityId();
    }

    // The Menu2 option picker is fullmem-only - select/input_select is
    // shown read-only on 64 KB widget devices instead (see Entity.mc), so
    // _selectMenuEntityId only exists on :fullmem, and initializing/
    // reading it is split out too.
    (:fullmem)
    hidden function resetSelectMenuEntityId() {
        _selectMenuEntityId = null;
    }

    (:lowmem)
    hidden function resetSelectMenuEntityId() {
    }

    (:fullmem)
    function getSelectMenuEntityId() {
        return _selectMenuEntityId;
    }

    function showRootMenu() {
        var menu = new Ui.Menu2({
            :title => "HassControl"
        });

        if (App.getApp().isLoggedIn()) {
            menu.addItem(new Ui.MenuItem(
                "Scenes",
                "",
                MenuController.MENU_SWITCH_TO_SCENES,
                {}
            ));
            menu.addItem(new Ui.MenuItem(
                "Entities",
                "",
                MenuController.MENU_SWITCH_TO_ENTITIES,
                {}
            ));
            menu.addItem(new Ui.MenuItem(
                "Entities&Scenes",
                "",
                MenuController.MENU_SWITCH_TO_ENTITIES_SCENES,
                {}
            ));
            menu.addItem(new Ui.MenuItem(
                "Settings",
                "",
                MenuController.MENU_ENTER_SETTINGS,
                {}
            ));
        } else {
            menu.addItem(new Ui.MenuItem(
                "Pair",
                "",
                MenuController.MENU_LOGIN,
                {}
            ));
        }

        Ui.pushView(menu, _delegate, Ui.SLIDE_IMMEDIATE);
        }

    function showSettingsMenu() {
        var menu = new Ui.Menu2({
            :title => "Settings"
        });

        menu.addItem(new Ui.MenuItem(
            "Start View",
            App.getApp().getStartView(),
            MenuController.MENU_SELECT_START_VIEW,
            {}
        ));
        addListViewToggle(menu);
        menu.addItem(new Ui.MenuItem(
            "Refresh entities",
            Hass.getGroup(),
            MenuController.MENU_REFRESH_ENTITIES,
            {}
        ));
        menu.addItem(new Ui.MenuItem(
            "Unpair",
            "",
            MenuController.MENU_LOGOUT,
            {}
        ));

        Ui.pushView(menu, _delegate, Ui.SLIDE_IMMEDIATE);
    }

    // The list/card style switch. Omitted on 64 KB widget devices, where
    // EntityListView is not compiled in (see monkey.jungle).
    (:fullmem)
    hidden function addListViewToggle(menu) {
        menu.addItem(new Ui.ToggleMenuItem(
            "List View",
            {
                :enabled => "3-row list",
                :disabled => "Classic card"
            },
            MenuController.MENU_TOGGLE_LIST_VIEW,
            App.getApp().viewController.useListEntityView(),
            {}
        ));
    }

    (:lowmem)
    hidden function addListViewToggle(menu) {
    }

    function showSelectStartViewMenu() {
        var menu = new Ui.Menu2({
            :title => "Start view"
        });

        var currentStartView = App.getApp().getStartView();
        var entitiesSubtitle = "";
        var scenesSubtitle = "";
        var entitiesScenesSubtitle = "";

        if (currentStartView == HassControlApp.ENTITIES_VIEW) {
            entitiesSubtitle = "selected";
        }

        if (currentStartView == HassControlApp.SCENES_VIEW) {
            scenesSubtitle = "selected";
        }
        if (currentStartView == HassControlApp.ENTITIES_SCENES_VIEW) {
            entitiesScenesSubtitle = "selected";
        }

        menu.addItem(new Ui.MenuItem(
            "Entities",
            entitiesSubtitle,
            MenuController.MENU_SELECT_START_VIEW_ENTITIES,
            {}
        ));

        menu.addItem(new Ui.MenuItem(
            "Scenes",
            scenesSubtitle,
            MenuController.MENU_SELECT_START_VIEW_SCENES,
            {}
        ));

        menu.addItem(new Ui.MenuItem(
            "Entities and Scenes",
            entitiesScenesSubtitle,
            MenuController.MENU_SELECT_START_VIEW_ENTITIES_SCENES,
            {}
        ));

        menu.addItem(new Ui.MenuItem(
            "Back",
            "",
            MenuController.MENU_BACK,
            {}
        ));

        Ui.pushView(menu, _delegate, Ui.SLIDE_IMMEDIATE);
    }

    (:fullmem)
    function showSelectOptionMenu(entity) {
        var menu = new Ui.Menu2({
            :title => entity.getRawName()
        });

        var options = entity.getOptions();
        var currentValue = entity.getState() == Hass.STATE_SENSOR ? entity.getSensorValue() : null;

        _selectMenuEntityId = entity.getId();

        if (options != null) {
            for (var i = 0; i < options.size(); i++) {
                // A checkmark glyph in the label itself, rather than a
                // "selected" subLabel, needs no localization and leaves
                // room for a longer option label on screen.
                var isSelected = currentValue != null && options[i].equals(currentValue);
                var label = isSelected ? "» " + options[i] : options[i];

                menu.addItem(new Ui.MenuItem(
                    label,
                    null,
                    options[i],
                    {}
                ));

                // Land on the entity's current option instead of always
                // defaulting to the top of the list.
                if (isSelected) {
                    menu.setFocus(i);
                }
            }
        }

        Ui.pushView(menu, _delegate, Ui.SLIDE_IMMEDIATE);
    }
}