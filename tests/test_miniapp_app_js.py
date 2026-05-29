import json
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest


def _run_app_js_harness(
    admin_payload: dict,
    *,
    admin_responses: list[dict] | None = None,
    admin_action_response: dict | None = None,
    settings_payload: dict | None = None,
    settings_responses: list[dict] | None = None,
    settings_session_value: str | None = None,
    status_snapshot: dict | None = None,
    status_snapshots: list[dict] | None = None,
    click_action: str | None = None,
    click_admin_subtab: str | None = None,
    ssh_form_values: dict | None = None,
    click_ssh_save: bool = False,
    files_session_value: str | None = None,
    click_files_apply: bool = False,
    click_first_file: bool = False,
    file_tree_items: list[dict] | None = None,
    file_read_payload: dict | None = None,
    wait_before_interval_ms: int = 900,
    run_interval: bool = True,
) -> dict:
    repo_root = Path(__file__).resolve().parent.parent
    script_template = textwrap.dedent(
        r"""
        const fs = require("fs");
        const vm = require("vm");

        const realSetTimeout = global.setTimeout;
        const fetchCalls = [];
        const fetchRequests = [];
        const openedLinks = [];
        const alerts = [];
        const intervalCalls = [];
        const adminPayload = __ADMIN_PAYLOAD__;
        const adminResponses = __ADMIN_RESPONSES__;
        const adminActionResponse = __ADMIN_ACTION_RESPONSE__;
        const settingsPayload = __SETTINGS_PAYLOAD__;
        const settingsResponses = __SETTINGS_RESPONSES__;
        const settingsSessionValue = __SETTINGS_SESSION_VALUE__;
        const statusSnapshot = __STATUS_SNAPSHOT__;
        const statusSnapshots = __STATUS_SNAPSHOTS__;
        const clickAction = __CLICK_ACTION__;
        const clickAdminSubtab = __CLICK_ADMIN_SUBTAB__;
        const sshFormValues = __SSH_FORM_VALUES__;
        const clickSshSave = __CLICK_SSH_SAVE__;
        const filesSessionValue = __FILES_SESSION_VALUE__;
        const clickFilesApply = __CLICK_FILES_APPLY__;
        const clickFirstFile = __CLICK_FIRST_FILE__;
        const fileTreeItems = __FILE_TREE_ITEMS__;
        const fileReadPayload = __FILE_READ_PAYLOAD__;
        const waitBeforeIntervalMs = __WAIT_BEFORE_INTERVAL_MS__;
        const runInterval = __RUN_INTERVAL__;
        let adminStatusIndex = 0;
        let settingsResponseIndex = 0;

        class ClassList {
          constructor() {
            this._set = new Set();
          }
          add(...names) {
            names.forEach((name) => this._set.add(String(name)));
          }
          remove(...names) {
            names.forEach((name) => this._set.delete(String(name)));
          }
          contains(name) {
            return this._set.has(String(name));
          }
          toggle(name, force) {
            const key = String(name);
            if (force === undefined) {
              if (this._set.has(key)) {
                this._set.delete(key);
                return false;
              }
              this._set.add(key);
              return true;
            }
            if (force) {
              this._set.add(key);
              return true;
            }
            this._set.delete(key);
            return false;
          }
        }

        class Element {
          constructor(tagName = "div", id = "") {
            this.tagName = String(tagName || "div").toUpperCase();
            this.id = String(id || "");
            this.dataset = {};
            this.style = {};
            this.value = "";
            this.checked = false;
            this.disabled = false;
            this.children = [];
            this.attributes = {};
            this.classList = new ClassList();
            this.parentElement = { style: {} };
            this.onclick = null;
            this._textContent = "";
            this._innerHTML = "";
            this.options = [];
          }
          set textContent(value) {
            this._textContent = String(value || "");
          }
          get textContent() {
            return this._textContent;
          }
          set innerHTML(value) {
            this._innerHTML = String(value || "");
            this.children = [];
            const optionCount = (this._innerHTML.match(/<option\b/g) || []).length;
            this.options = Array.from({ length: optionCount }, () => ({}));
          }
          get innerHTML() {
            return this._innerHTML;
          }
          appendChild(child) {
            if (child && typeof child === "object") {
              child.parentElement = this;
            }
            this.children.push(child);
            return child;
          }
          append(child) {
            this.appendChild(child);
          }
          remove() {}
          setAttribute(name, value) {
            this.attributes[String(name)] = String(value);
          }
          getAttribute(name) {
            return this.attributes[String(name)] || "";
          }
          querySelectorAll() {
            return [];
          }
          querySelector() {
            return null;
          }
          addEventListener() {}
          removeEventListener() {}
        }

        const elements = new Map();
        const selectIds = new Set([
          "logsType",
          "logsHistory",
          "logsSession",
          "filesSession",
          "settingsSession",
          "settingsRemoteControlHost",
          "statusSession",
          "adminSession",
          "schedulerProject",
          "schedulerSession",
          "schedulerTargetMode",
          "schedulerPayload",
          "sshHostAuth",
        ]);
        const checkboxIds = new Set(["logsAutoScroll", "ticksAutoScroll", "schedulerEnabled"]);

        function getElement(id) {
          const key = String(id || "");
          if (!elements.has(key)) {
            const tagName = selectIds.has(key) ? "select" : "div";
            const el = new Element(tagName, key);
            if (checkboxIds.has(key)) {
              el.checked = true;
            }
            if (key === "cfgSave") {
              el.disabled = true;
            }
            elements.set(key, el);
          }
          return elements.get(key);
        }

        const tabButtons = ["config", "files", "logs", "status", "scheduler", "admin"].map((tab) => {
          const el = new Element("button", `btn-${tab}`);
          el.dataset.tab = tab;
          if (tab === "config") {
            el.classList.add("active");
          }
          return el;
        });
        const tabPanes = ["config", "files", "editor", "logs", "status", "scheduler", "admin"].map((tab) => {
          const el = getElement(`tab-${tab}`);
          el.classList.add("tab");
          if (tab === "config") {
            el.classList.add("active");
          }
          return el;
        });
        const adminSubtabNames = ["overview", "monitoring", "operations", "config", "chat", "diagnostics"];
        const adminSubtabButtons = adminSubtabNames.map((tab) => {
          const el = new Element("button", `admin-subtab-${tab}`);
          el.dataset.adminSubtab = tab;
          if (tab === "overview") {
            el.classList.add("active");
          }
          return el;
        });
        const adminSubtabPanels = adminSubtabNames.map((tab) => {
          const id = `adminSubtab${tab.charAt(0).toUpperCase()}${tab.slice(1)}`;
          const el = getElement(id);
          el.dataset.adminSubtabPanel = tab;
          el.classList.add("admin-subtab-panel");
          if (tab === "overview") {
            el.classList.add("active");
          } else {
            el.classList.add("hidden");
          }
          return el;
        });

        const document = {
          body: new Element("body", "body"),
          getElementById(id) {
            return getElement(id);
          },
          querySelectorAll(selector) {
            if (selector === ".tabs button") {
              return tabButtons;
            }
            if (selector === ".tab") {
              return tabPanes;
            }
            if (selector === "[data-admin-subtab]") {
              return adminSubtabButtons;
            }
            if (selector === "[data-admin-subtab-panel]") {
              return adminSubtabPanels;
            }
            if (selector === "#filesTree li") {
              return getElement("filesTree").children;
            }
            return [];
          },
          querySelector(selector) {
            const match = String(selector || "").match(/^\.tabs button\[data-tab="([^"]+)"\]$/);
            if (match) {
              return tabButtons.find((item) => item.dataset.tab === match[1]) || null;
            }
            return null;
          },
          createElement(tagName) {
            return new Element(tagName);
          },
          createTextNode(text) {
            return { nodeType: 3, textContent: String(text || "") };
          },
        };

        const editor = {
          setTheme() {},
          setShowPrintMargin() {},
          setValue() {},
          getValue() { return ""; },
          resize() {},
          session: {
            setMode() {},
          },
        };

        function jsonResponse(body) {
          return {
            ok: true,
            status: 200,
            text: async () => JSON.stringify(body),
          };
        }

        async function fetchStub(url, options = {}) {
          const path = String(url || "");
          fetchCalls.push(path);
          fetchRequests.push({
            path,
            body: options && Object.prototype.hasOwnProperty.call(options, "body") ? options.body : null,
          });
          if (path.endsWith("./api/auth/me")) {
            return jsonResponse({ user_id: 1, is_admin: true, username: "admin" });
          }
          if (path.endsWith("./api/status/ws_ticket")) {
            return jsonResponse({ ticket: "status-ticket" });
          }
          if (path.endsWith("./api/logs/meta")) {
            return jsonResponse({ log_types: ["main"], history_options: [0], sessions: [] });
          }
          if (path.endsWith("./api/logs/ws_ticket")) {
            return jsonResponse({ ticket: "logs-ticket" });
          }
          if (path.endsWith("./api/files/ws_ticket")) {
            return jsonResponse({ ticket: "file-ticket" });
          }
          if (path.endsWith("./api/config/schema")) {
            return jsonResponse({ sections: [] });
          }
          if (path.endsWith("./api/config/view")) {
            return jsonResponse({
              revision: "r1",
              config: {
                telegram: { token: "t", whitelist_chat_ids: [], admlist_chat_ids: [1] },
                defaults: { workdir: "/tmp" },
                tools: {},
                mcp: {},
                mcp_clients: [],
                presets: [],
                miniapp: {
                  enabled: true,
                  base_path: "/cli-proxy",
                  max_edit_file_size_kb: 512,
                  enable_delete: true,
                },
              },
            });
          }
          if (path.includes("./api/session/") && path.endsWith("/settings")) {
            const current = settingsResponses[Math.min(settingsResponseIndex, settingsResponses.length - 1)] || settingsPayload || {};
            settingsResponseIndex += 1;
            return jsonResponse(current);
          }
          if (path.includes("./api/ssh/hosts")) {
            const method = String(options?.method || "GET").toUpperCase();
            if (method === "POST") {
              const body = options?.body ? JSON.parse(String(options.body)) : {};
              return jsonResponse({ ok: true, alias: String(body.alias || "") });
            }
            return jsonResponse({ ok: true, hosts: {} });
          }
          if (path.includes("./api/files/tree")) {
            return jsonResponse({ path: ".", items: fileTreeItems });
          }
          if (path.includes("./api/files/read")) {
            return jsonResponse(fileReadPayload);
          }
          if (path.includes("./api/v1/admin/status")) {
            const current = adminResponses[Math.min(adminStatusIndex, adminResponses.length - 1)] || adminPayload;
            adminStatusIndex += 1;
            if (current && current.kind === "http") {
              return {
                ok: false,
                status: Number(current.status || 500),
                text: async () => JSON.stringify(current.body || {}),
              };
            }
            if (current && current.kind === "timeout") {
              const err = new Error("timeout");
              err.name = "AbortError";
              throw err;
            }
            return jsonResponse(current);
          }
          if (path.endsWith("./api/v1/admin/action")) {
            return jsonResponse(adminActionResponse || { ok: true, action: "rescan", status: adminPayload });
          }
          return jsonResponse({});
        }

        class FakeWebSocket {
          constructor(url) {
            this.url = String(url || "");
            realSetTimeout(() => {
              if (typeof this.onopen === "function") {
                this.onopen();
              }
              if (this.url.includes("/status/ws")) {
                const snapshots = Array.isArray(statusSnapshots) && statusSnapshots.length
                  ? statusSnapshots
                  : [statusSnapshot];
                snapshots.forEach((snapshot, index) => {
                  const payload = {
                    type: index === 0 ? "snapshot" : "update",
                    status: snapshot,
                  };
                  if (typeof this.onmessage === "function") {
                    this.onmessage({ data: JSON.stringify(payload) });
                  }
                });
              } else if (this.url.includes("/logs/ws")) {
                if (typeof this.onmessage === "function") {
                  this.onmessage({ data: JSON.stringify({ type: "snapshot", entries: [] }) });
                }
              }
            }, 0);
          }
          close() {
            if (typeof this.onclose === "function") {
              this.onclose({ wasClean: true, code: 1000 });
            }
          }
        }

        global.document = document;
        global.window = {
          Telegram: {
            WebApp: {
              ready() {},
              expand() {},
              initData: "",
              openLink(url) {
                openedLinks.push(String(url || ""));
              },
            },
          },
          location: { href: "http://localhost/" },
          requestAnimationFrame(fn) { fn(); },
          addEventListener() {},
          confirm() { return true; },
          prompt() { return ""; },
          alert(message) {
            alerts.push(String(message || ""));
          },
        };
        global.fetch = fetchStub;
        global.ace = { edit() { return editor; } };
        global.WebSocket = FakeWebSocket;
        global.setInterval = (fn, ms) => {
          intervalCalls.push({ fn, ms });
          return intervalCalls.length;
        };
        global.clearInterval = () => {};

        async function wait(ms = 0) {
          await new Promise((resolve) => realSetTimeout(resolve, ms));
        }

        (async () => {
          try {
            const source = fs.readFileSync("miniapp/static/app.js", "utf8");
            vm.runInThisContext(source, { filename: "miniapp/static/app.js" });
            await wait(20);

            const adminButton = document.querySelector('.tabs button[data-tab="admin"]');
            if (!adminButton || typeof adminButton.onclick !== "function") {
              throw new Error("admin tab button handler missing");
            }
            adminButton.onclick();
            await wait(20);

            const firstPollCount = fetchCalls.filter((item) => item.includes("./api/v1/admin/status")).length;
            if (!intervalCalls.length) {
              throw new Error("admin polling interval missing");
            }

            await wait(20);

            if (filesSessionValue) {
              const filesSession = document.getElementById("filesSession");
              filesSession.value = String(filesSessionValue);
              if (clickFilesApply) {
                const filesButton = document.getElementById("filesApply");
                if (!filesButton || typeof filesButton.onclick !== "function") {
                  throw new Error("files apply button handler missing");
                }
                filesButton.onclick();
                await wait(20);
              }
              if (clickFirstFile) {
                const filesTree = document.getElementById("filesTree");
                const firstFile = filesTree.children[0];
                if (!firstFile || typeof firstFile.onclick !== "function") {
                  throw new Error("file item handler missing");
                }
                firstFile.onclick();
                await wait(20);
              }
            }

            if (settingsPayload || (Array.isArray(settingsResponses) && settingsResponses.length)) {
              const settingsSession = document.getElementById("settingsSession");
              const settingsApply = document.getElementById("settingsApply");
              settingsSession.value = String(settingsSessionValue || "thread:1:55");
              if (!settingsApply || typeof settingsApply.onclick !== "function") {
                throw new Error("settings apply handler missing");
              }
              await settingsApply.onclick();
              await wait(20);
            }

            if (sshFormValues && typeof sshFormValues === "object") {
              const assignValue = (id, value) => {
                const el = document.getElementById(id);
                if (!el) {
                  throw new Error(`missing ssh form field: ${id}`);
                }
                if (typeof value === "boolean") {
                  el.checked = value;
                } else {
                  el.value = String(value ?? "");
                }
              };
              Object.entries({
                sshHostAlias: sshFormValues.alias || "",
                sshHostAliasOriginal: sshFormValues.original_alias || "",
                sshHostAddr: sshFormValues.host || "",
                sshHostPort: sshFormValues.port || 22,
                sshHostUser: sshFormValues.user || "",
                sshHostAuth: sshFormValues.auth || "key",
                sshHostKeyFile: sshFormValues.key_file || "",
                sshHostKeyPassEnv: sshFormValues.key_passphrase_env || "",
                sshHostPasswordEnv: sshFormValues.password_env || "",
                sshHostPassword: sshFormValues.password || "",
                sshHostSudo: !!sshFormValues.sudo,
                sshHostSudoPassEnv: sshFormValues.sudo_password_env || "",
                sshHostSudoPassword: sshFormValues.sudo_password || "",
                sshHostRoles: sshFormValues.roles || "",
                sshHostDesc: sshFormValues.description || "",
                sshHostRemoteProjectRoot: sshFormValues.remote_project_root || "",
                sshHostTimeout: sshFormValues.idle_timeout_sec || 1200,
              }).forEach(([id, value]) => assignValue(id, value));
            }

            if (clickSshSave) {
              const saveButton = document.getElementById("sshHostSave");
              if (!saveButton || typeof saveButton.onclick !== "function") {
                throw new Error("ssh save button handler missing");
              }
              await saveButton.onclick();
              await wait(20);
            }

            if (clickAction) {
              const button = document.getElementById(clickAction);
              if (!button || typeof button.onclick !== "function") {
                throw new Error(`admin action button missing: ${clickAction}`);
              }
              button.onclick();
              await wait(20);
            }

            if (clickAdminSubtab) {
              const button = document.querySelectorAll("[data-admin-subtab]")
                .find((item) => String(item.dataset.adminSubtab || "") === String(clickAdminSubtab));
              if (!button || typeof button.onclick !== "function") {
                throw new Error(`admin subtab button missing: ${clickAdminSubtab}`);
              }
              button.onclick();
              await wait(20);
            }

            if (waitBeforeIntervalMs > 0) {
              await wait(waitBeforeIntervalMs);
            }

            if (runInterval) {
              await intervalCalls[0].fn();
              await wait(20);
            }

            const secondPollCount = fetchCalls.filter((item) => item.includes("./api/v1/admin/status")).length;
            const adminActionCount = fetchCalls.filter((item) => item.includes("./api/v1/admin/action")).length;
            const adminPane = document.getElementById("tab-admin");
            const adminSession = document.getElementById("adminSession");
            const filesSession = document.getElementById("filesSession");
            const adminPipelineStatus = document.getElementById("adminPipelineStatus");
            const adminMonitorReadiness = document.getElementById("adminMonitorReadiness");
            const adminMonitorReadinessHint = document.getElementById("adminMonitorReadinessHint");
            const adminLastAnalyzerAction = document.getElementById("adminLastAnalyzerAction");
            const adminLastAnalyzerActionHint = document.getElementById("adminLastAnalyzerActionHint");
            const adminDisabledState = document.getElementById("adminDisabledState");
            const adminActiveState = document.getElementById("adminActiveState");
            const adminStructuredState = document.getElementById("adminStructuredState");
            const adminDisabledHint = document.getElementById("adminDisabledHint");
            const adminStatusBanner = document.getElementById("adminStatusBanner");
            const adminStatusMessage = document.getElementById("adminStatusMessage");
            const adminEnableAction = document.getElementById("adminEnableAction");
            const adminDisableAction = document.getElementById("adminDisableAction");
            const adminRescanAction = document.getElementById("adminRescanAction");
            const adminPendingState = document.getElementById("adminPendingState");
            const adminPendingSkillInstalls = document.getElementById("adminPendingSkillInstalls");
            const adminMuteState = document.getElementById("adminMuteState");
            const adminRecentIncidents = document.getElementById("adminRecentIncidents");
            const adminRecentActions = document.getElementById("adminRecentActions");
            const adminApprovedOverrides = document.getElementById("adminApprovedOverrides");
            const adminLastDecision = document.getElementById("adminLastDecision");
            const adminLastAction = document.getElementById("adminLastAction");
            const adminSkillApprovalSelect = document.getElementById("adminSkillApprovalSelect");
            const adminRuntimeBody = document.getElementById("adminRuntimeBody");
            const adminReadinessBody = document.getElementById("adminReadinessBody");
            const adminEnvironmentBody = document.getElementById("adminEnvironmentBody");
            const adminOperatorBody = document.getElementById("adminOperatorBody");
            const adminDecisionBody = document.getElementById("adminDecisionBody");
            const adminHistoryBody = document.getElementById("adminHistoryBody");
            const adminSubtabButtons = Array.from(document.querySelectorAll("[data-admin-subtab]"));
            const adminSubtabPanels = Array.from(document.querySelectorAll("[data-admin-subtab-panel]"));
            const stGitText = document.getElementById("stGitText");
            const statusLastTickText = document.getElementById("statusLastTickText");
            const tickListContainer = document.getElementById("tickListContainer");

            console.log(JSON.stringify({
              tabActive: adminPane.classList.contains("active"),
              adminSessionValue: String(adminSession.value || ""),
              filesSessionValue: String(filesSession.value || ""),
              firstPollCount,
              secondPollCount,
              adminActionCount,
              pollIntervalMs: Number(intervalCalls[0].ms || 0),
              pipelineStatus: String(adminPipelineStatus.textContent || ""),
              monitorReadiness: String(adminMonitorReadiness.textContent || ""),
              monitorReadinessHint: String(adminMonitorReadinessHint.textContent || ""),
              lastAnalyzerAction: String(adminLastAnalyzerAction.textContent || ""),
              lastAnalyzerActionHint: String(adminLastAnalyzerActionHint.textContent || ""),
              disabledHidden: adminDisabledState.classList.contains("hidden"),
              activeHidden: adminActiveState.classList.contains("hidden"),
              structuredHidden: adminStructuredState.classList.contains("hidden"),
              disabledHint: String(adminDisabledHint.textContent || ""),
              bannerText: String(adminStatusBanner.textContent || ""),
              bannerHidden: adminStatusBanner.classList.contains("hidden"),
              messageText: String(adminStatusMessage.textContent || ""),
              messageHidden: adminStatusMessage.classList.contains("hidden"),
              enableDisabled: !!adminEnableAction.disabled,
              disableDisabled: !!adminDisableAction.disabled,
              rescanDisabled: !!adminRescanAction.disabled,
              pendingStateText: String(adminPendingState.textContent || ""),
              pendingSkillInstallsText: String(adminPendingSkillInstalls.textContent || ""),
              muteStateText: String(adminMuteState.textContent || ""),
              recentIncidentsText: String(adminRecentIncidents.textContent || ""),
              recentActionsText: String(adminRecentActions.textContent || ""),
              approvedOverridesText: String(adminApprovedOverrides.textContent || ""),
              lastDecisionText: String(adminLastDecision.textContent || ""),
              lastActionText: String(adminLastAction.textContent || ""),
              adminSkillApprovalSelectValue: String(adminSkillApprovalSelect.value || ""),
              adminSubtabs: adminSubtabButtons.map((button) => String(button.dataset.adminSubtab || "")),
              adminActiveSubtab: String(
                adminSubtabButtons.find((button) => button.classList.contains("active"))?.dataset.adminSubtab || ""
              ),
              adminVisibleSubtabPanels: adminSubtabPanels
                .filter((panel) => !panel.classList.contains("hidden"))
                .map((panel) => String(panel.dataset.adminSubtabPanel || "")),
              runtimeDetailsHtml: String(adminRuntimeBody.innerHTML || ""),
              readinessDetailsHtml: String(adminReadinessBody.innerHTML || ""),
              environmentDetailsHtml: String(adminEnvironmentBody.innerHTML || ""),
              operatorDetailsHtml: String(adminOperatorBody.innerHTML || ""),
              decisionDetailsHtml: String(adminDecisionBody.innerHTML || ""),
              historyDetailsHtml: String(adminHistoryBody.innerHTML || ""),
              gitText: String(stGitText.textContent || ""),
              lastTickText: String(statusLastTickText.textContent || ""),
              tickRows: Array.from(tickListContainer.children || []).map((child) => String(child.innerHTML || child.textContent || "")),
              editorPath: String(document.getElementById("editorPath").textContent || ""),
              fetchCalls,
              openedLinks,
              alerts,
              lastAdminStatusUrl: String(fetchCalls.filter((item) => item.includes("./api/v1/admin/status")).slice(-1)[0] || ""),
              lastAdminActionBody: (() => {
                const req = fetchRequests.filter((item) => String(item.path || "").includes("./api/v1/admin/action")).slice(-1)[0];
                if (!req || !req.body) return null;
                try {
                  return JSON.parse(String(req.body));
                } catch {
                  return { raw: String(req.body) };
                }
              })(),
              lastFilesTreeUrl: String(fetchCalls.filter((item) => item.includes("./api/files/tree")).slice(-1)[0] || ""),
              settingsFetchCount: fetchCalls.filter((item) => item.includes("/api/session/") && item.endsWith("/settings")).length,
              lastSshHostRequestPath: String(
                fetchRequests
                  .filter((item) => String(item.path || "").includes("./api/ssh/hosts") && item.body)
                  .slice(-1)[0]?.path || ""
              ),
              lastSshHostRequestBody: (() => {
                const req = fetchRequests
                  .filter((item) => String(item.path || "").includes("./api/ssh/hosts") && item.body)
                  .slice(-1)[0];
                if (!req || !req.body) return null;
                try {
                  return JSON.parse(String(req.body));
                } catch {
                  return { raw: String(req.body) };
                }
              })(),
            }));
          } catch (err) {
            console.error(err && err.stack ? err.stack : String(err));
            process.exit(1);
          }
        })();
        """
    )
    script = script_template.replace(
        "__ADMIN_PAYLOAD__",
        json.dumps(admin_payload, ensure_ascii=False),
    )
    script = script.replace(
        "__ADMIN_RESPONSES__",
        json.dumps(admin_responses or [admin_payload], ensure_ascii=False),
    )
    script = script.replace(
        "__ADMIN_ACTION_RESPONSE__",
        json.dumps(admin_action_response, ensure_ascii=False) if admin_action_response is not None else "null",
    )
    script = script.replace(
        "__SETTINGS_PAYLOAD__",
        json.dumps(settings_payload, ensure_ascii=False) if settings_payload is not None else "null",
    )
    script = script.replace(
        "__SETTINGS_RESPONSES__",
        json.dumps(settings_responses or [], ensure_ascii=False),
    )
    script = script.replace(
        "__SETTINGS_SESSION_VALUE__",
        json.dumps(settings_session_value, ensure_ascii=False),
    )
    script = script.replace(
        "__STATUS_SNAPSHOT__",
        json.dumps(
            status_snapshot
            or {
                "available_sessions": [
                    {
                        "session_uid": "thread:1:55",
                        "chat_id": 1,
                        "session_id": "s1",
                        "session_name": "Admin session",
                        "tool": "dummy",
                        "label": "Admin session (thread:1:55)",
                    },
                ],
                "selected_session_uid": "thread:1:55",
                "session_count": 1,
                "active_session": {
                    "id": "s1",
                    "session_uid": "thread:1:55",
                    "name": "Admin session",
                    "workdir": "/tmp",
                    "started_age_sec": 1,
                    "last_output_age_sec": 1,
                    "last_tick_age_sec": 1,
                    "busy": False,
                    "git_busy": False,
                    "git_conflict": False,
                    "queue_len": 0,
                    "advanced_orchestrator_enabled": False,
                    "active_mode": "admin",
                    "active_cli": "dummy",
                    "cli_work_type": "",
                    "manager_plan_status": "",
                    "agent_mode_status": "",
                    "analyst_mode_status": "",
                    "webmaster_mode_status": "",
                    "runtime_status": "",
                    "state_summary": "",
                    "last_tick_value": "",
                    "tick_history": [],
                    "fields": {},
                },
            },
            ensure_ascii=False,
        ),
    )
    script = script.replace(
        "__STATUS_SNAPSHOTS__",
        json.dumps(status_snapshots or [], ensure_ascii=False),
    )
    script = script.replace(
        "__CLICK_ACTION__",
        json.dumps(click_action, ensure_ascii=False),
    )
    script = script.replace(
        "__CLICK_ADMIN_SUBTAB__",
        json.dumps(click_admin_subtab, ensure_ascii=False),
    )
    script = script.replace(
        "__SSH_FORM_VALUES__",
        json.dumps(ssh_form_values, ensure_ascii=False) if ssh_form_values is not None else "null",
    )
    script = script.replace(
        "__CLICK_SSH_SAVE__",
        "true" if click_ssh_save else "false",
    )
    script = script.replace(
        "__FILES_SESSION_VALUE__",
        json.dumps(files_session_value, ensure_ascii=False),
    )
    script = script.replace(
        "__CLICK_FILES_APPLY__",
        "true" if click_files_apply else "false",
    )
    script = script.replace(
        "__CLICK_FIRST_FILE__",
        "true" if click_first_file else "false",
    )
    script = script.replace(
        "__FILE_TREE_ITEMS__",
        json.dumps(file_tree_items or [], ensure_ascii=False),
    )
    script = script.replace(
        "__FILE_READ_PAYLOAD__",
        json.dumps(
            file_read_payload
            or {
                "content": "hello",
                "revision": "rev-1",
                "meta": {
                    "path": "notes.txt",
                    "size": 5,
                    "mtime": 1,
                },
            },
            ensure_ascii=False,
        ),
    )
    script = script.replace(
        "__WAIT_BEFORE_INTERVAL_MS__",
        json.dumps(wait_before_interval_ms, ensure_ascii=False),
    )
    script = script.replace(
        "__RUN_INTERVAL__",
        "true" if run_interval else "false",
    )
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".js",
        prefix=".tmp_miniapp_app_js_test_",
        delete=False,
    ) as handle:
        handle.write(script)
        script_path = Path(handle.name)
    try:
        result = subprocess.run(
            ["node", str(script_path)],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        script_path.unlink(missing_ok=True)

    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip())


