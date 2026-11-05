import test from "node:test";
import assert from "node:assert/strict";
import { loadVoisr } from "./load.mjs";
import { makeNode } from "./dom.mjs";

/* ---------- home.js: render(), driven end to end in Node ----------

   Every element it writes to is reached through `V.ui.$`, and tests/js/dom.mjs
   resolves "#id" through the same registry as getElementById, so these run for
   real: the production render() writing to real fake nodes, with nothing patched
   per test. That is what lets the panel-isolation contract documented above
   render() ("a failure in one degrades that panel only") be executed at all. */

const HOME_IDS = ["greeting", "perm-strip", "home-hero", "home-hint", "home-status", "ptt-key-name",
                  "stat-saved", "stat-words", "stat-streak", "stat-wave", "stat-wave-block",
                  "recent-groups", "recent-empty", "home-view-all", "toast", "view-home",
                  "record-btn", "record-label", "record-title", "record-status", "record-error",
                  "record-time", "home-wave", "change-key", "nav-home"];

/* Home polls permissions with a real setInterval while something is blocked.
   Unref'd, a render that ends blocked can't keep the test process alive. */
const realSetInterval = globalThis.setInterval;
globalThis.setInterval = (...args) => {
  const timer = realSetInterval(...args);
  if (timer && typeof timer.unref === "function") timer.unref();
  return timer;
};

const HISTORY = Array.from({ length: 7 }, (_, i) => ({
  id: `h${i}`, ts: 1_750_000_000 + i * 60, text: `dictation ${i}`, words: 10 + i, duration_s: 4,
}));

/* Windows gates one thing: the microphone privacy switch (voisr/permissions.py). */
const MIC_PANES = [
  { key: "microphone", label: "Microphone", granted: true },
];

const OK_API = {
  get_permissions: async () => ({ panes: MIC_PANES }),
  hotkey_display: async () => ({ display: "Right Ctrl" }),
  get_hotkey_status: async () => ({ status: "active on Right Ctrl", state: "listening" }),
  get_stats: async () => ({ total_words: 12480, streak_days: 6, time_saved_seconds: 13320 }),
  get_history: async () => HISTORY,
  get_config: async () => ({ paste: true }),
};

/* V.api is stubbed directly rather than through js/api.js: api.js's dev
   fallback arms a 300ms setTimeout on load, which every one of these tests
   would then have to mock away for no benefit. Same shape either way:
   `V.api()` returns the bridge object. */
function mountHome(api) {
  const env = loadVoisr(["js/ui.js", "js/lists.js", "js/views/home.js"]);
  const nodes = {};
  for (const id of HOME_IDS) {
    nodes[id] = makeNode(id === "record-btn" ? "button" : "div");
    env.register(id, nodes[id]);
  }
  env.V.api = () => ({ ...OK_API, ...api });
  return { ...env, ...nodes };
}

const settle = () => new Promise((r) => setImmediate(r));

// a fake container's textContent doesn't include its descendants
const allText = (node) => [node.textContent || "", ...(node.children || []).map(allText)].join(" ");
const flatten = (node) => [node, ...(node.children || []).flatMap(flatten)];
const byClass = (node, cls) => flatten(node).filter((n) => String(n.className || "").split(/\s+/).includes(cls));

const banner = (h) => h["perm-strip"].children[0];
const buttonsIn = (node) => node.children.map((b) => b.textContent);
const actionsOf = (h) => buttonsIn(byClass(banner(h), "banner-actions")[0]);
const statusLabel = (h) => allText(byClass(h["home-status"], "status-label")[0]).trim();
const statusDetail = (h) => byClass(h["home-status"], "status-detail")[0].textContent;
const feedRows = (h) => byClass(h["recent-groups"], "item-row");

const pane = (key, label, granted) => ({ key, label, granted });
const panes = (...list) => ({ get_permissions: async () => ({ panes: list }) });
const MIC_OK = pane("microphone", "Microphone", true);
const MIC_OFF = panes(pane("microphone", "Microphone", false));
/* The hook isn't installed: Windows' status for it, from HotkeyService. */
const HOOK_FAILED = { get_hotkey_status: async () => (
  { status: "the keyboard hook could not be installed — retrying", state: "off" }) };

test("home render() fills every panel through V.ui.$: greeting, banner, stats, status and the feed", async () => {
  const h = mountHome({});

  await h.V.views.home.render();

  assert.ok(["Working late", "Good morning", "Good afternoon", "Good evening"]
    .includes(h.greeting.textContent), `unexpected greeting: ${h.greeting.textContent}`);
  // named from hotkey_display(), without the symbol a headline doesn't need
  assert.equal(h["ptt-key-name"].textContent, "Right Ctrl");
  assert.equal(h["home-hint"].textContent, "Let go and it lands at your cursor.");

  // 13320s -> 222 whole minutes -> 3h 42m (formatDuration rounds to minutes first)
  assert.equal(h["stat-saved"].textContent, "3h 42m");
  assert.equal(h["stat-words"].textContent, "12.5K");
  assert.equal(h["stat-words"].title, `${(12480).toLocaleString()} words`, "the exact count is on hover");
  assert.equal(h["stat-streak"].textContent, (6).toLocaleString());

  // 7 items in history, five on Home.
  assert.equal(feedRows(h).length, 5);
  assert.equal(feedRows(h)[0].dataset.id, "h0");
  assert.equal(h["recent-empty"].hidden, true, "the empty-state line hides once there are rows");
  assert.equal(h["home-view-all"].hidden, false);

  assert.equal(h["perm-strip"].dataset.state, "ok");
  assert.equal(h["perm-strip"].children.length, 0, "nothing to say: no blue banner");
  assert.equal(h["perm-strip"].hidden, true);
  assert.equal(h["home-hero"].hidden, false);
  assert.equal(statusLabel(h), "Ready");
});

