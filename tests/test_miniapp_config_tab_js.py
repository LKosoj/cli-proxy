import json
import subprocess
import textwrap
from pathlib import Path
from typing import Any

from miniapp.services.config_service import SECRET_UNCHANGED_SENTINEL, config_schema


def _run_config_tab_harness(
    config_view: dict,
    *,
    save_response: dict[str, Any] | None = None,
    trigger_save: bool = False,
    input_values: dict[str, str] | None = None,
    clear_secret_ids: list[str] | None = None,
) -> dict:
    repo_root = Path(__file__).resolve().parent.parent
    script_template = textwrap.dedent(
        r"""
        const fs = require("fs");
        const vm = require("vm");

        const realSetTimeout = global.setTimeout;
        const configView = __CONFIG_VIEW__;
        const saveResponse = __SAVE_RESPONSE__;
        const triggerSave = __TRIGGER_SAVE__;
        const inputValues = __INPUT_VALUES__;
        const clearSecretIds = __CLEAR_SECRET_IDS__;
        const fetchRequests = [];
        const confirmMessages = [];

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
            this.onchange = null;
            this.oninput = null;
            this._textContent = "";
            this._innerHTML = "";
            this.options = [];
            this.type = "";
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
        ]);
        const checkboxIds = new Set(["logsAutoScroll", "ticksAutoScroll", "schedulerEnabled"]);

        function getElement(id) {
          const key = String(id || "");
          if (!elements.has(key)) {
            const tagName = selectIds.has(key) ? "select" : "div";
            const el = new Element(tagName, key);
            if (checkboxIds.has(key)) {
              el.type = "checkbox";
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
            if (selector === "#filesTree li") {
              return [];
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
          if (path.endsWith("./api/config/schema")) {
            return jsonResponse({ sections: [] });
          }
          if (path.endsWith("./api/config/view")) {
            return jsonResponse(configView);
          }
          if (path.endsWith("./api/config/validate")) {
            return jsonResponse({ ok: true, errors: [], warnings: [] });
          }
          if (path.endsWith("./api/config/diff")) {
            return jsonResponse({
              changed: [],
              reloadable: [],
              restart_required: ((saveResponse || {}).reload || {}).restart_required || [],
            });
          }
          if (path.endsWith("./api/config/save")) {
            return jsonResponse(saveResponse || {
              ok: true,
              revision: "r2",
              diff: { changed: [], reloadable: [], restart_required: [] },
              reload: { status: "success", applied: [], restart_required: [], warnings: [] },
            });
          }
          if (path.includes("./api/files/tree")) {
            return jsonResponse({ path: ".", items: [] });
          }
          return jsonResponse({});
        }

        class FakeWebSocket {
          constructor() {
            realSetTimeout(() => {
              if (typeof this.onopen === "function") {
                this.onopen();
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
          confirm(message) {
            confirmMessages.push(String(message || ""));
            return true;
          },
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

        (async () => {
          try {
            const source = fs.readFileSync("miniapp/static/app.js", "utf8");
            vm.runInThisContext(source, { filename: "miniapp/static/app.js" });
            await wait(20);

            Object.entries(inputValues || {}).forEach(([id, value]) => {
              const el = document.getElementById(id);
              el.value = String(value ?? "");
              if (typeof el.oninput !== "function") {
                throw new Error(`missing input handler: ${id}`);
              }
              el.oninput();
            });
            (clearSecretIds || []).forEach((id) => {
              const clearEl = document.getElementById(`${id}-clear`);
              if (!clearEl || typeof clearEl.onclick !== "function") {
                throw new Error(`missing secret clear handler: ${id}`);
              }
              clearEl.onclick({ preventDefault() {} });
            });

            const cfgDirty = document.getElementById("cfgDirty");
            const cfgSave = document.getElementById("cfgSave");
            if (triggerSave) {
              cfgSave.disabled = false;
              await cfgSave.onclick();
              await wait(20);
            }
            const cfgRestartBanner = document.getElementById("cfgRestartBanner");
            const cfgReloadResult = document.getElementById("cfgReloadResult");
            const cfgDiffResult = document.getElementById("cfgDiffResult");
            const secretInputIds = [
              "tg-token",
              "def-openai-api-key",
              "def-zai-key",
              "def-tavily-key",
              "def-jina-key",
              "def-github-token",
              "def-gemini-oauth-client-secret",
              "mcp-token",
              "webhooks-secret-token",
            ];
            console.log(JSON.stringify({
              dirtyText: String(cfgDirty.textContent || ""),
              saveDisabled: !!cfgSave.disabled,
              restartBannerHidden: !!cfgRestartBanner.classList.contains("hidden"),
              restartBannerText: String(cfgRestartBanner.textContent || ""),
              reloadResultHtml: String(cfgReloadResult.innerHTML || ""),
              diffResultHtml: String(cfgDiffResult.innerHTML || ""),
              confirmMessages,
              secretInputs: Object.fromEntries(secretInputIds.map((id) => {
                const el = document.getElementById(id);
                const clearEl = document.getElementById(`${id}-clear`);
                return [id, {
                  type: String(el.type || ""),
                  value: String(el.value || ""),
                  secretPath: String(el.getAttribute("data-secret-path") || ""),
                  clearHandler: !!(clearEl && typeof clearEl.onclick === "function"),
                }];
              })),
              saveDraft: (() => {
                const req = fetchRequests
                  .filter((item) => String(item.path || "").endsWith("./api/config/save"))
                  .slice(-1)[0];
                if (!req || !req.body) return null;
                return JSON.parse(String(req.body)).draft;
              })(),
            }));
          } catch (err) {
            console.error(err && err.stack ? err.stack : String(err));
            process.exit(1);
          }
        })();
        """
    )
    script = script_template.replace("__CONFIG_VIEW__", json.dumps(config_view, ensure_ascii=False))
    script = script.replace("__SAVE_RESPONSE__", json.dumps(save_response, ensure_ascii=False))
    script = script.replace("__TRIGGER_SAVE__", "true" if trigger_save else "false")
    script = script.replace("__INPUT_VALUES__", json.dumps(input_values or {}, ensure_ascii=False))
    script = script.replace("__CLEAR_SECRET_IDS__", json.dumps(clear_secret_ids or [], ensure_ascii=False))
    result = subprocess.run(
        ["node", "-e", script],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout.strip())