def _run_scheduler_app_js_harness(
    scheduler_payload: dict,
    *,
    action: str,
    form_values: dict | None = None,
) -> dict:
    repo_root = Path(__file__).resolve().parent.parent
    script_template = textwrap.dedent(
        r"""
        const fs = require("fs");
        const vm = require("vm");

        const realSetTimeout = global.setTimeout;
        const fetchCalls = [];
        const fetchRequests = [];
        const schedulerPayload = __SCHEDULER_PAYLOAD__;
        const action = __ACTION__;
        const formValues = __FORM_VALUES__;

        class ClassList {
          constructor() {
            this._set = new Set();
          }
          add(...names) {
            names.forEach((name) => this._set.add(String(name)));
          }
          remove(...names) {
            names.forEach((name) => this._set.delete(String(name)));
          }
          contains(name) {
            return this._set.has(String(name));
          }
          toggle(name, force) {
            const key = String(name);
            if (force === undefined) {
              if (this._set.has(key)) {
                this._set.delete(key);
                return false;
              }
              this._set.add(key);
              return true;
            }
            if (force) {
              this._set.add(key);
              return true;
            }
            this._set.delete(key);
            return false;
          }
        }

        class Element {
          constructor(tagName = "div", id = "") {
            this.tagName = String(tagName || "div").toUpperCase();
            this.id = String(id || "");
            this.dataset = {};
            this.style = {};
            this.value = "";
            this.checked = false;
            this.disabled = false;
            this.children = [];
            this.attributes = {};
            this.classList = new ClassList();
            this.parentElement = { style: {} };
            this.onclick = null;
            this.options = [];
            this._textContent = "";
            this._innerHTML = "";
          }
          set textContent(value) {
            this._textContent = String(value || "");
          }
          get textContent() {
            return this._textContent;
          }
          set innerHTML(value) {
            this._innerHTML = String(value || "");
            this.children = [];
            const optionCount = (this._innerHTML.match(/<option\b/g) || []).length;
            this.options = Array.from({ length: optionCount }, () => ({}));
          }
          get innerHTML() {
            return this._innerHTML;
          }
          appendChild(child) {
            if (child && typeof child === "object") {
              child.parentElement = this;
            }
            this.children.push(child);
            return child;
          }
          append(child) {
            this.appendChild(child);
          }
          remove() {}
          setAttribute(name, value) {
            this.attributes[String(name)] = String(value);
          }
          getAttribute(name) {
            return this.attributes[String(name)] || "";
          }
          querySelectorAll() {
            return [];
          }
          querySelector() {
            return null;
          }
          addEventListener() {}
          removeEventListener() {}
        }

        const elements = new Map();
        const selectIds = new Set([
          "logsType",
          "logsHistory",
          "logsSession",
          "statusSession",
          "adminSession",
          "schedulerProject",
          "schedulerSession",
          "schedulerTargetMode",
          "schedulerPayload",
        ]);
        const checkboxIds = new Set(["logsAutoScroll", "ticksAutoScroll", "schedulerEnabled"]);

        function getElement(id) {
          const key = String(id || "");
          if (!elements.has(key)) {
            const tagName = selectIds.has(key) ? "select" : "div";
            const el = new Element(tagName, key);
            if (checkboxIds.has(key)) {
              el.checked = key === "schedulerEnabled" ? true : true;
            }
            if (key === "cfgSave") {
              el.disabled = true;
            }
            elements.set(key, el);
          }
          return elements.get(key);
        }

        const tabButtons = ["config", "files", "logs", "status", "scheduler", "admin"].map((tab) => {
          const el = new Element("button", `btn-${tab}`);
          el.dataset.tab = tab;
          if (tab === "config") {
            el.classList.add("active");
          }
          return el;
        });
        const tabPanes = ["config", "files", "editor", "logs", "status", "scheduler", "admin"].map((tab) => {
          const el = getElement(`tab-${tab}`);
          el.classList.add("tab");
          if (tab === "config") {
            el.classList.add("active");
          }
          return el;
        });

        const document = {
          body: new Element("body", "body"),
          getElementById(id) {
            return getElement(id);
          },
          querySelectorAll(selector) {
            if (selector === ".tabs button") {
              return tabButtons;
            }
            if (selector === ".tab") {
              return tabPanes;
            }
            if (selector === "#schedulerJobsList li") {
              return getElement("schedulerJobsList").children;
            }
            return [];
          },
          querySelector(selector) {
            const match = String(selector || "").match(/^\.tabs button\[data-tab="([^"]+)"\]$/);
            if (match) {
              return tabButtons.find((item) => item.dataset.tab === match[1]) || null;
            }
            return null;
          },
          createElement(tagName) {
            return new Element(tagName);
          },
          createTextNode(text) {
            return { nodeType: 3, textContent: String(text || "") };
          },
        };

        const editor = {
          setTheme() {},
          setShowPrintMargin() {},
          setValue() {},
          getValue() { return ""; },
          resize() {},
          session: { setMode() {} },
        };

        function jsonResponse(body) {
          return {
            ok: true,
            status: 200,
            text: async () => JSON.stringify(body),
          };
        }

        async function fetchStub(url, options = {}) {
          const path = String(url || "");
          const method = String(options?.method || "GET").toUpperCase();
          fetchCalls.push(path);
          fetchRequests.push({
            path,
            method,
            body: options && Object.prototype.hasOwnProperty.call(options, "body") ? options.body : null,
          });
          if (path.endsWith("./api/auth/me")) {
            return jsonResponse({ user_id: 1, is_admin: false, username: "scheduler-user" });
          }
          if (path.endsWith("./api/status/ws_ticket")) {
            return jsonResponse({ ticket: "status-ticket" });
          }
          if (path.endsWith("./api/logs/meta")) {
            return jsonResponse({ log_types: ["main"], history_options: [0], sessions: [] });
          }
          if (path.endsWith("./api/logs/ws_ticket")) {
            return jsonResponse({ ticket: "logs-ticket" });
          }
          if (path.includes("./api/v1/scheduler/jobs")) {
            if (method === "GET") {
              return jsonResponse(schedulerPayload);
            }
            return jsonResponse({ ok: true, job: schedulerPayload.jobs?.[0] || null, event: schedulerPayload.jobs?.[0] || null });
          }
          return jsonResponse({});
        }

        class FakeWebSocket {
          constructor(url) {
            this.url = String(url || "");
            realSetTimeout(() => {
              if (typeof this.onopen === "function") {
                this.onopen();
              }
              if (typeof this.onmessage === "function") {
                if (this.url.includes("/status/ws")) {
                  this.onmessage({
                    data: JSON.stringify({
                      type: "snapshot",
                      status: {
                        available_sessions: [],
                        selected_session_uid: "",
                        session_count: 0,
                        active_session: null,
                        modes: [
                          { id: "manager", label: "Manager" },
                          { id: "agent", label: "Agent" },
                        ],
                      },
                    }),
                  });
                } else if (this.url.includes("/logs/ws")) {
                  this.onmessage({ data: JSON.stringify({ type: "snapshot", entries: [] }) });
                }
              }
            }, 0);
          }
          close() {
            if (typeof this.onclose === "function") {
              this.onclose({ wasClean: true, code: 1000 });
            }
          }
        }

        global.document = document;
        global.window = {
          Telegram: { WebApp: { ready() {}, expand() {}, initData: "" } },
          location: { href: "http://localhost/" },
          requestAnimationFrame(fn) { fn(); },
          addEventListener() {},
          confirm() { return true; },
          prompt() { return ""; },
          alert() {},
        };
        global.fetch = fetchStub;
        global.ace = { edit() { return editor; } };
        global.WebSocket = FakeWebSocket;
        global.setInterval = () => 1;
        global.clearInterval = () => {};

        async function wait(ms = 0) {
          await new Promise((resolve) => realSetTimeout(resolve, ms));
        }

        function lastSchedulerBody(needle) {
          const req = fetchRequests
            .filter((item) => String(item.path || "").includes(needle) && item.body)
            .slice(-1)[0];
          if (!req) return null;
          try {
            return JSON.parse(String(req.body));
          } catch {
            return { raw: String(req.body) };
          }
        }

        (async () => {
          try {
            const source = fs.readFileSync("miniapp/static/app.js", "utf8");
            vm.runInThisContext(source, { filename: "miniapp/static/app.js" });
            await wait(30);

            const schedulerButton = document.querySelector('.tabs button[data-tab="scheduler"]');
            if (!schedulerButton || typeof schedulerButton.onclick !== "function") {
              throw new Error("scheduler tab button handler missing");
            }
            schedulerButton.onclick();
            await wait(30);

            const project = document.getElementById("schedulerProject");
            const session = document.getElementById("schedulerSession");
            const jobName = document.getElementById("schedulerJobName");
            const cron = document.getElementById("schedulerCron");
            const targetMode = document.getElementById("schedulerTargetMode");
            const enabled = document.getElementById("schedulerEnabled");
            const payload = document.getElementById("schedulerPayload");
            const jobList = document.getElementById("schedulerJobsList");

            if (formValues.project_slug) {
              project.value = String(formValues.project_slug);
            }
            if (formValues.telegram_session_uid) {
              session.value = String(formValues.telegram_session_uid);
            }
            if (formValues.job_name) {
              jobName.value = String(formValues.job_name);
            }
            if (formValues.cron) {
              cron.value = String(formValues.cron);
            }
            if (formValues.target_mode) {
              targetMode.value = String(formValues.target_mode);
            }
            if (formValues.payload) {
              payload.value = typeof formValues.payload === "string" ? formValues.payload : JSON.stringify(formValues.payload);
            }
            if (Object.prototype.hasOwnProperty.call(formValues, "enabled")) {
              enabled.checked = !!formValues.enabled;
            }

            if (action !== "create" && jobList.children.length) {
              const first = jobList.children[0];
              if (first && typeof first.onclick === "function") {
                first.onclick();
                await wait(20);
              }
            }

            if (action === "create" || action === "update") {
              const button = document.getElementById("schedulerSave");
              if (!button || typeof button.onclick !== "function") {
                throw new Error("scheduler save button handler missing");
              }
              button.onclick();
            } else if (action === "delete") {
              const button = document.getElementById("schedulerDelete");
              if (!button || typeof button.onclick !== "function") {
                throw new Error("scheduler delete button handler missing");
              }
              button.onclick();
            } else if (action === "run_now") {
              const button = document.getElementById("schedulerRunNow");
              if (!button || typeof button.onclick !== "function") {
                throw new Error("scheduler run_now button handler missing");
              }
              button.onclick();
            }

            await wait(40);

            console.log(JSON.stringify({
              tabActive: document.getElementById("tab-scheduler").classList.contains("active"),
              listFetchCount: fetchCalls.filter((item) => item.includes("./api/v1/scheduler/jobs")).length,
              selectedProject: String(project.value || ""),
              selectedSessionUid: String(session.value || ""),
              selectedPayload: String(payload.value || ""),
              selectedJobCount: jobList.children.length,
              schedulerStatus: String(document.getElementById("schedulerStatus").textContent || ""),
              createBody: lastSchedulerBody("./api/v1/scheduler/jobs"),
              updateBody: lastSchedulerBody("./api/v1/scheduler/jobs/update"),
              deleteBody: lastSchedulerBody("./api/v1/scheduler/jobs/delete"),
              runNowBody: lastSchedulerBody("./api/v1/scheduler/jobs/run_now"),
            }));
          } catch (err) {
            console.error(err && err.stack ? err.stack : String(err));
            process.exit(1);
          }
        })();
        """
    )
    script = script_template.replace(
        "__SCHEDULER_PAYLOAD__",
        json.dumps(scheduler_payload, ensure_ascii=False),
    )
    script = script.replace(
        "__ACTION__",
        json.dumps(action, ensure_ascii=False),
    )
    script = script.replace(
        "__FORM_VALUES__",
        json.dumps(form_values or {}, ensure_ascii=False),
    )
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".js",
        prefix=".tmp_miniapp_scheduler_app_js_test_",
        delete=False,
    ) as handle:
        handle.write(script)
        script_path = Path(handle.name)
    try:
        result = subprocess.run(
            ["node", str(script_path)],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        script_path.unlink(missing_ok=True)

    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip())


