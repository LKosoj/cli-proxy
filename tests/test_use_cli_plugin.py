import asyncio
import json

from agent.plugins.use_cli import UseCliTool
from modes.sdk.runtime.cli_contracts import CLIOutputType, CLIResponseFormat


def test_use_cli_plugin_builds_structured_claims_from_output_and_stream_artifacts(tmp_path):
    async def _run():
        normalized = tmp_path / "stream.normalized.jsonl"
        normalized.write_text(
            json.dumps(
                {
                    "kind": "tool_event",
                    "cli_name": "qwen",
                    "text": "read_file: header.blade.php",
                    "payload": {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "call-1",
                                    "name": "read_file",
                                    "input": {"absolute_path": str(tmp_path / "views" / "header.blade.php")},
                                }
                            ]
                        },
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        class _Session:
            last_cli_normalized_stream_path = str(normalized)
            config = None

            async def run_prompt(self, prompt: str, *, force_fresh: bool = False) -> str:
                del prompt, force_fresh
                return "- В header есть account dropdown.\n- Меню содержит login CTA."

        tool = UseCliTool()
        result = await tool.execute(
            {"task_text": "collect repo facts"},
            {"session": _Session(), "chat_id": 1},
        )

        assert result["success"] is True
        claim_texts = [str(item.get("text") or "") for item in result.get("claims") or []]
        assert "В header есть account dropdown." in claim_texts
        assert "Меню содержит login CTA." in claim_texts
        evidence_paths = [
            str(ev.get("path") or "")
            for claim in result.get("claims") or []
            for ev in (claim.get("evidence") or [])
        ]
        assert str(tmp_path / "views" / "header.blade.php") in evidence_paths

    asyncio.run(_run())


def test_use_cli_plugin_keeps_full_unstructured_output_without_trim_markers(tmp_path):
    async def _run():
        long_output = ("repo-grounded paragraph\n" * 400).strip()

        class _Session:
            last_cli_normalized_stream_path = ""
            config = None

            async def run_prompt(self, prompt: str, *, force_fresh: bool = False) -> str:
                del prompt, force_fresh
                return long_output

        tool = UseCliTool()
        result = await tool.execute(
            {"task_text": "collect repo facts"},
            {"session": _Session(), "chat_id": 1},
        )

        assert result["success"] is True
        assert result["output"] == long_output
        text_outputs = [item for item in result.get("outputs") or [] if str(item.get("type") or "") == "text"]
        assert len(text_outputs) == 1
        assert text_outputs[0]["content"] == long_output
        assert "...(truncated" not in text_outputs[0]["content"]

    asyncio.run(_run())


def test_use_cli_plugin_claim_bundle_json_mode_uses_structured_payload(tmp_path):
    async def _run():
        class _Session:
            last_cli_normalized_stream_path = ""
            config = None

            async def run_prompt(self, prompt: str, *, force_fresh: bool = False) -> str:
                del force_fresh
                assert f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.CLAIM_BUNDLE_JSON}" in prompt
                return json.dumps(
                    {
                        "final_text": "Финальный review текст",
                        "claims": [
                            {
                                "claim_id": "claim_1",
                                "status": "confirmed",
                                "text": "В header есть account dropdown.",
                                "evidence": [
                                    {
                                        "type": "repo_evidence",
                                        "path": str(tmp_path / "views" / "header.blade.php"),
                                        "preview": "read_file: header.blade.php",
                                    }
                                ],
                            }
                        ],
                        "evidence": [
                            {
                                "type": "repo_evidence",
                                "path": str(tmp_path / "views" / "header.blade.php"),
                                "preview": "read_file: header.blade.php",
                            }
                        ],
                        "open_gaps": ["Проверить mobile state"],
                    },
                    ensure_ascii=False,
                )

        tool = UseCliTool()
        result = await tool.execute(
            {"task_text": "final review", "response_format": CLIResponseFormat.CLAIM_BUNDLE_JSON},
            {"session": _Session(), "chat_id": 1},
        )

        assert result["success"] is True
        assert result["output"] == "Финальный review текст"
        claim_texts = [str(item.get("text") or "") for item in result.get("claims") or []]
        assert claim_texts == ["В header есть account dropdown."]
        assert result.get("open_gaps") == ["Проверить mobile state"]

    asyncio.run(_run())


