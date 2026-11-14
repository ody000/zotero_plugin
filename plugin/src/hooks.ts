// Zotero calls these four functions at fixed moments (see addon/bootstrap.js).
// They only dispatch: the real work lives in modules/highlighter.ts.

import { getString, initLocale } from "./utils/locale";
import { createZToolkit } from "./utils/ztoolkit";
import * as Highlighter from "./modules/highlighter";

// Plugin loads (Zotero starts, or the plugin is installed/enabled).
async function onStartup() {
  await Promise.all([
    Zotero.initializationPromise,
    Zotero.unlockPromise,
    Zotero.uiReadyPromise,
  ]);

  initLocale();
  registerPrefsPane();

  Highlighter.startup({
    pluginID: addon.data.config.addonID,
    prefsPrefix: addon.data.config.prefsPrefix,
  });

  await Promise.all(
    Zotero.getMainWindows().map((win) => onMainWindowLoad(win)),
  );

  // Lets the scaffold's test runner know the plugin finished loading.
  addon.data.initialized = true;
}

// A Zotero main window opens.
async function onMainWindowLoad(win: _ZoteroTypes.MainWindow): Promise<void> {
  addon.data.ztoolkit = createZToolkit();
  Highlighter.onMainWindowLoad(win);
}

// A Zotero main window closes.
async function onMainWindowUnload(win: Window): Promise<void> {
  Highlighter.onMainWindowUnload(win);
  ztoolkit.unregisterAll();
}

// Plugin is disabled/uninstalled, or Zotero quits.
function onShutdown(): void {
  Highlighter.shutdown();
  ztoolkit.unregisterAll();
  addon.data.alive = false;
  // @ts-expect-error - Plugin instance is not typed
  delete Zotero[addon.data.config.addonInstance];
}

// Adds "Paper Highlighter" to Zotero's Settings window (addon/content/preferences.xhtml).
function registerPrefsPane(): void {
  Zotero.PreferencePanes.register({
    pluginID: addon.data.config.addonID,
    src: rootURI + "content/preferences.xhtml",
    label: getString("prefs-title"),
    image: `chrome://${addon.data.config.addonRef}/content/icons/favicon.png`,
  });
}

export default {
  onStartup,
  onShutdown,
  onMainWindowLoad,
  onMainWindowUnload,
};