def _run_runs_app_js_harness(
    runs_payload: dict,
    run_detail_payload: dict,
    *,
    click_action: str | None = None,
    run_action_response: dict | None = None,
    status_snapshot: dict | None = None,
    is_admin: bool = False,
) -> dict:
    repo_root = Path(__file__).resolve().parent.parent
    script_template = textwrap.dedent(
        r"""
        const fs = require("fs");
        const vm = require("vm");

        const realSetTimeout = global.setTimeout;
        const fetchCalls = [];
        const fetchRequests = [];
        const runsPayload = __RUNS_PAYLOAD__;
        const runDetailPayload = __RUN_DETAIL_PAYLOAD__;
        const clickAction = __CLICK_ACTION__;
        const runActionResponse = __RUN_ACTION_RESPONSE__;
        const statusSnapshot = __STATUS_SNAPSHOT__;
        const isAdmin = __IS_ADMIN__;
        const intervalCalls = [];

        class ClassList {
          constructor() {
            this._set = new Set();
          }
          add(...names) {
            names.forEach((name) => this._set.add(String(name)));
          }
          remove(...names) {
            names.forEach((name) => this._set.delete(String(name)));
          }
          contains(name) {
            return this._set.has(String(name));
          }
          toggle(name, force) {
            const key = String(name);
            if (force === undefined) {
              if (this._set.has(key)) {
                this._set.delete(key);
                return false;
              }
              this._set.add(key);
              return true;
            }
            if (force) {
              this._set.add(key);
              return true;
            }
            this._set.delete(key);
            return false;
          }
        }

        class Element {
          constructor(tagName = "div", id = "") {
            this.tagName = String(tagName || "div").toUpperCase();
            this.id = String(id || "");
            this.dataset = {};
            this.style = {};
            this.value = "";
            this.checked = false;
            this.disabled = false;
            this.children = [];
            this.attributes = {};
            this.classList = new ClassList();
            this.parentElement = { style: {} };
            this.onclick = null;
            this._textContent = "";
            this._innerHTML = "";
            this.options = [];
          }
          set textContent(value) {
            this._textContent = String(value || "");
          }
          get textContent() {
            return this._textContent;
          }
          set innerHTML(value) {
            this._innerHTML = String(value || "");
            this.children = [];
            const optionCount = (this._innerHTML.match(/<option\b/g) || []).length;
            this.options = Array.from({ length: optionCount }, () => ({}));
          }
          get innerHTML() {
            return this._innerHTML;
          }
          appendChild(child) {
            if (child && typeof child === "object") {
              child.parentElement = this;
            }
            this.children.push(child);
            return child;
          }
          append(child) {
            this.appendChild(child);
          }
          remove() {}
          setAttribute(name, value) {
            this.attributes[String(name)] = String(value);
          }
          getAttribute(name) {
            return this.attributes[String(name)] || "";
          }
          querySelectorAll() {
            return [];
          }
          querySelector() {
            return null;
          }
          addEventListener() {}
          removeEventListener() {}
        }

        const elements = new Map();
        const selectIds = new Set([
          "logsType",
          "logsHistory",
          "logsSession",
          "filesSession",
          "statusSession",
          "adminSession",
          "schedulerProject",
          "schedulerSession",
          "schedulerTargetMode",
          "schedulerPayload",
        ]);
        const checkboxIds = new Set(["logsAutoScroll", "ticksAutoScroll", "schedulerEnabled"]);

        function getElement(id) {
          const key = String(id || "");
          if (!elements.has(key)) {
            const tagName = selectIds.has(key) ? "select" : "div";
            const el = new Element(tagName, key);
            if (checkboxIds.has(key)) {
              el.checked = true;
            }
            elements.set(key, el);
          }
          return elements.get(key);
        }

        const tabButtons = ["config", "files", "logs", "status", "scheduler", "admin"].map((tab) => {
          const el = new Element("button", `btn-${tab}`);
          el.dataset.tab = tab;
          if (tab === "config") {
            el.classList.add("active");
          }
          return el;
        });
        const tabPanes = ["config", "files", "editor", "logs", "status", "scheduler", "admin"].map((tab) => {
          const el = getElement(`tab-${tab}`);
          el.classList.add("tab");
          if (tab === "config") {
            el.classList.add("active");
          }
          return el;
        });

        const document = {
          body: new Element("body", "body"),
          getElementById(id) {
            return getElement(id);
          },
          querySelectorAll(selector) {
            if (selector === ".tabs button") {
              return tabButtons;
            }
            if (selector === ".tab") {
              return tabPanes;
            }
            if (selector === "#statusRunsList li") {
              return getElement("statusRunsList").children;
            }
            return [];
          },
          querySelector(selector) {
            const match = String(selector || "").match(/^\.tabs button\[data-tab="([^"]+)"\]$/);
            if (match) {
              return tabButtons.find((item) => item.dataset.tab === match[1]) || null;
            }
            return null;
          },
          createElement(tagName) {
            return new Element(tagName);
          },
          createTextNode(text) {
            return { nodeType: 3, textContent: String(text || "") };
          },
        };

        const editor = {
          setTheme() {},
          setShowPrintMargin() {},
          setValue() {},
          getValue() { return ""; },
          resize() {},
          session: { setMode() {} },
        };

        function jsonResponse(body) {
          return {
            ok: true,
            status: 200,
            text: async () => JSON.stringify(body),
          };
        }

        async function fetchStub(url, options = {}) {
          const path = String(url || "");
          fetchCalls.push(path);
          fetchRequests.push({
            path,
            body: options && Object.prototype.hasOwnProperty.call(options, "body") ? options.body : null,
          });
          if (path.endsWith("./api/auth/me")) {
            return jsonResponse({ user_id: 1, is_admin: isAdmin, username: "miniapp-user" });
          }
          if (path.endsWith("./api/status/ws_ticket")) {
            return jsonResponse({ ticket: "status-ticket" });
          }
          if (path.endsWith("./api/logs/meta")) {
            return jsonResponse({ log_types: ["main"], history_options: [0], sessions: [] });
          }
          if (path.endsWith("./api/logs/ws_ticket")) {
            return jsonResponse({ ticket: "logs-ticket" });
          }
          if (path.includes("./api/runs/") && /\/(doctor|recover|resume|apply_recommendation|promote_skills)$/.test(path)) {
            return jsonResponse(runActionResponse || { ok: true, result: {}, run: runDetailPayload.run || null });
          }
          if (path.includes("./api/runs/") && !path.includes("./api/runs?")) {
            return jsonResponse(runDetailPayload);
          }
          if (path.includes("./api/runs?")) {
            return jsonResponse(runsPayload);
          }
          return jsonResponse({});
        }

        class FakeWebSocket {
          constructor(url) {
            this.url = String(url || "");
            realSetTimeout(() => {
              if (typeof this.onopen === "function") {
                this.onopen();
              }
              if (this.url.includes("/status/ws")) {
                if (typeof this.onmessage === "function") {
                  this.onmessage({ data: JSON.stringify({ type: "snapshot", status: statusSnapshot }) });
                }
              } else if (this.url.includes("/logs/ws")) {
                if (typeof this.onmessage === "function") {
                  this.onmessage({ data: JSON.stringify({ type: "snapshot", entries: [] }) });
                }
              }
            }, 0);
          }
          close() {
            if (typeof this.onclose === "function") {
              this.onclose({ wasClean: true, code: 1000 });
            }
          }
        }

        global.document = document;
        global.window = {
          Telegram: {
            WebApp: {
              ready() {},
              expand() {},
              initData: "",
              openLink() {},
            },
          },
          location: { href: "http://localhost/" },
          requestAnimationFrame(fn) { fn(); },
          addEventListener() {},
          confirm() { return true; },
          prompt() { return ""; },
          alert() {},
        };
        global.fetch = fetchStub;
        global.ace = { edit() { return editor; } };
        global.WebSocket = FakeWebSocket;
        global.setInterval = (fn, ms) => {
          intervalCalls.push({ fn, ms });
          return intervalCalls.length;
        };
        global.clearInterval = () => {};

        async function wait(ms = 0) {
          await new Promise((resolve) => realSetTimeout(resolve, ms));
        }

        (async () => {
          try {
            const source = fs.readFileSync("miniapp/static/app.js", "utf8");
            vm.runInThisContext(source, { filename: "miniapp/static/app.js" });
            await wait(30);

            const statusButton = document.querySelector('.tabs button[data-tab="status"]');
            if (!statusButton || typeof statusButton.onclick !== "function") {
              throw new Error("status tab button handler missing");
            }
            statusButton.onclick();
            await wait(120);

            if (clickAction) {
              const button = document.getElementById(clickAction);
              if (!button || typeof button.onclick !== "function") {
                throw new Error(`run action button missing: ${clickAction}`);
              }
              button.onclick();
              await wait(120);
            }

            const lastActionRequest = fetchRequests
              .filter((item) => {
                const path = String(item.path || "");
                return path.includes("./api/runs/") && /\/(doctor|recover|resume|apply_recommendation|promote_skills)$/.test(path);
              })
              .slice(-1)[0] || null;
            const lastDetailUrl = String(
              fetchCalls
                .filter((item) => {
                    const path = String(item || "");
                    return (
                      path.includes("./api/runs/")
                    && !/\/(doctor|recover|resume|apply_recommendation|promote_skills)$/.test(path)
                    && !path.includes("./api/runs?")
                  );
                })
                .slice(-1)[0] || ""
            );
            const lastListUrl = String(fetchCalls.filter((item) => String(item).includes("./api/runs?")).slice(-1)[0] || "");

            console.log(JSON.stringify({
              tabActive: document.getElementById("tab-status").classList.contains("active"),
              runListUrl: lastListUrl,
              runDetailUrl: lastDetailUrl,
              runActionBody: (() => {
                if (!lastActionRequest || !lastActionRequest.body) return null;
                try {
                  return JSON.parse(String(lastActionRequest.body));
                } catch {
                  return { raw: String(lastActionRequest.body) };
                }
              })(),
              runActionPath: String(lastActionRequest ? lastActionRequest.path : ""),
              detailText: String(document.getElementById("statusRunDetailText").textContent || ""),
              skillLogText: String(document.getElementById("statusRunSkillLog").textContent || ""),
              actionMessage: String(document.getElementById("statusRunsActionMessage").textContent || ""),
              runsPanelHidden: document.getElementById("statusRunsPanel").classList.contains("hidden"),
              recoverDisabled: !!document.getElementById("statusRunRecover").disabled,
              resumeDisabled: !!document.getElementById("statusRunResume").disabled,
              fetchCalls,
            }));
          } catch (err) {
            console.error(err && err.stack ? err.stack : String(err));
            process.exit(1);
          }
        })();
        """
    )
    script = script_template.replace("__RUNS_PAYLOAD__", json.dumps(runs_payload, ensure_ascii=False))
    script = script.replace("__RUN_DETAIL_PAYLOAD__", json.dumps(run_detail_payload, ensure_ascii=False))
    script = script.replace("__CLICK_ACTION__", json.dumps(click_action, ensure_ascii=False))
    script = script.replace("__IS_ADMIN__", "true" if is_admin else "false")
    script = script.replace(
        "__RUN_ACTION_RESPONSE__",
        json.dumps(run_action_response or {"ok": True, "result": {}, "run": run_detail_payload.get("run")}, ensure_ascii=False),
    )
    script = script.replace(
        "__STATUS_SNAPSHOT__",
        json.dumps(
            status_snapshot
            or {
                "available_sessions": [
                    {
                        "session_uid": "thread:1:55",
                        "chat_id": 1,
                        "session_id": "s1",
                        "session_name": "Mini session",
                        "tool": "dummy",
                        "label": "Mini session (thread:1:55)",
                    },
                ],
                "selected_session_uid": "thread:1:55",
                "session_count": 1,
                "active_session": {
                    "id": "s1",
                    "session_uid": "thread:1:55",
                    "name": "Mini session",
                    "workdir": "/tmp",
                    "started_age_sec": 1,
                    "last_output_age_sec": 1,
                    "last_tick_age_sec": 1,
                    "busy": False,
                    "git_busy": False,
                    "git_conflict": False,
                    "queue_len": 0,
                    "advanced_orchestrator_enabled": False,
                    "active_mode": "agent",
                    "active_cli": "dummy",
                    "cli_work_type": "",
                    "manager_plan_status": "",
                    "agent_mode_status": "",
                    "analyst_mode_status": "",
                    "webmaster_mode_status": "",
                    "runtime_status": "",
                    "state_summary": "",
                    "last_tick_value": "",
                    "tick_history": [],
                    "fields": {},
                },
            },
            ensure_ascii=False,
        ),
    )
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".js",
        prefix=".tmp_miniapp_runs_app_js_test_",
        delete=False,
    ) as handle:
        handle.write(script)
        script_path = Path(handle.name)
    try:
        result = subprocess.run(
            ["node", str(script_path)],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        script_path.unlink(missing_ok=True)

    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip())