def test_use_cli_plugin_claim_bundle_json_mode_parses_full_output_before_trimming(tmp_path):
    async def _run():
        large_final_text = ("Подтвержденный фрагмент.\n" * 2000).strip()

        class _Session:
            last_cli_normalized_stream_path = ""
            config = None

            async def run_prompt(self, prompt: str, *, force_fresh: bool = False) -> str:
                del force_fresh
                assert f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.CLAIM_BUNDLE_JSON}" in prompt
                return json.dumps(
                    {
                        "final_text": large_final_text,
                        "claims": [
                            {
                                "claim_id": "claim_1",
                                "status": "confirmed",
                                "text": "Header подтвержден.",
                                "evidence": [
                                    {
                                        "type": "repo_evidence",
                                        "path": str(tmp_path / "views" / "header.blade.php"),
                                        "preview": "read_file: header.blade.php",
                                    }
                                ],
                            }
                        ],
                        "evidence": [],
                        "open_gaps": [],
                    },
                    ensure_ascii=False,
                )

        tool = UseCliTool()
        result = await tool.execute(
            {"task_text": "final review", "response_format": CLIResponseFormat.CLAIM_BUNDLE_JSON},
            {"session": _Session(), "chat_id": 1},
        )

        assert result["success"] is True
        assert result["output"] == large_final_text
        assert result.get("claims") and result["claims"][0]["text"] == "Header подтвержден."
        output_types = [str(item.get("type") or "") for item in result.get("outputs") or []]
        assert CLIOutputType.DEGRADED_MODE not in output_types

    asyncio.run(_run())


def test_use_cli_plugin_claim_bundle_json_mode_falls_back_when_required_fields_missing(tmp_path):
    async def _run():
        class _Session:
            last_cli_normalized_stream_path = ""
            config = None

            async def run_prompt(self, prompt: str, *, force_fresh: bool = False) -> str:
                del force_fresh
                assert f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.CLAIM_BUNDLE_JSON}" in prompt
                return json.dumps(
                    {
                        "claims": [
                            {
                                "claim_id": "claim_1",
                                "status": "confirmed",
                                "text": "В header есть account dropdown.",
                            }
                        ],
                        "evidence": [],
                        "open_gaps": [],
                    },
                    ensure_ascii=False,
                )

        tool = UseCliTool()
        raw = json.dumps(
            {
                "claims": [
                    {
                        "claim_id": "claim_1",
                        "status": "confirmed",
                        "text": "В header есть account dropdown.",
                    }
                ],
                "evidence": [],
                "open_gaps": [],
            },
            ensure_ascii=False,
        )
        result = await tool.execute(
            {"task_text": "final review", "response_format": CLIResponseFormat.CLAIM_BUNDLE_JSON},
            {"session": _Session(), "chat_id": 1},
        )

        assert result["success"] is True
        assert result["output"] == raw
        assert result.get("claims") == []
        assert result.get("open_gaps") == []
        output_types = [str(item.get("type") or "") for item in result.get("outputs") or []]
        assert CLIOutputType.DEGRADED_MODE in output_types

    asyncio.run(_run())


def test_use_cli_plugin_claim_bundle_json_mode_falls_back_when_claim_payload_malformed(tmp_path):
    async def _run():
        class _Session:
            last_cli_normalized_stream_path = ""
            config = None

            async def run_prompt(self, prompt: str, *, force_fresh: bool = False) -> str:
                del force_fresh
                assert f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.CLAIM_BUNDLE_JSON}" in prompt
                return json.dumps(
                    {
                        "final_text": "Финальный review текст",
                        "claims": [{"claim_id": "claim_1", "status": "confirmed"}],
                        "evidence": [],
                        "open_gaps": [],
                    },
                    ensure_ascii=False,
                )

        raw = json.dumps(
            {
                "final_text": "Финальный review текст",
                "claims": [{"claim_id": "claim_1", "status": "confirmed"}],
                "evidence": [],
                "open_gaps": [],
            },
            ensure_ascii=False,
        )
        tool = UseCliTool()
        result = await tool.execute(
            {"task_text": "final review", "response_format": CLIResponseFormat.CLAIM_BUNDLE_JSON},
            {"session": _Session(), "chat_id": 1},
        )

        assert result["success"] is True
        assert result["output"] == raw
        assert result.get("claims") == []
        output_types = [str(item.get("type") or "") for item in result.get("outputs") or []]
        assert CLIOutputType.DEGRADED_MODE in output_types

    asyncio.run(_run())


