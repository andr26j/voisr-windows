"use strict";
window.Voisr = window.Voisr || {};
(function (V) {
  V.api = () => window.pywebview.api;

  /* Dev fallback: when opened in a plain browser (no pywebview bridge),
     run against an in-memory mock so the UI can be previewed and tested. */
  setTimeout(() => {
    if (window.pywebview) return;
    const now = Date.now() / 1000;
    let mockHistory = [
      { id: "a1", ts: now - 300, text: "Hello team, please send the invoice to TradeMate by Friday.", words: 10, duration_s: 5 },
      { id: "a2", ts: now - 7200, text: "Draft a follow-up about the Brunswick site visit next week.", words: 10, duration_s: 6 },
      { id: "a3", ts: now - 90000, text: "Remember to book the trailer for Saturday morning.", words: 8, duration_s: 4 },
    ];
    let mockCfg = { hotkey: "ctrl_r", paste: true, show_widget: true, pill_theme: "classic", pill_color: null, dictionary: { trademake: "TradeMate", "trade mate": "TradeMate" }, model: "small.en", launch_at_login: false };
    let recording = false;
    // mirrors voisr/keys.py's hotkey_options(), used both for the dropdown
    // mock and to resolve a human label for hotkey_display() below.
    const HOTKEY_OPTIONS = [
      { value: "ctrl_r", label: "Right Ctrl" }, { value: "ctrl_l", label: "Left Ctrl" },
      { value: "alt_r", label: "Right Alt" }, { value: "alt_l", label: "Left Alt" },
      { value: "shift_r", label: "Right Shift" }, { value: "shift_l", label: "Left Shift" },
      { value: "win_r", label: "Right Win" }, { value: "caps", label: "Caps Lock" },
      { value: "f13", label: "F13" }, { value: "f14", label: "F14" }, { value: "f15", label: "F15" },
      { value: "scroll", label: "Scroll Lock" }, { value: "pause", label: "Pause" },
    ];
    window.pywebview = { api: {
      get_permissions: async () => ({ panes: [
        { key: "microphone", label: "Microphone", granted: true,
          settings_url: "ms-settings:privacy-microphone" },
      ]}),
      open_settings_pane: async () => ({ ok: true }),
      get_history: async () => [...mockHistory].sort((a, b) => b.ts - a.ts),
      get_stats: async () => ({
        total_words: 12480, dictations: mockHistory.length, avg_wpm: 132,
        streak_days: 6, total_seconds: 5400, time_saved_seconds: 13320,
        daily: Array.from({ length: 14 }, (_, i) => ({
          date: `2026-07-${String(12 + i).padStart(2, "0")}`,
          words: [0, 120, 340, 90, 0, 610, 480, 220, 0, 130, 700, 540, 310, 260][i],
        })),
      }),
      get_config: async () => JSON.parse(JSON.stringify(mockCfg)),
      set_config: async (u) => Object.assign(mockCfg, u),
      // mirrors Api.set_appearance() in voisr/ui.py, used by the command
      // palette's "Toggle appearance" command.
      set_appearance: async (mode) => {
        if (!["system", "light", "dark"].includes(mode)) return { error: `unknown appearance: ${mode}` };
        mockCfg.appearance = mode;
        return { ok: true, appearance: mode };
      },
      delete_history_item: async (id) => { mockHistory = mockHistory.filter((i) => i.id !== id); return { ok: true }; },
      restore_history_item: async (item) => { mockHistory.push(item); return { ok: true }; },
      // mirrors Api.clear_history() in voisr/ui.py: a single atomic truncate,
      // never a per-row delete_history_item loop.
      clear_history: async () => {
        const removed = mockHistory.length;
        mockHistory = [];
        return { ok: true, removed };
      },
      // mirrors Api.get_models() in voisr/ui.py; one model is left
      // uninstalled here so the mock exercises the disabled-option path.
      get_models: async () => ({
        models: [
          { value: "small.en", label: "Small — best accuracy", installed: true, bytes: 488 * 1024 * 1024 },
          { value: "base.en", label: "Base — fastest", installed: false, bytes: 0 },
        ],
        active: mockCfg.model || "small.en",
      }),
      // mirrors Api.set_launch_at_login() in voisr/ui.py.
      set_launch_at_login: async (enabled) => {
        mockCfg.launch_at_login = !!enabled;
        return { ok: true, enabled: !!enabled };
      },
      export_history: async (fmt) => {
        const stamp = new Date().toISOString().slice(0, 10);
        let text;
        if (fmt === "json") text = JSON.stringify(mockHistory, null, 2);
        else if (fmt === "md") text = mockHistory.map((i) => `- ${i.text}`).join("\n");
        else if (fmt === "txt") text = mockHistory.map((i) => i.text).join("\n");
        else return { error: `unsupported format: ${fmt}` };
        return { text, suggested_name: `voisr-history-${stamp}.${fmt}` };
      },
      save_text_file: async (text, suggestedName) => ({ ok: true, path: `/tmp/${suggestedName}` }),
      copy_text: async () => ({ ok: true }),
      capture_hotkey: async () => {
        await new Promise((r) => setTimeout(r, 1200));
        mockCfg.hotkey_vk = 0x5D; mockCfg.hotkey_label = "Menu";
        return { vk: 0x5D, label: "Menu" };
      },
      get_hotkey_options: async () => HOTKEY_OPTIONS,
      // mirrors Api.hotkey_display() in voisr/ui.py: captured label, else
      // the matching HOTKEY_OPTIONS label, else the raw value, else "unset".
      hotkey_display: async () => {
        if (mockCfg.hotkey_label) return { display: mockCfg.hotkey_label };
        const opt = HOTKEY_OPTIONS.find((o) => o.value === mockCfg.hotkey);
        if (opt) return { display: opt.label };
        return { display: mockCfg.hotkey || "unset" };
      },
      get_hotkey_status: async () => ({ status: `active on ${mockCfg.hotkey}` }),
      get_pill_themes: async () => [
        { value: "classic", label: "Classic" }, { value: "ember", label: "Ember" },
        { value: "aurora", label: "Aurora" }, { value: "ocean", label: "Ocean" },
        { value: "neon", label: "Neon EQ" }, { value: "breathe", label: "Breathe" },
        { value: "scanner", label: "Scanner" },
      ],
      start_recording: async () => { recording = true; return { recording: true }; },
      stop_recording: async () => {
        if (!recording) return { error: "not recording" };
        recording = false;
        await new Promise((r) => setTimeout(r, 900));
        const item = { id: Math.random().toString(36).slice(2), ts: Date.now() / 1000, text: "This is a mock dictation rendered in browser preview.", words: 9, duration_s: 4 };
        mockHistory.push(item);
        return { text: item.text, item, copied: true };
      },
    }};
    // main.js registers its init() on this event; dispatching it here
    // (instead of calling init() directly) keeps this file decoupled
    // from the view/main modules that load after it.
    window.dispatchEvent(new Event("pywebviewready"));
  }, 300);
})(window.Voisr);
