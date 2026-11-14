# Zotero plugin (TypeScript)

Built on `windingwind/zotero-plugin-template` (AGPL-3.0, see LICENSE). Our own code is `src/modules/highlighter.ts`; `src/hooks.ts` only calls into it.

## Files you might touch
- `src/modules/highlighter.ts` — everything the plugin does (reader hook, server calls, annotations, menus, notices)
- `src/hooks.ts` — the four lifecycle calls into highlighter.ts + registering the settings pane
- `addon/prefs.js` — default settings (density 10, autoRun true, serverUrl)
- `addon/content/preferences.xhtml` + `addon/locale/en-US/preferences.ftl` — the settings pane and its labels
- `package.json` → `config` — plugin name / ID / prefix

Everything else (bootstrap.js, manifest.json, utils/, typings/, build config) is template plumbing.

## Run it
1. One-time: `.env` must point at a separate **dev profile** and a separate **dev library** (`ZOTERO_PLUGIN_DATA_DIR`). Never leave the data dir empty, or Zotero opens your real library in `~/Zotero`. On Ziqi's Mac these are already set to `~/Library/Application Support/Zotero/Profiles/dev` and `~/Zotero-dev`. Quit your normal Zotero before `npm start`.
2. Start the server: `cd ../server && uv run uvicorn app:app --port 8765`
3. `npm start` — opens Zotero with the dev profile and reloads the plugin on every save.
4. Build an installable file: `npm run build` → `.scaffold/build/paper-highlighter.xpi`