SECRET_INPUT_PATHS = {
    "tg-token": "telegram.token",
    "def-openai-api-key": "defaults.openai_api_key",
    "def-zai-key": "defaults.zai_api_key",
    "def-tavily-key": "defaults.tavily_api_key",
    "def-jina-key": "defaults.jina_api_key",
    "def-github-token": "defaults.github_token",
    "def-gemini-oauth-client-secret": "defaults.gemini_oauth_client_secret",
    "mcp-token": "mcp.token",
    "webhooks-secret-token": "webhooks.secret_token",
}


def _config_view_with_redacted_secrets(*, secret_value: str = SECRET_UNCHANGED_SENTINEL) -> dict:
    return {
        "revision": "r1",
        "redaction": {
            "sentinel": SECRET_UNCHANGED_SENTINEL,
            "fields": sorted(SECRET_INPUT_PATHS.values()),
        },
        "config": {
            "telegram": {
                "token": secret_value,
                "whitelist_chat_ids": [1],
                "admlist_chat_ids": [1],
            },
            "defaults": {
                "workdir": "/tmp",
                "openai_api_key": secret_value,
                "zai_api_key": secret_value,
                "github_token": secret_value,
                "tavily_api_key": secret_value,
                "jina_api_key": secret_value,
                "gemini_oauth_client_secret": secret_value,
            },
            "tools": {
                "codex": {
                    "mode": "headless",
                    "cmd": ["codex"],
                    "enabled": True,
                }
            },
            "mcp": {"port": 8790, "token": secret_value},
            "mcp_clients": [],
            "presets": [],
            "miniapp": {
                "enabled": True,
                "base_path": "/cli-proxy",
                "max_edit_file_size_kb": 512,
                "enable_delete": True,
            },
            "webhooks": {
                "secret_token": secret_value,
                "path": "/webhooks/telegram",
                "request_timeout_sec": 30,
                "max_payload_bytes": 1048576,
            },
        },
    }


def test_config_tab_secret_inputs_are_password_and_do_not_render_loaded_secret_values() -> None:
    result = _run_config_tab_harness(_config_view_with_redacted_secrets(secret_value="real-secret"))

    for input_id, path in SECRET_INPUT_PATHS.items():
        assert result["secretInputs"][input_id]["type"] == "password"
        assert result["secretInputs"][input_id]["value"] == ""
        assert result["secretInputs"][input_id]["secretPath"] == path
        assert result["secretInputs"][input_id]["clearHandler"] is True


