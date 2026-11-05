import test from "node:test";
import assert from "node:assert/strict";
import { loadVoisr } from "./load.mjs";
import { makeNode } from "./dom.mjs";

/* ---------- first-run.js, driven through its real render paths ----------

   Polls and the level feed run on real setIntervals. Unref'd, a test that ends
   mid-step can't keep the process alive. */
const realSetInterval = globalThis.setInterval;
const realClearInterval = globalThis.clearInterval;
let intervals = [];  // what's running, so a test can fire one by hand
globalThis.setInterval = (fn, ms) => {
  const timer = realSetInterval(fn, ms);
  if (timer && typeof timer.unref === "function") timer.unref();
  intervals.push({ timer, fn, ms });
  return timer;
};
globalThis.clearInterval = (timer) => {
  intervals = intervals.filter((i) => i.timer !== timer);
  realClearInterval(timer);
};
const levelPoll = () => intervals.find((i) => i.ms === 200);
const panePoll = () => intervals.find((i) => i.ms === 1000);

const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };

/* Windows gates one thing: the microphone privacy switch. null is "couldn't
   check" (no device, or held exclusively), never a refusal. */
const pane = (key, label, over = {}) => ({ key, label, granted: true, ...over });
const MIC_ON = pane("microphone", "Microphone");
const MIC_UNCHECKED = { ...MIC_ON, granted: null };
const MIC_OFF = { ...MIC_ON, granted: false };

function mount(state = {}) {
  // each mount has its own copy of the page: the last one's polls are over
  intervals.forEach((i) => realClearInterval(i.timer));
  intervals = [];
  const env = loadVoisr(["js/ui.js", "js/views/home.js", "js/views/first-run.js"]);
  env.document.hasFocus = () => state.focused !== false;
  const ids = ["first-run", "first-run-step", "first-run-progress", "toast", "view-home"];
  const nodes = {};
  for (const id of ids) { nodes[id] = makeNode(); env.register(id, nodes[id]); }
  nodes["first-run"].hidden = true;
  nodes["first-run-progress"].append(makeNode("span"), makeNode("span"), makeNode("span"));
  const shell = makeNode();
  shell.className = "shell";
  nodes["view-home"].className = "view";
  env.document.body.append(shell, nodes["view-home"]);
  const calls = [];
  const s = { pending: true, panes: [MIC_OFF], level: null,
              display: "Right Ctrl", capture: { vk: 0x5D, label: "Menu" }, history: [], ...state };
  env.V.api = () => ({
    get_first_run: async () => (s.firstRunFails ? { error: "no" } : { pending: s.pending }),
    get_permissions: async () => { calls.push(["perms"]); return { panes: s.panes }; },
    open_settings_pane: async (key) => { calls.push(["open", key]); return { ok: true }; },
    hotkey_display: async () => ({ display: s.display }),
    capture_hotkey: async () => { calls.push(["capture"]); return s.capture; },
    get_level: async () => { calls.push(["level"]); return s.level; },
    get_history: async () => s.history,
    set_config: async (u) => { calls.push(["save", u]); return s.saveFails ? { error: "no" } : u; },
  });
  const entered = [];
  const start = () => env.V.views.firstRun.start(() => entered.push("app"));
  const box = nodes["first-run-step"];
  const all = (node, out = []) => {
    for (const kid of node.children || []) {
      if (kid && typeof kid === "object") { out.push(kid); all(kid, out); }
    }
    return out;
  };
  const btn = (text) => all(box).find((n) => n.tagName === "BUTTON" && n.textContent === text);
  const heading = () => box.querySelector(".heading-serif").textContent;
  const sub = () => box.querySelector(".first-run-sub").textContent;
  const rows = () => box.querySelectorAll(".first-run-row");
  return { ...env, ...nodes, shell, s, calls, entered, start, box, btn, heading, sub, rows, all };
}

test("nothing to do: it stays closed and says so", async () => {
  const h = mount({ pending: false });
  assert.equal(await h.start(), false);
  assert.equal(h["first-run"].hidden, true);
  assert.equal(h["view-home"].hidden, false);
});

