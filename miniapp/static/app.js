(() => {
  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  if (tg) {
    tg.ready();
    tg.expand();
  }
  if (window.ace && window.ace.config) {
    window.ace.config.set("basePath", "./vendor/ace");
    window.ace.config.set("modePath", "./vendor/ace");
    window.ace.config.set("themePath", "./vendor/ace");
    window.ace.config.set("workerPath", "./vendor/ace");
  }

  function syncColorScheme() {
    const scheme = tg && tg.colorScheme === "dark" ? "dark" : "light";
    const rootEl = document.documentElement;
    if (rootEl && rootEl.dataset && rootEl.style) {
      rootEl.dataset.colorScheme = scheme;
      rootEl.style.colorScheme = scheme;
    }
    if (window.__aceEditor) {
      window.__aceEditor.setTheme(scheme === "dark" ? "ace/theme/tomorrow_night" : "ace/theme/textmate");
    }
  }
  if (tg) {
    syncColorScheme();
    if (typeof tg.onEvent === "function") {
      tg.onEvent("themeChanged", syncColorScheme);
    }
  }
  const state = {
    me: null,
    schema: null,
    savedConfig: null,
    draft: null,
    revision: null,
    currentDir: ".",
    filesSessionUid: "",
    filesSessionsSignature: "",
    selectedPath: "",
    openFile: null,
    openFileRevision: null,
    editorReady: false,
    activeConfigSection: "telegram",
    activeDefaultsSubTab: "general",
    logsMeta: null,
    logsSocket: null,
    logsEntries: [],
    logsEntryIds: new Set(),
    logsType: "main",
    logsLevel: "",
    logsSessionKey: "",
    logsSessionUid: "",
    logsSessionId: "",
    logsReconnectTimer: null,
    logsReconnectAttempts: 0,
    logsShouldReconnect: false,
    statusSocket: null,
    statusReconnectTimer: null,
    statusReconnectAttempts: 0,
    statusShouldReconnect: false,
    statusLastPayload: null,
    statusSessionUid: "",
    statusSessionsSignature: "",
    runsPollTimer: null,
    runsSessionUid: "",
    runsRequestInFlight: false,
    runsSignature: "",
    runsSelectedRunId: "",
    runsSelectedModeId: "",
    runsLastActionMessage: "",
    runsCurrentDetail: null,
    adminPollTimer: null,
    adminSessionUid: "",
    adminSessionsSignature: "",
    adminLastPayload: null,
    adminRequestInFlight: false,
    adminStatusCache: {},
    adminChatCounters: { messages_count: -1, pending_count: -1, last_message_ts: "" },
    adminChatMessages: [],
    adminChatPending: [],
    adminChatSelectedApproval: "",
    adminChatMemoryDirty: false,
    adminChatSendInFlight: false,
    adminChatApproveInFlight: false,
    schedulerProjectSlug: "",
    schedulerSessionUid: "",
    schedulerProjectsSignature: "",
    schedulerSessionsSignature: "",
    schedulerSelectedJobId: "",
    schedulerJobs: [],
    schedulerNotificationTargets: [],
    schedulerRequestInFlight: false,
    settingsSessionUid: "",
    settingsSessionsSignature: "",
    settingsData: null,
    settingsLoading: false,
    sshHosts: {},
    sshHostsLoading: false,
    maxTickTsSeen: 0,
    ticksCount: 0,
    tickHistoryItems: [],
    tickHistoryKeys: new Set(),
    lastRenderedSessionId: null,
    redaction: null,
    reportsSessionUid: "",
    reportsSessionsSignature: "",
    reportsSelectedId: null,
    reportsSelectedSessionUid: null
  };

  const i18n = { catalog: {}, lang: "ru" };

  function t(key, fallback) {
    const parts = key.split(".");
    let node = i18n.catalog;
    for (const p of parts) {
      if (node == null || typeof node !== "object") return fallback !== undefined ? fallback : key;
      node = node[p];
    }
    if (node == null || typeof node === "object") return fallback !== undefined ? fallback : key;
    return String(node);
  }

  async function loadI18n(lang) {
    try {
      const data = await api(`/i18n/${encodeURIComponent(lang)}`);
      i18n.catalog = data || {};
      i18n.lang = lang;
    } catch {
      // fallback: каталог остаётся текущим
    }
  }

  function applyI18nToDOM() {
    document.querySelectorAll("[data-i18n-key]").forEach(el => {
      const key = el.getAttribute("data-i18n-key");
      const translated = t(key);
      if (translated !== key) el.textContent = translated;
    });
    // Parameterized keys: substitute {n} with the element's `value` (e.g. log history options).
    document.querySelectorAll("[data-i18n-key-n]").forEach(el => {
      const key = el.getAttribute("data-i18n-key-n");
      const n = el.getAttribute("value") || "";
      const translated = t(key);
      if (translated !== key) el.textContent = translated.replace("{n}", n);
    });
  }

  async function initLanguage() {
    const tgLang = tg && tg.initDataUnsafe && tg.initDataUnsafe.user
      ? (tg.initDataUnsafe.user.language_code || "ru")
      : "ru";
    await loadI18n(tgLang);
    applyI18nToDOM();
  }

  async function syncServerLanguage() {
    try {
      const res = await api("/i18n/user-lang");
      if (res.lang && res.lang !== i18n.lang) {
        await loadI18n(res.lang);
        applyI18nToDOM();
      }
      const sel = document.getElementById("langSelect");
      if (sel) sel.value = i18n.lang;
    } catch { /* ignore */ }
  }

  const SECRET_UNCHANGED_SENTINEL = "__CLI_PROXY_SECRET_UNCHANGED__";
  const SECRET_INPUT_PATHS = Object.freeze({
    "tg-token": "telegram.token",
    "def-openai-api-key": "defaults.openai_api_key",
    "def-zai-key": "defaults.zai_api_key",
    "def-tavily-key": "defaults.tavily_api_key",
    "def-jina-key": "defaults.jina_api_key",
    "def-github-token": "defaults.github_token",
    "def-gemini-oauth-client-secret": "defaults.gemini_oauth_client_secret",
    "mcp-token": "mcp.token",
    "webhooks-secret-token": "webhooks.secret_token",
  });
  const ADMIN_POLL_INTERVAL_MS = 5000;
  const ADMIN_STATUS_CACHE_TTL_MS = 750;
  const ADMIN_STATUS_TIMEOUT_MS = 4000;
  let pressedButton = null;
  let pressedButtonReleaseTimer = null;

  const editor = ace.edit("ace");
  editor.setTheme("ace/theme/textmate");
  editor.session.setMode("ace/mode/yaml");
  editor.setShowPrintMargin(false);
  window.__aceEditor = editor;
  syncColorScheme();

  function initData() {
    return (tg && tg.initData) || "";
  }

  function clearPressedButton(button = null) {
    const current = button || pressedButton;
    if (!current) return;
    current.classList.remove("is-pressed");
    if (pressedButton === current || !button) {
      pressedButton = null;
    }
  }

  function schedulePressedButtonRelease(button, delayMs = 140) {
    if (!button) return;
    if (pressedButtonReleaseTimer) {
      clearTimeout(pressedButtonReleaseTimer);
      pressedButtonReleaseTimer = null;
    }
    const delay = Math.max(0, Number(delayMs) || 0);
    pressedButtonReleaseTimer = window.setTimeout(() => {
      clearPressedButton(button);
      pressedButtonReleaseTimer = null;
    }, delay);
  }

  function installButtonPressFeedback() {
    if (!document || typeof document.addEventListener !== "function") {
      return;
    }

    const resolveButton = (target) => {
      if (!(target instanceof Element) || typeof target.closest !== "function") return null;
      const button = target.closest("button");
      if (!button || button.disabled) return null;
      return button;
    };

    const markPressed = (target) => {
      const button = resolveButton(target);
      if (!button) return;
      if (pressedButtonReleaseTimer) {
        clearTimeout(pressedButtonReleaseTimer);
        pressedButtonReleaseTimer = null;
      }
      if (pressedButton && pressedButton !== button) {
        clearPressedButton(pressedButton);
      }
      button.classList.add("is-pressed");
      pressedButton = button;
    };

    const releasePressed = (target, delayMs = 140) => {
      const button = resolveButton(target) || pressedButton;
      if (!button) return;
      schedulePressedButtonRelease(button, delayMs);
    };

    document.addEventListener("pointerdown", (event) => {
      markPressed(event.target);
    }, true);
    document.addEventListener("pointerup", (event) => {
      releasePressed(event.target, 160);
    }, true);
    document.addEventListener("pointercancel", () => {
      releasePressed(null, 0);
    }, true);
    document.addEventListener("click", (event) => {
      releasePressed(event.target, 160);
    }, true);
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      markPressed(event.target);
    }, true);
    document.addEventListener("keyup", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      releasePressed(event.target, 160);
    }, true);
  }

  async function api(path, options = {}) {
    const headers = {
      "Content-Type": "application/json",
      "X-Telegram-Init-Data": initData(),
      ...(options.headers || {}),
    };
    const res = await fetch(`./api${path}`, { ...options, headers });
    const rawText = await res.text();
    let body = {};
    try {
      body = rawText ? JSON.parse(rawText) : {};
    } catch {
      body = {};
    }
    if (!res.ok) {
      const message = body && body.error ? body.error : (res.status === 401 || res.status === 403 ? t("miniapp.error.auth", "Ошибка авторизации") : `HTTP ${res.status}`);
      const err = new Error(message);
      err.status = res.status;
      err.body = body;
      throw err;
    }
    return body;
  }

  // window.alert/confirm заблокированы в Telegram WebView на iOS/Android —
  // используем нативные попапы Bot API (>= 6.2) с фолбэком на браузерные.
  function uiAlert(message) {
    const text = String(message ?? "");
    return new Promise((resolve) => {
      if (tg && typeof tg.showAlert === "function") {
        try {
          tg.showAlert(text.slice(0, 256), () => resolve());
          return;
        } catch {
          // Bot API < 6.2 — фолбэк ниже
        }
      }
      window.alert(text);
      resolve();
    });
  }

  function uiConfirm(message) {
    const text = String(message ?? "");
    return new Promise((resolve) => {
      if (tg && typeof tg.showConfirm === "function") {
        try {
          tg.showConfirm(text.slice(0, 256), (ok) => resolve(Boolean(ok)));
          return;
        } catch {
          // Bot API < 6.2 — фолбэк ниже
        }
      }
      resolve(window.confirm(text));
    });
  }

  function blockUnauthorizedScreen() {
    document.body.innerHTML = "";
    const wrapper = document.createElement("div");
    wrapper.style.minHeight = "100vh";
    wrapper.style.display = "flex";
    wrapper.style.alignItems = "center";
    wrapper.style.justifyContent = "center";
    wrapper.style.fontFamily = '"IBM Plex Sans", "Segoe UI", system-ui, sans-serif';
    wrapper.style.background = "var(--tg-theme-bg-color, #f3f4f6)";
    wrapper.style.color = "var(--tg-theme-text-color, #111827)";
    wrapper.textContent = t("miniapp.error.access_denied", "Доступ запрещен");
    document.body.appendChild(wrapper);
  }

  function setAuthStatus(text, ok = true) {
    const el = document.getElementById("authStatus");
    if (!el) return;
    el.textContent = text;
    el.className = ok ? "status-ok" : "status-error";
  }

  function setStatus(text) {
    const el = document.getElementById("statusEmpty");
    const dashboard = document.getElementById("statusDashboard");
    if (!el || !dashboard) return;
    if (text) {
      el.textContent = String(text);
      el.classList.remove("hidden");
      dashboard.classList.add("hidden");
    } else {
      el.classList.add("hidden");
      dashboard.classList.remove("hidden");
    }
  }

  function setStatusRaw(text) {
    const el = document.getElementById("statusJson");
    if (!el) return;
    el.textContent = String(text || "");
  }

  function setStatusAccordion(detailsId, bodyId, text) {
    const details = document.getElementById(detailsId);
    const body = document.getElementById(bodyId);
    if (!details || !body) return;
    const value = String(text || "").trim();
    const hasValue = value.length > 0;
    if (details.tagName.toLowerCase() === "details") {
      details.classList.toggle("hidden", !hasValue);
    } else {
      details.classList.toggle("hidden", !hasValue);
    }
    body.textContent = hasValue ? value : "";
  }

  function setStatusAccordionHtml(detailsId, bodyId, htmlContent) {
    const details = document.getElementById(detailsId);
    const body = document.getElementById(bodyId);
    if (!details || !body) return;
    const value = String(htmlContent || "").trim();
    const hasValue = value.length > 0;
    if (details.tagName.toLowerCase() === "details") {
      details.classList.toggle("hidden", !hasValue);
    } else {
      details.classList.toggle("hidden", !hasValue);
    }
    body.innerHTML = hasValue ? value : "";
  }

  function setLogsStatus(text, ok = true) {
    const el = document.getElementById("logsStatus");
    if (!el) return;
    el.textContent = text;
    el.className = ok ? "status-ok" : "status-error";
  }

  function setLogsControlsEnabled(enabled) {
    ["logsType", "logsLevel", "logsHistory", "logsSession", "logsApply", "logsClear", "logsDownload", "logsAutoScroll"].forEach((id) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.disabled = !enabled;
    });
  }

  function hideAdminTabsForUser() {
    ["config", "files", "editor", "admin"].forEach((tab) => {
      const btn = document.querySelector(`.tabs button[data-tab="${tab}"]`);
      const pane = document.getElementById(`tab-${tab}`);
      if (btn) btn.style.display = "none";
      if (pane) pane.style.display = "none";
    });
  }

  function applyLogsStateFromControls() {
    const type = document.getElementById("logsType");
    const level = document.getElementById("logsLevel");
    const history = document.getElementById("logsHistory");
    const session = document.getElementById("logsSession");
    state.logsType = String(type?.value || "main");
    state.logsLevel = String(level?.value || "");
    state.logsHistory = Number(history?.value || 0);
    const selected = parseLogsSessionSelection(session ? session.value : state.logsSessionKey);
    state.logsSessionKey = selected.key;
    state.logsSessionUid = selected.sessionUid;
    state.logsSessionId = selected.sessionId;
  }

  function buildLogsSessionSelectionValue(sessionUid, sessionId) {
    const payload = {
      session_uid: String(sessionUid || ""),
      session_id: String(sessionId || ""),
    };
    return JSON.stringify(payload);
  }

  function parseLogsSessionSelection(rawValue) {
    const value = String(rawValue || "").trim();
    if (!value) {
      return { key: "", sessionUid: "", sessionId: "" };
    }
    try {
      const parsed = JSON.parse(value);
      if (parsed && typeof parsed === "object") {
        return {
          key: value,
          sessionUid: String(parsed.session_uid || ""),
          sessionId: String(parsed.session_id || ""),
        };
      }
    } catch {}
    return {
      key: value,
      sessionUid: value,
      sessionId: "",
    };
  }

  function applyStatusStateFromControls() {
    const session = document.getElementById("statusSession");
    state.statusSessionUid = String(session?.value || "");
  }

  function applyFilesStateFromControls() {
    const session = document.getElementById("filesSession");
    state.filesSessionUid = String(session?.value || "");
  }

  function currentFilesSessionUid() {
    return String(state.filesSessionUid || "");
  }

  function ensureAdminPanelLayout() {
    const root = document.getElementById("tab-admin");
    if (!root || root.dataset.ready === "1") return;
    root.innerHTML = `
      <div class="toolbar status-toolbar">
        <label for="adminSession">${escapeHtml(t("miniapp.label.session", "Сессия"))}</label>
        <select id="adminSession"></select>
        <button id="adminApply">${escapeHtml(t("miniapp.btn.apply", "Применить"))}</button>
      </div>
      <div id="adminStatusBanner" class="admin-status-banner hidden"></div>
      <div id="adminStatusMessage" class="status-empty">${escapeHtml(t("miniapp.admin.choose_session", "Выберите сессию для Admin-панели."))}</div>
      <div id="adminStatusDashboard" class="admin-dashboard hidden">
        <div id="adminDisabledState" class="admin-state-card admin-state-card-disabled hidden">
          <div class="admin-state-title">Admin disabled</div>
          <div id="adminDisabledHint" class="admin-state-copy">
            ${escapeHtml(t("miniapp.admin.disabled_hint_static", "Для выбранной сессии Admin-режим выключен. Включите его или обновите состояние через Rescan."))}
          </div>
          <div class="admin-state-actions">
            <button id="adminEnableAction" type="button">Enable</button>
            <button id="adminDisableAction" type="button">Disable</button>
            <button id="adminRescanAction" type="button">Rescan</button>
          </div>
        </div>
        <div id="adminActiveState" class="admin-live-overview hidden">
          <div class="admin-live-header">
            <div class="admin-live-title-block">
              <div class="admin-section-eyebrow">Live state</div>
              <div class="admin-state-title">${escapeHtml(t("miniapp.admin.state_title", "Состояние админа"))}</div>
              <div id="adminMonitorReadinessHint" class="admin-state-caption">-</div>
            </div>
            <div id="adminMonitorReadiness" class="admin-state-pill admin-state-pill-neutral">-</div>
          </div>
          <div class="admin-summary-grid">
            <div class="status-card admin-summary-card admin-summary-card-strong">
              <div class="status-card-header">Analyzer action</div>
              <div class="status-card-body">
                <div id="adminLastAnalyzerAction" class="admin-state-main-text">-</div>
                <div id="adminLastAnalyzerActionHint" class="admin-state-caption">-</div>
                <div class="admin-state-caption">decision: <span id="adminLastDecision">-</span></div>
                <div class="admin-state-caption">last action: <span id="adminLastAction">-</span></div>
              </div>
            </div>
            <div class="status-card admin-summary-card">
              <div class="status-card-header">Pipeline</div>
              <div class="status-card-body admin-phase-grid">
                <div class="admin-phase-row">
                  <span>Pipeline</span>
                  <strong id="adminPipelineStatus">-</strong>
                </div>
                <div class="admin-phase-row">
                  <span>Monitor</span>
                  <strong id="adminMonitorStatus">-</strong>
                </div>
                <div class="admin-phase-row">
                  <span>Analyzer</span>
                  <strong id="adminAnalyzerStatus">-</strong>
                </div>
                <div class="admin-phase-row">
                  <span>Executor</span>
                  <strong id="adminExecutorStatus">-</strong>
                </div>
                <div class="admin-phase-row">
                  <span>Notifier</span>
                  <strong id="adminNotifierStatus">-</strong>
                </div>
              </div>
            </div>
            <div class="status-card admin-summary-card">
              <div class="status-card-header">Runtime</div>
              <div class="status-card-body admin-compact-stack">
                <div id="adminRuntimeFlags" class="admin-state-caption admin-no-top-margin">-</div>
                <div><span class="admin-muted-label">CLI</span> <span id="adminPinnedCli">-</span></div>
                <div><span class="admin-muted-label">Executor</span> <span id="adminPinnedExecutorProfile">-</span></div>
              </div>
            </div>
            <div class="status-card admin-summary-card">
              <div class="status-card-header">Scan</div>
              <div class="status-card-body admin-compact-stack">
                <div><span class="admin-muted-label">${escapeHtml(t("miniapp.label.status", "Статус"))}</span> <span id="adminScanStatus">-</span></div>
                <div><span class="admin-muted-label">Readiness</span> <span id="adminReadiness">-</span></div>
                <div id="adminScanDetails" class="admin-state-caption admin-no-top-margin">-</div>
              </div>
            </div>
          </div>
          <div class="admin-state-actions admin-overview-actions">
            <button id="adminEnableActionActive" type="button">Enable</button>
            <button id="adminDisableActionActive" type="button">Disable</button>
            <button id="adminRescanActionActive" type="button">Rescan</button>
          </div>
        </div>
        <div id="adminStructuredState" class="admin-workspace hidden">
          <nav class="admin-subtabs" role="tablist" aria-label="Admin sections">
            <button type="button" class="active" data-admin-subtab="overview">${escapeHtml(t("miniapp.admin.subtab.overview", "Обзор"))}</button>
            <button type="button" data-admin-subtab="monitoring">${escapeHtml(t("miniapp.admin.subtab.monitoring", "Мониторинг"))}</button>
            <button type="button" data-admin-subtab="operations">${escapeHtml(t("miniapp.admin.subtab.operations", "Операции"))}</button>
            <button type="button" data-admin-subtab="config">${escapeHtml(t("miniapp.admin.subtab.config", "Конфиг"))}</button>
            <button type="button" data-admin-subtab="chat">${escapeHtml(t("miniapp.admin.subtab.chat", "Чат"))}</button>
            <button type="button" data-admin-subtab="diagnostics">${escapeHtml(t("miniapp.admin.subtab.data", "Данные"))}</button>
          </nav>
          <section id="adminSubtabOverview" class="admin-subtab-panel active" data-admin-subtab-panel="overview">
            <div class="admin-structured-grid">
              <details id="adminRuntimeDetails" class="status-card-details">
                <summary>Runtime overview</summary>
                <div class="status-card-body">
                  <div id="adminRuntimeBody" class="admin-detail-stack"></div>
                </div>
              </details>
              <details id="adminReadinessDetails" class="status-card-details">
                <summary>Component readiness</summary>
                <div class="status-card-body">
                  <div id="adminReadinessBody" class="admin-detail-stack"></div>
                </div>
              </details>
              <details id="adminDecisionDetails" class="status-card-details">
                <summary>Analyzer / executor</summary>
                <div class="status-card-body">
                  <div id="adminDecisionBody" class="admin-detail-stack"></div>
                </div>
              </details>
            </div>
          </section>
          <section id="adminSubtabMonitoring" class="admin-subtab-panel hidden" data-admin-subtab-panel="monitoring">
            <div class="admin-structured-grid">
              <details id="adminEnvironmentDetails" class="status-card-details">
                <summary>Environment</summary>
                <div class="status-card-body">
                  <div id="adminEnvironmentBody" class="admin-detail-stack"></div>
                </div>
              </details>
              <details id="adminAutonomyDetails" class="status-card-details">
                <summary>${escapeHtml(t("miniapp.autonomy.section_title", "Автономность: серверы / baseline / drift / память"))}</summary>
                <div class="status-card-body">
                  <div class="admin-state-actions">
                    <button id="autonomyRefresh" type="button">${escapeHtml(t("miniapp.btn.refresh", "Обновить"))}</button>
                    <button id="autonomyRescanAll" type="button">${escapeHtml(t("miniapp.autonomy.rescan_all", "Пересканировать всё"))}</button>
                    <button id="autonomyMaintenance" type="button">${escapeHtml(t("miniapp.autonomy.daily_maintenance", "Ежедневное обслуживание"))}</button>
                  </div>
                  <div id="autonomyStatusMessage" class="autonomy-status-msg"></div>
                  <div id="autonomyGlobalSummary" class="autonomy-global-summary"></div>
                  <div id="autonomyServersGrid" class="autonomy-servers-grid"></div>
                  <div id="autonomyServerDetail" class="autonomy-server-detail hidden"></div>
                </div>
              </details>
            </div>
          </section>
          <section id="adminSubtabOperations" class="admin-subtab-panel hidden" data-admin-subtab-panel="operations">
            <div class="admin-structured-grid">
              <div class="admin-approval-panel">
                <div class="admin-approval-main">
                  <div class="admin-section-eyebrow">${escapeHtml(t("miniapp.admin.pending_actions", "Ожидает действий"))}</div>
                  <div class="admin-compact-metrics">
                    <span>${escapeHtml(t("miniapp.admin.manual_confirms", "ручные подтверждения"))}: <strong id="adminPendingState">-</strong></span>
                    <span>skill installs: <strong id="adminPendingSkillInstalls">-</strong></span>
                    <span>mute: <strong id="adminMuteState">-</strong></span>
                  </div>
                </div>
                <div class="admin-state-actions admin-approval-actions">
                  <select id="adminSkillApprovalSelect">
                    <option value="">${escapeHtml(t("miniapp.admin.choose_skill", "Выберите pending skill install"))}</option>
                  </select>
                  <button id="adminSkillApprovalApprove" type="button">Approve skill</button>
                  <button id="adminSkillApprovalReject" type="button">Reject skill</button>
                </div>
              </div>
              <div class="admin-approval-panel">
                <div class="admin-approval-main">
                  <div class="admin-section-eyebrow">${escapeHtml(t("miniapp.admin.recent_events", "Последние события"))}</div>
                  <div class="admin-compact-metrics">
                    <span>${escapeHtml(t("miniapp.admin.incidents", "инциденты"))}: <strong id="adminRecentIncidents">-</strong></span>
                    <span>${escapeHtml(t("miniapp.admin.admin_actions", "действия Admin"))}: <strong id="adminRecentActions">-</strong></span>
                    <span>approved overrides: <strong id="adminApprovedOverrides">-</strong></span>
                  </div>
                </div>
              </div>
              <details id="adminOperatorDetails" class="status-card-details">
                <summary>${escapeHtml(t("miniapp.admin.current_pending", "Текущие ожидания / mute"))}</summary>
                <div class="status-card-body">
                  <div id="adminOperatorBody" class="admin-detail-stack"></div>
                </div>
              </details>
              <details id="adminHistoryDetails" class="status-card-details">
                <summary>${escapeHtml(t("miniapp.admin.event_history", "История событий / overrides"))}</summary>
                <div class="status-card-body">
                  <div id="adminHistoryBody" class="admin-detail-stack"></div>
                </div>
              </details>
              <details id="adminRunsDetails" class="status-card-details">
                <summary>Pipeline runs</summary>
                <div class="status-card-body">
                  <div class="admin-state-actions">
                    <button id="adminRunsRefresh" type="button">${escapeHtml(t("miniapp.admin.refresh_runs", "Обновить runs"))}</button>
                    <button id="adminRunsView" type="button">${escapeHtml(t("miniapp.admin.show_details", "Показать детали"))}</button>
                  </div>
                  <table id="adminRunsTable" class="admin-runs-table">
                    <thead>
                      <tr>
                        <th>run_id</th>
                        <th>status</th>
                        <th>phase</th>
                        <th>started_at</th>
                      </tr>
                    </thead>
                    <tbody id="adminRunsTableBody"></tbody>
                  </table>
                  <pre id="adminRunsDetailBody" class="admin-run-detail-body"></pre>
                </div>
              </details>
            </div>
          </section>
          <section id="adminSubtabConfig" class="admin-subtab-panel hidden" data-admin-subtab-panel="config">
            <div class="admin-structured-grid">
              <details id="adminMonitorServersDetails" class="status-card-details">
                <summary>Monitor servers</summary>
                <div class="status-card-body">
                  <div class="admin-state-actions">
                    <button id="adminMonitorServersReload" type="button">${escapeHtml(t("miniapp.btn.load", "Загрузить"))}</button>
                    <button id="adminMonitorServersAdd" type="button">${escapeHtml(t("miniapp.admin.add_server", "Добавить сервер"))}</button>
                    <button id="adminMonitorServersSave" type="button">${escapeHtml(t("miniapp.btn.save", "Сохранить"))}</button>
                  </div>
                  <div class="admin-monitor-meta">
                    <label>
                      <input type="checkbox" id="adminMonitorEnabled"> ${escapeHtml(t("miniapp.admin.enabled", "Включён"))}
                    </label>
                    <label>
                      interval_sec
                      <input type="number" id="adminMonitorInterval" min="1" step="1" style="width:80px;">
                    </label>
                  </div>
                  <div id="adminMonitorServersStatus" class="admin-config-status"></div>
                  <table id="adminMonitorServersTable" class="admin-runs-table">
                    <thead>
                      <tr>
                        <th>${escapeHtml(t("miniapp.admin.th_server_ssh", "server (из SSH hosts)"))}</th>
                        <th>action_id</th>
                        <th>timeout_sec</th>
                        <th></th>
                      </tr>
                    </thead>
                    <tbody id="adminMonitorServersBody"></tbody>
                  </table>
                  <div class="admin-monitor-hint">${escapeHtml(t("miniapp.admin.monitor_hint", "SSH-хосты редактируются на вкладке «Настройки». Здесь выбирается только server → action."))}</div>
                </div>
              </details>
              <details id="adminSshActionsDetails" class="status-card-details">
                <summary>SSH actions</summary>
                <div class="status-card-body">
                  <div class="admin-state-actions">
                    <button id="adminSshActionsReload" type="button">${escapeHtml(t("miniapp.btn.load", "Загрузить"))}</button>
                    <button id="adminSshActionsAdd" type="button">${escapeHtml(t("miniapp.admin.add_action", "Добавить действие"))}</button>
                    <button id="adminSshActionsSave" type="button">${escapeHtml(t("miniapp.btn.save", "Сохранить"))}</button>
                  </div>
                  <div id="adminSshActionsStatus" class="admin-config-status"></div>
                  <table id="adminSshActionsTable" class="admin-runs-table">
                    <thead>
                      <tr>
                        <th>action_id</th>
                        <th>${escapeHtml(t("miniapp.admin.th_argv", "argv (по одному пункту на строке)"))}</th>
                        <th>timeout_sec</th>
                        <th>risk_level</th>
                        <th>description</th>
                        <th></th>
                      </tr>
                    </thead>
                    <tbody id="adminSshActionsBody"></tbody>
                  </table>
                </div>
              </details>
              <details id="adminConfigDetails" class="status-card-details">
                <summary>Admin config YAML</summary>
                <div class="status-card-body">
                  <div class="admin-state-actions">
                    <button id="adminConfigReload" type="button">${escapeHtml(t("miniapp.btn.load", "Загрузить"))}</button>
                  </div>
                  <div id="adminConfigStatus" class="admin-config-status"></div>
                  <textarea id="adminConfigEditor" class="admin-config-editor" rows="20" spellcheck="false" readonly placeholder="${escapeHtml(t("miniapp.admin.config_placeholder", "Нажмите «Загрузить» для просмотра итогового YAML."))}"></textarea>
                </div>
              </details>
            </div>
          </section>
          <section id="adminSubtabChat" class="admin-subtab-panel hidden" data-admin-subtab-panel="chat">
            <details id="adminChatDetails" class="status-card-details admin-chat-details" open>
              <summary>Admin chat</summary>
              <div class="status-card-body admin-chat-body">
                <div id="adminChatStatus" class="admin-chat-status"></div>
                <div id="adminChatMessages" class="admin-chat-messages"></div>
                <div class="admin-chat-input-row">
                  <input id="adminChatInput" type="text" placeholder="${escapeHtml(t("miniapp.admin.chat_placeholder", "Спросите админа (Enter = отправить)"))}">
                  <button id="adminChatSend" type="button">Send</button>
                </div>
                <div class="admin-chat-pending">
                  <div id="adminChatPendingLabel" class="admin-chat-pending-label">Pending approvals (0):</div>
                  <div id="adminChatPendingList" class="admin-chat-pending-list"></div>
                </div>
                <div class="admin-chat-memory">
                  <label for="adminChatMemory" class="admin-chat-memory-label">MEMORY.md</label>
                  <textarea id="adminChatMemory" class="admin-chat-memory-editor" rows="6" placeholder="${escapeHtml(t("miniapp.admin.memory_placeholder", "Заметки для агента (свободный текст)."))}"></textarea>
                  <div class="admin-state-actions">
                    <button id="adminChatMemoryReload" type="button">Reload</button>
                    <button id="adminChatMemorySave" type="button">Save</button>
                  </div>
                </div>
              </div>
            </details>
          </section>
          <section id="adminSubtabDiagnostics" class="admin-subtab-panel hidden" data-admin-subtab-panel="diagnostics">
            <details class="status-card-details status-raw admin-raw-details">
              <summary>Admin JSON</summary>
              <div class="status-card-body">
                <pre id="adminStatusJson"></pre>
              </div>
            </details>
          </section>
        </div>
      </div>
    `;
    root.dataset.ready = "1";
    bindAdminSubtabHandlers();
    bindAdminChatHandlers();
  }

  function isAdminTabActive() {
    return !!document.getElementById("tab-admin")?.classList.contains("active");
  }

  function applyAdminStateFromControls() {
    const session = document.getElementById("adminSession");
    state.adminSessionUid = String(session?.value || "");
  }

  function adminActionButtons() {
    return [
      document.getElementById("adminEnableAction"),
      document.getElementById("adminDisableAction"),
      document.getElementById("adminRescanAction"),
      document.getElementById("adminSkillApprovalApprove"),
      document.getElementById("adminSkillApprovalReject"),
      document.getElementById("adminEnableActionActive"),
      document.getElementById("adminDisableActionActive"),
      document.getElementById("adminRescanActionActive"),
    ].filter(Boolean);
  }

  async function performAdminAction(action, extraPayload = null) {
    applyAdminStateFromControls();
    const sessionUid = String(state.adminSessionUid || "").trim();
    if (!sessionUid) {
      setAdminStatusBanner("");
      setAdminStateVisibility({});
      setAdminStructuredVisibility(false);
      setAdminStatusMessage(t("miniapp.status.no_session", "Сессия не выбрана"), false);
      return null;
    }
    state.adminRequestInFlight = true;
    adminActionButtons().forEach((button) => {
      button.disabled = true;
    });
    try {
      const body = {
        action: String(action || "").trim().toLowerCase(),
        session_uid: sessionUid,
      };
      if (extraPayload && typeof extraPayload === "object") {
        Object.assign(body, extraPayload);
      }
      const response = await api("/v1/admin/action", {
        method: "POST",
        body: JSON.stringify(body),
      });
      const payload = response?.status || {};
      const result = response?.result || null;
      setAdminCachedStatus(sessionUid, payload);
      setAdminStatusBanner(result && result.message ? String(result.message || "") : "");
      renderAdminPayload(payload);
      return payload;
    } catch (err) {
      const stalePayload = getAdminCachedStatus(sessionUid, { allowStale: true });
      if (stalePayload) {
        renderAdminPayload(stalePayload);
        setAdminStatusBanner(`${t("miniapp.admin.err_action_stale", "Ошибка admin action: {detail}. Показаны последние известные данные.").replace("{detail}", err.message || "unknown")}`, false);
        return stalePayload;
      }
      setAdminStatusBanner("");
      setAdminStateVisibility({});
      setAdminStructuredVisibility(false);
      setAdminStatusMessage(`${t("miniapp.admin.err_action", "Ошибка admin action:")} ${err.message || "unknown"}`, false);
      return null;
    } finally {
      state.adminRequestInFlight = false;
      if (state.adminLastPayload && String(state.adminLastPayload.session_uid || "") === sessionUid) {
        renderAdminPayload(state.adminLastPayload);
      }
    }
  }

  async function refreshAdminRuns() {
    applyAdminStateFromControls();
    const sessionUid = String(state.adminSessionUid || "").trim();
    const tbody = document.getElementById("adminRunsTableBody");
    const detailBody = document.getElementById("adminRunsDetailBody");
    if (detailBody) detailBody.textContent = "";
    if (!tbody) return;
    tbody.innerHTML = "";
    if (!sessionUid) {
      setAdminStatusBanner(t("miniapp.status.no_session", "Сессия не выбрана."), false);
      return;
    }
    try {
      const response = await api(`/v1/admin/runs?session_uid=${encodeURIComponent(sessionUid)}&limit=20`);
      const runs = Array.isArray(response?.runs) ? response.runs : [];
      if (!runs.length) {
        const tr = document.createElement("tr");
        const td = document.createElement("td");
        td.colSpan = 4;
        td.textContent = t("miniapp.admin.no_runs", "Нет runs для этой сессии.");
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
      }
      for (const row of runs) {
        const tr = document.createElement("tr");
        tr.dataset.runId = String(row.run_id || "");
        tr.onclick = () => {
          Array.from(tbody.querySelectorAll("tr")).forEach((el) => el.classList.remove("selected"));
          tr.classList.add("selected");
        };
        ["run_id", "status", "phase", "started_at"].forEach((key) => {
          const td = document.createElement("td");
          td.textContent = String(row[key] != null ? row[key] : "-");
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      }
    } catch (err) {
      setAdminStatusBanner(`${t("miniapp.admin.err_load_runs", "Ошибка загрузки runs:")} ${err.message || "unknown"}`, false);
    }
  }

  async function reloadAdminConfig() {
    applyAdminStateFromControls();
    const sessionUid = String(state.adminSessionUid || "").trim();
    const editor = document.getElementById("adminConfigEditor");
    const statusEl = document.getElementById("adminConfigStatus");
    if (!editor) return;
    if (!sessionUid) {
      if (statusEl) statusEl.textContent = t("miniapp.status.no_session", "Сессия не выбрана.");
      return;
    }
    if (statusEl) statusEl.textContent = t("miniapp.status.loading", "Загрузка…");
    try {
      const response = await api(`/v1/admin/config?session_uid=${encodeURIComponent(sessionUid)}`);
      editor.value = String(response?.yaml || "");
      if (statusEl) statusEl.textContent = `${t("miniapp.admin.loaded_prefix", "Загружено: ")}${response?.config_path || "-"}`;
    } catch (err) {
      if (statusEl) statusEl.textContent = `${t("miniapp.error.load_prefix", "Ошибка загрузки:")} ${err.message || "unknown"}`;
    }
  }

  async function loadAdminHostsOptions(sessionUid) {
    try {
      const response = await api(`/v1/admin/hosts?session_uid=${encodeURIComponent(sessionUid)}`);
      const hosts = Array.isArray(response?.hosts) ? response.hosts : [];
      state.adminHosts = hosts;
      return hosts;
    } catch (err) {
      state.adminHosts = [{ alias: "local", target: "local" }];
      return state.adminHosts;
    }
  }

  function renderAdminMonitorServers(rows) {
    const body = document.getElementById("adminMonitorServersBody");
    if (!body) return;
    body.innerHTML = "";
    const hosts = Array.isArray(state.adminHosts) ? state.adminHosts : [];
    rows.forEach((row, index) => {
      const tr = document.createElement("tr");

      const tdHost = document.createElement("td");
      const selHost = document.createElement("select");
      selHost.dataset.field = "server";
      selHost.dataset.index = String(index);
      for (const host of hosts) {
        const opt = document.createElement("option");
        opt.value = String(host.alias);
        opt.textContent = `${host.alias} (${host.target || "local"})`;
        if (host.alias === row.id) opt.selected = true;
        selHost.appendChild(opt);
      }
      if (row.id && !hosts.some((h) => h.alias === row.id)) {
        const opt = document.createElement("option");
        opt.value = row.id;
        opt.textContent = `${row.id} (missing)`;
        opt.selected = true;
        selHost.appendChild(opt);
      }
      tdHost.appendChild(selHost);
      tr.appendChild(tdHost);

      const tdAction = document.createElement("td");
      const inpAction = document.createElement("input");
      inpAction.type = "text";
      inpAction.value = String(row.action_id || "");
      inpAction.dataset.field = "action_id";
      inpAction.dataset.index = String(index);
      tdAction.appendChild(inpAction);
      tr.appendChild(tdAction);

      const tdTimeout = document.createElement("td");
      const inpTimeout = document.createElement("input");
      inpTimeout.type = "number";
      inpTimeout.min = "1";
      inpTimeout.step = "1";
      inpTimeout.style.width = "80px";
      inpTimeout.value = row.timeout_sec != null ? String(row.timeout_sec) : "";
      inpTimeout.dataset.field = "timeout_sec";
      inpTimeout.dataset.index = String(index);
      tdTimeout.appendChild(inpTimeout);
      tr.appendChild(tdTimeout);

      const tdDel = document.createElement("td");
      const btnDel = document.createElement("button");
      btnDel.type = "button";
      btnDel.textContent = t("miniapp.btn.delete", "Удалить");
      btnDel.dataset.action = "delete-monitor-server";
      btnDel.dataset.index = String(index);
      tdDel.appendChild(btnDel);
      tr.appendChild(tdDel);

      body.appendChild(tr);
    });
  }

  function collectAdminMonitorServersFromUi() {
    const body = document.getElementById("adminMonitorServersBody");
    if (!body) return [];
    const result = [];
    const rows = body.querySelectorAll("tr");
    rows.forEach((tr) => {
      const sel = tr.querySelector('select[data-field="server"]');
      const action = tr.querySelector('input[data-field="action_id"]');
      const timeout = tr.querySelector('input[data-field="timeout_sec"]');
      const alias = String(sel?.value || "").trim();
      if (!alias) return;
      const host = (state.adminHosts || []).find((h) => h.alias === alias);
      const target = host ? String(host.target || "local") : "local";
      const row = {
        id: alias,
        target,
        action_id: String(action?.value || "").trim(),
      };
      const timeoutRaw = String(timeout?.value || "").trim();
      if (timeoutRaw) row.timeout_sec = Number(timeoutRaw);
      result.push(row);
    });
    return result;
  }

  async function reloadAdminMonitorServers() {
    applyAdminStateFromControls();
    const sessionUid = String(state.adminSessionUid || "").trim();
    const statusEl = document.getElementById("adminMonitorServersStatus");
    if (!sessionUid) {
      if (statusEl) statusEl.textContent = t("miniapp.status.no_session", "Сессия не выбрана.");
      return;
    }
    if (statusEl) statusEl.textContent = t("miniapp.status.loading", "Загрузка…");
    try {
      await loadAdminHostsOptions(sessionUid);
      const response = await api(
        `/v1/admin/monitor/servers?session_uid=${encodeURIComponent(sessionUid)}`,
      );
      const servers = Array.isArray(response?.servers) ? response.servers : [];
      state.adminMonitorServers = servers.map((s) => ({
        id: String(s.id || ""),
        target: String(s.target || "local"),
        action_id: String(s.action_id || ""),
        timeout_sec: s.timeout_sec != null ? Number(s.timeout_sec) : null,
      }));
      const enabledCb = document.getElementById("adminMonitorEnabled");
      if (enabledCb) enabledCb.checked = Boolean(response?.enabled);
      const intervalInp = document.getElementById("adminMonitorInterval");
      if (intervalInp) intervalInp.value = String(response?.interval_sec || 30);
      renderAdminMonitorServers(state.adminMonitorServers);
      if (statusEl) statusEl.textContent = "";
    } catch (err) {
      if (statusEl) statusEl.textContent = `${t("miniapp.error.load_prefix", "Ошибка загрузки:")} ${err.message || "unknown"}`;
    }
  }

  function addAdminMonitorServerRow() {
    if (!Array.isArray(state.adminMonitorServers)) state.adminMonitorServers = [];
    const defaultAlias = (state.adminHosts && state.adminHosts[0]?.alias) || "local";
    state.adminMonitorServers = collectAdminMonitorServersFromUi();
    state.adminMonitorServers.push({
      id: defaultAlias,
      target: "local",
      action_id: "",
      timeout_sec: null,
    });
    renderAdminMonitorServers(state.adminMonitorServers);
  }

  async function saveAdminMonitorServers() {
    applyAdminStateFromControls();
    const sessionUid = String(state.adminSessionUid || "").trim();
    const statusEl = document.getElementById("adminMonitorServersStatus");
    if (!sessionUid) {
      if (statusEl) statusEl.textContent = t("miniapp.status.no_session", "Сессия не выбрана.");
      return;
    }
    const servers = collectAdminMonitorServersFromUi();
    const enabledCb = document.getElementById("adminMonitorEnabled");
    const intervalInp = document.getElementById("adminMonitorInterval");
    const body = {
      session_uid: sessionUid,
      servers,
      enabled: Boolean(enabledCb?.checked),
    };
    const intervalRaw = String(intervalInp?.value || "").trim();
    if (intervalRaw) body.interval_sec = Number(intervalRaw);
    if (statusEl) statusEl.textContent = t("miniapp.status.saving", "Сохранение…");
    try {
      await api("/v1/admin/monitor/servers", { method: "PUT", body: JSON.stringify(body) });
      if (statusEl) statusEl.textContent = t("miniapp.status.saved", "Сохранено.");
    } catch (err) {
      if (statusEl) statusEl.textContent = `${t("miniapp.error.save", "Ошибка сохранения")}: ${err.message || "unknown"}`;
    }
  }

  function renderAdminSshActions(rows) {
    const body = document.getElementById("adminSshActionsBody");
    if (!body) return;
    body.innerHTML = "";
    rows.forEach((row, index) => {
      const tr = document.createElement("tr");

      const tdId = document.createElement("td");
      const inpId = document.createElement("input");
      inpId.type = "text";
      inpId.value = String(row.action_id || "");
      inpId.dataset.field = "action_id";
      inpId.dataset.index = String(index);
      tdId.appendChild(inpId);
      tr.appendChild(tdId);

      const tdArgv = document.createElement("td");
      const taArgv = document.createElement("textarea");
      taArgv.rows = 3;
      taArgv.value = Array.isArray(row.argv) ? row.argv.join("\n") : "";
      taArgv.dataset.field = "argv";
      taArgv.dataset.index = String(index);
      tdArgv.appendChild(taArgv);
      tr.appendChild(tdArgv);

      const tdTimeout = document.createElement("td");
      const inpTimeout = document.createElement("input");
      inpTimeout.type = "number";
      inpTimeout.min = "1";
      inpTimeout.step = "1";
      inpTimeout.style.width = "80px";
      inpTimeout.value = row.timeout_sec != null ? String(row.timeout_sec) : "30";
      inpTimeout.dataset.field = "timeout_sec";
      inpTimeout.dataset.index = String(index);
      tdTimeout.appendChild(inpTimeout);
      tr.appendChild(tdTimeout);

      const tdRisk = document.createElement("td");
      const selRisk = document.createElement("select");
      for (const level of ["low", "medium", "high"]) {
        const opt = document.createElement("option");
        opt.value = level;
        opt.textContent = level;
        if (row.risk_level === level) opt.selected = true;
        selRisk.appendChild(opt);
      }
      selRisk.dataset.field = "risk_level";
      selRisk.dataset.index = String(index);
      tdRisk.appendChild(selRisk);
      tr.appendChild(tdRisk);

      const tdDesc = document.createElement("td");
      const inpDesc = document.createElement("input");
      inpDesc.type = "text";
      inpDesc.value = String(row.description || "");
      inpDesc.dataset.field = "description";
      inpDesc.dataset.index = String(index);
      tdDesc.appendChild(inpDesc);
      tr.appendChild(tdDesc);

      const tdDel = document.createElement("td");
      const btnDel = document.createElement("button");
      btnDel.type = "button";
      btnDel.textContent = t("miniapp.btn.delete", "Удалить");
      btnDel.dataset.action = "delete-ssh-action";
      btnDel.dataset.index = String(index);
      tdDel.appendChild(btnDel);
      tr.appendChild(tdDel);

      body.appendChild(tr);
    });
  }

  function collectAdminSshActionsFromUi() {
    const body = document.getElementById("adminSshActionsBody");
    if (!body) return [];
    const result = [];
    const rows = body.querySelectorAll("tr");
    rows.forEach((tr) => {
      const actionId = String(tr.querySelector('input[data-field="action_id"]')?.value || "").trim();
      const argvRaw = String(tr.querySelector('textarea[data-field="argv"]')?.value || "").trim();
      if (!actionId || !argvRaw) return;
      const argv = argvRaw.split(/\r?\n/).map((s) => s.trim()).filter((s) => s.length > 0);
      const timeoutRaw = String(tr.querySelector('input[data-field="timeout_sec"]')?.value || "").trim();
      const risk = String(tr.querySelector('select[data-field="risk_level"]')?.value || "low").trim();
      const description = String(tr.querySelector('input[data-field="description"]')?.value || "").trim();
      const row = { action_id: actionId, argv, risk_level: risk };
      if (timeoutRaw) row.timeout_sec = Number(timeoutRaw);
      if (description) row.description = description;
      result.push(row);
    });
    return result;
  }

  async function reloadAdminSshActions() {
    applyAdminStateFromControls();
    const sessionUid = String(state.adminSessionUid || "").trim();
    const statusEl = document.getElementById("adminSshActionsStatus");
    if (!sessionUid) {
      if (statusEl) statusEl.textContent = t("miniapp.status.no_session", "Сессия не выбрана.");
      return;
    }
    if (statusEl) statusEl.textContent = t("miniapp.status.loading", "Загрузка…");
    try {
      const response = await api(
        `/v1/admin/actions/ssh?session_uid=${encodeURIComponent(sessionUid)}`,
      );
      const actions = Array.isArray(response?.actions) ? response.actions : [];
      state.adminSshActions = actions.map((a) => ({
        action_id: String(a.action_id || ""),
        argv: Array.isArray(a.argv) ? a.argv.map(String) : [],
        timeout_sec: a.timeout_sec != null ? Number(a.timeout_sec) : 30,
        risk_level: String(a.risk_level || "low"),
        description: String(a.description || ""),
      }));
      renderAdminSshActions(state.adminSshActions);
      if (statusEl) statusEl.textContent = "";
    } catch (err) {
      if (statusEl) statusEl.textContent = `${t("miniapp.error.load_prefix", "Ошибка загрузки:")} ${err.message || "unknown"}`;
    }
  }

  function addAdminSshActionRow() {
    if (!Array.isArray(state.adminSshActions)) state.adminSshActions = [];
    state.adminSshActions = collectAdminSshActionsFromUi();
    state.adminSshActions.push({
      action_id: "",
      argv: [],
      timeout_sec: 30,
      risk_level: "low",
      description: "",
    });
    renderAdminSshActions(state.adminSshActions);
  }

  async function saveAdminSshActions() {
    applyAdminStateFromControls();
    const sessionUid = String(state.adminSessionUid || "").trim();
    const statusEl = document.getElementById("adminSshActionsStatus");
    if (!sessionUid) {
      if (statusEl) statusEl.textContent = t("miniapp.status.no_session", "Сессия не выбрана.");
      return;
    }
    const actions = collectAdminSshActionsFromUi();
    if (statusEl) statusEl.textContent = t("miniapp.status.saving", "Сохранение…");
    try {
      await api("/v1/admin/actions/ssh", {
        method: "PUT",
        body: JSON.stringify({ session_uid: sessionUid, actions }),
      });
      if (statusEl) statusEl.textContent = t("miniapp.status.saved", "Сохранено.");
    } catch (err) {
      if (statusEl) statusEl.textContent = `${t("miniapp.error.save", "Ошибка сохранения")}: ${err.message || "unknown"}`;
    }
  }

  function adminRunDetailText(payload) {
    const detail = isPlainObject(payload) ? payload : {};
    const statePayload = isPlainObject(detail.state) ? detail.state : {};
    const events = Array.isArray(detail.events) ? detail.events : [];
    const lines = [
      `run_id: ${statusValueText(detail.run_id)}`,
      `status: ${statusValueText(statePayload.status)}`,
      `phase: ${statusValueText(statePayload.phase)}`,
      `started_at: ${statusValueText(statePayload.started_at)}`,
      `finished_at: ${statusValueText(statePayload.finished_at)}`,
      "",
      "--- events (tail) ---",
    ];
    events.slice(-20).forEach((event) => {
      if (!isPlainObject(event)) return;
      const eventType = event.event_type || event.type || "-";
      const eventStatus = event.status || "-";
      const eventTs = event.ts || event.timestamp || "-";
      const message = event.message || event.reason || "";
      lines.push(
        `[${statusValueText(eventTs)}] ${statusValueText(eventType)} status=${statusValueText(eventStatus)}${message ? ` | ${statusValueText(message)}` : ""}`,
      );
    });
    return lines.join("\n").trim() || t("miniapp.status.no_data", "Нет данных.");
  }

  async function viewAdminRunDetail() {
    applyAdminStateFromControls();
    const sessionUid = String(state.adminSessionUid || "").trim();
    const detailBody = document.getElementById("adminRunsDetailBody");
    if (!sessionUid || !detailBody) return;
    const tbody = document.getElementById("adminRunsTableBody");
    const selected = tbody?.querySelector("tr.selected");
    const runId = String(selected?.dataset?.runId || "").trim();
    if (!runId) {
      detailBody.textContent = t("miniapp.runs.select_hint", "Выберите run в таблице.");
      return;
    }
    detailBody.textContent = t("miniapp.status.loading", "Загрузка…");
    try {
      const payload = await api(
        `/v1/admin/runs/${encodeURIComponent(runId)}?session_uid=${encodeURIComponent(sessionUid)}&events_limit=50`
      );
      detailBody.textContent = adminRunDetailText(payload);
    } catch (err) {
      detailBody.textContent = `${t("miniapp.error.generic_prefix", "Ошибка:")} ${err.message || "unknown"}`;
    }
  }

  function applySettingsStateFromControls() {
    const session = document.getElementById("settingsSession");
    state.settingsSessionUid = String(session?.value || "");
  }

  async function fetchSessionSettings() {
    applySettingsStateFromControls();
    const uid = state.settingsSessionUid;
    const panel = document.getElementById("settingsPanel");
    const empty = document.getElementById("settingsEmpty");
    if (!uid) {
      empty.textContent = t("miniapp.settings.choose_session", "Выберите сессию для управления настройками.");
      empty.classList.remove("hidden");
      panel.classList.add("hidden");
      return;
    }
    state.settingsLoading = true;
    try {
      const data = await api(`/session/${uid}/settings`);
      state.settingsData = data;
      renderSettings();
      empty.classList.add("hidden");
      panel.classList.remove("hidden");
    } catch (err) {
      empty.textContent = `${t("miniapp.error.generic_prefix", "Ошибка:")} ${err.message || "unknown"}`;
      empty.classList.remove("hidden");
      panel.classList.add("hidden");
    } finally {
      state.settingsLoading = false;
    }
  }

  function renderSettings() {
    const data = state.settingsData;
    if (!data || !data.settings) return;
    const activeMode = document.getElementById("settingsActiveMode");
    const sshEnabled = document.getElementById("settingsSshEnabled");
    const sshNote = document.getElementById("settingsSshNote");
    const sshHostsCard = document.getElementById("settingsSshHostsCard");
    const executionBackend = document.getElementById("settingsExecutionBackend");
    const executionBackendNote = document.getElementById("settingsExecutionBackendNote");

    // NEW Remote Control elements
    const rcEnabled = document.getElementById("settingsRemoteControlEnabled");
    const rcHostField = document.getElementById("settingsRemoteControlHostField");
    const rcHostSelect = document.getElementById("settingsRemoteControlHost");
    const rcError = document.getElementById("settingsRemoteControlError");
    const execTargetBanner = document.getElementById("settingsExecutionTargetBanner");

    const sshRemoteEnabled = !!data.settings.ssh_remote_enabled;
    const sshConfigExists = !!data.available?.ssh_config_exists;
    const sshAvailable = !!data.available?.ssh_available;
    const modeItems = Array.isArray(data.available?.modes)
      ? data.available.modes
      : (Array.isArray(state.statusLastPayload?.modes) ? state.statusLastPayload.modes : []);
    const directCliAllowed = data.available?.direct_cli_allowed !== false;
    const activeModeOptions = [
      ...(directCliAllowed ? [`<option value="">${escapeHtml(t("miniapp.mode.direct_cli", "Прямой CLI"))}</option>`] : []),
      ...modeItems
        .map((item) => {
          const modeId = String(item?.id || "");
          const label = String(item?.label || modeId);
          return modeId ? `<option value="${escapeHtml(modeId)}">${escapeHtml(label)}</option>` : "";
        })
        .filter(Boolean),
    ];
    activeMode.innerHTML = activeModeOptions.join("");
    activeMode.value = String(data.settings.active_mode || "");

    const backendOptions = Array.isArray(data.available?.execution_backends)
      ? data.available.execution_backends
      : [];
    executionBackend.innerHTML = backendOptions
      .map((backend) => `<option value="${escapeHtml(String(backend))}">${escapeHtml(String(backend))}</option>`)
      .join("");
    if (data.settings.execution_backend) {
      executionBackend.value = String(data.settings.execution_backend || "");
    }
    const backendBlockers = Array.isArray(data.available?.backend_switch_blockers)
      ? data.available.backend_switch_blockers
      : [];
    executionBackendNote.textContent = backendBlockers.length
      ? `${t("miniapp.settings.backend_configured", "Backend настраивается в Config settings")}: ${backendBlockers.join(", ")}`
      : t("miniapp.settings.backend_configured", "Backend настраивается в Config settings");

    sshEnabled.checked = sshRemoteEnabled;
    sshEnabled.disabled = false;
    sshNote.style.color = "";
    if (!sshRemoteEnabled) {
      sshNote.textContent = t("miniapp.ssh.toggle_hint", "Включает/выключает удаленное выполнение команд через SSH.");
    } else if (!sshConfigExists) {
      sshNote.textContent = t("miniapp.ssh.no_config", "SSH Remote включён, но ssh.yaml отсутствует в .cli-proxy/.");
      sshNote.style.color = "var(--danger)";
    } else if (!sshAvailable) {
      sshNote.textContent = t("miniapp.ssh.enabled_no_hosts", "SSH Remote включён. Добавьте hosts в ssh.yaml или через форму ниже.");
    } else {
      sshNote.textContent = t("miniapp.ssh.toggle_hint", "Включает/выключает удаленное выполнение команд через SSH.");
    }

    if (sshEnabled.checked) {
      sshHostsCard.classList.remove("hidden");
      void fetchSshHosts();
    } else {
      sshHostsCard.classList.add("hidden");
    }

    // Remote Control binding
    const isRcEnabled = !!data.settings.remote_control_enabled;
    rcEnabled.checked = isRcEnabled;
    
    // Fill host select
    const rcHosts = data.available?.remote_control_hosts || data.remote_control_hosts || {};
    const validHosts = Object.entries(rcHosts).filter(([_, cfg]) => !!cfg.remote_project_root);
    if (validHosts.length > 0) {
      rcHostSelect.innerHTML = validHosts.map(([alias, _]) => {
        return `<option value="${escapeHtml(alias)}">${escapeHtml(alias)}</option>`;
      }).join("");
    } else if (Object.keys(rcHosts).length > 0) {
      rcHostSelect.innerHTML = `<option value="">${escapeHtml(t("miniapp.settings.no_eligible_hosts", "Нет подходящих хостов"))}</option>`;
    } else {
      rcHostSelect.innerHTML = "";
    }

    if (data.settings.remote_control_host_alias) {
      rcHostSelect.value = data.settings.remote_control_host_alias;
    }

    // Toggle field visibility
    rcHostField.style.display = isRcEnabled ? "block" : "none";
    if (Object.keys(rcHosts).length > 0 && validHosts.length === 0) {
      rcError.textContent = t("miniapp.settings.rc_error_no_root", "SSH host существует, но для Remote Control нужно заполнить remote_project_root.");
      rcError.style.display = "block";
    } else {
      rcError.style.display = "none";
      rcError.textContent = "";
    }

    // Show effective target
    const effective = data.effective || {};
    if (effective.execution_target === "remote") {
      execTargetBanner.textContent = t("miniapp.settings.exec_target_remote", "Цель выполнения: удалённо — {host}").replace("{host}", effective.host_alias + " — " + effective.remote_project_root);
      execTargetBanner.style.backgroundColor = "var(--button-primary)";
      execTargetBanner.style.color = "white";
    } else {
      execTargetBanner.textContent = t("miniapp.settings.exec_target_local", "Цель выполнения: локально");
      execTargetBanner.style.backgroundColor = "var(--card-bg)";
      execTargetBanner.style.color = "";
    }
    
    // Busy session lock
    const isBusy = !!state.statusLastPayload?.active_session?.busy;
    activeMode.disabled = isBusy;
    executionBackend.disabled = true;
    sshEnabled.disabled = isBusy;
    rcEnabled.disabled = isBusy;
    rcHostSelect.disabled = isBusy || validHosts.length === 0;
    document.getElementById("settingsSave").disabled = isBusy;
    document.getElementById("settingsRemoteControlRecheck").disabled = isBusy;
  }

  async function saveSessionSettings() {
    const uid = state.settingsSessionUid;
    if (!uid) return;
    const activeMode = document.getElementById("settingsActiveMode").value;
    const sshEnabled = document.getElementById("settingsSshEnabled").checked;
    const rcEnabled = document.getElementById("settingsRemoteControlEnabled").checked;
    const rcHost = document.getElementById("settingsRemoteControlHost").value;

    const rcError = document.getElementById("settingsRemoteControlError");
    rcError.style.display = "none";
    rcError.textContent = "";

    try {
      await api(`/session/${uid}/settings`, {
        method: "PUT",
        body: JSON.stringify({
          active_mode: activeMode,
          ssh_remote_enabled: sshEnabled,
          remote_control_enabled: rcEnabled,
          remote_control_host_alias: rcHost
        })
      });
      tg.showScanResult?.(t("miniapp.settings.saved", "Настройки сохранены"));
      await fetchSessionSettings();
    } catch (err) {
      const preflightError = err?.body?.preflight?.error ?? err?.body?.error;
      rcError.textContent = preflightError
        ? `Preflight failed: ${t("errors." + preflightError, preflightError)}`
        : `${t("miniapp.error.save", "Ошибка сохранения")}: ${err.message || "unknown"}`;
      rcError.style.display = "block";
    }
  }

  async function fetchSshHosts() {
    const data = state.settingsData;
    if (!data || !data.settings) return;
    const workdir = getSshWorkdir();
    if (!workdir) {
      console.warn("SSH hosts: project workdir is unavailable");
      return;
    }
    
    state.sshHostsLoading = true;
    try {
      const res = await api(`/ssh/hosts?workdir=${encodeURIComponent(workdir)}`);
      if (res.ok) {
        state.sshHosts = res.hosts || {};
        renderSshHosts();
      }
    } catch (err) {
      console.error("Failed to fetch SSH hosts", err);
    } finally {
      state.sshHostsLoading = false;
    }
  }

  function renderSshHosts() {
    const list = document.getElementById("sshHostsList");
    const empty = document.getElementById("sshHostsEmpty");
    const tbody = document.getElementById("sshHostsTableBody");
    if (!list || !empty || !tbody) return;

    const aliases = Object.keys(state.sshHosts);
    if (aliases.length === 0) {
      list.classList.add("hidden");
      empty.classList.remove("hidden");
      return;
    }

    empty.classList.add("hidden");
    list.classList.remove("hidden");
    
    tbody.innerHTML = aliases.map(alias => {
      const host = state.sshHosts[alias];
      return `
        <tr style="border-bottom: 1px solid var(--surface-alt);">
          <td style="padding: 8px;"><strong>${escapeHtml(alias)}</strong></td>
          <td style="padding: 8px;">${escapeHtml(host.host)}:${host.port}</td>
          <td style="padding: 8px;">${escapeHtml(host.user)}</td>
          <td style="padding: 8px;">${escapeHtml(host.auth)}</td>
          <td style="padding: 8px;">
            <button class="btn-sm ssh-edit-btn" data-alias="${escapeHtml(alias)}">Edit</button>
            <button class="btn-sm ssh-test-btn" data-alias="${escapeHtml(alias)}">Test</button>
            <button class="btn-sm ssh-keygen-btn" data-alias="${escapeHtml(alias)}" ${host.auth !== 'key' ? 'disabled' : ''}>Keygen</button>
            <button class="btn-sm ssh-delete-btn btn-danger" data-alias="${escapeHtml(alias)}">Del</button>
          </td>
        </tr>
      `;
    }).join("");

    // Bind row buttons
    tbody.querySelectorAll(".ssh-edit-btn").forEach(btn => {
      btn.onclick = () => openSshHostForm(btn.dataset.alias);
    });
    tbody.querySelectorAll(".ssh-test-btn").forEach(btn => {
      btn.onclick = () => testSshConnection(btn.dataset.alias);
    });
    tbody.querySelectorAll(".ssh-keygen-btn").forEach(btn => {
      btn.onclick = () => generateSshKey(btn.dataset.alias);
    });
    tbody.querySelectorAll(".ssh-delete-btn").forEach(btn => {
      btn.onclick = () => deleteSshHost(btn.dataset.alias);
    });
  }

  function getSshWorkdir() {
    const workdir =
      state.settingsData?.available?.project_workdir ||
      state.statusLastPayload?.active_session?.workdir ||
      state.currentDir ||
      "";
    return String(workdir || "").trim();
  }

  function openSshHostForm(alias = "") {
    const form = document.getElementById("sshHostForm");
    const title = document.getElementById("sshHostFormTitle");
    const aliasInput = document.getElementById("sshHostAlias");
    const origAliasInput = document.getElementById("sshHostAliasOriginal");
    
    form.classList.remove("hidden");
    origAliasInput.value = alias;
    
    if (alias && state.sshHosts[alias]) {
      const h = state.sshHosts[alias];
      title.textContent = `${t("miniapp.ssh.edit_prefix", "Редактировать")} ${alias}`;
      aliasInput.value = alias;
      aliasInput.disabled = true; // Cannot change alias of existing host easily
      document.getElementById("sshHostAddr").value = h.host || "";
      document.getElementById("sshHostPort").value = h.port || 22;
      document.getElementById("sshHostUser").value = h.user || "";
      document.getElementById("sshHostAuth").value = h.auth || "key";
      document.getElementById("sshHostKeyFile").value = h.key_file || "";
      document.getElementById("sshHostKeyPassEnv").value = h.key_passphrase_env || "";
      document.getElementById("sshHostPasswordEnv").value = h.password_env || "";
      document.getElementById("sshHostPassword").value = "";
      document.getElementById("sshHostSudo").checked = !!h.sudo;
      document.getElementById("sshHostSudoPassEnv").value = h.sudo_password_env || "";
      document.getElementById("sshHostSudoPassword").value = "";
      document.getElementById("sshHostRoles").value = (h.roles || []).join(",");
      document.getElementById("sshHostDesc").value = h.description || "";
      document.getElementById("sshHostRemoteProjectRoot").value = h.remote_project_root || "";
      document.getElementById("sshHostTimeout").value = h.idle_timeout_sec || 1200;
    } else {
      title.textContent = t("miniapp.ssh.add_host", "Добавить хост");
      aliasInput.value = "";
      aliasInput.disabled = false;
      document.getElementById("sshHostAddr").value = "";
      document.getElementById("sshHostPort").value = 22;
      document.getElementById("sshHostUser").value = "";
      document.getElementById("sshHostAuth").value = "key";
      document.getElementById("sshHostKeyFile").value = "";
      document.getElementById("sshHostKeyPassEnv").value = "";
      document.getElementById("sshHostPasswordEnv").value = "";
      document.getElementById("sshHostPassword").value = "";
      document.getElementById("sshHostSudo").checked = false;
      document.getElementById("sshHostSudoPassEnv").value = "";
      document.getElementById("sshHostSudoPassword").value = "";
      document.getElementById("sshHostRoles").value = "";
      document.getElementById("sshHostDesc").value = "";
      document.getElementById("sshHostRemoteProjectRoot").value = "";
      document.getElementById("sshHostTimeout").value = 1200;
    }
    
    toggleSshFormFields();
    form.scrollIntoView({ behavior: "smooth" });
  }

  function toggleSshFormFields() {
    const auth = document.getElementById("sshHostAuth").value;
    document.getElementById("sshHostKeyFields").classList.toggle("hidden", auth !== "key");
    document.getElementById("sshHostPasswordFields").classList.toggle("hidden", auth !== "password");
    
    const sudo = document.getElementById("sshHostSudo").checked;
    document.getElementById("sshHostSudoFields").classList.toggle("hidden", !sudo);
  }

  function closeSshHostForm() {
    document.getElementById("sshHostForm").classList.add("hidden");
  }

  async function saveSshHost() {
    const workdir = getSshWorkdir();
    if (!workdir) {
      alert(t("miniapp.ssh.no_workdir_save", "Не удалось определить каталог проекта для сохранения SSH host."));
      return;
    }
    const alias = document.getElementById("sshHostAlias").value.trim();
    const originalAlias = document.getElementById("sshHostAliasOriginal").value.trim();
    if (!alias) {
      alert(t("miniapp.ssh.alias_required", "Требуется псевдоним (alias)"));
      return;
    }

    const isUpdate = originalAlias && originalAlias === alias;
    const endpoint = isUpdate ? "/ssh/hosts/update" : "/ssh/hosts";

    const payload = {
      alias: alias,
      host: document.getElementById("sshHostAddr").value.trim(),
      port: parseInt(document.getElementById("sshHostPort").value) || 22,
      user: document.getElementById("sshHostUser").value.trim(),
      auth: document.getElementById("sshHostAuth").value,
      key_file: document.getElementById("sshHostKeyFile").value.trim(),
      key_passphrase_env: document.getElementById("sshHostKeyPassEnv").value.trim(),
      password_env: document.getElementById("sshHostPasswordEnv").value.trim(),
      password: document.getElementById("sshHostPassword").value,
      sudo: document.getElementById("sshHostSudo").checked,
      sudo_password_env: document.getElementById("sshHostSudoPassEnv").value.trim(),
      sudo_password: document.getElementById("sshHostSudoPassword").value,
      roles: document.getElementById("sshHostRoles").value.split(",").map(s => s.trim()).filter(Boolean),
      description: document.getElementById("sshHostDesc").value.trim(),
      remote_project_root: document.getElementById("sshHostRemoteProjectRoot").value.trim(),
      idle_timeout_sec: parseInt(document.getElementById("sshHostTimeout").value) || 1200,
    };

    try {
      const res = await api(`${endpoint}?workdir=${encodeURIComponent(workdir)}`, {
        method: "POST",
        body: JSON.stringify(payload)
      });
      if (res.ok) {
        tg.showScanResult?.(t("miniapp.ssh.host_saved", "Хост сохранен"));
        closeSshHostForm();
        await fetchSshHosts();
        await fetchSessionSettings();
      }
    } catch (err) {
      alert(`${t("miniapp.ssh.err_save_host", "Ошибка сохранения хоста:")} ${err.message}`);
    }
  }

  async function deleteSshHost(alias) {
    if (!confirm(`${t("miniapp.ssh.delete_confirm", "Удалить хост")} ${alias}?`)) return;
    const workdir = getSshWorkdir();
    if (!workdir) {
      alert(t("miniapp.ssh.no_workdir_delete", "Не удалось определить каталог проекта для удаления SSH host."));
      return;
    }
    try {
      const res = await api(`/ssh/hosts/delete?workdir=${encodeURIComponent(workdir)}`, {
        method: "POST",
        body: JSON.stringify({ alias })
      });
      if (res.ok) {
        await fetchSshHosts();
        await fetchSessionSettings();
      }
    } catch (err) {
      alert(`${t("miniapp.ssh.err_delete", "Ошибка удаления:")} ${err.message}`);
    }
  }

  async function testSshConnection(alias) {
    const workdir = getSshWorkdir();
    if (!workdir) {
      alert(t("miniapp.ssh.no_workdir_test", "Не удалось определить каталог проекта для SSH-проверки."));
      return;
    }
    try {
      const res = await api(`/ssh/test-connection?workdir=${encodeURIComponent(workdir)}`, {
        method: "POST",
        body: JSON.stringify({ alias })
      });
      if (res.ok) {
        alert(`${t("miniapp.ssh.test_ok", "Успешно:")} ${res.message}\n${res.server_info || ""}`);
      } else {
        alert(`${t("miniapp.error.generic_prefix", "Ошибка:")} ${res.message}`);
      }
    } catch (err) {
      alert(`${t("miniapp.ssh.err_request", "Ошибка запроса:")} ${err.message}`);
    }
  }

  async function generateSshKey(alias) {
    if (!confirm(`${t("miniapp.ssh.keygen_confirm", "Сгенерировать новый ключ для")} ${alias}? ${t("miniapp.ssh.keygen_warn", "Это перезапишет существующий в конфиге путь к ключу.")}`)) return;
    const workdir = getSshWorkdir();
    if (!workdir) {
      alert(t("miniapp.ssh.no_workdir_keygen", "Не удалось определить каталог проекта для генерации ключа."));
      return;
    }
    try {
      const res = await api(`/ssh/keygen?workdir=${encodeURIComponent(workdir)}`, {
        method: "POST",
        body: JSON.stringify({ alias })
      });
      if (res.ok) {
        // We might want to show the public key so user can add it to server
        const pub = res.public_key;
        alert(`${t("miniapp.ssh.keygen_success", "Ключ сгенерирован и прописан в конфиг.")}\n\n${t("miniapp.ssh.keygen_add_hint", "Добавьте этот публичный ключ в ~/.ssh/authorized_keys на сервере:")}\n\n${pub}`);
        await fetchSshHosts();
      }
    } catch (err) {
      alert(`${t("miniapp.ssh.err_keygen", "Ошибка генерации ключа:")} ${err.message}`);
    }
  }

  function compactLogLine(text) {
    return String(text || "")
      .replace(/\s+\[chat=[^\]]+\]/g, "");
  }

  function compactLogText(rawText) {
    const lines = String(rawText || "").split("\n");
    if (!lines.length) return "";
    lines[0] = compactLogLine(lines[0]);
    return lines.join("\n");
  }

  function shouldAutoScrollLogs() {
    const autoscrollCb = document.getElementById("logsAutoScroll");
    return !autoscrollCb || autoscrollCb.checked;
  }

  function syncLogsOutput() {
    const out = document.getElementById("logsOutput");
    if (!out) return null;
    out.textContent = state.logsEntries.join("\n");
    return out;
  }

  function appendLogsOutput(lines) {
    const out = document.getElementById("logsOutput");
    if (!out) return null;
    if (!lines.length) return out;
    const chunk = lines.join("\n");
    if (!chunk) return out;
    if (out.textContent) {
      out.append(document.createTextNode(`\n${chunk}`));
    } else {
      out.textContent = chunk;
    }
    return out;
  }

  function logEntryMatchesLevel(entryText, levelFilter) {
    const level = String(levelFilter || "").trim().toUpperCase();
    if (!level) return true;
    // Match Python-style log lines: "2024-01-01 12:00:00,000 LEVEL ..."
    // or "2024-01-01T12:00:00 LEVEL ..." etc.
    const match = /^\d{4}-\d{2}-\d{2}[\sT]\S+\s+(\w+)/.exec(String(entryText || ""));
    if (!match) return true; // Non-standard lines pass through
    return match[1].toUpperCase() === level;
  }

  function renderLogsEntries(entries, { replace = false } = {}) {
    if (replace) {
      state.logsEntries = [];
      state.logsEntryIds.clear();
    }
    const newNormalized = [];
    const levelFilter = String(state.logsLevel || "").trim().toUpperCase();
    (entries || []).forEach((entry) => {
      const eid = String(entry?.id || "");
      if (eid && state.logsEntryIds.has(eid)) {
        return; // Deduplicate
      }
      if (eid) {
        state.logsEntryIds.add(eid);
      }
      const text = compactLogText(entry?.text || "").trimEnd();
      if (!text) return;
      if (levelFilter && !logEntryMatchesLevel(text, levelFilter)) return;
      newNormalized.push(text);
    });

    if (!newNormalized.length && !replace) {
      return;
    }

    state.logsEntries.push(...newNormalized);

    const MAX_ENTRIES = 5000;
    if (state.logsEntries.length > MAX_ENTRIES) {
      state.logsEntries = state.logsEntries.slice(-MAX_ENTRIES);
      // Prune IDs cache periodically to prevent memory leaks,
      // though Set of 5000 strings is small.
      if (state.logsEntryIds.size > MAX_ENTRIES * 2) {
        // Keep only newest IDs (not perfect but good enough for UI)
        state.logsEntryIds = new Set(Array.from(state.logsEntryIds).slice(-MAX_ENTRIES));
      }
    }

    let out = null;
    if (replace || newNormalized.length > 20) {
      out = syncLogsOutput();
    } else {
      out = appendLogsOutput(newNormalized);
    }
    if (out) {
      if (shouldAutoScrollLogs()) {
        out.scrollTop = out.scrollHeight;
      }
    }
  }

  function clearLogsView() {
    state.logsEntries = [];
    state.logsEntryIds.clear();
    syncLogsOutput();
  }

  function openDownloadUrl(downloadUrl) {
    if (tg && typeof tg.openLink === "function") {
      tg.openLink(downloadUrl);
      return;
    }
    const a = document.createElement("a");
    a.href = downloadUrl;
    a.target = "_blank";
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  function downloadLogsView() {
    const type = document.getElementById("logsType");
    const history = document.getElementById("logsHistory");
    const session = document.getElementById("logsSession");
    const logType = String(type?.value || state.logsType || "main");
    const historyValue = Number(history?.value || state.logsHistory || 0);
    const selected = parseLogsSessionSelection(session ? session.value : state.logsSessionKey);
    const sessionUid = String(selected.sessionUid || "");
    const sessionId = String(selected.sessionId || "");
    const qs = new URLSearchParams({
      log_type: logType,
      history: String(historyValue),
    });
    if (sessionUid) {
      qs.set("session_uid", sessionUid);
    }
    if (sessionId) {
      qs.set("session_id", sessionId);
    }

    setLogsStatus(t("miniapp.logs.preparing", "Подготовка файла..."));
    api("/logs/ws_ticket")
      .then((payload) => {
        const ticket = String(payload.ticket || "");
        if (!ticket) {
          throw new Error("download ticket missing");
        }
        qs.set("ticket", ticket);
        const downloadUrl = new URL(`./api/logs/download?${qs.toString()}`, window.location.href).toString();
        openDownloadUrl(downloadUrl);
        setLogsStatus(t("miniapp.logs.download_started", "Скачивание запущено"));
      })
      .catch((err) => {
        setLogsStatus(`${t("miniapp.files.download_error_prefix", "Ошибка скачивания:")} ${err.message || "unknown"}`, false);
      });
  }

  function clearLogsReconnectTimer() {
    if (state.logsReconnectTimer) {
      clearTimeout(state.logsReconnectTimer);
      state.logsReconnectTimer = null;
    }
  }

  function clearStatusReconnectTimer() {
    if (state.statusReconnectTimer) {
      clearTimeout(state.statusReconnectTimer);
      state.statusReconnectTimer = null;
    }
  }

  function disconnectLogsWs({ manual = true } = {}) {
    if (manual) {
      state.logsShouldReconnect = false;
      state.logsReconnectAttempts = 0;
    }
    clearLogsReconnectTimer();
    if (state.logsSocket) {
      try {
        state.logsSocket.close();
      } catch {}
      state.logsSocket = null;
    }
  }

  function disconnectStatusWs({ manual = true } = {}) {
    if (manual) {
      state.statusShouldReconnect = false;
      state.statusReconnectAttempts = 0;
    }
    clearStatusReconnectTimer();
    if (state.statusSocket) {
      try {
        state.statusSocket.close();
      } catch {}
      state.statusSocket = null;
    }
  }

  function scheduleLogsReconnect() {
    if (!state.logsShouldReconnect) return;
    if (state.logsReconnectTimer) return;
    state.logsReconnectAttempts += 1;
    const delayMs = Math.min(10000, 1000 * (2 ** Math.max(0, state.logsReconnectAttempts - 1)));
    setLogsStatus(`${t("miniapp.logs.ws_disconnected_reconnect", "Поток логов отключен, переподключение через")} ${Math.round(delayMs / 1000)}с...`, false);
    state.logsReconnectTimer = setTimeout(() => {
      state.logsReconnectTimer = null;
      if (!state.logsShouldReconnect) return;
      connectLogsWs();
    }, delayMs);
  }

  function scheduleStatusReconnect() {
    if (!state.statusShouldReconnect) return;
    if (state.statusReconnectTimer) return;
    state.statusReconnectAttempts += 1;
    const delayMs = Math.min(10000, 1000 * (2 ** Math.max(0, state.statusReconnectAttempts - 1)));
    setStatus(`${t("miniapp.status.ws_disconnected_reconnect", "Поток статуса отключен, переподключение через")} ${Math.round(delayMs / 1000)}с...`);
    state.statusReconnectTimer = setTimeout(() => {
      state.statusReconnectTimer = null;
      if (!state.statusShouldReconnect) return;
      connectStatusWs();
    }, delayMs);
  }

  function buildLogsWsUrl(ticket) {
    const url = new URL("./api/logs/ws", window.location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    url.searchParams.set("ticket", String(ticket || ""));
    url.searchParams.set("log_type", state.logsType || "main");
    url.searchParams.set("history", String(state.logsHistory || 0));
    if (state.logsSessionUid) {
      url.searchParams.set("session_uid", state.logsSessionUid);
    }
    if (state.logsSessionId) {
      url.searchParams.set("session_id", state.logsSessionId);
    }
    return url.toString();
  }

  function buildStatusWsUrl(ticket) {
    const url = new URL("./api/status/ws", window.location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    url.searchParams.set("ticket", String(ticket || ""));
    if (state.statusSessionUid) {
      url.searchParams.set("session_uid", state.statusSessionUid);
    }
    return url.toString();
  }

  function boolText(value) {
    return value ? t("miniapp.status.bool_yes", "да") : t("miniapp.status.bool_no", "нет");
  }

  function ageText(seconds) {
    const sec = Number(seconds);
    if (!Number.isFinite(sec) || sec < 0) return t("miniapp.status.bool_no", "нет");
    if (sec < 60) return `${Math.floor(sec)}с`;
    const min = Math.floor(sec / 60);
    const rest = Math.floor(sec % 60);
    if (min < 60) return `${min}м ${rest}с`;
    const hr = Math.floor(min / 60);
    const minRest = min % 60;
    return `${hr}ч ${minRest}м`;
  }

  function serverTimeText(isoValue, epochValue) {
    const iso = String(isoValue || "").trim();
    if (iso) {
      const date = new Date(iso);
      if (!Number.isNaN(date.getTime())) {
        return date.toLocaleString("ru-RU");
      }
      return iso;
    }
    const epoch = Number(epochValue);
    if (Number.isFinite(epoch) && epoch > 0) {
      return new Date(epoch * 1000).toLocaleString("ru-RU");
    }
    return "-";
  }

  function statusValueText(value) {
    if (value === null || value === undefined) return "-";
    if (typeof value === "string") {
      const trimmed = value.trim();
      return trimmed || "-";
    }
    if (typeof value === "number" || typeof value === "boolean") {
      return String(value);
    }
    if (Array.isArray(value)) {
      if (!value.length) return "0";
      const preview = value.slice(0, 3).map((item) => statusValueText(item)).filter(Boolean);
      const suffix = value.length > 3 ? ` | +${value.length - 3}` : "";
      return `${value.length} item${value.length === 1 ? "" : "s"}${preview.length ? ` | ${preview.join(" | ")}` : ""}${suffix}`;
    }
    if (isPlainObject(value)) {
      const parts = Object.entries(value).slice(0, 5).map(([key, nested]) => {
        if (Array.isArray(nested)) return `${key}=${nested.length} items`;
        if (isPlainObject(nested)) return `${key}=...`;
        return `${key}=${statusValueText(nested)}`;
      });
      if (!parts.length) return "-";
      const suffix = Object.keys(value).length > 5 ? " | ..." : "";
      return `${parts.join(" | ")}${suffix}`;
    }
    return String(value);
  }

  function setAdminStatusMessage(text, ok = true) {
    const empty = document.getElementById("adminStatusMessage");
    const dashboard = document.getElementById("adminStatusDashboard");
    if (!empty || !dashboard) return;
    const value = String(text || "").trim();
    if (value) {
      empty.textContent = value;
      empty.className = ok ? "status-empty" : "status-error";
      empty.classList.remove("hidden");
      dashboard.classList.add("hidden");
      return;
    }
    empty.classList.add("hidden");
    dashboard.classList.remove("hidden");
  }

  function setAdminStatusBanner(text, ok = true) {
    const banner = document.getElementById("adminStatusBanner");
    if (!banner) return;
    const value = String(text || "").trim();
    if (!value) {
      banner.textContent = "";
      banner.classList.add("hidden");
      return;
    }
    banner.textContent = value;
    banner.className = ok ? "admin-status-banner status-empty" : "admin-status-banner status-error";
    banner.classList.remove("hidden");
  }

  function adminCacheKey(sessionUid) {
    return String(sessionUid || "").trim();
  }

  function setAdminCachedStatus(sessionUid, payload) {
    const key = adminCacheKey(sessionUid);
    if (!key || !payload || typeof payload !== "object") return;
    state.adminStatusCache[key] = {
      cachedAt: Date.now(),
      payload,
    };
  }

  function getAdminCachedStatus(sessionUid, { allowStale = false } = {}) {
    const key = adminCacheKey(sessionUid);
    if (!key) return null;
    const entry = state.adminStatusCache[key];
    if (!entry || !entry.payload) return null;
    const ageMs = Date.now() - Number(entry.cachedAt || 0);
    if (!allowStale && ageMs > ADMIN_STATUS_CACHE_TTL_MS) return null;
    return entry.payload;
  }

  function buildAdminStatusErrorMessage(err, { stale = false } = {}) {
    const status = Number(err?.status || 0);
    if (err?.name === "AbortError" || err?.code === "timeout") {
      return stale
        ? t("miniapp.admin.err_timeout_stale", "Таймаут запроса Admin status. Показаны последние известные данные.")
        : t("miniapp.admin.err_timeout", "Таймаут запроса Admin status. Повторите попытку.");
    }
    if (status === 401 || status === 403) {
      return stale
        ? t("miniapp.admin.err_access_stale", "Нет доступа к Admin status для выбранной сессии. Показаны последние известные данные.")
        : t("miniapp.admin.err_access", "Нет доступа к Admin status для выбранной сессии.");
    }
    if (status >= 500) {
      return stale
        ? t("miniapp.admin.err_server_stale", "Сервер Admin status временно недоступен. Показаны последние известные данные.")
        : t("miniapp.admin.err_server", "Сервер Admin status временно недоступен.");
    }
    const detail = String(err?.message || "unknown").trim();
    return stale
      ? t("miniapp.admin.err_load_stale", "Ошибка загрузки Admin-статуса: {detail}. Показаны последние известные данные.").replace("{detail}", detail || "unknown")
      : t("miniapp.admin.err_load", "Ошибка загрузки Admin-статуса: {detail}").replace("{detail}", detail || "unknown");
  }

  function setAdminStateVisibility({ active = false, disabled = false } = {}) {
    const activeState = document.getElementById("adminActiveState");
    const disabledState = document.getElementById("adminDisabledState");
    if (activeState) {
      activeState.classList.toggle("hidden", !active);
    }
    if (disabledState) {
      disabledState.classList.toggle("hidden", !disabled);
    }
  }

  function setAdminStructuredVisibility(visible) {
    const structuredState = document.getElementById("adminStructuredState");
    if (!structuredState) return;
    structuredState.classList.toggle("hidden", !visible);
    if (visible) {
      activateAdminSubtab(state.adminActiveSubtab || "overview");
    }
  }

  function activateAdminSubtab(tabName) {
    const requested = String(tabName || "overview").trim() || "overview";
    const buttons = Array.from(document.querySelectorAll("[data-admin-subtab]"));
    const panels = Array.from(document.querySelectorAll("[data-admin-subtab-panel]"));
    if (!buttons.length || !panels.length) return;
    const hasRequested = buttons.some((button) => String(button.dataset.adminSubtab || "") === requested);
    const activeTab = hasRequested ? requested : "overview";
    buttons.forEach((button) => {
      const isActive = String(button.dataset.adminSubtab || "") === activeTab;
      button.classList.toggle("active", isActive);
      button.setAttribute("aria-selected", isActive ? "true" : "false");
    });
    panels.forEach((panel) => {
      const isActive = String(panel.dataset.adminSubtabPanel || "") === activeTab;
      panel.classList.toggle("active", isActive);
      panel.classList.toggle("hidden", !isActive);
    });
    state.adminActiveSubtab = activeTab;
  }

  function bindAdminSubtabHandlers() {
    document.querySelectorAll("[data-admin-subtab]").forEach((button) => {
      button.onclick = () => {
        activateAdminSubtab(button.dataset.adminSubtab || "overview");
      };
    });
    activateAdminSubtab(state.adminActiveSubtab || "overview");
  }

  function isPlainObject(value) {
    return !!value && typeof value === "object" && !Array.isArray(value);
  }

  function adminBoolValue(value) {
    return value ? t("common.yes", "Да") : t("common.no", "Нет");
  }

  function adminTimeValue(value) {
    const num = Number(value);
    if (!Number.isFinite(num) || num <= 0) return "-";
    return serverTimeText("", num);
  }

  function adminCountValue(value) {
    const num = Number(value);
    if (!Number.isFinite(num) || num < 0) return 0;
    return Math.trunc(num);
  }

  function adminCompactText(value, maxLength = 56) {
    const text = String(value ?? "").trim();
    if (!text) return "";
    return text.length > maxLength ? `${text.slice(0, maxLength)}...` : text;
  }

  function adminPendingSummaryText(askUser, approvals) {
    const askPayload = isPlainObject(askUser) ? askUser : {};
    const approvalsPayload = isPlainObject(approvals) ? approvals : {};
    const askCount = adminCountValue(askPayload.count);
    const approvalCount = adminCountValue(approvalsPayload.count);
    const active = Boolean(askPayload.active || approvalsPayload.active);
    if (!active && askCount === 0 && approvalCount === 0) return t("miniapp.admin.no_pending", "нет ожидания");
    return `ask_user ${askCount} | approvals ${approvalCount}${active ? " | active" : ""}`;
  }

  function adminSkillInstallsSummaryText(payload, items) {
    const count = adminCountValue(payload?.count);
    const itemCount = Array.isArray(items) ? items.length : 0;
    const active = Boolean(payload?.active);
    const parts = [`${count} pending`];
    if (itemCount) parts.push(`${itemCount} listed`);
    if (active) parts.push("active");
    return parts.join(" | ");
  }

  function adminMuteSummaryText(payload) {
    const mutePayload = isPlainObject(payload) ? payload : {};
    if (!mutePayload.muted) return "off";
    const until = adminTimeValue(mutePayload.muted_until_ts);
    return until === "-" ? "muted" : `muted until ${until}`;
  }

  function adminRowSummaryText(rows, idKeys) {
    const items = Array.isArray(rows) ? rows : [];
    if (!items.length) return "0";
    const first = isPlainObject(items[0]) ? items[0] : {};
    const payload = isPlainObject(first.payload) ? first.payload : {};
    const decision = isPlainObject(payload.decision) ? payload.decision : {};
    const id = idKeys
      .map((key) => String(first[key] || ""))
      .find((value) => value.trim());
    const label = (
      decision.incident_type
      || decision.diagnosis
      || decision.action
      || first.title
      || first.action
      || payload.event
      || ""
    );
    const suffix = label ? ` | ${adminCompactText(label)}` : "";
    return `${items.length}${id ? ` | ${adminCompactText(id, 42)}` : ""}${suffix}`;
  }

  function adminIsEmptyValue(value) {
    if (value === null || value === undefined) return true;
    if (typeof value === "string") return value.trim() === "";
    if (Array.isArray(value)) return value.length === 0;
    if (isPlainObject(value)) return Object.keys(value).length === 0;
    return false;
  }

  function adminScalarHtml(value) {
    return `<span class="admin-inline-value">${escapeHtml(statusValueText(value))}</span>`;
  }

  function adminObjectHtml(value, depth) {
    const entries = Object.entries(value || {});
    if (!entries.length) {
      return `<div class="admin-empty-note">${escapeHtml(t("miniapp.status.no_data_short", "Нет данных"))}</div>`;
    }
    const simpleEntries = entries.every(
      ([, nested]) => !Array.isArray(nested) && !isPlainObject(nested),
    );
    if (simpleEntries) {
      const rows = entries.map(([key, nested]) => `
        <div class="kv-row">
          <span class="kv-key">${escapeHtml(String(key))}</span>
          <span class="kv-val">${escapeHtml(statusValueText(nested))}</span>
        </div>
      `);
      return `<div class="kv-list">${rows.join("")}</div>`;
    }
    const rows = entries.map(([key, nested]) => `
      <div class="admin-object-row">
        <div class="admin-object-key">${escapeHtml(String(key))}</div>
        <div class="admin-object-value">${adminStructuredValueHtml(nested, { depth: depth + 1 })}</div>
      </div>
    `);
    return `<div class="admin-object-grid">${rows.join("")}</div>`;
  }

  function adminArrayHtml(value, depth) {
    const items = Array.isArray(value) ? value : [];
    if (!items.length) {
      return `<div class="admin-empty-note">${escapeHtml(t("miniapp.status.no_data_short", "Нет данных"))}</div>`;
    }
    const rows = items.map((item) => `
      <div class="admin-stack-item">
        ${adminStructuredValueHtml(item, { depth: depth + 1 })}
      </div>
    `);
    return `<div class="admin-stack-list">${rows.join("")}</div>`;
  }

  function adminStructuredValueHtml(value, { depth = 0 } = {}) {
    if (adminIsEmptyValue(value)) {
      return `<div class="admin-empty-note">${escapeHtml(t("miniapp.status.no_data_short", "Нет данных"))}</div>`;
    }
    if (Array.isArray(value)) {
      return adminArrayHtml(value, depth);
    }
    if (isPlainObject(value)) {
      return adminObjectHtml(value, depth);
    }
    return adminScalarHtml(value);
  }

  function setAdminDetailHtml(detailsId, bodyId, value) {
    setStatusAccordionHtml(
      detailsId,
      bodyId,
      adminStructuredValueHtml(value),
    );
  }

  function renderAdminSessionOptions(payload) {
    ensureAdminPanelLayout();
    const select = document.getElementById("adminSession");
    if (!select) return;
    const sessions = Array.isArray(payload?.available_sessions) ? payload.available_sessions : [];
    const signatureParts = sessions.map((item) => {
      const uid = String(item?.session_uid || "");
      const label = String(item?.label || uid);
      return `${uid}\u0000${label}`;
    });
    const nextSignature = signatureParts.join("\u0001");
    const previousSelected = String(state.adminSessionUid || "");
    const fallbackSelected = String(payload?.selected_session_uid || "");
    const desiredSelected = previousSelected || fallbackSelected;
    if (!select.options.length || state.adminSessionsSignature !== nextSignature) {
      const options = sessions
        .map((item) => `<option value="${escapeHtml(item.session_uid)}">${escapeHtml(item.label || item.session_uid)}</option>`)
        .join("");
      select.innerHTML = `<option value="">${escapeHtml(t("miniapp.label.choose_session", "Выберите сессию"))}</option>${options}`;
      if (desiredSelected) {
        select.value = desiredSelected;
      }
      if (select.value !== desiredSelected && fallbackSelected) {
        select.value = fallbackSelected;
      }
      state.adminSessionsSignature = nextSignature;
    }
    state.adminSessionUid = String(select.value || "");
    const cachedPayload = getAdminCachedStatus(state.adminSessionUid);
    if (cachedPayload) {
      renderAdminPayload(cachedPayload);
    }
    if (isAdminTabActive() && state.adminSessionUid && state.adminSessionUid !== previousSelected) {
      void fetchAdminStatus();
    }
  }

  function renderAdminPayload(payload) {
    ensureAdminPanelLayout();
    state.adminLastPayload = payload || null;
    if (!payload || typeof payload !== "object") {
      setAdminStateVisibility({});
      setAdminStructuredVisibility(false);
      setAdminStatusMessage(t("miniapp.admin.status_unavailable", "Admin status недоступен"), false);
      const raw = document.getElementById("adminStatusJson");
      if (raw) raw.textContent = "";
      return;
    }
    const monitorReadiness = document.getElementById("adminMonitorReadiness");
    const monitorReadinessHint = document.getElementById("adminMonitorReadinessHint");
    const analyzerAction = document.getElementById("adminLastAnalyzerAction");
    const analyzerActionHint = document.getElementById("adminLastAnalyzerActionHint");
    const pipeline = document.getElementById("adminPipelineStatus");
    const monitorStatus = document.getElementById("adminMonitorStatus");
    const runtimeFlags = document.getElementById("adminRuntimeFlags");
    const pinnedCli = document.getElementById("adminPinnedCli");
    const pinnedExecutorProfile = document.getElementById("adminPinnedExecutorProfile");
    const scanStatus = document.getElementById("adminScanStatus");
    const scanDetails = document.getElementById("adminScanDetails");
    const readiness = document.getElementById("adminReadiness");
    const analyzer = document.getElementById("adminAnalyzerStatus");
    const analyzerMessage = document.getElementById("adminAnalyzerMessage");
    const lastDecision = document.getElementById("adminLastDecision");
    const executor = document.getElementById("adminExecutorStatus");
    const executorMessage = document.getElementById("adminExecutorMessage");
    const lastAction = document.getElementById("adminLastAction");
    const notifierStatus = document.getElementById("adminNotifierStatus");
    const notifierMessage = document.getElementById("adminNotifierMessage");
    const pendingState = document.getElementById("adminPendingState");
    const pendingSkillInstalls = document.getElementById("adminPendingSkillInstalls");
    const muteState = document.getElementById("adminMuteState");
    const recentIncidents = document.getElementById("adminRecentIncidents");
    const recentActions = document.getElementById("adminRecentActions");
    const approvedOverrides = document.getElementById("adminApprovedOverrides");
    const skillApprovalSelect = document.getElementById("adminSkillApprovalSelect");
    const skillApprovalApprove = document.getElementById("adminSkillApprovalApprove");
    const skillApprovalReject = document.getElementById("adminSkillApprovalReject");
    const disabledHint = document.getElementById("adminDisabledHint");
    const raw = document.getElementById("adminStatusJson");
    const pipelineStatus = String(payload.pipeline_status || "").trim();
    const monitorStatusValue = String(payload.monitor_status || "").trim();
    const analyzerStatus = String(payload.analyzer_status || "").trim();
    const executorStatus = String(payload.executor_status || "").trim();
    const notifierStatusValue = String(payload.notifier_status || "").trim();
    const analyzerText = String(payload.analyzer_message || "").trim();
    const hasFailure = [pipelineStatus, analyzerStatus, executorStatus].includes("failed");
    const runtimeBits = [
      payload.mode_tasks_running ? "mode tasks running" : "mode tasks idle",
      payload.tick_active ? "tick active" : "tick idle",
      payload.run_lock_locked ? "run lock locked" : "run lock free",
      payload.busy ? "session busy" : "session idle",
    ];
    const isActive = payload.active !== false;
    let readinessText = "Ready";
    let readinessTone = "admin-state-pill-ok";
    let readinessHintText = t("miniapp.admin.readiness_hint_ok", "Pipeline готов к следующему monitor-cycle.");

    if (hasFailure) {
      readinessText = "Needs attention";
      readinessTone = "admin-state-pill-danger";
      readinessHintText = t("miniapp.admin.readiness_hint_failure", "В пайплайне есть failure-state. Проверьте Analyzer и Executor.");
    } else if (pipelineStatus === "running" || payload.mode_tasks_running) {
      readinessText = "Live";
      readinessTone = "admin-state-pill-ok";
      readinessHintText = t("miniapp.admin.readiness_hint_live", "Monitor-cycle активен, данные приходят из текущего pipeline-run.");
    } else if (pipelineStatus === "disabled") {
      readinessText = "Disabled";
      readinessTone = "admin-state-pill-neutral";
      readinessHintText = t("miniapp.admin.readiness_hint_disabled", "Пайплайн выключен и не выполняет monitor-checks.");
    }

    if (pipeline) pipeline.textContent = statusValueText(payload.pipeline_status);
    if (monitorStatus) monitorStatus.textContent = statusValueText(payload.monitor_status);
    if (runtimeFlags) runtimeFlags.textContent = runtimeBits.join(" | ");
    if (pinnedCli) pinnedCli.textContent = statusValueText(payload.pinned_cli);
    if (pinnedExecutorProfile) pinnedExecutorProfile.textContent = statusValueText(payload.pinned_executor_profile);
    if (scanStatus) scanStatus.textContent = statusValueText(payload.scan_status);
    if (scanDetails) {
      const detailParts = [];
      if (payload.initialized_at) detailParts.push(`initialized=${serverTimeText("", payload.initialized_at)}`);
      if (payload.last_scan_at) detailParts.push(`last_scan=${serverTimeText("", payload.last_scan_at)}`);
      if (payload.scan_error) detailParts.push(`error=${payload.scan_error}`);
      scanDetails.textContent = detailParts.join(" | ") || "-";
    }
    if (readiness) readiness.textContent = statusValueText(payload.component_readiness);
    if (analyzer) analyzer.textContent = statusValueText(payload.analyzer_status);
    if (analyzerMessage) analyzerMessage.textContent = statusValueText(payload.analyzer_message);
    if (lastDecision) lastDecision.textContent = statusValueText(payload.last_analyzer_decision);
    if (executor) executor.textContent = statusValueText(payload.executor_status);
    if (executorMessage) executorMessage.textContent = statusValueText(payload.executor_message);
    if (lastAction) lastAction.textContent = statusValueText(payload.last_action);
    if (notifierStatus) notifierStatus.textContent = statusValueText(payload.notifier_status);
    if (notifierMessage) notifierMessage.textContent = statusValueText(payload.notifier_message);
    if (pendingState) {
      pendingState.textContent = adminPendingSummaryText(payload.pending_ask_user, payload.pending_approvals);
    }
    const pendingSkillPayload = isPlainObject(payload.pending_skill_installs) ? payload.pending_skill_installs : {};
    const pendingSkillItems = Array.isArray(pendingSkillPayload.items) ? pendingSkillPayload.items : [];
    if (pendingSkillInstalls) {
      pendingSkillInstalls.textContent = adminSkillInstallsSummaryText(pendingSkillPayload, pendingSkillItems);
    }
    if (skillApprovalSelect) {
      const previous = String(skillApprovalSelect.value || "");
      const options = [`<option value="">${escapeHtml(t("miniapp.admin.choose_skill", "Выберите pending skill install"))}</option>`].concat(
        pendingSkillItems.map((item) => {
          const approvalId = String(item?.approval_id || "");
          const skillId = String(item?.skill_id || "skill");
          const modePhase = [String(item?.mode_id || ""), String(item?.phase || "")]
            .filter((token) => token.trim())
            .join("/");
          const label = [approvalId, skillId, modePhase].filter((token) => token.trim()).join(" | ");
          return `<option value="${escapeHtml(approvalId)}">${escapeHtml(label)}</option>`;
        }),
      );
      skillApprovalSelect.innerHTML = options.join("");
      if (previous && pendingSkillItems.some((item) => String(item?.approval_id || "") === previous)) {
        skillApprovalSelect.value = previous;
      } else if (pendingSkillItems.length === 1) {
        skillApprovalSelect.value = String(pendingSkillItems[0]?.approval_id || "");
      }
    }
    if (muteState) muteState.textContent = adminMuteSummaryText(payload.mute_state);
    if (recentIncidents) recentIncidents.textContent = adminRowSummaryText(payload.recent_incidents, ["incident_id", "id"]);
    if (recentActions) recentActions.textContent = adminRowSummaryText(payload.recent_admin_actions, ["action_id", "id"]);
    if (approvedOverrides) approvedOverrides.textContent = adminRowSummaryText(payload.approved_overrides, ["override_id", "id"]);
    if (monitorReadiness) {
      monitorReadiness.textContent = readinessText;
      monitorReadiness.className = `admin-state-pill ${readinessTone}`;
    }
    if (monitorReadinessHint) {
      monitorReadinessHint.textContent = readinessHintText;
    }
    if (analyzerAction) {
      analyzerAction.textContent = analyzerText || statusValueText(payload.analyzer_status);
    }
    if (analyzerActionHint) {
      analyzerActionHint.textContent = analyzerStatus
        ? `Analyzer status: ${analyzerStatus}`
        : t("miniapp.admin.analyzer_no_action", "Analyzer ещё не публиковал подробное действие.");
    }

    setAdminDetailHtml("adminRuntimeDetails", "adminRuntimeBody", {
      mode: statusValueText(payload.mode || "admin"),
      session_uid: statusValueText(payload.session_uid),
      session_id: statusValueText(payload.session_id),
      active: adminBoolValue(isActive),
      pipeline_status: statusValueText(payload.pipeline_status),
      monitor_status: statusValueText(payload.monitor_status),
      analyzer_status: statusValueText(payload.analyzer_status),
      executor_status: statusValueText(payload.executor_status),
      notifier_status: statusValueText(payload.notifier_status),
      busy: adminBoolValue(payload.busy),
      run_lock_locked: adminBoolValue(payload.run_lock_locked),
      tick_active: adminBoolValue(payload.tick_active),
      mode_tasks_running: adminBoolValue(payload.mode_tasks_running),
      initialized_at: adminTimeValue(payload.initialized_at),
      last_scan_at: adminTimeValue(payload.last_scan_at),
      scan_status: statusValueText(payload.scan_status),
      scan_error: statusValueText(payload.scan_error),
      pinned_executor_profile: statusValueText(payload.pinned_executor_profile),
      pinned_cli: payload.pinned_cli,
    });
    setAdminDetailHtml("adminReadinessDetails", "adminReadinessBody", {
      monitor_readiness: readinessText,
      readiness_hint: readinessHintText,
      component_readiness: {
        monitor: adminBoolValue(payload.component_readiness?.monitor),
        analyzer: adminBoolValue(payload.component_readiness?.analyzer),
        executor: adminBoolValue(payload.component_readiness?.executor),
        notifier: adminBoolValue(payload.component_readiness?.notifier),
      },
      monitor_status: statusValueText(payload.monitor_status),
      notifier_status: statusValueText(payload.notifier_status),
    });
    setAdminDetailHtml("adminEnvironmentDetails", "adminEnvironmentBody", {
      environment_services: payload.environment_services,
      environment_stack_facts: payload.environment_stack_facts,
      pinned_cli: payload.pinned_cli,
    });
    setAdminDetailHtml("adminOperatorDetails", "adminOperatorBody", {
      pending_ask_user: {
        count: statusValueText(payload.pending_ask_user?.count ?? 0),
        active: adminBoolValue(payload.pending_ask_user?.active),
        current: payload.pending_ask_user?.current || {},
      },
      pending_approvals: {
        count: statusValueText(payload.pending_approvals?.count ?? 0),
        active: adminBoolValue(payload.pending_approvals?.active),
      },
      pending_skill_installs: {
        count: statusValueText(pendingSkillPayload.count ?? 0),
        active: adminBoolValue(pendingSkillPayload.active),
        items: pendingSkillItems,
      },
      mute_state: {
        muted: adminBoolValue(payload.mute_state?.muted),
        muted_until_ts: adminTimeValue(payload.mute_state?.muted_until_ts),
      },
    });
    setAdminDetailHtml("adminDecisionDetails", "adminDecisionBody", {
      analyzer_message: statusValueText(payload.analyzer_message),
      executor_message: statusValueText(payload.executor_message),
      notifier_message: statusValueText(payload.notifier_message),
      last_monitor_snapshot: payload.last_monitor_snapshot,
      last_analyzer_decision: payload.last_analyzer_decision,
      last_action: payload.last_action,
    });
    setAdminDetailHtml("adminHistoryDetails", "adminHistoryBody", {
      recent_incidents: payload.recent_incidents,
      recent_admin_actions: payload.recent_admin_actions,
      approved_overrides: payload.approved_overrides,
    });
    if (raw) raw.textContent = JSON.stringify(payload, null, 2);
    [
      document.getElementById("adminEnableAction"),
      document.getElementById("adminEnableActionActive"),
    ].forEach((button) => {
      if (button) button.disabled = isActive || state.adminRequestInFlight;
    });
    [
      document.getElementById("adminDisableAction"),
      document.getElementById("adminDisableActionActive"),
    ].forEach((button) => {
      if (button) button.disabled = !isActive || state.adminRequestInFlight;
    });
    [
      document.getElementById("adminRescanAction"),
      document.getElementById("adminRescanActionActive"),
    ].forEach((button) => {
      if (button) button.disabled = state.adminRequestInFlight;
    });
    const selectedApprovalId = String(skillApprovalSelect?.value || "");
    const skillApprovalDisabled = state.adminRequestInFlight || !isActive || !selectedApprovalId;
    if (skillApprovalSelect) {
      skillApprovalSelect.disabled = state.adminRequestInFlight || pendingSkillItems.length === 0;
    }
    if (skillApprovalApprove) {
      skillApprovalApprove.disabled = skillApprovalDisabled;
    }
    if (skillApprovalReject) {
      skillApprovalReject.disabled = skillApprovalDisabled;
    }
    setAdminStructuredVisibility(true);
    if (payload.active === false) {
      if (disabledHint) {
        disabledHint.textContent = t("miniapp.admin.disabled_hint", "Для выбранной сессии Admin-режим выключен. Нажмите Enable, чтобы включить режим, Disable недоступен до включения, либо запустите Rescan.");
      }
      setAdminStateVisibility({ disabled: true });
      setAdminStatusMessage("");
      return;
    }
    setAdminStateVisibility({ active: true });
    setAdminStatusMessage("");
    syncAdminChatFromPayload(payload);
  }

  function clearAdminPollTimer() {
    if (state.adminPollTimer) {
      clearInterval(state.adminPollTimer);
      state.adminPollTimer = null;
    }
  }

  async function fetchAdminStatus() {
    ensureAdminPanelLayout();
    applyAdminStateFromControls();
    const selected = String(
      state.adminSessionUid || state.statusSessionUid || state.statusLastPayload?.selected_session_uid || ""
    );
    const cachedPayload = getAdminCachedStatus(selected);
    if (!state.me?.is_admin) return cachedPayload;
    if (state.adminRequestInFlight) return cachedPayload;
    if (!selected) {
      setAdminStatusBanner("");
      setAdminStateVisibility({});
      setAdminStructuredVisibility(false);
      setAdminStatusMessage(t("miniapp.status.no_session", "Сессия не выбрана"));
      return null;
    }
    if (cachedPayload) {
      renderAdminPayload(cachedPayload);
      setAdminStatusBanner("");
      return cachedPayload;
    }
    const qs = new URLSearchParams({ session_uid: selected });
    state.adminRequestInFlight = true;
    try {
      const controller = typeof AbortController === "function" ? new AbortController() : null;
      const timeoutId = controller
        ? setTimeout(() => {
          controller.abort();
        }, ADMIN_STATUS_TIMEOUT_MS)
        : null;
      let payload = null;
      try {
        payload = await api(`/v1/admin/status?${qs.toString()}`, controller ? { signal: controller.signal } : {});
      } finally {
        if (timeoutId !== null) {
          clearTimeout(timeoutId);
        }
      }
      setAdminCachedStatus(selected, payload || {});
      setAdminStatusBanner("");
      renderAdminPayload(payload || {});
      return payload || {};
    } catch (err) {
      const stalePayload = getAdminCachedStatus(selected, { allowStale: true });
      if (stalePayload) {
        renderAdminPayload(stalePayload);
        setAdminStatusBanner(buildAdminStatusErrorMessage(err, { stale: true }), false);
        return stalePayload;
      }
      setAdminStatusBanner("");
      setAdminStateVisibility({});
      setAdminStructuredVisibility(false);
      setAdminStatusMessage(buildAdminStatusErrorMessage(err, { stale: false }), false);
      const raw = document.getElementById("adminStatusJson");
      if (raw) raw.textContent = "";
      return null;
    } finally {
      state.adminRequestInFlight = false;
      if (state.adminLastPayload && String(state.adminLastPayload.session_uid || "") === selected) {
        renderAdminPayload(state.adminLastPayload);
      }
    }
  }

  function startAdminPolling() {
    if (!state.me?.is_admin) return;
    ensureAdminPanelLayout();
    if (!state.adminPollTimer) {
      state.adminPollTimer = setInterval(() => {
        void fetchAdminStatus();
      }, ADMIN_POLL_INTERVAL_MS);
    }
    void fetchAdminStatus();
    void refreshAutonomy();
  }

  function stopAdminPolling() {
    clearAdminPollTimer();
  }

  function syncAdminChatFromPayload(payload) {
    if (!payload || typeof payload !== "object") return;
    const chat = payload.chat || {};
    const next = {
      messages_count: Number(chat.messages_count ?? -1),
      pending_count: Number(chat.pending_count ?? -1),
      last_message_ts: String(chat.last_message_ts || ""),
    };
    const prev = state.adminChatCounters || { messages_count: -1, pending_count: -1, last_message_ts: "" };
    const messagesChanged = (
      next.messages_count !== prev.messages_count
      || next.last_message_ts !== prev.last_message_ts
    );
    const pendingChanged = next.pending_count !== prev.pending_count;
    state.adminChatCounters = next;
    if (messagesChanged) void fetchAdminChatMessages();
    if (pendingChanged) void fetchAdminChatPending();
  }

  function adminChatSessionUid() {
    return String(
      state.adminSessionUid || state.statusSessionUid || state.statusLastPayload?.selected_session_uid || ""
    );
  }

  function setAdminChatStatus(text) {
    const el = document.getElementById("adminChatStatus");
    if (el) el.textContent = String(text || "");
  }

  function renderAdminChatMessages() {
    const container = document.getElementById("adminChatMessages");
    if (!container) return;
    const list = Array.isArray(state.adminChatMessages) ? state.adminChatMessages : [];
    container.innerHTML = "";
    list.forEach((entry) => {
      if (!entry || typeof entry !== "object") return;
      const row = document.createElement("div");
      row.className = "admin-chat-message admin-chat-message-" + String(entry.role || "user");
      const intentType = String(entry.intent_type || "");
      if (intentType === "intent_autopilot_executed") {
        row.classList.add("admin-chat-message-autopilot-executed");
      } else if (intentType === "intent_autopilot_blocked") {
        row.classList.add("admin-chat-message-autopilot-blocked");
      }
      const meta = document.createElement("div");
      meta.className = "admin-chat-message-meta";
      const ts = String(entry.ts || "");
      const role = String(entry.role || "user");
      let metaText = ts ? `[${ts}] ${role}` : role;
      if (intentType === "intent_autopilot_executed") metaText += " · autopilot ✓";
      else if (intentType === "intent_autopilot_blocked") metaText += " · autopilot ⚠";
      meta.textContent = metaText;
      row.appendChild(meta);
      const body = document.createElement("div");
      body.className = "admin-chat-message-body";
      body.textContent = String(entry.text || "");
      row.appendChild(body);
      container.appendChild(row);
    });
    container.scrollTop = container.scrollHeight;
  }

  function renderAdminChatPending() {
    const label = document.getElementById("adminChatPendingLabel");
    const list = document.getElementById("adminChatPendingList");
    const items = Array.isArray(state.adminChatPending) ? state.adminChatPending : [];
    if (label) label.textContent = t("miniapp.admin.pending_approvals", "Подтверждения ({n}):").replace("{n}", String(items.length));
    if (!list) return;
    list.innerHTML = "";
    items.forEach((item) => {
      if (!item || typeof item !== "object") return;
      const approvalId = String(item.approval_id || "");
      const intent = item.intent && typeof item.intent === "object" ? item.intent : {};
      const intentType = String(intent.type || "?");
      const actionId = String(intent.action_id || "");
      const target = String(intent.target || "");
      const row = document.createElement("div");
      row.className = "admin-chat-pending-row";
      if (state.adminChatSelectedApproval === approvalId) {
        row.classList.add("admin-chat-pending-row-selected");
      }
      const label = document.createElement("div");
      label.className = "admin-chat-pending-row-label";
      const parts = [approvalId, intentType];
      if (actionId) parts.push(actionId);
      if (target) parts.push(`→ ${target}`);
      label.textContent = parts.join(" · ");
      label.onclick = () => {
        state.adminChatSelectedApproval = approvalId;
        renderAdminChatPending();
      };
      row.appendChild(label);
      const blockedReason = item.autopilot_blocked ? String(item.autopilot_blocked) : "";
      if (blockedReason) {
        const blocked = document.createElement("div");
        blocked.className = "admin-chat-autopilot-blocked";
        blocked.textContent = `auto-exec blocked: ${blockedReason}`;
        row.appendChild(blocked);
      }
      const actions = document.createElement("div");
      actions.className = "admin-chat-pending-row-actions";
      const approveBtn = document.createElement("button");
      approveBtn.type = "button";
      approveBtn.textContent = t("miniapp.admin.approve", "Одобрить");
      approveBtn.disabled = state.adminChatApproveInFlight;
      approveBtn.onclick = () => {
        state.adminChatSelectedApproval = approvalId;
        void approveAdminChatPending(approvalId);
      };
      actions.appendChild(approveBtn);
      const rejectBtn = document.createElement("button");
      rejectBtn.type = "button";
      rejectBtn.textContent = t("miniapp.admin.reject", "Отклонить");
      rejectBtn.onclick = () => {
        state.adminChatSelectedApproval = approvalId;
        void rejectAdminChatPending(approvalId);
      };
      actions.appendChild(rejectBtn);
      row.appendChild(actions);
      list.appendChild(row);
    });
  }

  async function fetchAdminChatMessages() {
    const sessionUid = adminChatSessionUid();
    if (!sessionUid) return;
    try {
      const qs = new URLSearchParams({ session_uid: sessionUid });
      const resp = await api(`/v1/admin/chat/messages?${qs.toString()}`);
      if (resp && resp.ok) {
        state.adminChatMessages = Array.isArray(resp.messages) ? resp.messages : [];
        renderAdminChatMessages();
      }
    } catch (err) {
      setAdminChatStatus(`${t("miniapp.admin.err_load_messages", "Ошибка загрузки сообщений:")} ${err.message || err}`);
    }
  }

  async function fetchAdminChatPending() {
    const sessionUid = adminChatSessionUid();
    if (!sessionUid) return;
    try {
      const qs = new URLSearchParams({ session_uid: sessionUid });
      const resp = await api(`/v1/admin/chat/pending?${qs.toString()}`);
      if (resp && resp.ok) {
        state.adminChatPending = Array.isArray(resp.items) ? resp.items : [];
        renderAdminChatPending();
      }
    } catch (err) {
      setAdminChatStatus(`${t("miniapp.admin.err_load_pending", "Ошибка загрузки pending:")} ${err.message || err}`);
    }
  }

  async function fetchAdminChatMemory() {
    const sessionUid = adminChatSessionUid();
    if (!sessionUid) return;
    try {
      const qs = new URLSearchParams({ session_uid: sessionUid });
      const resp = await api(`/v1/admin/chat/memory?${qs.toString()}`);
      if (resp && resp.ok) {
        const textarea = document.getElementById("adminChatMemory");
        if (textarea && !state.adminChatMemoryDirty) {
          textarea.value = String(resp.text || "");
        }
      }
    } catch (err) {
      setAdminChatStatus(`${t("miniapp.admin.err_load_memory", "Ошибка загрузки memory:")} ${err.message || err}`);
    }
  }

  async function postAdminChatMessage() {
    const sessionUid = adminChatSessionUid();
    if (!sessionUid) { setAdminChatStatus(t("miniapp.label.choose_session", "Выберите сессию")); return; }
    const input = document.getElementById("adminChatInput");
    const text = input ? String(input.value || "").trim() : "";
    if (!text) return;
    if (state.adminChatSendInFlight) return;
    state.adminChatSendInFlight = true;
    if (input) input.value = "";
    setAdminChatStatus(t("miniapp.admin.chat_sending", "Отправка…"));
    try {
      const resp = await api("/v1/admin/chat/messages", {
        method: "POST",
        body: JSON.stringify({ session_uid: sessionUid, text }),
      });
      if (!resp || !resp.ok) {
        setAdminChatStatus(`${t("miniapp.error.generic_prefix", "Ошибка:")} ${resp && resp.error ? resp.error : "unknown"}`);
      } else {
        const intent = resp.intent && typeof resp.intent === "object" ? resp.intent : {};
        const intentType = String(intent.type || "");
        if (intentType && intentType !== "answer") {
          setAdminChatStatus(`${t("miniapp.admin.chat_intent", "Намерение:")} ${intentType}`);
        } else {
          setAdminChatStatus("");
        }
      }
      await fetchAdminChatMessages();
      await fetchAdminChatPending();
    } catch (err) {
      setAdminChatStatus(`${t("miniapp.admin.err_chat_send", "Ошибка отправки:")} ${err.message || err}`);
    } finally {
      state.adminChatSendInFlight = false;
    }
  }

  async function approveAdminChatPending(approvalId) {
    const sessionUid = adminChatSessionUid();
    if (!sessionUid || !approvalId) return;
    if (state.adminChatApproveInFlight) return;
    state.adminChatApproveInFlight = true;
    setAdminChatStatus(`Approving ${approvalId}…`);
    renderAdminChatPending();
    try {
      const resp = await api(`/v1/admin/chat/pending/${encodeURIComponent(approvalId)}/approve`, {
        method: "POST",
        body: JSON.stringify({ session_uid: sessionUid }),
      });
      if (!resp || !resp.ok) {
        setAdminChatStatus(`${t("miniapp.admin.err_approve", "Approve ошибка:")} ${resp && resp.error ? resp.error : "unknown"}`);
      } else {
        const targetKind = String(resp.target_kind || "?");
        const parts = [`Executed (${targetKind})`];
        if (resp.exit_code !== undefined && resp.exit_code !== null) parts.push(`exit=${resp.exit_code}`);
        if (resp.duration_ms !== undefined && resp.duration_ms !== null) parts.push(`${resp.duration_ms}ms`);
        setAdminChatStatus(parts.join(" · "));
      }
      await fetchAdminChatMessages();
      await fetchAdminChatPending();
    } catch (err) {
      setAdminChatStatus(`${t("miniapp.admin.err_approve_req", "Ошибка approve:")} ${err.message || err}`);
    } finally {
      state.adminChatApproveInFlight = false;
      renderAdminChatPending();
    }
  }

  async function rejectAdminChatPending(approvalId) {
    const sessionUid = adminChatSessionUid();
    if (!sessionUid || !approvalId) return;
    try {
      const resp = await api(`/v1/admin/chat/pending/${encodeURIComponent(approvalId)}/reject`, {
        method: "POST",
        body: JSON.stringify({ session_uid: sessionUid }),
      });
      if (resp && resp.ok) {
        setAdminChatStatus(`Rejected ${approvalId}`);
      } else {
        setAdminChatStatus(`${t("miniapp.admin.err_reject", "Reject ошибка:")} ${resp && resp.error ? resp.error : "unknown"}`);
      }
      await fetchAdminChatPending();
    } catch (err) {
      setAdminChatStatus(`${t("miniapp.admin.err_reject_req", "Ошибка reject:")} ${err.message || err}`);
    }
  }

  async function saveAdminChatMemory() {
    const sessionUid = adminChatSessionUid();
    if (!sessionUid) { setAdminChatStatus(t("miniapp.label.choose_session", "Выберите сессию")); return; }
    const textarea = document.getElementById("adminChatMemory");
    const text = textarea ? String(textarea.value || "") : "";
    try {
      const resp = await api("/v1/admin/chat/memory", {
        method: "PUT",
        body: JSON.stringify({ session_uid: sessionUid, text }),
      });
      if (resp && resp.ok) {
        state.adminChatMemoryDirty = false;
        setAdminChatStatus(t("miniapp.admin.memory_saved", "Memory сохранён"));
      } else {
        setAdminChatStatus(`${t("miniapp.admin.err_memory", "Memory ошибка:")} ${resp && resp.error ? resp.error : "unknown"}`);
      }
    } catch (err) {
      setAdminChatStatus(`${t("miniapp.error.save", "Ошибка сохранения")}: ${err.message || err}`);
    }
  }

  function bindAdminChatHandlers() {
    const sendBtn = document.getElementById("adminChatSend");
    if (sendBtn && !sendBtn.dataset.bound) {
      sendBtn.dataset.bound = "1";
      sendBtn.onclick = () => { void postAdminChatMessage(); };
    }
    const input = document.getElementById("adminChatInput");
    if (input && !input.dataset.bound) {
      input.dataset.bound = "1";
      input.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" && !ev.shiftKey) {
          ev.preventDefault();
          void postAdminChatMessage();
        }
      });
    }
    const memoryReload = document.getElementById("adminChatMemoryReload");
    if (memoryReload && !memoryReload.dataset.bound) {
      memoryReload.dataset.bound = "1";
      memoryReload.onclick = () => {
        state.adminChatMemoryDirty = false;
        void fetchAdminChatMemory();
      };
    }
    const memorySave = document.getElementById("adminChatMemorySave");
    if (memorySave && !memorySave.dataset.bound) {
      memorySave.dataset.bound = "1";
      memorySave.onclick = () => { void saveAdminChatMemory(); };
    }
    const memoryTextarea = document.getElementById("adminChatMemory");
    if (memoryTextarea && !memoryTextarea.dataset.bound) {
      memoryTextarea.dataset.bound = "1";
      memoryTextarea.addEventListener("input", () => {
        state.adminChatMemoryDirty = true;
      });
    }
    const chatDetails = document.getElementById("adminChatDetails");
    if (chatDetails && !chatDetails.dataset.bound) {
      chatDetails.dataset.bound = "1";
      chatDetails.addEventListener("toggle", () => {
        if (chatDetails.open) {
          void fetchAdminChatMessages();
          void fetchAdminChatPending();
          if (!state.adminChatMemoryDirty) void fetchAdminChatMemory();
        }
      });
    }
  }

  function isSchedulerTabActive() {
    return !!document.getElementById("tab-scheduler")?.classList.contains("active");
  }

  function setSchedulerStatus(text, ok = true) {
    const el = document.getElementById("schedulerStatus");
    const panel = document.getElementById("schedulerPanel");
    if (!el || !panel) return;
    const value = String(text || "").trim();
    if (value) {
      el.textContent = value;
      el.className = ok ? "status-empty" : "status-error";
      el.classList.remove("hidden");
      panel.classList.add("hidden");
      return;
    }
    el.classList.add("hidden");
    panel.classList.remove("hidden");
  }

  function applySchedulerStateFromControls() {
    state.schedulerProjectSlug = String(document.getElementById("schedulerProject")?.value || "");
    state.schedulerSessionUid = String(document.getElementById("schedulerSession")?.value || "");
  }

  function schedulerModeOptions() {
    const payloadModes = Array.isArray(state.statusLastPayload?.modes) ? state.statusLastPayload.modes : [];
    if (payloadModes.length) {
      return payloadModes.map((item) => ({
        id: String(item?.id || ""),
        label: String(item?.label || item?.id || ""),
      })).filter((item) => item.id);
    }
    return [];
  }

  function renderSchedulerModeOptions(selectedMode = "") {
    const select = document.getElementById("schedulerTargetMode");
    if (!select) return;
    const current = String(selectedMode || select.value || "");
    const options = schedulerModeOptions()
      .map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.label)}</option>`)
      .join("");
    select.innerHTML = options;
    if (current) {
      select.value = current;
    }
    if (!select.value && select.options.length) {
      select.selectedIndex = 0;
    }
  }

  function renderSchedulerProjectOptions(projects, selectedProjectSlug) {
    const select = document.getElementById("schedulerProject");
    if (!select) return;
    const projectList = Array.isArray(projects) ? projects : [];
    const nextSignature = projectList
      .map((item) => `${String(item?.slug || "")}\u0000${String(item?.name || "")}`)
      .join("\u0001");
    const desiredSelected = String(
      selectedProjectSlug || state.schedulerProjectSlug || (projectList.length === 1 ? projectList[0]?.slug || "" : "")
    );
    if (!select.options.length || state.schedulerProjectsSignature !== nextSignature) {
      const options = projectList
        .map((item) => `<option value="${escapeHtml(item.slug)}">${escapeHtml(item.name || item.slug)}</option>`)
        .join("");
      select.innerHTML = `<option value="">${escapeHtml(t("miniapp.label.choose_project", "Выберите проект"))}</option>${options}`;
      state.schedulerProjectsSignature = nextSignature;
    }
    if (desiredSelected) {
      select.value = desiredSelected;
    }
    state.schedulerProjectSlug = String(select.value || "");
  }

  function renderSchedulerSessionOptions(options, selectedSessionUid = "") {
    const select = document.getElementById("schedulerSession");
    if (!select) return;
    const values = Array.isArray(options) ? options : [];
    const nextSignature = values
      .map((item) => `${String(item?.telegram_session_uid || "")}\u0000${String(item?.label || "")}`)
      .join("\u0001");
    // Auto-bind to active admin session if no explicit selection is available
    const desiredSelected = String(selectedSessionUid || state.schedulerSessionUid || state.adminSessionUid || "");
    if (!select.options.length || state.schedulerSessionsSignature !== nextSignature) {
      const items = values
        .map((item) => `<option value="${escapeHtml(item.telegram_session_uid)}">${escapeHtml(item.label || item.telegram_session_uid)}</option>`)
        .join("");
      select.innerHTML = `<option value="">${escapeHtml(t("miniapp.label.choose_session_uid", "Выберите session_uid"))}</option>${items}`;
      state.schedulerSessionsSignature = nextSignature;
    }
    if (desiredSelected) {
      select.value = desiredSelected;
    }
    state.schedulerSessionUid = String(select.value || "");
  }

  function findSchedulerJob(jobId) {
    const token = String(jobId || "").trim();
    return (state.schedulerJobs || []).find((item) => String(item?.job_id || "") === token) || null;
  }

  function resetSchedulerForm({ keepProject = true, keepSession = true } = {}) {
    const saveButton = document.getElementById("schedulerSave");
    const deleteButton = document.getElementById("schedulerDelete");
    const runNowButton = document.getElementById("schedulerRunNow");
    const pauseButton = document.getElementById("schedulerPause");
    const resumeButton = document.getElementById("schedulerResume");
    state.schedulerSelectedJobId = "";
    document.getElementById("schedulerJobName").value = "";
    document.getElementById("schedulerCron").value = "";
    document.getElementById("schedulerPayload").value = "";
    renderSchedulerModeOptions("manager");
    document.getElementById("schedulerEnabled").checked = true;
    if (!keepProject) {
      document.getElementById("schedulerProject").value = "";
      state.schedulerProjectSlug = "";
    }
    if (!keepSession) {
      document.getElementById("schedulerSession").value = "";
      state.schedulerSessionUid = "";
    }
    if (saveButton) saveButton.textContent = t("miniapp.btn.create", "Создать");
    if (deleteButton) deleteButton.disabled = true;
    if (runNowButton) runNowButton.disabled = true;
    if (pauseButton) pauseButton.disabled = true;
    if (resumeButton) resumeButton.disabled = true;
  }

  function selectSchedulerJob(jobId) {
    const job = findSchedulerJob(jobId);
    if (!job) {
      resetSchedulerForm();
      return;
    }
    state.schedulerSelectedJobId = String(job.job_id || "");
    document.getElementById("schedulerJobName").value = String(job.job_name || "");
    document.getElementById("schedulerCron").value = String(job.cron || "");
    const payload = job.payload || {};
    document.getElementById("schedulerPayload").value = JSON.stringify(payload, null, 2);
    renderSchedulerModeOptions(String(job.target_mode || ""));
    document.getElementById("schedulerEnabled").checked = !!job.enabled;
    renderSchedulerSessionOptions(
      Array.isArray(state.schedulerNotificationTargets) ? state.schedulerNotificationTargets : [],
      String(job.notification_target?.telegram_session_uid || "")
    );
    const saveButton = document.getElementById("schedulerSave");
    const deleteButton = document.getElementById("schedulerDelete");
    const runNowButton = document.getElementById("schedulerRunNow");
    if (saveButton) saveButton.textContent = t("miniapp.btn.refresh", "Обновить");
    if (deleteButton) deleteButton.disabled = false;
    if (runNowButton) runNowButton.disabled = false;
    updateSchedulerPauseResumeButtons();
  }

  function renderSchedulerJobs(jobs) {
    const list = document.getElementById("schedulerJobsList");
    const meta = document.getElementById("schedulerSelectedMeta");
    if (!list || !meta) return;
    const items = Array.isArray(jobs) ? jobs : [];
    state.schedulerJobs = items;
    list.innerHTML = "";
    if (!items.length) {
      meta.textContent = state.schedulerProjectSlug
        ? `${t("miniapp.scheduler.no_jobs_for_project", "Для проекта")} ${state.schedulerProjectSlug} jobs пока нет.`
        : t("miniapp.scheduler.select_project_first", "Сначала выберите проект.");
      if (!state.schedulerSelectedJobId) {
        resetSchedulerForm();
      }
      return;
    }
    items.forEach((job) => {
      const li = document.createElement("li");
      li.textContent = `${job.enabled ? "⏰" : "⏸"} ${job.job_name || job.job_id} — ${job.cron} → ${job.target_mode}`;
      li.dataset.jobId = String(job.job_id || "");
      if (String(job.job_id || "") === String(state.schedulerSelectedJobId || "")) {
        li.classList.add("selected");
      }
      li.onclick = () => {
        document.querySelectorAll("#schedulerJobsList li").forEach((item) => item.classList.remove("selected"));
        li.classList.add("selected");
        selectSchedulerJob(job.job_id);
        const nextRun = Number(job.next_run_at || 0);
        const lastRun = Number(job.last_fired_at || 0);
        meta.textContent = [
          `Job: ${job.job_name || job.job_id}`,
          `ID: ${job.job_id}`,
          `Next run: ${nextRun > 0 ? serverTimeText("", nextRun) : "-"}`,
          `Last fired: ${lastRun > 0 ? serverTimeText("", lastRun) : "-"}`,
        ].join("\n");
      };
      list.appendChild(li);
    });
    if (state.schedulerSelectedJobId) {
      const selected = findSchedulerJob(state.schedulerSelectedJobId);
      if (selected) {
        selectSchedulerJob(selected.job_id);
        return;
      }
    }
    resetSchedulerForm();
    meta.textContent = `${t("miniapp.scheduler.selected_jobs", "Выбрано jobs")}: ${items.length}. ${t("miniapp.scheduler.click_to_edit", "Нажмите на job для редактирования.")}`;
  }

  function renderSchedulerPayload(payload) {
    const body = payload || {};
    const projects = Array.isArray(body.projects) ? body.projects : [];
    const notificationTargets = Array.isArray(body.notification_targets) ? body.notification_targets : [];
    state.schedulerNotificationTargets = notificationTargets;
    renderSchedulerProjectOptions(projects, String(body.selected_project_slug || ""));
    renderSchedulerSessionOptions(notificationTargets);
    renderSchedulerModeOptions();
    if (!state.schedulerProjectSlug) {
      renderSchedulerJobs([]);
      setSchedulerStatus(projects.length ? t("miniapp.scheduler.choose_project", "Выберите проект для управления расписанием.") : t("miniapp.scheduler.no_projects", "Нет доступных проектов."));
      return;
    }
    renderSchedulerJobs(Array.isArray(body.jobs) ? body.jobs : []);
    setSchedulerStatus("");
  }

  async function fetchSchedulerJobs() {
    applySchedulerStateFromControls();
    const qs = new URLSearchParams();
    if (state.schedulerProjectSlug) {
      qs.set("project_slug", state.schedulerProjectSlug);
    }
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    const payload = await api(`/v1/scheduler/jobs${suffix}`);
    renderSchedulerPayload(payload);
    if (!state.schedulerProjectSlug && Array.isArray(payload?.projects) && payload.projects.length === 1) {
      state.schedulerProjectSlug = String(payload.projects[0]?.slug || "");
      document.getElementById("schedulerProject").value = state.schedulerProjectSlug;
      return fetchSchedulerJobs();
    }
    return payload;
  }

  function schedulerRequestBody() {
    applySchedulerStateFromControls();
    const projectSlug = String(state.schedulerProjectSlug || "");
    const telegramSessionUid = String(state.schedulerSessionUid || "");
    const jobName = String(document.getElementById("schedulerJobName")?.value || "").trim();
    const cron = String(document.getElementById("schedulerCron")?.value || "").trim();
    const targetMode = String(document.getElementById("schedulerTargetMode")?.value || "").trim();
    const enabled = !!document.getElementById("schedulerEnabled")?.checked;
    if (!projectSlug) {
      throw new Error(t("miniapp.scheduler.err_no_project", "Проект не выбран"));
    }
    if (!telegramSessionUid) {
      throw new Error(t("miniapp.scheduler.err_no_session_uid", "Telegram session_uid не выбран"));
    }
    if (!cron) {
      throw new Error(t("miniapp.scheduler.err_no_cron", "Cron обязателен"));
    }
    if (!targetMode) {
      throw new Error(t("miniapp.scheduler.err_no_target_mode", "Target mode обязателен"));
    }

    let payload = { project_slug: projectSlug };
    const rawPayload = String(document.getElementById("schedulerPayload")?.value || "").trim();
    if (rawPayload) {
      try {
        const parsed = JSON.parse(rawPayload);
        if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
          throw new Error(t("miniapp.scheduler.err_payload_not_object", "Payload должен быть JSON-объектом"));
        }
        payload = Object.assign({}, parsed, { project_slug: projectSlug });
      } catch (err) {
        throw new Error(`${t("miniapp.scheduler.err_invalid_json", "Некорректный JSON в Payload:")} ${err.message}`);
      }
    }

    return {
      project_slug: projectSlug,
      job_id: String(state.schedulerSelectedJobId || ""),
      job_name: jobName,
      cron,
      target_mode: targetMode,
      enabled,
      notification_target: {
        telegram_session_uid: telegramSessionUid,
      },
      payload,
    };
  }

  async function saveSchedulerJob() {
    try {
      const body = schedulerRequestBody();
      const hasSelection = !!String(state.schedulerSelectedJobId || "").trim();
      const path = hasSelection ? "/v1/scheduler/jobs/update" : "/v1/scheduler/jobs";
      await api(path, {
        method: "POST",
        body: JSON.stringify(body),
      });
      setSchedulerStatus("");
      await fetchSchedulerJobs();
    } catch (err) {
      setSchedulerStatus(`${t("miniapp.scheduler.err_save", "Ошибка scheduler save:")} ${err.message || "unknown"}`, false);
    }
  }

  async function deleteSchedulerJob() {
    applySchedulerStateFromControls();
    const jobId = String(state.schedulerSelectedJobId || "").trim();
    if (!jobId) {
      setSchedulerStatus(t("miniapp.scheduler.no_job_selected", "Job не выбрана"), false);
      return;
    }
    if (!(await uiConfirm(`${t("miniapp.scheduler.delete_job_confirm", "Удалить job")} ${jobId}?`))) {
      return;
    }
    try {
      await api("/v1/scheduler/jobs/delete", {
        method: "POST",
        body: JSON.stringify({
          project_slug: String(state.schedulerProjectSlug || ""),
          job_id: jobId,
        }),
      });
      resetSchedulerForm();
      await fetchSchedulerJobs();
    } catch (err) {
      setSchedulerStatus(`${t("miniapp.scheduler.err_delete", "Ошибка scheduler delete:")} ${err.message || "unknown"}`, false);
    }
  }

  async function runSchedulerJobNow() {
    applySchedulerStateFromControls();
    const jobId = String(state.schedulerSelectedJobId || "").trim();
    if (!jobId) {
      setSchedulerStatus(t("miniapp.scheduler.no_job_selected", "Job не выбрана"), false);
      return;
    }
    try {
      await api("/v1/scheduler/jobs/run_now", {
        method: "POST",
        body: JSON.stringify({
          project_slug: String(state.schedulerProjectSlug || ""),
          job_id: jobId,
        }),
      });
      setSchedulerStatus(t("miniapp.scheduler.job_dispatched", "Job отправлена на немедленную публикацию события."));
      await fetchSchedulerJobs();
    } catch (err) {
      setSchedulerStatus(`${t("miniapp.scheduler.err_run_now", "Ошибка scheduler run_now:")} ${err.message || "unknown"}`, false);
    }
  }

  async function pauseSchedulerJob() {
    applySchedulerStateFromControls();
    const jobId = String(state.schedulerSelectedJobId || "").trim();
    if (!jobId) {
      setSchedulerStatus(t("miniapp.scheduler.no_job_selected", "Job не выбрана"), false);
      return;
    }
    try {
      await api("/v1/scheduler/jobs/pause", {
        method: "POST",
        body: JSON.stringify({
          project_slug: String(state.schedulerProjectSlug || ""),
          job_id: jobId,
        }),
      });
      setSchedulerStatus(t("miniapp.scheduler.job_paused", "Job приостановлена."));
      await fetchSchedulerJobs();
    } catch (err) {
      setSchedulerStatus(`${t("miniapp.scheduler.err_pause", "Ошибка scheduler pause:")} ${err.message || "unknown"}`, false);
    }
  }

  async function resumeSchedulerJob() {
    applySchedulerStateFromControls();
    const jobId = String(state.schedulerSelectedJobId || "").trim();
    if (!jobId) {
      setSchedulerStatus(t("miniapp.scheduler.no_job_selected", "Job не выбрана"), false);
      return;
    }
    try {
      await api("/v1/scheduler/jobs/resume", {
        method: "POST",
        body: JSON.stringify({
          project_slug: String(state.schedulerProjectSlug || ""),
          job_id: jobId,
        }),
      });
      setSchedulerStatus(t("miniapp.scheduler.job_resumed", "Job возобновлена."));
      await fetchSchedulerJobs();
    } catch (err) {
      setSchedulerStatus(`${t("miniapp.scheduler.err_resume", "Ошибка scheduler resume:")} ${err.message || "unknown"}`, false);
    }
  }

  function updateSchedulerPauseResumeButtons() {
    const pauseButton = document.getElementById("schedulerPause");
    const resumeButton = document.getElementById("schedulerResume");
    if (!pauseButton || !resumeButton) return;
    const jobId = String(state.schedulerSelectedJobId || "").trim();
    if (!jobId) {
      pauseButton.disabled = true;
      resumeButton.disabled = true;
      return;
    }
    const job = findSchedulerJob(jobId);
    const isPaused = job ? !job.enabled : false;
    pauseButton.disabled = isPaused;
    resumeButton.disabled = !isPaused;
  }

  function tickTimeText(tsValue) {
    const ts = Number(tsValue);
    if (!Number.isFinite(ts) || ts <= 0) return "-";
    return new Date(ts * 1000).toLocaleString("ru-RU");
  }

  function tickValueText(value) {
    return statusValueText(value);
  }

  function normalizeTickKind(value) {
    return String(value || "").trim().toLowerCase();
  }

  function normalizeTickHistoryItem(item) {
    if (item && typeof item === "object" && !Array.isArray(item)) {
      const ts = Number(item.ts);
      return {
        ts: Number.isFinite(ts) && ts > 0 ? ts : null,
        value: tickValueText(item.value),
        kind: normalizeTickKind(item.kind),
      };
    }
    return {
      ts: null,
      value: tickValueText(item),
      kind: "",
    };
  }

  function tickHistoryItemKey(item) {
    return `${item.ts === null ? "na" : String(item.ts)}|${item.kind}|${item.value}`;
  }

  function resetTickHistoryState() {
    state.maxTickTsSeen = 0;
    state.ticksCount = 0;
    state.tickHistoryItems = [];
    state.tickHistoryKeys = new Set();
    const container = document.getElementById("tickListContainer");
    if (container) container.innerHTML = "";
  }

  function mergeTickHistoryItems(items) {
    const nextItems = [];
    const nextKeys = new Set();
    for (const rawItem of Array.isArray(items) ? items : []) {
      const item = normalizeTickHistoryItem(rawItem);
      const key = tickHistoryItemKey(item);
      if (nextKeys.has(key)) continue;
      nextKeys.add(key);
      nextItems.push(item);
    }
    nextItems.sort((left, right) => {
      const leftTs = left.ts === null ? 0 : left.ts;
      const rightTs = right.ts === null ? 0 : right.ts;
      if (leftTs !== rightTs) return leftTs - rightTs;
      return left.value.localeCompare(right.value, "ru");
    });
    if (nextItems.length > 1000) {
      state.tickHistoryItems = nextItems.slice(-200);
      state.tickHistoryKeys = new Set(state.tickHistoryItems.map((item) => tickHistoryItemKey(item)));
    } else {
      state.tickHistoryItems = nextItems;
      state.tickHistoryKeys = nextKeys;
    }
    state.maxTickTsSeen = state.tickHistoryItems.reduce((maxTs, item) => {
        if (item.ts === null) return maxTs;
        return item.ts > maxTs ? item.ts : maxTs;
    }, 0);
    return true;
  }

  function renderTickHistoryPanel() {
    const container = document.getElementById("tickListContainer");
    const details = document.getElementById("statusTicksDetails");
    const autoscrollCb = document.getElementById("ticksAutoScroll");
    if (!container || !details) return;

    container.innerHTML = "";
    for (const item of state.tickHistoryItems) {
      const div = document.createElement("div");
      div.className = item.kind ? `tick-row tick-row-${item.kind}` : "tick-row";
      div.innerHTML = `<div class="tick-val">${escapeHtml(item.value)}</div>`;
      container.appendChild(div);
    }

    state.ticksCount = state.tickHistoryItems.length;
    if (state.tickHistoryItems.length > 0) {
      details.classList.remove("hidden");
    } else {
      details.classList.add("hidden");
    }

    if (state.tickHistoryItems.length > 0 && autoscrollCb && autoscrollCb.checked) {
      const textContainer = document.getElementById("statusTicksText");
      if (textContainer) {
        textContainer.scrollTop = textContainer.scrollHeight;
      }
    }
  }

  function renderStatusSessionOptions(payload) {
    const select = document.getElementById("statusSession");
    if (!select) return;
    const sessions = Array.isArray(payload?.available_sessions) ? payload.available_sessions : [];
    const defaultLabel = t("miniapp.label.choose_session", "Выберите сессию");
    const signatureParts = sessions.map((item) => {
      const uid = String(item?.session_uid || "");
      const label = String(item?.label || uid);
      return `${uid}\u0000${label}`;
    });
    const nextSignature = `${defaultLabel}\u0002${signatureParts.join("\u0001")}`;
    const shouldRebuild = !select.options.length || state.statusSessionsSignature !== nextSignature;
    const fallbackSelected = String(payload?.selected_session_uid || "");
    const desiredSelected = state.statusSessionUid || fallbackSelected;
    if (shouldRebuild) {
      const options = sessions
        .map((item) => `<option value="${escapeHtml(item.session_uid)}">${escapeHtml(item.label || item.session_uid)}</option>`)
        .join("");
      select.innerHTML = `<option value="">${escapeHtml(defaultLabel)}</option>${options}`;
      if (desiredSelected) {
        select.value = desiredSelected;
      }
      if (select.value !== desiredSelected && fallbackSelected) {
        select.value = fallbackSelected;
      }
      state.statusSessionsSignature = nextSignature;
    }
    state.statusSessionUid = String(select.value || "");
  }

  function renderFilesSessionOptions(payload) {
    const select = document.getElementById("filesSession");
    if (!select) return;
    const sessions = Array.isArray(payload?.available_sessions) ? payload.available_sessions : [];
    const defaultLabel = t("miniapp.label.choose_session", "Выберите сессию");
    const signatureParts = sessions.map((item) => {
      const uid = String(item?.session_uid || "");
      const label = String(item?.label || uid);
      return `${uid}\u0000${label}`;
    });
    const nextSignature = `${defaultLabel}\u0002${signatureParts.join("\u0001")}`;
    const fallbackSelected = String(payload?.selected_session_uid || "");
    const desiredSelected = state.filesSessionUid || fallbackSelected;
    if (!select.options.length || state.filesSessionsSignature !== nextSignature) {
      const options = sessions
        .map((item) => `<option value="${escapeHtml(item.session_uid)}">${escapeHtml(item.label || item.session_uid)}</option>`)
        .join("");
      select.innerHTML = `<option value="">${escapeHtml(defaultLabel)}</option>${options}`;
      if (desiredSelected) {
        select.value = desiredSelected;
      }
      if (select.value !== desiredSelected && fallbackSelected) {
        select.value = fallbackSelected;
      }
      state.filesSessionsSignature = nextSignature;
    }
    state.filesSessionUid = String(select.value || "");
  }

  function renderSettingsSessionOptions(payload) {
    const select = document.getElementById("settingsSession");
    if (!select) return;
    const sessions = Array.isArray(payload?.available_sessions) ? payload.available_sessions : [];
    const defaultLabel = t("miniapp.label.choose_session", "Выберите сессию");
    const signatureParts = sessions.map((item) => {
      const uid = String(item?.session_uid || "");
      const label = String(item?.label || uid);
      return `${uid}\u0000${label}`;
    });
    const nextSignature = `${defaultLabel}\u0002${signatureParts.join("\u0001")}`;
    const fallbackSelected = String(payload?.selected_session_uid || "");
    const desiredSelected = state.settingsSessionUid || fallbackSelected;
    if (!select.options.length || state.settingsSessionsSignature !== nextSignature) {
      const options = sessions
        .map((item) => `<option value="${escapeHtml(item.session_uid)}">${escapeHtml(item.label || item.session_uid)}</option>`)
        .join("");
      select.innerHTML = `<option value="">${escapeHtml(defaultLabel)}</option>${options}`;
      if (desiredSelected) {
        select.value = desiredSelected;
      }
      if (select.value !== desiredSelected && fallbackSelected) {
        select.value = fallbackSelected;
      }
      state.settingsSessionsSignature = nextSignature;
    }
    state.settingsSessionUid = String(select.value || "");
  }

  function isStatusTabActive() {
    return !!document.getElementById("tab-status")?.classList.contains("active");
  }

  function setRunsMessage(text, ok = true) {
    const empty = document.getElementById("statusRunsMessage");
    const panel = document.getElementById("statusRunsPanel");
    if (!empty || !panel) return;
    const value = String(text || "").trim();
    if (value) {
      empty.textContent = value;
      empty.className = ok ? "status-empty" : "status-error";
      empty.classList.remove("hidden");
      panel.classList.add("hidden");
      return;
    }
    empty.classList.add("hidden");
    panel.classList.remove("hidden");
  }

  function setRunsActionMessage(text, ok = true) {
    const el = document.getElementById("statusRunsActionMessage");
    if (!el) return;
    const value = String(text || "").trim();
    el.textContent = value;
    el.className = ok ? "status-empty" : "status-error";
    el.classList.toggle("hidden", !value);
  }

  function setRunActionButtonsEnabled(enabled, run = null) {
    const doctorButton = document.getElementById("statusRunDoctor");
    const recoverButton = document.getElementById("statusRunRecover");
    const resumeButton = document.getElementById("statusRunResume");
    const applyButton = document.getElementById("statusRunApplyRecommendation");
    const promoteButton = document.getElementById("statusRunPromote");
    const hasRun = !!run;
    const status = String(run?.status || "").trim().toLowerCase();
    const terminalBlocked = !!run?.terminal_actions_blocked || status === "completed" || status === "superseded";
    const canResume = !!run?.can_resume && !terminalBlocked;
    const canRecover = !!run?.can_recover && !terminalBlocked && !run?.can_apply_recommendation;
    const canApplyRecommendation = !!run?.can_apply_recommendation;
    const canPromote = !!(state.me?.is_admin && Array.isArray(run?.project_local_skill_ids) && run.project_local_skill_ids.length);
    if (doctorButton) doctorButton.disabled = !(enabled && hasRun);
    if (recoverButton) recoverButton.disabled = !(enabled && hasRun && canRecover);
    if (resumeButton) resumeButton.disabled = !(enabled && hasRun && canResume);
    if (applyButton) {
      applyButton.disabled = !(enabled && hasRun && canApplyRecommendation);
      applyButton.textContent = runActionLabel(run?.recommended_action);
    }
    if (promoteButton) promoteButton.disabled = !(enabled && hasRun && canPromote);
  }

  function runActionLabel(action) {
    const token = String(action || "").trim();
    if (token === "rerun_same_operation") return "Rerun";
    if (token === "run_validate") return "Validate";
    if (token === "run_repair") return "Repair";
    return "Apply Recommendation";
  }

  function renderRunDetail(run) {
    const detail = document.getElementById("statusRunDetailText");
    const skillLog = document.getElementById("statusRunSkillLog");
    if (!detail || !skillLog) return;
    state.runsCurrentDetail = run || null;
    if (!run) {
      detail.textContent = t("miniapp.runs.select_run", "Выберите запуск.");
      skillLog.textContent = "";
      setRunActionButtonsEnabled(false, null);
      return;
    }
    const issueCodes = Array.isArray(run.issue_codes) ? run.issue_codes.filter(Boolean) : [];
    const detailLines = [
      `Run: ${String(run.run_id || "-")}`,
      `Mode: ${String(run.mode_id || "-")}`,
      `Phase: ${String(run.phase || "-")}`,
      `Status: ${String(run.status || "-")}`,
      `Action: ${String(run.recommended_action || "-")}`,
      `Current unit: ${String(run.current_unit_id || "-")}`,
      `Issues: ${issueCodes.length ? issueCodes.join(", ") : t("miniapp.status.bool_no", "нет")}`,
      `Local skills: ${
        Array.isArray(run.project_local_skill_ids) && run.project_local_skill_ids.length
          ? run.project_local_skill_ids.join(", ")
          : t("miniapp.status.bool_no", "нет")
      }`,
    ];
    detail.textContent = detailLines.join("\n");
    const skills = Array.isArray(run.skill_log) ? run.skill_log.filter(Boolean) : [];
    skillLog.textContent = skills.length ? `Skills:\n${skills.join("\n")}` : t("miniapp.runs.no_injections", "Skills: нет инъекций");
    setRunActionButtonsEnabled(!state.runsRequestInFlight, run);
  }

  async function fetchRunDetail(runId, modeId) {
    const sessionUid = String(state.statusSessionUid || "").trim();
    const targetRunId = String(runId || "").trim();
    const targetModeId = String(modeId || "").trim();
    if (!sessionUid || !targetRunId) {
      renderRunDetail(null);
      return null;
    }
    const qs = new URLSearchParams({ session_uid: sessionUid });
    if (targetModeId) {
      qs.set("mode_id", targetModeId);
    }
    try {
      const payload = await api(`/runs/${encodeURIComponent(targetRunId)}?${qs.toString()}`);
      const run = payload && payload.run ? payload.run : null;
      state.runsSelectedRunId = String(run?.run_id || targetRunId);
      state.runsSelectedModeId = String(run?.mode_id || targetModeId);
      renderRunDetail(run);
      return run;
    } catch (err) {
      renderRunDetail(null);
      setRunsActionMessage(`${t("miniapp.runs.err_detail", "Ошибка run detail:")} ${err.message || "unknown"}`, false);
      return null;
    }
  }

  function renderRunsList(payload) {
    const list = document.getElementById("statusRunsList");
    if (!list) return;
    const runs = Array.isArray(payload?.runs) ? payload.runs : [];
    state.runsSignature = runs
      .map((item) => [item?.mode_id, item?.run_id, item?.status, item?.phase].map((part) => String(part || "")).join("\u0000"))
      .join("\u0001");
    list.innerHTML = "";
    runs.forEach((item) => {
      const li = document.createElement("li");
      if (String(item?.run_id || "") === String(state.runsSelectedRunId || "")) {
        li.classList.add("selected");
      }
      const chunks = [
        `${String(item?.mode_id || "-")}  - ${String(item?.run_id || "-")}`,
        `${String(item?.phase || "-")}  - ${String(item?.status || "-")}`,
      ];
      if (Array.isArray(item?.skill_log) && item.skill_log.length) {
        chunks.push(String(item.skill_log[0] || ""));
      }
      li.innerHTML = chunks.map((line) => `<div>${escapeHtml(line)}</div>`).join("");
      li.onclick = () => {
        state.runsSelectedRunId = String(item?.run_id || "");
        state.runsSelectedModeId = String(item?.mode_id || "");
        setRunsActionMessage(state.runsLastActionMessage);
        document.querySelectorAll("#statusRunsList li").forEach((node) => node.classList.remove("selected"));
        li.classList.add("selected");
        void fetchRunDetail(item?.run_id, item?.mode_id);
      };
      list.appendChild(li);
    });
    if (!runs.length) {
      renderRunDetail(null);
    }
  }

  async function fetchRuns() {
    const sessionUid = String(state.statusSessionUid || "").trim();
    state.runsSessionUid = sessionUid;
    if (!sessionUid) {
      state.runsSelectedRunId = "";
      state.runsSelectedModeId = "";
      setRunsActionMessage("");
      setRunsMessage(t("miniapp.runs.choose_session", "Выберите сессию, чтобы просмотреть run artifacts."));
      renderRunDetail(null);
      return null;
    }
    if (state.runsRequestInFlight) {
      return null;
    }
    state.runsRequestInFlight = true;
    setRunActionButtonsEnabled(false, null);
    let detailRun = null;
    try {
      const payload = await api(`/runs?${new URLSearchParams({ session_uid: sessionUid, limit: "12" }).toString()}`);
      const runs = Array.isArray(payload?.runs) ? payload.runs : [];
      renderRunsList(payload || {});
      if (!runs.length) {
        setRunsMessage(t("miniapp.runs.no_runs", "Запусков пока нет."));
        setRunsActionMessage("");
        return payload;
      }
      setRunsMessage("");
      let selected = runs.find((item) => {
        return (
          String(item?.run_id || "") === String(state.runsSelectedRunId || "")
          && String(item?.mode_id || "") === String(state.runsSelectedModeId || "")
        );
      });
      if (!selected) {
        selected = runs[0];
        state.runsSelectedRunId = String(selected?.run_id || "");
        state.runsSelectedModeId = String(selected?.mode_id || "");
      }
      if (state.runsLastActionMessage) {
        setRunsActionMessage(state.runsLastActionMessage);
      }
      detailRun = await fetchRunDetail(selected?.run_id, selected?.mode_id);
      return payload;
    } catch (err) {
      state.runsSelectedRunId = "";
      state.runsSelectedModeId = "";
      setRunsActionMessage("");
      setRunsMessage(`${t("miniapp.runs.err_load_artifacts", "Ошибка загрузки run artifacts:")} ${err.message || "unknown"}`, false);
      renderRunDetail(null);
      return null;
    } finally {
      state.runsRequestInFlight = false;
      renderRunDetail(detailRun || state.runsCurrentDetail);
    }
  }

  async function performRunAction(action) {
    const sessionUid = String(state.statusSessionUid || "").trim();
    const runId = String(state.runsSelectedRunId || "").trim();
    const modeId = String(state.runsSelectedModeId || "").trim();
    if (!sessionUid || !runId) {
      setRunsActionMessage(t("miniapp.runs.select_run_first", "Сначала выберите запуск."), false);
      return null;
    }
    state.runsRequestInFlight = true;
    setRunActionButtonsEnabled(false, null);
    let detailRun = null;
    try {
      const payload = await api(`/runs/${encodeURIComponent(runId)}/${encodeURIComponent(String(action || ""))}`, {
        method: "POST",
        body: JSON.stringify({
          session_uid: sessionUid,
          mode_id: modeId,
        }),
      });
      const result = payload?.result || {};
      const run = payload?.run || null;
      state.runsLastActionMessage = String(result.message || "");
      setRunsActionMessage(state.runsLastActionMessage, String(result.status || "") === "ok");
      if (run) {
        state.runsSelectedRunId = String(run.run_id || runId);
        state.runsSelectedModeId = String(run.mode_id || modeId);
        detailRun = run;
        renderRunDetail(run);
      }
      await fetchRuns();
      return payload;
    } catch (err) {
      state.runsLastActionMessage = `${t("miniapp.runs.err_action", "Ошибка run action:")} ${err.message || "unknown"}`;
      setRunsActionMessage(state.runsLastActionMessage, false);
      return null;
    } finally {
      state.runsRequestInFlight = false;
      renderRunDetail(detailRun || state.runsCurrentDetail);
    }
  }

  function startRunsPolling() {
    if (state.runsPollTimer) {
      return;
    }
    state.runsPollTimer = setInterval(() => {
      if (!isStatusTabActive() || state.runsRequestInFlight) return;
      void fetchRuns();
    }, 5000);
  }

  function stopRunsPolling() {
    if (!state.runsPollTimer) {
      return;
    }
    clearInterval(state.runsPollTimer);
    state.runsPollTimer = null;
  }

  function renderStatusPayload(payload) {
    state.statusLastPayload = payload || null;
    const previousStatusSessionUid = String(state.statusSessionUid || "");
    renderStatusSessionOptions(payload || {});
    renderFilesSessionOptions(payload || {});
    renderSettingsSessionOptions(payload || {});
    renderReportsSessionOptions(payload || {});
    if (state.me?.is_admin) {
      renderAdminSessionOptions(payload || {});
    }
    if (String(state.statusSessionUid || "") !== previousStatusSessionUid) {
      state.runsSessionUid = String(state.statusSessionUid || "");
      state.runsSelectedRunId = "";
      state.runsSelectedModeId = "";
      state.runsLastActionMessage = "";
      renderRunDetail(null);
      if (isStatusTabActive()) {
        void fetchRuns();
      }
    }

    const p = payload || {};
    const active = p.active_session || null;

    if (active && active.id !== state.lastRenderedSessionId) {
        state.lastRenderedSessionId = active.id;
        resetTickHistoryState();
    }
    if (!active || !active.id) {
        state.lastRenderedSessionId = null;
        resetTickHistoryState();
        // Still update global time if possible
        const stServerTime = document.getElementById("stServerTime");
        const stSessionCount = document.getElementById("stSessionCount");
        if (stServerTime) stServerTime.textContent = serverTimeText(p.server_time_iso, p.server_time_epoch);
        if (stSessionCount) stSessionCount.textContent = String(p.session_count || 0);

        setStatus(p.status_text || t("miniapp.status.session_unavailable", "Сессия не выбрана или недоступна"));
        setStatusRaw(JSON.stringify(payload || {}, null, 2));
        return;
    }
    
    setStatus(""); // Switch to dashboard view
    
    document.getElementById("stServerTime").textContent = serverTimeText(p.server_time_iso, p.server_time_epoch);
    document.getElementById("stSessionCount").textContent = String(p.session_count || 0);
    document.getElementById("stWorkdir").textContent = String(active.workdir || "-");
    
    // Update Execution Target banners
    const execTarget = active.execution_target || "local";
    const remoteHost = active.remote_host_alias || "unknown";
    const remoteRoot = active.remote_project_root || "unknown";
    
    const bannerElements = [
        document.getElementById("statusExecutionTargetBanner"),
        document.getElementById("filesExecutionTargetBanner"),
        document.getElementById("editorExecutionTargetBanner"),
    ];
    const remoteFsElements = [
        document.getElementById("filesRemoteFsBanner"),
        document.getElementById("editorRemoteFsBanner"),
    ];
    
    if (execTarget === "remote") {
        const text = t("miniapp.settings.exec_target_remote", "Цель выполнения: удалённо — {host}").replace("{host}", `${remoteHost} · ${remoteRoot}`);
        bannerElements.forEach(el => {
            if (el) {
                el.textContent = text;
                el.style.backgroundColor = "var(--button-primary)";
                el.style.color = "white";
                el.style.display = "block";
            }
        });
        remoteFsElements.forEach(el => {
            if (el) {
                el.textContent = `${t("miniapp.banner.remote_fs", "Удалённая ФС")} · ${remoteHost} · ${remoteRoot}`;
                el.style.display = "block";
            }
        });
    } else {
        const text = t("miniapp.settings.exec_target_local", "Цель выполнения: локально");
        bannerElements.forEach(el => {
            if (el) {
                el.textContent = text;
                el.style.backgroundColor = "var(--card-bg)";
                el.style.color = "";
                el.style.display = "block";
            }
        });
        remoteFsElements.forEach(el => {
            if (el) el.style.display = "none";
        });
    }

    document.getElementById("stUptime").textContent = ageText(active.started_age_sec);
    document.getElementById("stLastOutput").textContent = ageText(active.last_output_age_sec);
    
    const isBusy = !!active.busy;
    const stStatusIcon = document.getElementById("stStatusIcon");
    const stStatusText = document.getElementById("stStatusText");
    if (stStatusIcon) stStatusIcon.textContent = isBusy ? "🔄" : "⏳";
    if (stStatusText) {
        stStatusText.textContent = isBusy ? t("miniapp.status.busy", "Работает") : t("miniapp.status.waiting", "Ожидает");
        stStatusText.style.color = isBusy ? "var(--tg-theme-link-color, #2481cc)" : "var(--muted)";
        stStatusText.style.fontWeight = isBusy ? "bold" : "normal";
    }

    const stGitText = document.getElementById("stGitText");
    if (stGitText) {
        if (active.execution_target === "remote" && active.git_available === false) {
            stGitText.textContent = t("miniapp.status.git_unavailable", "git недоступен для этой цели");
            stGitText.style.color = "var(--muted)";
            stGitText.style.fontWeight = "normal";
        } else if (active.git_conflict) {
            const conflictKind = String(active.git_conflict_kind || "да");
            stGitText.textContent = `${t("miniapp.status.git_conflict", "Конфликт:")} ${conflictKind}`;
            stGitText.style.color = "var(--danger)";
            stGitText.style.fontWeight = "bold";
        } else if (active.git_busy) {
            stGitText.textContent = t("miniapp.status.git_busy", "Занят");
            stGitText.style.color = "var(--tg-theme-link-color, #2481cc)";
            stGitText.style.fontWeight = "normal";
        } else {
            stGitText.textContent = t("miniapp.status.git_free", "Свободен");
            stGitText.style.color = "var(--muted)";
            stGitText.style.fontWeight = "normal";
        }
    }

    const qLen = Number(active.queue_len) || 0;
    const stQueueLabel = document.getElementById("stQueueLabel");
    if (stQueueLabel) {
        stQueueLabel.textContent = String(qLen);
        stQueueLabel.parentElement.style.color = qLen > 0 ? "var(--tg-theme-link-color, #2481cc)" : "var(--muted)";
        stQueueLabel.style.fontWeight = qLen > 0 ? "bold" : "normal";
    }

    const stOrchLabel = document.getElementById("stOrchestratorLabel");
    if (stOrchLabel) {
        stOrchLabel.textContent = active.advanced_orchestrator_enabled ? t("miniapp.status.on", "вкл") : t("miniapp.status.off", "выкл");
        stOrchLabel.parentElement.style.color = active.advanced_orchestrator_enabled ? "var(--tg-theme-link-color, #2481cc)" : "var(--muted)";
    }
    
    let activeModeLabel = t("miniapp.mode.direct_cli", "Прямой CLI");
    let isActiveMode = false;
    const mode = String(active.active_mode || "");
    const modes = Array.isArray(p.modes) ? p.modes : [];
    if (mode) {
        isActiveMode = true;
        const found = modes.find(m => m.id === mode);
        activeModeLabel = found ? found.label : mode;
    }
    
    const modeCls = isActiveMode ? "mode-inline-badge" : "mode-inline-badge default";
    document.getElementById("stMode").innerHTML = `<div class="${modeCls}"><div class="mode-inline-dot"></div><span>${escapeHtml(activeModeLabel)}</span></div>`;
    
    document.getElementById("stTool").textContent = String(active.tool || "-");
    document.getElementById("stCli").textContent = String(active.active_cli || active.tool || "-") + (active.executor_profile ? ` (${active.executor_profile})` : "");
    document.getElementById("stWorkType").textContent = String(active.cli_work_type || "-");
    document.getElementById("stResumeToken").textContent = String(active.active_resume_token || "-");
    document.getElementById("stProjectRoot").textContent = String(active.project_root || "-");

    const pulse = document.getElementById("stPulseDot");
    if (pulse) {
        pulse.style.display = isBusy ? "block" : "none";
    }

    const manContent = mode === "manager" ? String(active.manager_plan_status || "").trim() : "";
    const agentContent = mode === "agent" ? String(active.agent_mode_status || "").trim() : "";
    const anaContent = mode === "analyst" ? String(active.analyst_mode_status || "").trim() : "";
    const webContent = mode === "webmaster" ? String(active.webmaster_mode_status || "").trim() : "";
    const runtimeData = active.runtime_progress && typeof active.runtime_progress === "object" ? active.runtime_progress : {};
    let runtimeContent = String(active.runtime_status || "").trim();
    if (!runtimeContent) {
      const rSrc = String(runtimeData.last_source || "").trim();
      const rPhase = String(runtimeData.last_phase || "").trim();
      const rState = String(runtimeData.last_status || "").trim();
      const rMsg = String(runtimeData.last_message || "").trim();
      const parts = [rSrc, rPhase, rState].filter(Boolean);
      if (parts.length || rMsg) {
        runtimeContent = rMsg ? `${parts.join("/") || "-"}: ${rMsg}` : parts.join("/");
      }
    }
    const recentEvents = Array.isArray(runtimeData.recent_events) ? runtimeData.recent_events : [];
    if (recentEvents.length) {
      const rows = recentEvents.slice(-8).map((item) => {
        const src = String(item?.source || "").trim();
        const phase = String(item?.phase || "").trim();
        const status = String(item?.status || "").trim();
        const msg = String(item?.message || "").trim();
        const prefixParts = [src, phase, status].filter(Boolean);
        const prefix = prefixParts.length ? prefixParts.join("/") : "-";
        return msg ? `${prefix}: ${msg}` : prefix;
      });
      if (rows.length) {
        runtimeContent = runtimeContent ? `${runtimeContent}\n\n${rows.join("\n")}` : rows.join("\n");
      }
    }

    setStatusAccordion("stManagerPlanCard", "statusManagerPlanText", manContent);
    setStatusAccordion("stAgentCard", "statusAgentModeText", agentContent);
    setStatusAccordion("stAnalystCard", "statusAnalystModeText", anaContent);
    setStatusAccordion("stWebmasterCard", "statusWebmasterModeText", webContent);
    setStatusAccordion("stRuntimeCard", "statusRuntimeText", runtimeContent);
    
    const queuePreview = Array.isArray(active.queue_preview) ? active.queue_preview : [];
    document.getElementById("stQueueLen").textContent = String(active.queue_len || 0);
    let qContent = "";
    if (queuePreview.length > 0) {
        qContent = queuePreview.map((item, idx) => {
            const txt = String(item?.text || item?.item || "").replace(/\s+/g, " ").trim();
            return `${idx + 1}. ${txt || "-"}`;
        }).join("\n");
    }
    setStatusAccordion("statusQueueDetails", "stQueueText", qContent);

    const fields = active.fields && typeof active.fields === "object" ? active.fields : {};
    const stateSummarySource = active.state_summary !== undefined ? active.state_summary : fields.state_summary;
    let stateSummaryValue = "";
    if (typeof stateSummarySource === "string") {
        stateSummaryValue = stateSummarySource.trim();
    } else if (stateSummarySource !== null && stateSummarySource !== undefined) {
        stateSummaryValue = statusValueText(stateSummarySource);
    }
    setStatusAccordion("statusSummaryDetails", "statusSummaryText", stateSummaryValue === "-" ? "" : stateSummaryValue);

    const lastTickRaw = active.last_assistant_text_value;
    let lastTickStr = "";
    if (lastTickRaw !== null && lastTickRaw !== undefined) {
        const renderedLastTick = statusValueText(lastTickRaw);
        if (renderedLastTick !== "-") lastTickStr = renderedLastTick;
    }
    const assistantTickCount = Number(active.assistant_tick_count);
    document.getElementById("stTickCount").textContent = Number.isFinite(assistantTickCount)
      ? String(assistantTickCount)
      : String(state.tickHistoryItems.filter((item) => item.kind === "assistant_text").length);
    setStatusAccordion("statusLastTickDetails", "statusLastTickText", lastTickStr);

    const tickHistory = Array.isArray(active.tick_history) ? active.tick_history : [];
    mergeTickHistoryItems(tickHistory);
    renderTickHistoryPanel();

    const alreadyShown = new Set([
      "id", "name", "tool", "active_cli", "active_mode", "executor_profile", 
      "cli_work_type", "workdir", "idle_timeout_sec", "queue", "busy", 
      "git_busy", "git_conflict", "resume_token", "resume_tokens", 
      "advanced_orchestrator_enabled", "started_at", "last_output_ts", 
      "last_tick_ts", "last_tick_value", "last_assistant_text_ts", "last_assistant_text_value",
      "last_assistant_text_age_sec", "assistant_tick_count", "tick_history", "tick_seen", 
      "project_root", "state_summary", "manager_plan_status", "analyst_mode_status", 
      "webmaster_mode_status", "agent_mode_status", "runtime_status", "runtime_progress", "state_updated_at", "_headless_interrupt_flag", 
      "current_proc", "headless_forced_stop", "config"
    ]);
    const extraEntries = Object.entries(fields)
      .filter(([key]) => !alreadyShown.has(String(key)))
      .sort((a, b) => String(a[0]).localeCompare(String(b[0]), "ru"));
    let extraTextHtml = "";
    if (extraEntries.length) {
      const rows = extraEntries.map(([key, value]) => {
        return `<div class="kv-row"><span class="kv-key">${escapeHtml(String(key))}:</span> <span class="kv-val">${escapeHtml(statusValueText(value))}</span></div>`;
      }).join("");
      extraTextHtml = `<div class="kv-list">${rows}</div>`;
    }
    setStatusAccordionHtml("statusExtraDetails", "statusExtraText", extraTextHtml);

    setStatusRaw(JSON.stringify(payload || {}, null, 2));
  }

  async function connectLogsWs() {
    disconnectLogsWs({ manual: false });
    state.logsShouldReconnect = true;
    clearLogsView();
    setLogsStatus(t("miniapp.logs.ws_connecting", "Подключение к потоку логов..."));
    let ticket = "";
    try {
      const payload = await api("/logs/ws_ticket");
      ticket = String(payload.ticket || "");
    } catch (err) {
      const status = Number(err && err.status);
      const isAuthError = status === 401 || status === 403;
      if (isAuthError) {
        state.logsShouldReconnect = false;
        state.logsReconnectAttempts = 0;
        setLogsStatus(`${t("miniapp.logs.err_ws_token", "Ошибка получения ws-токена:")} ${err.message || "unknown"}. ${t("miniapp.logs.err_reopen", "Откройте MiniApp заново.")}`, false);
        return;
      }
      setLogsStatus(`${t("miniapp.logs.err_ws_token", "Ошибка получения ws-токена:")} ${err.message || "unknown"}`, false);
      scheduleLogsReconnect();
      return;
    }
    if (!ticket) {
      setLogsStatus(t("miniapp.logs.err_no_ws_token", "ws-токен не получен"), false);
      scheduleLogsReconnect();
      return;
    }

    const ws = new WebSocket(buildLogsWsUrl(ticket));
    state.logsSocket = ws;

    ws.onopen = () => {
      if (state.logsSocket !== ws) return;
      state.logsReconnectAttempts = 0;
      clearLogsReconnectTimer();
      setLogsStatus(t("miniapp.logs.ws_connected", "Поток логов подключен"));
    };

    ws.onmessage = (event) => {
      if (state.logsSocket !== ws) return;
      let payload = {};
      try {
        payload = JSON.parse(event.data || "{}");
      } catch {
        return;
      }
      const type = String(payload.type || "");
      if (type === "snapshot") {
        renderLogsEntries(payload.entries || [], { replace: true });
        return;
      }
      if (type === "append") {
        renderLogsEntries(payload.entries || [], { replace: false });
        return;
      }
      if (type === "error") {
        setLogsStatus(String(payload.error || t("miniapp.logs.ws_error", "Ошибка потока логов")), false);
        return;
      }
      if (type === "keepalive") {
        return;
      }
    };

    ws.onerror = () => {
      if (state.logsSocket !== ws) return;
      setLogsStatus(t("miniapp.logs.ws_conn_error", "Ошибка websocket-подключения"), false);
    };

    ws.onclose = (event) => {
      if (state.logsSocket !== ws) return;
      state.logsSocket = null;
      const isNormal = Boolean(event && event.wasClean && Number(event.code) === 1000);
      if (isNormal) {
        setLogsStatus(t("miniapp.logs.ws_disconnected", "Поток логов отключен"), true);
        return;
      }
      const code = Number(event?.code || 0);
      const reason = String(event?.reason || "").trim();
      const details = reason ? ` (code=${code}, reason=${reason})` : ` (code=${code})`;
      setLogsStatus(`${t("miniapp.logs.ws_disconnected_error", "Поток логов отключен с ошибкой")}${details}`, false);
      scheduleLogsReconnect();
    };
  }

  async function connectStatusWs() {
    disconnectStatusWs({ manual: false });
    state.statusShouldReconnect = true;
    setStatus(t("miniapp.status.ws_connecting", "Подключение к потоку статуса..."));
    let ticket = "";
    try {
      const payload = await api("/status/ws_ticket");
      ticket = String(payload.ticket || "");
    } catch (err) {
      const status = Number(err && err.status);
      const isAuthError = status === 401 || status === 403;
      if (isAuthError) {
        state.statusShouldReconnect = false;
        state.statusReconnectAttempts = 0;
        setStatus(`${t("miniapp.status.err_ws_token", "Ошибка получения status ws-токена:")} ${err.message || "unknown"}. ${t("miniapp.logs.err_reopen", "Откройте MiniApp заново.")}`);
        return;
      }
      setStatus(`${t("miniapp.status.err_ws_token", "Ошибка получения status ws-токена:")} ${err.message || "unknown"}`);
      scheduleStatusReconnect();
      return;
    }
    if (!ticket) {
      setStatus(t("miniapp.status.err_no_ws_token", "status ws-токен не получен"));
      scheduleStatusReconnect();
      return;
    }

    const ws = new WebSocket(buildStatusWsUrl(ticket));
    state.statusSocket = ws;

    ws.onopen = () => {
      if (state.statusSocket !== ws) return;
      state.statusReconnectAttempts = 0;
      clearStatusReconnectTimer();
      if (!state.statusLastPayload) {
        setStatus(t("miniapp.status.ws_connected_waiting", "Поток статуса подключен, ожидание данных..."));
      }
    };

    ws.onmessage = (event) => {
      if (state.statusSocket !== ws) return;
      let payload = {};
      try {
        payload = JSON.parse(event.data || "{}");
      } catch {
        return;
      }
      const type = String(payload.type || "");
      if (type === "snapshot" || type === "update") {
        renderStatusPayload(payload.status || {});
        return;
      }
      if (type === "error") {
        setStatus(String(payload.error || t("miniapp.status.ws_error", "Ошибка потока статуса")));
        return;
      }
      if (type === "keepalive") {
        return;
      }
    };

    ws.onerror = () => {
      if (state.statusSocket !== ws) return;
      setStatus(t("miniapp.status.ws_conn_error", "Ошибка websocket-подключения статуса"));
    };

    ws.onclose = (event) => {
      if (state.statusSocket !== ws) return;
      state.statusSocket = null;
      const isNormal = Boolean(event && event.wasClean && Number(event.code) === 1000);
      if (isNormal) {
        setStatus(t("miniapp.status.ws_disconnected", "Поток статуса отключен"));
        return;
      }
      const code = Number(event?.code || 0);
      const reason = String(event?.reason || "").trim();
      const details = reason ? ` (code=${code}, reason=${reason})` : ` (code=${code})`;
      setStatus(`${t("miniapp.status.ws_disconnected_error", "Поток статуса отключен с ошибкой")}${details}`);
      scheduleStatusReconnect();
    };
  }

  function renderLogsMeta(meta) {
    state.logsMeta = meta || {};
    const type = document.getElementById("logsType");
    const session = document.getElementById("logsSession");
    const logTypes = Array.isArray(state.logsMeta.log_types) ? state.logsMeta.log_types : [];
    if (!logTypes.length) {
      throw new Error("Сервер не вернул список типов логов");
    }
    if (type) {
      const options = logTypes
        .map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(t(`log_type.${item.id}`, item.label || item.id))}</option>`)
        .join("");
      type.innerHTML = options;
      if (state.logsType) {
        type.value = state.logsType;
      }
      if (!type.value && logTypes[0]) {
        type.value = String(logTypes[0].id || "main");
      }
    }
    if (session) {
      const allLabel = state.me && state.me.is_admin ? t("miniapp.logs.all_sessions", "Все сессии") : t("miniapp.logs.my_sessions", "Все мои сессии");
      const sessions = Array.isArray(state.logsMeta.sessions) ? state.logsMeta.sessions : [];
      const options = sessions
        .map((item) => {
          const value = buildLogsSessionSelectionValue(item.session_uid, item.session_id);
          return `<option value="${escapeHtml(value)}">${escapeHtml(item.label || item.session_uid)}</option>`;
        })
        .join("");
      session.innerHTML = `<option value="">${escapeHtml(allLabel)}</option>${options}`;
      if (state.logsSessionKey) {
        session.value = state.logsSessionKey;
      } else if (state.logsSessionUid) {
        session.value = buildLogsSessionSelectionValue(state.logsSessionUid, state.logsSessionId);
      }
    }
    applyLogsStateFromControls();
  }

  async function loadLogsMeta() {
    const meta = await api("/logs/meta");
    renderLogsMeta(meta);
  }

  function scheduleEditorResize() {
    window.requestAnimationFrame(() => {
      try {
        editor.resize(true);
      } catch {}
    });
  }

  function switchTab(tab) {
    const navTab = tab === "editor" ? "files" : tab;
    document.querySelectorAll(".tabs button").forEach((b) => {
      const isActive = b.dataset.tab === navTab;
      b.classList.toggle("active", isActive);
      b.setAttribute("aria-selected", isActive ? "true" : "false");
      b.setAttribute("tabindex", isActive ? "0" : "-1");
    });
    document.querySelectorAll(".tab").forEach((el) => {
      const isActive = el.id === `tab-${tab}`;
      el.classList.toggle("active", isActive);
      el.setAttribute("aria-hidden", isActive ? "false" : "true");
    });
    if (tab === "editor") {
      scheduleEditorResize();
    }
    if (tab === "admin") {
      startAdminPolling();
    } else {
      stopAdminPolling();
    }
    if (tab === "status") {
      startRunsPolling();
      void fetchRuns();
    } else {
      stopRunsPolling();
    }
    if (tab === "files") {
      void loadTree(state.currentDir);
    }
    if (tab === "scheduler") {
      void fetchSchedulerJobs();
    }
    if (tab === "settings") {
      void fetchSessionSettings();
    }
    if (tab === "tasks") {
      void fetchTasks();
    }
    if (tab === "reports") {
      void fetchReports();
    }
  }


  function dirtySections() {
    if (!state.savedConfig || !state.draft) return [];
    const names = [
      "telegram",
      "defaults",
      "tools",
      "mcp",
      "mcp_clients",
      "presets",
      "miniapp",
      "thread_mode",
      "webhooks",
      "scheduler",
      "security",
      "lint_evolution",
    ];
    return names.filter((name) => JSON.stringify(state.savedConfig[name]) !== JSON.stringify(state.draft[name]));
  }

  function isFiniteNumber(value) {
    return Number.isFinite(Number(value));
  }

  function validateClientDraft() {
    const cfg = state.draft;
    const errors = [];
    if (!cfg || typeof cfg !== "object") {
      return [t("miniapp.cfg.err_draft_not_loaded", "Черновик конфига не загружен")];
    }

    if (!String(cfg.telegram?.token || "").trim()) {
      errors.push(t("miniapp.cfg.err_telegram_token", "telegram.token обязателен"));
    }
    if (!String(cfg.defaults?.workdir || "").trim()) {
      errors.push(t("miniapp.cfg.err_defaults_workdir", "defaults.workdir обязателен"));
    }
    if (!["headless", "tmux"].includes(String(cfg.defaults?.default_execution_backend || "headless"))) {
      errors.push(t("miniapp.cfg.err_default_backend", "defaults.default_execution_backend должен быть headless или tmux"));
    }
    if (!String(cfg.miniapp?.base_path || "").trim()) {
      errors.push(t("miniapp.cfg.err_miniapp_base_path", "miniapp.base_path обязателен"));
    }
    if (!String(cfg.miniapp?.bind_host || "").trim()) {
      errors.push(t("miniapp.cfg.err_miniapp_bind_host", "miniapp.bind_host обязателен"));
    }
    if (!isFiniteNumber(cfg.miniapp?.bind_port) || Number(cfg.miniapp.bind_port) <= 0) {
      errors.push(t("miniapp.cfg.err_miniapp_bind_port", "miniapp.bind_port должен быть числом > 0"));
    }
    Object.entries(cfg.tools || {}).forEach(([toolName, tool]) => {
      if (!tool || typeof tool !== "object") {
        errors.push(`tools.${toolName}: ${t("miniapp.cfg.err_tool_bad_struct", "неверная структура")}`);
        return;
      }
      if (!["headless", "interactive"].includes(String(tool.mode || ""))) {
        errors.push(`tools.${toolName}.mode ${t("miniapp.cfg.err_tool_mode", "должен быть headless или interactive")}`);
      }
      const executionBackends = Array.isArray(tool.execution_backends)
        ? tool.execution_backends.map((x) => String(x).trim()).filter(Boolean)
        : [];
      const validBackends = ["headless", "tmux"];
      executionBackends.forEach((backend) => {
        if (!validBackends.includes(backend)) {
          errors.push(`tools.${toolName}.execution_backends ${t("miniapp.cfg.err_backend", "должен содержать только headless/tmux")}`);
        }
      });
      if (new Set(executionBackends).size !== executionBackends.length) {
        errors.push(`tools.${toolName}.execution_backends ${t("miniapp.cfg.err_backend_dupes", "не должен содержать дубликаты")}`);
      }
      const toolDefaultBackend = String(tool.default_execution_backend || "").trim();
      if (toolDefaultBackend && (!executionBackends.length || !executionBackends.includes(toolDefaultBackend))) {
        errors.push(`tools.${toolName}.default_execution_backend ${t("miniapp.cfg.err_backend_default", "должен входить в execution_backends")}`);
      }
      if (tool.tmux_user != null && typeof tool.tmux_user !== "string") {
        errors.push(`tools.${toolName}.tmux_user ${t("miniapp.cfg.err_string", "должен быть строкой")}`);
      }
      const interactiveCmd = Array.isArray(tool.interactive_cmd)
        ? tool.interactive_cmd.map((x) => String(x).trim()).filter(Boolean)
        : [];
      if (executionBackends.includes("tmux") && !interactiveCmd.length) {
        errors.push(`tools.${toolName}.interactive_cmd ${t("miniapp.cfg.err_tmux_interactive_cmd", "обязателен для tmux backend")}`);
      }
      if (tool.interactive_resume_cmd != null && !Array.isArray(tool.interactive_resume_cmd)) {
        errors.push(`tools.${toolName}.interactive_resume_cmd ${t("miniapp.cfg.err_array", "должен быть списком")}`);
      }
      const cmd = Array.isArray(tool.cmd) ? tool.cmd.map((x) => String(x).trim()).filter(Boolean) : [];
      if (!cmd.length) {
        errors.push(`tools.${toolName}.cmd ${t("miniapp.cfg.err_tool_cmd_empty", "не должен быть пустым")}`);
      }
    });

    if (cfg.mcp && !isFiniteNumber(cfg.mcp.port)) {
      errors.push(t("miniapp.cfg.err_mcp_port", "mcp.port должен быть числом"));
    }

    if (!["private", "group"].includes(String(cfg.thread_mode?.mode || "private"))) {
      errors.push(t("miniapp.cfg.err_thread_mode", "thread_mode.mode должен быть private или group"));
    }
    const threadTopicsChatId = cfg.thread_mode?.topics_chat_id;
    if (
      String(cfg.thread_mode?.mode || "private") === "group"
      && (threadTopicsChatId === null || threadTopicsChatId === undefined || threadTopicsChatId === "" || !Number.isInteger(Number(threadTopicsChatId)))
    ) {
      errors.push(t("miniapp.cfg.err_thread_topics_chat_id", "thread_mode.topics_chat_id обязателен для mode=group"));
    }
    if (!String(cfg.webhooks?.path || "").trim()) {
      errors.push(t("miniapp.cfg.err_webhooks_path", "webhooks.path обязателен"));
    } else if (!String(cfg.webhooks.path).startsWith("/")) {
      errors.push(t("miniapp.cfg.err_webhooks_path_slash", "webhooks.path должен начинаться с /"));
    }
    if (!isFiniteNumber(cfg.webhooks?.request_timeout_sec) || Number(cfg.webhooks.request_timeout_sec) <= 0) {
      errors.push(t("miniapp.cfg.err_webhooks_timeout", "webhooks.request_timeout_sec должен быть > 0"));
    }
    if (!isFiniteNumber(cfg.webhooks?.max_payload_bytes) || Number(cfg.webhooks.max_payload_bytes) <= 0) {
      errors.push(t("miniapp.cfg.err_webhooks_max_payload", "webhooks.max_payload_bytes должен быть > 0"));
    }
    if (!String(cfg.scheduler?.timezone || "").trim()) {
      errors.push(t("miniapp.cfg.err_scheduler_timezone", "scheduler.timezone обязателен"));
    }
    ["tick_interval_sec", "max_concurrent_jobs", "job_timeout_sec"].forEach((field) => {
      if (!isFiniteNumber(cfg.scheduler?.[field]) || Number(cfg.scheduler[field]) <= 0) {
        errors.push(`scheduler.${field} ${t("miniapp.cfg.err_must_be_positive", "должен быть > 0")}`);
      }
    });
    if (!isFiniteNumber(cfg.scheduler?.misfire_grace_sec) || Number(cfg.scheduler.misfire_grace_sec) < 0) {
      errors.push(t("miniapp.cfg.err_scheduler_misfire", "scheduler.misfire_grace_sec должен быть >= 0"));
    }
    const rateLimits = cfg.security?.rate_limits || {};
    if (!["sqlite"].includes(String(rateLimits.backend || "sqlite"))) {
      errors.push(t("miniapp.cfg.err_rate_limit_backend", "security.rate_limits.backend должен быть sqlite"));
    }
    if (rateLimits.enabled) {
      const hasDefault = !!(rateLimits.default && typeof rateLimits.default === "object");
      const hasPolicies = !!(rateLimits.policies && typeof rateLimits.policies === "object" && Object.keys(rateLimits.policies).length);
      if (!hasDefault && !hasPolicies) {
        errors.push(t("miniapp.cfg.err_rate_limit_policy", "security.rate_limits.default или security.rate_limits.policies обязателен при enabled=true"));
      }
    }
    const lintEvolution = cfg.lint_evolution || {};
    [
      "level1_cooldown_hours",
      "level2_cooldown_hours",
      "level3_cooldown_hours",
      "error_retry_hours",
      "fp_growth_threshold_pct",
    ].forEach((field) => {
      if (!isFiniteNumber(lintEvolution[field]) || Number(lintEvolution[field]) < 0) {
        errors.push(`lint_evolution.${field} ${t("miniapp.cfg.err_must_be_nonneg", "должен быть >= 0")}`);
      }
    });
    ["lock_ttl_minutes", "canary_rolling_days", "canary_baseline_days"].forEach((field) => {
      if (!isFiniteNumber(lintEvolution[field]) || Number(lintEvolution[field]) <= 0) {
        errors.push(`lint_evolution.${field} ${t("miniapp.cfg.err_must_be_positive", "должен быть > 0")}`);
      }
    });
    if (
      !Number.isInteger(Number(lintEvolution.canary_max_schema_fields_per_180d))
      || Number(lintEvolution.canary_max_schema_fields_per_180d) < 0
    ) {
      errors.push(t("miniapp.cfg.err_lint_canary_max", "lint_evolution.canary_max_schema_fields_per_180d должен быть целым >= 0"));
    }

    (cfg.mcp_clients || []).forEach((client, idx) => {
      const prefix = `mcp_clients[${idx}]`;
      if (!String(client?.name || "").trim()) {
        errors.push(`${prefix}.name ${t("miniapp.cfg.err_field_required", "обязателен")}`);
      }
      const transport = String(client?.transport || "stdio");
      if (!["stdio", "http"].includes(transport)) {
        errors.push(`${prefix}.transport ${t("miniapp.cfg.err_transport", "должен быть stdio или http")}`);
      }
      if (!isFiniteNumber(client?.timeout_ms) || Number(client.timeout_ms) <= 0) {
        errors.push(`${prefix}.timeout_ms ${t("miniapp.cfg.err_must_be_positive", "должен быть > 0")}`);
      }
      if (transport === "stdio") {
        const cmd = Array.isArray(client?.cmd) ? client.cmd.map((x) => String(x).trim()).filter(Boolean) : [];
        if (!cmd.length) {
          errors.push(`${prefix}.cmd ${t("miniapp.cfg.err_cmd_empty_stdio", "не должен быть пустым для transport=stdio")}`);
        }
      }
      if (transport === "http" && !String(client?.url || "").trim()) {
        errors.push(`${prefix}.url ${t("miniapp.cfg.err_url_required_http", "обязателен для transport=http")}`);
      }
    });

    (cfg.presets || []).forEach((preset, idx) => {
      if (!String(preset?.name || "").trim()) {
        errors.push(`presets[${idx}].name ${t("miniapp.cfg.err_field_required", "обязателен")}`);
      }
      if (!String(preset?.prompt || "").trim()) {
        errors.push(`presets[${idx}].prompt ${t("miniapp.cfg.err_field_required", "обязателен")}`);
      }
    });

    return errors;
  }

  function renderValidationErrors(errors, source = t("miniapp.cfg.local_validation", "Локальная валидация")) {
    const el = document.getElementById("cfgValidation");
    if (!errors.length) {
      el.innerHTML = "";
      return;
    }
    const items = errors.map((e) => `• ${escapeHtml(e)}`).join("<br>");
    el.innerHTML = `<div class="status-error"><strong>${escapeHtml(source)}</strong><br>${items}</div>`;
  }

  function renderDirty() {
    const sections = dirtySections();
    const el = document.getElementById("cfgDirty");
    const clientErrors = validateClientDraft();
    if (!sections.length) {
      el.textContent = t("miniapp.cfg.no_changes", "Изменений нет");
      document.getElementById("cfgSave").disabled = true;
      return;
    }
    el.textContent = `${t("miniapp.cfg.changed_sections", "Изменены секции")}: ${sections.join(", ")}`;
    document.getElementById("cfgSave").disabled = clientErrors.length > 0;
  }

  function kvToLines(obj) {
    const out = [];
    Object.entries(obj || {}).forEach(([k, v]) => {
      out.push(`${k}=${Array.isArray(v) ? v.join(",") : String(v)}`);
    });
    return out.join("\n");
  }

  function linesToKv(txt, parseArray = false) {
    const out = {};
    (txt || "")
      .split("\n")
      .map((l) => l.trim())
      .filter(Boolean)
      .forEach((line) => {
        const idx = line.indexOf("=");
        if (idx < 1) return;
        const key = line.slice(0, idx).trim();
        const val = line.slice(idx + 1).trim();
        if (!key) return;
        out[key] = parseArray ? val.split(",").map((x) => x.trim()).filter(Boolean) : val;
      });
    return out;
  }

  function arrToMultiline(arr) {
    return (arr || []).join("\n");
  }

  function multilineToStrArr(txt) {
    return (txt || "")
      .split("\n")
      .map((x) => x.trim())
      .filter(Boolean);
  }

  function multilineToIntArr(txt) {
    return (txt || "")
      .split("\n")
      .map((x) => x.trim())
      .filter(Boolean)
      .map((x) => Number(x))
      .filter((x) => Number.isInteger(x));
  }

  function userModesToLines(map) {
    return Object.entries(map || {})
      .sort(([left], [right]) => String(left).localeCompare(String(right)))
      .map(([chatId, value]) => {
        if (String(value || "").trim() === "all") {
          return `${chatId}=all`;
        }
        const items = Array.isArray(value) ? value.map((item) => String(item).trim()).filter(Boolean) : [];
        return items.length ? `${chatId}=${items.join(",")}` : "";
      })
      .filter(Boolean)
      .join("\n");
  }

  function linesToUserModes(txt) {
    const out = {};
    (txt || "")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
      .forEach((line) => {
        const idx = line.indexOf("=");
        if (idx < 1) return;
        const chatId = line.slice(0, idx).trim();
        const rawValue = line.slice(idx + 1).trim();
        if (!chatId || !rawValue) return;
        if (rawValue === "all") {
          out[chatId] = "all";
          return;
        }
        const modes = rawValue.split(",").map((item) => item.trim()).filter(Boolean);
        if (modes.length) {
          out[chatId] = modes;
        }
      });
    return out;
  }

  function optionalNumber(value) {
    const txt = String(value ?? "").trim();
    if (!txt) return null;
    const parsed = Number(txt);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function jsonObjectToText(value) {
    if (!value || typeof value !== "object" || Array.isArray(value) || !Object.keys(value).length) {
      return "";
    }
    return JSON.stringify(value, null, 2);
  }

  function parseJsonObjectText(value) {
    const txt = String(value || "").trim();
    if (!txt) {
      return { ok: true, value: null };
    }
    try {
      const parsed = JSON.parse(txt);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        return { ok: false, value: null };
      }
      return { ok: true, value: parsed };
    } catch (_error) {
      return { ok: false, value: null };
    }
  }

  function optionalText(v) {
    const txt = String(v || "").trim();
    return txt ? txt : null;
  }

  function redactionSentinel() {
    const redaction = state.redaction && typeof state.redaction === "object" ? state.redaction : {};
    return redaction.sentinel || SECRET_UNCHANGED_SENTINEL;
  }

  function configSecretPaths() {
    const redaction = state.redaction && typeof state.redaction === "object" ? state.redaction : {};
    const fields = Array.isArray(redaction.fields) ? redaction.fields : [];
    return Array.from(new Set([...Object.values(SECRET_INPUT_PATHS), ...fields].map((path) => String(path || "")).filter(Boolean))).sort();
  }

  function getConfigPathValue(root, path) {
    let current = root;
    const parts = String(path || "").split(".").filter(Boolean);
    for (const part of parts) {
      if (!current || typeof current !== "object" || !(part in current)) {
        return undefined;
      }
      current = current[part];
    }
    return current;
  }

  function originalSecretValue(path) {
    const original = getConfigPathValue(state.savedConfig, path);
    return original === undefined ? null : original;
  }

  function secretInputDisplayValue(path, value) {
    if (value === null || value === undefined || value === redactionSentinel()) {
      return "";
    }
    return value === originalSecretValue(path) ? "" : String(value);
  }

  function changedSecretPaths() {
    const sentinel = redactionSentinel();
    return configSecretPaths().filter((path) => {
      const original = getConfigPathValue(state.savedConfig, path);
      const draft = getConfigPathValue(state.draft, path);
      if (original === sentinel) {
        return draft !== sentinel;
      }
      if (original === null || original === undefined || original === "") {
        return draft !== null && draft !== undefined && draft !== "" && draft !== sentinel;
      }
      return draft !== original;
    });
  }

  async function confirmSecretChangesBeforeSave() {
    const paths = changedSecretPaths();
    if (!paths.length) {
      return true;
    }
    return uiConfirm(t("miniapp.cfg.secret_changed_confirm", "Изменены или очищены secret-поля: {paths}. Продолжить сохранение?").replace("{paths}", paths.join(", ")));
  }

  function optionalMap(obj) {
    return Object.keys(obj || {}).length ? obj : null;
  }

  function optionalList(arr) {
    return (arr || []).length ? arr : null;
  }

  function toIdSafe(value) {
    return String(value || "")
      .toLowerCase()
      .replace(/[^a-z0-9_-]+/g, "-")
      .replace(/^-+|-+$/g, "");
  }

  function escapeHtml(value) {
    return String(value || "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function bindInput(id, getter, setter) {
    const el = document.getElementById(id);
    if (!el) return;
    if (el.type === "checkbox") {
      el.checked = !!getter();
      el.onchange = () => {
        setter(el.checked);
        renderDirty();
      };
      return;
    }
    el.value = getter() ?? "";
    el.oninput = () => {
      setter(el.value);
      renderDirty();
    };
  }

  function bindSecretInput(id, path, getter, setter) {
    const el = document.getElementById(id);
    if (!el) return;
    el.type = "password";
    el.setAttribute("autocomplete", "new-password");
    el.setAttribute("data-secret-path", path);
    el.value = secretInputDisplayValue(path, getter());
    el.oninput = () => {
      const txt = String(el.value || "").trim();
      setter(txt ? txt : originalSecretValue(path));
      renderDirty();
    };
    const clearEl = document.getElementById(`${id}-clear`);
    if (clearEl) {
      clearEl.onclick = (event) => {
        if (event && typeof event.preventDefault === "function") {
          event.preventDefault();
        }
        setter(null);
        el.value = "";
        renderDirty();
      };
    }
  }

  function bindJsonArea(id, getter, setter, label) {
    const el = document.getElementById(id);
    if (!el) return;
    el.value = jsonObjectToText(getter());
    el.onchange = () => {
      const parsed = parseJsonObjectText(el.value);
      if (!parsed.ok) {
        uiAlert(`${label} ${t("miniapp.cfg.err_must_be_json_object", "должен быть JSON-объектом")}`);
        el.value = jsonObjectToText(getter());
        return;
      }
      setter(parsed.value);
      renderDirty();
    };
  }

  function fieldHtml({ id, label, hint, kind = "text", placeholder = "", options = [], readonly = false, clearable = false }) {
    if (kind === "checkbox") {
      return `<div class="field"><label>${label}</label><input id="${id}" type="checkbox" /><small>${hint || ""}</small></div>`;
    }
    if (kind === "select") {
      const optionsHtml = options
        .map((option) => `<option value="${escapeHtml(option)}">${escapeHtml(option)}</option>`)
        .join("");
      return `<div class="field"><label>${label}</label><select id="${id}">${optionsHtml}</select><small>${hint || ""}</small></div>`;
    }
    if (kind === "textarea") {
      return `<div class="field"><label>${label}</label><textarea id="${id}" placeholder="${placeholder}"></textarea><small>${hint || ""}</small></div>`;
    }
    const inputHtml = `<input id="${id}" type="${kind}" placeholder="${placeholder}" ${readonly ? "readonly" : ""} />`;
    if (clearable) {
      return `<div class="field"><label>${label}</label><div class="secret-input-row">${inputHtml}<button type="button" id="${id}-clear" class="btn-sm">${escapeHtml(t("miniapp.btn.clear", "Очистить"))}</button></div><small>${hint || ""}</small></div>`;
    }
    return `<div class="field"><label>${label}</label>${inputHtml}<small>${hint || ""}</small></div>`;
  }

  function sectionHtml({ key, title, inner, actions = "" }) {
    return `<div class="section-header"><span>${title}</span><span class="section-actions">${actions}<button type="button" data-reset="${key}">${escapeHtml(t("miniapp.cfg.reset_section", "Сбросить секцию"))}</button></span></div><div class="section-body">${inner}</div>`;
  }

  function normalizeConfigRoot(config) {
    const cfg = config && typeof config === "object" ? config : {};
    cfg.telegram = cfg.telegram || {};
    cfg.defaults = cfg.defaults || {};
    cfg.tools = cfg.tools || {};
    cfg.mcp = cfg.mcp || {};
    cfg.mcp_clients = Array.isArray(cfg.mcp_clients) ? cfg.mcp_clients : [];
    cfg.presets = Array.isArray(cfg.presets) ? cfg.presets : [];
    cfg.miniapp = cfg.miniapp || {};
    cfg.thread_mode = cfg.thread_mode || {};
    cfg.webhooks = cfg.webhooks || {};
    cfg.scheduler = cfg.scheduler || {};
    cfg.security = cfg.security || {};
    cfg.security.rate_limits = cfg.security.rate_limits || {};
    cfg.security.content_screening = cfg.security.content_screening || {};
    cfg.lint_evolution = cfg.lint_evolution || {};
    cfg.defaults.pending_input_confirmation_enabled =
      cfg.defaults.pending_input_confirmation_enabled !== undefined ? !!cfg.defaults.pending_input_confirmation_enabled : true;
    cfg.defaults.default_execution_backend = ["headless", "tmux"].includes(String(cfg.defaults.default_execution_backend || ""))
      ? String(cfg.defaults.default_execution_backend)
      : "headless";
    if (!String(cfg.miniapp.bind_host || "").trim()) {
      cfg.miniapp.bind_host = "127.0.0.1";
    }
    if (!Number.isFinite(Number(cfg.miniapp.bind_port)) || Number(cfg.miniapp.bind_port) <= 0) {
      cfg.miniapp.bind_port = 8088;
    }
    cfg.thread_mode.enabled = cfg.thread_mode.enabled !== undefined ? !!cfg.thread_mode.enabled : true;
    cfg.thread_mode.mode = cfg.thread_mode.mode || "private";
    if (!Number.isFinite(Number(cfg.thread_mode.inactivity_ttl_sec)) || Number(cfg.thread_mode.inactivity_ttl_sec) <= 0) {
      cfg.thread_mode.inactivity_ttl_sec = 86400;
    }
    cfg.webhooks.enabled = cfg.webhooks.enabled !== undefined ? !!cfg.webhooks.enabled : true;
    cfg.webhooks.path = cfg.webhooks.path || "/webhooks/telegram";
    if (!Number.isFinite(Number(cfg.webhooks.request_timeout_sec)) || Number(cfg.webhooks.request_timeout_sec) <= 0) {
      cfg.webhooks.request_timeout_sec = 30;
    }
    if (!Number.isFinite(Number(cfg.webhooks.max_payload_bytes)) || Number(cfg.webhooks.max_payload_bytes) <= 0) {
      cfg.webhooks.max_payload_bytes = 1048576;
    }
    cfg.scheduler.enabled = cfg.scheduler.enabled !== undefined ? !!cfg.scheduler.enabled : false;
    cfg.scheduler.timezone = cfg.scheduler.timezone || "UTC";
    if (!Number.isFinite(Number(cfg.scheduler.tick_interval_sec)) || Number(cfg.scheduler.tick_interval_sec) <= 0) {
      cfg.scheduler.tick_interval_sec = 60;
    }
    if (!Number.isFinite(Number(cfg.scheduler.max_concurrent_jobs)) || Number(cfg.scheduler.max_concurrent_jobs) <= 0) {
      cfg.scheduler.max_concurrent_jobs = 1;
    }
    if (!Number.isFinite(Number(cfg.scheduler.job_timeout_sec)) || Number(cfg.scheduler.job_timeout_sec) <= 0) {
      cfg.scheduler.job_timeout_sec = 3600;
    }
    if (!Number.isFinite(Number(cfg.scheduler.misfire_grace_sec)) || Number(cfg.scheduler.misfire_grace_sec) < 0) {
      cfg.scheduler.misfire_grace_sec = 30;
    }
    cfg.security.rate_limits.enabled = cfg.security.rate_limits.enabled !== undefined ? !!cfg.security.rate_limits.enabled : false;
    cfg.security.rate_limits.backend = cfg.security.rate_limits.backend || "sqlite";
    cfg.security.content_screening.enabled = cfg.security.content_screening.enabled !== undefined ? !!cfg.security.content_screening.enabled : false;
    cfg.security.content_screening.mode = ["warn", "block"].includes(String(cfg.security.content_screening.mode || ""))
      ? String(cfg.security.content_screening.mode)
      : "warn";
    if (!Number.isFinite(Number(cfg.security.content_screening.max_chars)) || Number(cfg.security.content_screening.max_chars) <= 0) {
      cfg.security.content_screening.max_chars = 16000;
    }
    if (!Number.isFinite(Number(cfg.security.content_screening.timeout_ms)) || Number(cfg.security.content_screening.timeout_ms) <= 0) {
      cfg.security.content_screening.timeout_ms = 8000;
    }
    cfg.lint_evolution.enabled = cfg.lint_evolution.enabled !== undefined ? !!cfg.lint_evolution.enabled : false;
    if (!Number.isFinite(Number(cfg.lint_evolution.level1_cooldown_hours)) || Number(cfg.lint_evolution.level1_cooldown_hours) < 0) {
      cfg.lint_evolution.level1_cooldown_hours = 24;
    }
    if (!Number.isFinite(Number(cfg.lint_evolution.level2_cooldown_hours)) || Number(cfg.lint_evolution.level2_cooldown_hours) < 0) {
      cfg.lint_evolution.level2_cooldown_hours = 720;
    }
    if (!Number.isFinite(Number(cfg.lint_evolution.level3_cooldown_hours)) || Number(cfg.lint_evolution.level3_cooldown_hours) < 0) {
      cfg.lint_evolution.level3_cooldown_hours = 720;
    }
    if (!Number.isFinite(Number(cfg.lint_evolution.lock_ttl_minutes)) || Number(cfg.lint_evolution.lock_ttl_minutes) <= 0) {
      cfg.lint_evolution.lock_ttl_minutes = 30;
    }
    if (!Number.isFinite(Number(cfg.lint_evolution.error_retry_hours)) || Number(cfg.lint_evolution.error_retry_hours) < 0) {
      cfg.lint_evolution.error_retry_hours = 1;
    }
    if (!Number.isFinite(Number(cfg.lint_evolution.fp_growth_threshold_pct)) || Number(cfg.lint_evolution.fp_growth_threshold_pct) < 0) {
      cfg.lint_evolution.fp_growth_threshold_pct = 50;
    }
    if (!Number.isFinite(Number(cfg.lint_evolution.canary_rolling_days)) || Number(cfg.lint_evolution.canary_rolling_days) <= 0) {
      cfg.lint_evolution.canary_rolling_days = 7;
    }
    if (!Number.isFinite(Number(cfg.lint_evolution.canary_baseline_days)) || Number(cfg.lint_evolution.canary_baseline_days) <= 0) {
      cfg.lint_evolution.canary_baseline_days = 30;
    }
    if (
      !Number.isInteger(Number(cfg.lint_evolution.canary_max_schema_fields_per_180d))
      || Number(cfg.lint_evolution.canary_max_schema_fields_per_180d) < 0
    ) {
      cfg.lint_evolution.canary_max_schema_fields_per_180d = 3;
    }
    return cfg;
  }

  function renderConfigForm() {
    const cfg = normalizeConfigRoot(state.draft);
    state.draft = cfg;

    const root = document.getElementById("cfgSections");
    root.innerHTML = "";
    const toolNames = Object.keys(cfg.tools || {}).sort();

    const sections = [
      {
        key: "telegram",
        title: "telegram",
        inner: [
          fieldHtml({ id: "tg-token", label: "token", hint: "Токен Telegram бота", kind: "password", clearable: true }),
          fieldHtml({ id: "tg-whitelist", label: "whitelist_chat_ids", hint: "По одному chat_id в строке", kind: "textarea" }),
          fieldHtml({ id: "tg-admins", label: "admlist_chat_ids", hint: "По одному chat_id в строке", kind: "textarea" }),
          fieldHtml({ id: "tg-user-workdirs", label: "user_workdirs", hint: "chat_id=/path1,/path2", kind: "textarea" }),
          fieldHtml({ id: "tg-user-modes", label: "user_modes", hint: "chat_id=all или chat_id=agent,analyst,sdd,direct_cli,orchestrator", kind: "textarea" }),
          fieldHtml({ id: "tg-conn-pool", label: "connection_pool_size", kind: "number", hint: "restart required" }),
          fieldHtml({ id: "tg-connect-timeout", label: "connect_timeout_sec", kind: "number", hint: "restart required" }),
          fieldHtml({ id: "tg-read-timeout", label: "read_timeout_sec", kind: "number", hint: "restart required" }),
          fieldHtml({ id: "tg-write-timeout", label: "write_timeout_sec", kind: "number", hint: "restart required" }),
          fieldHtml({ id: "tg-pool-timeout", label: "pool_timeout_sec", kind: "number", hint: "restart required" }),
          fieldHtml({ id: "tg-poll-timeout", label: "polling_timeout_sec", kind: "number", hint: "restart required" }),
          fieldHtml({ id: "tg-poll-interval", label: "poll_interval_sec", kind: "number", hint: "restart required" }),
        ].join(""),
      },
      {
        key: "defaults",
        title: "defaults",
        inner: (() => {
          const subTabs = [
            { key: "general", title: t("miniapp.cfg.subtab_general", "Общие") },
            { key: "paths", title: t("miniapp.cfg.subtab_paths", "Пути и хранение") },
            { key: "apikeys", title: t("miniapp.cfg.subtab_apikeys", "API-ключи") },
            { key: "orchestration", title: t("miniapp.cfg.subtab_orchestration", "Оркестрация") },
            { key: "skills", title: t("miniapp.cfg.subtab_skills", "Навыки") },
            { key: "runtime", title: t("miniapp.cfg.subtab_runtime", "Runtime и LLM") },
          ];
          const ast = state.activeDefaultsSubTab || "general";
          const tabsBar = `<div class="defaults-sub-tabs">${subTabs.map((t) =>
            `<button type="button" data-defaults-subtab="${t.key}" class="${t.key === ast ? "active" : ""}">${t.title}</button>`
          ).join("")}</div>`;

          const groups = {
            general: [
              fieldHtml({ id: "def-workdir", label: "workdir", hint: "Рабочая директория по умолчанию для новых сессий" }),
              fieldHtml({ id: "def-default-cli", label: "default_cli", hint: "CLI-агент по умолчанию (qwen, codex, gemini, claude, grok, kimi)" }),
              fieldHtml({ id: "def-idle-timeout", label: "idle_timeout_sec", kind: "number", hint: "Таймаут простоя до очистки сессии (сек)" }),
              fieldHtml({ id: "def-summary-max", label: "summary_max_chars", kind: "number", hint: "Макс. размер summary (символы)" }),
              fieldHtml({ id: "def-html-prefix", label: "html_filename_prefix", hint: "Префикс для генерируемых HTML-артефактов" }),
              fieldHtml({ id: "def-codebase-mapper-usage", label: "codebase_mapper_usage", kind: "select", options: ["auto", "enabled", "disabled"], hint: "Режим работы codebase mapper" }),
              fieldHtml({ id: "def-clarification-enabled", label: "clarification_enabled", kind: "checkbox", hint: "Включить запросы на уточнение" }),
              fieldHtml({ id: "def-pending-input-confirmation-enabled", label: "pending_input_confirmation_enabled", kind: "checkbox", hint: "Сначала подтверждать любое новое сообщение; после подтверждения busy-сессия отдельно спрашивает о постановке в очередь" }),
              fieldHtml({ id: "def-clarification-keywords", label: "clarification_keywords", kind: "textarea", hint: "Ключевые слова, по одному в строке" }),
            ].join(""),
            paths: [
              fieldHtml({ id: "def-state-path", label: "state_path", hint: "Файл состояния сессий бота" }),
              fieldHtml({ id: "def-desktop-state-path", label: "desktop_state_path", hint: "Файл состояния desktop-приложения" }),
              fieldHtml({ id: "def-toolhelp-path", label: "toolhelp_path", hint: "Кэш tool help" }),
              fieldHtml({ id: "def-log-path", label: "log_path", hint: "Путь к основному лог-файлу" }),
              fieldHtml({ id: "def-image-temp", label: "image_temp_dir", hint: "Директория для загружаемых изображений" }),
              fieldHtml({ id: "def-image-max", label: "image_max_mb", kind: "number", hint: "Макс. размер изображения (МиБ)" }),
              fieldHtml({ id: "def-memory-max", label: "memory_max_kb", kind: "number", hint: "Макс. размер компактной памяти (КиБ)" }),
              fieldHtml({ id: "def-memory-target", label: "memory_compact_target_kb", kind: "number", hint: "Целевой размер после компактификации (КиБ)" }),
            ].join(""),
            apikeys: [
              `<div class="subtab-description">API-ключи и модели для внешних сервисов</div>`,
              fieldHtml({ id: "def-openai-api-key", label: "openai_api_key", hint: "Ключ OpenAI API", kind: "password", clearable: true }),
              fieldHtml({ id: "def-openai-model", label: "openai_model", hint: "Модель OpenAI по умолчанию" }),
              fieldHtml({ id: "def-openai-big-model", label: "openai_big_model", hint: "Модель для суммаризации и тяжёлых задач" }),
              fieldHtml({ id: "def-openai-base-url", label: "openai_base_url", hint: "URL для OpenAI-совместимого API" }),
              fieldHtml({ id: "def-zai-key", label: "zai_api_key", hint: "Ключ Z.ai API", kind: "password", clearable: true }),
              fieldHtml({ id: "def-tavily-key", label: "tavily_api_key", hint: "Ключ Tavily API (поиск)", kind: "password", clearable: true }),
              fieldHtml({ id: "def-jina-key", label: "jina_api_key", hint: "Ключ Jina API", kind: "password", clearable: true }),
              fieldHtml({ id: "def-github-token", label: "github_token", hint: "GitHub токен для интеграций", kind: "password", clearable: true }),
              fieldHtml({ id: "def-gemini-oauth-client-secret", label: "gemini_oauth_client_secret", hint: "Gemini OAuth client secret для обновления quota credentials; restart required", kind: "password", clearable: true }),
            ].join(""),
            orchestration: [
              `<div class="subtab-description">Настройки Manager, Analyst и Webmaster режимов</div>`,
              fieldHtml({ id: "def-manager-max-tasks", label: "manager_max_tasks", kind: "number", hint: "Макс. задач при декомпозиции" }),
              fieldHtml({ id: "def-manager-max-attempts", label: "manager_max_attempts", kind: "number", hint: "Макс. попыток на задачу" }),
              fieldHtml({ id: "def-manager-decompose-timeout", label: "manager_decompose_timeout_sec", kind: "number", hint: "Таймаут декомпозиции (сек)" }),
              fieldHtml({ id: "def-manager-dev-timeout", label: "manager_dev_timeout_sec", kind: "number", hint: "Таймаут разработки (сек)" }),
              fieldHtml({ id: "def-manager-review-timeout", label: "manager_review_timeout_sec", kind: "number", hint: "Таймаут ревью (сек)" }),
              fieldHtml({ id: "def-manager-report-max", label: "manager_dev_report_max_chars", kind: "number", hint: "Макс. размер dev-отчёта (символы)" }),
              fieldHtml({ id: "def-manager-auto-resume", label: "manager_auto_resume", kind: "checkbox", hint: "Автовозобновление менеджера" }),
              fieldHtml({ id: "def-manager-auto-commit", label: "manager_auto_commit", kind: "checkbox", hint: "Git commit после каждого одобренного шага" }),
              fieldHtml({ id: "def-manager-response-archive", label: "manager_response_archive", kind: "checkbox", hint: "Сохранять сырые ответы CLI/агентов" }),
              fieldHtml({ id: "def-analyst-use-cli-timeout", label: "analyst_use_cli_timeout_sec", kind: "number", hint: "Таймаут CLI для Analyst (сек)" }),
              fieldHtml({ id: "def-webmaster-use-cli-timeout", label: "webmaster_use_cli_timeout_sec", kind: "number", hint: "Таймаут CLI для Webmaster (сек)" }),
              fieldHtml({ id: "def-webmaster-validation-max-fix-iterations", label: "webmaster_validation_max_fix_iterations", kind: "number", hint: "Макс. итераций исправления валидации" }),
              fieldHtml({ id: "def-cli-routing", label: "cli_routing", kind: "textarea", hint: "Приоритеты CLI по типу задачи: work_type=cli1,cli2" }),
            ].join(""),
            skills: [
              `<div class="subtab-description">Обнаружение, установка и управление навыками</div>`,
              fieldHtml({ id: "def-skill-discovery-mode", label: "skill_discovery_mode", kind: "select", options: ["off", "suggest", "auto"], hint: "Режим обнаружения навыков; restart required" }),
              fieldHtml({ id: "def-skill-install-policy", label: "skill_install_policy", kind: "select", options: ["manual", "admin_approve", "allowlisted_auto"], hint: "Политика установки внешних навыков; restart required" }),
              fieldHtml({ id: "def-skill-registry-paths", label: "skill_registry_paths", kind: "textarea", hint: "Локальные пути реестров навыков, по одному в строке; restart required" }),
              fieldHtml({ id: "def-skill-allowlisted-sources", label: "skill_allowlisted_sources", kind: "textarea", hint: "Разрешённые типы источников, по одному в строке; restart required" }),
            ].join(""),
            runtime: [
              `<div class="subtab-description">Артефакты, метрики, диагностика и настройки LLM</div>`,
              fieldHtml({ id: "def-run-artifacts-enabled", label: "run_artifacts_enabled", kind: "checkbox", hint: "Сохранять артефакты запусков; restart required" }),
              fieldHtml({ id: "def-run-artifacts-retention-days", label: "run_artifacts_retention_days", kind: "number", hint: "Срок хранения артефактов (дни); restart required" }),
              fieldHtml({ id: "def-memory-events-enabled", label: "memory_events_enabled", kind: "checkbox", hint: "Shadow-события памяти; restart required" }),
              fieldHtml({ id: "def-memory-native-cli-hooks-enabled", label: "memory_native_cli_hooks_enabled", kind: "checkbox", hint: "Opt-in native CLI hooks adapter; restart required" }),
              fieldHtml({ id: "def-memory-outcomes-enabled", label: "memory_outcomes_enabled", kind: "checkbox", hint: "Shadow outcome records; restart required" }),
              fieldHtml({ id: "def-memory-dreaming-enabled", label: "memory_dreaming_enabled", kind: "checkbox", hint: "Async dreaming pipeline; restart required" }),
              fieldHtml({ id: "def-memory-events-retention-days", label: "memory_events_retention_days", kind: "number", hint: "Срок хранения memory events (дни); restart required" }),
              fieldHtml({ id: "def-memory-events-max-payload-chars", label: "memory_events_max_payload_chars", kind: "number", hint: "Макс. размер payload события; restart required" }),
              fieldHtml({ id: "def-memory-events-redaction-enabled", label: "memory_events_redaction_enabled", kind: "checkbox", hint: "Редактировать секреты перед записью; restart required" }),
              fieldHtml({ id: "def-memory-dreaming-batch-size", label: "memory_dreaming_batch_size", kind: "number", hint: "Размер batch для dreaming pass; restart required" }),
              fieldHtml({ id: "def-run-doctor-enabled", label: "run_doctor_enabled", kind: "checkbox", hint: "Проверки готовности (doctor/recover); restart required" }),
              fieldHtml({ id: "def-run-boundary-validation-enabled", label: "run_boundary_validation_enabled", kind: "checkbox", hint: "Валидация границ фаз; restart required" }),
              fieldHtml({ id: "def-run-metrics-enabled", label: "run_metrics_enabled", kind: "checkbox", hint: "Сбор метрик запусков; restart required" }),
              fieldHtml({ id: "def-cli-json-stream-archive-enabled", label: "cli_json_stream_archive_enabled", kind: "checkbox", hint: "Архивировать JSON-поток CLI" }),
              fieldHtml({ id: "def-assistant-preview-enabled", label: "assistant_preview_enabled", kind: "checkbox", hint: "Превью ассистента во время выполнения" }),
              fieldHtml({ id: "def-default-execution-backend", label: "default_execution_backend", kind: "select", options: ["headless", "tmux"], hint: "Default backend for new sessions" }),
              fieldHtml({ id: "def-tool-disclosure", label: "tool_disclosure", kind: "select", options: ["full", "progressive"], hint: "full — все схемы, progressive — суммари + meta-tool" }),
              fieldHtml({ id: "def-context-window-tokens", label: "context_window_tokens", kind: "number", hint: "Размер контекстного окна (токены)" }),
              fieldHtml({ id: "def-context-reserve-tokens", label: "context_reserve_tokens", kind: "number", hint: "Резерв токенов для ответа LLM" }),
              fieldHtml({ id: "def-summarization-threshold", label: "summarization_threshold", kind: "number", hint: "Порог заполнения контекста для суммаризации (0.0–1.0)" }),
              fieldHtml({ id: "def-llm-trace-enabled", label: "llm_trace_enabled", kind: "checkbox", hint: "Логирование LLM-вызовов в EVENTS.jsonl" }),
            ].join(""),
          };

          const panels = subTabs.map((t) =>
            `<div class="defaults-subtab ${t.key === ast ? "active" : ""}" data-subtab-key="${t.key}">${groups[t.key]}</div>`
          ).join("");

          return tabsBar + panels;
        })(),
      },
      {
        key: "tools",
        title: "tools",
        actions: `<button type="button" data-tool-add="1">${escapeHtml(t("miniapp.cfg.add_tool", "Добавить tool"))}</button>`,
        inner: toolNames.length
          ? toolNames
              .map((toolName, index) => {
                const safeName = escapeHtml(toolName);
                const idPrefix = `tool-${index}-${toIdSafe(toolName)}`;
                return `
                  <div class="object-list-item">
                    <h4>${safeName}</h4>
                    ${fieldHtml({ id: `${idPrefix}-name`, label: "name", readonly: true, hint: "ключ секции tools" })}
                    ${fieldHtml({ id: `${idPrefix}-enabled`, label: "enabled", kind: "checkbox" })}
                    ${fieldHtml({ id: `${idPrefix}-mode`, label: "mode", kind: "select", options: ["headless", "interactive"] })}
                    ${fieldHtml({ id: `${idPrefix}-execution-backends`, label: "execution_backends", kind: "textarea", hint: "headless/tmux, one per line" })}
                    ${fieldHtml({ id: `${idPrefix}-default-execution-backend`, label: "default_execution_backend", kind: "select", options: ["", "headless", "tmux"], hint: "optional" })}
                    ${fieldHtml({ id: `${idPrefix}-tmux-user`, label: "tmux_user", hint: "optional user for tmux backend" })}
                    ${fieldHtml({ id: `${idPrefix}-cmd`, label: "cmd", kind: "textarea", hint: "по одному аргументу в строке" })}
                    ${fieldHtml({ id: `${idPrefix}-headless-cmd`, label: "headless_cmd", kind: "textarea", hint: "optional" })}
                    ${fieldHtml({ id: `${idPrefix}-resume-cmd`, label: "resume_cmd", kind: "textarea", hint: "optional" })}
                    ${fieldHtml({ id: `${idPrefix}-image-cmd`, label: "image_cmd", kind: "textarea", hint: "optional" })}
                    ${fieldHtml({ id: `${idPrefix}-interactive-cmd`, label: "interactive_cmd", kind: "textarea", hint: "optional" })}
                    ${fieldHtml({ id: `${idPrefix}-interactive-resume-cmd`, label: "interactive_resume_cmd", kind: "textarea", hint: "optional, use {resume}" })}
                    ${fieldHtml({ id: `${idPrefix}-prompt-regex`, label: "prompt_regex" })}
                    ${fieldHtml({ id: `${idPrefix}-resume-regex`, label: "resume_regex" })}
                    ${fieldHtml({ id: `${idPrefix}-help-cmd`, label: "help_cmd" })}
                    ${fieldHtml({ id: `${idPrefix}-env`, label: "env", kind: "textarea", hint: "KEY=value" })}
                    ${fieldHtml({ id: `${idPrefix}-auto-commands`, label: "auto_commands", kind: "textarea", hint: "по одной команде в строке" })}
                    ${fieldHtml({ id: `${idPrefix}-separate-stderr`, label: "separate_stderr", kind: "checkbox" })}
                    <button type="button" data-tool-remove="${escapeHtml(toolName)}">${escapeHtml(t("miniapp.cfg.remove_tool", "Удалить tool"))}</button>
                  </div>
                `;
              })
              .join("")
          : `<div class="field"><label>tools</label><small>${escapeHtml(t("miniapp.cfg.no_tools", "Нет инструментов, добавьте кнопку \"Добавить tool\"."))} </small></div>`,
      },
      {
        key: "mcp",
        title: "mcp",
        inner: [
          fieldHtml({ id: "mcp-enabled", label: "enabled", kind: "checkbox" }),
          fieldHtml({ id: "mcp-host", label: "host" }),
          fieldHtml({ id: "mcp-port", label: "port", kind: "number" }),
          fieldHtml({ id: "mcp-token", label: "token", kind: "password", clearable: true }),
        ].join(""),
      },
      {
        key: "mcp_clients",
        title: "mcp_clients",
        actions: `<button type="button" data-mcp-client-add="1">${escapeHtml(t("miniapp.cfg.add_mcp_client", "Добавить mcp_client"))}</button>`,
        inner: (cfg.mcp_clients || []).length
          ? (cfg.mcp_clients || [])
              .map((client, index) => {
                const idPrefix = `mcp-client-${index}`;
                const transport = String(client?.transport || "stdio");
                return `
                  <div class="object-list-item">
                    <h4>mcp_client #${index + 1}</h4>
                    ${fieldHtml({ id: `${idPrefix}-name`, label: "name" })}
                    ${fieldHtml({ id: `${idPrefix}-enabled`, label: "enabled", kind: "checkbox" })}
                    ${fieldHtml({ id: `${idPrefix}-transport`, label: "transport", kind: "select", options: ["stdio", "http"] })}
                    ${transport === "stdio" ? fieldHtml({ id: `${idPrefix}-cmd`, label: "cmd", kind: "textarea", hint: "по одному аргументу в строке" }) : ""}
                    ${transport === "stdio" ? fieldHtml({ id: `${idPrefix}-cwd`, label: "cwd" }) : ""}
                    ${fieldHtml({ id: `${idPrefix}-env`, label: "env", kind: "textarea", hint: "KEY=value" })}
                    ${transport === "http" ? fieldHtml({ id: `${idPrefix}-url`, label: "url" }) : ""}
                    ${transport === "http" ? fieldHtml({ id: `${idPrefix}-headers`, label: "headers", kind: "textarea", hint: "Header=value" }) : ""}
                    ${fieldHtml({ id: `${idPrefix}-timeout-ms`, label: "timeout_ms", kind: "number" })}
                    <button type="button" data-mcp-client-remove="${index}">${escapeHtml(t("miniapp.cfg.remove_mcp_client", "Удалить mcp_client"))}</button>
                  </div>
                `;
              })
              .join("")
          : `<div class="field"><label>mcp_clients</label><small>${escapeHtml(t("miniapp.cfg.list_empty", "Список пуст."))}</small></div>`,
      },
      {
        key: "presets",
        title: "presets",
        actions: `<button type="button" data-preset-add="1">${escapeHtml(t("miniapp.cfg.add_preset", "Добавить preset"))}</button>`,
        inner: (cfg.presets || []).length
          ? (cfg.presets || [])
              .map((_, index) => {
                const idPrefix = `preset-${index}`;
                return `
                  <div class="object-list-item">
                    <h4>preset #${index + 1}</h4>
                    ${fieldHtml({ id: `${idPrefix}-name`, label: "name" })}
                    ${fieldHtml({ id: `${idPrefix}-prompt`, label: "prompt", kind: "textarea" })}
                    <button type="button" data-preset-remove="${index}">${escapeHtml(t("miniapp.cfg.remove_preset", "Удалить preset"))}</button>
                  </div>
                `;
              })
              .join("")
          : `<div class="field"><label>presets</label><small>${escapeHtml(t("miniapp.cfg.list_empty", "Список пуст."))}</small></div>`,
      },
      {
        key: "miniapp",
        title: "miniapp",
        inner: [
          fieldHtml({ id: "mini-enabled", label: "enabled", kind: "checkbox", hint: "restart required" }),
          fieldHtml({ id: "mini-bind-host", label: "bind_host", hint: "restart required" }),
          fieldHtml({ id: "mini-bind-port", label: "bind_port", kind: "number", hint: "restart required" }),
          fieldHtml({ id: "mini-base-path", label: "base_path", hint: "restart required" }),
          fieldHtml({ id: "mini-public-url", label: "public_url", hint: "для кнопки /miniapp, абсолютный URL" }),
          fieldHtml({ id: "mini-max-size", label: "max_edit_file_size_kb", kind: "number", hint: "restart required" }),
          fieldHtml({ id: "mini-enable-delete", label: "enable_delete", kind: "checkbox", hint: "изменяется на лету" }),
        ].join(""),
      },
      {
        key: "thread_mode",
        title: "thread_mode",
        inner: [
          fieldHtml({ id: "thread-enabled", label: "enabled", kind: "checkbox" }),
          fieldHtml({ id: "thread-mode", label: "mode", kind: "select", options: ["private", "group"] }),
          fieldHtml({ id: "thread-topics-chat-id", label: "topics_chat_id", kind: "number" }),
          fieldHtml({ id: "thread-topic-title-prefix", label: "topic_title_prefix" }),
          fieldHtml({ id: "thread-inactivity-ttl", label: "inactivity_ttl_sec", kind: "number" }),
        ].join(""),
      },
      {
        key: "webhooks",
        title: "webhooks",
        inner: [
          fieldHtml({ id: "webhooks-enabled", label: "enabled", kind: "checkbox", hint: "restart required" }),
          fieldHtml({ id: "webhooks-path", label: "path", hint: "restart required" }),
          fieldHtml({ id: "webhooks-public-base-url", label: "public_base_url", hint: "restart required" }),
          fieldHtml({ id: "webhooks-secret-token", label: "secret_token", kind: "password", clearable: true }),
          fieldHtml({ id: "webhooks-request-timeout", label: "request_timeout_sec", kind: "number", hint: "restart required" }),
          fieldHtml({ id: "webhooks-max-payload", label: "max_payload_bytes", kind: "number", hint: "restart required" }),
        ].join(""),
      },
      {
        key: "scheduler",
        title: "scheduler",
        inner: [
          fieldHtml({ id: "sched-enabled", label: "enabled", kind: "checkbox" }),
          fieldHtml({ id: "sched-timezone", label: "timezone" }),
          fieldHtml({ id: "sched-tick-interval", label: "tick_interval_sec", kind: "number" }),
          fieldHtml({ id: "sched-max-concurrent", label: "max_concurrent_jobs", kind: "number" }),
          fieldHtml({ id: "sched-job-timeout", label: "job_timeout_sec", kind: "number" }),
          fieldHtml({ id: "sched-misfire-grace", label: "misfire_grace_sec", kind: "number" }),
        ].join(""),
      },
      {
        key: "security",
        title: "security",
        inner: [
          fieldHtml({ id: "sec-rate-enabled", label: "rate_limits.enabled", kind: "checkbox" }),
          fieldHtml({ id: "sec-rate-backend", label: "rate_limits.backend", kind: "select", options: ["sqlite"] }),
          fieldHtml({ id: "sec-rate-sqlite-path", label: "rate_limits.sqlite_path" }),
          fieldHtml({ id: "sec-rate-default", label: "rate_limits.default", kind: "textarea", hint: 'JSON-объект: {"limit":10,"window_sec":60}' }),
          fieldHtml({ id: "sec-rate-policies", label: "rate_limits.policies", kind: "textarea", hint: "JSON-объект карты политик" }),
          fieldHtml({ id: "sec-screen-enabled", label: "content_screening.enabled", kind: "checkbox", hint: "restart required" }),
          fieldHtml({ id: "sec-screen-mode", label: "content_screening.mode", kind: "select", options: ["warn", "block"], hint: "restart required" }),
          fieldHtml({ id: "sec-screen-max-chars", label: "content_screening.max_chars", kind: "number", hint: "restart required" }),
          fieldHtml({ id: "sec-screen-timeout", label: "content_screening.timeout_ms", kind: "number", hint: "restart required" }),
        ].join(""),
      },
      {
        key: "lint_evolution",
        title: "lint_evolution",
        inner: [
          fieldHtml({ id: "lint-evo-enabled", label: "enabled", kind: "checkbox" }),
          fieldHtml({ id: "lint-evo-level1-cooldown", label: "level1_cooldown_hours", kind: "number" }),
          fieldHtml({ id: "lint-evo-level2-cooldown", label: "level2_cooldown_hours", kind: "number" }),
          fieldHtml({ id: "lint-evo-level3-cooldown", label: "level3_cooldown_hours", kind: "number" }),
          fieldHtml({ id: "lint-evo-lock-ttl", label: "lock_ttl_minutes", kind: "number" }),
          fieldHtml({ id: "lint-evo-error-retry", label: "error_retry_hours", kind: "number" }),
          fieldHtml({ id: "lint-evo-fp-growth", label: "fp_growth_threshold_pct", kind: "number" }),
          fieldHtml({ id: "lint-evo-canary-rolling", label: "canary_rolling_days", kind: "number" }),
          fieldHtml({ id: "lint-evo-canary-baseline", label: "canary_baseline_days", kind: "number" }),
          fieldHtml({ id: "lint-evo-canary-schema-fields", label: "canary_max_schema_fields_per_180d", kind: "number" }),
        ].join(""),
      },
    ];

    const sectionByKey = Object.fromEntries(sections.map((s) => [s.key, s]));
    if (!sectionByKey[state.activeConfigSection]) {
      state.activeConfigSection = sections[0].key;
    }
    const activeSection = sectionByKey[state.activeConfigSection];

    const tabs = document.createElement("div");
    tabs.className = "cfg-section-tabs";
    tabs.innerHTML = sections
      .map(
        (section) =>
          `<button type="button" data-cfg-section="${section.key}" class="${section.key === state.activeConfigSection ? "active" : ""}">${section.title}</button>`
      )
      .join("");
    root.appendChild(tabs);

    const panel = document.createElement("div");
    panel.className = "cfg-section-panel";
    panel.innerHTML = sectionHtml(activeSection);
    root.appendChild(panel);

    root.querySelectorAll("button[data-cfg-section]").forEach((btn) => {
      btn.onclick = (e) => {
        e.preventDefault();
        const key = btn.dataset.cfgSection;
        if (!key || key === state.activeConfigSection) return;
        state.activeConfigSection = key;
        renderConfigForm();
      };
    });

    root.querySelectorAll("button[data-defaults-subtab]").forEach((btn) => {
      btn.onclick = (e) => {
        e.preventDefault();
        const key = btn.dataset.defaultsSubtab;
        if (!key || key === state.activeDefaultsSubTab) return;
        state.activeDefaultsSubTab = key;
        root.querySelectorAll("button[data-defaults-subtab]").forEach((b) => b.classList.toggle("active", b.dataset.defaultsSubtab === key));
        root.querySelectorAll(".defaults-subtab").forEach((p) => p.classList.toggle("active", p.dataset.subtabKey === key));
      };
    });

    bindSecretInput("tg-token", "telegram.token", () => cfg.telegram.token, (v) => (cfg.telegram.token = v));
    bindInput("tg-whitelist", () => arrToMultiline(cfg.telegram.whitelist_chat_ids), (v) => (cfg.telegram.whitelist_chat_ids = multilineToIntArr(v)));
    bindInput("tg-admins", () => arrToMultiline(cfg.telegram.admlist_chat_ids), (v) => (cfg.telegram.admlist_chat_ids = multilineToIntArr(v)));
    bindInput("tg-user-workdirs", () => kvToLines(cfg.telegram.user_workdirs || {}), (v) => (cfg.telegram.user_workdirs = linesToKv(v, true)));
    bindInput("tg-user-modes", () => userModesToLines(cfg.telegram.user_modes || {}), (v) => (cfg.telegram.user_modes = linesToUserModes(v)));
    bindInput("tg-conn-pool", () => cfg.telegram.connection_pool_size, (v) => (cfg.telegram.connection_pool_size = Number(v || 0)));
    bindInput("tg-connect-timeout", () => cfg.telegram.connect_timeout_sec, (v) => (cfg.telegram.connect_timeout_sec = Number(v || 0)));
    bindInput("tg-read-timeout", () => cfg.telegram.read_timeout_sec, (v) => (cfg.telegram.read_timeout_sec = Number(v || 0)));
    bindInput("tg-write-timeout", () => cfg.telegram.write_timeout_sec, (v) => (cfg.telegram.write_timeout_sec = Number(v || 0)));
    bindInput("tg-pool-timeout", () => cfg.telegram.pool_timeout_sec, (v) => (cfg.telegram.pool_timeout_sec = Number(v || 0)));
    bindInput("tg-poll-timeout", () => cfg.telegram.polling_timeout_sec, (v) => (cfg.telegram.polling_timeout_sec = Number(v || 0)));
    bindInput("tg-poll-interval", () => cfg.telegram.poll_interval_sec, (v) => (cfg.telegram.poll_interval_sec = Number(v || 0)));

    bindInput("def-workdir", () => cfg.defaults.workdir, (v) => (cfg.defaults.workdir = v));
    bindInput("def-idle-timeout", () => cfg.defaults.idle_timeout_sec, (v) => (cfg.defaults.idle_timeout_sec = Number(v || 0)));
    bindInput("def-summary-max", () => cfg.defaults.summary_max_chars, (v) => (cfg.defaults.summary_max_chars = Number(v || 0)));
    bindInput("def-html-prefix", () => cfg.defaults.html_filename_prefix, (v) => (cfg.defaults.html_filename_prefix = v));
    bindInput("def-state-path", () => cfg.defaults.state_path, (v) => (cfg.defaults.state_path = v));
    bindInput("def-desktop-state-path", () => cfg.defaults.desktop_state_path, (v) => (cfg.defaults.desktop_state_path = v));
    bindInput("def-toolhelp-path", () => cfg.defaults.toolhelp_path, (v) => (cfg.defaults.toolhelp_path = v));
    bindSecretInput("def-openai-api-key", "defaults.openai_api_key", () => cfg.defaults.openai_api_key, (v) => (cfg.defaults.openai_api_key = v));
    bindInput("def-openai-model", () => cfg.defaults.openai_model || "", (v) => (cfg.defaults.openai_model = optionalText(v)));
    bindInput("def-openai-big-model", () => cfg.defaults.openai_big_model || "", (v) => (cfg.defaults.openai_big_model = optionalText(v)));
    bindInput("def-openai-base-url", () => cfg.defaults.openai_base_url || "", (v) => (cfg.defaults.openai_base_url = optionalText(v)));
    bindSecretInput("def-zai-key", "defaults.zai_api_key", () => cfg.defaults.zai_api_key, (v) => (cfg.defaults.zai_api_key = v));
    bindSecretInput("def-tavily-key", "defaults.tavily_api_key", () => cfg.defaults.tavily_api_key, (v) => (cfg.defaults.tavily_api_key = v));
    bindSecretInput("def-jina-key", "defaults.jina_api_key", () => cfg.defaults.jina_api_key, (v) => (cfg.defaults.jina_api_key = v));
    bindSecretInput("def-github-token", "defaults.github_token", () => cfg.defaults.github_token, (v) => (cfg.defaults.github_token = v));
    bindSecretInput("def-gemini-oauth-client-secret", "defaults.gemini_oauth_client_secret", () => cfg.defaults.gemini_oauth_client_secret, (v) => (cfg.defaults.gemini_oauth_client_secret = v));
    bindInput("def-log-path", () => cfg.defaults.log_path, (v) => (cfg.defaults.log_path = v));
    bindInput("def-image-temp", () => cfg.defaults.image_temp_dir, (v) => (cfg.defaults.image_temp_dir = v));
    bindInput("def-image-max", () => cfg.defaults.image_max_mb, (v) => (cfg.defaults.image_max_mb = Number(v || 0)));
    bindInput("def-memory-max", () => cfg.defaults.memory_max_kb, (v) => (cfg.defaults.memory_max_kb = Number(v || 0)));
    bindInput("def-memory-target", () => cfg.defaults.memory_compact_target_kb, (v) => (cfg.defaults.memory_compact_target_kb = Number(v || 0)));
    bindInput("def-clarification-enabled", () => cfg.defaults.clarification_enabled, (v) => (cfg.defaults.clarification_enabled = !!v));
    bindInput("def-default-cli", () => cfg.defaults.default_cli || "", (v) => (cfg.defaults.default_cli = optionalText(v)));
    bindInput("def-clarification-keywords", () => arrToMultiline(cfg.defaults.clarification_keywords || []), (v) => (cfg.defaults.clarification_keywords = multilineToStrArr(v)));
    bindInput("def-manager-max-tasks", () => cfg.defaults.manager_max_tasks, (v) => (cfg.defaults.manager_max_tasks = Number(v || 0)));
    bindInput("def-manager-max-attempts", () => cfg.defaults.manager_max_attempts, (v) => (cfg.defaults.manager_max_attempts = Number(v || 0)));
    bindInput("def-manager-decompose-timeout", () => cfg.defaults.manager_decompose_timeout_sec, (v) => (cfg.defaults.manager_decompose_timeout_sec = Number(v || 0)));
    bindInput("def-manager-dev-timeout", () => cfg.defaults.manager_dev_timeout_sec, (v) => (cfg.defaults.manager_dev_timeout_sec = Number(v || 0)));
    bindInput("def-manager-review-timeout", () => cfg.defaults.manager_review_timeout_sec, (v) => (cfg.defaults.manager_review_timeout_sec = Number(v || 0)));
    bindInput("def-analyst-use-cli-timeout", () => cfg.defaults.analyst_use_cli_timeout_sec, (v) => (cfg.defaults.analyst_use_cli_timeout_sec = Number(v || 0)));
    bindInput("def-webmaster-use-cli-timeout", () => cfg.defaults.webmaster_use_cli_timeout_sec, (v) => (cfg.defaults.webmaster_use_cli_timeout_sec = Number(v || 0)));
    bindInput(
      "def-webmaster-validation-max-fix-iterations",
      () => cfg.defaults.webmaster_validation_max_fix_iterations,
      (v) => (cfg.defaults.webmaster_validation_max_fix_iterations = Number(v || 0))
    );
    bindInput("def-manager-report-max", () => cfg.defaults.manager_dev_report_max_chars, (v) => (cfg.defaults.manager_dev_report_max_chars = Number(v || 0)));
    bindInput("def-manager-auto-resume", () => cfg.defaults.manager_auto_resume, (v) => (cfg.defaults.manager_auto_resume = !!v));
    bindInput("def-manager-auto-commit", () => cfg.defaults.manager_auto_commit, (v) => (cfg.defaults.manager_auto_commit = !!v));
    bindInput("def-manager-response-archive", () => cfg.defaults.manager_response_archive, (v) => (cfg.defaults.manager_response_archive = !!v));
    bindInput(
      "def-cli-json-stream-archive-enabled",
      () => cfg.defaults.cli_json_stream_archive_enabled,
      (v) => (cfg.defaults.cli_json_stream_archive_enabled = !!v)
    );
    bindInput(
      "def-assistant-preview-enabled",
      () => cfg.defaults.assistant_preview_enabled,
      (v) => (cfg.defaults.assistant_preview_enabled = !!v)
    );
    bindInput("def-codebase-mapper-usage", () => cfg.defaults.codebase_mapper_usage || "auto", (v) => (cfg.defaults.codebase_mapper_usage = v || "auto"));
    bindInput("def-run-artifacts-enabled", () => cfg.defaults.run_artifacts_enabled, (v) => (cfg.defaults.run_artifacts_enabled = !!v));
    bindInput("def-run-artifacts-retention-days", () => cfg.defaults.run_artifacts_retention_days, (v) => (cfg.defaults.run_artifacts_retention_days = Number(v || 0)));
    bindInput("def-memory-events-enabled", () => cfg.defaults.memory_events_enabled, (v) => (cfg.defaults.memory_events_enabled = !!v));
    bindInput(
      "def-memory-native-cli-hooks-enabled",
      () => cfg.defaults.memory_native_cli_hooks_enabled,
      (v) => (cfg.defaults.memory_native_cli_hooks_enabled = !!v)
    );
    bindInput("def-memory-outcomes-enabled", () => cfg.defaults.memory_outcomes_enabled, (v) => (cfg.defaults.memory_outcomes_enabled = !!v));
    bindInput("def-memory-dreaming-enabled", () => cfg.defaults.memory_dreaming_enabled, (v) => (cfg.defaults.memory_dreaming_enabled = !!v));
    bindInput(
      "def-memory-events-retention-days",
      () => cfg.defaults.memory_events_retention_days,
      (v) => (cfg.defaults.memory_events_retention_days = Number(v || 0))
    );
    bindInput(
      "def-memory-events-max-payload-chars",
      () => cfg.defaults.memory_events_max_payload_chars,
      (v) => (cfg.defaults.memory_events_max_payload_chars = Number(v || 0))
    );
    bindInput(
      "def-memory-events-redaction-enabled",
      () => cfg.defaults.memory_events_redaction_enabled,
      (v) => (cfg.defaults.memory_events_redaction_enabled = !!v)
    );
    bindInput(
      "def-memory-dreaming-batch-size",
      () => cfg.defaults.memory_dreaming_batch_size,
      (v) => (cfg.defaults.memory_dreaming_batch_size = Number(v || 0))
    );
    bindInput("def-run-doctor-enabled", () => cfg.defaults.run_doctor_enabled, (v) => (cfg.defaults.run_doctor_enabled = !!v));
    bindInput(
      "def-pending-input-confirmation-enabled",
      () => cfg.defaults.pending_input_confirmation_enabled !== undefined ? cfg.defaults.pending_input_confirmation_enabled : true,
      (v) => (cfg.defaults.pending_input_confirmation_enabled = !!v)
    );
    bindInput(
      "def-run-boundary-validation-enabled",
      () => cfg.defaults.run_boundary_validation_enabled,
      (v) => (cfg.defaults.run_boundary_validation_enabled = !!v)
    );
    bindInput("def-run-metrics-enabled", () => cfg.defaults.run_metrics_enabled, (v) => (cfg.defaults.run_metrics_enabled = !!v));
    bindInput("def-skill-discovery-mode", () => cfg.defaults.skill_discovery_mode || "suggest", (v) => (cfg.defaults.skill_discovery_mode = v || "suggest"));
    bindInput("def-skill-install-policy", () => cfg.defaults.skill_install_policy || "manual", (v) => (cfg.defaults.skill_install_policy = v || "manual"));
    bindInput("def-skill-registry-paths", () => arrToMultiline(cfg.defaults.skill_registry_paths || []), (v) => (cfg.defaults.skill_registry_paths = multilineToStrArr(v)));
    bindInput(
      "def-skill-allowlisted-sources",
      () => arrToMultiline(cfg.defaults.skill_allowlisted_sources || []),
      (v) => (cfg.defaults.skill_allowlisted_sources = multilineToStrArr(v))
    );
    bindInput("def-cli-routing", () => kvToLines(cfg.defaults.cli_routing || {}), (v) => (cfg.defaults.cli_routing = optionalMap(linesToKv(v, true))));
    bindInput("def-default-execution-backend", () => cfg.defaults.default_execution_backend || "headless", (v) => (cfg.defaults.default_execution_backend = v || "headless"));
    bindInput("def-tool-disclosure", () => cfg.defaults.tool_disclosure || "full", (v) => (cfg.defaults.tool_disclosure = v || "full"));
    bindInput("def-context-window-tokens", () => cfg.defaults.context_window_tokens, (v) => (cfg.defaults.context_window_tokens = Number(v || 0)));
    bindInput("def-context-reserve-tokens", () => cfg.defaults.context_reserve_tokens, (v) => (cfg.defaults.context_reserve_tokens = Number(v || 0)));
    bindInput("def-summarization-threshold", () => cfg.defaults.summarization_threshold, (v) => (cfg.defaults.summarization_threshold = Number(v || 0)));
    bindInput("def-llm-trace-enabled", () => cfg.defaults.llm_trace_enabled, (v) => (cfg.defaults.llm_trace_enabled = !!v));

    bindInput("mcp-enabled", () => cfg.mcp.enabled, (v) => (cfg.mcp.enabled = !!v));
    bindInput("mcp-host", () => cfg.mcp.host, (v) => (cfg.mcp.host = v));
    bindInput("mcp-port", () => cfg.mcp.port, (v) => (cfg.mcp.port = Number(v || 0)));
    bindSecretInput("mcp-token", "mcp.token", () => cfg.mcp.token, (v) => (cfg.mcp.token = v));

    toolNames.forEach((toolName, index) => {
      const idPrefix = `tool-${index}-${toIdSafe(toolName)}`;
      const tool = cfg.tools[toolName];
      if (!tool) return;

      bindInput(`${idPrefix}-name`, () => toolName, () => {});
      bindInput(`${idPrefix}-enabled`, () => !!tool.enabled, (v) => (tool.enabled = !!v));
      bindInput(`${idPrefix}-mode`, () => tool.mode || "headless", (v) => (tool.mode = v || "headless"));
      bindInput(`${idPrefix}-execution-backends`, () => arrToMultiline(tool.execution_backends || []), (v) => (tool.execution_backends = optionalList(multilineToStrArr(v))));
      bindInput(`${idPrefix}-default-execution-backend`, () => tool.default_execution_backend || "", (v) => (tool.default_execution_backend = String(v || "").trim() || null));
      bindInput(`${idPrefix}-tmux-user`, () => tool.tmux_user || "", (v) => (tool.tmux_user = String(v || "").trim() || null));
      bindInput(`${idPrefix}-cmd`, () => arrToMultiline(tool.cmd || []), (v) => (tool.cmd = multilineToStrArr(v)));
      bindInput(`${idPrefix}-headless-cmd`, () => arrToMultiline(tool.headless_cmd || []), (v) => (tool.headless_cmd = optionalList(multilineToStrArr(v))));
      bindInput(`${idPrefix}-resume-cmd`, () => arrToMultiline(tool.resume_cmd || []), (v) => (tool.resume_cmd = optionalList(multilineToStrArr(v))));
      bindInput(`${idPrefix}-image-cmd`, () => arrToMultiline(tool.image_cmd || []), (v) => (tool.image_cmd = optionalList(multilineToStrArr(v))));
      bindInput(`${idPrefix}-interactive-cmd`, () => arrToMultiline(tool.interactive_cmd || []), (v) => (tool.interactive_cmd = optionalList(multilineToStrArr(v))));
      bindInput(`${idPrefix}-interactive-resume-cmd`, () => arrToMultiline(tool.interactive_resume_cmd || []), (v) => (tool.interactive_resume_cmd = optionalList(multilineToStrArr(v))));
      bindInput(`${idPrefix}-prompt-regex`, () => tool.prompt_regex || "", (v) => (tool.prompt_regex = optionalText(v)));
      bindInput(`${idPrefix}-resume-regex`, () => tool.resume_regex || "", (v) => (tool.resume_regex = optionalText(v)));
      bindInput(`${idPrefix}-help-cmd`, () => tool.help_cmd || "", (v) => (tool.help_cmd = optionalText(v)));
      bindInput(`${idPrefix}-env`, () => kvToLines(tool.env || {}), (v) => (tool.env = optionalMap(linesToKv(v, false))));
      bindInput(`${idPrefix}-auto-commands`, () => arrToMultiline(tool.auto_commands || []), (v) => (tool.auto_commands = optionalList(multilineToStrArr(v))));
      bindInput(`${idPrefix}-separate-stderr`, () => !!tool.separate_stderr, (v) => (tool.separate_stderr = !!v));
    });

    (cfg.mcp_clients || []).forEach((client, index) => {
      const idPrefix = `mcp-client-${index}`;
      bindInput(`${idPrefix}-name`, () => client.name || "", (v) => (client.name = v));
      bindInput(`${idPrefix}-enabled`, () => !!client.enabled, (v) => (client.enabled = !!v));
      const transportEl = document.getElementById(`${idPrefix}-transport`);
      if (transportEl) {
        transportEl.value = client.transport || "stdio";
        transportEl.onchange = () => {
          client.transport = transportEl.value || "stdio";
          renderConfigForm();
        };
      }
      bindInput(`${idPrefix}-cmd`, () => arrToMultiline(client.cmd || []), (v) => (client.cmd = multilineToStrArr(v)));
      bindInput(`${idPrefix}-url`, () => client.url || "", (v) => (client.url = optionalText(v)));
      bindInput(`${idPrefix}-cwd`, () => client.cwd || "", (v) => (client.cwd = optionalText(v)));
      bindInput(`${idPrefix}-env`, () => kvToLines(client.env || {}), (v) => (client.env = optionalMap(linesToKv(v, false))));
      bindInput(`${idPrefix}-headers`, () => kvToLines(client.headers || {}), (v) => (client.headers = optionalMap(linesToKv(v, false))));
      bindInput(`${idPrefix}-timeout-ms`, () => client.timeout_ms, (v) => (client.timeout_ms = Number(v || 0)));
    });

    (cfg.presets || []).forEach((preset, index) => {
      const idPrefix = `preset-${index}`;
      bindInput(`${idPrefix}-name`, () => preset.name || "", (v) => (preset.name = v));
      bindInput(`${idPrefix}-prompt`, () => preset.prompt || "", (v) => (preset.prompt = v));
    });

    bindInput("mini-enabled", () => cfg.miniapp.enabled, (v) => (cfg.miniapp.enabled = !!v));
    bindInput("mini-bind-host", () => cfg.miniapp.bind_host || "", (v) => (cfg.miniapp.bind_host = v));
    bindInput("mini-bind-port", () => cfg.miniapp.bind_port, (v) => (cfg.miniapp.bind_port = Number(v || 0)));
    bindInput("mini-base-path", () => cfg.miniapp.base_path, (v) => (cfg.miniapp.base_path = v));
    bindInput("mini-public-url", () => cfg.miniapp.public_url || "", (v) => (cfg.miniapp.public_url = v));
    bindInput("mini-max-size", () => cfg.miniapp.max_edit_file_size_kb, (v) => (cfg.miniapp.max_edit_file_size_kb = Number(v || 0)));
    bindInput("mini-enable-delete", () => cfg.miniapp.enable_delete, (v) => (cfg.miniapp.enable_delete = !!v));

    bindInput("thread-enabled", () => cfg.thread_mode.enabled, (v) => (cfg.thread_mode.enabled = !!v));
    bindInput("thread-mode", () => cfg.thread_mode.mode || "private", (v) => (cfg.thread_mode.mode = v || "private"));
    bindInput("thread-topics-chat-id", () => cfg.thread_mode.topics_chat_id, (v) => (cfg.thread_mode.topics_chat_id = optionalNumber(v)));
    bindInput("thread-topic-title-prefix", () => cfg.thread_mode.topic_title_prefix || "", (v) => (cfg.thread_mode.topic_title_prefix = v));
    bindInput("thread-inactivity-ttl", () => cfg.thread_mode.inactivity_ttl_sec, (v) => (cfg.thread_mode.inactivity_ttl_sec = Number(v || 0)));

    bindInput("webhooks-enabled", () => cfg.webhooks.enabled, (v) => (cfg.webhooks.enabled = !!v));
    bindInput("webhooks-path", () => cfg.webhooks.path || "", (v) => (cfg.webhooks.path = v));
    bindInput("webhooks-public-base-url", () => cfg.webhooks.public_base_url || "", (v) => (cfg.webhooks.public_base_url = optionalText(v)));
    bindSecretInput("webhooks-secret-token", "webhooks.secret_token", () => cfg.webhooks.secret_token, (v) => (cfg.webhooks.secret_token = v));
    bindInput("webhooks-request-timeout", () => cfg.webhooks.request_timeout_sec, (v) => (cfg.webhooks.request_timeout_sec = Number(v || 0)));
    bindInput("webhooks-max-payload", () => cfg.webhooks.max_payload_bytes, (v) => (cfg.webhooks.max_payload_bytes = Number(v || 0)));

    bindInput("sched-enabled", () => cfg.scheduler.enabled, (v) => (cfg.scheduler.enabled = !!v));
    bindInput("sched-timezone", () => cfg.scheduler.timezone || "UTC", (v) => (cfg.scheduler.timezone = v));
    bindInput("sched-tick-interval", () => cfg.scheduler.tick_interval_sec, (v) => (cfg.scheduler.tick_interval_sec = Number(v || 0)));
    bindInput("sched-max-concurrent", () => cfg.scheduler.max_concurrent_jobs, (v) => (cfg.scheduler.max_concurrent_jobs = Number(v || 0)));
    bindInput("sched-job-timeout", () => cfg.scheduler.job_timeout_sec, (v) => (cfg.scheduler.job_timeout_sec = Number(v || 0)));
    bindInput("sched-misfire-grace", () => cfg.scheduler.misfire_grace_sec, (v) => (cfg.scheduler.misfire_grace_sec = Number(v || 0)));

    bindInput("sec-rate-enabled", () => cfg.security.rate_limits.enabled, (v) => (cfg.security.rate_limits.enabled = !!v));
    bindInput("sec-rate-backend", () => cfg.security.rate_limits.backend || "sqlite", (v) => (cfg.security.rate_limits.backend = v || "sqlite"));
    bindInput("sec-rate-sqlite-path", () => cfg.security.rate_limits.sqlite_path || "", (v) => (cfg.security.rate_limits.sqlite_path = optionalText(v)));
    bindJsonArea("sec-rate-default", () => cfg.security.rate_limits.default, (v) => (cfg.security.rate_limits.default = v), "security.rate_limits.default");
    bindJsonArea("sec-rate-policies", () => cfg.security.rate_limits.policies, (v) => (cfg.security.rate_limits.policies = v || {}), "security.rate_limits.policies");

    bindInput("sec-screen-enabled", () => cfg.security.content_screening.enabled, (v) => (cfg.security.content_screening.enabled = !!v));
    bindInput("sec-screen-mode", () => cfg.security.content_screening.mode || "warn", (v) => (cfg.security.content_screening.mode = v || "warn"));
    bindInput("sec-screen-max-chars", () => cfg.security.content_screening.max_chars, (v) => (cfg.security.content_screening.max_chars = Number(v || 0)));
    bindInput("sec-screen-timeout", () => cfg.security.content_screening.timeout_ms, (v) => (cfg.security.content_screening.timeout_ms = Number(v || 0)));

    bindInput("lint-evo-enabled", () => cfg.lint_evolution.enabled, (v) => (cfg.lint_evolution.enabled = !!v));
    bindInput("lint-evo-level1-cooldown", () => cfg.lint_evolution.level1_cooldown_hours, (v) => (cfg.lint_evolution.level1_cooldown_hours = Number(v || 0)));
    bindInput("lint-evo-level2-cooldown", () => cfg.lint_evolution.level2_cooldown_hours, (v) => (cfg.lint_evolution.level2_cooldown_hours = Number(v || 0)));
    bindInput("lint-evo-level3-cooldown", () => cfg.lint_evolution.level3_cooldown_hours, (v) => (cfg.lint_evolution.level3_cooldown_hours = Number(v || 0)));
    bindInput("lint-evo-lock-ttl", () => cfg.lint_evolution.lock_ttl_minutes, (v) => (cfg.lint_evolution.lock_ttl_minutes = Number(v || 0)));
    bindInput("lint-evo-error-retry", () => cfg.lint_evolution.error_retry_hours, (v) => (cfg.lint_evolution.error_retry_hours = Number(v || 0)));
    bindInput("lint-evo-fp-growth", () => cfg.lint_evolution.fp_growth_threshold_pct, (v) => (cfg.lint_evolution.fp_growth_threshold_pct = Number(v || 0)));
    bindInput("lint-evo-canary-rolling", () => cfg.lint_evolution.canary_rolling_days, (v) => (cfg.lint_evolution.canary_rolling_days = Number(v || 0)));
    bindInput("lint-evo-canary-baseline", () => cfg.lint_evolution.canary_baseline_days, (v) => (cfg.lint_evolution.canary_baseline_days = Number(v || 0)));
    bindInput("lint-evo-canary-schema-fields", () => cfg.lint_evolution.canary_max_schema_fields_per_180d, (v) => (cfg.lint_evolution.canary_max_schema_fields_per_180d = Number(v || 0)));

    root.querySelectorAll("button[data-reset]").forEach((btn) => {
      btn.onclick = (e) => {
        e.preventDefault();
        const sec = btn.dataset.reset;
        state.draft[sec] = JSON.parse(JSON.stringify(state.savedConfig[sec]));
        renderConfigForm();
        renderDirty();
      };
    });

    root.querySelectorAll("button[data-tool-add]").forEach((btn) => {
      btn.onclick = (e) => {
        e.preventDefault();
        const name = window.prompt(t("miniapp.cfg.prompt_tool_name", "Имя инструмента (ключ в tools)"));
        if (!name) return;
        const toolName = name.trim();
        if (!toolName) return;
        if (cfg.tools[toolName]) {
          uiAlert(t("miniapp.cfg.err_tool_exists", "Инструмент с таким именем уже есть"));
          return;
        }
        cfg.tools[toolName] = {
          name: toolName,
          mode: "headless",
          cmd: [],
          enabled: true,
          separate_stderr: false,
        };
        renderConfigForm();
      };
    });
    root.querySelectorAll("button[data-tool-remove]").forEach((btn) => {
      btn.onclick = async (e) => {
        e.preventDefault();
        const toolName = btn.dataset.toolRemove || "";
        if (!toolName || !cfg.tools[toolName]) return;
        if (!(await uiConfirm(t("miniapp.cfg.confirm_delete_tool", "Удалить tool '{name}'?").replace("{name}", toolName)))) return;
        delete cfg.tools[toolName];
        renderConfigForm();
      };
    });

    root.querySelectorAll("button[data-mcp-client-add]").forEach((btn) => {
      btn.onclick = (e) => {
        e.preventDefault();
        cfg.mcp_clients.push({
          name: "",
          enabled: true,
          transport: "stdio",
          cmd: [],
          url: null,
          cwd: null,
          env: null,
          headers: null,
          timeout_ms: 30000,
        });
        renderConfigForm();
      };
    });
    root.querySelectorAll("button[data-mcp-client-remove]").forEach((btn) => {
      btn.onclick = (e) => {
        e.preventDefault();
        const idx = Number(btn.dataset.mcpClientRemove);
        if (!Number.isInteger(idx) || idx < 0 || idx >= cfg.mcp_clients.length) return;
        cfg.mcp_clients.splice(idx, 1);
        renderConfigForm();
      };
    });

    root.querySelectorAll("button[data-preset-add]").forEach((btn) => {
      btn.onclick = (e) => {
        e.preventDefault();
        cfg.presets.push({ name: "", prompt: "" });
        renderConfigForm();
      };
    });
    root.querySelectorAll("button[data-preset-remove]").forEach((btn) => {
      btn.onclick = (e) => {
        e.preventDefault();
        const idx = Number(btn.dataset.presetRemove);
        if (!Number.isInteger(idx) || idx < 0 || idx >= cfg.presets.length) return;
        cfg.presets.splice(idx, 1);
        renderConfigForm();
      };
    });

    renderDirty();
  }

  async function loadConfig() {
    const view = await api("/config/view");
    state.savedConfig = normalizeConfigRoot(view.config || {});
    state.draft = JSON.parse(JSON.stringify(state.savedConfig));
    state.revision = view.revision;
    state.redaction = view.redaction && typeof view.redaction === "object" ? view.redaction : null;
    renderConfigForm();
    const reloadResult = document.getElementById("cfgReloadResult");
    if (reloadResult) {
      reloadResult.innerHTML = "";
    }
    const restartBanner = document.getElementById("cfgRestartBanner");
    if (restartBanner) {
      restartBanner.textContent = t("miniapp.cfg.restart_banner", "Требуется перезапуск для части изменений");
      restartBanner.classList.add("hidden");
    }
  }

  async function validateConfig() {
    const clientErrors = validateClientDraft();
    if (clientErrors.length) {
      renderValidationErrors(clientErrors);
      document.getElementById("cfgSave").disabled = true;
      return false;
    }

    const result = await api("/config/validate", { method: "POST", body: JSON.stringify({ draft: state.draft }) });
    const el = document.getElementById("cfgValidation");
    if (result.ok) {
      el.innerHTML = `<div class="status-ok">${escapeHtml(t("miniapp.cfg.validation_passed", "Валидация пройдена"))}</div>`;
    } else {
      renderValidationErrors(result.errors || [], t("miniapp.cfg.server_validation", "Серверная валидация"));
    }
    document.getElementById("cfgSave").disabled = !result.ok || !dirtySections().length;
    return result.ok;
  }

  async function previewDiff() {
    const result = await api("/config/diff", { method: "POST", body: JSON.stringify({ draft: state.draft }) });
    const el = document.getElementById("cfgDiffResult");
    el.innerHTML = renderConfigDiffResult(result);
    const restartRequired = Array.isArray(result.restart_required) ? result.restart_required : [];
    const restartBanner = document.getElementById("cfgRestartBanner");
    if (restartBanner) {
      if (restartRequired.length > 0) {
        const listed = restartRequired.slice(0, 4).join(", ");
        const suffix = restartRequired.length > 4 ? ` ${t("miniapp.cfg.and_more", "и ещё")} ${restartRequired.length - 4}` : "";
        restartBanner.textContent = `${t("miniapp.cfg.restart_required_for", "Требуется перезапуск для применения:")} ${listed}${suffix}`;
        restartBanner.classList.remove("hidden");
      } else {
        restartBanner.textContent = t("miniapp.cfg.restart_banner", "Требуется перезапуск для части изменений");
        restartBanner.classList.add("hidden");
      }
    }
  }

  function renderConfigDiffList(title, values, emptyText) {
    const items = Array.isArray(values) ? values.filter((item) => String(item || "").trim()) : [];
    if (!items.length) {
      return `<div class="kv-row"><span class="kv-key">${escapeHtml(title)}:</span> <span class="kv-val">${escapeHtml(emptyText)}</span></div>`;
    }
    return `
      <div class="kv-row">
        <span class="kv-key">${escapeHtml(title)}:</span>
        <span class="kv-val">${items.map((item) => `<code>${escapeHtml(item)}</code>`).join(" ")}</span>
      </div>
    `;
  }

  function renderConfigDiffResult(result) {
    const payload = isPlainObject(result) ? result : {};
    const changed = Array.isArray(payload.changed) ? payload.changed : [];
    const reloadable = Array.isArray(payload.reloadable) ? payload.reloadable : [];
    const restartRequired = Array.isArray(payload.restart_required) ? payload.restart_required : [];
    const warnings = Array.isArray(payload.warnings) ? payload.warnings : [];
    const sections = [
      renderConfigDiffList(t("miniapp.cfg.diff_changed", "Изменено"), changed, t("miniapp.cfg.diff_none", "нет")),
      renderConfigDiffList("Hot-reload", reloadable, t("miniapp.cfg.diff_no_live", "нет live-изменений")),
      renderConfigDiffList(t("miniapp.cfg.diff_restart", "Требует перезапуска"), restartRequired, t("miniapp.cfg.diff_none", "нет")),
    ];
    if (warnings.length) {
      sections.push(renderConfigDiffList("Warnings", warnings, t("miniapp.cfg.diff_none", "нет")));
    }
    return `<div class="kv-list">${sections.join("")}</div>`;
  }

  async function saveConfig() {
    const ok = await validateConfig();
    if (!ok) return;
    await previewDiff();
    if (!(await confirmSecretChangesBeforeSave())) return;
    if (!(await uiConfirm(t("miniapp.cfg.save_confirm", "Сохранить config.yaml и применить hot-reload?")))) return;
    const result = await api("/config/save", {
      method: "POST",
      body: JSON.stringify({ draft: state.draft, expected_revision: state.revision }),
    });
    await loadConfig();
    const reload = result && typeof result.reload === "object" && result.reload ? result.reload : {};
    const reloadResult = document.getElementById("cfgReloadResult");
    const restartBanner = document.getElementById("cfgRestartBanner");
    const restartRequired = Array.isArray(reload.restart_required) ? reload.restart_required : [];
    const warnings = Array.isArray(reload.warnings) ? reload.warnings : [];
    if (restartBanner) {
      if (restartRequired.length > 0) {
        const listed = restartRequired.slice(0, 4).join(", ");
        const suffix = restartRequired.length > 4 ? ` ${t("miniapp.cfg.and_more", "и ещё")} ${restartRequired.length - 4}` : "";
        restartBanner.textContent = `${t("miniapp.cfg.restart_required_for", "Требуется перезапуск для применения:")} ${listed}${suffix}`;
        restartBanner.classList.remove("hidden");
      } else {
        restartBanner.textContent = t("miniapp.cfg.restart_banner", "Требуется перезапуск для части изменений");
        restartBanner.classList.add("hidden");
      }
    }
    if (reloadResult) {
      if (String(reload.status || "") === "error") {
        const text = warnings[0] || t("miniapp.cfg.reload_error", "Hot-reload не применил изменения.");
        reloadResult.innerHTML = `<div class="status-error">${escapeHtml(text)}</div>`;
      } else if (restartRequired.length > 0) {
        reloadResult.innerHTML = (
          `<div class="banner">${escapeHtml(t("miniapp.cfg.reload_live_only", "Hot-reload применил только live-изменения."))} ` +
          `${escapeHtml(t("miniapp.cfg.reload_restart_hint", "Чтобы задействовать restart-only поля ({fields}), перезапустите процесс.").replace("{fields}", restartRequired.join(", ")))}</div>`
        );
      } else {
        reloadResult.innerHTML = `<div class="status-ok">${escapeHtml(t("miniapp.cfg.reload_ok", "Изменения применены hot-reload без перезапуска."))}</div>`;
      }
    }
  }

  async function loadTree(path = state.currentDir || ".") {
    const status = document.getElementById("filesStatus");
    status.textContent = t("miniapp.status.loading", "Загрузка…");
    try {
      const sessionUid = currentFilesSessionUid();
      if (!sessionUid) {
        status.textContent = t("miniapp.status.no_session", "Сессия не выбрана");
        return;
      }
      const result = await api(`/files/tree?path=${encodeURIComponent(path)}&session_uid=${encodeURIComponent(sessionUid)}`);
      state.currentDir = result.path || ".";
      const active = state.statusLastPayload?.active_session || {};
      if (active.execution_target === "remote") {
        const root = active.remote_project_root || "unknown";
        document.getElementById("filesPath").textContent = `${t("miniapp.files.path_remote", "Текущий путь (Remote: {root}):", ).replace("{root}", root)} ${state.currentDir}`;
      } else {
        document.getElementById("filesPath").textContent = `${t("miniapp.files.path_local", "Текущий путь:")} ${state.currentDir}`;
      }
      const tree = document.getElementById("filesTree");
      tree.innerHTML = "";
      (result.items || []).forEach((item) => {
        const li = document.createElement("li");
        li.textContent = `${item.is_dir ? "📁" : "📄"} ${item.name}`;
        li.onclick = async () => {
          document.querySelectorAll("#filesTree li").forEach((e) => e.classList.remove("selected"));
          li.classList.add("selected");
          state.selectedPath = item.path;
          if (item.is_dir) {
            await loadTree(item.path);
          } else {
            await openFile(item.path);
            switchTab("editor");
          }
        };
        tree.appendChild(li);
      });
      status.textContent = "";
    } catch (err) {
      if (err.status === 400) {
        status.textContent = t("miniapp.status.no_session", "Сессия не выбрана");
      } else {
        status.textContent = err.message;
      }
    }
  }

  async function openFile(path) {
    const sessionUid = currentFilesSessionUid();
    if (!sessionUid) {
      throw new Error(t("miniapp.status.no_session", "Сессия не выбрана"));
    }
    const result = await api(`/files/read?path=${encodeURIComponent(path)}&session_uid=${encodeURIComponent(sessionUid)}`);
    state.openFile = path;
    state.openFileRevision = result.revision;
    editor.setValue(result.content || "", -1);
    document.getElementById("editorPath").textContent = path;
    document.getElementById("editorMeta").textContent = statusValueText(result.meta || {});
    const lower = path.toLowerCase();
    if (lower.endsWith(".py")) editor.session.setMode("ace/mode/python");
    else if (lower.endsWith(".md")) editor.session.setMode("ace/mode/markdown");
    else if (lower.endsWith(".json")) editor.session.setMode("ace/mode/json");
    else if (lower.endsWith(".yml") || lower.endsWith(".yaml")) editor.session.setMode("ace/mode/yaml");
    else editor.session.setMode("ace/mode/text");
    scheduleEditorResize();
  }

  async function saveFile(force = false) {
    if (!state.openFile) return;
    const sessionUid = currentFilesSessionUid();
    if (!sessionUid) {
      throw new Error(t("miniapp.status.no_session", "Сессия не выбрана"));
    }
    const content = editor.getValue();
    try {
      const result = await api("/files/write", {
        method: "POST",
        body: JSON.stringify({ 
          session_uid: sessionUid, 
          path: state.openFile, 
          content, 
          expected_revision: state.openFileRevision,
          force: force
        }),
      });
      state.openFileRevision = result.revision;
      
      // Hide conflict UI if it was visible and save succeeded
      document.getElementById("editorConflictDialog").style.display = "none";
      document.getElementById("editorForceSave").style.display = "none";
      document.getElementById("editorSave").style.display = "inline-block";
      
      tg.showScanResult?.(t("miniapp.files.saved", "Файл сохранён"));
      await loadTree(state.currentDir);
    } catch (err) {
      if (err.status === 409 && err.body && err.body.diff_unified) {
        document.getElementById("editorConflictDialog").style.display = "block";
        document.getElementById("editorConflictDiff").textContent = err.body.diff_unified;
        document.getElementById("editorSave").style.display = "none";
        document.getElementById("editorForceSave").style.display = "inline-block";
      } else {
        uiAlert(`${t("miniapp.files.save_error_prefix", "Ошибка сохранения:")} ${err.message || "unknown"}`);
      }
    }
  }

  async function resetOpenFileBuffer() {
    if (!state.openFile) return;
    const ok = await uiConfirm(t("miniapp.files.reset_confirm", "Сбросить несохраненные изменения и перезагрузить файл с диска?"));
    if (!ok) return;
    await openFile(state.openFile);
  }

  function downloadOpenFile() {
    if (!state.openFile) return;
    const sessionUid = currentFilesSessionUid();
    if (!sessionUid) {
      throw new Error(t("miniapp.status.no_session", "Сессия не выбрана"));
    }
    const qs = new URLSearchParams({
      path: state.openFile,
      session_uid: sessionUid,
    });
    api("/files/ws_ticket")
      .then((payload) => {
        const ticket = String(payload.ticket || "");
        if (!ticket) {
          throw new Error("download ticket missing");
        }
        qs.set("ticket", ticket);
        const downloadUrl = new URL(`./api/files/download?${qs.toString()}`, window.location.href).toString();
        openDownloadUrl(downloadUrl);
      })
      .catch((err) => {
        uiAlert(`${t("miniapp.files.download_error_prefix", "Ошибка скачивания:")} ${err.message || "unknown"}`);
      });
  }

  async function createPath(kind) {
    const sessionUid = currentFilesSessionUid();
    if (!sessionUid) {
      throw new Error(t("miniapp.status.no_session", "Сессия не выбрана"));
    }
    const name = window.prompt(kind === "file" ? t("miniapp.files.prompt_filename", "Имя файла") : t("miniapp.files.prompt_dirname", "Имя папки"));
    if (!name) return;
    const path = state.currentDir === "." ? name : `${state.currentDir}/${name}`;
    await api("/files/create", { method: "POST", body: JSON.stringify({ session_uid: sessionUid, path, kind }) });
    await loadTree(state.currentDir);
  }

  async function deletePath() {
    if (!state.selectedPath) return;
    const sessionUid = currentFilesSessionUid();
    if (!sessionUid) {
      throw new Error(t("miniapp.status.no_session", "Сессия не выбрана"));
    }
    if (!(await uiConfirm(t("miniapp.files.delete_confirm", "Удалить {path}?").replace("{path}", state.selectedPath)))) return;
    await api("/files/delete", { method: "POST", body: JSON.stringify({ session_uid: sessionUid, path: state.selectedPath }) });
    if (state.openFile === state.selectedPath) {
      state.openFile = null;
      state.openFileRevision = null;
      editor.setValue("", -1);
    }
    await loadTree(state.currentDir);
  }

  function bindButtons() {
    document.querySelectorAll(".tabs button").forEach((b) => {
      b.onclick = () => switchTab(b.dataset.tab);
    });

    document.getElementById("cfgRefresh").onclick = async () => loadConfig();
    document.getElementById("cfgValidate").onclick = async () => validateConfig();
    document.getElementById("cfgDiff").onclick = async () => previewDiff();
    document.getElementById("cfgSave").onclick = async () => saveConfig();

    document.getElementById("filesSession").onchange = () => {
      applyFilesStateFromControls();
    };
    document.getElementById("filesApply").onclick = async () => {
      applyFilesStateFromControls();
      state.selectedPath = "";
      await loadTree(".");
    };
    document.getElementById("filesRefresh").onclick = async () => loadTree(state.currentDir);
    document.getElementById("filesUp").onclick = async () => {
      if (state.currentDir === ".") return;
      const parts = state.currentDir.split("/").filter(Boolean);
      parts.pop();
      await loadTree(parts.length ? parts.join("/") : ".");
    };
    document.getElementById("filesCreateFile").onclick = async () => createPath("file");
    document.getElementById("filesCreateDir").onclick = async () => createPath("dir");
    document.getElementById("filesDelete").onclick = async () => deletePath();

    document.getElementById("editorReload").onclick = async () => {
      if (state.openFile) await openFile(state.openFile);
    };
    document.getElementById("editorClose").onclick = () => switchTab("files");
    document.getElementById("editorDownload").onclick = () => downloadOpenFile();
    document.getElementById("editorSave").onclick = async () => saveFile();
    document.getElementById("editorForceSave").onclick = async () => saveFile(true);
    document.getElementById("editorConflictCancel").onclick = () => {
      document.getElementById("editorConflictDialog").style.display = "none";
      document.getElementById("editorForceSave").style.display = "none";
      document.getElementById("editorSave").style.display = "inline-block";
    };
    document.getElementById("editorConflictReload").onclick = async () => {
      document.getElementById("editorConflictDialog").style.display = "none";
      document.getElementById("editorForceSave").style.display = "none";
      document.getElementById("editorSave").style.display = "inline-block";
      if (state.openFile) await openFile(state.openFile);
    };
    document.getElementById("editorConflictForceSave").onclick = async () => saveFile(true);
    document.getElementById("editorUndo").onclick = async () => resetOpenFileBuffer();

    document.getElementById("logsApply").onclick = () => {
      applyLogsStateFromControls();
      void connectLogsWs();
    };
    document.getElementById("logsClear").onclick = () => clearLogsView();
    document.getElementById("logsDownload").onclick = () => downloadLogsView();
    document.getElementById("statusApply").onclick = () => {
      applyStatusStateFromControls();
      void connectStatusWs();
      void fetchRuns();
    };
    document.getElementById("settingsApply").onclick = async () => {
      applySettingsStateFromControls();
      await fetchSessionSettings();
    };
    document.getElementById("settingsSession").onchange = () => {
      applySettingsStateFromControls();
      void fetchSessionSettings();
    };
    document.getElementById("settingsSave").onclick = async () => {
      await saveSessionSettings();
    };
    document.getElementById("settingsRemoteControlEnabled").onchange = (e) => {
      const field = document.getElementById("settingsRemoteControlHostField");
      field.style.display = e.target.checked ? "block" : "none";
    };
    document.getElementById("settingsRemoteControlRecheck").onclick = async () => {
      const uid = state.settingsSessionUid;
      if (!uid) return;
      
      const rcError = document.getElementById("settingsRemoteControlError");
      rcError.style.display = "none";
      rcError.textContent = "";

      try {
        const result = await api(`/session/${uid}/remote-control/recheck`, {
          method: "POST"
        });
        if (result?.preflight?.ok) {
          tg.showScanResult?.(t("miniapp.settings.preflight_ok", "Preflight пройден успешно"));
        } else {
          const pfeCode = result?.preflight?.error || result?.error || "unknown";
          rcError.textContent = `${t("miniapp.settings.preflight_error_prefix", "Ошибка перепроверки:")} ${t("errors." + pfeCode, pfeCode)}`;
          rcError.style.display = "block";
        }
        await fetchSessionSettings();
      } catch (err) {
        rcError.textContent = `${t("miniapp.settings.preflight_error_prefix", "Ошибка перепроверки:")} ${err?.body?.preflight?.error || err.message || "unknown"}`;
        rcError.style.display = "block";
      }
    };
    document.getElementById("sshHostAdd").onclick = () => {
      openSshHostForm();
    };
    document.getElementById("sshHostAuth").onchange = () => {
      toggleSshFormFields();
    };
    document.getElementById("sshHostSudo").onchange = () => {
      toggleSshFormFields();
    };
    document.getElementById("sshHostSave").onclick = async () => {
      await saveSshHost();
    };
    document.getElementById("sshHostCancel").onclick = () => {
      closeSshHostForm();
    };
    document.getElementById("statusRunDoctor").onclick = () => {
      void performRunAction("doctor");
    };
    document.getElementById("statusRunRecover").onclick = () => {
      void performRunAction("recover");
    };
    document.getElementById("statusRunResume").onclick = () => {
      void performRunAction("resume");
    };
    document.getElementById("statusRunApplyRecommendation").onclick = () => {
      void performRunAction("apply_recommendation");
    };
    document.getElementById("statusRunPromote").onclick = () => {
      void performRunAction("promote_skills");
    };
    document.getElementById("schedulerRefresh").onclick = () => {
      void fetchSchedulerJobs();
    };
    document.getElementById("schedulerProject").onchange = () => {
      applySchedulerStateFromControls();
      state.schedulerSelectedJobId = "";
      void fetchSchedulerJobs();
    };
    document.getElementById("schedulerSession").onchange = () => {
      applySchedulerStateFromControls();
    };
    document.getElementById("schedulerSave").onclick = () => {
      void saveSchedulerJob();
    };
    document.getElementById("schedulerDelete").onclick = () => {
      void deleteSchedulerJob();
    };
    document.getElementById("schedulerRunNow").onclick = () => {
      void runSchedulerJobNow();
    };
    document.getElementById("schedulerPause").onclick = () => {
      void pauseSchedulerJob();
    };
    document.getElementById("schedulerResume").onclick = () => {
      void resumeSchedulerJob();
    };
    document.getElementById("schedulerReset").onclick = () => {
      resetSchedulerForm();
    };
    const adminApply = document.getElementById("adminApply");
    if (adminApply) {
      adminApply.onclick = () => {
        applyAdminStateFromControls();
        void fetchAdminStatus();
      };
    }
    [
      ["adminEnableAction", "enable"],
      ["adminDisableAction", "disable"],
      ["adminRescanAction", "rescan"],
      ["adminEnableActionActive", "enable"],
      ["adminDisableActionActive", "disable"],
      ["adminRescanActionActive", "rescan"],
    ].forEach(([elementId, action]) => {
      const button = document.getElementById(elementId);
      if (!button) return;
      button.onclick = () => {
        void performAdminAction(action);
      };
    });
    const adminSkillApprovalApprove = document.getElementById("adminSkillApprovalApprove");
    if (adminSkillApprovalApprove) {
      adminSkillApprovalApprove.onclick = () => {
        const approvalId = String(document.getElementById("adminSkillApprovalSelect")?.value || "").trim();
        if (!approvalId) {
          setAdminStatusBanner(t("miniapp.admin.choose_skill", "Выберите pending skill install."), false);
          return;
        }
        void performAdminAction("approve_skill_install", { approval_id: approvalId });
      };
    }
    const adminSkillApprovalReject = document.getElementById("adminSkillApprovalReject");
    if (adminSkillApprovalReject) {
      adminSkillApprovalReject.onclick = () => {
        const approvalId = String(document.getElementById("adminSkillApprovalSelect")?.value || "").trim();
        if (!approvalId) {
          setAdminStatusBanner(t("miniapp.admin.choose_skill", "Выберите pending skill install."), false);
          return;
        }
        void performAdminAction("reject_skill_install", { approval_id: approvalId });
      };
    }
    const adminRunsRefresh = document.getElementById("adminRunsRefresh");
    if (adminRunsRefresh) {
      adminRunsRefresh.onclick = () => {
        void refreshAdminRuns();
      };
    }
    const adminRunsView = document.getElementById("adminRunsView");
    if (adminRunsView) {
      adminRunsView.onclick = () => {
        void viewAdminRunDetail();
      };
    }
    const adminConfigReload = document.getElementById("adminConfigReload");
    if (adminConfigReload) {
      adminConfigReload.onclick = () => {
        void reloadAdminConfig();
      };
    }
    const adminMonitorServersReload = document.getElementById("adminMonitorServersReload");
    if (adminMonitorServersReload) {
      adminMonitorServersReload.onclick = () => {
        void reloadAdminMonitorServers();
      };
    }
    const adminMonitorServersAdd = document.getElementById("adminMonitorServersAdd");
    if (adminMonitorServersAdd) {
      adminMonitorServersAdd.onclick = () => {
        addAdminMonitorServerRow();
      };
    }
    const adminMonitorServersSave = document.getElementById("adminMonitorServersSave");
    if (adminMonitorServersSave) {
      adminMonitorServersSave.onclick = () => {
        void saveAdminMonitorServers();
      };
    }
    const adminMonitorServersBody = document.getElementById("adminMonitorServersBody");
    if (adminMonitorServersBody) {
      adminMonitorServersBody.addEventListener("click", (evt) => {
        const target = evt.target;
        if (!(target instanceof HTMLElement)) return;
        if (target.dataset.action !== "delete-monitor-server") return;
        const index = Number(target.dataset.index || "");
        if (!Number.isFinite(index)) return;
        state.adminMonitorServers = collectAdminMonitorServersFromUi();
        state.adminMonitorServers.splice(index, 1);
        renderAdminMonitorServers(state.adminMonitorServers);
      });
    }
    const adminSshActionsReload = document.getElementById("adminSshActionsReload");
    if (adminSshActionsReload) {
      adminSshActionsReload.onclick = () => {
        void reloadAdminSshActions();
      };
    }
    const adminSshActionsAdd = document.getElementById("adminSshActionsAdd");
    if (adminSshActionsAdd) {
      adminSshActionsAdd.onclick = () => {
        addAdminSshActionRow();
      };
    }
    const adminSshActionsSave = document.getElementById("adminSshActionsSave");
    if (adminSshActionsSave) {
      adminSshActionsSave.onclick = () => {
        void saveAdminSshActions();
      };
    }
    const adminSshActionsBody = document.getElementById("adminSshActionsBody");
    if (adminSshActionsBody) {
      adminSshActionsBody.addEventListener("click", (evt) => {
        const target = evt.target;
        if (!(target instanceof HTMLElement)) return;
        if (target.dataset.action !== "delete-ssh-action") return;
        const index = Number(target.dataset.index || "");
        if (!Number.isFinite(index)) return;
        state.adminSshActions = collectAdminSshActionsFromUi();
        state.adminSshActions.splice(index, 1);
        renderAdminSshActions(state.adminSshActions);
      });
    }
    const autonomyRefresh = document.getElementById("autonomyRefresh");
    if (autonomyRefresh) {
      autonomyRefresh.onclick = () => { void refreshAutonomy(); };
    }
    const autonomyRescanAll = document.getElementById("autonomyRescanAll");
    if (autonomyRescanAll) {
      autonomyRescanAll.onclick = () => { void autonomyRescanAllAction(); };
    }
    const autonomyMaintenance = document.getElementById("autonomyMaintenance");
    if (autonomyMaintenance) {
      autonomyMaintenance.onclick = () => { void autonomyRunDailyMaintenance(); };
    }

    const langSave = document.getElementById("langSave");
    if (langSave) {
      langSave.addEventListener("click", async () => {
        const lang = document.getElementById("langSelect").value;
        try {
          await api("/i18n/user-lang", { method: "PUT", body: JSON.stringify({ lang }), headers: { "Content-Type": "application/json" } });
        } catch { /* ignore save errors */ }
        await loadI18n(lang);
        applyI18nToDOM();
        document.getElementById("langSelect").value = lang;
      });
    }

    const langSelect = document.getElementById("langSelect");
    if (langSelect) {
      langSelect.value = i18n.lang;
    }

    window.addEventListener("resize", () => {
      if (document.getElementById("tab-editor").classList.contains("active")) {
        scheduleEditorResize();
      }
    });
    window.addEventListener("beforeunload", () => {
      disconnectLogsWs();
      disconnectStatusWs();
      stopAdminPolling();
      stopRunsPolling();
    });

    document.getElementById("tasksRefresh").onclick = () => {
      void fetchTasks();
    };

    document.getElementById("reportsRefresh").onclick = () => {
      state.reportsSessionUid = String(document.getElementById("reportsSession")?.value || "");
      void fetchReports();
    };
    document.getElementById("reportsSession").onchange = () => {
      state.reportsSessionUid = String(document.getElementById("reportsSession")?.value || "");
      state.reportsSelectedId = null;
      state.reportsSelectedSessionUid = null;
      const dlBtn = document.getElementById("reportsDownloadMd");
      if (dlBtn) dlBtn.disabled = true;
      void fetchReports();
    };
    document.getElementById("reportsDownloadMd").onclick = () => {
      void downloadReportMd();
    };
  }

  // ==================================================================
  // Admin Autonomy (baseline / drift / memory / runbooks / snapshots)
  // ==================================================================

  function autonomyCurrentSessionUid() {
    return String(state.adminSessionUid || "").trim();
  }

  function setAutonomyStatus(text, ok = true) {
    const el = document.getElementById("autonomyStatusMessage");
    if (!el) return;
    el.textContent = String(text || "");
    el.className = "autonomy-status-msg " + (ok ? "status-ok" : "status-error");
  }

  function autonomyEscape(value) {
    const str = value == null ? "" : String(value);
    return str
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function autonomyFormatError(err) {
    const code = err && err.body && err.body.code;
    const msg = (err && err.message) || "";
    if (code === "checksum_mismatch") {
      return `<div class="status-error"><strong>⚠ Integrity mismatch:</strong> ${autonomyEscape(msg)}<br><small>${escapeHtml(t("miniapp.autonomy.checksum_mismatch", "SHA1 расходится с зафиксированным в runbook — скрипт мог быть изменён. Выполнение остановлено."))}</small></div>`;
    }
    return `<div class="status-error">${escapeHtml(t("miniapp.error.generic_prefix", "Ошибка:"))} ${autonomyEscape(msg)}</div>`;
  }

  async function autonomyRunWithLock(btn, asyncFn) {
    const buttons = Array.isArray(btn) ? btn.filter(Boolean) : (btn ? [btn] : []);
    buttons.forEach((b) => { b.disabled = true; });
    try {
      return await asyncFn();
    } finally {
      buttons.forEach((b) => { b.disabled = false; });
    }
  }

  function autonomyYamlDump(value, indent) {
    const pad = " ".repeat(indent || 0);
    if (value === null || value === undefined) return pad + "null";
    if (typeof value === "string") {
      if (value.includes("\n") || value.includes(":")) {
        return pad + JSON.stringify(value);
      }
      return pad + value;
    }
    if (typeof value === "number" || typeof value === "boolean") return pad + String(value);
    if (Array.isArray(value)) {
      if (!value.length) return pad + "[]";
      return value.map((item) => {
        const rendered = autonomyYamlDump(item, (indent || 0) + 2).replace(/^\s*/, "");
        return pad + "- " + rendered;
      }).join("\n");
    }
    if (typeof value === "object") {
      const keys = Object.keys(value);
      if (!keys.length) return pad + "{}";
      return keys.map((k) => {
        const v = value[k];
        if (v !== null && typeof v === "object") {
          return pad + k + ":\n" + autonomyYamlDump(v, (indent || 0) + 2);
        }
        return pad + k + ": " + autonomyYamlDump(v, 0).trim();
      }).join("\n");
    }
    return pad + String(value);
  }

  function autonomyInlineValue(value) {
    if (value !== null && typeof value === "object") {
      return autonomyYamlDump(value, 0).replace(/\n+/g, " | ");
    }
    return statusValueText(value);
  }

  function autonomyBadgeForStatus(status) {
    const s = String(status || "").toLowerCase();
    const label = {
      ok: "ok",
      warn: "warn",
      alarm: "alarm",
      proposed_baseline: "baseline?",
      no_baseline: "no baseline",
    }[s] || s;
    return `<span class="autonomy-badge autonomy-badge-${s}">${autonomyEscape(label)}</span>`;
  }

  async function refreshAutonomy() {
    const sessionUid = autonomyCurrentSessionUid();
    if (!sessionUid) {
      setAutonomyStatus(t("miniapp.autonomy.no_session", "Сессия не выбрана."), false);
      renderAutonomyDashboard(null, []);
      return;
    }
    setAutonomyStatus(t("miniapp.status.loading", "Загрузка…"));
    try {
      const payload = await api(`/v1/admin/autonomy/dashboard?session_uid=${encodeURIComponent(sessionUid)}`);
      renderAutonomyDashboard(payload.global || null, Array.isArray(payload.servers) ? payload.servers : []);
      setAutonomyStatus("");
    } catch (err) {
      setAutonomyStatus(`${t("miniapp.error.load_prefix", "Ошибка загрузки:")} ${err.message || "unknown"}`, false);
    }
  }

  function renderAutonomyDashboard(globalSummary, servers) {
    const summaryEl = document.getElementById("autonomyGlobalSummary");
    const grid = document.getElementById("autonomyServersGrid");
    if (summaryEl) {
      if (globalSummary) {
        const totals = globalSummary.totals || {};
        const statuses = globalSummary.statuses || {};
        const parts = [];
        parts.push(`<strong>Servers:</strong> ${Number(globalSummary.server_count || 0)}`);
        const statusParts = Object.keys(statuses).map((k) => `${autonomyEscape(k)}: ${statuses[k]}`);
        if (statusParts.length) parts.push(`<strong>${escapeHtml(t("miniapp.autonomy.statuses_label", "Статусы"))}</strong>: ${statusParts.join(", ")}`);
        const totalsParts = ["alarm", "warn", "info", "noise"].filter((k) => totals[k]).map((k) => `${k}: ${totals[k]}`);
        if (totalsParts.length) parts.push(`<strong>Open drifts:</strong> ${totalsParts.join(", ")}`);
        if (globalSummary.admin_root) parts.push(`<small>${autonomyEscape(globalSummary.admin_root)}</small>`);
        summaryEl.innerHTML = parts.join(" • ");
      } else {
        summaryEl.innerHTML = "";
      }
    }
    if (!grid) return;
    grid.innerHTML = "";
    if (!servers.length) {
      grid.innerHTML = `<div class="status-empty">${escapeHtml(t("miniapp.autonomy.no_servers", "Нет серверов. Добавьте их в admin-конфиг."))}</div>`;
      return;
    }
    for (const s of servers) {
      grid.appendChild(renderAutonomyServerCard(s));
    }
  }

  function renderAutonomyServerCard(server) {
    const card = document.createElement("div");
    card.className = "autonomy-server-card";
    card.dataset.serverId = String(server.server_id || "");
    const drifts = server.open_drifts || {};
    const driftsParts = ["alarm", "warn", "info", "noise"]
      .filter((k) => Number(drifts[k] || 0) > 0)
      .map((k) => `<span class="autonomy-drift-${k}">${k}: ${drifts[k]}</span>`);
    const tags = Array.isArray(server.tags) ? server.tags.join(", ") : "";
    card.innerHTML = `
      <div class="autonomy-server-card-header">
        <strong>${autonomyEscape(server.label || server.server_id)}</strong>
        ${autonomyBadgeForStatus(server.status)}
      </div>
      <div class="autonomy-server-card-meta">
        <span>${autonomyEscape(server.transport || "")}${server.host ? " • " + autonomyEscape(server.host) : ""}</span>
        ${tags ? `<span class="autonomy-server-tags">${autonomyEscape(tags)}</span>` : ""}
      </div>
      <div class="autonomy-server-card-drifts">
        ${driftsParts.join(" ") || `<span class="autonomy-drift-none">${escapeHtml(t("miniapp.autonomy.no_open_drifts", "нет открытых drift"))}</span>`}
      </div>
      <div class="autonomy-server-card-footer">
        <span>memory: ${Number(server.memory_entries || 0)}</span>
        <span>scan: ${autonomyEscape(server.last_scan_ts || "—")}</span>
      </div>
    `;
    card.onclick = () => { void showAutonomyServerDetail(String(server.server_id || "")); };
    return card;
  }

  async function autonomyRescanAllAction() {
    const sessionUid = autonomyCurrentSessionUid();
    if (!sessionUid) { setAutonomyStatus(t("miniapp.autonomy.no_session", "Сессия не выбрана."), false); return; }
    setAutonomyStatus(t("miniapp.autonomy.rescan_all_started", "Rescan all запущен…"));
    try {
      await api(`/v1/admin/autonomy/rescan_all?session_uid=${encodeURIComponent(sessionUid)}`, { method: "POST" });
      setAutonomyStatus(t("miniapp.autonomy.rescan_all_done", "Rescan all завершён."));
      await refreshAutonomy();
    } catch (err) {
      setAutonomyStatus(`${t("miniapp.autonomy.err_rescan_all", "Ошибка rescan all:")} ${err.message || "unknown"}`, false);
    }
  }

  async function autonomyRunDailyMaintenance() {
    const sessionUid = autonomyCurrentSessionUid();
    if (!sessionUid) { setAutonomyStatus(t("miniapp.autonomy.no_session", "Сессия не выбрана."), false); return; }
    setAutonomyStatus(t("miniapp.autonomy.maintenance_started", "Daily maintenance запущен…"));
    try {
      const payload = await api("/v1/admin/autonomy/maintenance/daily", {
        method: "POST",
        body: JSON.stringify({ session_uid: sessionUid }),
      });
      const servers = (payload.report && payload.report.servers) || {};
      const count = Object.keys(servers).length;
      setAutonomyStatus(t("miniapp.autonomy.maintenance_done", "Daily maintenance готов (серверов: {count}).").replace("{count}", count));
    } catch (err) {
      setAutonomyStatus(`${t("miniapp.autonomy.err_maintenance", "Ошибка maintenance:")} ${err.message || "unknown"}`, false);
    }
  }

  async function showAutonomyServerDetail(serverId) {
    const sessionUid = autonomyCurrentSessionUid();
    if (!sessionUid || !serverId) return;
    const panel = document.getElementById("autonomyServerDetail");
    if (!panel) return;
    panel.classList.remove("hidden");
    panel.dataset.serverId = serverId;
    panel.innerHTML = `<div class="status-empty">${escapeHtml(t("miniapp.autonomy.loading_server", "Загрузка {server}…").replace("{server}", serverId))}</div>`;
    let detail;
    try {
      detail = await api(
        `/v1/admin/autonomy/servers/${encodeURIComponent(serverId)}?session_uid=${encodeURIComponent(sessionUid)}`
      );
    } catch (err) {
      panel.innerHTML = `<div class="status-error">${escapeHtml(t("miniapp.error.generic_prefix", "Ошибка:"))} ${autonomyEscape(err.message || "")}</div>`;
      return;
    }
    renderAutonomyServerDetailShell(panel, serverId, detail.summary || {}, detail.dossier || {});
    await autonomyActivateTab(serverId, "overview");
  }

  function renderAutonomyServerDetailShell(panel, serverId, summary, dossier) {
    const sidSafe = autonomyEscape(serverId);
    panel.innerHTML = `
      <div class="autonomy-detail-header">
        <div>
          <h3 class="autonomy-detail-title">${autonomyEscape(summary.label || serverId)}</h3>
          <div class="autonomy-detail-meta">
            ${autonomyBadgeForStatus(summary.status)}
            <span>${autonomyEscape(summary.transport || "")}${summary.host ? " • " + autonomyEscape(summary.host) : ""}</span>
            <span>last scan: ${autonomyEscape(summary.last_scan_ts || "—")}</span>
          </div>
        </div>
        <div class="autonomy-detail-actions">
          <button type="button" data-autonomy-act="rescan" data-server-id="${sidSafe}">Rescan now</button>
          <button type="button" data-autonomy-act="close">${escapeHtml(t("miniapp.btn.close", "Закрыть"))}</button>
        </div>
      </div>
      <nav class="autonomy-tabs" role="tablist">
        <button type="button" data-autonomy-tab="overview" class="active">Overview</button>
        <button type="button" data-autonomy-tab="baseline">Baseline</button>
        <button type="button" data-autonomy-tab="drifts">Drifts</button>
        <button type="button" data-autonomy-tab="memory">Memory</button>
        <button type="button" data-autonomy-tab="runbooks">Runbooks</button>
        <button type="button" data-autonomy-tab="builder">Builder</button>
        <button type="button" data-autonomy-tab="snapshots">Snapshots</button>
        <button type="button" data-autonomy-tab="prereqs">Prereqs</button>
        <button type="button" data-autonomy-tab="actions">Actions</button>
      </nav>
      <div class="autonomy-tab-body" id="autonomyTabBody"></div>
    `;
    panel._autonomyDossier = dossier || {};
    panel._autonomySummary = summary || {};
    panel.querySelectorAll("[data-autonomy-tab]").forEach((btn) => {
      btn.onclick = () => {
        panel.querySelectorAll("[data-autonomy-tab]").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        void autonomyActivateTab(serverId, btn.dataset.autonomyTab);
      };
    });
    panel.querySelectorAll("[data-autonomy-act]").forEach((btn) => {
      const act = btn.dataset.autonomyAct;
      if (act === "close") {
        btn.onclick = () => { panel.classList.add("hidden"); };
      } else if (act === "rescan") {
        btn.onclick = () => { void autonomyRescanServer(serverId); };
      }
    });
  }

  async function autonomyActivateTab(serverId, tab) {
    const panel = document.getElementById("autonomyServerDetail");
    const body = document.getElementById("autonomyTabBody");
    if (!panel || !body) return;
    const sessionUid = autonomyCurrentSessionUid();
    const dossier = panel._autonomyDossier || {};
    const summary = panel._autonomySummary || {};
    body.innerHTML = `<div class="status-empty">${escapeHtml(t("miniapp.status.loading", "Загрузка…"))}</div>`;
    if (tab === "overview") {
      renderAutonomyOverview(body, summary, dossier);
    } else if (tab === "baseline") {
      await renderAutonomyBaselineTab(body, serverId, sessionUid);
    } else if (tab === "drifts") {
      await renderAutonomyDriftsTab(body, serverId, sessionUid);
    } else if (tab === "memory") {
      await renderAutonomyMemoryTab(body, serverId, sessionUid);
    } else if (tab === "runbooks") {
      await renderAutonomyRunbooksTab(body, serverId, sessionUid, dossier);
    } else if (tab === "builder") {
      await renderAutonomyBuilderTab(body, serverId, sessionUid);
    } else if (tab === "snapshots") {
      await renderAutonomySnapshotsTab(body, serverId, sessionUid);
    } else if (tab === "prereqs") {
      await renderAutonomyPrereqsTab(body, serverId, sessionUid);
    } else if (tab === "actions") {
      await renderAutonomyActionsTab(body, serverId, sessionUid, dossier);
    }
  }

  function renderAutonomyOverview(body, summary, dossier) {
    const facts = (dossier && dossier.memory && dossier.memory.facts) || {};
    const notes = (dossier && dossier.memory && dossier.memory.notes_recent) || [];
    const runbooks = (dossier && dossier.matched_runbooks) || [];
    const recentDrifts = (dossier && dossier.recent_drifts) || [];
    const factsHtml = Object.keys(facts).length
      ? '<dl class="autonomy-facts-list">' + Object.keys(facts).map((k) => (
          `<dt>${autonomyEscape(k)}</dt><dd>${autonomyEscape(autonomyInlineValue(facts[k]))}</dd>`
        )).join("") + "</dl>"
      : `<div class="status-empty">${escapeHtml(t("miniapp.autonomy.no_facts", "Нет facts."))}</div>`;
    const notesHtml = notes.length
      ? notes.map((n) => `<div class="autonomy-note-row"><small>${autonomyEscape(n.ts || "")} • ${autonomyEscape(n.source || "")}</small><div>${autonomyEscape(n.text || "")}</div></div>`).join("")
      : `<div class="status-empty">${escapeHtml(t("miniapp.autonomy.no_notes", "Нет notes."))}</div>`;
    const rbHtml = runbooks.length
      ? '<ul class="autonomy-rb-list">' + runbooks.map((rb) => `<li><strong>${autonomyEscape(rb.title || rb.id || "")}</strong> <small>(${autonomyEscape((rb.tags || []).join(", "))})</small></li>`).join("") + "</ul>"
      : `<div class="status-empty">${escapeHtml(t("miniapp.autonomy.no_runbooks", "Нет совпадающих runbooks."))}</div>`;
    const driftsHtml = recentDrifts.length
      ? '<ul class="autonomy-drift-list">' + recentDrifts.slice(0, 5).map((d) => `<li><span class="autonomy-drift-${autonomyEscape(d.severity || "")}">${autonomyEscape(d.severity || "")}</span> ${autonomyEscape(d.check_id || "")} <small>${autonomyEscape(d.ts || "")}</small></li>`).join("") + "</ul>"
      : `<div class="status-empty">${escapeHtml(t("miniapp.autonomy.no_drift", "Нет drift."))}</div>`;
    body.innerHTML = `
      <div class="autonomy-subgrid">
        <div class="autonomy-subcard">
          <h4>Facts</h4>
          ${factsHtml}
        </div>
        <div class="autonomy-subcard">
          <h4>Recent notes</h4>
          ${notesHtml}
        </div>
        <div class="autonomy-subcard">
          <h4>Matched runbooks</h4>
          ${rbHtml}
        </div>
        <div class="autonomy-subcard">
          <h4>Recent drifts</h4>
          ${driftsHtml}
        </div>
      </div>
    `;
  }

  async function renderAutonomyBaselineTab(body, serverId, sessionUid) {
    let payload;
    try {
      const res = await api(`/v1/admin/autonomy/servers/${encodeURIComponent(serverId)}/baseline?session_uid=${encodeURIComponent(sessionUid)}`);
      payload = res.baseline || {};
    } catch (err) {
      body.innerHTML = `<div class="status-error">${escapeHtml(t("miniapp.error.generic_prefix", "Ошибка:"))} ${autonomyEscape(err.message || "")}</div>`;
      return;
    }
    const baselineYaml = autonomyYamlDump(payload.baseline || {}, 0);
    const proposedYaml = payload.has_proposed ? autonomyYamlDump(payload.proposed || {}, 0) : "";
    const diffHtml = payload.has_proposed
      ? renderAutonomyBaselineDiff(baselineYaml, proposedYaml)
      : "";
    body.innerHTML = `
      <div class="autonomy-baseline-toolbar">
        <span><strong>Baseline path:</strong> <code>${autonomyEscape(payload.baseline_path || "")}</code></span>
        ${payload.has_proposed ? '<button type="button" data-act="accept">Accept proposed</button><button type="button" data-act="discard">Discard proposal</button>' : ""}
        ${payload.has_proposed ? '<button type="button" data-view="split">Side-by-side</button><button type="button" data-view="diff">Diff</button>' : ""}
      </div>
      <div class="autonomy-baseline-views">
        <div class="autonomy-baseline-split">
          <div class="autonomy-baseline-cols">
            <div>
              <h4>Baseline</h4>
              <pre class="autonomy-baseline-yaml">${autonomyEscape(baselineYaml || t("miniapp.autonomy.empty", "(пусто)"))}</pre>
            </div>
            ${payload.has_proposed ? `<div><h4>Proposed</h4><pre class="autonomy-baseline-yaml autonomy-baseline-proposed">${autonomyEscape(proposedYaml)}</pre></div>` : ""}
          </div>
        </div>
        ${payload.has_proposed ? `<div class="autonomy-baseline-diff hidden"><pre class="autonomy-baseline-diff-body">${diffHtml}</pre></div>` : ""}
      </div>
    `;
    const splitView = body.querySelector(".autonomy-baseline-split");
    const diffView = body.querySelector(".autonomy-baseline-diff");
    body.querySelectorAll("[data-view]").forEach((btn) => {
      btn.onclick = () => {
        const kind = btn.dataset.view;
        if (kind === "split" && splitView && diffView) {
          splitView.classList.remove("hidden");
          diffView.classList.add("hidden");
        } else if (kind === "diff" && splitView && diffView) {
          splitView.classList.add("hidden");
          diffView.classList.remove("hidden");
        }
      };
    });
    const acceptBtn = body.querySelector('[data-act="accept"]');
    if (acceptBtn) {
      acceptBtn.onclick = async () => {
        try {
          await api(`/v1/admin/autonomy/servers/${encodeURIComponent(serverId)}/baseline/accept`, {
            method: "POST",
            body: JSON.stringify({ session_uid: sessionUid }),
          });
          setAutonomyStatus(t("miniapp.autonomy.baseline_accepted", "Baseline accepted для {server}.").replace("{server}", serverId));
          await renderAutonomyBaselineTab(body, serverId, sessionUid);
          await refreshAutonomy();
        } catch (err) {
          setAutonomyStatus(`${t("miniapp.autonomy.err_accept", "Ошибка accept:")} ${err.message || "unknown"}`, false);
        }
      };
    }
    const discardBtn = body.querySelector('[data-act="discard"]');
    if (discardBtn) {
      discardBtn.onclick = async () => {
        try {
          await api(`/v1/admin/autonomy/servers/${encodeURIComponent(serverId)}/baseline/proposal?session_uid=${encodeURIComponent(sessionUid)}`, { method: "DELETE" });
          setAutonomyStatus(t("miniapp.autonomy.proposal_discarded", "Proposal discarded для {server}.").replace("{server}", serverId));
          await renderAutonomyBaselineTab(body, serverId, sessionUid);
          await refreshAutonomy();
        } catch (err) {
          setAutonomyStatus(`${t("miniapp.autonomy.err_discard", "Ошибка discard:")} ${err.message || "unknown"}`, false);
        }
      };
    }
  }

  async function renderAutonomyDriftsTab(body, serverId, sessionUid) {
    const prevSeverity = body._autonomySeverityMin || "";
    const prevOpenOnly = body._autonomyOpenOnly !== false;
    body.innerHTML = `
      <div class="autonomy-drift-controls admin-state-actions">
        <label>severity ≥
          <select id="autonomyDriftSeverity">
            <option value="">all</option>
            <option value="noise">noise</option>
            <option value="info">info</option>
            <option value="warn">warn</option>
            <option value="alarm">alarm</option>
          </select>
        </label>
        <label><input type="checkbox" id="autonomyDriftOpenOnly" ${prevOpenOnly ? "checked" : ""}/> open only</label>
        <button type="button" id="autonomyDriftRefresh">${escapeHtml(t("miniapp.btn.refresh", "Обновить"))}</button>
      </div>
      <div id="autonomyDriftTableWrap"></div>
    `;
    const sevSel = body.querySelector("#autonomyDriftSeverity");
    if (sevSel && prevSeverity) sevSel.value = prevSeverity;
    const reload = async () => {
      body._autonomySeverityMin = String(sevSel.value || "");
      body._autonomyOpenOnly = !!body.querySelector("#autonomyDriftOpenOnly").checked;
      await autonomyReloadDrifts(body, serverId, sessionUid);
    };
    sevSel.onchange = reload;
    body.querySelector("#autonomyDriftOpenOnly").onchange = reload;
    body.querySelector("#autonomyDriftRefresh").onclick = reload;
    await autonomyReloadDrifts(body, serverId, sessionUid);
  }

  async function autonomyReloadDrifts(body, serverId, sessionUid) {
    const wrap = body.querySelector("#autonomyDriftTableWrap");
    if (!wrap) return;
    const severityMin = String(body._autonomySeverityMin || "");
    const openOnly = body._autonomyOpenOnly !== false;
    wrap.innerHTML = `<div class="status-empty">${escapeHtml(t("miniapp.status.loading", "Загрузка…"))}</div>`;
    let drifts;
    try {
      const qs = new URLSearchParams({
        session_uid: sessionUid,
        limit: "200",
        open_only: openOnly ? "true" : "false",
      });
      const res = await api(`/v1/admin/autonomy/servers/${encodeURIComponent(serverId)}/drifts?${qs.toString()}`);
      drifts = Array.isArray(res.drifts) ? res.drifts : [];
    } catch (err) {
      wrap.innerHTML = `<div class="status-error">${escapeHtml(t("miniapp.error.generic_prefix", "Ошибка:"))} ${autonomyEscape(err.message || "")}</div>`;
      return;
    }
    if (severityMin) {
      const order = { noise: 0, info: 1, warn: 2, alarm: 3 };
      const threshold = order[severityMin] || 0;
      drifts = drifts.filter((d) => (order[String(d.severity || "").toLowerCase()] || 0) >= threshold);
    }
    if (!drifts.length) {
      wrap.innerHTML = `<div class="status-empty">${escapeHtml(t("miniapp.autonomy.no_drifts_filtered", "Нет подходящих drift."))}</div>`;
      return;
    }
    const rows = drifts.map((d) => `
      <tr data-drift-id="${autonomyEscape(d.id != null ? d.id : "")}">
        <td>${autonomyEscape(d.ts || "")}</td>
        <td>${autonomyEscape(d.check_id || "")}</td>
        <td><span class="autonomy-drift-${autonomyEscape(d.severity || "")}">${autonomyEscape(d.severity || "")}</span></td>
        <td>${autonomyEscape(d.kind || "")}</td>
        <td>${autonomyEscape((d.summary || d.message || "")).slice(0, 120)}</td>
        <td>${d.id != null ? `<button type="button" data-ack="${autonomyEscape(d.id)}">Ack</button>` : ""}</td>
      </tr>
    `).join("");
    wrap.innerHTML = `
      <table class="autonomy-drifts-table">
        <thead><tr><th>ts</th><th>check</th><th>severity</th><th>kind</th><th>summary</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    `;
    wrap.querySelectorAll("[data-ack]").forEach((btn) => {
      btn.onclick = async () => {
        const driftId = btn.dataset.ack;
        try {
          await api(`/v1/admin/autonomy/servers/${encodeURIComponent(serverId)}/drifts/${encodeURIComponent(driftId)}/ack`, {
            method: "POST",
            body: JSON.stringify({ session_uid: sessionUid, by: "miniapp" }),
          });
          setAutonomyStatus(`Drift #${driftId} acknowledged.`);
          await autonomyReloadDrifts(body, serverId, sessionUid);
        } catch (err) {
          setAutonomyStatus(`${t("miniapp.autonomy.err_ack", "Ошибка ack:")} ${err.message || "unknown"}`, false);
        }
      };
    });
  }

  async function renderAutonomyMemoryTab(body, serverId, sessionUid) {
    let payload;
    try {
      const res = await api(`/v1/admin/autonomy/servers/${encodeURIComponent(serverId)}/memory?session_uid=${encodeURIComponent(sessionUid)}`);
      payload = res.memory || {};
    } catch (err) {
      body.innerHTML = `<div class="status-error">${escapeHtml(t("miniapp.error.generic_prefix", "Ошибка:"))} ${autonomyEscape(err.message || "")}</div>`;
      return;
    }
    const facts = payload.facts || {};
    const factRows = Object.keys(facts).map((k) => {
      const v = facts[k];
      const valDisplay = autonomyInlineValue(v);
      return `
        <tr data-fact-key="${autonomyEscape(k)}">
          <td><code>${autonomyEscape(k)}</code></td>
          <td><input type="text" class="autonomy-fact-value" value="${autonomyEscape(valDisplay)}"></td>
          <td>
            <button type="button" data-fact-save="${autonomyEscape(k)}">Save</button>
            <button type="button" data-fact-delete="${autonomyEscape(k)}">Del</button>
          </td>
        </tr>`;
    }).join("");
    body.innerHTML = `
      <div class="autonomy-subgrid autonomy-memory-grid">
        <div class="autonomy-subcard">
          <h4>Facts</h4>
          <table class="autonomy-facts-table">
            <thead><tr><th>key</th><th>value</th><th></th></tr></thead>
            <tbody>${factRows || `<tr><td colspan="3" class="status-empty">${escapeHtml(t("miniapp.autonomy.no_facts", "Нет facts."))}</td></tr>`}</tbody>
          </table>
          <div class="autonomy-fact-add">
            <input type="text" id="autonomyNewFactKey" placeholder="key">
            <input type="text" id="autonomyNewFactValue" placeholder="value">
            <button type="button" id="autonomyAddFact">Add fact</button>
          </div>
        </div>
        <div class="autonomy-subcard">
          <h4>Notes</h4>
          <pre class="autonomy-notes-text">${autonomyEscape(payload.notes_text || t("miniapp.autonomy.empty", "(пусто)"))}</pre>
          <textarea id="autonomyNewNote" rows="3" placeholder="${escapeHtml(t("miniapp.autonomy.new_note_placeholder", "Новая заметка…"))}"></textarea>
          <div class="admin-state-actions">
            <button type="button" id="autonomyAppendNote">Append note</button>
            <button type="button" id="autonomyCompactNotes">Compact notes</button>
          </div>
        </div>
      </div>
    `;
    body.querySelectorAll("[data-fact-save]").forEach((btn) => {
      btn.onclick = async () => {
        const key = btn.dataset.factSave;
        const row = body.querySelector(`tr[data-fact-key="${CSS.escape(key)}"]`);
        const input = row ? row.querySelector(".autonomy-fact-value") : null;
        const value = input ? String(input.value) : "";
        try {
          await api(`/v1/admin/autonomy/servers/${encodeURIComponent(serverId)}/memory/facts`, {
            method: "POST",
            body: JSON.stringify({ session_uid: sessionUid, key, value, by: "miniapp" }),
          });
          setAutonomyStatus(`Fact ${key} saved.`);
        } catch (err) {
          setAutonomyStatus(`${t("miniapp.autonomy.err_save_fact", "Ошибка save fact:")} ${err.message || "unknown"}`, false);
        }
      };
    });
    body.querySelectorAll("[data-fact-delete]").forEach((btn) => {
      btn.onclick = async () => {
        const key = btn.dataset.factDelete;
        try {
          await api(`/v1/admin/autonomy/servers/${encodeURIComponent(serverId)}/memory/facts/${encodeURIComponent(key)}?session_uid=${encodeURIComponent(sessionUid)}`, {
            method: "DELETE",
          });
          setAutonomyStatus(`Fact ${key} deleted.`);
          await renderAutonomyMemoryTab(body, serverId, sessionUid);
        } catch (err) {
          setAutonomyStatus(`${t("miniapp.autonomy.err_delete_fact", "Ошибка delete fact:")} ${err.message || "unknown"}`, false);
        }
      };
    });
    const addBtn = body.querySelector("#autonomyAddFact");
    if (addBtn) {
      addBtn.onclick = async () => {
        const key = String(body.querySelector("#autonomyNewFactKey")?.value || "").trim();
        const value = String(body.querySelector("#autonomyNewFactValue")?.value || "");
        if (!key) { setAutonomyStatus("key required", false); return; }
        try {
          await api(`/v1/admin/autonomy/servers/${encodeURIComponent(serverId)}/memory/facts`, {
            method: "POST",
            body: JSON.stringify({ session_uid: sessionUid, key, value, by: "miniapp" }),
          });
          setAutonomyStatus(`Fact ${key} added.`);
          await renderAutonomyMemoryTab(body, serverId, sessionUid);
        } catch (err) {
          setAutonomyStatus(`${t("miniapp.autonomy.err_add_fact", "Ошибка add fact:")} ${err.message || "unknown"}`, false);
        }
      };
    }
    const appendBtn = body.querySelector("#autonomyAppendNote");
    if (appendBtn) {
      appendBtn.onclick = async () => {
        const text = String(body.querySelector("#autonomyNewNote")?.value || "").trim();
        if (!text) { setAutonomyStatus("text required", false); return; }
        try {
          await api(`/v1/admin/autonomy/servers/${encodeURIComponent(serverId)}/memory/notes`, {
            method: "POST",
            body: JSON.stringify({ session_uid: sessionUid, text, source: "miniapp" }),
          });
          setAutonomyStatus("Note appended.");
          await renderAutonomyMemoryTab(body, serverId, sessionUid);
        } catch (err) {
          setAutonomyStatus(`${t("miniapp.autonomy.err_append_note", "Ошибка append note:")} ${err.message || "unknown"}`, false);
        }
      };
    }
    const compactBtn = body.querySelector("#autonomyCompactNotes");
    if (compactBtn) {
      compactBtn.onclick = async () => {
        try {
          await api(`/v1/admin/autonomy/servers/${encodeURIComponent(serverId)}/memory/compact`, {
            method: "POST",
            body: JSON.stringify({ session_uid: sessionUid, force: false }),
          });
          setAutonomyStatus("Notes compacted.");
          await renderAutonomyMemoryTab(body, serverId, sessionUid);
        } catch (err) {
          setAutonomyStatus(`${t("miniapp.autonomy.err_compact", "Ошибка compact:")} ${err.message || "unknown"}`, false);
        }
      };
    }
  }

  async function renderAutonomyRunbooksTab(body, serverId, sessionUid, dossier) {
    let runbooks;
    try {
      const res = await api(`/v1/admin/autonomy/runbooks?session_uid=${encodeURIComponent(sessionUid)}&server_id=${encodeURIComponent(serverId)}`);
      runbooks = Array.isArray(res.runbooks) ? res.runbooks : [];
    } catch (err) {
      body.innerHTML = `<div class="status-error">${escapeHtml(t("miniapp.error.generic_prefix", "Ошибка:"))} ${autonomyEscape(err.message || "")}</div>`;
      return;
    }
    if (!runbooks.length) {
      body.innerHTML = `<div class="status-empty">${escapeHtml(t("miniapp.autonomy.no_runbooks_server", "Нет runbooks для этого сервера."))}</div>`;
      return;
    }
    body.innerHTML = `
      <ul class="autonomy-rb-list-big">
        ${runbooks.map((rb) => `
          <li>
            <div class="autonomy-rb-row">
              <strong>${autonomyEscape(rb.title || rb.id)}</strong>
              <small>${autonomyEscape((rb.tags || []).join(", "))}</small>
              <button type="button" data-rb-id="${autonomyEscape(rb.id || "")}">${escapeHtml(t("miniapp.autonomy.open", "Открыть"))}</button>
            </div>
          </li>
        `).join("")}
      </ul>
      <div id="autonomyRbViewer" class="autonomy-rb-viewer"></div>
    `;
    body.querySelectorAll("[data-rb-id]").forEach((btn) => {
      btn.onclick = async () => {
        const rbId = btn.dataset.rbId;
        const viewer = body.querySelector("#autonomyRbViewer");
        if (!viewer) return;
        await renderAutonomyRunbookDetail(viewer, rbId, serverId, sessionUid);
      };
    });
  }

  async function renderAutonomyRunbookDetail(viewer, rbId, serverId, sessionUid) {
    viewer.innerHTML = escapeHtml(t("miniapp.status.loading", "Загрузка…"));
    let rb = {};
    try {
      const res = await api(`/v1/admin/autonomy/runbooks/${encodeURIComponent(rbId)}?session_uid=${encodeURIComponent(sessionUid)}&server_id=${encodeURIComponent(serverId)}`);
      rb = res.runbook || {};
    } catch (err) {
      viewer.innerHTML = `<div class="status-error">${escapeHtml(t("miniapp.error.generic_prefix", "Ошибка:"))} ${autonomyEscape(err.message || "")}</div>`;
      return;
    }
    const meta = rb.metadata || {};
    const steps = Array.isArray(meta.steps) ? meta.steps : [];
    const autoAction = meta.auto_action || {};
    const confidence = typeof autoAction.confidence === "number" ? autoAction.confidence : null;
    const stepsHtml = steps.length
      ? '<table class="autonomy-rb-steps"><thead><tr><th>step</th><th>script</th><th>target</th><th>checksum</th></tr></thead><tbody>' +
        steps.map((s) => `<tr><td>${autonomyEscape(s.name || "")}</td><td>${autonomyEscape(s.script || "")}</td><td>${autonomyEscape(s.target_hint || "")}</td><td><code>${autonomyEscape(String(s.checksum || "").slice(0, 16))}</code></td></tr>`).join("") +
        '</tbody></table>'
      : '';
    viewer.innerHTML = `
      <h4>${autonomyEscape(rb.title || rb.id)}</h4>
      <div><small>tags: ${autonomyEscape((rb.tags || []).join(", "))} • scope: ${autonomyEscape(rb.scope || "")} • servers: ${autonomyEscape((rb.servers || []).join(", "))}${confidence !== null ? ` • confidence: ${confidence}` : ""}</small></div>
      ${stepsHtml}
      <div class="autonomy-rb-actions">
        <button type="button" data-rb-act="validate">${escapeHtml(t("miniapp.autonomy.validate", "Проверить"))}</button>
        <button type="button" data-rb-act="promote">${escapeHtml(t("miniapp.autonomy.promote_ellipsis", "Применить…"))}</button>
        ${steps.length ? `<button type="button" data-rb-act="run-dry">${escapeHtml(t("miniapp.autonomy.run_first_step_dry", "Первый шаг (dry-run)"))}</button>` : ""}
      </div>
      <div id="autonomyRbReport" class="autonomy-rb-report"></div>
      <pre class="autonomy-rb-body">${autonomyEscape(rb.body || "")}</pre>
    `;
    const report = viewer.querySelector("#autonomyRbReport");
    viewer.querySelectorAll("[data-rb-act]").forEach((b) => {
      const act = b.dataset.rbAct;
      if (act === "validate") {
        b.onclick = () => autonomyRunWithLock(b, async () => {
          report.innerHTML = escapeHtml(t("miniapp.autonomy.validating", "Проверка…"));
          try {
            const res = await api(`/v1/admin/autonomy/runbooks/${encodeURIComponent(rbId)}/validate`, {
              method: "POST",
              body: JSON.stringify({ session_uid: sessionUid }),
            });
            report.innerHTML = renderAutonomyValidationReport(res.report || {});
          } catch (err) {
            report.innerHTML = autonomyFormatError(err);
          }
        });
      } else if (act === "promote") {
        b.onclick = () => renderAutonomyPromoteForm(report, rbId, sessionUid);
      } else if (act === "run-dry") {
        b.onclick = () => autonomyRunWithLock(b, async () => {
          const firstStep = steps[0] || {};
          report.innerHTML = escapeHtml(t("miniapp.autonomy.running_dry_run", "Dry-run…"));
          try {
            const res = await api(`/v1/admin/autonomy/runbooks/${encodeURIComponent(rbId)}/run-step`, {
              method: "POST",
              body: JSON.stringify({
                session_uid: sessionUid,
                step_name: firstStep.name || "",
                server_id: serverId,
                dry_run: true,
              }),
            });
            report.innerHTML = renderAutonomyRunResult(res.result || {});
          } catch (err) {
            report.innerHTML = autonomyFormatError(err);
          }
        });
      }
    });
  }

  function renderAutonomyValidationReport(report) {
    const checks = Array.isArray(report.checks) ? report.checks : [];
    const errors = Array.isArray(report.errors) ? report.errors : [];
    const warnings = Array.isArray(report.warnings) ? report.warnings : [];
    const status = report.ok ? '<span class="status-ok">OK</span>' : '<span class="status-error">FAIL</span>';
    return `
      <div class="autonomy-rb-validation">
        <div>${escapeHtml(t("miniapp.autonomy.validation_label", "Проверка:"))} ${status}</div>
        ${checks.length
          ? '<ul class="autonomy-rb-checks">' + checks.map((c) => `<li><strong>${autonomyEscape(c.step || "")}</strong> · checksum=${autonomyEscape(c.checksum || "?")} · bash -n=${autonomyEscape(c.bash_n || "?")} · shellcheck=${autonomyEscape(c.shellcheck || "?")}</li>`).join("") + "</ul>"
          : ""}
        ${errors.length ? `<div class="status-error">${escapeHtml(t("miniapp.autonomy.errors_label", "Ошибки:"))}<ul>` + errors.map((e) => `<li>${autonomyEscape(e)}</li>`).join("") + "</ul></div>" : ""}
        ${warnings.length ? `<div class="status-warn">${escapeHtml(t("miniapp.autonomy.warnings_label", "Предупреждения:"))}<ul>` + warnings.map((w) => `<li>${autonomyEscape(w)}</li>`).join("") + "</ul></div>" : ""}
      </div>
    `;
  }

  function renderAutonomyRunResult(result) {
    const header = `rb=${autonomyEscape(result.rb_id || "")} step=${autonomyEscape(result.step || "")} target=${autonomyEscape(result.target || "")} ${result.dry_run ? '(DRY-RUN)' : `rc=${result.exit_code}`}`;
    const body = result.error
      ? `<div class="status-error">${autonomyEscape(result.error)}</div>`
      : (result.stdout || result.stderr)
        ? `<pre>${autonomyEscape((result.stdout || "") + (result.stderr ? "\n[stderr]\n" + result.stderr : ""))}</pre>`
        : "";
    const cssClass = result.success === false ? "status-error" : "status-ok";
    return `<div class="autonomy-rb-result ${cssClass}"><div>${header}</div>${body}</div>`;
  }

  function renderAutonomyPromoteForm(container, rbId, sessionUid) {
    container.innerHTML = `
      <div class="autonomy-rb-promote">
        <h5>${escapeHtml(t("miniapp.autonomy.promote_runbook_title", "Применить сценарий"))}</h5>
        <label>${escapeHtml(t("miniapp.autonomy.promote_add_servers", "Добавить серверы (через запятую):"))}
          <input type="text" id="autonomyPromoteServers" placeholder="prod-01, prod-02" />
        </label>
        <label>${escapeHtml(t("miniapp.autonomy.promote_confidence", "Уверенность (0.0–1.0, опц.):"))}
          <input type="number" id="autonomyPromoteConfidence" min="0" max="1" step="0.1" />
        </label>
        <label><input type="checkbox" id="autonomyPromoteValidate" checked /> ${escapeHtml(t("miniapp.autonomy.run_validation_first", "сначала запустите проверку"))}</label>
        <button type="button" id="autonomyPromoteSubmit">${escapeHtml(t("miniapp.autonomy.promote", "Применить"))}</button>
        <div id="autonomyPromoteResult"></div>
      </div>
    `;
    const submit = container.querySelector("#autonomyPromoteSubmit");
    const result = container.querySelector("#autonomyPromoteResult");
    submit.onclick = () => autonomyRunWithLock(submit, async () => {
      const serversStr = (container.querySelector("#autonomyPromoteServers").value || "").trim();
      const confidenceStr = (container.querySelector("#autonomyPromoteConfidence").value || "").trim();
      const validate = container.querySelector("#autonomyPromoteValidate").checked;
      const add_servers = serversStr.split(",").map((s) => s.trim()).filter(Boolean);
      if (!add_servers.length) {
        result.innerHTML = `<div class="status-error">${escapeHtml(t("miniapp.autonomy.err_no_server_id", "Укажите хотя бы один server_id"))}</div>`;
        return;
      }
      const payload = { session_uid: sessionUid, add_servers, run_validation: validate };
      if (confidenceStr) {
        const conf = Number(confidenceStr);
        if (!Number.isFinite(conf) || conf < 0 || conf > 1) {
          result.innerHTML = `<div class="status-error">${escapeHtml(t("miniapp.autonomy.err_confidence_range", "Confidence должен быть числом в [0.0, 1.0]"))}</div>`;
          return;
        }
        payload.confidence = conf;
      }
      result.innerHTML = escapeHtml(t("miniapp.autonomy.promoting", "Применение…"));
      try {
        const res = await api(`/v1/admin/autonomy/runbooks/${encodeURIComponent(rbId)}/promote`, {
          method: "POST",
          body: JSON.stringify(payload),
        });
        const r = res.result || {};
        result.innerHTML = `<div class="status-ok">Added: ${autonomyEscape((r.added_servers || []).join(", ") || "—")}; already: ${autonomyEscape((r.already_present || []).join(", ") || "—")}${r.confidence_after !== null && r.confidence_after !== undefined ? `; confidence ${r.confidence_before} → ${r.confidence_after}` : ""}</div>`;
      } catch (err) {
        result.innerHTML = autonomyFormatError(err);
      }
    });
  }

  async function renderAutonomyBuilderTab(body, serverId, sessionUid) {
    body.innerHTML = `
      <div class="autonomy-builder">
        <div class="autonomy-subcard">
          <h4>${escapeHtml(t("miniapp.autonomy.builder_sources_title", "1. Источник скриптов (admin.runbook_sources)"))}</h4>
          <div class="autonomy-builder-row">
            <input type="text" id="autonomyBuilderDir" placeholder="/path/to/scripts/dir" />
            <button type="button" id="autonomyBuilderScan">Scan</button>
          </div>
          <div id="autonomyBuilderFiles" class="autonomy-builder-files"></div>
        </div>
        <div class="autonomy-subcard">
          <h4>${escapeHtml(t("miniapp.autonomy.builder_params_title", "2. Параметры runbook"))}</h4>
          <label>Title: <input type="text" id="autonomyBuilderTitle" /></label>
          <label>rb_id (${escapeHtml(t("miniapp.label.optional", "опционально"))}): <input type="text" id="autonomyBuilderRbId" /></label>
          <label>Tags (comma): <input type="text" id="autonomyBuilderTags" /></label>
          <label>Dev server_id: <input type="text" id="autonomyBuilderDev" value="${autonomyEscape(serverId || "")}" /></label>
          <label>Target hint:
            <select id="autonomyBuilderTarget">
              <option value="local" selected>local</option>
              <option value="ssh">ssh</option>
            </select>
          </label>
          <label>Description:
            <textarea id="autonomyBuilderDesc" rows="2"></textarea>
          </label>
        </div>
        <div class="autonomy-subcard">
          <h4>${escapeHtml(t("miniapp.autonomy.builder_build_title", "3. Собрать и проверить"))}</h4>
          <div class="admin-state-actions">
            <button type="button" id="autonomyBuilderBuild">Build runbook</button>
            <button type="button" id="autonomyBuilderValidate" disabled>${escapeHtml(t("miniapp.autonomy.validate", "Проверить"))}</button>
          </div>
          <div id="autonomyBuilderResult"></div>
        </div>
      </div>
    `;
    const filesBox = body.querySelector("#autonomyBuilderFiles");
    const resultBox = body.querySelector("#autonomyBuilderResult");
    const validateBtn = body.querySelector("#autonomyBuilderValidate");
    let scannedFiles = [];
    let builtRbId = null;

    const scanBtn = body.querySelector("#autonomyBuilderScan");
    scanBtn.onclick = () => autonomyRunWithLock(scanBtn, async () => {
      const dir = (body.querySelector("#autonomyBuilderDir").value || "").trim();
      if (!dir) {
        filesBox.innerHTML = `<div class="status-error">${escapeHtml(t("miniapp.autonomy.err_enter_dir", "Введите каталог."))}</div>`;
        return;
      }
      filesBox.innerHTML = escapeHtml(t("miniapp.autonomy.scanning", "Сканируем…"));
      try {
        const res = await api(`/v1/admin/autonomy/scripts/scan`, {
          method: "POST",
          body: JSON.stringify({ session_uid: sessionUid, directory: dir }),
        });
        scannedFiles = Array.isArray(res.files) ? res.files : [];
      } catch (err) {
        filesBox.innerHTML = autonomyFormatError(err);
        return;
      }
      if (!scannedFiles.length) {
        filesBox.innerHTML = `<div class="status-empty">${escapeHtml(t("miniapp.autonomy.no_scripts", "Скрипты не найдены."))}</div>`;
        return;
      }
      filesBox.innerHTML = '<ul class="autonomy-builder-file-list">' + scannedFiles.map((f, i) => `
        <li>
          <label>
            <input type="checkbox" data-script-idx="${i}" checked />
            <strong>${autonomyEscape(f.name)}</strong>
            <small>${f.size_bytes} ${escapeHtml(t("miniapp.autonomy.bytes", "байт"))} · sha1:${autonomyEscape(String(f.sha1).slice(0, 8))}…</small>
          </label>
        </li>
      `).join("") + "</ul>";
    });

    const buildBtn = body.querySelector("#autonomyBuilderBuild");
    buildBtn.onclick = () => autonomyRunWithLock(buildBtn, async () => {
      const title = (body.querySelector("#autonomyBuilderTitle").value || "").trim();
      const rbIdInput = (body.querySelector("#autonomyBuilderRbId").value || "").trim();
      const tagsStr = (body.querySelector("#autonomyBuilderTags").value || "").trim();
      const devSid = (body.querySelector("#autonomyBuilderDev").value || "").trim();
      const target = body.querySelector("#autonomyBuilderTarget").value;
      const description = (body.querySelector("#autonomyBuilderDesc").value || "").trim();
      const tags = tagsStr.split(",").map((s) => s.trim()).filter(Boolean);
      const picked = [];
      filesBox.querySelectorAll("[data-script-idx]:checked").forEach((cb) => {
        const f = scannedFiles[Number(cb.dataset.scriptIdx)];
        if (f) picked.push(f);
      });
      if (!title) { resultBox.innerHTML = `<div class="status-error">${escapeHtml(t("miniapp.autonomy.err_title_required", "Title обязателен."))}</div>`; return; }
      if (!devSid) { resultBox.innerHTML = `<div class="status-error">${escapeHtml(t("miniapp.autonomy.err_dev_server_required", "Dev server_id обязателен."))}</div>`; return; }
      if (!picked.length) { resultBox.innerHTML = `<div class="status-error">${escapeHtml(t("miniapp.autonomy.err_select_script", "Выберите хотя бы один скрипт."))}</div>`; return; }
      const scripts = picked.map((f) => ({
        source_path: f.path,
        name: f.name,
        target_hint: target,
      }));
      resultBox.innerHTML = escapeHtml(t("miniapp.autonomy.building_runbook", "Собираем runbook…"));
      try {
        const res = await api(`/v1/admin/autonomy/runbooks/build`, {
          method: "POST",
          body: JSON.stringify({
            session_uid: sessionUid,
            title,
            dev_server_id: devSid,
            rb_id: rbIdInput || undefined,
            tags,
            description,
            scripts,
          }),
        });
        const rb = res.runbook || {};
        builtRbId = rb.id;
        validateBtn.disabled = !builtRbId;
        resultBox.innerHTML = `<div class="status-ok">${escapeHtml(t("miniapp.autonomy.runbook_built_prefix", "Собран runbook"))} <strong>${autonomyEscape(rb.id)}</strong> (${scripts.length} steps). ${escapeHtml(t("miniapp.autonomy.runbook_built_suffix", "Нажмите Validate, чтобы проверить."))}</div>`;
      } catch (err) {
        resultBox.innerHTML = autonomyFormatError(err);
      }
    });

    validateBtn.onclick = () => autonomyRunWithLock(validateBtn, async () => {
      if (!builtRbId) return;
      resultBox.innerHTML = escapeHtml(t("miniapp.autonomy.validating", "Проверка…"));
      try {
        const res = await api(`/v1/admin/autonomy/runbooks/${encodeURIComponent(builtRbId)}/validate`, {
          method: "POST",
          body: JSON.stringify({ session_uid: sessionUid }),
        });
        resultBox.innerHTML = renderAutonomyValidationReport(res.report || {});
      } catch (err) {
        resultBox.innerHTML = autonomyFormatError(err);
      }
    });
  }

  async function renderAutonomyActionsTab(body, serverId, sessionUid, dossier) {
    const drifts = (dossier && dossier.recent_drifts) || [];
    const recent = drifts.slice(0, 10);
    body.innerHTML = `
      <div class="autonomy-subcard">
        <h4>${escapeHtml(t("miniapp.autonomy.manual_actions", "Ручные действия"))}</h4>
        <div class="admin-state-actions">
          <button type="button" id="autonomyActionRescan">Rescan now</button>
          <button type="button" id="autonomyActionCompactMem">Compact memory</button>
        </div>
      </div>
      <div class="autonomy-subcard">
        <h4>Recent drifts / actions</h4>
        ${recent.length
          ? '<ul class="autonomy-drift-list">' + recent.map((d) => `<li><span class="autonomy-drift-${autonomyEscape(d.severity || "")}">${autonomyEscape(d.severity || "")}</span> ${autonomyEscape(d.check_id || "")} <small>${autonomyEscape(d.ts || "")}</small></li>`).join("") + "</ul>"
          : `<div class="status-empty">${escapeHtml(t("miniapp.autonomy.history_empty", "История пуста."))}</div>`}
      </div>
    `;
    const rescanBtn = body.querySelector("#autonomyActionRescan");
    if (rescanBtn) {
      rescanBtn.onclick = () => { void autonomyRescanServer(serverId); };
    }
    const compactBtn = body.querySelector("#autonomyActionCompactMem");
    if (compactBtn) {
      compactBtn.onclick = async () => {
        try {
          await api(`/v1/admin/autonomy/servers/${encodeURIComponent(serverId)}/memory/compact`, {
            method: "POST",
            body: JSON.stringify({ session_uid: sessionUid, force: false }),
          });
          setAutonomyStatus("Memory compacted.");
        } catch (err) {
          setAutonomyStatus(`${t("miniapp.autonomy.err_compact", "Ошибка compact:")} ${err.message || "unknown"}`, false);
        }
      };
    }
  }

  async function renderAutonomySnapshotsTab(body, serverId, sessionUid) {
    body.innerHTML = `
      <div class="admin-state-actions">
        <label>check_id:
          <select id="autonomySnapCheck"><option value="">—</option></select>
        </label>
        <button type="button" id="autonomySnapReload">${escapeHtml(t("miniapp.autonomy.refresh_list", "Обновить список"))}</button>
      </div>
      <div id="autonomySnapBody" class="status-empty">${escapeHtml(t("miniapp.autonomy.select_check_id", "Выберите check_id для просмотра истории."))}</div>
    `;
    const sel = body.querySelector("#autonomySnapCheck");
    const reloadList = async () => {
      try {
        const res = await api(`/v1/admin/autonomy/servers/${encodeURIComponent(serverId)}/snapshot-checks?session_uid=${encodeURIComponent(sessionUid)}`);
        const checks = Array.isArray(res.checks) ? res.checks : [];
        const prev = String(sel.value || "");
        sel.innerHTML = '<option value="">—</option>' + checks.map((c) => `<option value="${autonomyEscape(c)}">${autonomyEscape(c)}</option>`).join("");
        if (prev && checks.includes(prev)) sel.value = prev;
      } catch (err) {
        body.querySelector("#autonomySnapBody").innerHTML = `<div class="status-error">${escapeHtml(t("miniapp.error.generic_prefix", "Ошибка:"))} ${autonomyEscape(err.message || "")}</div>`;
      }
    };
    const loadData = async () => {
      const checkId = String(sel.value || "");
      const out = body.querySelector("#autonomySnapBody");
      if (!checkId) { out.innerHTML = `<div class="status-empty">${escapeHtml(t("miniapp.autonomy.select_check_id_short", "Выберите check_id."))}</div>`; return; }
      out.innerHTML = escapeHtml(t("miniapp.status.loading", "Загрузка…"));
      try {
        const qs = new URLSearchParams({
          session_uid: sessionUid,
          check_id: checkId,
          limit: "200",
        });
        const res = await api(`/v1/admin/autonomy/servers/${encodeURIComponent(serverId)}/snapshots?${qs.toString()}`);
        const rows = Array.isArray(res.snapshots) ? res.snapshots : [];
        if (!rows.length) { out.innerHTML = `<div class="status-empty">${escapeHtml(t("miniapp.autonomy.no_snapshots", "Нет snapshots."))}</div>`; return; }
        const trs = rows.map((r) => {
          const valueText = (typeof r.value === "object" && r.value !== null)
            ? autonomyYamlDump(r.value, 0)
            : String(r.value == null ? "" : r.value);
          const valueTrimmed = valueText.length > 240 ? valueText.slice(0, 240) + "…" : valueText;
          const hashStr = String(r.hash || r.value_hash || "").slice(0, 16);
          return `<tr><td>${autonomyEscape(r.ts || r.timestamp || "")}</td><td><pre class="autonomy-snap-value">${autonomyEscape(valueTrimmed)}</pre></td><td><code>${autonomyEscape(hashStr)}</code></td></tr>`;
        }).join("");
        out.innerHTML = `<table class="autonomy-snapshots-table"><thead><tr><th>ts</th><th>value</th><th>hash</th></tr></thead><tbody>${trs}</tbody></table>`;
      } catch (err) {
        out.innerHTML = `<div class="status-error">${escapeHtml(t("miniapp.error.generic_prefix", "Ошибка:"))} ${autonomyEscape(err.message || "")}</div>`;
      }
    };
    body.querySelector("#autonomySnapReload").onclick = async () => { await reloadList(); };
    sel.onchange = loadData;
    await reloadList();
  }

  async function renderAutonomyPrereqsTab(body, serverId, sessionUid) {
    body.innerHTML = `
      <div class="admin-state-actions">
        <button type="button" id="autonomyPrereqsCheck">Check prereqs</button>
        <button type="button" id="autonomyPrereqsBootstrap" disabled>Build bootstrap runbook</button>
      </div>
      <div id="autonomyPrereqsBody" class="status-empty">${escapeHtml(t("miniapp.autonomy.prereqs_hint", "Нажмите «Check prereqs» для запуска проверки."))}</div>
    `;
    const bodyOut = body.querySelector("#autonomyPrereqsBody");
    const bootstrapBtn = body.querySelector("#autonomyPrereqsBootstrap");
    let lastReport = null;
    body.querySelector("#autonomyPrereqsCheck").onclick = async () => {
      bodyOut.innerHTML = escapeHtml(t("miniapp.autonomy.requesting", "Запрос…"));
      bootstrapBtn.disabled = true;
      try {
        const res = await api(`/v1/admin/autonomy/servers/${encodeURIComponent(serverId)}/prereqs?session_uid=${encodeURIComponent(sessionUid)}`);
        const report = res.report || {};
        lastReport = report;
        bodyOut.innerHTML = `<pre class="autonomy-prereqs-yaml">${autonomyEscape(autonomyYamlDump(report, 0))}</pre>`;
        const missing = [].concat(report.required_missing || [], report.recommended_missing || []);
        bootstrapBtn.disabled = !(missing.length > 0 && report.installable);
      } catch (err) {
        bodyOut.innerHTML = `<div class="status-error">${escapeHtml(t("miniapp.error.generic_prefix", "Ошибка:"))} ${autonomyEscape(err.message || "")}</div>`;
      }
    };
    bootstrapBtn.onclick = async () => {
      if (!lastReport) return;
      bodyOut.innerHTML += `\n${escapeHtml(t("miniapp.autonomy.building_bootstrap", "Сборка bootstrap-runbook…"))}`;
      try {
        const res = await api(`/v1/admin/autonomy/servers/${encodeURIComponent(serverId)}/prereqs/bootstrap`, {
          method: "POST",
          body: JSON.stringify({ session_uid: sessionUid }),
        });
        const reason = String(res.reason || "");
        if (!res.runbook) {
          bodyOut.insertAdjacentHTML("beforeend", `<div class="status-empty">${escapeHtml(t("miniapp.autonomy.runbook_not_created", "Runbook не создан. Причина:"))} ${autonomyEscape(reason || "unknown")}</div>`);
          return;
        }
        const rb = res.runbook;
        bodyOut.insertAdjacentHTML("beforeend", `<div class="status-ok">${escapeHtml(t("miniapp.autonomy.runbook_created_prefix", "Создан runbook"))} <strong>${autonomyEscape(rb.id || "")}</strong>. ${escapeHtml(t("miniapp.autonomy.path_label", "Путь:"))} ${autonomyEscape(rb.path || "")}</div>`);
      } catch (err) {
        bodyOut.insertAdjacentHTML("beforeend", `<div class="status-error">${escapeHtml(t("miniapp.error.generic_prefix", "Ошибка:"))} ${autonomyEscape(err.message || "")}</div>`);
      }
    };
  }

  function renderAutonomyBaselineDiff(baseText, proposedText) {
    const baseLines = String(baseText || "").split("\n");
    const propLines = String(proposedText || "").split("\n");
    const baseSet = new Set(baseLines);
    const propSet = new Set(propLines);
    const out = [];
    const seen = new Set();
    for (const line of baseLines) {
      if (propSet.has(line)) {
        if (!seen.has("base:" + line)) { out.push(`  ${autonomyEscape(line)}`); seen.add("base:" + line); }
      } else {
        out.push(`<span class="autonomy-diff-del">- ${autonomyEscape(line)}</span>`);
      }
    }
    for (const line of propLines) {
      if (!baseSet.has(line)) {
        out.push(`<span class="autonomy-diff-add">+ ${autonomyEscape(line)}</span>`);
      }
    }
    return out.join("\n") || t("miniapp.autonomy.no_diff", "(нет различий)");
  }

  async function autonomyRescanServer(serverId) {
    const sessionUid = autonomyCurrentSessionUid();
    if (!sessionUid || !serverId) return;
    setAutonomyStatus(`Rescan ${serverId}…`);
    try {
      await api(`/v1/admin/autonomy/servers/${encodeURIComponent(serverId)}/rescan?session_uid=${encodeURIComponent(sessionUid)}`, { method: "POST" });
      setAutonomyStatus(`Rescan ${serverId} done.`);
      await refreshAutonomy();
      await showAutonomyServerDetail(serverId);
    } catch (err) {
      setAutonomyStatus(`${t("miniapp.autonomy.err_rescan", "Ошибка rescan:")} ${err.message || "unknown"}`, false);
    }
  }

  function renderTasks(tasks) {
    const emptyEl = document.getElementById("tasksEmpty");
    const panel = document.getElementById("tasksPanel");
    const list = document.getElementById("tasksList");
    if (!emptyEl || !panel || !list) return;
    if (!tasks || tasks.length === 0) {
      emptyEl.textContent = t("miniapp.tasks.empty", "Нет активных задач.");
      emptyEl.classList.remove("hidden");
      panel.classList.add("hidden");
      return;
    }
    emptyEl.classList.add("hidden");
    panel.classList.remove("hidden");
    list.innerHTML = "";
    tasks.forEach((task) => {
      const li = document.createElement("li");
      li.style.cssText = "display:flex;align-items:center;justify-content:space-between;padding:6px 4px;border-bottom:1px solid var(--border)";
      const info = document.createElement("span");
      info.textContent = `[${task.session_uid || ""}] ${task.mode_id || ""}: ${task.name || ""}`;
      info.style.flex = "1";
      li.appendChild(info);
      const btn = document.createElement("button");
      btn.textContent = t("miniapp.tasks.btn_cancel", "Отменить");
      btn.style.marginLeft = "8px";
      btn.onclick = async () => {
        btn.disabled = true;
        try {
          await api(`/tasks/${encodeURIComponent(task.session_uid)}/cancel`, { method: "POST" });
          void fetchTasks();
        } catch (err) {
          btn.disabled = false;
          alert(`${t("miniapp.tasks.err_cancel", "Ошибка отмены:")} ${err.message || "unknown"}`);
        }
      };
      li.appendChild(btn);
      list.appendChild(li);
    });
  }

  async function fetchTasks() {
    try {
      const data = await api("/tasks");
      renderTasks(data.tasks || []);
    } catch (err) {
      const emptyEl = document.getElementById("tasksEmpty");
      if (emptyEl) {
        emptyEl.textContent = `${t("miniapp.tasks.err_load", "Ошибка загрузки задач:")} ${err.message || "unknown"}`;
        emptyEl.classList.remove("hidden");
      }
      const panel = document.getElementById("tasksPanel");
      if (panel) panel.classList.add("hidden");
    }
  }

  // ==================================================================
  // Reports tab
  // ==================================================================

  function renderReportsSessionOptions(payload) {
    const select = document.getElementById("reportsSession");
    if (!select) return;
    const sessions = Array.isArray(payload?.available_sessions) ? payload.available_sessions : [];
    const defaultLabel = t("miniapp.label.choose_session", "Выберите сессию");
    const signatureParts = sessions.map((item) => {
      const uid = String(item?.session_uid || "");
      const label = String(item?.label || uid);
      return `${uid} ${label}`;
    });
    const nextSignature = `${defaultLabel}${signatureParts.join("")}`;
    const fallbackSelected = String(payload?.selected_session_uid || "");
    const desiredSelected = state.reportsSessionUid || fallbackSelected;
    if (!select.options.length || state.reportsSessionsSignature !== nextSignature) {
      const options = sessions
        .map((item) => `<option value="${escapeHtml(item.session_uid)}">${escapeHtml(item.label || item.session_uid)}</option>`)
        .join("");
      select.innerHTML = `<option value="">${escapeHtml(defaultLabel)}</option>${options}`;
      if (desiredSelected) {
        select.value = desiredSelected;
      }
      if (select.value !== desiredSelected && fallbackSelected) {
        select.value = fallbackSelected;
      }
      state.reportsSessionsSignature = nextSignature;
    }
    state.reportsSessionUid = String(select.value || "");
  }

  function _reportsSetEmpty(msg) {
    const empty = document.getElementById("reportsEmpty");
    const panel = document.getElementById("reportsPanel");
    if (empty) {
      empty.textContent = msg;
      empty.classList.remove("hidden");
    }
    if (panel) panel.classList.add("hidden");
  }

  function _reportsShowPanel() {
    const empty = document.getElementById("reportsEmpty");
    const panel = document.getElementById("reportsPanel");
    if (empty) empty.classList.add("hidden");
    if (panel) panel.classList.remove("hidden");
  }

  function renderReportsList(reports) {
    const list = document.getElementById("reportsList");
    if (!list) return;
    list.innerHTML = "";
    if (!reports || reports.length === 0) {
      _reportsSetEmpty(t("miniapp.reports.no_reports", "Отчётов нет."));
      return;
    }
    _reportsShowPanel();
    reports.forEach((rep) => {
      const li = document.createElement("li");
      li.style.cssText = "padding:6px 8px;border-bottom:1px solid var(--border);cursor:pointer;";
      const name = document.createElement("div");
      name.textContent = rep.name || rep.id;
      name.style.fontWeight = "bold";
      name.style.fontSize = "0.9em";
      const meta = document.createElement("div");
      meta.textContent = rep.date || "";
      meta.style.fontSize = "0.78em";
      meta.style.color = "var(--muted, #888)";
      li.appendChild(name);
      li.appendChild(meta);
      li.onclick = () => {
        list.querySelectorAll("li").forEach((el) => el.classList.remove("active"));
        li.classList.add("active");
        void loadReportContent(rep.id, rep.session_uid);
        state.reportsSelectedId = rep.id;
        state.reportsSelectedSessionUid = rep.session_uid;
        const dlBtn = document.getElementById("reportsDownloadMd");
        if (dlBtn) dlBtn.disabled = false;
      };
      list.appendChild(li);
    });
  }

  async function loadReportContent(reportId, sessionUid) {
    const viewerEmpty = document.getElementById("reportsViewerEmpty");
    const viewerContent = document.getElementById("reportsViewerContent");
    if (!viewerEmpty || !viewerContent) return;
    viewerEmpty.classList.remove("hidden");
    viewerContent.classList.add("hidden");
    viewerEmpty.textContent = t("miniapp.reports.loading", "Загрузка...");
    try {
      const data = await api(
        `/reports/${encodeURIComponent(reportId)}?session_uid=${encodeURIComponent(sessionUid)}`
      );
      viewerEmpty.classList.add("hidden");
      viewerContent.classList.remove("hidden");
      viewerContent.textContent = data.content || "";
    } catch (err) {
      viewerEmpty.textContent = `${t("miniapp.reports.err_load", "Ошибка загрузки:")} ${err.message || "unknown"}`;
    }
  }

  async function fetchReports() {
    const sessionUid = state.reportsSessionUid || "";
    if (!sessionUid) {
      _reportsSetEmpty(t("miniapp.reports.empty", "Выберите сессию для просмотра отчётов."));
      return;
    }
    try {
      const data = await api(`/reports?session_uid=${encodeURIComponent(sessionUid)}`);
      renderReportsList(data.reports || []);
    } catch (err) {
      _reportsSetEmpty(
        `${t("miniapp.reports.err_load", "Ошибка загрузки:")} ${err.message || "unknown"}`
      );
    }
  }

  async function downloadReportMd() {
    const reportId = state.reportsSelectedId;
    const sessionUid = state.reportsSelectedSessionUid;
    if (!reportId || !sessionUid) return;
    try {
      const url = `/api/reports/${encodeURIComponent(reportId)}/download?session_uid=${encodeURIComponent(sessionUid)}&format=md`;
      const a = document.createElement("a");
      a.href = url;
      a.download = reportId;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    } catch (err) {
      alert(`${t("miniapp.reports.err_download", "Ошибка скачивания:")} ${err.message || "unknown"}`);
    }
  }

  async function boot() {
    await initLanguage();
    ensureAdminPanelLayout();
    installButtonPressFeedback();
    bindButtons();
    try {
      state.me = await api("/auth/me");
      await syncServerLanguage();
      void connectStatusWs();
      try {
        await loadLogsMeta();
        setLogsControlsEnabled(true);
        applyLogsStateFromControls();
        void connectLogsWs();
      } catch (err) {
        setLogsControlsEnabled(false);
        clearLogsView();
        setLogsStatus(`${t("miniapp.logs.load_error_prefix", "Ошибка загрузки логов:")} ${err.message || "unknown"}`, false);
      }

      if (state.me.is_admin) {
        state.schema = await api("/config/schema");
        await loadConfig();
      } else {
        hideAdminTabsForUser();
        switchTab("logs");
      }
    } catch (err) {
      if (err && (err.status === 401 || err.status === 403)) {
        blockUnauthorizedScreen();
        return;
      }
      setAuthStatus(t("miniapp.error.init", "Ошибка инициализации"), false);
      setStatus(`${t("miniapp.error.init", "Ошибка инициализации")}: ${err.message || "unknown"}`);
    }
  }

  boot();
})();