test("the feed groups rows by real day, with each row's time beside its text", async () => {
  const DAY = 86400;
  const h = mountHome({ get_history: async () => [
    { id: "a", ts: 1_750_000_000, text: "one", words: 1 },
    { id: "b", ts: 1_750_000_000 - 2 * DAY, text: "two", words: 1 },
  ] });
  await h.V.views.home.render();
  const groups = byClass(h["recent-groups"], "day-group");
  assert.equal(groups.length, 2, "a day apart, so two labels");
  assert.deepEqual(groups.map((g) => byClass(g, "item-row").length), [1, 1]);
  assert.ok(byClass(groups[0], "date-label")[0].textContent);
  assert.ok(byClass(feedRows(h)[0], "row-time")[0].textContent, "the time column");
  assert.equal(byClass(feedRows(h)[0], "item-meta").length, 0, "no word count line");
});

test("a snippet's row says so, because it only holds the phrase", async () => {
  const h = mountHome({ get_history: async () => [
    { id: "a", ts: 1_750_000_000, text: "my email", words: 2, snippet: true },
    { id: "b", ts: 1_750_000_000, text: "a sentence", words: 2 },
  ] });
  await h.V.views.home.render();
  assert.equal(byClass(feedRows(h)[0], "tag")[0].textContent, "Snippet");
  assert.equal(byClass(feedRows(h)[1], "tag").length, 0);
});

test("the empty feed says so, with nothing to view", async () => {
  const h = mountHome({ get_history: async () => [] });
  // Start from "already hidden", i.e. the state a previous non-empty render
  // left behind. Asserting against a fresh node instead would pass without
  // render() ever running: `hidden` is false by default.
  h["recent-empty"].hidden = true;

  await h.V.views.home.render();

  assert.equal(feedRows(h).length, 0);
  assert.equal(h["recent-empty"].hidden, false);
  assert.equal(h["home-view-all"].hidden, true);
});

test("home render() degrades one panel at a time: a failed get_stats still leaves the feed rendered", async (t) => {
  // The panel-isolation contract from the comment above render(): stats used
  // to throw on `stats.total_words` and take the whole pane down, leaving a
  // blank screen. Timers are mocked because tryApi's failure toast arms a
  // 3.5s dismissal that would otherwise hold the process open.
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const h = mountHome({ get_stats: async () => ({ error: "db locked" }) });

  await h.V.views.home.render();
  await settle();

  assert.equal(h["stat-words"].textContent, "",
    "the stats panel stays untouched rather than printing a wrong number");
  assert.equal(feedRows(h).length, 5, "the feed is a separate panel and must still render");
  assert.equal(h["ptt-key-name"].textContent, "Right Ctrl", "so must the banner");
  assert.equal(statusLabel(h), "Ready", "so must the status");

  const toastText = h.toast.children.map((c) => c.textContent).join("");
  assert.match(toastText, /Load stats failed: db locked/);
  assert.doesNotMatch(toastText, /Home failed to load/,
    "one dead panel is not a whole-view failure");
});

test("large word counts are compact; small ones are exact", () => {
  const { compactCount } = mountHome({}).V.views.home;
  assert.equal(compactCount(0), "0");
  assert.equal(compactCount(999), "999");
  assert.equal(compactCount(1000), "1K");
  assert.equal(compactCount(41300), "41.3K");
  assert.equal(compactCount(999949), "999.9K");
  assert.equal(compactCount(999950), "1M", "never 1000K");
  assert.equal(compactCount(1234567), "1.2M");
});

test("no time saved yet reads 0m, not a dash beside the word saved", async () => {
  const h = mountHome({ get_stats: async () => ({ total_words: 0, streak_days: 0, time_saved_seconds: 0 }) });
  await h.V.views.home.render();
  assert.equal(h["stat-saved"].textContent, "0m");
  assert.equal(h["stat-words"].textContent, "0");
});

for (const [display, shown] of [
  ["Left Alt", "Left Alt"], ["Menu", "Menu"], ["F13", "F13"], ["unset", "your key"],
  ["Right Ctrl (Ctrl)", "Right Ctrl"],
]) {
  test(`the banner names ${display} as "${shown}"`, async () => {
    const h = mountHome({ hotkey_display: async () => ({ display }) });
    await h.V.views.home.render();
    assert.equal(h["ptt-key-name"].textContent, shown);
  });
}

test("Change key opens Settings at the key's section", () => {
  const h = mountHome({});
  const shown = [];
  h.V.show = (...args) => { shown.push(args); };
  h.V.views.home.init();
  h["change-key"].dispatch("click");
  assert.deepEqual(shown, [["settings", { section: "key" }]]);
});

/* ---------- how "Ready" maps onto the real states ---------- */

test("Ready names what was checked, on hover", async () => {
  const h = mountHome({});
  await h.V.views.home.render();
  assert.equal(byClass(h["home-status"], "status-label")[0].getAttribute("title"),
               "Microphone granted.", "the pane stays on hover");
  assert.equal(byClass(h["home-status"], "status-detail").length, 0, "Ready is one line");
});