def test_miniapp_app_js_switches_to_admin_tab_and_polls_status() -> None:
    payload = _run_app_js_harness(
        {
            "mode": "admin",
            "session_uid": "thread:1:55",
            "session_id": "s1",
            "active": True,
            "busy": False,
            "run_lock_locked": False,
            "tick_active": False,
            "mode_tasks_running": True,
            "pipeline_status": "running",
            "analyzer_status": "completed",
            "analyzer_message": "restart_nginx (high)",
            "executor_status": "running",
            "executor_message": "Executor ok",
        }
    )

    assert payload["tabActive"] is True
    assert payload["adminSessionValue"] == "thread:1:55"
    assert payload["filesSessionValue"] == "thread:1:55"
    assert payload["firstPollCount"] >= 1
    assert payload["secondPollCount"] >= 2
    assert payload["pollIntervalMs"] == 5000
    assert "session_uid=thread%3A1%3A55" in payload["lastAdminStatusUrl"]
    assert payload["pipelineStatus"] == "running"
    assert payload["monitorReadiness"] == "Live"
    assert "pipeline-run" in payload["monitorReadinessHint"]
    assert payload["lastAnalyzerAction"] == "restart_nginx (high)"
    assert payload["lastAnalyzerActionHint"] == "Analyzer status: completed"
    assert payload["disabledHidden"] is True
    assert payload["activeHidden"] is False
    assert payload["structuredHidden"] is False
    assert payload["adminSubtabs"] == ["overview", "monitoring", "operations", "config", "chat", "diagnostics"]
    assert payload["adminActiveSubtab"] == "overview"
    assert payload["adminVisibleSubtabPanels"] == ["overview"]
    assert payload["bannerHidden"] is True
    assert "pipeline_status" in payload["runtimeDetailsHtml"]
    assert "running" in payload["runtimeDetailsHtml"]


