import test from "node:test";
import assert from "node:assert/strict";
import { loadVoisr } from "./load.mjs";
import { makeNode } from "./dom.mjs";

/* Load guard, same reasoning as settings.test.mjs's first test: settings.js
   must not touch the DOM at parse time, or this whole module (and the update
   controls added to it) could never be exercised outside a browser. */
test("settings.js exposes render/init after adding update controls", () => {
  const env = loadVoisr(["js/ui.js", "js/views/settings.js"]);
  const settings = env.V.views.settings;
  assert.equal(typeof settings.init, "function");
  assert.equal(typeof settings.render, "function");
});

/* ---------- settings.js: update controls ----------

   render() and init() gain an update-toggle checkbox, an app-version label
   and a "Check now" button. mountUpdateSettings mirrors settings.test.mjs's
   mountSettings, but only registers the ids these tests actually touch. */

// init() binds a listener onto every one of these, update controls included
// -- the same requirement settings.test.mjs's mountSettings documents -- so
// all of them must be registered or getElementById(...) hands back null and
// init() throws on the very first addEventListener.
const UPDATE_IDS = [
  "paste-toggle", "widget-toggle", "appearance-select", "launch-toggle",
  "model-select", "hotkey-current", "hotkey-status",
  "update-toggle", "app-version", "update-result", "update-check",
  "color-open", "color-popover", "clear-history", "toast",
];

function mountUpdateSettings(t, api) {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const env = loadVoisr(["js/ui.js", "js/views/settings.js"]);
  const nodes = {};
  for (const id of UPDATE_IDS) {
    nodes[id] = makeNode(id === "update-toggle" || id === "launch-toggle" || id === "paste-toggle"
      || id === "widget-toggle" ? "input"
      : id === "appearance-select" || id === "model-select" ? "select"
      : id === "update-check" || id === "clear-history" ? "button" : "div");
    env.register(id, nodes[id]);
  }
  nodes["color-popover"].hidden = true;
  const defaultApi = {
    get_config: async () => ({ check_for_updates: true }),
    get_hotkey_status: async () => ({ status: "active" }),
    get_models: async () => ({ models: [], active: null }),
    hotkey_display: async () => ({ display: "unset" }),
    get_update_status: async () => ({
      state: "idle", current_version: "1.0.0", version: null,
      notes: null, error: null, enabled: true,
    }),
    ...api,
  };
  env.V.api = () => defaultApi;
  return { ...env, ...nodes };
}

const settle = () => new Promise((r) => setImmediate(r));

test("render() shows the current version from state, and the toggle from config", async (t) => {
  const h = mountUpdateSettings(t, {
    get_config: async () => ({ check_for_updates: false }),
    get_update_status: async () => ({
      state: "ready", current_version: "1.2.3", version: "1.3.0",
      notes: "notes", error: null, enabled: true,
    }),
  });

  await h.V.views.settings.render();

  assert.equal(h["app-version"].textContent, "Voisr 1.2.3");
  assert.equal(h["update-toggle"].checked, false);
});

test("render() defaults the toggle on when check_for_updates is absent from config", async (t) => {
  const h = mountUpdateSettings(t, { get_config: async () => ({}) });
  await h.V.views.settings.render();
  assert.equal(h["update-toggle"].checked, true);
});

test("the update toggle saves on change and reverts on a failed save", async (t) => {
  const calls = [];
  const h = mountUpdateSettings(t, {
    set_check_for_updates: async (enabled) => { calls.push(enabled); return { ok: true, enabled }; },
  });
  h.V.views.settings.init();

  h["update-toggle"].checked = true;
  h["update-toggle"].dispatch("change");
  await settle();

  assert.deepEqual(calls, [true]);
  assert.equal(h["update-toggle"].checked, true, "a successful save keeps the checked state");
  assert.match(h.toast.children.map((c) => c.textContent).join(""), /Update checks on/);
});

test("a failed toggle save reverts the checkbox, like the launch toggle", async (t) => {
  const h = mountUpdateSettings(t, {
    set_check_for_updates: async () => ({ error: "disk full" }),
  });
  h.V.views.settings.init();

  h["update-toggle"].checked = true;
  h["update-toggle"].dispatch("change");
  await settle();

  assert.equal(h["update-toggle"].checked, false,
    "a save that failed must not leave the switch showing a state that was never persisted");
  assert.match(h.toast.children.map((c) => c.textContent).join(""), /Save update setting failed: disk full/);
});

test("Check now reports a ready update by version, never by up_to_date default", async (t) => {
  const h = mountUpdateSettings(t, {
    check_for_updates_now: async () => ({
      state: "ready", current_version: "1.0.0", version: "1.1.0",
      notes: "notes", error: null, enabled: true,
    }),
  });
  h.V.views.settings.init();

  h["update-check"].dispatch("click");
  await settle();

  assert.match(h["update-result"].textContent, /1\.1\.0 ready to install/);
});

test("Check now reports up to date when nothing is ready", async (t) => {
  const h = mountUpdateSettings(t, {
    check_for_updates_now: async () => ({
      state: "idle", current_version: "1.0.0", version: null,
      notes: null, error: null, enabled: true, up_to_date: true,
    }),
  });
  h.V.views.settings.init();

  h["update-check"].dispatch("click");
  await settle();

  assert.match(h["update-result"].textContent, /up to date/);
});

test("Check now surfaces an error inline", async (t) => {
  const h = mountUpdateSettings(t, {
    check_for_updates_now: async () => ({ error: "updates unavailable" }),
  });
  h.V.views.settings.init();

  h["update-check"].dispatch("click");
  await settle();

  assert.match(h["update-result"].textContent, /updates unavailable/);
});

/* The contradiction the plan's Step 4 draft had: already_checking:true means
   a check is still in flight (started by the background schedule, say), not
   a completed one -- reporting it as "up to date" would be a lie about a
   check that never actually ran to completion on this click. */
test("Check now reports an in-progress check, never as up to date, when one is already running", async (t) => {
  const h = mountUpdateSettings(t, {
    check_for_updates_now: async () => ({
      state: "checking", current_version: "1.0.0", version: null,
      notes: null, error: null, enabled: true, already_checking: true,
    }),
  });
  h.V.views.settings.init();

  h["update-check"].dispatch("click");
  await settle();

  assert.doesNotMatch(h["update-result"].textContent, /up to date/);
  assert.match(h["update-result"].textContent, /already/i);
});

test("Check now reports a download in progress, never as up to date, when state is downloading", async (t) => {
  const h = mountUpdateSettings(t, {
    check_for_updates_now: async () => ({
      state: "downloading", current_version: "1.0.0", version: "1.1.0",
      notes: null, error: null, enabled: true,
    }),
  });
  h.V.views.settings.init();

  h["update-check"].dispatch("click");
  await settle();

  assert.doesNotMatch(h["update-result"].textContent, /up to date/);
  assert.match(h["update-result"].textContent, /progress/i);
});

test("a stale version left over from a superseded release is not reported as ready unless state says ready", async (t) => {
  // UpdateChecker never clears its manifest, so `version` can carry a release
  // that is no longer offered. Only state === "ready" means installable.
  const h = mountUpdateSettings(t, {
    check_for_updates_now: async () => ({
      state: "idle", current_version: "1.0.0", version: "0.9.0",
      notes: null, error: null, enabled: true, up_to_date: true,
    }),
  });
  h.V.views.settings.init();

  h["update-check"].dispatch("click");
  await settle();

  assert.doesNotMatch(h["update-result"].textContent, /0\.9\.0/);
  assert.match(h["update-result"].textContent, /up to date/);
});