test("a microphone that couldn't be checked says so quietly, and nothing is blue", async () => {
  // null is "couldn't check" (no device, or held exclusively), not a refusal:
  // the next dictation says what is actually wrong
  const h = mountHome(panes(pane("microphone", "Microphone", null)));
  await h.V.views.home.render();
  assert.equal(statusLabel(h), "Mic unchecked");
  assert.match(statusDetail(h), /Couldn't check the microphone/);
  assert.equal(h["perm-strip"].dataset.state, "pending");
  assert.equal(h["perm-strip"].children.length, 0);
  assert.equal(h["home-hero"].hidden, false, "holding the key may well work");
  assert.equal(byClass(h["home-status"], "status-dot").length, 0);
});

test("a tap still starting is undecided, not broken", async (t) => {
  t.mock.timers.enable({ apis: ["setInterval"] });
  let status = { status: "starting push-to-talk on Right Ctrl…", state: "off" };
  const h = mountHome({ get_hotkey_status: async () => status });
  await h.V.views.home.render();
  assert.equal(statusLabel(h), "Starting…");
  assert.equal(h["home-hero"].hidden, false);
  assert.equal(h["perm-strip"].children.length, 0, "no alarm while it comes up");

  status = { status: "active on Right Ctrl", state: "listening" };
  t.mock.timers.tick(1000);
  await settle();
  assert.equal(statusLabel(h), "Ready", "and Home notices without a click");
});

for (const status of ["not started", "active on Right Ctrl"]) {
  test(`a listener that's off with status "${status}" is only held up, so no alarm`, async (t) => {
    // the page can load before the hook starts, and Settings' key capture stops it
    // for up to 8 s with "active on" still in place
    t.mock.timers.enable({ apis: ["setInterval"] });
    const h = mountHome({ get_hotkey_status: async () => ({ status, state: "off" }) });
    await h.V.views.home.render();
    assert.equal(statusLabel(h), "Starting…");
    assert.equal(h["perm-strip"].children.length, 0);
    assert.equal(h["home-hero"].hidden, false);
  });
}

test("busy says the key is paused", async () => {
  const h = mountHome({ get_hotkey_status: async () => ({ status: "x", state: "busy" }) });
  await h.V.views.home.render();
  assert.equal(statusLabel(h), "Recording here");
  assert.match(statusDetail(h), /paused/);
});

test("a listener that's off with the mic on says so, and offers a way to keep working", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const h = mountHome({
    get_hotkey_status: async () => ({ status: "push-to-talk not running: boom", state: "off" }),
  });
  await h.V.views.home.render();
  assert.equal(h["perm-strip"].dataset.state, "attention");
  assert.equal(h["home-hero"].hidden, true, "Hold the key would be a lie");
  assert.match(allText(banner(h)), /The key isn't listening/);
  assert.match(allText(banner(h)), /quit Voisr from its tray icon/);
  assert.deepEqual(actionsOf(h), ["Click to record"], "no Relaunch: Windows has none to offer");
  assert.equal(statusLabel(h), "Needs you");
  assert.equal(statusDetail(h), "Push-to-talk not running: boom.");
});

test("a hook Windows wouldn't install reads as the key not listening, in Windows' words", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const h = mountHome(HOOK_FAILED);
  await h.V.views.home.render();
  assert.match(allText(banner(h)), /The key isn't listening/);
  assert.equal(statusDetail(h), "The keyboard hook could not be installed — retrying.");
  const checks = byClass(h["home-status"], "perm-checks")[0];
  assert.deepEqual(checks.children.map((li) => li.className), ["perm-ok-row"],
    "the mic is listed too, so the panel says the key is the whole problem");
});

test("a stopped listener doesn't raise the alarm before the permissions have answered", async () => {
  // it stops for a missing grant too, and that gets a better banner
  const h = mountHome({ get_hotkey_status: async () => ({ status: "waiting", state: "off" }) });
  await h.V.views.home.renderListener();
  assert.equal(h["perm-strip"].children.length, 0);
});

test("home render() does not claim it is ready when the status call fails", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const h = mountHome({ get_hotkey_status: async () => ({ error: "bridge gone" }) });
  await h.V.views.home.render();
  assert.equal(statusLabel(h), "Status unknown");
  assert.equal(h["home-hero"].hidden, false, "and it doesn't raise the alarm either");
  assert.equal(byClass(h["home-status"], "status-dot").length, 0);
});

test("a failed permissions read never reads as Ready", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const h = mountHome({ get_permissions: async () => ({ error: "bridge gone" }) });
  await h.V.views.home.render();
  assert.equal(statusLabel(h), "Status unknown");
});

test("the status doesn't flash unknown before both checks have answered", async () => {
  const h = mountHome({});
  await h.V.views.home.renderPermissions();
  assert.equal(h["home-status"].children.length, 0);
});

/* ---------- needs you ---------- */

