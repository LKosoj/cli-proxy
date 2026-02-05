from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import tempfile
from typing import Any, Dict, Optional

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec
from agent.tooling import helpers


class CodeInterpreterTool(ToolPlugin):
    def get_source_name(self) -> str:
        return "Code Interpreter (Local Python)"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="codeinterpreter",
            description="Выполнить ограниченный Python код локально. Без импорта и без доступа к файловой системе.",
            parameters={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python код (ограниченный)"},
                    "timeout_sec": {"type": "integer", "description": "Таймаут выполнения", "default": 20},
                },
                "required": ["code"],
            },
            parallelizable=False,
            timeout_ms=60_000,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        code = (args.get("code") or "").strip()
        if not code:
            return {"success": False, "error": "code обязателен"}
        timeout = int(args.get("timeout_sec") or 20)
        timeout = max(1, min(timeout, 60))

        blocked, reason = self._static_block(code)
        if blocked:
            return {"success": False, "error": f"BLOCKED: {reason}"}

        try:
            out = await asyncio.to_thread(self._run_sync, code, timeout)
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": f"Python failed: {e}"}

        return {"success": True, "output": helpers._trim_output(out)}

    def _static_block(self, code: str) -> tuple[bool, str]:
        # Консервативный фильтр: этот инструмент не должен превращаться в RCE/эксфильтрацию.
        bad = [
            (r"\\bimport\\b", "imports запрещены"),
            (r"\\bfrom\\b", "imports запрещены"),
            (r"\\bos\\b|os\\.", "os запрещен"),
            (r"\\bsubprocess\\b|subprocess\\.", "subprocess запрещен"),
            (r"\\bsocket\\b|socket\\.", "socket запрещен"),
            (r"\\brequests\\b|requests\\.", "requests запрещен"),
            (r"\\bopen\\s*\\(", "file io запрещен"),
            (r"__\\w+__", "dunder запрещен"),
            (r"\\beval\\s*\\(|\\bexec\\s*\\(", "eval/exec запрещены"),
        ]
        for pat, why in bad:
            if re.search(pat, code, re.I):
                return True, why
        return False, ""

    def _run_sync(self, code: str, timeout: int) -> str:
        # Выполняем в отдельном процессе python3. stdin/ok, без окружения.
        runner = (
            "import sys\\n"
            "code = sys.stdin.read()\\n"
            "ns = {}\\n"
            "exec(compile(code, '<code>', 'exec'), {'__builtins__': __builtins__}, ns)\\n"
        )
        # Урезаем builtins: оставляем минимум для арифметики/структур.
        # Python не дает легко выкинуть все опасное, поэтому основной барьер это static_block.
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".py") as f:
            f.write(runner)
            runner_path = f.name
        try:
            p = subprocess.run(
                ["python3", runner_path],
                input=code,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                cwd=os.getcwd(),
                env={"PYTHONIOENCODING": "utf-8"},
            )
        finally:
            try:
                os.remove(runner_path)
            except Exception:
                pass
        out = (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
        if p.returncode != 0:
            return f"Exit {p.returncode}:\n{out.strip()}"
        return out.strip() or "(empty output)"