def test_use_cli_plugin_repo_review_bundle_json_mode_uses_review_specific_schema(tmp_path):
    async def _run():
        class _Session:
            last_cli_normalized_stream_path = ""
            config = None

            async def run_prompt(self, prompt: str, *, force_fresh: bool = False) -> str:
                del force_fresh
                assert f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.REPO_REVIEW_BUNDLE_JSON}" in prompt
                return json.dumps(
                    {
                        "verdict": "Критичных расхождений не найдено.",
                        "mismatches": ["Несовпадение в описании mobile state"],
                        "unverified_claims": ["Telegram WebApp не подтвержден"],
                        "corrections": ["Заменить формулировку про Telegram на 'не подтверждено'."],
                        "claims": [
                            {
                                "claim_id": "claim_1",
                                "status": "confirmed",
                                "text": "В header есть account dropdown.",
                                "evidence": [
                                    {
                                        "type": "repo_evidence",
                                        "path": str(tmp_path / "views" / "header.blade.php"),
                                        "preview": "read_file: header.blade.php",
                                    }
                                ],
                            }
                        ],
                        "evidence": [
                            {
                                "type": "repo_evidence",
                                "path": str(tmp_path / "views" / "header.blade.php"),
                                "preview": "read_file: header.blade.php",
                            }
                        ],
                        "open_gaps": ["Проверить mobile state"],
                    },
                    ensure_ascii=False,
                )

        tool = UseCliTool()
        result = await tool.execute(
            {"task_text": "repo final review", "response_format": CLIResponseFormat.REPO_REVIEW_BUNDLE_JSON},
            {"session": _Session(), "chat_id": 1},
        )

        assert result["success"] is True
        assert "VERDICT" in result["output"]
        assert "MISMATCHES" in result["output"]
        assert "UNVERIFIED_CLAIMS" in result["output"]
        assert "CORRECTIONS" in result["output"]
        assert result.get("open_gaps") == ["Проверить mobile state"]
        claim_texts = [str(item.get("text") or "") for item in result.get("claims") or []]
        assert claim_texts == ["В header есть account dropdown."]
        output_types = [str(item.get("type") or "") for item in result.get("outputs") or []]
        assert CLIOutputType.REPO_REVIEW_VERDICT in output_types
        assert CLIOutputType.REPO_REVIEW_MISMATCH in output_types
        assert CLIOutputType.REPO_REVIEW_UNVERIFIED_CLAIM in output_types
        assert CLIOutputType.REPO_REVIEW_CORRECTION in output_types

    asyncio.run(_run())


def test_use_cli_plugin_repo_review_bundle_json_falls_back_when_verdict_missing(tmp_path):
    async def _run():
        class _Session:
            last_cli_normalized_stream_path = ""
            config = None

            async def run_prompt(self, prompt: str, *, force_fresh: bool = False) -> str:
                del prompt, force_fresh
                return json.dumps(
                    {
                        "mismatches": ["gap"],
                        "unverified_claims": [],
                        "corrections": [],
                        "claims": [],
                        "evidence": [],
                        "open_gaps": [],
                    },
                    ensure_ascii=False,
                )

        raw = json.dumps(
            {
                "mismatches": ["gap"],
                "unverified_claims": [],
                "corrections": [],
                "claims": [],
                "evidence": [],
                "open_gaps": [],
            },
            ensure_ascii=False,
        )
        tool = UseCliTool()
        result = await tool.execute(
            {"task_text": "repo final review", "response_format": CLIResponseFormat.REPO_REVIEW_BUNDLE_JSON},
            {"session": _Session(), "chat_id": 1},
        )

        assert result["success"] is True
        assert result["output"] == raw
        assert result.get("claims") == []
        output_types = [str(item.get("type") or "") for item in result.get("outputs") or []]
        assert CLIOutputType.DEGRADED_MODE in output_types

    asyncio.run(_run())


def test_use_cli_plugin_retries_once_on_retryable_cli_output(tmp_path):
    async def _run():
        class _Session:
            last_cli_normalized_stream_path = ""
            config = None

            def __init__(self):
                self.calls = 0

            async def run_prompt(self, prompt: str, *, force_fresh: bool = False) -> str:
                del prompt, force_fresh
                self.calls += 1
                if self.calls == 1:
                    return "[API Error: Qwen API quota exceeded]"
                return json.dumps(
                    {
                        "final_text": "Финальный review текст",
                        "claims": [],
                        "evidence": [],
                        "open_gaps": [],
                    },
                    ensure_ascii=False,
                )

        session = _Session()
        tool = UseCliTool()
        result = await tool.execute(
            {"task_text": "final review", "response_format": CLIResponseFormat.CLAIM_BUNDLE_JSON},
            {"session": session, "chat_id": 1},
        )

        assert result["success"] is True
        assert result["output"] == "Финальный review текст"
        assert session.calls == 2
        output_types = [str(item.get("type") or "") for item in result.get("outputs") or []]
        assert CLIOutputType.CLI_RETRY_NOTICE in output_types

    asyncio.run(_run())