test("a microphone switched off takes the banner's place and says what broke", async () => {
  const h = mountHome(MIC_OFF);

  await h.V.views.home.renderPermissions();

  assert.equal(h["perm-strip"].dataset.state, "attention");
  assert.equal(h["perm-strip"].hidden, false);
  assert.equal(banner(h).className, "banner banner-needs-you");
  // Without the microphone nothing records: the banner promising it works
  // must be gone, not left sitting above the warning.
  assert.equal(h["home-hero"].hidden, true);

  const text = allText(banner(h));
  assert.match(text, /Voisr can't hear you/, "the heading names what broke");
  assert.match(text, /Privacy & security → Microphone/, "and where to fix it");
  assert.deepEqual(actionsOf(h), ["Open Microphone settings"],
    "no Click to record: without the microphone nothing records");
});

test("Open Microphone settings opens the microphone page", async () => {
  const opened = [];
  const h = mountHome({ ...MIC_OFF,
    open_settings_pane: async (key) => { opened.push(key); return { ok: true }; } });
  await h.V.views.home.renderPermissions();
  byClass(banner(h), "banner-actions")[0].children[0].dispatch("click");
  await settle();
  assert.deepEqual(opened, ["microphone"]);
});

test("Settings refusing to open says so instead of looking dead", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const h = mountHome({ ...MIC_OFF, open_settings_pane: async () => ({ ok: false }) });
  await h.V.views.home.renderPermissions();
  byClass(banner(h), "banner-actions")[0].children[0].dispatch("click");
  await settle();
  assert.match(h.toast.children.map((c) => c.textContent).join(""), /Couldn't open Settings/);
});

test("the status panel lists the microphone and says why it matters", async () => {
  const h = mountHome(MIC_OFF);
  await h.V.views.home.render();
  const checks = byClass(h["home-status"], "perm-checks")[0];
  assert.deepEqual(checks.children.map((li) => li.className), ["perm-denied-row"]);
  assert.equal(byClass(checks.children[0], "perm-why")[0].textContent, "needed to hear you");
  assert.equal(statusLabel(h), "Needs you");
  assert.equal(byClass(h["home-status"], "status-dot").length, 1);
  assert.equal(statusDetail(h), "Nothing records until the microphone is back on.");
});

test("an unchecked mic beside a dead key says it couldn't check, not that it's needed", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const h = mountHome({ ...panes(pane("microphone", "Microphone", null)), ...HOOK_FAILED });
  await h.V.views.home.render();
  const row = byClass(h["home-status"], "perm-checks")[0].children[0];
  assert.equal(row.className, "perm-unknown-row");
  assert.match(byClass(row, "perm-why")[0].textContent, /couldn't check/);
});

test("with the key dead and the mic off too, the mic leads and there's no Click to record", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const h = mountHome({ ...MIC_OFF, ...HOOK_FAILED });
  await h.V.views.home.render();
  assert.match(allText(banner(h)), /Voisr can't hear you/);
  assert.ok(!actionsOf(h).includes("Click to record"));
});

test("with auto-paste off, the banner says it copies", async () => {
  const h = mountHome({ get_config: async () => ({ paste: false }) });
  await h.V.views.home.render();
  assert.equal(h["home-hint"].textContent, "Let go and it's copied to your clipboard.");
  assert.equal(statusLabel(h), "Ready", "that's a choice, not a problem");
});

test("a focus refresh with no change leaves banner and status alone, so they aren't re-announced", async () => {
  const h = mountHome(MIC_OFF);
  await h.V.views.home.render();
  const [first, label] = [banner(h), h["home-status"].children[0]];
  await h.V.views.home.renderPermissions();
  await h.V.views.home.renderListener();
  assert.equal(banner(h), first, "aria-live re-reads the strip whenever its children are replaced");
  assert.equal(h["home-status"].children[0], label);
});

test("a permission change still re-renders the banner", async () => {
  let granted = true;
  const h = mountHome({ get_permissions: async () => ({ panes: [
    pane("microphone", "Microphone", granted),
  ]}) });
  await h.V.views.home.render();
  assert.equal(h["perm-strip"].children.length, 0);
  granted = false;
  await h.V.views.home.renderPermissions();
  assert.equal(banner(h).className, "banner banner-needs-you");
  granted = true;
  await h.V.views.home.renderPermissions();
  assert.equal(h["perm-strip"].children.length, 0, "and it goes once fixed");
  assert.equal(h["home-hero"].hidden, false, "the banner must come back");
});

/* ---------- the record flow, in the banner ---------- */

function recordingApi(extra = {}) {
  let recording = false;
  const calls = { start: 0, stop: 0 };
  const api = {
    get_hotkey_status: async () => ({ status: "x", state: recording ? "busy" : "listening" }),
    start_recording: async () => { calls.start += 1; recording = true; return { recording: true }; },
    stop_recording: async () => { calls.stop += 1; recording = false; return { text: "Hi" }; },
    ...extra,
  };
  return { api, calls };
}

/* The one-second tick is a real setInterval: a test that ends with the banner
   recording keeps Node alive, so every recording test stops it. */
async function stopRecording(h) {
  if (h["home-hero"].dataset.state === "recording") await h.V.views.home.toggleRecord();
}

test("recording tells you where the text goes, and marks the Home nav item until it stops", async () => {
  const { api } = recordingApi();
  const h = mountHome(api);
  await h.V.views.home.toggleRecord();
  await settle();
  try {
    assert.equal(h["home-hero"].dataset.state, "recording");
    assert.equal(h["record-title"].textContent, "Listening");
    assert.equal(h["record-time"].textContent, "0:00");
    assert.match(h["record-status"].textContent, /Click to finish/);
    assert.match(h["record-status"].textContent, /clipboard, not pasted/);
    assert.equal(h["record-label"].textContent, "Finish");
    assert.equal(h["nav-home"].dataset.recording, "true",
      "visible from every view, since push-to-talk is dropped meanwhile");
  } catch (e) {
    await stopRecording(h);
    throw e;
  }

  await h.V.views.home.toggleRecord();
  await settle();
  assert.equal(h["nav-home"].dataset.recording, undefined);
  assert.equal(h["home-hero"].dataset.state, "done");
  assert.equal(h["record-title"].textContent, "Copied to clipboard");
  assert.equal(h["record-label"].textContent, "Click to record");
});

test("the panel says the key is paused while recording here, and listening again after", async () => {
  const { api } = recordingApi();
  const h = mountHome(api);
  await h.V.views.home.render();
  await h.V.views.home.toggleRecord();
  await settle();
  try {
    assert.equal(statusLabel(h), "Recording here");
  } finally {
    await stopRecording(h);
  }
  await settle();
  assert.equal(statusLabel(h), "Ready");
});

test("while transcribing the banner says so and the button is dead", async () => {
  let finish;
  const { api } = recordingApi({ stop_recording: () => new Promise((r) => { finish = () => r({ text: "Hi" }); }) });
  const h = mountHome(api);
  await h.V.views.home.toggleRecord();
  const stopping = h.V.views.home.toggleRecord();
  await settle();
  assert.equal(h["home-hero"].dataset.state, "processing");
  assert.equal(h["record-title"].textContent, "Transcribing");
  assert.equal(h["record-btn"].getAttribute("aria-disabled"), "true");
  finish();
  await stopping;
  assert.equal(h["record-btn"].getAttribute("aria-disabled"), null);
});

test("the done state clears itself", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const { api } = recordingApi();
  const h = mountHome(api);
  await h.V.views.home.toggleRecord();
  await h.V.views.home.toggleRecord();
  assert.equal(h["home-hero"].dataset.state, "done");
  t.mock.timers.tick(2600);
  assert.equal(h["home-hero"].dataset.state, undefined);
});

