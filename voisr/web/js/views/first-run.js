"use strict";
window.Voisr = window.Voisr || {};
(function (V) {
  /* Three steps over the whole window: the microphone, the key, a first
     dictation. It talks to the same bridge calls as Home and Settings, reads
     the grant with Home's rules (V.permissions), and never traps anyone: every
     step that can get stuck has a way out to Home, whose banner takes over
     from there.

     Windows gates one thing, the microphone privacy switch; the key needs no
     permission at all. So step 1 is the switch, and it is the only thing a
     step waits on. */

  const POLL_MS = 1000;
  const LEVEL_MS = 200;       // only "is the mic open?", not the shape of a voice
  const LISTENING_SPEED = 3.2;  // wave.js's listening pace
  const LEAVE_MS = 240;       // --slow, the fade into Home
  const tickSvg = (cls) => `<svg class="${cls}" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3.2 3.2L13 4.8"/></svg>`;

  /* What's on screen. `step` is 1, 2, 3 or "done". */
  let step = null, enterApp = null, leaving = false;
  let panes = null;
  let poll = 0, pollInFlight = false, levelPoll = 0, levelInFlight = false;
  let listening = false, capturing = false, waveEl = null;
  const capture = V.ui.captureGuard();
  let drawn = "";             // what the current step last drew, so polls stay quiet
  const granted = new Set();  // grants already shown as on: only a new one draws its tick

  const el = (...args) => V.ui.el(...args);
  const $ = (sel) => V.ui.$(sel);

  /* ---------- what's true ---------- */

  const mic = () => (panes || []).find((p) => p.key === "microphone");
  const micRefused = () => { const m = mic(); return !!m && m.granted === false; };
  const micPending = () => { const m = mic(); return !!m && V.permissions.micPending(m); };

  /* Fails quietly on a poll: the last answer stays on screen, and a toast a
     second would help nobody. The first read of a step says what went wrong. */
  async function readPanes(quiet) {
    let res = null;
    if (quiet) {
      try { res = await V.api().get_permissions(); } catch (e) { res = null; }
    } else {
      res = await V.ui.tryApi(V.api().get_permissions(), "Check permissions");
    }
    if (res && res.panes) panes = res.panes;
    syncPoll();
  }

  /* Only a refused microphone is worth watching every second: that's the one
     thing someone goes off to Settings to change. With the switch on,
     get_permissions() opens a stream to prove the mic works, and doing that
     once a second would flash Windows' mic-in-use icon the whole time. A
     refusal is read off the switch alone, with no stream. */
  function syncPoll() {
    const live = (step === 1 || step === 3) && micRefused();
    if (live && !poll) poll = setInterval(tick, POLL_MS);
    if (!live && poll) { clearInterval(poll); poll = 0; }
  }

  /* ---------- the frame ---------- */

  function setProgress() {
    const at = step === "done" ? 3 : step;
    const bar = $("#first-run-progress");
    bar.setAttribute("aria-label", `Step ${at} of 3`);
    Array.from(bar.children).forEach((seg, i) => seg.classList.toggle("is-on", i < at));
  }

  /* A new step replays the entrance from frame 0 and moves focus to its
     headline, so a screen reader starts there. A poll changing a step's content
     only swaps the slots, in place. */
  function paint(head, sub, body, foot, entering) {
    const box = $("#first-run-step");
    const hadFocus = entering || (document.activeElement && box.contains(document.activeElement));
    box.replaceChildren(
      el("header", { class: "first-run-head" },
        el("h1", { class: "heading-serif", tabindex: "-1", text: head }),
        el("p", { class: "first-run-sub", text: sub })),
      el("div", { class: "first-run-body" }, body),
      el("div", { class: "first-run-foot" }, foot));
    if (entering) {
      box.classList.remove("is-entering");
      void box.offsetWidth;  // force reflow, so the entrance restarts
      box.classList.add("is-entering");
    } else {
      box.classList.remove("is-entering");  // or the new children would play it
    }
    if (hadFocus) {
      const main = box.querySelector(".btn-primary");
      const target = !entering && main && !main.disabled ? main : box.querySelector(".heading-serif");
      if (target && target.focus) target.focus();
    }
  }

  function button(cls, text, onClick, attrs) {
    return el("button", { type: "button", class: cls, text, on: { click: onClick }, ...attrs });
  }

  function goTo(next) {
    step = next;
    drawn = "";
    capturing = false;
    setListening(false);
    if (waveEl && V.wave) V.wave.unmount(waveEl);
    waveEl = null;
    if (next) setProgress();  // leaving: the bar stays as it was while it fades
    window.removeEventListener("voisr:new-dictation", onDictation);
    if (next === 3) window.addEventListener("voisr:new-dictation", onDictation);
    syncPoll();
    if (next === 3 && !levelPoll) levelPoll = setInterval(tickLevel, LEVEL_MS);
    if (next !== 3 && levelPoll) { clearInterval(levelPoll); levelPoll = 0; }
    return draw(true);
  }

  function draw(entering) {
    if (step === 1) return drawPermissions(entering);
    if (step === 2) return drawKey(entering);
    if (step === 3) return drawDictation(entering);
    return undefined;
  }

  /* Granting happens in Settings, outside the app, so a step that waits on it
     keeps looking. It carries on while another app is in front, because that's
     where the switch is; only a hidden window stops it. */
  async function tick() {
    if (pollInFlight || document.visibilityState === "hidden") return;
    pollInFlight = true;
    try {
      await readPanes(true);
    } finally {
      pollInFlight = false;
    }
    draw(false);
  }

  /* ---------- step 1: the microphone ---------- */

  const WHY = {
    microphone: "To hear you",
  };

  function openButton(p) {
    return button("btn-signal", "Open Settings", V.permissions.openPane(p.key));
  }

  function permissionRow(p) {
    let why = WHY[p.key] || "", control;
    if (p.granted === true) {
      const cls = granted.has(p.key) ? "first-run-tick" : "first-run-tick is-new";
      control = el("span", { class: "first-run-on", html: `${tickSvg(cls)}On` });
      granted.add(p.key);
    } else if (V.permissions.micPending(p)) {
      // not a refusal: no device, or another app holding it
      why = "Couldn't check it. Is one plugged in?";
      control = el("span", { class: "pill", text: "Unchecked" });
    } else {
      why = "Switched off in Windows, so nothing can be recorded";
      control = openButton(p);
    }
    return el("div", { class: "first-run-row" },
      el("div", { class: "first-run-row-text" },
        el("span", { class: "first-run-row-name", text: p.label }),
        el("span", { class: "first-run-row-why", text: why })),
      control);
  }

  function drawPermissions(entering) {
    const shown = panes || [];
    const sig = JSON.stringify(shown.map((p) => [p.key, p.granted]));
    if (!entering && sig === drawn) return;
    // an unknown microphone isn't a refusal, so it doesn't hold anyone here
    const ok = !!panes && !micRefused();
    paint("One permission first",
      "Windows has to let Voisr use the microphone. Everything stays on this PC.",
      el("div", { class: "first-run-card", "aria-live": "polite" }, shown.map(permissionRow)),
      [button("btn-primary btn-full", "Continue", () => goTo(2), { disabled: ok ? null : "disabled" }),
       ok ? null : button("text-btn", "Set up later", finish)],
      entering);
    drawn = sig;
  }

  /* ---------- step 2: key ---------- */

  async function changeKey() {
    if (capturing || capture.recent()) return;
    capturing = true;
    await drawKey(false);
    // tryApi says why a timeout failed; Esc cancelling it needs no comment
    await V.ui.tryApi(V.api().capture_hotkey(), "Change key");
    capture.ended();
    if (step !== 2) return;
    capturing = false;
    await drawKey(false);
    const change = $("#first-run-step").querySelector(".btn-secondary");
    if (change && change.focus) change.focus();
  }

  async function drawKey(entering) {
    const res = await V.ui.tryApi(V.api().hotkey_display(), "Load shortcut");
    if (step !== 2) return;
    const key = V.ui.keyLabel(res && res.display);
    // The step is about this key, so a key with no symbol wears its own name.
    // A Windows keycap prints a word ("Ctrl", "Shift"), which needs a smaller
    // face than a glyph to fit the cap.
    const cap = key.symbol || key.name;
    const size = cap.length > 5 ? " is-word" : cap.length > 3 ? " is-label" : "";
    const on = capturing ? " is-listening" : "";
    paint("Pick the key you'll hold",
      "Hold it in any app and talk. Let go and the text lands where you were typing.",
      [el("kbd", { class: `first-run-keycap${size}${on}`, text: cap }),
       el("span", { class: `first-run-key-name${on}`, text: capturing ? "Press a key…" : key.name }),
       button("btn-secondary", "Change…", changeKey, { disabled: capturing ? "disabled" : null })],
      [button("btn-primary btn-full", "Continue", () => { if (!capture.recent()) goTo(3); },
              { disabled: capturing ? "disabled" : null })],
      entering);
  }

  /* ---------- step 3: first dictation ---------- */

  function setListening(on) {
    if (on === listening) return;
    listening = on;
    if (waveEl && V.wave) V.wave.setSpeed(waveEl, on ? LISTENING_SPEED : 1);
  }

  /* A level means a recorder is metering, so the mic is open. Not read without
     focus, which is when wave.js stops: this step can sit behind other apps for
     as long as the app stays resident. */
  async function tickLevel() {
    if (levelInFlight || !waveEl || !document.hasFocus()) return;
    levelInFlight = true;
    try {
      const level = await V.api().get_level();
      if (step === 3) setListening(level !== null && level !== undefined);
    } catch (e) {
      // the wave just keeps drifting
    } finally {
      levelInFlight = false;
    }
  }

  function drawDictation(entering) {
    const sig = micRefused() ? "refused" : micPending() ? "pending" : "ready";
    if (!entering && sig === drawn) return;
    drawn = sig;
    if (waveEl && V.wave) V.wave.unmount(waveEl);
    waveEl = null;
    if (sig === "refused") {
      paint("Voisr can't hear you",
        "Nothing can be recorded until microphone access is on in Settings → Privacy & security → Microphone.", null,
        [button("btn-signal btn-full", "Open Settings", V.permissions.openPane("microphone")),
         button("text-btn", "Skip", finish)], entering);
      return;
    }
    const wave = el("div", { class: "first-run-wave" });
    paint("Hold your key and say something",
      sig === "pending"
        ? "Anything. Only your PC is listening. If nothing happens, check a microphone is plugged in."
        : "Anything. Only your PC is listening.",
      wave, [button("btn-secondary", "Skip", finish)], entering);
    waveEl = wave;
    if (V.wave) {
      V.wave.mount(wave);
      if (listening) V.wave.setSpeed(wave, LISTENING_SPEED);
    }
  }

  /* The real pill says when a clip was too short or nothing was heard, and those
     leave no row, so this only ever hears about a dictation that landed. */
  async function onDictation() {
    if (step !== 3) return;
    const rows = await V.ui.tryApi(V.api().get_history(), "Load your dictation");
    if (step !== 3) return;
    const newest = (rows || []).reduce((a, b) => (!a || b.ts > a.ts ? b : a), null);
    showDone(newest);
  }

  function showDone(item) {
    goTo("done");
    const words = item && item.words;
    const line = words === 1 ? "1 word. It didn't leave this PC."
      : words ? `${words} words. None of them left this PC.`
      : "None of it left this PC.";
    paint("That's it", line,
      item ? el("p", { class: "first-run-said", text: item.text }) : null,
      [button("btn-primary btn-full", "Open Voisr", finish)], true);
    const open = $("#first-run-step").querySelector(".btn-primary");
    if (open && open.focus) open.focus();
  }

  /* ---------- the keyboard ----------

     First run hides the whole app, so a button Tab can't reach is a dead end.
     On the document, because a redrawn step can leave focus on the body. */
  function onKey(e) {
    if (e.key === "Tab" && step) V.ui.walkTab(e, $("#first-run"));
  }

  /* ---------- in and out ---------- */

  async function finish() {
    if (leaving) return;
    leaving = true;
    goTo(null);
    // a failed save only means it shows again next launch
    await V.ui.tryApi(V.api().set_config({ first_run_done: true }), "Save setup");
    const root = $("#first-run"), box = $("#first-run-step");
    $(".shell").hidden = false;
    enterApp();
    root.classList.add("is-leaving");
    setTimeout(() => {
      root.hidden = true;
      root.classList.remove("is-leaving");
      box.replaceChildren();
      leaving = false;
    }, LEAVE_MS);
  }

  /* Opens first run if it's still to do, and says whether it did. Home's views
     stay hidden meanwhile, which also keeps their polls and waves quiet. If it
     can't be asked, the app has to start anyway. It always opens on step 1:
     the Mac skips a step whose grant is already in, but on Windows the switch
     is usually on from the start, and skipping would leave a new user on step
     2 of 3 with no idea what step 1 was. A switch already on just shows its
     tick, and Continue. */
  async function start(onDone) {
    const res = await V.ui.tryApi(V.api().get_first_run(), "Check first run");
    if (!res || !res.pending) return false;
    enterApp = onDone;
    document.querySelectorAll(".view").forEach((v) => { v.hidden = true; });
    $(".shell").hidden = true;
    $("#first-run").hidden = false;
    try {
      await readPanes(false);
      for (const p of panes || []) if (p.granted === true) granted.add(p.key);
      await goTo(1);
    } catch (e) {
      // the app has to start anyway, and first run gets another go next launch
      goTo(null);
      $("#first-run").hidden = true;
      $(".shell").hidden = false;
      return false;
    }
    window.addEventListener("focus", () => {
      if (step === 1 || step === 3) tick();
    });
    document.addEventListener("keydown", onKey);
    return true;
  }

  V.views = V.views || {};
  V.views.firstRun = { start };
})(window.Voisr);
