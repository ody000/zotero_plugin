/**
 * highlighter.ts — the whole Zotero side of the paper highlighter.
 *
 * What it does
 *   1. When a PDF opens in the reader (and auto-run is on), sends the PDF's file path
 *      to the local Python server, gets back which sentences to highlight, and saves
 *      them as normal Zotero highlight annotations tagged "auto-highlight".
 *   2. Adds two right-click items to the item list:
 *        "Highlight this paper"   — re-run; replaces the old auto-highlights
 *        "Remove auto-highlights" — delete them (Zotero only, no server needed)
 *   3. Checks the server's /health on startup and shows a notice if it's not running.
 *
 * Wiring into the plugin template (src/hooks.ts), four calls:
 *   import * as Highlighter from "./modules/highlighter";
 *   onStartup():            Highlighter.startup({ pluginID: addon.data.config.addonID,
 *                                                 prefsPrefix: addon.data.config.prefsPrefix });
 *   onMainWindowLoad(win):  Highlighter.onMainWindowLoad(win);
 *   onMainWindowUnload(win):Highlighter.onMainWindowUnload(win);
 *   onShutdown():           Highlighter.shutdown();
 *
 * Settings (add to addon/prefs.js; the template adds the prefix itself):
 *   pref("density", 10);                          // percent of body sentences, 5–20
 *   pref("autoRun", true);
 *   pref("serverUrl", "http://127.0.0.1:8765");
 *
 * The JSON shapes below are the contract with server/app.py (see docs/api-contract.md).
 * Zotero objects are typed loosely (`any`) on purpose: this file is glue code.
 */

// ---------------------------------------------------------------- contract

export type Facet = "contribution" | "method" | "result";

export interface HighlightRequest {
  pdf_path: string;
  density: number; // fraction, 0.05–0.20
  force: boolean; // true = ignore the server cache
}

export interface Highlight {
  sentence_id: number;
  page_index: number; // 0-based
  rects: number[][]; // [[x1, y1, x2, y2], ...] in PDF points, bottom-left origin
  sort_index: string; // "PPPPP|OOOOOO|TTTTT"
  text: string;
  facet: Facet;
  label: string; // objective | novelty | method | result
  why: string;
}

export interface HighlightResponse {
  highlights: Highlight[];
  scorer: string;
  cached: boolean;
  stats: { n_sentences: number; n_selected: number; ms: number };
}

// ---------------------------------------------------------------- settings

const AUTO_TAG = "auto-highlight";

// Zotero's built-in annotation palette, so the sidebar's color filter works.
const FACET_COLORS: Record<Facet, string> = {
  contribution: "#a28ae5", // purple
  method: "#2ea8e5", // blue
  result: "#5fb236", // green
};
const FACET_NAMES: Record<Facet, string> = {
  contribution: "Contribution",
  method: "Method",
  result: "Result",
};

const DEFAULTS = {
  density: 10,
  autoRun: true,
  serverUrl: "http://127.0.0.1:8765",
};

const HIGHLIGHT_TIMEOUT_MS = 120_000; // LLM calls can take a while
const HEALTH_TIMEOUT_MS = 3_000;
const BACKEND_DOWN =
  "Backend not running. In server/, run: uv run uvicorn app:app --port 8765";

const MENU_IDS = {
  separator: "paper-highlighter-separator",
  run: "paper-highlighter-run",
  remove: "paper-highlighter-remove",
};

let pluginID = "";
let prefsPrefix = "";
const inFlight = new Set<number>(); // attachment ids being highlighted right now

const Z = (): any => Zotero;

function getPref<K extends keyof typeof DEFAULTS>(key: K): (typeof DEFAULTS)[K] {
  const value = Z().Prefs.get(`${prefsPrefix}.${key}`, true);
  return value === undefined ? DEFAULTS[key] : value;
}

function serverUrl(): string {
  return String(getPref("serverUrl")).replace(/\/+$/, "");
}

function densityFraction(): number {
  const percent = Number(getPref("density"));
  const safe = Number.isFinite(percent) ? percent : DEFAULTS.density;
  return Math.min(20, Math.max(5, safe)) / 100;
}

// ---------------------------------------------------------------- lifecycle