test("a double-click on record starts one recording and shows no error", async () => {
  const pending = [];
  const { api, calls } = recordingApi();
  const slowStart = api.start_recording;
  api.start_recording = () => new Promise((r) => { pending.push(() => r(slowStart())); });
  const h = mountHome(api);

  const first = h.V.views.home.toggleRecord();
  const second = h.V.views.home.toggleRecord();   // lands while the first is in flight
  pending.forEach((go) => go());
  await Promise.all([first, second]);

  try {
    assert.equal(calls.start, 1);
    assert.equal(calls.stop, 0, "the second click must not stop what the first started");
    assert.equal(h["record-error"].textContent, "");
    assert.equal(h["home-hero"].dataset.state, "recording");
  } finally {
    await stopRecording(h);
  }
});

test("a failed start returns to idle with a line that stays until the next try", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { api, calls } = recordingApi();
  api.start_recording = async () => { calls.start += 1; return { error: "already recording" }; };
  const h = mountHome(api);
  await h.V.views.home.toggleRecord();
  assert.equal(h["record-error"].textContent, "Couldn't start.");
  assert.equal(h["home-hero"].dataset.state, undefined);
  await h.V.views.home.toggleRecord();
  assert.equal(calls.start, 2);
});

test("a failed transcription says so", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { api } = recordingApi({ stop_recording: async () => ({ error: "whisper fell over" }) });
  const h = mountHome(api);
  await h.V.views.home.toggleRecord();
  await h.V.views.home.toggleRecord();
  assert.equal(h["record-error"].textContent, "Transcription failed.");
  assert.equal(h["home-hero"].dataset.state, undefined);
});

test("a dictation whose History write failed says so instead of a bare 'Copied'", async () => {
  const { api } = recordingApi({
    stop_recording: async () => ({ text: "Hi", copied: true, warning: "history failed: disk full" }),
  });
  const h = mountHome(api);
  await h.V.views.home.toggleRecord();
  await h.V.views.home.toggleRecord();
  assert.match(h["record-error"].textContent, /History/);
});

test("a clipboard that failed is never reported as Copied", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { api } = recordingApi({ stop_recording: async () => ({ text: "Hi", copied: false }) });
  const h = mountHome(api);
  await h.V.views.home.toggleRecord();
  await h.V.views.home.toggleRecord();
  assert.equal(h["home-hero"].dataset.state, undefined);
  assert.match(h["record-error"].textContent, /Couldn't copy/);
});

test("the wave speeds up while listening and slows again for transcribing", async () => {
  const { api } = recordingApi();
  const h = mountHome(api);
  const speeds = [];
  h.V.wave = { setSpeed: (el, n) => { speeds.push([el, n]); } };
  await h.V.views.home.toggleRecord();
  await h.V.views.home.toggleRecord();
  assert.deepEqual(speeds.map(([, n]) => n), [3.2, 1]);
  assert.ok(speeds.every(([el]) => el === h["home-wave"]));
});

test("dictateFromAnywhere switches to Home before recording, so the banner is in view", async () => {
  const { api, calls } = recordingApi();
  const h = mountHome(api);
  const shown = [];
  h.V.show = (view) => { shown.push(view); };
  try {
    await h.V.views.home.dictateFromAnywhere();
    assert.deepEqual(shown, ["home"]);
    assert.equal(calls.start, 1);
  } finally {
    await stopRecording(h);
  }
});

/* The key isn't listening, the one blue banner on Windows that still offers
   Click to record. It outlasts a recording here: the hook stays down. */
const DEAD_KEY = HOOK_FAILED;

async function blockKey(h) {
  await h.V.views.home.renderPermissions();
  await h.V.views.home.renderListener();
}

function clickToRecord(h) {
  const button = byClass(banner(h), "banner-actions")[0].children
    .find((b) => b.textContent === "Click to record");
  button.dispatch("click", { target: button });
}

/* A recording's outcome under a blue banner: the dark banner holding
   #record-error is hidden, so the blue one has to say it. */
const blueError = (h) => byClass(banner(h), "banner-error")[0];

