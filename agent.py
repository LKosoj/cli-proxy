import asyncio
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from config import AppConfig
from utils import is_within_root, strip_ansi


def _get_openai_config(config: Optional[AppConfig] = None) -> Optional[Tuple[str, str, str]]:
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL")
    base_url = os.getenv("OPENAI_BASE_URL")
    if config:
        api_key = api_key or config.defaults.openai_api_key
        model = model or config.defaults.openai_model
        base_url = base_url or config.defaults.openai_base_url
    if not base_url:
        base_url = "https://api.openai.com"
    if not api_key or not model:
        return None
    return api_key, model, base_url.rstrip("/")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _compact_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]..."


class AgentRunner:
    def __init__(self, config: AppConfig):
        self.config = config
        self._cfg = _get_openai_config(config)

    async def run(
        self,
        session: Any,
        user_text: str,
        bot: Any,
        context: Any,
        dest: Dict[str, Any],
    ) -> str:
        if not self._cfg:
            return "Агент не настроен: отсутствуют OPENAI_API_KEY/OPENAI_MODEL."
        chat_id = dest.get("chat_id")
        messages: List[Dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "Ты — агент-исполнитель внутри Telegram-бота. "
                    "Твоя задача: решать задачи пользователя, "
                    "используя доступные инструменты. "
                    "Когда нужна работа через CLI-инструменты, используй tool run_command. "
                    "Если можно решить без CLI — отвечай напрямую. "
                    "Если нужен файл — используй tool send_file. "
                    "Результаты инструментов могут быть JSON-строками; "
                    "если есть file_path — можешь отправить файл пользователю. "
                    "Отвечай на русском, по делу, без лишнего."
                ),
            },
            {"role": "user", "content": user_text},
        ]
        tools = self._tools_schema()
        max_steps = 8
        for _ in range(max_steps):
            msg = await asyncio.to_thread(self._call_openai, messages, tools)
            tool_calls = msg.get("tool_calls") or []
            content = (msg.get("content") or "").strip()
            if not tool_calls:
                return content or "Готово."
            messages.append(
                {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls,
                }
            )
            for call in tool_calls:
                tool_name = call.get("function", {}).get("name")
                raw_args = call.get("function", {}).get("arguments") or "{}"
                try:
                    args = json.loads(raw_args)
                except Exception:
                    args = {}
                result = await self._run_tool(
                    tool_name,
                    args,
                    session=session,
                    bot=bot,
                    context=context,
                    chat_id=chat_id,
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "content": result,
                    }
                )
        return "Не успел завершить задачу за ограниченное число шагов."

    def _call_openai(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        api_key, model, base_url = self._cfg  # type: ignore[misc]
        payload = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0.2,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        resp = requests.post(
            f"{base_url}/v1/chat/completions",
            json=payload,
            headers=headers,
            timeout=90,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]

    async def _run_tool(
        self,
        name: Optional[str],
        args: Dict[str, Any],
        session: Any,
        bot: Any,
        context: Any,
        chat_id: Optional[int],
    ) -> str:
        if not name:
            return "Ошибка: инструмент не указан."
        handler = getattr(self, f"_tool_{name}", None)
        if not handler:
            return f"Ошибка: неизвестный инструмент {name}."
        try:
            return await handler(args, session=session, bot=bot, context=context, chat_id=chat_id)
        except Exception as e:
            return f"Ошибка инструмента {name}: {e}"

    def _tools_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "run_command",
                    "description": "Запустить команду/запрос в CLI текущей сессии.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string"},
                        },
                        "required": ["prompt"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Прочитать файл в рабочей директории.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "Записать файл в рабочей директории.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                            "append": {"type": "boolean"},
                        },
                        "required": ["path", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "edit_file",
                    "description": "Найти и заменить текст в файле.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "find": {"type": "string"},
                            "replace": {"type": "string"},
                            "count": {"type": "integer"},
                        },
                        "required": ["path", "find", "replace"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "delete_file",
                    "description": "Удалить файл в рабочей директории.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_directory",
                    "description": "Показать содержимое директории.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_files",
                    "description": "Поиск файлов по подстроке в имени.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pattern": {"type": "string"},
                            "path": {"type": "string"},
                            "max_results": {"type": "integer"},
                        },
                        "required": ["pattern"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_text",
                    "description": "Поиск текста в файлах.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "path": {"type": "string"},
                            "max_results": {"type": "integer"},
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_web",
                    "description": "Поиск в интернете (DuckDuckGo HTML).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "fetch_page",
                    "description": "Скачать страницу по URL и вернуть текст.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string"},
                        },
                        "required": ["url"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "send_file",
                    "description": "Отправить файл пользователю.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "message",
                    "description": "Отправить сообщение пользователю.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                        },
                        "required": ["text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_meme",
                    "description": "Получить случайный мем (текст).",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "schedule_task",
                    "description": "Запланировать сообщение пользователю через delay_sec.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "delay_sec": {"type": "integer"},
                        },
                        "required": ["text", "delay_sec"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "memory",
                    "description": "Управление памятью агента (set/get/delete/list/clear).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "op": {"type": "string"},
                            "key": {"type": "string"},
                            "value": {},
                        },
                        "required": ["op"],
                    },
                },
            },
        ]

    async def _tool_run_command(self, args: Dict[str, Any], session: Any, **_: Any) -> str:
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return "Ошибка: пустой prompt."
        output = await session.run_prompt(prompt)
        clean = strip_ansi(output)
        payload = {"chars": len(clean), "truncated": False, "output": clean}
        if len(clean) > 12000:
            path = self._write_temp(session.workdir, clean, prefix="agent_cli_output", suffix=".txt")
            payload["truncated"] = True
            payload["file_path"] = path
            payload["output"] = clean[:12000] + "\n...[truncated]..."
        return _json_dumps(payload)

    async def _tool_read_file(self, args: Dict[str, Any], session: Any, **_: Any) -> str:
        path = self._resolve_path(session.workdir, args.get("path"))
        if not path:
            return "Ошибка: путь вне рабочей директории."
        if not os.path.exists(path):
            return "Файл не найден."
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        payload = {"chars": len(text), "truncated": False, "content": text}
        if len(text) > 12000:
            payload["truncated"] = True
            payload["content"] = text[:12000] + "\n...[truncated]..."
        return _json_dumps(payload)

    async def _tool_write_file(self, args: Dict[str, Any], session: Any, **_: Any) -> str:
        path = self._resolve_path(session.workdir, args.get("path"))
        if not path:
            return "Ошибка: путь вне рабочей директории."
        content = args.get("content") or ""
        append = bool(args.get("append"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = "a" if append else "w"
        with open(path, mode, encoding="utf-8") as f:
            f.write(content)
        return f"Записано в {path}."

    async def _tool_edit_file(self, args: Dict[str, Any], session: Any, **_: Any) -> str:
        path = self._resolve_path(session.workdir, args.get("path"))
        if not path:
            return "Ошибка: путь вне рабочей директории."
        if not os.path.exists(path):
            return "Файл не найден."
        find = args.get("find")
        replace = args.get("replace")
        if find is None or replace is None:
            return "Ошибка: find/replace обязательны."
        count = args.get("count")
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        if count is None:
            new_text = text.replace(find, replace)
            changed = text.count(find)
        else:
            new_text = text.replace(find, replace, int(count))
            changed = min(text.count(find), int(count))
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_text)
        return f"Готово. Замен: {changed}."

    async def _tool_delete_file(self, args: Dict[str, Any], session: Any, **_: Any) -> str:
        path = self._resolve_path(session.workdir, args.get("path"))
        if not path:
            return "Ошибка: путь вне рабочей директории."
        if not os.path.exists(path):
            return "Файл не найден."
        os.remove(path)
        return f"Удалено: {path}"

    async def _tool_list_directory(self, args: Dict[str, Any], session: Any, **_: Any) -> str:
        base = self._resolve_path(session.workdir, args.get("path") or session.workdir)
        if not base:
            return "Ошибка: путь вне рабочей директории."
        if not os.path.isdir(base):
            return "Каталог не найден."
        entries = []
        for name in sorted(os.listdir(base)):
            path = os.path.join(base, name)
            try:
                is_dir = os.path.isdir(path)
                size = os.path.getsize(path) if not is_dir else None
                entries.append({"name": name, "path": path, "is_dir": is_dir, "size": size})
            except Exception:
                continue
        return _json_dumps({"path": base, "entries": entries})

    async def _tool_search_files(self, args: Dict[str, Any], session: Any, **_: Any) -> str:
        pattern = (args.get("pattern") or "").strip()
        if not pattern:
            return "Ошибка: pattern пустой."
        base = self._resolve_path(session.workdir, args.get("path") or session.workdir)
        if not base:
            return "Ошибка: путь вне рабочей директории."
        max_results = int(args.get("max_results") or 50)
        results = []
        for root, _, files in os.walk(base):
            for name in files:
                if pattern.lower() in name.lower():
                    results.append(os.path.join(root, name))
                    if len(results) >= max_results:
                        return _json_dumps({"truncated": True, "results": results})
        return _json_dumps({"truncated": False, "results": results})

    async def _tool_search_text(self, args: Dict[str, Any], session: Any, **_: Any) -> str:
        query = (args.get("query") or "").strip()
        if not query:
            return "Ошибка: query пустой."
        base = self._resolve_path(session.workdir, args.get("path") or session.workdir)
        if not base:
            return "Ошибка: путь вне рабочей директории."
        max_results = int(args.get("max_results") or 50)
        results = []
        for root, _, files in os.walk(base):
            for name in files:
                path = os.path.join(root, name)
                try:
                    if os.path.getsize(path) > 2 * 1024 * 1024:
                        continue
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        for idx, line in enumerate(f, start=1):
                            if query.lower() in line.lower():
                                results.append({"path": path, "line": idx, "text": line.strip()})
                                if len(results) >= max_results:
                                    return _json_dumps({"truncated": True, "results": results})
                except Exception:
                    continue
        return _json_dumps({"truncated": False, "results": results})

    async def _tool_search_web(self, args: Dict[str, Any], **_: Any) -> str:
        query = (args.get("query") or "").strip()
        if not query:
            return "Ошибка: query пустой."
        url = "https://duckduckgo.com/html/"
        resp = requests.get(url, params={"q": query}, timeout=15)
        resp.raise_for_status()
        html = resp.text
        results = []
        for match in re.finditer(r'class="result__a" href="([^"]+)"[^>]*>(.*?)</a>', html):
            href = match.group(1)
            title = re.sub(r"<.*?>", "", match.group(2))
            results.append({"title": title, "url": href})
            if len(results) >= 5:
                break
        return _json_dumps({"query": query, "results": results})

    async def _tool_fetch_page(self, args: Dict[str, Any], **_: Any) -> str:
        url = (args.get("url") or "").strip()
        if not url:
            return "Ошибка: url пустой."
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        text = resp.text
        return _json_dumps({"chars": len(text), "content": _compact_text(text, 12000)})

    async def _tool_send_file(
        self,
        args: Dict[str, Any],
        session: Any,
        bot: Any,
        context: Any,
        chat_id: Optional[int],
        **_: Any,
    ) -> str:
        path = self._resolve_path(session.workdir, args.get("path"))
        if not path:
            return "Ошибка: путь вне рабочей директории."
        if not os.path.exists(path):
            return "Файл не найден."
        if chat_id is None:
            return "Ошибка: chat_id неизвестен."
        with open(path, "rb") as f:
            await bot._send_document(context, chat_id=chat_id, document=f)
        return f"Файл отправлен: {os.path.basename(path)}"

    async def _tool_message(
        self,
        args: Dict[str, Any],
        bot: Any,
        context: Any,
        chat_id: Optional[int],
        **_: Any,
    ) -> str:
        text = (args.get("text") or "").strip()
        if not text:
            return "Ошибка: пустое сообщение."
        if chat_id is None:
            return "Ошибка: chat_id неизвестен."
        await bot._send_message(context, chat_id=chat_id, text=text)
        return "Сообщение отправлено."

    async def _tool_get_meme(self, args: Dict[str, Any], **_: Any) -> str:
        memes = [
            "Сделано быстрее, чем кофе заварился.",
            "Работает — не трогай.",
            "Задача выполнилась без костылей, удивительно.",
            "Код чистый, как совесть после рефакторинга.",
            "Не баг, а фича.",
        ]
        return memes[int(time.time()) % len(memes)]

    async def _tool_schedule_task(
        self,
        args: Dict[str, Any],
        bot: Any,
        context: Any,
        chat_id: Optional[int],
        **_: Any,
    ) -> str:
        text = (args.get("text") or "").strip()
        delay = int(args.get("delay_sec") or 0)
        if not text:
            return "Ошибка: text пустой."
        if chat_id is None:
            return "Ошибка: chat_id неизвестен."

        async def _job():
            await asyncio.sleep(max(0, delay))
            await bot._send_message(context, chat_id=chat_id, text=text)

        asyncio.create_task(_job())
        return f"Запланировано через {delay} сек."

    async def _tool_memory(self, args: Dict[str, Any], session: Any, **_: Any) -> str:
        op = (args.get("op") or "").strip().lower()
        mem = getattr(session, "agent_memory", None)
        if mem is None:
            session.agent_memory = {}
            mem = session.agent_memory
        if op == "set":
            key = args.get("key")
            if key is None:
                return "Ошибка: key обязателен."
            mem[str(key)] = args.get("value")
            return "OK"
        if op == "get":
            key = args.get("key")
            if key is None:
                return "Ошибка: key обязателен."
            return _json_dumps({"value": mem.get(str(key))})
        if op == "delete":
            key = args.get("key")
            if key is None:
                return "Ошибка: key обязателен."
            mem.pop(str(key), None)
            return "OK"
        if op == "list":
            return _json_dumps({"keys": list(mem.keys())})
        if op == "clear":
            mem.clear()
            return "OK"
        return "Ошибка: неизвестная операция."

    def _resolve_path(self, root: str, path: Optional[str]) -> Optional[str]:
        if not path:
            return None
        if os.path.isabs(path):
            candidate = path
        else:
            candidate = os.path.join(root, path)
        if not is_within_root(candidate, root):
            return None
        return candidate

    def _write_temp(self, root: str, content: str, prefix: str, suffix: str) -> str:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        name = f"{prefix}_{stamp}{suffix}"
        path = os.path.join(root, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path