test("a failed check can't stop the app starting", async () => {
  const h = mount({ firstRunFails: true });
  assert.equal(await h.start(), false);
  assert.equal(h["first-run"].hidden, true);
});

test("opening it hides the app behind, which keeps Home's polls quiet", async () => {
  const h = mount();
  assert.equal(await h.start(), true);
  assert.equal(h["first-run"].hidden, false);
  assert.equal(h["view-home"].hidden, true);
  assert.equal(h.entered.length, 0);
});

test("step 1 with the microphone switched off: Continue waits, and there's a way out", async () => {
  const h = mount();
  await h.start();
  assert.equal(h.heading(), "One permission first");
  assert.match(h.sub(), /Everything stays on this PC/);
  assert.equal(h.btn("Continue").attributes.disabled, "disabled");
  assert.ok(h.btn("Set up later"));
  assert.equal(h["first-run-progress"].getAttribute("aria-label"), "Step 1 of 3");
  const row = h.rows()[0];
  assert.equal(row.querySelector(".first-run-row-name").textContent, "Microphone");
  assert.equal(row.querySelector(".first-run-row-why").textContent,
               "Switched off in Windows, so nothing can be recorded");
  h.btn("Open Settings").click();
  await flush();
  assert.deepEqual(h.calls.filter(([n]) => n === "open"), [["open", "microphone"]]);
});

test("there is nothing to reset or relaunch on Windows", async () => {
  const h = mount();
  await h.start();
  assert.equal(h.all(h.box).filter((n) => /Reset|Relaunch/.test(n.textContent || "")).length, 0);
});

test("a microphone already on still opens on step 1, ticked, with Continue", async () => {
  // the Mac skips a finished step, but here the switch is usually on from the
  // start, and a new user would open on step 2 of 3
  const h = mount({ panes: [MIC_ON] });
  await h.start();
  await flush();
  assert.equal(h.heading(), "One permission first");
  assert.equal(h["first-run-progress"].getAttribute("aria-label"), "Step 1 of 3");
  const on = h.rows()[0].querySelector(".first-run-on");
  assert.match(on.innerHTML, /class="first-run-tick"/, "already on: no tick drawing itself in");
  assert.equal(h.btn("Continue").attributes.disabled, undefined);
  assert.equal(h.btn("Set up later"), undefined);
});

