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

/* ---------- overlap guards ----------

   Nothing previously stopped a second click while the first check was still
   in flight, and nothing stamped a response with the click that caused it --
   so two overlapping calls could resolve out of order and the earlier
   (stale) one could paint over the later, correct one. Mirrors main.js's
   #hotkey-capture: disable the button for the duration, plus a token so a
   response can tell whether it is still the latest thing asked for. */

test("clicking Check now twice while a check is in flight only starts one request", async (t) => {
  let resolveFirst;
  let calls = 0;
  const h = mountUpdateSettings(t, {
    check_for_updates_now: async () => {
      calls += 1;
      return new Promise((r) => { resolveFirst = r; });
    },
  });
  h.V.views.settings.init();

  h["update-check"].dispatch("click");
  assert.equal(h["update-check"].disabled, true, "the button disables itself for the duration of the check");
  h["update-check"].dispatch("click");   // a second click while the first is still pending
  await settle();

  assert.equal(calls, 1, "a second click while a check is in flight must not start a second request");

  resolveFirst({
    state: "ready", current_version: "1.0.0", version: "1.1.0",
    notes: null, error: null, enabled: true,
  });
  await settle();

  assert.equal(h["update-check"].disabled, false, "re-enabled once the check settles");
  assert.match(h["update-result"].textContent, /1\.1\.0 ready to install/);
});

test("a stale response from a superseded click cannot overwrite a newer one", async (t) => {
  // The button-disable guard is what a real click sequence hits, but this
  // pins the second, independent guard (the sequence token) that protects
  // against the same overlap however it might arise -- a response arriving
  // out of order relative to a newer one already in flight, which disabling
  // the button for THIS click cannot by itself rule out for a future trigger.
  // Re-enabling the button between dispatches models that: without the
  // token, whichever response lands last wins regardless of which click was
  // actually more recent.
  const responses = [];
  const h = mountUpdateSettings(t, {
    check_for_updates_now: async () => new Promise((r) => { responses.push(r); }),
  });
  h.V.views.settings.init();

  h["update-check"].dispatch("click");         // click A: the eventually-stale one
  h["update-check"].disabled = false;
  h["update-check"].dispatch("click");         // click B: the newer, superseding one
  assert.equal(responses.length, 2, "sanity: both clicks actually started a request");

  // B (the newer click) resolves first.
  responses[1]({
    state: "ready", current_version: "1.0.0", version: "2.0.0",
    notes: null, error: null, enabled: true,
  });
  await settle();
  assert.match(h["update-result"].textContent, /2\.0\.0 ready to install/);

  // A (now stale) resolves after -- must not be allowed to overwrite B's result.
  responses[0]({
    state: "idle", current_version: "1.0.0", version: null,
    notes: null, error: null, enabled: true, up_to_date: true,
  });
  await settle();
  assert.match(h["update-result"].textContent, /2\.0\.0 ready to install/,
    "a stale response landing after a newer one must not repaint over it");
});

test("Check now surfaces a thrown rejection, not only an {error} response", async (t) => {
  const h = mountUpdateSettings(t, {
    check_for_updates_now: async () => { throw new Error("bridge unreachable"); },
  });
  h.V.views.settings.init();

  h["update-check"].dispatch("click");
  await settle();

  assert.match(h["update-result"].textContent, /Check for updates failed: bridge unreachable/);
  assert.equal(h["update-check"].disabled, false, "re-enabled after a thrown rejection too");
});

/* ---------- render() ---------- */

test("render() falls back to an em dash when current_version is absent from status", async (t) => {
  const h = mountUpdateSettings(t, {
    get_update_status: async () => ({
      state: "idle", version: null, notes: null, error: null, enabled: true,
    }),
  });
  await h.V.views.settings.render();
  assert.equal(h["app-version"].textContent, "Voisr —");
});

test("render() clears a stale update-result left over from a previous check", async (t) => {
  const h = mountUpdateSettings(t, {});
  h["update-result"].textContent = " — 1.1.0 ready to install";

  await h.V.views.settings.render();

  assert.equal(h["update-result"].textContent, "",
    "a result from a check made before navigating away must not survive re-render");
});

/* ---------- home.js: update banner ----------

   Home's counterpart to the Settings row above: a persistent banner for a
   ready update, for the user who never opens Settings. Rendered from
   `state`, never from `version` alone -- the updater never clears its
   manifest, so `version` can still name a withdrawn release.

   homeEnv() monkeypatches document.getElementById directly (a plain object
   keyed by the three ids these tests touch) rather than going through
   env.register()'s full byId registry. The patch is applied to whichever
   document loadVoisr just installed as globalThis.document, and is restored
   via the returned `restore()` -- otherwise it would leak onto a document a
   later test in this file loads, exactly the failure mode Task 10's own
   tests would be exposed to if they run after these in file order. */

