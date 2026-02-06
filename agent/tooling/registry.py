from __future__ import annotations

import asyncio
import difflib
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.mcp.manager import MCPManager
from agent.plugins.base import ToolPlugin
from agent.tooling.loader import PluginLoader
from agent.tooling.mcp_plugin import MCPRemoteToolPlugin
from agent.tooling.spec import ToolSpec
from agent.tooling.constants import TOOL_TIMEOUT_MS


class ToolRegistry:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.plugins: Dict[str, ToolPlugin] = {}
        self.plugin_instances: Dict[str, ToolPlugin] = {}
        self.specs: Dict[str, ToolSpec] = {}

        self._mcp_manager = MCPManager(config)
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
        loader = PluginLoader(Path(__file__).resolve().parent.parent / "plugins")
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

    async def get_definitions_async(
        self, allowed_tools: Optional[List[str]] = None, model_family: str = "openai"
    ) -> List[Dict[str, Any]]:
        await self.ensure_mcp_loaded()
        return self.get_definitions(allowed_tools, model_family=model_family)

    def get_plugin_commands(self, allowed_tools: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        names = self._filter_allowed(allowed_tools)
        commands: List[Dict[str, Any]] = []
        seen = set()
        for name in names:
            plugin = self.plugins.get(name)
            if not plugin:
                continue
            try:
                for cmd in plugin.get_commands() or []:
                    normalized = self._validate_and_normalize_command(cmd, plugin.get_plugin_id())
                    if not normalized:
                        continue
                    key = normalized.get("command")
                    if key:
                        if key in seen:
                            raise ValueError(f"Duplicate plugin command '{key}'")
                        seen.add(key)
                    commands.append(normalized)
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                raise
        return commands

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

    def build_bot_commands(self, allowed_tools: Optional[List[str]] = None) -> Dict[str, Any]:
        names = self._filter_allowed(allowed_tools)
        plugin_commands = self.get_plugin_commands(allowed_tools)
        menu_entries: List[Dict[str, Any]] = []
        for cmd in plugin_commands:
            if cmd.get("add_to_menu") and cmd.get("command") and cmd.get("description"):
                menu_entries.append({"command": cmd["command"], "description": cmd["description"], "plugin_name": cmd.get("plugin_name")})

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

        return {"plugin_commands": plugin_commands, "menu_entries": menu_entries, "plugin_menu": plugin_menu}

    def build_bot_ui(self, allowed_tools: Optional[List[str]] = None) -> Dict[str, Any]:
        build = self.build_bot_commands(allowed_tools)
        return {
            **build,
            "message_handlers": self.get_message_handlers(allowed_tools),
            "inline_handlers": self.get_inline_handlers(allowed_tools),
        }

    def _validate_and_normalize_command(self, cmd: Dict[str, Any], plugin_name: str) -> Optional[Dict[str, Any]]:
        if not isinstance(cmd, dict):
            logging.exception("tool failed Invalid command definition: not a dict")
            return None
        # Callback query handler entry (Telegram UI hook).
        if cmd.get("callback_query_handler") and cmd.get("callback_pattern"):
            handler = cmd.get("callback_query_handler")
            pattern = cmd.get("callback_pattern")
            if not callable(handler):
                logging.exception(f"tool failed Invalid callback_query_handler from {plugin_name}")
                return None
            if not isinstance(pattern, str) or not pattern:
                logging.exception(f"tool failed Invalid callback_pattern from {plugin_name}: {pattern}")
                return None
            normalized = dict(cmd)
            normalized.setdefault("handler_kwargs", {})
            normalized.setdefault("add_to_menu", False)
            normalized["plugin_name"] = plugin_name
            return normalized
        command = cmd.get("command")
        description = cmd.get("description")
        handler = cmd.get("handler")
        if not command or not description or not handler:
            logging.exception(f"tool failed Invalid command definition from {plugin_name}: {cmd}")
            return None
        if isinstance(command, str) and command.startswith("/"):
            command = command[1:]
        if not isinstance(command, str) or " " in command:
            logging.exception(f"tool failed Invalid command name from {plugin_name}: {command}")
            return None
        if not callable(handler):
            logging.exception(f"tool failed Invalid handler for command '{command}' from {plugin_name}")
            return None
        normalized = dict(cmd)
        normalized["command"] = command
        normalized.setdefault("handler_kwargs", {})
        normalized.setdefault("add_to_menu", True)
        normalized["plugin_name"] = plugin_name
        return normalized

    def _validate_and_normalize_handler(self, item: Any, plugin_name: str, kind: str) -> Optional[Dict[str, Any]]:
        if not isinstance(item, dict):
            logging.exception(f"tool failed Invalid {kind} handler definition from {plugin_name}: not a dict")
            return None
        handler = item.get("handler")
        if handler is None:
            logging.exception(f"tool failed Invalid {kind} handler definition from {plugin_name}: missing handler")
            return None
        normalized = dict(item)
        normalized.setdefault("handler_kwargs", {})
        normalized["plugin_name"] = plugin_name
        return normalized

    def _filter_allowed(self, allowed_tools: Optional[List[str]]) -> List[str]:
        if not allowed_tools or allowed_tools == ["None"]:
            return []
        if allowed_tools == ["All"]:
            return list(self.specs.keys())
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
        allowed_tools = ctx.get("allowed_tools")
        if allowed_tools and allowed_tools != ["All"] and name not in allowed_tools:
            return {"success": False, "error": f"Tool not allowed: {name}"}
        errors = self._validate_args(spec, args or {})
        if errors:
            return {"success": False, "error": f"Invalid args for {name}: {errors}"}
        timeout = int(spec.timeout_ms or TOOL_TIMEOUT_MS) / 1000
        try:
            return await asyncio.wait_for(plugin.execute(args or {}, ctx), timeout=timeout)
        except asyncio.TimeoutError:
            return {"success": False, "error": f"⏱️ Tool {name} timed out after {int(timeout)}s"}
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": str(e)}

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