const OUTCOMES = [
  ["a failed start", { start_recording: async () => { throw new Error("no mic"); } }, 1, "Couldn't start."],
  ["a failed transcription", { stop_recording: async () => { throw new Error("nope"); } }, 2, "Transcription failed."],
  ["a failed copy", { stop_recording: async () => ({ text: "Hi", copied: false }) }, 2,
   "Couldn't copy it. Copy it from the list below."],
];

for (const [what, extra, clicks, message] of OUTCOMES) {
  test(`${what} stays on screen with a blue banner up`, async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
    const { api } = recordingApi(extra);
    const h = mountHome({ ...api, ...DEAD_KEY });
    await blockKey(h);
    assert.equal(blueError(h).textContent, "");

    for (let i = 0; i < clicks; i++) await h.V.views.home.toggleRecord();
    await settle();

    assert.equal(h["home-hero"].hidden, true, "the dark banner has gone back under the blue one");
    assert.equal(h["perm-strip"].hidden, false);
    assert.equal(blueError(h).textContent, message);
  });
}

test("'not saved to History' outlasts the done state under a blue banner", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const { api } = recordingApi({ stop_recording: async () => ({ text: "Hi", warning: "disk full" }) });
  const h = mountHome({ ...api, ...DEAD_KEY });
  await blockKey(h);
  await h.V.views.home.toggleRecord();
  await h.V.views.home.toggleRecord();
  assert.equal(h["home-hero"].dataset.state, "done");
  assert.equal(h["record-error"].textContent, "Copied, but not saved to History.");

  t.mock.timers.tick(2600);
  await settle();

  assert.equal(h["home-hero"].hidden, true);
  assert.equal(blueError(h).textContent, "Copied, but not saved to History.");
});