export function startup(options: { pluginID: string; prefsPrefix: string }): void {
  pluginID = options.pluginID;
  prefsPrefix = options.prefsPrefix;
  Z().Reader.registerEventListener("renderToolbar", onReaderToolbar, pluginID);
  void checkHealth();
}

export function shutdown(): void {
  Z().Reader.unregisterEventListener("renderToolbar", onReaderToolbar);
  for (const win of Z().getMainWindows()) onMainWindowUnload(win);
}

export function onMainWindowLoad(win: any): void {
  const doc = win.document;
  const itemMenu = doc.getElementById("zotero-itemmenu");
  if (!itemMenu || doc.getElementById(MENU_IDS.run)) return;

  const separator = doc.createXULElement("menuseparator");
  separator.id = MENU_IDS.separator;

  const run = menuItem(doc, MENU_IDS.run, "Highlight this paper", () =>
    forSelectedPDFs(win, (attachment) => runHighlight(attachment, true)),
  );
  const remove = menuItem(doc, MENU_IDS.remove, "Remove auto-highlights", () =>
    forSelectedPDFs(win, async (attachment) => {
      const count = await removeAutoHighlights(attachment);
      notify("Removed auto-highlights", `${count} removed.`);
    }),
  );
  itemMenu.append(separator, run, remove);
}

export function onMainWindowUnload(win: any): void {
  for (const id of Object.values(MENU_IDS)) win.document.getElementById(id)?.remove();
}

function menuItem(doc: any, id: string, label: string, onCommand: () => Promise<void>): any {
  const item = doc.createXULElement("menuitem");
  item.id = id;
  item.setAttribute("label", label);
  item.addEventListener("command", () => void onCommand());
  return item;
}

// ---------------------------------------------------------------- triggers

// Fires when a reader tab finishes building its toolbar, i.e. when a PDF opens.
function onReaderToolbar(event: any): void {
  const itemID: number | undefined = event?.reader?.itemID;
  if (itemID == null || !getPref("autoRun")) return;

  const attachment = Z().Items.get(itemID);
  if (!attachment?.isPDFAttachment?.()) return;
  if (hasAutoHighlights(attachment)) return; // "only once": the tagged annotations are the record

  void runHighlight(attachment, false);
}

async function forSelectedPDFs(
  win: any,
  action: (attachment: any) => Promise<unknown>,
): Promise<void> {
  const selected: any[] = win.ZoteroPane.getSelectedItems();
  let found = 0;
  for (const item of selected) {
    const attachment = await pdfAttachmentOf(item);
    if (!attachment) continue;
    found++;
    await action(attachment);
  }
  if (found === 0) notify("No PDF found", "Select a paper that has a PDF attached.");
}

async function pdfAttachmentOf(item: any): Promise<any | null> {
  if (item.isPDFAttachment?.()) return item;
  if (item.isRegularItem?.()) {
    const best = await item.getBestAttachment();
    return best?.isPDFAttachment?.() ? best : null;
  }
  return null;
}

// ---------------------------------------------------------------- main flow

async function runHighlight(attachment: any, force: boolean): Promise<void> {
  if (inFlight.has(attachment.id)) return;
  inFlight.add(attachment.id);
  const progress = notify("Highlighting…", paperTitle(attachment), 0);

  try {
    const pdfPath: string | false = await attachment.getFilePathAsync();
    if (!pdfPath) throw new Error("The PDF file isn't on this computer.");

    const response = await requestHighlights({
      pdf_path: pdfPath,
      density: densityFraction(),
      force,
    });

    // Only clear the old set once the new one has arrived, so a failure loses nothing.
    if (force || hasAutoHighlights(attachment)) await removeAutoHighlights(attachment);
    const saved = await saveHighlights(attachment, response.highlights);

    progress.close();
    const details = [response.scorer, response.cached ? "cached" : "", `${response.stats?.ms ?? "?"} ms`]
      .filter(Boolean)
      .join(" · ");
    notify(`Added ${saved} highlights`, details);
  } catch (error) {
    progress.close();
    notify("Highlighting failed", describeError(error), 8000);
    Z().logError(error);
  } finally {
    inFlight.delete(attachment.id);
  }
}

// ---------------------------------------------------------------- server calls

