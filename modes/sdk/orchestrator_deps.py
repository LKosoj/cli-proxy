from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class OrchestratorDeps:
    ExecutorRequest: Any
    ExecutorResponse: Any
    PlanStep: Any
    validate_response: Callable[[Any], None]
    Dispatcher: Any
    Executor: Any
    get_tool_registry: Callable[[Any], Any]
    read_json_locked: Callable[..., Any]
    update_json_locked: Callable[..., Any]
    compress_memory: Callable[..., Any]
    decide_memory_save: Callable[..., Any]
    append_memory_structured: Callable[..., Any]
    compact_memory_by_priority: Callable[..., Any]
    memory_size_bytes: Callable[..., int]
    read_memory: Callable[..., str]
    remove_expired_entries: Callable[..., str]
    trim_for_context: Callable[..., str]
    write_memory: Callable[..., None]
    format_retrieved_context: Callable[..., str]
    retrieve_relevant_context: Callable[..., Any]
    plan_steps: Callable[..., Any]
    chat_completion: Callable[..., Any]


def load_default_deps() -> OrchestratorDeps:
    contracts = importlib.import_module("modes.sdk.runtime.contracts")
    dispatcher_mod = importlib.import_module("modes.sdk.runtime.dispatcher")
    executor_mod = importlib.import_module("modes.sdk.runtime.executor")
    registry_mod = importlib.import_module("modes.sdk.runtime.tooling.registry")
    store_mod = importlib.import_module("modes.sdk.json_store")
    memory_policy_mod = importlib.import_module("modes.sdk.runtime.memory_policy")
    memory_store_mod = importlib.import_module("modes.sdk.runtime.memory_store")
    memory_retrieval_mod = importlib.import_module("modes.sdk.runtime.memory_retrieval")
    planner_mod = importlib.import_module("modes.sdk.runtime.planner")
    openai_mod = importlib.import_module("modes.sdk.runtime.openai_client")

    return OrchestratorDeps(
        ExecutorRequest=getattr(contracts, "ExecutorRequest"),
        ExecutorResponse=getattr(contracts, "ExecutorResponse"),
        PlanStep=getattr(contracts, "PlanStep"),
        validate_response=getattr(contracts, "validate_response"),
        Dispatcher=getattr(dispatcher_mod, "Dispatcher"),
        Executor=getattr(executor_mod, "Executor"),
        get_tool_registry=getattr(registry_mod, "get_tool_registry"),
        read_json_locked=getattr(store_mod, "read_json_locked"),
        update_json_locked=getattr(store_mod, "update_json_locked"),
        compress_memory=getattr(memory_policy_mod, "compress_memory"),
        decide_memory_save=getattr(memory_policy_mod, "decide_memory_save"),
        append_memory_structured=getattr(memory_store_mod, "append_memory_structured"),
        compact_memory_by_priority=getattr(memory_store_mod, "compact_memory_by_priority"),
        memory_size_bytes=getattr(memory_store_mod, "memory_size_bytes"),
        read_memory=getattr(memory_store_mod, "read_memory"),
        remove_expired_entries=getattr(memory_store_mod, "remove_expired_entries"),
        trim_for_context=getattr(memory_store_mod, "trim_for_context"),
        write_memory=getattr(memory_store_mod, "write_memory"),
        format_retrieved_context=getattr(memory_retrieval_mod, "format_retrieved_context"),
        retrieve_relevant_context=getattr(memory_retrieval_mod, "retrieve_relevant_context"),
        plan_steps=getattr(planner_mod, "plan_steps"),
        chat_completion=getattr(openai_mod, "chat_completion"),
    )