test("the message survives the blue banner being rebuilt, and clears when the next recording starts", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  let mic = true, hook = false;
  const { api } = recordingApi({
    stop_recording: async () => ({ text: "Hi", copied: false }),
    get_permissions: async () => ({ panes: [pane("microphone", "Microphone", mic)] }),
    get_hotkey_status: async () => (hook ? { status: "active on Right Ctrl", state: "listening" }
      : { status: "the keyboard hook could not be installed — retrying", state: "off" }),
  });
  const h = mountHome(api);
  await blockKey(h);
  await h.V.views.home.toggleRecord();
  await h.V.views.home.toggleRecord();
  await settle();
  const before = banner(h);

  // the microphone is now to blame, so apply() builds a new banner
  mic = false;
  await h.V.views.home.renderPermissions();
  assert.notEqual(banner(h), before, "the banner really was rebuilt");
  assert.match(blueError(h).textContent, /Couldn't copy/);

  // everything fixed takes the blue banner away: the dark one still says it
  mic = true;
  hook = true;
  await blockKey(h);
  assert.equal(h["perm-strip"].hidden, true);
  assert.match(h["record-error"].textContent, /Couldn't copy/);

  // and back to blue, then the next recording clears both
  hook = false;
  await blockKey(h);
  assert.match(blueError(h).textContent, /Couldn't copy/);
  await h.V.views.home.toggleRecord();
  assert.equal(h["record-error"].textContent, "");
  assert.equal(blueError(h).textContent, "");
  await stopRecording(h);
});

test("'Click to record' under a blue banner brings the recording up in the dark one, then goes back", async (t) => {
  // M11: the button has to start something: focus alone looked like it did nothing
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const { api, calls } = recordingApi();
  const h = mountHome({ ...api, ...DEAD_KEY });
  await blockKey(h);
  assert.equal(h["home-hero"].hidden, true);
  clickToRecord(h);
  await settle();
  assert.equal(calls.start, 1);
  assert.equal(h["home-hero"].dataset.state, "recording");
  assert.equal(h["home-hero"].hidden, false, "the mic is open: show it");
  assert.equal(h["perm-strip"].hidden, true);

  await h.V.views.home.toggleRecord();
  await settle();
  assert.equal(h["home-hero"].hidden, false, "the result shows there too");
  t.mock.timers.tick(2600);
  assert.equal(h["home-hero"].hidden, true, "and the warning comes back");
  assert.equal(h["perm-strip"].hidden, false);
});

test("'Click to record' never stops a recording already running", async () => {
  const { api, calls } = recordingApi();
  const h = mountHome({ ...api, ...DEAD_KEY });
  await blockKey(h);
  await h.V.views.home.toggleRecord();          // recording from the banner
  try {
    clickToRecord(h);
    await settle();
    assert.equal(calls.stop, 0);
    assert.equal(h["home-hero"].dataset.state, "recording");
  } finally {
    await stopRecording(h);
  }
});

/* ---------- stats count up ----------

   The fake window has no requestAnimationFrame, so every update is instant
   unless a test installs these: frames queue up and the test runs them with
   the timestamps it wants. */
function frames(h) {
  const queue = [];
  h.window.requestAnimationFrame = (fn) => queue.push(fn);
  return {
    run(ts) { queue.splice(0).forEach((fn) => fn(ts)); },
    get pending() { return queue.length; },
  };
}

const statsWith = (over) => async () => ({ ...(await OK_API.get_stats()), ...over });
// exact, so a tween can be read: below 1000 the panel doesn't round
const smallWords = (n) => statsWith({ total_words: n });
const num = (s) => Number(String(s).replace(/\D/g, ""));

test("opening Home shows the stats as they are: no count from zero", async () => {
  const h = mountHome({});
  const f = frames(h);
  await h.V.views.home.render({ entering: true });
  assert.equal(h["stat-words"].textContent, "12.5K");
  assert.equal(h["stat-saved"].textContent, "3h 42m");
  assert.equal(f.pending, 0, "nothing to tween on open");
});

test("a stat that changes while Home is on screen counts up over 600 ms", async () => {
  const api = { get_stats: smallWords(100) };
  const h = mountHome(api);
  const f = frames(h);
  await h.V.views.home.render({ entering: true });

  api.get_stats = statsWith({ total_words: 900, time_saved_seconds: 13920 });
  await h.V.views.home.render();
  assert.equal(num(h["stat-words"].textContent), 100, "starts from the old value");

  f.run(1000);                       // first frame: the clock starts here
  assert.equal(num(h["stat-words"].textContent), 100);
  f.run(1300);                       // halfway in time, well past halfway in value (ease-out)
  const mid = num(h["stat-words"].textContent);
  assert.ok(mid > 500 && mid < 900, `halfway frame showed ${mid}`);
  f.run(1600);
  assert.equal(h["stat-words"].textContent, "900");
  assert.equal(h["stat-saved"].textContent, "3h 52m", "time saved tweens its seconds and lands formatted");
  assert.equal(f.pending, 0, "the tween stops once it lands");
});

test("an unchanged stat schedules no frames", async () => {
  const h = mountHome({});
  const f = frames(h);
  await h.V.views.home.render({ entering: true });
  await h.V.views.home.render();
  assert.equal(f.pending, 0);
});

test("a new value mid-count carries on from what is on screen, never jumping back", async () => {
  const api = { get_stats: smallWords(100) };
  const h = mountHome(api);
  const f = frames(h);
  await h.V.views.home.render({ entering: true });
  api.get_stats = smallWords(500);
  await h.V.views.home.render();
  f.run(0);
  f.run(300);
  const shown = num(h["stat-words"].textContent);

  api.get_stats = smallWords(900);
  await h.V.views.home.render();
  f.run(500);
  assert.equal(num(h["stat-words"].textContent), shown, "the second count starts where the first stood");
  f.run(1100);
  assert.equal(h["stat-words"].textContent, "900");
});

test("reduced motion jumps a changed stat straight to its value", async () => {
  const api = { get_stats: smallWords(100) };
  const h = mountHome(api);
  const f = frames(h);
  h.window.matchMedia = (q) => ({ matches: /reduce/.test(q), addEventListener() {} });
  await h.V.views.home.render({ entering: true });
  api.get_stats = smallWords(900);
  await h.V.views.home.render();
  assert.equal(h["stat-words"].textContent, "900");
  assert.equal(f.pending, 0);
});

test("a feed refresh that fails mid-animation says so instead of an unhandled rejection", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const api = {};
  const h = mountHome(api);
  h["recent-groups"].animate = () => ({ finished: Promise.resolve() });  // lists.js animates it
  await h.V.views.home.render({ entering: true });

  api.get_history = async () => [{ id: "new", ts: 1_750_000_000, text: "fresh", words: 1, duration_s: 1 }, ...HISTORY];
  h.V.ui.itemRow = () => { throw new Error("bad row"); };
  await h.V.views.home.render();
  await settle();

  const toastText = h.toast.children.map((c) => c.textContent).join("");
  assert.match(toastText, /Recent dictations failed to update/);
});

/* ---------- polling ---------- */

test("while the microphone is off, permissions re-render every second", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  let calls = 0;
  const h = mountHome({ get_permissions: async () => {
    calls += 1;
    return (await MIC_OFF.get_permissions());
  } });
  await h.V.views.home.render();
  const before = calls;
  for (let i = 0; i < 3; i++) {
    t.mock.timers.tick(1000);
    await settle();   // each tick's calls finish before the next (see in-flight guard)
  }
  assert.ok(calls >= before + 3, `expected ≥3 polls, got ${calls - before}`);
});

test("while only the key is wrong, the poll re-reads the key but never the microphone", async (t) => {
  // With the switch on, get_permissions opens a stream to prove the mic works:
  // once a second would flash Windows' mic-in-use icon for nothing.
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  let perms = 0, keys = 0, hook = false;
  const h = mountHome({
    get_permissions: async () => { perms += 1; return { panes: MIC_PANES }; },
    get_hotkey_status: async () => {
      keys += 1;
      return hook ? { status: "active on Right Ctrl", state: "listening" }
        : { status: "the keyboard hook could not be installed — retrying", state: "off" };
    },
  });
  await h.V.views.home.render();
  const [p0, k0] = [perms, keys];
  for (let i = 0; i < 3; i++) {
    t.mock.timers.tick(1000);
    await settle();
  }
  assert.equal(perms, p0, "no stream opened by the poll");
  assert.ok(keys >= k0 + 3, "but the key is watched");
  hook = true;
  t.mock.timers.tick(1000);
  await settle();
  assert.equal(statusLabel(h), "Ready", "and Home notices the hook coming back");
});

const micPane = (granted) => ({ panes: [pane("microphone", "Microphone", granted)] });

test("polling stops once permissions flip to all granted", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  let fixed = false;
  let calls = 0;
  const h = mountHome({ get_permissions: async () => {
    calls += 1;
    return micPane(fixed);
  } });
  await h.V.views.home.render();
  fixed = true;
  t.mock.timers.tick(1000);
  await settle();
  assert.equal(h["perm-strip"].dataset.state, "ok");
  const after = calls;
  t.mock.timers.tick(5000);
  await settle();
  assert.equal(calls, after, "no get_permissions calls once nothing is blocked");
});

