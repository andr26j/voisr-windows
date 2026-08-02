"use strict";
window.Voisr = window.Voisr || {};
(function (V) {
  const PERM_HELP = {
    microphone: "Voisr needs the microphone to hear you.",
  };

  async function renderPermissions() {
    const strip = V.ui.$("#perm-strip");
    const res = await V.ui.tryApi(V.api().get_permissions(), "Load permissions");
    // Leave whatever the strip last showed rather than wiping it: a stale
    // strip beside a toast explaining the staleness beats an empty one.
    if (!res || !res.panes) return;
    const panes = res.panes;
    const bad = panes.filter((p) => p.granted !== true);

    if (bad.length === 0) {
      // a healthy app should not shout — one quiet line, not three badges
      strip.replaceChildren(V.ui.el("p", {
        class: "perm-ok",
        text: "Microphone access granted.",
      }));
      strip.dataset.state = "ok";
      return;
    }

    strip.dataset.state = "attention";
    strip.replaceChildren(...panes.map((p) => {
      const state = p.granted === true ? "ok"
                  : p.granted === false ? "denied" : "unknown";
      const chip = V.ui.el("button", {
        class: `perm-chip perm-${state}`,
        type: "button",
        title: PERM_HELP[p.key] || "",
        data: { key: p.key },
        // open_settings_pane answers {"ok": bool} — a false is Windows
        // refusing to open the pane, which used to vanish silently and
        // leave the user clicking a chip that appeared to do nothing.
        on: { click: async () => {
          const res = await V.ui.tryApi(
            V.api().open_settings_pane(p.key), "Open Settings");
          if (res && res.ok === false) V.ui.toast("Couldn't open Settings");
        } },
      }, V.ui.el("span", { class: "perm-dot", "aria-hidden": "true" }),
         V.ui.el("span", { text: p.label }));
      if (state === "ok") chip.disabled = true;
      return chip;
    }));
  }

  // granting a permission happens in Settings, outside the app;
  // refresh when the user comes back so the strip never shows a stale state
  window.addEventListener("focus", () => {
    if (document.getElementById("view-home").hidden) return;
    // Background refresh, so it is deliberately not awaited — but it must
    // still be caught. Bridge failures already toast inside
    // renderPermissions; this catch only covers a genuine throw, which
    // would otherwise surface as an unhandled rejection nobody sees.
    renderPermissions().catch(() => V.ui.toast("Permissions failed to refresh"));
  });

  async function onCopy(item) {
    if (!await V.ui.tryApi(V.api().copy_text(item.text), "Copy")) return;
    V.ui.toast("Copied to clipboard");
  }

  /* Mirrors views/history.js's onDelete exactly — the same undo contract has
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

  async function renderHero() {
    const res = await V.ui.tryApi(V.api().hotkey_display(), "Load shortcut");
    if (!res) return;
    V.ui.$("#hero-keycap").textContent = res.display || "unset";
  }

  /* 14 daily word counts -> a polyline across a 240x40 viewBox. A flat run
     (including all-zero) draws along the baseline rather than dividing by 0. */
  function sparklinePath(daily) {
    if (!daily || daily.length < 2) return "";
    const max = Math.max(...daily.map((d) => d.words), 0);
    const stepX = 240 / (daily.length - 1);
    return daily.map((d, i) => {
      const x = (i * stepX).toFixed(1);
      const y = (max === 0 ? 38 : 38 - (d.words / max) * 34).toFixed(1);
      return `${i === 0 ? "M" : "L"}${x} ${y}`;
    }).join(" ");
  }

  /* ---------- update banner ----------

     Home's persistent indicator for a ready update -- Settings has its own
     app-version row and Check-now button (Task 10), but a banner here is what
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
    document.getElementById("update-banner-text").textContent =
      `Version ${status.version} is ready${notes}`;
  }

  function dismissUpdateBanner() {
    dismissedVersion = lastReadyVersion;
    const banner = document.getElementById("update-banner");
    if (banner) banner.hidden = true;
  }

  /* Home is five independent panels (greeting, permissions, hero shortcut,
     stats + recent list, update banner), so a failure in one degrades that
     panel only and the rest still render — `stats.total_words` on a failed
     get_stats used to throw and take the whole pane down with it, leaving a
     blank screen and no message. Each section is guarded on its own null;
     the try/catch is the backstop for anything that still manages to throw. */
  async function render() {
    const $ = V.ui.$;
    try {
      $("#greeting").textContent = greeting();
      // All five run concurrently, and all five are awaited together so a
      // throw in any of them lands in the catch below. renderPermissions and
      // renderHero used to be fired bare, which both hid their failures and
      // left a rejection nobody was listening for; joining them to this
      // Promise.all attaches the handler in the same tick they start.
      // renderUpdateBanner already has its own try/catch (a background
      // concern must never take the dashboard down with it) -- joining it
      // here is only the backstop for anything that still manages to throw.
      const [stats, history] = await Promise.all([
        V.ui.tryApi(V.api().get_stats(), "Load stats"),
        V.ui.tryApi(V.api().get_history(), "Load history"),
        renderPermissions(),
        renderHero(),
        renderUpdateBanner(),
      ]);

      if (stats) {
        $("#stat-saved").textContent = V.ui.formatDuration(stats.time_saved_seconds);
        $("#stat-words").textContent = (stats.total_words || 0).toLocaleString();
        $("#stat-streak").textContent = (stats.streak_days || 0).toLocaleString();
        $("#spark-path").setAttribute("d", sparklinePath(stats.daily));
      }

      if (history) {
        const recent = history.slice(0, 5);
        $("#recent-list").replaceChildren(
          ...recent.map((item) => V.ui.itemRow(item, { onCopy, onDelete })));
        $("#recent-empty").hidden = recent.length > 0;
      }
    } catch (e) {
      V.ui.toast("Home failed to load");
    }
  }

  /* ---------- record flow ---------- */

  let recState = "idle"; // idle | recording | processing

  async function toggleRecord() {
    const $ = V.ui.$;
    const btn = $("#record-btn");
    const status = $("#record-status");
    const errEl = $("#record-error");
    const wave = $("#hero-wave");

    if (recState === "processing") return;

    if (recState === "idle") {
      errEl.textContent = "";
      $("#record-result").hidden = true;
      const res = await V.ui.tryApi(V.api().start_recording(), "Start recording");
      // tryApi owns the detail (and the throw path, which used to be able to
      // strand recState). #record-error keeps its job as the persistent
      // marker beside the button, since a 3.5s toast is easy to miss.
      if (!res) { errEl.textContent = "Couldn't start recording."; return; }
      recState = "recording";
      wave.dataset.state = "recording";
      btn.setAttribute("aria-label", "Stop recording");
      status.textContent = "Listening… click again to finish";
      return;
    }

    // recording -> stop
    recState = "processing";
    wave.dataset.state = "processing";
    status.textContent = "Transcribing on this PC…";
    // Reset the button state before anything can return: a bridge throw here
    // used to leave recState stuck on "processing", which killed the record
    // button for the rest of the session.
    const res = await V.ui.tryApi(V.api().stop_recording(), "Transcribe");
    delete wave.dataset.state;
    btn.setAttribute("aria-label", "Start recording");
    recState = "idle";
    status.textContent = "Ready";

    if (!res) { errEl.textContent = "Transcription failed."; return; }
    $("#result-text").textContent = res.text;
    $("#record-result").hidden = false;
    render();
  }

  V.views = V.views || {};
  V.views.home = {
    render, toggleRecord, renderPermissions, sparklinePath,
    renderUpdateBanner, dismissUpdateBanner,
  };
})(window.Voisr);