def test_miniapp_app_js_admin_inner_tabs_switch_sections() -> None:
    payload = _run_app_js_harness(
        {
            "mode": "admin",
            "session_uid": "thread:1:55",
            "session_id": "s1",
            "active": True,
            "busy": False,
            "run_lock_locked": False,
            "tick_active": False,
            "mode_tasks_running": False,
            "pipeline_status": "idle",
            "analyzer_status": "idle",
            "analyzer_message": "",
            "executor_status": "idle",
            "executor_message": "",
            "pending_skill_installs": {"count": 1, "active": True, "items": []},
        },
        click_admin_subtab="operations",
        wait_before_interval_ms=0,
        run_interval=False,
    )

    assert payload["adminActiveSubtab"] == "operations"
    assert payload["adminVisibleSubtabPanels"] == ["operations"]
    assert payload["pendingSkillInstallsText"] == "1 pending | active"


def test_miniapp_app_js_files_tab_uses_its_own_session_selector() -> None:
    payload = _run_app_js_harness(
        {
            "mode": "admin",
            "session_uid": "thread:1:55",
            "session_id": "s1",
            "active": True,
            "busy": False,
            "run_lock_locked": False,
            "tick_active": False,
            "mode_tasks_running": False,
            "pipeline_status": "idle",
            "analyzer_status": "idle",
            "analyzer_message": "",
            "executor_status": "idle",
            "executor_message": "",
        },
        status_snapshot={
            "available_sessions": [
                {
                    "session_uid": "thread:1:55",
                    "chat_id": 1,
                    "session_id": "s1",
                    "session_name": "Session one",
                    "tool": "dummy",
                    "label": "Session one",
                },
                {
                    "session_uid": "thread:1:77",
                    "chat_id": 1,
                    "session_id": "s2",
                    "session_name": "Session two",
                    "tool": "dummy",
                    "label": "Session two",
                },
            ],
            "selected_session_uid": "thread:1:55",
            "session_count": 2,
            "active_session": {
                "id": "s1",
                "session_uid": "thread:1:55",
                "name": "Session one",
                "workdir": "/tmp",
                "started_age_sec": 1,
                "last_output_age_sec": 1,
                "last_tick_age_sec": 1,
                "busy": False,
                "git_busy": False,
                "git_conflict": False,
                "queue_len": 0,
                "advanced_orchestrator_enabled": False,
                "active_mode": "admin",
                "active_cli": "dummy",
                "cli_work_type": "",
                "manager_plan_status": "",
                "agent_mode_status": "",
                "analyst_mode_status": "",
                "webmaster_mode_status": "",
                "runtime_status": "",
                "state_summary": "",
                "last_tick_value": "",
                "tick_history": [],
                "fields": {},
            },
        },
        files_session_value="thread:1:77",
        click_files_apply=True,
        wait_before_interval_ms=0,
        run_interval=False,
    )

    assert payload["adminSessionValue"] == "thread:1:55"
    assert payload["filesSessionValue"] == "thread:1:77"

    assert "session_uid=thread%3A1%3A77" in payload["lastFilesTreeUrl"]