test("repeated renders while blocked don't stack poll intervals", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  let calls = 0;
  const h = mountHome({ get_permissions: async () => {
    calls += 1;
    return micPane(false);
  } });
  await h.V.views.home.render();
  await h.V.views.home.render();
  await h.V.views.home.renderPermissions();
  const before = calls;
  t.mock.timers.tick(1000);
  await settle();
  assert.equal(calls - before, 1, "one tick, one poll");
});

test("the poll tick does nothing while Home is hidden", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  let calls = 0;
  const h = mountHome({ get_permissions: async () => {
    calls += 1;
    return micPane(false);
  } });
  await h.V.views.home.render();
  h["view-home"].hidden = true;
  const before = calls;
  t.mock.timers.tick(3000);
  await settle();
  assert.equal(calls, before);
});

test("the poll tick does nothing while the window is hidden: the app stays resident", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  let calls = 0;
  const h = mountHome({ get_permissions: async () => {
    calls += 1;
    return micPane(false);
  } });
  await h.V.views.home.render();
  h.document.visibilityState = "hidden";
  const before = calls;
  t.mock.timers.tick(3000);
  await settle();
  assert.equal(calls, before);
});

test("a failing bridge doesn't toast every second from the poll", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  let broken = false;
  const h = mountHome({
    get_permissions: async () => {
      if (broken) throw new Error("bridge down");
      return micPane(false);
    },
    get_hotkey_status: async () => {
      if (broken) throw new Error("bridge down");
      return { state: "listening" };
    },
  });
  await h.V.views.home.render();
  broken = true;
  t.mock.timers.tick(3000);
  await settle();
  assert.equal(h.toast.children.map((c) => c.textContent).join(""), "",
    "the poll is quiet; user-initiated renders still toast");
  await h.V.views.home.renderPermissions();
  assert.match(h.toast.children.map((c) => c.textContent).join(""), /bridge down/);
});

test("a poll tick is skipped while the previous tick's calls are still pending", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  let calls = 0;
  let hang = false;
  const h = mountHome({ get_permissions: () => {
    calls += 1;
    return hang ? new Promise(() => {}) : MIC_OFF.get_permissions();
  } });
  await h.V.views.home.render();
  hang = true;
  const before = calls;
  t.mock.timers.tick(1000);
  await settle();
  assert.equal(calls, before + 1, "the first tick polls");
  for (let i = 0; i < 4; i++) {
    t.mock.timers.tick(1000);
    await settle();
  }
  assert.equal(calls, before + 1, "a slow bridge never gets a second overlapping poll");
});

/* ---------- the stats panel's wave and big number ---------- */

const dailyOf = (...words) => words.map((n, i) => ({ date: `2026-11-${i + 1}`, words: n, saved_seconds: 0 }));

test("the wave draws words per day: the peak is tallest, a silent day is a dot, today stands out", async () => {
  const h = mountHome({ get_stats: statsWith({ daily: dailyOf(0, 100, 500, 1, 250) }) });
  await h.V.views.home.render();
  const bars = h["stat-wave"].children;
  assert.equal(h["stat-wave-block"].hidden, false);
  assert.deepEqual([...bars].map((b) => b.style.height), ["4px", "9px", "46px", "8px", "23px"]);
  assert.ok(bars[0].className.includes("is-silent"));
  assert.ok(bars[4].className.includes("is-today"));
  assert.ok(!bars[3].className.includes("is-today"));
  assert.equal(h["stat-wave"].getAttribute("aria-label"), "Words per day: dictated on 4 of the last 5 days.");
});

test("a new dictation keeps the wave's bars, so it grows today's without replaying the rise", async () => {
  const api = { get_stats: statsWith({ daily: dailyOf(100, 50) }) };
  const h = mountHome(api);
  await h.V.views.home.render();
  const today = h["stat-wave"].children[1];
  api.get_stats = statsWith({ daily: dailyOf(100, 100) });
  await h.V.views.home.render();
  assert.equal(h["stat-wave"].children[1], today);
  assert.equal(today.style.height, "46px");
});

test("a new install's wave is all dots and today isn't marked", async () => {
  const h = mountHome({ get_stats: statsWith({ daily: dailyOf(0, 0, 0) }) });
  await h.V.views.home.render();
  const bars = [...h["stat-wave"].children];
  assert.ok(bars.every((b) => b.style.height === "4px" && b.className.includes("is-silent")));
  assert.ok(!bars[2].className.includes("is-today"));
  assert.equal(h["stat-wave"].getAttribute("aria-label"), "No dictations in the last 3 days.");
});

test("no daily series hides the wave and leaves the numbers", async () => {
  const h = mountHome({});
  h["stat-wave-block"].hidden = false;
  await h.V.views.home.render();
  assert.equal(h["stat-wave-block"].hidden, true);
  assert.equal(h["stat-words"].textContent, "12.5K");
});

test("the big number steps down so a long total stays on one line", async () => {
  const sizeFor = async (seconds) => {
    const h = mountHome({ get_stats: statsWith({ time_saved_seconds: seconds }) });
    await h.V.views.home.render();
    return [h["stat-saved"].textContent, h["stat-saved"].dataset.size];
  };
  assert.deepEqual(await sizeFor(13320), ["3h 42m", ""]);
  assert.deepEqual(await sizeFor(45240), ["12h 34m", "m"]);
  assert.deepEqual(await sizeFor(444300), ["123h 25m", "s"]);
});