async function requestHighlights(body: HighlightRequest): Promise<HighlightResponse> {
  const xhr = await Z().HTTP.request("POST", `${serverUrl()}/highlight`, {
    body: JSON.stringify(body),
    headers: { "Content-Type": "application/json" },
    responseType: "json",
    timeout: HIGHLIGHT_TIMEOUT_MS,
  });
  const response = xhr.response as HighlightResponse | null;
  if (!response || !Array.isArray(response.highlights)) {
    throw new Error("The server sent an unexpected response.");
  }
  return response;
}

async function checkHealth(): Promise<void> {
  try {
    const xhr = await Z().HTTP.request("GET", `${serverUrl()}/health`, {
      responseType: "json",
      timeout: HEALTH_TIMEOUT_MS,
    });
    Z().debug(`[paper-highlighter] backend OK: ${JSON.stringify(xhr.response)}`);
  } catch {
    notify("Paper highlighter", BACKEND_DOWN, 8000);
  }
}

function describeError(error: any): string {
  const http = Z().HTTP;
  if (http.TimeoutException && error instanceof http.TimeoutException) {
    return "The server took too long to answer.";
  }
  if (error?.status === 0) return BACKEND_DOWN;
  if (error?.status) {
    const detail = error.xmlhttp?.response?.detail ?? error.xmlhttp?.responseText ?? "";
    const text = typeof detail === "string" ? detail : JSON.stringify(detail);
    return `Server error ${error.status}${text ? `: ${text}` : ""}`;
  }
  return String(error?.message ?? error);
}

// ---------------------------------------------------------------- annotations

function hasAutoHighlights(attachment: any): boolean {
  return attachment.getAnnotations().some((annotation: any) => annotation.hasTag(AUTO_TAG));
}

async function removeAutoHighlights(attachment: any): Promise<number> {
  const ids: number[] = attachment
    .getAnnotations()
    .filter((annotation: any) => annotation.hasTag(AUTO_TAG))
    .map((annotation: any) => annotation.id);
  if (ids.length) await Z().Items.erase(ids);
  return ids.length;
}

async function saveHighlights(attachment: any, highlights: Highlight[]): Promise<number> {
  let saved = 0;
  for (const highlight of highlights) {
    if (!highlight.rects?.length) continue;
    await Z().Annotations.saveFromJSON(attachment, {
      key: Z().DataObjectUtilities.generateKey(),
      type: "highlight",
      text: highlight.text,
      comment: commentFor(highlight),
      color: FACET_COLORS[highlight.facet] ?? FACET_COLORS.contribution,
      pageLabel: String(highlight.page_index + 1),
      sortIndex: validSortIndex(highlight),
      position: { pageIndex: highlight.page_index, rects: highlight.rects },
      tags: [{ name: AUTO_TAG }],
    });
    saved++;
  }
  return saved;
}

// "Result · novelty — main quantitative finding"
function commentFor(highlight: Highlight): string {
  const facet = FACET_NAMES[highlight.facet] ?? highlight.facet;
  const label = highlight.label && highlight.label !== highlight.facet ? ` · ${highlight.label}` : "";
  const why = highlight.why ? ` — ${highlight.why}` : "";
  return `${facet}${label}${why}`;
}

// Zotero rejects PDF annotations whose sortIndex isn't "PPPPP|OOOOOO|TTTTT".
// The server should always send a correct one; this only keeps a bad value from failing the save.
function validSortIndex(highlight: Highlight): string {
  if (/^\d{5}\|\d{6}\|\d{5}$/.test(highlight.sort_index ?? "")) return highlight.sort_index;
  return `${String(highlight.page_index).padStart(5, "0")}|000000|00000`;
}

// ---------------------------------------------------------------- notices

function notify(headline: string, text: string, closeAfterMs = 4000): any {
  const window = new (Z().ProgressWindow)({ closeOnClick: true });
  window.changeHeadline(headline);
  if (text) window.addDescription(text);
  window.show();
  if (closeAfterMs > 0) window.startCloseTimer(closeAfterMs);
  return window;
}

function paperTitle(attachment: any): string {
  const parent = attachment.parentItem;
  return String(parent?.getField?.("title") || attachment.getField?.("title") || "");
}
