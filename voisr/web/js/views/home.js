"use strict";
window.Voisr = window.Voisr || {};
(function (V) {
  /* What a missing grant costs you: the banner leads with the consequence, not
     the name of the setting. Windows gates one thing, the microphone privacy
     switch (voisr/permissions.py); the key itself needs no permission. */
  const MIC_BREAK = "Nothing can be recorded until microphone access is on in " +
                    "Settings → Privacy & security → Microphone.";
  const OFF_BREAK =
    "Holding the key does nothing right now. If it doesn't come back in a " +
    "moment, quit Voisr from its tray icon and open it again.";

  const HINT_PASTE = "Let go and it lands at your cursor.";
  const HINT_COPY = "Let go and it's copied to your clipboard.";

  const MIC_UNCHECKED = "couldn't check — plugged in?";

  const PERM_WHY = {
    microphone: "needed to hear you",
  };
  const whyText = (p) => PERM_WHY[p.key] || "";

  /* A null mic is "couldn't check": the probe couldn't open a stream (no
     device, or another app holding it exclusively). That is not a refusal, so
     it never blocks: the next dictation says what is actually wrong. */
  const micPending = (p) => p.key === "microphone" && p.granted === null;
  const blocking = (panes) => panes.filter((p) => p.granted !== true && !micPending(p));

  const listLabels = (panes) => {
    const names = panes.map((p) => p.label);
    return names.length < 2 ? names.join("")
      : `${names.slice(0, -1).join(", ")} and ${names.at(-1)}`;
  };

  /* ---------- what is true right now ----------

     Everything Home says about whether dictation works is decided here, from
     the permission panes and the listener's state:

       ready     the key is listening and the microphone is on
       pending   the same, but the microphone couldn't be checked
       starting  the hook is coming up or held for a moment: undecided, not broken
       busy      a click-to-record owns the mic, so the key is paused
       unknown   a check failed: never claims Ready, never raises the alarm
       needs     the blue banner: the microphone is off, or the key isn't listening */
  function readiness(panes, listener) {
    const state = listener.state;
    // Off but not stopped: the hook is coming up, hasn't started yet, or is held
    // for a moment by Settings' key capture, which leaves "active on ..." in place.
    const starting = state === "off" && /^(starting|not started|active)/.test(listener.status || "");
    if (!panes) {
      if (state === "off" && !starting) return { kind: "off", checks: null };
      return { kind: starting ? "starting" : "unknown", checks: null };
    }
    const bad = blocking(panes);
    if (bad.length) {
      const lead = bad.find((p) => p.granted === false) || bad[0];
      return { kind: "mic", lead, panes, checks: panes };
    }
    if (state === "off" && !starting) return { kind: "off", panes, checks: panes };
    if (starting) return { kind: "starting", checks: null };
    if (state === "busy") return { kind: "busy", checks: null };
    if (state !== "listening") return { kind: "unknown", checks: null };
    if (panes.some(micPending)) return { kind: "pending", checks: null };
    return { kind: "ready", panes, checks: null };
  }

  const NEEDS_BANNER = new Set(["mic", "off"]);
  const NEEDS_YOU = NEEDS_BANNER;
  // #perm-strip's data-state, which is what the strip is announcing
  const stripState = (kind) => (NEEDS_YOU.has(kind) ? "attention" : kind === "pending" ? "pending" : "ok");

  function statusFor(r, listener) {
    switch (r.kind) {
      case "ready": return { label: "Ready", title: `${listLabels(r.panes)} granted.` };
      case "pending": return { label: "Mic unchecked",
        detail: "Couldn't check the microphone. If a dictation comes back silent, check it's plugged in." };
      case "starting": return { label: "Starting…", detail: "The key will work in a moment." };
      case "busy": return { label: "Recording here", detail: "The key is paused until you finish." };
      case "unknown": return { label: "Status unknown", detail: "Couldn't check the key." };
      case "mic": return { label: "Needs you", dot: true, detail: "Nothing records until the microphone is back on." };
      default: {  // off
        const said = (listener.status || "").trim();
        return { label: "Needs you", dot: true,
                 detail: said ? `${said[0].toUpperCase()}${said.slice(1)}.`.replace(/\.\.$/, ".")
                   : "Push-to-talk isn't running." };
      }
    }
  }

  function openPane(key) {
    return async () => {
      // open_settings_pane answers {"ok": bool}: a false is Windows refusing
      // to open the page, which used to vanish silently and leave the user
      // clicking a control that appeared to do nothing.
      const res = await V.ui.tryApi(
        V.api().open_settings_pane(key), "Open Settings");
      if (res && res.ok === false) V.ui.toast("Couldn't open Settings");
    };
  }

  /* Recording here still works while the key isn't listening; without the
     microphone nothing does, so no second path. Only from idle: toggleRecord
     would stop a recording already running, which is not what "Click to
     record" says. */
  function recordButton(panes) {
    if ((panes || []).some((p) => p.key === "microphone" && p.granted === false)) return null;
    return V.ui.el("button", { type: "button", class: "btn-secondary", text: "Click to record",
      on: { click: () => { if (recState === "idle") toggleRecord(); } } });
  }

  /* The blue banner. A dead key is not a footnote: the whole product does
     nothing when you hold it, and a banner still saying "hold this key to
     dictate" would be a lie. */
  function needsBanner(r) {
    const el = V.ui.el;
    let heading, body, actions;
    if (r.kind === "off") {
      heading = "The key isn't listening";
      body = OFF_BREAK;
      actions = [recordButton(r.panes)];
    } else {
      heading = "Voisr can't hear you";
      body = MIC_BREAK;
      actions = [el("button", { type: "button", class: "btn-signal", text: "Open Microphone settings",
                                on: { click: openPane(r.lead.key) } })];
    }
    return el("section", { class: "banner banner-needs-you", "aria-label": "Permission problem" },
      el("h2", { class: "banner-title", text: heading }),
      el("p", { class: "banner-sub", text: body }),
      el("p", { class: "banner-error", role: "alert", text: recordError }),
      el("div", { class: "banner-actions" }, actions));
  }

  /* Every pane stays listed, so one trip to Settings can clear them all. */
  function permChecks(panes) {
    const el = V.ui.el;
    return el("ul", { class: "perm-checks" }, panes.map((p) => {
      const state = p.granted === true ? "ok" : p.granted === false ? "denied" : "unknown";
      const mark = state === "ok" ? "✓" : state === "denied" ? "!" : "?";
      const why = state === "ok" ? "granted"
        : micPending(p) ? MIC_UNCHECKED : whyText(p);
      return el("li", { class: `perm-${state}-row` },
        el("span", { class: "perm-mark", "aria-hidden": "true", text: mark }),
        el("span", { class: "perm-name", text: p.label }),
        el("span", { class: "perm-why", text: why }));
    }));
  }

  /* The last answers from the bridge. The panes survive a failed read: a stale
     banner beside a toast explaining the staleness beats an empty one. */
  let lastPanes = null, permsRead = false, lastKind = "unknown";
  let listener = { state: "unknown", status: "" }, listenerRead = false;
  let pasteOff = false;

  let permPoll = null;
  // A tick's bridge calls can outlast the 1 s interval on a slow bridge;
  // overlapping ticks would stack calls. Skip a tick until the last settles.
  let pollInFlight = false;

  /* A bridge call that, when `quiet`, swallows its failure instead of
     toasting it: the 1 s poll would otherwise toast twice a second for as
     long as the bridge stayed broken. User-initiated renders keep toasting. */
  async function callApi(call, label, quiet) {
    if (!quiet) return V.ui.tryApi(call(), label);
    try {
      const res = await call();
      return res && res.error ? null : res;
    } catch (e) {
      return null;
    }
  }

  /* Granting happens in Settings and the hook can come up late, so while
     anything is wrong Home re-checks itself every second. Nothing runs while
     Home or the window is hidden: this app stays resident.

     The microphone is only re-read while it is the problem. With the privacy
     switch on, get_permissions() opens a stream to prove the mic works, and a
     poll doing that every second would flash Windows' mic-in-use icon for as
     long as the key was the thing that was wrong. */
  function syncPoll(kind) {
    const wanted = NEEDS_YOU.has(kind) || kind === "starting";
    if (wanted && permPoll === null) {
      permPoll = setInterval(() => {
        if (document.getElementById("view-home").hidden) return;
        if (document.visibilityState === "hidden") return;
        if (pollInFlight) return;
        pollInFlight = true;
        Promise.allSettled([
          lastKind === "mic" ? renderPermissions({ quiet: true }) : null,
          renderListener({ quiet: true }),
        ]).then(() => { pollInFlight = false; });
      }, 1000);
    } else if (!wanted && permPoll !== null) {
      clearInterval(permPoll);
      permPoll = null;
    }
  }

  /* Both are aria-live, and replacing their children makes a screen reader read
     them again: on every window focus, it re-announced what hadn't changed. So
     each re-renders only when what it says actually moved. */
  function renderStatus(r) {
    const el = V.ui.el;
    const box = V.ui.$("#home-status");
    const status = statusFor(r, listener);
    const checks = r.checks ? r.checks.map((p) => [p.key, p.granted]) : null;
    const sig = JSON.stringify([status.label, status.detail, status.title, status.action && status.action.key, checks]);
    if (box.dataset.sig === sig) return;
    box.dataset.sig = sig;
    box.dataset.state = r.kind;
    const parts = [
      el("span", { class: "status-label", title: status.title },
        status.dot ? el("span", { class: "status-dot", "aria-hidden": "true" }) : null,
        el("span", { text: status.label })),
      status.detail ? el("span", { class: "status-detail", text: status.detail }) : null,
    ].filter(Boolean);
    if (status.action) {
      parts.push(el("button", { type: "button", class: "btn-secondary status-action",
                                text: `Open ${status.action.label}`,
                                on: { click: openPane(status.action.key) } }));
    }
    if (r.checks) parts.push(permChecks(r.checks));
    box.replaceChildren(...parts);
  }

  /* The dark banner carries a recording in progress, even from under a blue one:
     "Click to record" there has to show the mic is open. */
  function syncBanners() {
    const strip = V.ui.$("#perm-strip");
    const hero = V.ui.$("#home-hero");
    const showBlue = strip.children.length > 0 && !recordingShown();
    strip.hidden = !showBlue;
    hero.hidden = showBlue;
  }

  function apply() {
    // until the permissions have answered once, a stopped listener isn't news:
    // it stops for a missing grant too, and that gets a better banner
    const r = !permsRead ? { kind: "unknown", checks: null }
      : readiness(lastPanes, listener);
    lastKind = r.kind;
    const strip = V.ui.$("#perm-strip");
    strip.dataset.state = stripState(r.kind);
    const blue = NEEDS_BANNER.has(r.kind);
    const sig = blue
      ? JSON.stringify([r.kind, (r.panes || []).map((p) => [p.key, p.granted])])
      : "";
    if (strip.dataset.sig !== sig) {
      strip.dataset.sig = sig;
      strip.replaceChildren(...(blue ? [needsBanner(r)] : []));
    }
    syncBanners();
    // both answers are in, or it would flash "Status unknown" as Home opens
    if ((permsRead && listenerRead) || NEEDS_YOU.has(r.kind)) renderStatus(r);
    V.ui.$("#home-hint").textContent = pasteOff ? HINT_COPY : HINT_PASTE;
    syncPoll(r.kind);
  }

  async function renderPermissions(opts = {}) {
    const res = await callApi(() => V.api().get_permissions(), "Load permissions", opts.quiet);
    if (res && res.panes) lastPanes = res.panes;
    permsRead = true;
    apply();
  }

  /* Reports the listener as it is, from get_hotkey_status(). */
  async function renderListener(opts = {}) {
    const res = await callApi(() => V.api().get_hotkey_status(), "Load shortcut status", opts.quiet);
    const known = res && ["listening", "busy", "off"].includes(res.state);
    listener = known ? { state: res.state, status: res.status || "" } : { state: "unknown", status: "" };
    listenerRead = true;
    apply();
  }

  // granting a permission happens in Settings, outside the app;
  // refresh when the user comes back so Home never shows a stale state
  window.addEventListener("focus", () => {
    if (document.getElementById("view-home").hidden) return;
    // Background refresh, so it is deliberately not awaited, but it must
    // still be caught. Bridge failures already toast inside
    // renderPermissions; this catch only covers a genuine throw, which
    // would otherwise surface as an unhandled rejection nobody sees.
    renderPermissions().catch(() => V.ui.toast("Permissions failed to refresh"));
    renderListener().catch(() => {});
  });

  async function onCopy(item) {
    if (!await V.ui.tryApi(V.api().copy_text(item.text), "Copy")) return;
    V.ui.toast("Copied to clipboard");
  }

  /* Mirrors views/history.js's onDelete exactly: the same undo contract has
     to hold on both surfaces the user can delete from. */
  async function onDelete(item) {
    if (!await V.ui.tryApi(V.api().delete_history_item(item.id), "Delete dictation")) return;
    await render();
    V.ui.offerDictationUndo({
      restore: () => V.ui.tryApi(V.api().restore_history_item(item), "Restore dictation"),
      refresh: render,
    });
  }

  function greeting() {
    const h = new Date().getHours();
    if (h < 5) return "Working late";
    if (h < 12) return "Good morning";
    if (h < 17) return "Good afternoon";
    return "Good evening";
  }

  /* The banner names the key from hotkey_display(), so the raw config value
     (e.g. "ctrl_r") never reaches it. A label's symbol stays out of a headline:
     "Name (symbol)" reads "Name". */
  async function renderPtt() {
    const res = await V.ui.tryApi(V.api().hotkey_display(), "Load shortcut");
    if (!res) return;
    const shown = V.ui.keyLabel(res.display).name;
    V.ui.$("#ptt-key-name").textContent = shown !== "unset" ? shown : "your key";
  }

  /* ---------- stats count up ----------

     A stat that changes while Home is on screen tweens from the old value
     to the new over 600 ms on --ease. Opening Home shows the numbers as
     they are, never a count from zero, and so does reduced motion. A new
     value mid-count carries on from whatever is on screen. */
  const COUNT_MS = 600;

  /* cubic-bezier(.2, .8, .2, 1), the --ease token: solve x(t) = p by
     Newton's method (x' never reaches zero on this curve), answer y(t). */
  const ease = (() => {
    const [x1, y1, x2, y2] = [0.2, 0.8, 0.2, 1];
    const cx = 3 * x1, bx = 3 * (x2 - x1) - cx, ax = 1 - cx - bx;
    const cy = 3 * y1, by = 3 * (y2 - y1) - cy, ay = 1 - cy - by;
    return (p) => {
      let t = p;
      for (let i = 0; i < 8; i++) {
        const err = ((ax * t + bx) * t + cx) * t - p;
        if (Math.abs(err) < 1e-6) break;
        t -= err / ((3 * ax * t + 2 * bx) * t + cx);
      }
      t = Math.min(1, Math.max(0, t));
      return ((ay * t + by) * t + cy) * t;
    };
  })();

  const counters = new Map();  // node -> { value } on screen, one per count

  function countTo(node, to, format, animate) {
    const prev = counters.get(node);
    const from = prev ? prev.value : to;
    const state = { value: from };
    counters.set(node, state);
    const canTween = animate && from !== to && typeof window.requestAnimationFrame === "function"
      && !(V.lists && V.lists.reduced());
    if (!canTween) {
      state.value = to;
      node.textContent = format(to);
      return;
    }
    let start = null;
    const frame = (now) => {
      if (counters.get(node) !== state) return;  // a newer count took over
      if (start === null) start = now;
      const p = Math.min(1, (now - start) / COUNT_MS);
      state.value = p === 1 ? to : from + (to - from) * ease(p);
      node.textContent = format(state.value);
      if (p < 1) window.requestAnimationFrame(frame);
    };
    window.requestAnimationFrame(frame);
  }

  /* "41.3K": the panel's numbers are for glancing at; the exact count is on hover. */
  function compactCount(value) {
    const n = Math.round(value);
    if (n < 1000) return n.toLocaleString();
    const short = (x) => (Math.round(x * 10) / 10).toString();
    if (n < 999950) return `${short(n / 1000)}K`;
    return `${short(n / 1000000)}M`;
  }

  const savedText = (s) => (s >= 1 ? V.ui.formatDuration(s) : "0m");

  // "12h 34m" no longer fits the panel at the full size; "123h 45m" needs less again
  const heroSize = (text) => (text.length >= 8 ? "s" : text.length >= 7 ? "m" : "");

  const WAVE_PEAK_PX = 46, WAVE_MIN_PX = 8, WAVE_SILENT_PX = 4;

  /* Words per day as a sound wave, with a silent day as a dot. The bars are kept
     and only their heights change, so a new dictation grows today's bar instead
     of replaying the rise that plays when Home opens. No daily series (an older
     or failed answer) hides the wave and leaves the numbers. */
  function renderWave(daily) {
    const block = V.ui.$("#stat-wave-block");
    const wave = V.ui.$("#stat-wave");
    if (!Array.isArray(daily) || !daily.length) {
      block.hidden = true;
      return;
    }
    block.hidden = false;
    if (wave.children.length !== daily.length) {
      wave.replaceChildren(...daily.map((_, i) => {
        const bar = V.ui.el("span", { class: "stat-bar" });
        bar.style.animationDelay = `${i * 35}ms`;
        return bar;
      }));
    }
    const words = daily.map((d) => d.words || 0);
    const peak = Math.max(1, ...words);
    words.forEach((n, i) => {
      const bar = wave.children[i];
      bar.style.height = `${n ? Math.max(WAVE_MIN_PX, Math.round((n / peak) * WAVE_PEAK_PX)) : WAVE_SILENT_PX}px`;
      const today = i === words.length - 1;
      bar.className = `stat-bar${n ? (today ? " is-today" : "") : " is-silent"}`;
    });
    const active = words.filter(Boolean).length;
    wave.setAttribute("aria-label", active
      ? `Words per day: dictated on ${active} of the last ${words.length} days.`
      : `No dictations in the last ${words.length} days.`);
  }

  /* ---------- update banner ----------

     Home's persistent indicator for a ready update -- Settings has its own
     Updates section with the version and Check now, but a banner here is what
     a user who never opens Settings actually sees.

     Session-scoped, and tracked by version rather than as a flat yes/no: a
     dismissed banner stays down for the rest of this session for the release
     it was dismissed for, but a *newer* one (Check-now finds 1.2.0 after the
     user dismissed 1.1.0) is exactly when they should be re-prompted, so it
     is not suppressed by the earlier dismissal. */
  let dismissedVersion = null;
  // Whatever version the banner is currently offering (or last offered), so
  // dismissUpdateBanner() -- which has no status object of its own -- knows
  // which version it is dismissing.
  let lastReadyVersion = null;

  /* Bound once both controls are actually found and wired -- unlike
     settings.js's wheelDrawn (which sets its flag first, before the work,
     since drawColorWheel/initColorWheel cannot themselves fail), binding here
     only claims success after both elements exist, so a render that finds
     one missing simply retries on the next one rather than being stuck
     unbound for the rest of the session. Not reachable today (index.html
     always renders both together), but cheap insurance against ever adding a
     render path where the banner exists before its own buttons do.

     Deferred to here rather than done at module-parse time (the convention
     this file otherwise uses for the focus listener above): several tests
     load this file standalone without ever registering #update-install or
     #update-dismiss, and a bare `document.getElementById(...).addEventListener`
     at parse time would throw on all of them before any test got a chance to
     set anything up. */
  let updateControlsBound = false;

  function bindUpdateControls() {
    if (updateControlsBound) return;
    const installBtn = document.getElementById("update-install");
    const dismissBtn = document.getElementById("update-dismiss");
    if (!installBtn || !dismissBtn) return;
    updateControlsBound = true;

    installBtn.addEventListener("click", async () => {
      // Same overlap guard as main.js's #hotkey-capture and settings.js's
      // Check-now: without it, a second click while the first install is
      // still in flight could fire a second install_update(). The Python
      // side refuses a second call once installing (compare-and-set, a
      // terminal state), so this is not catastrophic -- but skipping the
      // guard would still let the user see a confusing second toast while
      // the app is in the middle of shutting down.
      if (installBtn.disabled) return;
      installBtn.disabled = true;

      const textEl = document.getElementById("update-banner-text");
      const priorText = textEl ? textEl.textContent : "";
      // The banner's own text is the channel of record for this message, not
      // the toast below. V.ui.toast() can be silently swallowed: if an Undo
      // toast is already live -- and Home's own recent-list delete buttons
      // sit right below this banner, so that is one click away -- ui.js's
      // toastActionLive gate just queues this message for up to 5s instead
      // of showing it. A successful install kills the process well inside
      // that window, so the queued message would never be shown; the user
      // would watch Voisr vanish with a stale "Dictation deleted / Undo"
      // still on screen and no explanation. The banner has no such queue.
      if (textEl) textEl.textContent = "Installing update — Voisr will restart";
      // Emitted BEFORE the call, and that ordering must survive any future
      // tidy-up. A successful install_update() kills the app roughly 0.75s
      // later via Application.Exit(), and pywebview's evaluate_js blocks on
      // a semaphore that destroy_window never releases -- so if Exit() wins
      // that race, this await's promise never settles and anything written
      // after it would simply never run.
      V.ui.toast("Installing update — Voisr will restart");
      // Routed through V.ui.tryApi, unlike settings.js's Check-now:
      // install_update() only ever answers success ({ok, version}) or
      // failure ({error}), so there is nothing tryApi's null-on-error
      // collapses away that this handler still needs -- unlike Check-now,
      // which has to tell "ready" apart from "already checking" from the
      // very same response.
      if (!await V.ui.tryApi(V.api().install_update(), "Install update")) {
        installBtn.disabled = false;
        if (textEl) textEl.textContent = priorText;
        return;
      }
      // Success: the app is about to exit. Leave the button disabled and the
      // banner reading "Installing…" -- re-enabling the button, or reverting
      // the text, would only invite a pointless second click during shutdown.
    });

    dismissBtn.addEventListener("click", dismissUpdateBanner);
  }

  async function renderUpdateBanner() {
    const banner = document.getElementById("update-banner");
    if (!banner) return;
    bindUpdateControls();
    let status;
    try {
      status = await V.api().get_update_status();
    } catch {
      // A background concern must never take the dashboard down with it.
      banner.hidden = true;
      return;
    }
    // Render from `state`, never `version` alone: the updater never clears
    // its manifest, so `version` can still name a withdrawn release.
    if (!status || status.state !== "ready" || !status.version) {
      banner.hidden = true;
      return;
    }
    lastReadyVersion = status.version;
    if (status.version === dismissedVersion) {
      banner.hidden = true;
      return;
    }
    // Reveal before writing the text: #update-banner is aria-live="polite",
    // and a live region that is still hidden when its content changes is not
    // reliably announced once it is unhidden -- the mutation has to happen
    // while the region is already visible/attached.
    banner.hidden = false;
    const notes = status.notes ? ` — ${status.notes}` : "";
    const msg = `Version ${status.version} is ready${notes}`;
    const textEl = document.getElementById("update-banner-text");
    // render() calls this on every Home render -- view switches, each new
    // dictation, after recording, after a delete -- so with an undismissed
    // update sitting there, a naive unconditional write would mutate this
    // live region's text on every single one of those, even when the
    // message never actually changed. A screen reader would re-announce
    // "Version 1.1.0 is ready…" after every dictation. Only write when the
    // text is actually different.
    if (textEl.textContent !== msg) textEl.textContent = msg;
  }

  function dismissUpdateBanner() {
    dismissedVersion = lastReadyVersion;
    const banner = document.getElementById("update-banner");
    if (banner) banner.hidden = true;
  }

  /* Home is independent panels (greeting, banner, stats, status, feed, and on
     Windows the update banner), so a
     failure in one degrades that panel only and the rest still render:
     `stats.total_words` on a failed get_stats used to throw and take the whole
     pane down with it, leaving a blank screen and no message. Each section is
     guarded on its own null; the try/catch is the backstop for anything that
     still manages to throw. */
  let knownIds = null;  // every history id at the last render (see below)

  /* opts.entering: Home is just being opened (main.js show()), so its
     numbers appear as they are rather than counting. */
  async function render(opts = {}) {
    const $ = V.ui.$;
    try {
      $("#greeting").textContent = greeting();
      // All run concurrently, and all are awaited together so a throw in any of
      // them lands in the catch below.
      const [stats, history, cfg] = await Promise.all([
        V.ui.tryApi(V.api().get_stats(), "Load stats"),
        V.ui.tryApi(V.api().get_history(), "Load history"),
        V.ui.tryApi(V.api().get_config(), "Load settings"),
        renderPermissions(),
        renderPtt(),
        renderListener(),
        // renderUpdateBanner has its own try/catch (a background concern must
        // never take the dashboard down with it); joining it here is only the
        // backstop for anything that still manages to throw.
        renderUpdateBanner(),
      ]);

      if (cfg) {
        pasteOff = cfg.paste === false;
        apply();
      }

      if (stats) {
        const animate = !opts.entering;
        const saved = stats.time_saved_seconds || 0;
        $("#stat-saved").dataset.size = heroSize(savedText(saved));
        countTo($("#stat-saved"), saved, savedText, animate);
        const words = stats.total_words || 0;
        countTo($("#stat-words"), words, compactCount, animate);
        $("#stat-words").title = `${words.toLocaleString()} words`;
        $("#stat-streak").textContent = (stats.streak_days || 0).toLocaleString();
        renderWave(stats.daily);
      }

      if (history) renderFeed(history);
    } catch (e) {
      V.ui.toast("Home failed to load");
    }
  }

  /* Keyed by day and id, so a refresh moves only what changed (js/lists.js). The
     whole history decides each row's story: an id still in it just fell out of
     the top five (fades under the card edge) or slid into it (fills from below);
     an id that was never seen is new or restored (lands with the wash); one that
     is gone was deleted. */
  function renderFeed(history) {
    const $ = V.ui.$;
    const recent = history.slice(0, 5);
    const days = new Map();
    for (const item of recent) {
      const day = V.ui.dayOf(item.ts);
      if (!days.has(day)) days.set(day, []);
      days.get(day).push(item);
    }
    const all = new Set(history.map((item) => item.id));
    const known = knownIds;
    knownIds = all;
    V.lists.syncGroups($("#recent-groups"), [...days].map(([day, items]) => ({
      key: day,
      build: () => V.ui.dayGroup(day),
      rows: items.map((item) => ({
        key: item.id, sig: item.id,
        build: () => V.ui.itemRow(item, { onCopy, onDelete }),
      })),
    })), {
      animate: true,
      stillExists: (id) => all.has(id),
      wasKnown: (id) => !!known && known.has(id),
    // Not awaited, so the stats never wait on a row's exit. An animated
    // update can fail after render's try/catch has returned: catch it here.
    }).catch(() => V.ui.toast("Recent dictations failed to update"));
    $("#recent-empty").hidden = recent.length > 0;
    $("#home-view-all").hidden = recent.length === 0;
  }

  /* ---------- record flow ----------

     Capture is the fallback path, not the headline: the key works anywhere on
     the PC, and this exists for when it can't be used (screen sharing, a
     captured modifier, testing the mic). The banner carries the whole state
     machine: elapsed time while listening, then transcribing, then a done
     state, so nothing else on Home has to move. It never pastes; the text goes
     to the clipboard, and the banner says so. */

  // "starting" covers the await on start_recording: a second click in that
  // window used to start a second recording and strand "Couldn't start."
  let recState = "idle"; // idle | starting | recording | processing
  let shownState = "";   // what the banner shows, which outlasts recState by "done"
  let tickTimer = null;
  let doneTimer = null;
  let elapsed = 0;
  let meter = null;
  let recordError = "";  // the last recording's outcome, until the next one starts

  const LISTENING_SPEED = 3.2;

  const recordingShown = () => shownState === "recording" || shownState === "processing"
    || shownState === "done";

  /* The wave follows the live mic (js/wave-meter.js), read only while this
     records. Absent in tests that don't load it. */
  function waveMeter() {
    if (!meter && V.waveMeter) {
      const el = V.ui.$("#home-wave");
      if (el) meter = V.waveMeter.create({ el, readLevel: () => V.api().get_level() });
    }
    return meter;
  }

  function waveSpeed(speed) {
    const el = V.ui.$("#home-wave");
    if (V.wave && el) V.wave.setSpeed(el, speed);
  }

  function stopTick() {
    clearInterval(tickTimer);
    tickTimer = null;
  }

  function showElapsed() {
    V.ui.$("#record-time").textContent =
      `${Math.floor(elapsed / 60)}:${String(elapsed % 60).padStart(2, "0")}`;
  }

  const LIVE_COPY = {
    recording: ["Listening", "Click to finish. It's copied to your clipboard, not pasted."],
    processing: ["Transcribing", "On this PC."],
    done: ["Copied to clipboard", "Paste it wherever you need it."],
  };

  /* Both timers are tracked so they can always be cleared: a stale tick or a
     stale reset must never fire against a banner that has since moved on. */
  function setRecordState(state) {
    const $ = V.ui.$;
    shownState = state;
    const hero = $("#home-hero");
    if (state) hero.dataset.state = state;
    else delete hero.dataset.state;
    const copy = LIVE_COPY[state];
    $("#record-title").textContent = copy ? copy[0] : "";
    $("#record-status").textContent = copy ? copy[1] : "";
    $("#record-label").textContent = state === "recording" ? "Finish" : "Click to record";
    const btn = $("#record-btn");
    if (state === "processing") btn.setAttribute("aria-disabled", "true");
    else btn.removeAttribute("aria-disabled");
    // The mic stays open on every view and push-to-talk is dropped meanwhile,
    // so the Home nav item carries it.
    const nav = $("#nav-home");
    if (state === "recording" || state === "processing") nav.dataset.recording = "true";
    else delete nav.dataset.recording;
    syncBanners();
  }

  /* Whichever banner is up has to carry it: the dark one is hidden under a blue
     one, and the blue one is rebuilt whenever what it says changes, so its copy
     is written from recordError each time (needsBanner). */
  function showRecordError(text) {
    recordError = text;
    V.ui.$("#record-error").textContent = text;
    const inBlue = V.ui.$("#perm-strip").querySelector(".banner-error");
    if (inBlue) inBlue.textContent = text;
  }

  async function toggleRecord() {
    if (recState === "processing" || recState === "starting") return;

    if (recState === "idle") {
      showRecordError("");
      clearTimeout(doneTimer);
      recState = "starting";
      const res = await V.ui.tryApi(V.api().start_recording(), "Start recording");
      // tryApi owns the detail (and the throw path, which used to be able to
      // strand recState). The banner keeps the persistent marker, since a 3.5s
      // toast is easy to miss.
      if (!res) { recState = "idle"; setRecordState(""); showRecordError("Couldn't start."); return; }
      recState = "recording";
      elapsed = 0;
      showElapsed();
      setRecordState("recording");
      waveSpeed(LISTENING_SPEED);
      if (waveMeter()) waveMeter().start();
      stopTick();
      tickTimer = setInterval(() => { elapsed += 1; showElapsed(); }, 1000);
      renderListener().catch(() => {});
      return;
    }

    // recording -> stop
    recState = "processing";
    stopTick();
    setRecordState("processing");
    waveSpeed(1);
    if (waveMeter()) waveMeter().settle();
    // Reset the state before anything can return: a bridge throw here used to
    // leave recState stuck on "processing", which killed the button for the
    // rest of the session.
    const res = await V.ui.tryApi(V.api().stop_recording(), "Transcribe");
    recState = "idle";
    renderListener().catch(() => {});

    if (waveMeter()) waveMeter().idle();
    if (!res) {
      setRecordState("");
      showRecordError("Transcription failed.");
      return;
    }

    // Rare, and "Copied" would send them off to paste whatever was there before.
    if (res.copied === false) {
      setRecordState("");
      showRecordError("Couldn't copy it. Copy it from the list below.");
      render();
      return;
    }

    // The transcript itself lands at the top of the feed a line below, so the
    // banner reports the outcome rather than repeating the text.
    setRecordState("done");
    // unless History couldn't save it: then "Copied" alone would be the only
    // trace, and a wrong one
    if (res.warning) showRecordError("Copied, but not saved to History.");
    doneTimer = setTimeout(() => {
      if (recState !== "idle") return;
      setRecordState("");
      elapsed = 0;
      showElapsed();
    }, 2600);
    render();
  }

  /* The palette's "Start dictation" runs from any view; switching to Home
     first puts the banner, the only thing showing the mic is open, on screen. */
  async function dictateFromAnywhere() {
    V.show("home");
    await toggleRecord();
  }

  function init() {
    V.ui.$("#change-key").addEventListener("click", () => V.show("settings", { section: "key" }));
  }

  /* First run asks for the same grant, and must read it the same way. */
  V.permissions = { micPending, openPane };

  V.views = V.views || {};
  V.views.home = { render, init, toggleRecord, dictateFromAnywhere, renderPermissions,
                   renderListener, compactCount, renderUpdateBanner, dismissUpdateBanner };
})(window.Voisr);