function homeEnv(status) {
  const env = loadVoisr(["js/ui.js", "js/views/home.js"]);
  const banner = makeNode("div");
  const nodes = {
    "update-banner": banner,
    "update-banner-text": makeNode("span"),
    "update-install": makeNode("button"),
  };
  const realGet = globalThis.document.getElementById;
  globalThis.document.getElementById = (id) =>
    nodes[id] || realGet.call(globalThis.document, id);
  const calls = [];
  globalThis.window.Voisr.api = () => ({
    get_update_status: async () => status,
    install_update: async () => { calls.push("install"); return { ok: true }; },
  });
  const restore = () => { globalThis.document.getElementById = realGet; };
  return { env, nodes, calls, restore };
}

test("a ready update renders the banner with its version and notes", async (t) => {
  const { env, nodes, restore } = homeEnv({
    state: "ready", version: "1.1.0", notes: "Adds auto-update.",
  });
  t.after(restore);
  await env.V.views.home.renderUpdateBanner();
  assert.equal(nodes["update-banner"].hidden, false);
  assert.match(nodes["update-banner-text"].textContent, /1\.1\.0/);
  assert.match(nodes["update-banner-text"].textContent, /Adds auto-update/);
});

test("no update means no banner", async (t) => {
  const { env, nodes, restore } = homeEnv({ state: "idle", version: null });
  t.after(restore);
  await env.V.views.home.renderUpdateBanner();
  assert.equal(nodes["update-banner"].hidden, true);
});

test("a failed status check hides the banner instead of throwing", async (t) => {
  const { env, nodes, restore } = homeEnv(null);
  t.after(restore);
  globalThis.window.Voisr.api = () => ({
    get_update_status: async () => { throw new Error("bridge down"); },
  });
  await assert.doesNotReject(() => env.V.views.home.renderUpdateBanner());
  assert.equal(nodes["update-banner"].hidden, true);
});

test("dismissing hides the banner and it does not come back this session", async (t) => {
  const { env, nodes, restore } = homeEnv({
    state: "ready", version: "1.1.0", notes: "Adds auto-update.",
  });
  t.after(restore);
  await env.V.views.home.renderUpdateBanner();
  env.V.views.home.dismissUpdateBanner();
  assert.equal(nodes["update-banner"].hidden, true);
  await env.V.views.home.renderUpdateBanner();
  assert.equal(nodes["update-banner"].hidden, true);
});

/* ---------- home.js: Install button ----------

   Omitted from the plan, but the fix pattern is already established at
   main.js's #hotkey-capture and settings.js's Check-now: disable the button
   for the duration of the call. Here it matters more than for Check-now -- a
   second click could fire a second install_update(). Python refuses it
   (installing is terminal, guarded by compare-and-set), so it is not
   catastrophic, but the user would still get a confusing second toast while
   the app is mid-shutdown.

   Asymmetric on success, unlike Check-now: install_update() succeeding means
   the app is about to exit, so the button is deliberately left disabled
   rather than re-enabled -- re-enabling would only invite a pointless second
   click during shutdown. */

test("the Install button guards against a double-click and stays disabled after a successful install", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { env, nodes, calls, restore } = homeEnv({
    state: "ready", version: "1.1.0", notes: "Adds auto-update.",
  });
  t.after(restore);
  // The click handler's very first toast ("Installing update…") needs a
  // #toast host to write into -- the four tests above never trigger a
  // toast, so homeEnv()'s minimal node set doesn't include one.
  nodes["toast"] = makeNode("div");
  await env.V.views.home.renderUpdateBanner();

  nodes["update-install"].dispatch("click");
  assert.equal(nodes["update-install"].disabled, true,
    "the button disables itself for the duration of the install");
  nodes["update-install"].dispatch("click");   // a second click while the first is still pending

  await settle();

  assert.deepEqual(calls, ["install"],
    "a second click while install is in flight must not start a second install");
  assert.equal(nodes["update-install"].disabled, true,
    "a successful install leaves the button disabled -- the app is about to exit");
  assert.match(nodes.toast.children.map((c) => c.textContent).join(""),
    /Installing update/);
});

test("a failed install re-enables the button and toasts the reason", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { env, nodes, restore } = homeEnv({
    state: "ready", version: "1.1.0", notes: "Adds auto-update.",
  });
  t.after(restore);
  nodes["toast"] = makeNode("div");
  globalThis.window.Voisr.api = () => ({
    get_update_status: async () => ({ state: "ready", version: "1.1.0", notes: null }),
    install_update: async () => ({ error: "disk full" }),
  });
  await env.V.views.home.renderUpdateBanner();

  nodes["update-install"].dispatch("click");
  await settle();

  assert.equal(nodes["update-install"].disabled, false,
    "a failed install must not leave the button stuck disabled");
  assert.match(nodes.toast.children.map((c) => c.textContent).join(""),
    /Install update failed: disk full/);
});
