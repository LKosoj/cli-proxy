from __future__ import annotations

import asyncio
import difflib
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from modes.sdk.runtime.mcp.manager import MCPManager
from modes.sdk.runtime.json_normalizer import loads_safe
from agent.plugins.base import ToolPlugin
from modes.sdk.runtime.tooling.loader import PluginLoader
from modes.sdk.runtime.tooling.mcp_plugin import MCPRemoteToolPlugin
from modes.sdk.runtime.tooling.spec import ToolSpec
from modes.sdk.runtime.tooling.constants import TOOL_TIMEOUT_MS


class ToolRegistry:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.plugins: Dict[str, ToolPlugin] = {}
        self.plugin_instances: Dict[str, ToolPlugin] = {}
        self.specs: Dict[str, ToolSpec] = {}

        self._mcp_manager = MCPManager(config)
        # Импорт отложен: content_screening_service тянет modes.sdk.runtime.*, а этот модуль
        # сам находится внутри modes.sdk.runtime — импорт на уровне модуля замыкает цикл.
        from app.services.content_screening_service import build_content_screening_service

        self._content_screening = build_content_screening_service(config)
        self._mcp_loaded = False
        self._mcp_lock = asyncio.Lock()
        self._mcp_tool_keys: set[str] = set()

        # shared state stores
        self.pending_questions: Dict[str, asyncio.Future] = {}
        self.recent_messages: Dict[int, List[int]] = {}
        self.task_store: Dict[str, List[Dict[str, Any]]] = {}
        self.scheduler_tasks: Dict[str, Dict[str, Any]] = {}
        self.user_tasks: Dict[int, set] = {}

        self._load_plugins()
        # Register cached MCP tools (if any) so they can appear immediately in the tool list.
        self._register_mcp_cached_tools()

    def _load_plugins(self) -> None:
        # Resolve project-local plugin directory without importing `agent` package
        # to avoid import cycles during runtime/tooling initialization.
        plugins_dir = Path(__file__).resolve().parents[4] / "agent" / "plugins"
        loader = PluginLoader(plugins_dir)
        loaded = loader.load()
        for plugin in loaded:
            try:
                self.register(plugin)
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                continue

    def _unique_tool_name(self, base: str) -> str:
        if base not in self.specs:
            return base
        i = 2
        while True:
            name = f"{base}_{i}"
            if name not in self.specs:
                return name
            i += 1

    def _register_mcp_cached_tools(self) -> None:
        try:
            cached = self._mcp_manager.load_cached_tools()
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return
        for server_name, tool in cached:
            try:
                key = f"{server_name}::{tool.name}"
                if key in self._mcp_tool_keys:
                    continue
                base_name = self._mcp_manager.build_registry_name(server_name, tool.name)
                name = self._unique_tool_name(base_name)
                plugin = MCPRemoteToolPlugin(
                    registry_name=name,
                    server_name=server_name,
                    tool=tool,
                    manager=self._mcp_manager,
                )
                self.register(plugin)
                self._mcp_tool_keys.add(key)
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                continue

    async def ensure_mcp_loaded(self) -> None:
        # If no MCP client config, nothing to do.
        if not getattr(self.config, "mcp_clients", None):
            return
        if self._mcp_loaded:
            return
        async with self._mcp_lock:
            if self._mcp_loaded:
                return
            discovered = await self._mcp_manager.list_all_tools()
            for server_name, tool in discovered:
                key = f"{server_name}::{tool.name}"
                if key in self._mcp_tool_keys:
                    continue
                base_name = self._mcp_manager.build_registry_name(server_name, tool.name)
                name = self._unique_tool_name(base_name)
                try:
                    plugin = MCPRemoteToolPlugin(
                        registry_name=name,
                        server_name=server_name,
                        tool=tool,
                        manager=self._mcp_manager,
                    )
                    self.register(plugin)
                    self._mcp_tool_keys.add(key)
                except Exception as e:
                    logging.exception(f"tool failed {str(e)}")
                    continue
            try:
                self._mcp_manager.save_cached_tools(discovered)
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
            self._mcp_loaded = True

    def _build_services(self) -> Dict[str, Any]:
        return {
            "config": self.config,
            "pending_questions": self.pending_questions,
            "recent_messages": self.recent_messages,
            "task_store": self.task_store,
            "scheduler_tasks": self.scheduler_tasks,
            "user_tasks": self.user_tasks,
            "_tool_registry": self,
        }

    def register(self, plugin: ToolPlugin) -> None:
        services = self._build_services()
        plugin.initialize(config=self.config, services=services)
        spec = plugin.get_spec()
        if not spec or not spec.name:
            raise ValueError("Plugin spec missing name")
        if spec.parameters is not None and not isinstance(spec.parameters, dict):
            raise ValueError(f"Invalid parameters schema for {spec.name}: must be dict")
        if isinstance(spec.parameters, dict) and spec.parameters.get("type") and spec.parameters.get("type") != "object":
            raise ValueError(f"Invalid parameters schema for {spec.name}: type must be object")
        name = self._normalize_spec_name(spec, plugin)
        spec.name = name
        if name in self.specs:
            raise ValueError(f"Duplicate tool name: {name}")
        self.plugins[name] = plugin
        self.specs[name] = spec

    def _normalize_spec_name(self, spec: ToolSpec, plugin: ToolPlugin) -> str:
        name = spec.name
        prefix = plugin.get_function_prefix() if hasattr(plugin, "get_function_prefix") else None
        if prefix and "." not in name:
            return f"{prefix}.{name}"
        return name

    def list_tool_names(self) -> List[str]:
        return sorted(self.specs.keys())

    def get_definitions(self, allowed_tools: Optional[List[str]] = None, model_family: str = "openai") -> List[Dict[str, Any]]:
        names = self._filter_allowed(allowed_tools)
        specs = [self.specs[n] for n in names]
        if model_family == "google":
            return [{"function_declarations": [s.to_google_tool() for s in specs]}]
        return [s.to_openai_tool() for s in specs]

    def get_summary_definitions(self, allowed_tools: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Return compact tool schemas (name + one_liner, no parameters) for progressive disclosure."""
        names = self._filter_allowed(allowed_tools)
        return [self.specs[n].to_openai_summary() for n in names]

    def get_tool_detail(self, tool_name: str) -> Optional[Dict[str, Any]]:
        """Return full schema for a single tool by name."""
        spec = self.specs.get(tool_name)
        if not spec:
            return None
        return spec.to_openai_tool()

    async def get_definitions_async(
        self, allowed_tools: Optional[List[str]] = None, model_family: str = "openai"
    ) -> List[Dict[str, Any]]:
        await self.ensure_mcp_loaded()
        return self.get_definitions(allowed_tools, model_family=model_family)

    def any_awaiting_input(self, chat_id: int) -> bool:
        """Return True if any plugin is currently waiting for free-text input from the user."""
        for plugin in self.plugins.values():
            try:
                if plugin.awaiting_input(chat_id):
                    return True
            except Exception:
                continue
        return False

    def cancel_all_inputs(self, chat_id: int) -> int:
        """Cancel pending input dialogs in all plugins. Returns number of cancelled dialogs."""
        cancelled = 0
        for plugin in self.plugins.values():
            try:
                if plugin.cancel_input(chat_id):
                    cancelled += 1
            except Exception:
                continue
        return cancelled

    def get_message_handlers(self, allowed_tools: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        names = self._filter_allowed(allowed_tools)
        handlers: List[Dict[str, Any]] = []
        for name in names:
            plugin = self.plugins.get(name)
            if not plugin:
                continue
            try:
                for item in plugin.get_message_handlers() or []:
                    normalized = self._validate_and_normalize_handler(item, plugin.get_plugin_id(), kind="message")
                    if normalized:
                        handlers.append(normalized)
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                raise
        return handlers

    def get_inline_handlers(self, allowed_tools: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        names = self._filter_allowed(allowed_tools)
        handlers: List[Dict[str, Any]] = []
        for name in names:
            plugin = self.plugins.get(name)
            if not plugin:
                continue
            try:
                for item in plugin.get_inline_handlers() or []:
                    normalized = self._validate_and_normalize_handler(item, plugin.get_plugin_id(), kind="inline")
                    if normalized:
                        handlers.append(normalized)
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                raise
        return handlers

    def build_bot_ui(self, allowed_tools: Optional[List[str]] = None) -> Dict[str, Any]:
        names = self._filter_allowed(allowed_tools)
        # Two-level plugin menu: collect plugins that declare get_menu_label/get_menu_actions.
        plugin_menu: List[Dict[str, Any]] = []
        seen_pids: set = set()
        for name in names:
            plugin = self.plugins.get(name)
            if not plugin:
                continue
            pid = plugin.get_plugin_id()
            if pid in seen_pids:
                continue
            label = plugin.get_menu_label()
            actions = plugin.get_menu_actions()
            if label and actions:
                seen_pids.add(pid)
                plugin_menu.append({"plugin_id": pid, "label": label, "actions": actions, "plugin": plugin})

        return {
            "plugin_menu": plugin_menu,
            "message_handlers": self.get_message_handlers(allowed_tools),
            "inline_handlers": self.get_inline_handlers(allowed_tools),
        }

    def _validate_and_normalize_handler(self, item: Any, plugin_name: str, kind: str) -> Optional[Dict[str, Any]]:
        if not isinstance(item, dict):
            logging.warning("tool registry: invalid %s handler definition from plugin %r: not a dict", kind, plugin_name)
            return None
        handler = item.get("handler")
        if handler is None:
            logging.warning("tool registry: invalid %s handler definition from plugin %r: missing handler", kind, plugin_name)
            return None
        normalized = dict(item)
        normalized.setdefault("handler_kwargs", {})
        normalized["plugin_name"] = plugin_name
        return normalized

    def _filter_allowed(self, allowed_tools: Optional[List[str]]) -> List[str]:
        if allowed_tools is None or allowed_tools == [] or allowed_tools == ["All"]:
            return list(self.specs.keys())
        if allowed_tools == ["None"]:
            return []
        missing = [p for p in allowed_tools if p not in self.specs]
        if missing:
            raise ValueError(f"Allowed tools not found: {missing}")
        return [p for p in allowed_tools if p in self.specs]

    def _validate_args(self, spec: ToolSpec, args: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        schema = spec.parameters or {}
        if schema.get("type") and schema.get("type") != "object":
            return ["parameters schema must be object"]
        required = schema.get("required") or []
        props = schema.get("properties") or {}
        for r in required:
            if r not in args:
                errors.append(f"missing required: {r}")
        for key, value in args.items():
            prop = props.get(key) or {}
            ptype = prop.get("type")
            if ptype:
                if not self._check_type(ptype, value):
                    errors.append(f"invalid type for {key}: expected {ptype}")
            enum = prop.get("enum")
            if enum and value not in enum:
                errors.append(f"invalid value for {key}: expected one of {enum}")
        return errors

    def _coerce_args(self, spec: ToolSpec, args: Dict[str, Any]) -> Dict[str, Any]:
        schema = spec.parameters or {}
        props = schema.get("properties") or {}
        if not isinstance(props, dict) or not args:
            return args

        coerced = dict(args)
        for key, value in args.items():
            prop = props.get(key) or {}
            ptype = prop.get("type")
            if ptype == "integer":
                coerced[key] = self._coerce_integer(value)
            elif ptype == "number":
                coerced[key] = self._coerce_number(value)
        return coerced

    def _coerce_integer(self, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        raw = value.strip()
        if not raw or not re.fullmatch(r"[+-]?\d+", raw):
            return value
        try:
            return int(raw)
        except Exception:
            return value

    def _coerce_number(self, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        raw = value.strip()
        if not raw:
            return value
        if re.fullmatch(r"[+-]?\d+", raw):
            try:
                return int(raw)
            except Exception:
                return value
        if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", raw):
            return value
        try:
            return float(raw)
        except Exception:
            return value

    def _check_type(self, ptype: str, value: Any) -> bool:
        if ptype == "string":
            return isinstance(value, str)
        if ptype == "number":
            return isinstance(value, (int, float))
        if ptype == "integer":
            return isinstance(value, int)
        if ptype == "boolean":
            return isinstance(value, bool)
        if ptype == "array":
            return isinstance(value, list)
        if ptype == "object":
            return isinstance(value, dict)
        return True

    async def execute(self, name: str, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        spec = self.specs.get(name)
        plugin = self.plugins.get(name)
        if not spec or not plugin:
            return {"success": False, "error": f"Unknown tool: {name}"}
        ctx = ctx or {}
        allowed_tools = ctx.get("allowed_tools")
        if allowed_tools and allowed_tools != ["All"] and name not in allowed_tools:
            return {"success": False, "error": f"Tool not allowed: {name}"}
        normalized_args: Dict[str, Any]
        if args is None:
            normalized_args = {}
        elif isinstance(args, dict):
            normalized_args = args
        elif isinstance(args, str):
            raw = args.strip()
            if not raw:
                normalized_args = {}
            else:
                try:
                    parsed = loads_safe(raw, strict_first=False)
                except Exception as e:
                    logging.getLogger(__name__).exception("tool args parse failed name=%s", name)
                    return {"success": False, "error": f"Invalid args for {name}: expected object, got string ({e})"}
                if not isinstance(parsed, dict):
                    return {
                        "success": False,
                        "error": f"Invalid args for {name}: expected object, got {type(parsed).__name__}",
                    }
                normalized_args = parsed
        else:
            return {
                "success": False,
                "error": f"Invalid args for {name}: expected object, got {type(args).__name__}",
            }

        normalized_args = self._coerce_args(spec, normalized_args)
        errors = self._validate_args(spec, normalized_args)
        if errors:
            return {"success": False, "error": f"Invalid args for {name}: {errors}"}
        override_timeout_ms = None
        tool_timeouts = ctx.get("tool_timeouts_ms")
        if isinstance(tool_timeouts, dict):
            raw_timeout = tool_timeouts.get(name)
            parsed_timeout = None
            if isinstance(raw_timeout, bool):
                parsed_timeout = None
            elif isinstance(raw_timeout, (int, float)):
                parsed_timeout = int(raw_timeout)
            elif isinstance(raw_timeout, str):
                value = raw_timeout.strip()
                if value.isdigit():
                    parsed_timeout = int(value)
            if parsed_timeout and parsed_timeout > 0:
                override_timeout_ms = parsed_timeout
        timeout_ms = int(override_timeout_ms or spec.timeout_ms or TOOL_TIMEOUT_MS)
        timeout = timeout_ms / 1000
        try:
            result = await asyncio.wait_for(plugin.execute(normalized_args, ctx), timeout=timeout)
        except asyncio.TimeoutError:
            return {"success": False, "error": f"⏱️ Tool {name} timed out after {int(timeout)}s"}
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": str(e)}
        if self._content_screening is not None and spec.returns_external_content:
            result = await self._apply_content_screening(name, result)
        return result

    async def _apply_content_screening(self, name: str, result: Dict[str, Any]) -> Dict[str, Any]:
        """Прогнать результат внешнего инструмента через классификатор prompt injection.

        Баг в этой логике не должен ронять сам инструмент: любая ошибка здесь
        логируется и возвращается исходный (нескринённый) результат.
        """
        try:
            if not isinstance(result, dict) or not result.get("success"):
                return result
            output = result.get("output")
            if not isinstance(output, str) or not output.strip():
                return result
            verdict = await self._content_screening.screen(name, output)
            if verdict.decision == "auto":
                return result
            new_result = dict(result)
            new_result["output"] = self._content_screening.apply(verdict, output)
            await self._content_screening.audit(name, verdict)
            return new_result
        except Exception:
            logging.exception(f"content screening failed for tool {name}")
            return result

    async def execute_many(self, calls: List[Dict[str, Any]], ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
        # determine parallelizable
        parallel = True
        for call in calls:
            name = call.get("name") or call.get("tool")
            if not name:
                parallel = False
                break
            spec = self.specs.get(name)
            if not spec or not spec.parallelizable:
                parallel = False
                break
        if not parallel:
            results = []
            for call in calls:
                name = call.get("name") or call.get("tool")
                args = call.get("args") or call.get("arguments") or {}
                results.append(await self.execute(name, args, ctx))
            return results
        tasks = []
        for call in calls:
            name = call.get("name") or call.get("tool")
            args = call.get("args") or call.get("arguments") or {}
            tasks.append(self.execute(name, args, ctx))
        return await asyncio.gather(*tasks)

    def record_message(self, chat_id: int, message_id: int) -> None:
        if not chat_id or not message_id:
            return
        items = self.recent_messages.setdefault(chat_id, [])
        items.append(message_id)
        if len(items) > 20:
            del items[:-20]

    def resolve_question(self, question_id: str, answer: str) -> bool:
        fut = self.pending_questions.get(question_id)
        if not fut or fut.done():
            return False
        fut.set_result(answer)
        self.pending_questions.pop(question_id, None)
        return True

    def close_all(self) -> None:
        for plugin in self.plugins.values():
            try:
                plugin.close()
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                continue

    async def close_mcp(self) -> None:
        """Остановить все MCP-клиенты через менеджер."""
        await self._mcp_manager.close_all()

    def get_missing_suggestions(self, name: str) -> List[str]:
        candidates = list(self.specs.keys())
        return difflib.get_close_matches(name, candidates, n=3, cutoff=0.6)


_REGISTRY_SINGLETON: Optional[ToolRegistry] = None


def get_tool_registry(config: Any) -> ToolRegistry:
    """
    Process-wide singleton ToolRegistry.
    Avoids re-loading plugins multiple times and keeps shared tool state consistent.
    """
    global _REGISTRY_SINGLETON
    if _REGISTRY_SINGLETON is None:
        _REGISTRY_SINGLETON = ToolRegistry(config)
    return _REGISTRY_SINGLETON