def test_config_tab_unchanged_secret_sentinel_does_not_warn_or_overwrite() -> None:
    result = _run_config_tab_harness(
        _config_view_with_redacted_secrets(),
        trigger_save=True,
    )

    assert result["confirmMessages"] == ["Сохранить config.yaml и применить hot-reload?"]
    assert result["saveDraft"]["telegram"]["token"] == SECRET_UNCHANGED_SENTINEL
    assert result["saveDraft"]["defaults"]["openai_api_key"] == SECRET_UNCHANGED_SENTINEL


def test_config_tab_new_secret_value_warns_and_sends_new_secret() -> None:
    result = _run_config_tab_harness(
        _config_view_with_redacted_secrets(),
        trigger_save=True,
        input_values={"def-openai-api-key": "new-openai-key"},
    )

    assert len(result["confirmMessages"]) == 2
    assert "defaults.openai_api_key" in result["confirmMessages"][0]
    assert result["confirmMessages"][1] == "Сохранить config.yaml и применить hot-reload?"
    assert result["saveDraft"]["defaults"]["openai_api_key"] == "new-openai-key"


def test_config_tab_clear_secret_is_explicit_and_warns() -> None:
    result = _run_config_tab_harness(
        _config_view_with_redacted_secrets(),
        trigger_save=True,
        clear_secret_ids=["def-github-token"],
    )

    assert len(result["confirmMessages"]) == 2
    assert "defaults.github_token" in result["confirmMessages"][0]
    assert result["saveDraft"]["defaults"]["github_token"] is None


def test_config_tab_initial_tools_payload_stays_clean() -> None:
    result = _run_config_tab_harness(
        {
            "revision": "r1",
            "config": {
                "telegram": {"token": "t", "whitelist_chat_ids": [1], "admlist_chat_ids": [1]},
                "defaults": {"workdir": "/tmp"},
                "tools": {
                    "codex": {
                        "mode": "headless",
                        "cmd": ["codex"],
                        "enabled": True,
                    }
                },
                "mcp": {"port": 8790},
                "mcp_clients": [],
                "presets": [],
                "miniapp": {
                    "enabled": True,
                    "base_path": "/cli-proxy",
                    "max_edit_file_size_kb": 512,
                    "enable_delete": True,
                },
            },
        }
    )

    assert result["dirtyText"] == "Изменений нет"
    assert result["saveDisabled"] is True
    assert result["restartBannerHidden"] is True
    assert result["reloadResultHtml"] == ""


def test_config_tab_save_surfaces_restart_required_reload_hint() -> None:
    result = _run_config_tab_harness(
        {
            "revision": "r1",
            "config": {
                "telegram": {"token": "t", "whitelist_chat_ids": [1], "admlist_chat_ids": [1]},
                "defaults": {"workdir": "/tmp"},
                "tools": {
                    "codex": {
                        "mode": "headless",
                        "cmd": ["codex"],
                        "enabled": True,
                    }
                },
                "mcp": {"port": 8790},
                "mcp_clients": [],
                "presets": [],
                "miniapp": {
                    "enabled": True,
                    "base_path": "/cli-proxy",
                    "max_edit_file_size_kb": 512,
                    "enable_delete": True,
                },
            },
        },
        save_response={
            "ok": True,
            "revision": "r2",
            "diff": {"changed": [], "reloadable": [], "restart_required": []},
            "reload": {
                "status": "success_with_warnings",
                "applied": [],
                "restart_required": [
                    "defaults.run_metrics_enabled",
                    "defaults.skill_discovery_mode",
                ],
                "warnings": ["Some changes require process restart."],
            },
        },
        trigger_save=True,
    )

    assert result["restartBannerHidden"] is False
    assert "defaults.run_metrics_enabled" in result["restartBannerText"]
    assert "Требует перезапуска" in result["diffResultHtml"]
    assert "{" not in result["diffResultHtml"]
    assert "defaults.skill_discovery_mode" in result["reloadResultHtml"]
    assert "перезапустите процесс" in result["reloadResultHtml"]