test("a microphone that couldn't be checked doesn't hold anyone back", async () => {
  const h = mount({ panes: [MIC_UNCHECKED] });
  await h.start();
  const row = h.rows()[0];
  assert.match(row.querySelector(".first-run-row-why").textContent, /Couldn't check it/);
  assert.equal(row.querySelector(".pill").textContent, "Unchecked");
  assert.equal(h.btn("Open Settings"), undefined);
  assert.equal(h.btn("Continue").attributes.disabled, undefined);
});

test("the switch going on, on a poll, unlocks Continue and draws the tick", async () => {
  const h = mount();
  await h.start();
  h.s.panes = [MIC_ON];
  await panePoll().fn();
  await flush();
  const row = h.rows()[0];
  assert.match(row.querySelector(".first-run-on").innerHTML, /first-run-tick is-new/);
  assert.equal(h.btn("Continue").attributes.disabled, undefined);
  assert.equal(h.btn("Set up later"), undefined);
});

test("the poll runs only while the microphone is refused, so no stream opens every second", async () => {
  const off = mount();
  await off.start();
  assert.ok(panePoll(), "a refusal is read off the switch, with no stream");
  off.s.panes = [MIC_ON];
  await panePoll().fn();
  await flush();
  assert.equal(panePoll(), undefined, "once it's on, nothing polls");

  const on = mount({ panes: [MIC_ON] });
  await on.start();
  assert.equal(panePoll(), undefined);
});

test("coming back to the window re-reads the switch", async () => {
  const h = mount({ panes: [MIC_ON] });
  await h.start();
  h.s.panes = [MIC_OFF];
  await h.window.dispatch("focus");
  await flush();
  assert.equal(h.btn("Continue").attributes.disabled, "disabled");
  assert.ok(panePoll(), "and watches it from then on");
});

test("a poll that changes nothing doesn't redraw, so focus and screen readers stay put", async () => {
  const h = mount();
  await h.start();
  const before = h.btn("Continue");
  await h.window.dispatch("focus");
  await flush();
  assert.equal(h.btn("Continue"), before);
});

test("Set up later saves it as done and enters the app", async () => {
  const h = mount();
  await h.start();
  h.btn("Set up later").click();
  await flush();
  assert.deepEqual(h.calls.filter(([n]) => n === "save"), [["save", { first_run_done: true }]]);
  assert.deepEqual(h.entered, ["app"]);
  assert.equal(h.shell.hidden, false);
  assert.ok(h["first-run"].classList.contains("is-leaving"));
});

test("a failed save still lets them in: it just shows again next launch", async () => {
  const h = mount({ saveFails: true });
  await h.start();
  h.btn("Set up later").click();
  await flush();
  assert.deepEqual(h.entered, ["app"]);
});

const toastText = (h) => h.all(h.toast).map((n) => n.textContent).filter(Boolean)
  .concat((h.toast.children || []).filter((c) => typeof c === "string"));

async function atKey(state) {
  const h = mount({ panes: [MIC_ON], ...state });
  await h.start();
  await flush();
  h.btn("Continue").click();
  await flush();
  h.calls.length = 0;
  return h;
}

test("step 2 shows the real key and changes it through capture_hotkey", async () => {
  const h = await atKey();
  assert.equal(h.box.querySelector(".first-run-keycap").textContent, "Ctrl");
  assert.equal(h.box.querySelector(".first-run-key-name").textContent, "Right Ctrl");
  h.s.display = "Menu";
  h.btn("Change…").click();
  assert.equal(h.box.querySelector(".first-run-key-name").textContent, "Right Ctrl", "drawn before the bridge answers");
  await flush();
  assert.deepEqual(h.calls, [["capture"]]);
  assert.equal(h.box.querySelector(".first-run-keycap").textContent, "Menu");
});

test("step 2 waits while it listens for a key", async () => {
  const h = await atKey();
  let answer;
  h.V.api = ((real) => () => ({ ...real(), capture_hotkey: () => new Promise((r) => { answer = r; }) }))(h.V.api);
  h.btn("Change…").click();
  await flush();
  assert.equal(h.box.querySelector(".first-run-key-name").textContent, "Press a key…");
  assert.equal(h.btn("Change…").attributes.disabled, "disabled");
  assert.equal(h.btn("Continue").attributes.disabled, "disabled");
  answer({ cancelled: true });
  await flush();
  assert.equal(h.box.querySelector(".first-run-key-name").textContent, "Right Ctrl");
  assert.equal(h.btn("Continue").attributes.disabled, undefined);
});

test("the key press that was captured can't also press a button", async () => {
  const h = await atKey();
  h.btn("Change…").click();
  await flush();
  h.btn("Continue").click();   // Return reaching the page just after Python answered
  await flush();
  assert.equal(h.heading(), "Pick the key you'll hold");
  h.btn("Change…").click();
  await flush();
  assert.equal(h.calls.filter(([name]) => name === "capture").length, 1);
});

test("step 2: Esc says nothing, a timeout says why", async () => {
  const cancelled = await atKey({ capture: { cancelled: true } });
  cancelled.btn("Change…").click();
  await flush();
  assert.deepEqual(toastText(cancelled), []);

  const timedOut = await atKey({ capture: { error: "no key detected" } });
  timedOut.btn("Change…").click();
  await flush();
  // tryApi says what failed, and either way the step is usable again
  assert.match(toastText(timedOut).join(" "), /no key detected/);
  assert.equal(timedOut.btn("Continue").attributes.disabled, undefined);
});

async function atDictation(state) {
  const h = await atKey(state);
  h.btn("Continue").click();
  await flush();
  return h;
}

test("step 3 mentions the microphone only if it couldn't be checked, and offers Skip", async () => {
  const on = await atDictation();
  assert.equal(on.heading(), "Hold your key and say something");
  assert.equal(on.sub(), "Anything. Only your PC is listening.");
  assert.ok(on.btn("Skip"));
  assert.equal(on["first-run-progress"].getAttribute("aria-label"), "Step 3 of 3");

  const unchecked = await atDictation({ panes: [MIC_UNCHECKED] });
  assert.match(unchecked.sub(), /check a microphone is plugged in/);
});

test("step 3 has no drawn pill: the real one floats at the bottom of the screen", async () => {
  const h = await atDictation();
  assert.equal(h.box.querySelector(".pill-preview"), null);
  assert.ok(h.box.querySelector(".first-run-wave"));
});

test("a refused mic swaps the step for the way to fix it, and back", async () => {
  const h = await atDictation();
  h.s.panes = [MIC_OFF];
  h.window.dispatch("focus");
  await flush();
  assert.equal(h.heading(), "Voisr can't hear you");
  assert.match(h.sub(), /Privacy & security → Microphone/);
  assert.equal(h.box.querySelector(".first-run-wave"), null);
  assert.ok(h.btn("Skip"));
  h.btn("Open Settings").click();
  await flush();
  assert.deepEqual(h.calls.at(-1), ["open", "microphone"]);
  h.s.panes = [MIC_ON];
  await panePoll().fn();
  await flush();
  assert.equal(h.heading(), "Hold your key and say something", "the poll notices it coming back");
});

test("a dictation landing finishes the step by itself, and shows the newest", async () => {
  const h = await atDictation({
    history: [{ id: "old", ts: 1, text: "older", words: 1 },
              { id: "new", ts: 9, text: "Testing, testing. Is this thing writing down everything I say?", words: 10 }],
  });
  h.window.dispatch("voisr:new-dictation");
  await flush();
  assert.equal(h.heading(), "That's it");
  assert.equal(h.sub(), "10 words. None of them left this PC.");
  assert.equal(h.box.querySelector(".first-run-said").textContent,
               "Testing, testing. Is this thing writing down everything I say?");
  h.btn("Open Voisr").click();
  await flush();
  assert.deepEqual(h.calls.at(-1), ["save", { first_run_done: true }]);
  assert.deepEqual(h.entered, ["app"]);
});

test("one word reads as one word", async () => {
  const h = await atDictation({ history: [{ id: "a", ts: 1, text: "Testing.", words: 1 }] });
  h.window.dispatch("voisr:new-dictation");
  await flush();
  assert.equal(h.sub(), "1 word. It didn't leave this PC.");
});

test("a dictation before step 3 doesn't finish anything", async () => {
  const h = await atKey({ history: [{ id: "a", ts: 1, text: "stray", words: 1 }] });
  h.window.dispatch("voisr:new-dictation");
  await flush();
  assert.equal(h.heading(), "Pick the key you'll hold");
});

test("Skip goes to the app without a dictation", async () => {
  const h = await atDictation();
  h.btn("Skip").click();
  await flush();
  assert.deepEqual(h.calls.at(-1), ["save", { first_run_done: true }]);
  assert.deepEqual(h.entered, ["app"]);
});

test("finishing twice only enters once", async () => {
  const h = await atDictation();
  const skip = h.btn("Skip");
  skip.click();
  skip.click();
  await flush();
  assert.deepEqual(h.entered, ["app"]);
});

test("a repaint in place doesn't replay the step's entrance", async () => {
  const h = await atKey();
  assert.ok(h.box.classList.contains("is-entering"), "a new step does animate in");
  h.btn("Change…").click();
  await flush();
  assert.equal(h.box.classList.contains("is-entering"), false);
});

test("a grant arriving on a poll doesn't replay the entrance either", async () => {
  const h = mount();
  await h.start();
  h.s.panes = [MIC_ON];
  await panePoll().fn();
  await flush();
  assert.equal(h.box.classList.contains("is-entering"), false);
});

test("finishing leaves the progress bar alone while it fades", async () => {
  const h = await atDictation();
  h.btn("Skip").click();
  await flush();
  assert.equal(h["first-run-progress"].getAttribute("aria-label"), "Step 3 of 3");
  assert.ok(h["first-run-progress"].children.every((seg) => seg.classList.contains("is-on")));
});

function withWave(h) {
  const speeds = [];
  h.V.wave = { mount() {}, unmount() {}, setSpeed: (el, n) => speeds.push(n) };
  return speeds;
}

test("the wave answers the mic, and settles when it closes", async () => {
  const h = await atKey({ level: 0.4 });
  const speeds = withWave(h);
  h.btn("Continue").click();
  await flush();
  await levelPoll().fn();
  await flush();
  assert.deepEqual(speeds, [3.2]);
  h.s.level = null;
  await levelPoll().fn();
  await flush();
  assert.deepEqual(speeds, [3.2, 1]);
});

test("the level isn't read while the window isn't focused", async () => {
  const h = await atDictation({ focused: false, level: 0.4 });
  await levelPoll().fn();
  await flush();
  assert.equal(h.calls.filter(([name]) => name === "level").length, 0);
});

test("the level poll only runs on step 3", async () => {
  const h = await atDictation();
  assert.ok(levelPoll());
  h.btn("Skip").click();
  await flush();
  assert.equal(levelPoll(), undefined);
});

test("an error while opening still lets the app start", async () => {
  const h = mount();
  h.register("first-run-progress", null);
  assert.equal(await h.start(), false);
  assert.equal(h.shell.hidden, false);
  assert.equal(h["first-run"].hidden, true);
});

test("an error drawing step 1 still lets the app start", async () => {
  const h = mount({ panes: [MIC_ON] });
  h.register("first-run-step", null);
  assert.equal(await h.start(), false);
  assert.equal(h.shell.hidden, false);
  assert.equal(h["first-run"].hidden, true);
});

/* ---------- the keyboard ----------

   The fake DOM has no focus and no layout, and only takes single selectors, so
   this mount adds what the walker reads: focus() moves document.activeElement,
   anything not marked hidden has a box, and querySelectorAll answers for the
   controls. The real selector and WebKit's own keys need a real page. */
function mountKeys(state) {
  const h = mount(state);
  const create = h.document.createElement;
  h.document.createElement = (tag) => {
    const node = create(tag);
    node.focus = () => { h.document.activeElement = node; };
    node.getClientRects = () => (node.hidden ? [] : [{}]);
    const setAttribute = node.setAttribute;
    node.setAttribute = (k, v) => {
      setAttribute(k, v);
      if (k === "disabled") node.disabled = true;  // reflected, as in a browser
    };
    return node;
  };
  h["first-run"].append(h.box);
  h["first-run"].querySelectorAll = (sel) => {
    assert.equal(sel, "button, input, select, textarea, summary");
    return h.all(h.box).filter((n) => n.tagName === "BUTTON");
  };
  const press = (over = {}) => {
    const ev = { key: "Tab", shiftKey: false, preventDefault() { ev.prevented = true; }, ...over };
    h.document.dispatch("keydown", ev);
    return ev;
  };
  const focused = () => h.document.activeElement && h.document.activeElement.textContent;
  return { ...h, press, focused };
}

for (const [display, cap, name, size] of [
  ["Right Ctrl", "Ctrl", "Right Ctrl", "is-label"],
  ["Left Alt", "Alt", "Left Alt", ""],
  ["Right Shift", "Shift", "Right Shift", "is-label"],
  ["Right Win", "Win", "Right Win", ""],
  ["Caps Lock", "Caps", "Caps Lock", "is-label"],
  ["F13", "F13", "F13", ""],
  ["Scroll Lock", "Scroll Lock", "Scroll Lock", "is-word"],
  ["VK 0x5D", "VK 0x5D", "VK 0x5D", "is-word"],
]) {
  test(`the key step always has a keycap that fits: ${display}`, async () => {
    const h = await atKey({ display });
    const keycap = h.box.querySelector(".first-run-keycap");
    assert.equal(keycap.textContent, cap);
    for (const cls of ["is-word", "is-label"]) {
      assert.equal(keycap.className.includes(cls), cls === size, `${cls} on ${display}`);
    }
    assert.equal(h.box.querySelector(".first-run-key-name").textContent, name);
  });
}

test("each step opens with focus on its headline, for a screen reader", async () => {
  const h = mountKeys({ panes: [MIC_ON] });
  await h.start();
  assert.equal(h.document.activeElement.textContent, h.heading());
  h.btn("Continue").click();
  await flush();
  assert.equal(h.heading(), "Pick the key you'll hold");
  assert.equal(h.document.activeElement.textContent, h.heading());
  h.btn("Continue").click();
  await flush();
  assert.equal(h.heading(), "Hold your key and say something");
  assert.equal(h.document.activeElement.textContent, h.heading());
});

test("Tab reaches every button in the permissions step and wraps inside first run", async () => {
  const h = mountKeys();
  await h.start();
  assert.equal(h.heading(), "One permission first");
  const ev = h.press();
  assert.equal(ev.prevented, true, "walked by hand, so it stays inside first run");
  assert.equal(h.focused(), "Open Settings");
  h.press();
  assert.equal(h.focused(), "Set up later");
  h.press();
  assert.equal(h.focused(), "Open Settings", "wraps; a disabled Continue is skipped");
});

test("Shift-Tab walks back, and from the headline lands on the last button", async () => {
  const h = mountKeys();
  await h.start();
  h.press({ shiftKey: true });
  assert.equal(h.focused(), "Set up later");
  h.press({ shiftKey: true });
  assert.equal(h.focused(), "Open Settings");
});

test("Tab reaches the key step's buttons, and stays inside first run during a capture", async () => {
  const h = mountKeys({ panes: [MIC_ON] });
  let release;
  h.s.capture = new Promise((r) => { release = r; });
  await h.start();
  h.btn("Continue").click();
  await flush();
  assert.equal(h.heading(), "Pick the key you'll hold");
  h.press();
  assert.equal(h.focused(), "Change…");
  h.press();
  assert.equal(h.focused(), "Continue");
  h.press();
  assert.equal(h.focused(), "Change…");

  h.btn("Change…").click();
  await flush();
  const ev = h.press();
  assert.equal(ev.prevented, true, "nothing to reach, and Tab still doesn't leave");
  release({ vk: 0x5D, label: "Menu" });
  await flush();
  assert.equal(h.focused(), "Change…", "the capture hands focus back to its button");
});

test("Tab reaches Skip on the dictation step and Open Voisr at the end", async () => {
  const h = mountKeys({ panes: [MIC_ON] });
  await h.start();
  h.btn("Continue").click();
  await flush();
  h.btn("Continue").click();
  await flush();
  h.press();
  assert.equal(h.focused(), "Skip");
  h.press();
  assert.equal(h.focused(), "Skip", "one button wraps to itself");
  h.s.history = [{ id: "a", ts: 1, words: 2, text: "Hello there" }];
  h.window.dispatch("voisr:new-dictation");
  await flush();
  assert.equal(h.focused(), "Open Voisr");
  h.press();
  assert.equal(h.focused(), "Open Voisr");
});

test("a focused button that a redraw took away doesn't lose the walk", async () => {
  const h = mountKeys();
  await h.start();
  h.document.activeElement = h.document.body;
  h.press();
  assert.equal(h.focused(), "Open Settings");
});

test("other keys are left alone, and leaving ends the walk", async () => {
  const h = mountKeys();
  await h.start();
  assert.equal(h.press({ key: "a" }).prevented, undefined);
  assert.equal(h.press().prevented, true);

  h.btn("Set up later").click();
  await flush();
  assert.equal(h.press().prevented, undefined, "the app has its own keys now");
});