def test_miniapp_status_header_meta_is_compact_without_duplicate_labels() -> None:
    index_html = (Path(__file__).resolve().parent.parent / "miniapp" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (Path(__file__).resolve().parent.parent / "miniapp" / "static" / "app.js").read_text(encoding="utf-8")

    assert '<span id="stWorkdir">/path</span>' in index_html
    assert '<span id="stServerTime">-</span>' in index_html
    assert '<span class="meta-icon">OUT</span> <span id="stLastOutput">0s</span>' in index_html
    assert '<span class="meta-icon">GIT</span> <span id="stGitText">Свободен</span>' in index_html

    assert ">DIR</span>" not in index_html

    assert ">SYNC</span>" not in index_html

    assert ">TICK</span>" not in index_html

    assert "Последний вывод:" not in index_html

    assert "Последний тик:" not in index_html

    assert "Статус:" not in index_html

    assert "Очередь:" not in index_html

    assert "Git: Свободен" not in index_html

    assert "Orchestrator:" not in index_html

    assert "В работе:" not in index_html

    assert "Сессий:" not in index_html

    assert "Обновлено:" not in index_html
    assert 'id="statusLastTickDetails"' in index_html
    assert 'id="statusTicksDetails"' in index_html
    assert 'id="ticksAssistantOnly"' not in index_html
    assert 'id="stLastTick"' not in index_html
    assert 'id="tickListContainer"' in index_html
    assert 'id="statusRunsDetails" open' not in index_html
    assert 'getElementById("stLastTick")' not in app_js
    assert 'statusLastTickDetails' in app_js
    assert 'statusTicksDetails' in app_js
    assert 'ticksAssistantOnly' not in app_js
    assert 'tickListContainer' in app_js


def test_miniapp_app_js_status_uses_last_assistant_text_and_shows_all_ticks() -> None:
    payload = _run_app_js_harness(
        {
            "mode": "admin",
            "session_uid": "thread:1:55",
            "session_id": "s1",
            "active": True,
            "busy": False,
            "run_lock_locked": False,
            "tick_active": False,
            "mode_tasks_running": False,
            "pipeline_status": "idle",
            "analyzer_status": "idle",
            "analyzer_message": "",
            "executor_status": "idle",
            "executor_message": "",
        },
        status_snapshot={
            "available_sessions": [
                {
                    "session_uid": "thread:1:55",
                    "chat_id": 1,
                    "session_id": "s1",
                    "session_name": "Session one",
                    "tool": "dummy",
                    "label": "Session one",
                },
            ],
            "selected_session_uid": "thread:1:55",
            "session_count": 1,
            "active_session": {
                "id": "s1",
                "session_uid": "thread:1:55",
                "name": "Session one",
                "workdir": "/tmp",
                "started_age_sec": 1,
                "last_output_age_sec": 1,
                "last_tick_age_sec": 1,
                "busy": False,
                "git_busy": False,
                "git_conflict": False,
                "queue_len": 0,
                "advanced_orchestrator_enabled": False,
                "active_mode": "admin",
                "active_cli": "dummy",
                "cli_work_type": "",
                "manager_plan_status": "",
                "agent_mode_status": "",
                "analyst_mode_status": "",
                "webmaster_mode_status": "",
                "runtime_status": "",
                "state_summary": "",
                "last_tick_value": "tool tick",
                "last_assistant_text_value": "assistant final",
                "assistant_tick_count": 2,
                "tick_history": [
                    {"ts": 1.0, "value": "tool tick", "kind": "tool_event"},
                    {"ts": 2.0, "value": "assistant first", "kind": "assistant_text"},
                    {"ts": 3.0, "value": "assistant final", "kind": "assistant_text"},
                    {"ts": 4.0, "value": "thinking detail", "kind": "thinking"},
                ],
                "fields": {},
            },
        },
    )

    assert payload["lastTickText"] == "assistant final"
    assert len(payload["tickRows"]) == 4
    assert any("tool tick" in row for row in payload["tickRows"])
    assert any("assistant first" in row for row in payload["tickRows"])
    assert any("assistant final" in row for row in payload["tickRows"])
    assert any("thinking detail" in row for row in payload["tickRows"])


def test_miniapp_app_js_replaces_streamed_assistant_tick_in_place() -> None:
    base_status = {
        "available_sessions": [
            {
                "session_uid": "thread:1:55",
                "chat_id": 1,
                "session_id": "s1",
                "session_name": "Session one",
                "tool": "dummy",
                "label": "Session one",
            },
        ],
        "selected_session_uid": "thread:1:55",
        "session_count": 1,
        "active_session": {
            "id": "s1",
            "session_uid": "thread:1:55",
            "name": "Session one",
            "workdir": "/tmp",
            "started_age_sec": 1,
            "last_output_age_sec": 1,
            "last_tick_age_sec": 1,
            "busy": False,
            "git_busy": False,
            "git_conflict": False,
            "queue_len": 0,
            "advanced_orchestrator_enabled": False,
            "active_mode": "admin",
            "active_cli": "dummy",
            "cli_work_type": "",
            "manager_plan_status": "",
            "agent_mode_status": "",
            "analyst_mode_status": "",
            "webmaster_mode_status": "",
            "runtime_status": "",
            "state_summary": "",
            "last_tick_value": "",
            "fields": {},
        },
    }
    first_status = json.loads(json.dumps(base_status))
    first_status["active_session"]["last_assistant_text_value"] = "assistant part"
    first_status["active_session"]["assistant_tick_count"] = 1
    first_status["active_session"]["tick_history"] = [
        {"ts": 1.0, "value": "assistant part", "kind": "assistant_text"},
    ]

    second_status = json.loads(json.dumps(base_status))
    second_status["active_session"]["last_assistant_text_value"] = "assistant final"
    second_status["active_session"]["assistant_tick_count"] = 1
    second_status["active_session"]["tick_history"] = [
        {"ts": 2.0, "value": "assistant final", "kind": "assistant_text"},
    ]

    payload = _run_app_js_harness(
        {
            "mode": "admin",
            "session_uid": "thread:1:55",
            "session_id": "s1",
            "active": True,
            "busy": False,
            "run_lock_locked": False,
            "tick_active": False,
            "mode_tasks_running": False,
            "pipeline_status": "idle",
            "analyzer_status": "idle",
            "analyzer_message": "",
            "executor_status": "idle",
            "executor_message": "",
        },
        status_snapshot=first_status,
        status_snapshots=[first_status, second_status],
    )

    assert payload["lastTickText"] == "assistant final"
    assert len(payload["tickRows"]) == 1
    assert "assistant final" in payload["tickRows"][0]
    assert "assistant part" not in payload["tickRows"][0]


def test_miniapp_app_js_git_status_value_does_not_repeat_git_prefix() -> None:
    payload = _run_app_js_harness(
        {
            "mode": "admin",
            "session_uid": "thread:1:55",
            "session_id": "s1",
            "active": True,
            "busy": False,
            "run_lock_locked": False,
            "tick_active": False,
            "mode_tasks_running": False,
            "pipeline_status": "idle",
            "analyzer_status": "idle",
            "analyzer_message": "",
            "executor_status": "idle",
            "executor_message": "",
        }
    )

    assert payload["gitText"] == "Свободен"


def test_miniapp_app_js_renders_disabled_admin_state() -> None:
    payload = _run_app_js_harness(
        {
            "mode": "admin",
            "session_uid": "thread:1:55",
            "session_id": "s1",
            "active": False,
            "busy": False,
            "run_lock_locked": False,
            "tick_active": False,
            "mode_tasks_running": False,
            "pipeline_status": "disabled",
            "analyzer_status": "idle",
            "analyzer_message": "",
            "executor_status": "idle",
            "executor_message": "",
        }
    )

    assert payload["tabActive"] is True
    assert payload["firstPollCount"] >= 1
    assert payload["secondPollCount"] >= 2
    assert payload["disabledHidden"] is False
    assert payload["activeHidden"] is True
    assert payload["structuredHidden"] is False
    assert payload["disabledHint"]

    assert "Admin-режим выключен" in payload["disabledHint"]
    assert payload["enableDisabled"] is False
    assert payload["disableDisabled"] is True
    assert payload["rescanDisabled"] is False

    assert "pipeline_status" in payload["runtimeDetailsHtml"]

    assert "disabled" in payload["runtimeDetailsHtml"]


def test_miniapp_admin_operations_separates_waiting_and_history_labels() -> None:
    app_js = (Path(__file__).resolve().parent.parent / "miniapp" / "static" / "app.js").read_text(encoding="utf-8")

    assert "Ожидает действий" in app_js
    assert "Последние события" in app_js
    assert "ручные подтверждения" in app_js
    assert "История событий / overrides" in app_js
    assert 'admin-section-eyebrow">Pending' not in app_js


def test_miniapp_app_js_renders_admin_payload_fields_as_ui_sections() -> None:
    payload = _run_app_js_harness(
        {
            "mode": "admin",
            "session_uid": "thread:1:55",
            "session_id": "s1",
            "active": False,
            "busy": False,
            "run_lock_locked": False,
            "tick_active": False,
            "mode_tasks_running": False,
            "pinned_cli": {},
            "pinned_executor_profile": None,
            "initialized_at": None,
            "last_scan_at": None,
            "scan_status": "not_started",
            "scan_error": None,
            "component_readiness": {
                "monitor": False,
                "analyzer": False,
                "executor": False,
                "notifier": False,
            },
            "environment_services": ["nginx", "python"],
            "environment_stack_facts": {"python": "3.12", "nginx": "1.27"},
            "pending_ask_user": {
                "count": 1,
                "active": True,
                "current": {"kind": "confirm", "question": "Перезапустить nginx?"},
            },
            "pending_approvals": {"count": 2, "active": True},
            "mute_state": {"muted_until_ts": None, "muted": False},
            "recent_incidents": [{"id": "inc-1", "title": "HTTP 502"}],
            "recent_admin_actions": [{"id": "act-1", "action": "restart_nginx"}],
            "approved_overrides": [{"action": "restart_nginx", "ttl": 3600}],
            "pipeline_status": "disabled",
            "monitor_status": "disabled",
            "analyzer_status": "idle",
            "analyzer_message": "",
            "executor_status": "idle",
            "executor_message": "",
            "notifier_status": "disabled",
            "notifier_message": "",
            "last_monitor_snapshot": {"http_502_count": 3},
            "last_analyzer_decision": {"action": "notify_admin", "confidence": "low"},
            "last_action": {"action": "restart_nginx", "status": "done"},
        }
    )

    assert payload["structuredHidden"] is False

    assert "session_uid" in payload["runtimeDetailsHtml"]

    assert "thread:1:55" in payload["runtimeDetailsHtml"]

    assert "session_id" in payload["runtimeDetailsHtml"]

    assert "s1" in payload["runtimeDetailsHtml"]

    assert "component_readiness" in payload["readinessDetailsHtml"]

    assert "monitor" in payload["readinessDetailsHtml"]

    assert "environment_services" in payload["environmentDetailsHtml"]

    assert "nginx" in payload["environmentDetailsHtml"]

    assert "pending_ask_user" in payload["operatorDetailsHtml"]

    assert "Перезапустить nginx" in payload["operatorDetailsHtml"]

    assert "last_analyzer_decision" in payload["decisionDetailsHtml"]

    assert "notify_admin" in payload["decisionDetailsHtml"]

    assert "recent_admin_actions" in payload["historyDetailsHtml"]

    assert "restart_nginx" in payload["historyDetailsHtml"]

    assert payload["pendingStateText"] == "ask_user 1 | approvals 2 | active"
    assert payload["pendingSkillInstallsText"] == "0 pending"
    assert payload["muteStateText"] == "off"
    assert payload["recentIncidentsText"] == "1 | inc-1 | HTTP 502"
    assert payload["recentActionsText"] == "1 | act-1 | restart_nginx"
    assert payload["approvedOverridesText"] == "1 | restart_nginx"
    assert payload["lastDecisionText"] == "action=notify_admin | confidence=low"
    assert payload["lastActionText"] == "action=restart_nginx | status=done"
    assert "{" not in payload["pendingStateText"]
    assert "{" not in payload["recentIncidentsText"]
    assert "{" not in payload["recentActionsText"]
    assert "{" not in payload["lastDecisionText"]
    assert "{" not in payload["lastActionText"]


@pytest.mark.parametrize(
    ("click_action", "payload_body"),
    [
        (
            "adminEnableAction",
            {
                "mode": "admin",
                "session_uid": "thread:1:55",
                "session_id": "s1",
                "active": False,
                "busy": False,
                "run_lock_locked": False,
                "tick_active": False,
                "mode_tasks_running": False,
                "pipeline_status": "disabled",
                "analyzer_status": "idle",
                "analyzer_message": "",
                "executor_status": "idle",
                "executor_message": "",
            },
        ),
        (
            "adminDisableActionActive",
            {
                "mode": "admin",
                "session_uid": "thread:1:55",
                "session_id": "s1",
                "active": True,
                "busy": False,
                "run_lock_locked": False,
                "tick_active": False,
                "mode_tasks_running": True,
                "pipeline_status": "running",
                "analyzer_status": "completed",
                "analyzer_message": "notify_admin",
                "executor_status": "idle",
                "executor_message": "",
            },
        ),
        (
            "adminRescanActionActive",
            {
                "mode": "admin",
                "session_uid": "thread:1:55",
                "session_id": "s1",
                "active": True,
                "busy": False,
                "run_lock_locked": False,
                "tick_active": False,
                "mode_tasks_running": True,
                "pipeline_status": "running",
                "analyzer_status": "completed",
                "analyzer_message": "notify_admin",
                "executor_status": "idle",
                "executor_message": "",
            },
        ),
    ],
)
def test_miniapp_app_js_admin_buttons_call_action_endpoint(click_action: str, payload_body: dict) -> None:
    payload = _run_app_js_harness(
        payload_body,
        click_action=click_action,
        wait_before_interval_ms=0,
        run_interval=False,
    )

    assert payload["adminActionCount"] >= 1
    assert payload["lastAdminActionBody"]["session_uid"] == "thread:1:55"

    assert "session_id" not in payload["lastAdminActionBody"]


def test_miniapp_app_js_admin_tab_treats_chat_session_uid_as_opaque_identifier() -> None:
    payload = _run_app_js_harness(
        {
            "mode": "admin",
            "session_uid": "chat:1",
            "session_id": "s1",
            "active": False,
            "busy": False,
            "run_lock_locked": False,
            "tick_active": False,
            "mode_tasks_running": False,
            "pipeline_status": "disabled",
            "analyzer_status": "idle",
            "analyzer_message": "",
            "executor_status": "idle",
            "executor_message": "",
        },
        status_snapshot={
            "available_sessions": [
                {
                    "session_uid": "chat:1",
                    "chat_id": 1,
                    "session_id": "s1",
                    "session_name": "Chat session",
                    "tool": "dummy",
                    "label": "Chat session (chat:1)",
                },
            ],
            "selected_session_uid": "chat:1",
            "session_count": 1,
            "active_session": {
                "id": "s1",
                "session_uid": "chat:1",
                "name": "Chat session",
                "workdir": "/tmp",
                "started_age_sec": 1,
                "last_output_age_sec": 1,
                "last_tick_age_sec": 1,
                "busy": False,
                "git_busy": False,
                "git_conflict": False,
                "queue_len": 0,
                "advanced_orchestrator_enabled": False,
                "active_mode": "admin",
                "active_cli": "dummy",
                "cli_work_type": "",
                "manager_plan_status": "",
                "agent_mode_status": "",
                "analyst_mode_status": "",
                "webmaster_mode_status": "",
                "runtime_status": "",
                "state_summary": "",
                "last_tick_value": "",
                "tick_history": [],
                "fields": {},
            },
        },
        click_action="adminEnableAction",
        wait_before_interval_ms=0,
        run_interval=False,
    )

    assert payload["adminSessionValue"] == "chat:1"
    assert "session_uid=chat%3A1" in payload["lastAdminStatusUrl"]
    assert payload["lastAdminActionBody"]["session_uid"] == "chat:1"
    assert "session_id" not in payload["lastAdminActionBody"]


def test_miniapp_app_js_admin_skill_approval_buttons_call_action_endpoint() -> None:
    payload = _run_app_js_harness(
        {
            "mode": "admin",
            "session_uid": "thread:1:55",
            "session_id": "s1",
            "active": True,
            "busy": False,
            "run_lock_locked": False,
            "tick_active": False,
            "mode_tasks_running": False,
            "pipeline_status": "idle",
            "monitor_status": "idle",
            "analyzer_status": "idle",
            "analyzer_message": "",
            "executor_status": "idle",
            "executor_message": "",
            "notifier_status": "idle",
            "notifier_message": "",
            "scan_status": "ready",
            "pending_skill_installs": {
                "count": 1,
                "active": True,
                "items": [
                    {
                        "approval_id": "approval-1",
                        "skill_id": "playwright-cli-local",
                        "mode_id": "agent",
                        "phase": "execute",
                    }
                ],
            },
        },
        admin_action_response={
            "ok": True,
            "action": "approve_skill_install",
            "result": {
                "status": "ok",
                "approval_id": "approval-1",
                "skill_id": "playwright-cli-local",
                "message": "Skill `playwright-cli-local` установлен локально после approve.",
            },
            "status": {
                "mode": "admin",
                "session_uid": "thread:1:55",
                "session_id": "s1",
                "active": True,
                "busy": False,
                "run_lock_locked": False,
                "tick_active": False,
                "mode_tasks_running": False,
                "pipeline_status": "idle",
                "monitor_status": "idle",
                "analyzer_status": "idle",
                "analyzer_message": "",
                "executor_status": "idle",
                "executor_message": "",
                "notifier_status": "idle",
                "notifier_message": "",
                "scan_status": "ready",
                "pending_skill_installs": {"count": 0, "active": False, "items": []},
            },
        },
        click_action="adminSkillApprovalApprove",
        wait_before_interval_ms=0,
        run_interval=False,
    )

    assert payload["adminActionCount"] >= 1
    assert payload["adminSkillApprovalSelectValue"] == "approval-1"
    assert payload["lastAdminActionBody"]["action"] == "approve_skill_install"
    assert payload["lastAdminActionBody"]["session_uid"] == "thread:1:55"
    assert payload["lastAdminActionBody"]["approval_id"] == "approval-1"
    assert payload["pendingSkillInstallsText"] == "0 pending"

    assert "установлен локально" in payload["bannerText"]


def test_miniapp_app_js_admin_tab_requires_explicit_session_uid_selection() -> None:
    payload = _run_app_js_harness(
        {
            "mode": "admin",
            "session_uid": "thread:1:55",
            "session_id": "s1",
            "active": True,
            "busy": False,
            "run_lock_locked": False,
            "tick_active": False,
            "mode_tasks_running": False,
            "pipeline_status": "idle",
            "analyzer_status": "idle",
            "analyzer_message": "",
            "executor_status": "idle",
            "executor_message": "",
        },
        status_snapshot={
            "available_sessions": [
                {
                    "session_uid": "thread:1:55",
                    "chat_id": 1,
                    "session_id": "s1",
                    "session_name": "Admin session",
                    "tool": "dummy",
                    "label": "Admin session (thread:1:55)",
                },
            ],
            "selected_session_uid": "",
            "session_count": 1,
            "active_session": None,
            "status_text": "Сессия не выбрана",
        },
        wait_before_interval_ms=0,
        run_interval=False,
    )

    assert payload["adminSessionValue"] == ""
    assert payload["firstPollCount"] == 0
    assert payload["messageHidden"] is False

    assert "Сессия не выбрана" in payload["messageText"]


def test_miniapp_app_js_uses_cached_admin_status_for_immediate_repeat_request() -> None:
    payload = _run_app_js_harness(
        {
            "mode": "admin",
            "session_uid": "thread:1:55",
            "session_id": "s1",
            "active": True,
            "busy": False,
            "run_lock_locked": False,
            "tick_active": False,
            "mode_tasks_running": False,
            "pipeline_status": "idle",
            "analyzer_status": "idle",
            "analyzer_message": "Waiting for next cycle",
            "executor_status": "idle",
            "executor_message": "",
        },
        click_action="adminApply",
        wait_before_interval_ms=0,
        run_interval=False,
    )

    assert payload["firstPollCount"] == 1
    assert payload["secondPollCount"] == 1


@pytest.mark.parametrize(
    ("error_response", "expected_banner"),
    [
        ({"kind": "timeout"}, "Таймаут запроса Admin status."),
        ({"kind": "http", "status": 503, "body": {"error": "server unavailable"}}, "Сервер Admin status временно недоступен."),
        ({"kind": "http", "status": 403, "body": {"error": "forbidden"}}, "Нет доступа к Admin status для выбранной сессии."),
    ],
)
def test_miniapp_app_js_keeps_stale_admin_payload_on_status_errors(error_response: dict, expected_banner: str) -> None:
    payload = _run_app_js_harness(
        {
            "mode": "admin",
            "session_uid": "thread:1:55",
            "session_id": "s1",
            "active": True,
            "busy": False,
            "run_lock_locked": False,
            "tick_active": False,
            "mode_tasks_running": True,
            "pipeline_status": "running",
            "analyzer_status": "completed",
            "analyzer_message": "notify_admin",
            "executor_status": "running",
            "executor_message": "Executor ok",
        },
        admin_responses=[
            {
                "mode": "admin",
                "session_uid": "thread:1:55",
                "session_id": "s1",
                "active": True,
                "busy": False,
                "run_lock_locked": False,
                "tick_active": False,
                "mode_tasks_running": True,
                "pipeline_status": "running",
                "analyzer_status": "completed",
                "analyzer_message": "notify_admin",
                "executor_status": "running",
                "executor_message": "Executor ok",
            },
            error_response,
        ],
    )

    assert payload["pipelineStatus"] == "running"
    assert payload["activeHidden"] is False
    assert payload["bannerHidden"] is False
    assert expected_banner in payload["bannerText"]


def test_miniapp_app_js_scheduler_tab_creates_job_with_explicit_session_uid() -> None:
    payload = _run_scheduler_app_js_harness(
        {
            "ok": True,
            "projects": [{"slug": "alpha", "name": "Alpha"}],
            "selected_project_slug": "alpha",
            "notification_targets": [
                {
                    "telegram_session_uid": "thread:1:55",
                    "label": "Alpha session (thread:1:55)",
                },
            ],
            "jobs": [],
        },
        action="create",
        form_values={
            "project_slug": "alpha",
            "telegram_session_uid": "thread:1:55",
            "job_name": "Morning digest",
            "cron": "*/15 * * * *",
            "target_mode": "manager",
            "enabled": True,
        },
    )

    assert payload["tabActive"] is True
    assert payload["listFetchCount"] >= 1
    assert payload["selectedProject"] == "alpha"
    assert payload["selectedSessionUid"] == "thread:1:55"
    assert payload["createBody"]["project_slug"] == "alpha"
    assert payload["createBody"]["notification_target"] == {"telegram_session_uid": "thread:1:55"}

    assert "session_id" not in payload["createBody"]


def test_miniapp_app_js_scheduler_selected_job_actions_keep_project_scope() -> None:
    payload = _run_scheduler_app_js_harness(
        {
            "ok": True,
            "projects": [{"slug": "alpha", "name": "Alpha"}],
            "selected_project_slug": "alpha",
            "notification_targets": [
                {
                    "telegram_session_uid": "thread:1:55",
                    "label": "Alpha session (thread:1:55)",
                },
            ],
            "jobs": [
                {
                    "job_id": "job-alpha",
                    "job_name": "Morning digest",
                    "cron": "*/15 * * * *",
                    "target_mode": "manager",
                    "enabled": True,
                    "notification_target": {"telegram_session_uid": "thread:1:55"},
                    "payload": {"project_slug": "alpha"},
                    "next_run_at": 0,
                    "last_fired_at": 0,
                },
            ],
        },
        action="run_now",
        form_values={
            "project_slug": "alpha",
            "telegram_session_uid": "thread:1:55",
        },
    )

    assert payload["tabActive"] is True
    assert payload["selectedJobCount"] == 1
    assert payload["runNowBody"] == {"project_slug": "alpha", "job_id": "job-alpha"}


def test_miniapp_app_js_runs_panel_calls_routes_and_applies_recovery_state() -> None:
    payload = _run_runs_app_js_harness(
        {
            "ok": True,
            "session_uid": "thread:1:55",
            "runs": [
                {
                    "session_uid": "thread:1:55",
                    "mode_id": "agent",
                    "run_id": "run_20260313T120000Z_a1b2c3d4",
                    "status": "running",
                    "phase": "execute",
                    "can_resume": False,
                    "can_recover": True,
                    "recommended_action": "rollback_to_checkpoint",
                    "issue_codes": ["legacy_store_mismatch"],
                    "skill_log": ["Injected: playwright-cli, xlsx"],
                },
            ],
        },
        {
            "ok": True,
            "session_uid": "thread:1:55",
            "run": {
                "session_uid": "thread:1:55",
                "mode_id": "agent",
                "run_id": "run_20260313T120000Z_a1b2c3d4",
                "status": "running",
                "phase": "execute",
                "can_resume": False,
                "can_recover": True,
                "recommended_action": "rollback_to_checkpoint",
                "issue_codes": ["legacy_store_mismatch"],
                "skill_log": ["Injected: playwright-cli, xlsx"],
                "current_unit_id": "unit-3",
                "recovery": {
                    "status": "needs_recovery",
                    "recommended_action": "rollback_to_checkpoint",
                },
            },
        },
        click_action="statusRunRecover",
        run_action_response={
            "ok": True,
            "action": "recover",
            "result": {
                "operation": "recover",
                "status": "ok",
                "mode_id": "agent",
                "phase": "execute",
                "message": "Recovery: подготовлен безопасный workflow.",
                "run_id": "run_20260313T120000Z_a1b2c3d4",
                "recommended_action": "rollback_to_checkpoint",
                "blocked_by": [],
                "report": {
                    "status": "needs_recovery",
                    "recommended_action": "rollback_to_checkpoint",
                },
            },
            "run": {
                "session_uid": "thread:1:55",
                "mode_id": "agent",
                "run_id": "run_20260313T120000Z_a1b2c3d4",
                "status": "running",
                "phase": "execute",
                "can_resume": False,
                "can_recover": True,
                "recommended_action": "rollback_to_checkpoint",
                "issue_codes": ["legacy_store_mismatch"],
                "skill_log": ["Injected: playwright-cli, xlsx"],
                "current_unit_id": "unit-3",
                "recovery": {
                    "status": "needs_recovery",
                    "recommended_action": "rollback_to_checkpoint",
                    "last_requested_operation": {
                        "operation": "recover",
                        "status": "prepared",
                    },
                },
            },
        },
    )

    assert payload["tabActive"] is True

    assert "./api/runs?session_uid=thread%3A1%3A55&limit=12" in payload["runListUrl"]

    assert "./api/runs/run_20260313T120000Z_a1b2c3d4?session_uid=thread%3A1%3A55&mode_id=agent" in payload["runDetailUrl"]
    assert payload["runActionPath"].endswith("./api/runs/run_20260313T120000Z_a1b2c3d4/recover")
    assert payload["runActionBody"] == {"session_uid": "thread:1:55", "mode_id": "agent"}

    assert "rollback_to_checkpoint" in payload["detailText"]

    assert "Injected: playwright-cli, xlsx" in payload["skillLogText"]

    assert "Recovery: подготовлен безопасный workflow." in payload["actionMessage"]
    assert payload["runsPanelHidden"] is False


def test_miniapp_app_js_runs_panel_calls_apply_recommendation_route() -> None:
    payload = _run_runs_app_js_harness(
        {
            "ok": True,
            "session_uid": "thread:1:55",
            "runs": [
                {
                    "session_uid": "thread:1:55",
                    "mode_id": "codebase_mapper",
                    "run_id": "run_20260313T120500Z_mapper",
                    "status": "failed",
                    "phase": "operation",
                    "can_resume": False,
                    "can_apply_recommendation": True,
                    "recommended_action": "run_validate",
                    "issue_codes": ["boundary_contract_failed"],
                    "skill_log": [],
                },
            ],
        },
        {
            "ok": True,
            "session_uid": "thread:1:55",
            "run": {
                "session_uid": "thread:1:55",
                "mode_id": "codebase_mapper",
                "run_id": "run_20260313T120500Z_mapper",
                "status": "failed",
                "phase": "operation",
                "can_resume": False,
                "can_apply_recommendation": True,
                "recommended_action": "run_validate",
                "issue_codes": ["boundary_contract_failed"],
                "skill_log": [],
                "current_unit_id": "mapper_validate",
                "recovery": {
                    "status": "needs_recovery",
                    "recommended_action": "run_validate",
                },
            },
        },
        click_action="statusRunApplyRecommendation",
        run_action_response={
            "ok": True,
            "action": "apply_recommendation",
            "result": {
                "operation": "apply_recommendation",
                "status": "ok",
                "mode_id": "codebase_mapper",
                "phase": "operation",
                "message": "Validate operation executed.",
                "run_id": "run_20260313T120500Z_mapper",
                "recommended_action": "run_validate",
                "blocked_by": [],
                "report": {
                    "status": "needs_recovery",
                    "recommended_action": "run_validate",
                },
            },
            "run": {
                "session_uid": "thread:1:55",
                "mode_id": "codebase_mapper",
                "run_id": "run_20260313T120500Z_mapper",
                "status": "running",
                "phase": "operation",
                "can_resume": False,
                "can_apply_recommendation": True,
                "recommended_action": "run_validate",
                "issue_codes": [],
                "skill_log": [],
                "current_unit_id": "mapper_validate",
                "recovery": {
                    "status": "needs_recovery",
                    "recommended_action": "run_validate",
                    "last_requested_operation": {
                        "operation": "apply_recommendation",
                        "status": "executed",
                        "executed_operation": "validate",
                    },
                },
            },
        },
    )

    assert payload["runActionPath"].endswith("./api/runs/run_20260313T120500Z_mapper/apply_recommendation")
    assert payload["runActionBody"] == {"session_uid": "thread:1:55", "mode_id": "codebase_mapper"}

    assert "run_validate" in payload["detailText"]

    assert "Validate operation executed." in payload["actionMessage"]


def test_miniapp_app_js_runs_panel_calls_promote_route_and_applies_result() -> None:
    payload = _run_runs_app_js_harness(
        {
            "ok": True,
            "session_uid": "thread:1:55",
            "runs": [
                {
                    "session_uid": "thread:1:55",
                    "mode_id": "agent",
                    "run_id": "run_20260313T120000Z_a1b2c3d4",
                    "status": "running",
                    "phase": "execute",
                    "can_resume": True,
                    "recommended_action": "resume_same_phase",
                    "issue_codes": [],
                    "selected_skill_ids": ["playwright-cli"],
                    "project_local_skill_ids": ["playwright-cli"],
                    "skill_log": ["Injected: playwright-cli"],
                },
            ],
        },
        {
            "ok": True,
            "session_uid": "thread:1:55",
            "run": {
                "session_uid": "thread:1:55",
                "mode_id": "agent",
                "run_id": "run_20260313T120000Z_a1b2c3d4",
                "status": "running",
                "phase": "execute",
                "can_resume": True,
                "recommended_action": "resume_same_phase",
                "issue_codes": [],
                "selected_skill_ids": ["playwright-cli"],
                "project_local_skill_ids": ["playwright-cli"],
                "skill_log": ["Injected: playwright-cli"],
                "recovery": {},
            },
        },
        click_action="statusRunPromote",
        run_action_response={
            "ok": True,
            "action": "promote_skills",
            "result": {
                "status": "ok",
                "message": "Skills promoted to global registry.",
                "mode_id": "agent",
                "run_id": "run_20260313T120000Z_a1b2c3d4",
                "promoted_skill_ids": ["playwright-cli"],
                "skipped_skill_ids": [],
                "results": [],
            },
            "run": {
                "session_uid": "thread:1:55",
                "mode_id": "agent",
                "run_id": "run_20260313T120000Z_a1b2c3d4",
                "status": "running",
                "phase": "execute",
                "can_resume": True,
                "recommended_action": "resume_same_phase",
                "issue_codes": [],
                "selected_skill_ids": ["playwright-cli"],
                "project_local_skill_ids": ["playwright-cli"],
                "skill_log": ["Injected: playwright-cli", "Promoted: playwright-cli"],
                "recovery": {},
            },
        },
        is_admin=True,
    )

    assert payload["runActionPath"].endswith("./api/runs/run_20260313T120000Z_a1b2c3d4/promote_skills")
    assert payload["runActionBody"] == {"session_uid": "thread:1:55", "mode_id": "agent"}

    assert "Promoted: playwright-cli" in payload["skillLogText"]

    assert "Skills promoted to global registry." in payload["actionMessage"]


def test_miniapp_app_js_runs_panel_terminal_status_gating_matches_desktop_semantics() -> None:
    cases = [
        {
            "name": "running",
            "status": "running",
            "terminal_actions_blocked": False,
            "expected_recover_disabled": False,
            "expected_resume_disabled": False,
        },
        {
            "name": "failed",
            "status": "failed",
            "terminal_actions_blocked": False,
            "expected_recover_disabled": False,
            "expected_resume_disabled": False,
        },
        {
            "name": "superseded",
            "status": "superseded",
            "terminal_actions_blocked": False,
            "expected_recover_disabled": True,
            "expected_resume_disabled": True,
        },
        {
            "name": "completed",
            "status": "completed",
            "terminal_actions_blocked": False,
            "expected_recover_disabled": True,
            "expected_resume_disabled": True,
        },
    ]

    for case in cases:
        run_payload = {
            "session_uid": "thread:1:55",
            "mode_id": "agent",
            "run_id": f"run_{case['name']}",
            "status": case["status"],
            "can_resume": True,
            "can_recover": True,
            "recommended_action": "rollback_to_checkpoint",
            "terminal_actions_blocked": case["terminal_actions_blocked"],
        }
        payload = _run_runs_app_js_harness(
            {
                "ok": True,
                "session_uid": "thread:1:55",
                "runs": [dict(run_payload)],
            },
            {
                "ok": True,
                "session_uid": "thread:1:55",
                "run": dict(run_payload),
            },
        )

        assert payload["recoverDisabled"] is case["expected_recover_disabled"], case["name"]
        assert payload["resumeDisabled"] is case["expected_resume_disabled"], case["name"]


def test_miniapp_app_js_editor_download_button_uses_current_open_file() -> None:
    payload = _run_app_js_harness(
        {
            "mode": "admin",
            "session_uid": "thread:1:55",
            "session_id": "s1",
            "active": True,
            "busy": False,
            "run_lock_locked": False,
            "tick_active": False,
            "mode_tasks_running": False,
            "pipeline_status": "idle",
            "analyzer_status": "idle",
            "analyzer_message": "",
            "executor_status": "idle",
            "executor_message": "",
        },
        click_action="editorDownload",
        files_session_value="thread:1:55",
        click_files_apply=True,
        click_first_file=True,
        file_tree_items=[
            {
                "name": "notes.txt",
                "path": "notes.txt",
                "is_dir": False,
                "size": 5,
                "mtime": 1,
            },
        ],
        file_read_payload={
            "content": "hello",
            "revision": "rev-1",
            "meta": {
                "path": "notes.txt",
                "size": 5,
                "mtime": 1,
            },
        },
        wait_before_interval_ms=0,
        run_interval=False,
    )

    assert payload["editorPath"] == "notes.txt"
    assert any("./api/files/read?path=notes.txt&session_uid=thread%3A1%3A55" in item for item in payload["fetchCalls"])
    assert payload["alerts"] == []

    assert "./api/files/ws_ticket" in payload["fetchCalls"]
    assert len(payload["openedLinks"]) == 1

    assert "/api/files/download?path=notes.txt&session_uid=thread%3A1%3A55&ticket=file-ticket" in payload["openedLinks"][0]


def test_miniapp_app_js_scheduler_payload_roundtrip_and_validation() -> None:
    """
    Тест проверяет UI round-trip для scheduler payloads.

    Примечание: Используется JS harness вместо Playwright из-за ограничений среды:
    - Sandbox не позволяет запускать настоящий браузер
    - JS harness обеспечивает эквивалентное покрытие логики app.js:
      * Симуляция DOM и событий
      * Перехват fetch вызовов
      * Валидация JSON payload
      * Проверка project_slug preservation

    Для production-валидации рекомендуется ручное тестирование в Telegram MiniApp.
    """
    # 1. Create with custom payload
    payload = _run_scheduler_app_js_harness(
        {
            "ok": True,
            "projects": [{"slug": "beta", "name": "Beta"}],
            "selected_project_slug": "beta",
            "notification_targets": [{"telegram_session_uid": "t1", "label": "Lab"}],
            "jobs": [],
        },
        action="create",
        form_values={
            "project_slug": "beta",
            "telegram_session_uid": "t1",
            "cron": "0 0 * * *",
            "target_mode": "agent",
            "payload": '{"foo": "bar", "project_slug": "wrong"}'
        },
    )
    # project_slug should be preserved/overridden by current project_slug
    assert payload["createBody"]["payload"] == {"foo": "bar", "project_slug": "beta"}

    # 2. Select job and see it in payload field
    job_with_payload = {
        "job_id": "j1",
        "job_name": "Task",
        "cron": "0 0 * * *",
        "target_mode": "agent",
        "enabled": True,
        "notification_target": {"telegram_session_uid": "t1"},
        "payload": {"baz": 123, "project_slug": "beta"},
    }
    payload_res = _run_scheduler_app_js_harness(
        {
            "ok": True,
            "projects": [{"slug": "beta", "name": "Beta"}],
            "selected_project_slug": "beta",
            "notification_targets": [{"telegram_session_uid": "t1", "label": "Lab"}],
            "jobs": [job_with_payload],
        },
        action="select",  # this will trigger onclick for the first job
    )
    # harness returns the value of the payload field
    assert '"baz": 123' in payload_res["selectedPayload"]
    assert '"project_slug": "beta"' in payload_res["selectedPayload"]

    # 3. Validation error for bad JSON
    payload_err = _run_scheduler_app_js_harness(
        {
            "ok": True,
            "projects": [{"slug": "beta", "name": "Beta"}],
            "selected_project_slug": "beta",
            "notification_targets": [{"telegram_session_uid": "t1", "label": "Lab"}],
            "jobs": [],
        },
        action="create",
        form_values={
            "project_slug": "beta",
            "telegram_session_uid": "t1",
            "cron": "0 0 * * *",
            "target_mode": "agent",
            "payload": '{"invalid": json'
        },
    )

    assert "Некорректный JSON" in payload_err["schedulerStatus"]


def test_miniapp_app_js_save_ssh_host_refreshes_session_settings() -> None:
    initial_settings = {
        "ok": True,
        "settings": {
            "ssh_remote_enabled": True,
            "remote_control_enabled": False,
            "remote_control_host_alias": None,
        },
        "available": {
            "ssh_config_exists": True,
            "ssh_available": True,
            "project_workdir": "/projects/demo",
            "remote_control_hosts": {},
        },
        "remote_control_hosts": {},
        "effective": {
            "execution_target": "local",
            "host_alias": None,
            "remote_project_root": None,
            "git_available": True,
        },
    }
    refreshed_settings = {
        "ok": True,
        "settings": {
            "ssh_remote_enabled": True,
            "remote_control_enabled": False,
            "remote_control_host_alias": None,
        },
        "available": {
            "ssh_config_exists": True,
            "ssh_available": True,
            "project_workdir": "/projects/demo",
            "remote_control_hosts": {
                "Mb_test": {
                    "host": "83.69.203.41",
                    "user": "la",
                    "remote_project_root": "/",
                    "description": "",
                },
            },
        },
        "remote_control_hosts": {
            "Mb_test": {
                "host": "83.69.203.41",
                "user": "la",
                "remote_project_root": "/",
                "description": "",
            },
        },
        "effective": {
            "execution_target": "local",
            "host_alias": None,
            "remote_project_root": None,
            "git_available": True,
        },
    }

    payload = _run_app_js_harness(
        {
            "mode": "admin",
            "session_uid": "thread:1:55",
            "session_id": "s1",
            "active": True,
            "busy": False,
            "run_lock_locked": False,
            "tick_active": False,
            "mode_tasks_running": False,
            "pipeline_status": "idle",
            "analyzer_status": "idle",
            "analyzer_message": "",
            "executor_status": "idle",
            "executor_message": "",
        },
        settings_payload=initial_settings,
        settings_responses=[initial_settings, refreshed_settings],
        settings_session_value="thread:1:55",
        ssh_form_values={
            "alias": "Mb_test",
            "host": "83.69.203.41",
            "port": 37121,
            "user": "la",
            "auth": "password",
            "password": "secret-text",
            "remote_project_root": "/",
        },
        click_ssh_save=True,
    )

    assert payload["settingsFetchCount"] >= 2
    assert payload["lastSshHostRequestPath"].endswith("/api/ssh/hosts?workdir=%2Fprojects%2Fdemo")
    assert payload["lastSshHostRequestBody"]["remote_project_root"] == "/"