def test_config_schema_exposes_runtime_sections_and_missing_fields() -> None:
    schema = config_schema()
    sections = schema["sections"]

    assert "user_modes" in sections["telegram"]["fields"]
    assert "direct_cli" in sections["telegram"]["fields"]["user_modes"]["description"]
    assert "orchestrator" in sections["telegram"]["fields"]["user_modes"]["description"]
    assert "desktop_state_path" in sections["defaults"]["fields"]
    assert "webmaster_use_cli_timeout_sec" in sections["defaults"]["fields"]
    assert "cli_json_stream_archive_enabled" in sections["defaults"]["fields"]
    assert "assistant_preview_enabled" in sections["defaults"]["fields"]
    assert "pending_input_confirmation_enabled" in sections["defaults"]["fields"]
    assert "memory_events_enabled" in sections["defaults"]["fields"]
    assert "memory_native_cli_hooks_enabled" in sections["defaults"]["fields"]
    assert "memory_outcomes_enabled" in sections["defaults"]["fields"]
    assert "memory_dreaming_enabled" in sections["defaults"]["fields"]
    assert "codebase_mapper_usage" in sections["defaults"]["fields"]
    assert "skill_registry_paths" in sections["defaults"]["fields"]
    assert "bind_host" in sections["miniapp"]["fields"]
    assert "bind_port" in sections["miniapp"]["fields"]
    assert "enabled" in sections["thread_mode"]["fields"]
    assert "path" in sections["webhooks"]["fields"]
    assert "timezone" in sections["scheduler"]["fields"]
    assert "rate_limits.default" in sections["security"]["fields"]
    assert "rate_limits.policies" in sections["security"]["fields"]
    assert "enabled" in sections["lint_evolution"]["fields"]
    assert "lock_ttl_minutes" in sections["lint_evolution"]["fields"]
    assert (
        sections["defaults"]["fields"]["pending_input_confirmation_enabled"]["description"]
        == (
            "Require explicit confirmation for every new incoming message before "
            "further processing. Busy-session queue choice is always confirmed "
            "separately."
        )
    )


def test_config_tab_source_covers_extended_config_surface() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    source = (repo_root / "miniapp/static/app.js").read_text(encoding="utf-8")

    required_tokens = [
        "tg-user-modes",
        (
            'fieldHtml({ id: "tg-user-modes", label: "user_modes", '
            'hint: "chat_id=all или chat_id=agent,analyst,direct_cli,orchestrator", kind: "textarea" })'
        ),
        "def-desktop-state-path",
        "def-webmaster-use-cli-timeout",
        "def-cli-json-stream-archive-enabled",
        "def-assistant-preview-enabled",
        "def-memory-events-enabled",
        "def-memory-native-cli-hooks-enabled",
        "def-memory-outcomes-enabled",
        "def-memory-dreaming-enabled",
        "def-memory-events-retention-days",
        "def-memory-events-max-payload-chars",
        "def-memory-events-redaction-enabled",
        "def-memory-dreaming-batch-size",
        (
            'fieldHtml({ id: "def-pending-input-confirmation-enabled", label: "pending_input_confirmation_enabled", '
            'kind: "checkbox", hint: "Сначала подтверждать любое новое сообщение; '
            'после подтверждения busy-сессия отдельно спрашивает о постановке в очередь" })'
        ),
        "def-codebase-mapper-usage",
        "def-run-artifacts-enabled",
        "def-skill-registry-paths",
        "mini-bind-host",
        "mini-bind-port",
        'fieldHtml({ id: "mini-max-size", label: "max_edit_file_size_kb", kind: "number", hint: "restart required" })',
        'key: "thread_mode"',
        'key: "webhooks"',
        'key: "scheduler"',
        'key: "security"',
        'key: "lint_evolution"',
        "sec-rate-default",
        "sec-rate-policies",
        "lint-evo-enabled",
        "lint-evo-lock-ttl",
        'fieldHtml({ id: "webhooks-enabled", label: "enabled", kind: "checkbox", hint: "restart required" })',
    ]

    for token in required_tokens:
        assert token in source

    assert "def-analyst-templates" not in source


def test_config_tab_source_marks_webhooks_enabled_restart_required() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    source = (repo_root / "miniapp/static/app.js").read_text(encoding="utf-8")

    assert (
        'fieldHtml({ id: "webhooks-enabled", label: "enabled", kind: "checkbox", hint: "restart required" })'
        in source
    )
    assert (
        'fieldHtml({ id: "mini-max-size", label: "max_edit_file_size_kb", kind: "number", hint: "restart required" })'
        in source
    )
