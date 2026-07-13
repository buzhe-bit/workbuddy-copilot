"""llm.py 单元测试：prompt 构建 + JSON 解析 + 降级逻辑。

不实际调用 LLM API，只测试本地逻辑。
"""
from __future__ import annotations

import json
import math
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from copilot.llm import (
    AnalysisOutcome,
    build_system_prompt,
    build_user_prompt,
    _parse_json_content,
    _fallback,
    analyze,
    answer_question,
    coerce_question_answer_outcome,
    question_fallback_answer,
    summarize_reply,
)
from copilot.transcript import TranscriptSnapshot, Message
from copilot.models import AnalysisResult, QuestionAnswerOutcome


class TestCoerceQuestionAnswerOutcome:
    @pytest.mark.parametrize(
        "outcome",
        [
            QuestionAnswerOutcome(status="unknown", answer="不应采用"),
            QuestionAnswerOutcome(status="answered", answer="   "),
        ],
    )
    def test_invalid_custom_outcome_fails_closed(self, outcome):
        result = coerce_question_answer_outcome(outcome)

        assert result.status == "failed"
        assert result.answer == question_fallback_answer()
        assert result.error_code == "llm_provider_error"

    @pytest.mark.parametrize(
        "unsafe_error",
        ["provider secret token=abc", "x" * 81],
    )
    def test_custom_outcome_error_code_cannot_leak_unbounded_text(self, unsafe_error):
        result = coerce_question_answer_outcome(QuestionAnswerOutcome(
            status="failed",
            answer="安全回答",
            error_code=unsafe_error,
        ))

        assert result.status == "failed"
        assert result.answer == "安全回答"
        assert result.error_code == "llm_provider_error"
        assert "secret" not in result.error_code

    def test_valid_custom_degraded_outcome_is_preserved(self):
        result = coerce_question_answer_outcome(QuestionAnswerOutcome(
            status="degraded",
            answer="  安全降级回答  ",
            error_code="llm_config_missing",
        ))

        assert result == QuestionAnswerOutcome(
            status="degraded",
            answer="安全降级回答",
            error_code="llm_config_missing",
        )

    def test_answered_clears_even_valid_error_code(self):
        result = coerce_question_answer_outcome(QuestionAnswerOutcome(
            status="answered",
            answer="正常回答",
            error_code="llm_timeout",
        ))

        assert result == QuestionAnswerOutcome(
            status="answered",
            answer="正常回答",
            error_code="",
        )

    def test_non_answered_without_error_code_gets_safe_generic_code(self):
        result = coerce_question_answer_outcome(QuestionAnswerOutcome(
            status="failed",
            answer="安全回答",
        ))

        assert result.error_code == "llm_provider_error"


class TestBuildUserPrompt:
    def test_includes_event_and_prompt(self):
        snap = TranscriptSnapshot(messages=[Message("user", "学 Python")])
        prompt = build_user_prompt(snap, "Stop", "学 Python asyncio")
        assert "Stop" in prompt
        assert "学 Python asyncio" in prompt
        assert "学 Python" in prompt

    def test_includes_tool_and_reasoning_counts(self):
        snap = TranscriptSnapshot(
            messages=[Message("user", "hi")],
            tool_calls=5,
            reasoning_steps=3,
        )
        prompt = build_user_prompt(snap, "Stop", "hi")
        assert "5" in prompt
        assert "3" in prompt

    def test_empty_prompt_shown_as_none(self):
        snap = TranscriptSnapshot()
        prompt = build_user_prompt(snap, "UserPromptSubmit", "")
        assert "(无)" in prompt


class TestBuildSystemPrompt:
    def test_includes_process_reminder_prompt_and_frequency_guardrail(self):
        cfg = {
            "analysis": {
                "process_reminder_prompt": "如果学员反复试错、没验证结果，只给轻提醒。",
            }
        }

        prompt = build_system_prompt(cfg)

        assert "如果学员反复试错、没验证结果，只给轻提醒。" in prompt
        assert "少而准" in prompt
        assert "不要频繁打断" in prompt

    def test_analysis_protocol_requires_bounded_evidence_and_forbids_invention(self):
        prompt = build_system_prompt({})

        assert '"confidence"' in prompt
        assert '"evidence"' in prompt
        assert "证据不足" in prompt
        assert "不得编造" in prompt
        assert "文件" in prompt
        assert "报错" in prompt
        assert "学习状态" in prompt

    def test_prompt_hash_tracks_only_effective_static_protocol(self):
        import copilot.llm as llm_module

        assert hasattr(llm_module, "analysis_prompt_hash")
        base = llm_module.analysis_prompt_hash({"analysis": {}})
        same = llm_module.analysis_prompt_hash({"analysis": {}})
        changed = llm_module.analysis_prompt_hash({
            "analysis": {"process_reminder_prompt": "只在第三次重复失败时提醒。"}
        })

        assert len(base) == 64
        assert base == same
        assert changed != base


class TestParseJsonContent:
    def test_valid_json(self):
        content = json.dumps({
            "topic": "学 Python",
            "understanding": "high",
            "off_topic": False,
            "stuck_at": "",
            "progress": "顺利",
            "guidance": "继续",
            "alert": "",
        })
        result = _parse_json_content(content)
        assert result["topic"] == "学 Python"
        assert result["understanding"] == "high"

    def test_json_with_code_fence(self):
        content = '```json\n{"topic": "test", "understanding": "medium"}\n```'
        result = _parse_json_content(content)
        assert result["topic"] == "test"

    def test_invalid_json_returns_fallback(self):
        result = _parse_json_content("不是 JSON")
        assert result["understanding"] == "unknown"
        assert result["topic"] == "解析失败"

    def test_missing_fields_filled(self):
        result = _parse_json_content('{"topic": "partial"}')
        assert result["understanding"] == "unknown"
        assert result["off_topic"] is False

    def test_long_fields_truncated(self):
        result = _parse_json_content(json.dumps({
            "topic": "x" * 200,
            "guidance": "y" * 200,
        }))
        assert len(result["topic"]) <= 40
        assert len(result["guidance"]) <= 60

    def test_confidence_is_clamped_and_evidence_is_sanitized(self):
        result = _parse_json_content(json.dumps({
            "topic": "边界排查",
            "confidence": 1.7,
            "evidence": [
                "  学员贴出了 IndexError 原文  ",
                42,
                "",
                "x" * 200,
                "学员说明已经复现两次",
                "第四条有效证据不应保留",
            ],
        }))

        assert result["confidence"] == 1.0
        assert result["evidence"] == [
            "学员贴出了 IndexError 原文",
            "x" * 160,
            "学员说明已经复现两次",
        ]

    @pytest.mark.parametrize(
        "invalid",
        [True, False, None, "0.8", float("nan"), float("inf"), -float("inf")],
    )
    def test_invalid_confidence_uses_compatible_default(self, invalid):
        result = AnalysisResult.from_dict({"confidence": invalid})

        assert result.confidence == 0.5
        assert math.isfinite(result.confidence)
        assert result.evidence == []

    def test_numeric_confidence_clamps_both_bounds(self):
        assert AnalysisResult.from_dict({"confidence": -0.2}).confidence == 0.0
        assert AnalysisResult.from_dict({"confidence": 2}).confidence == 1.0


class TestFallback:
    def test_fallback_with_title(self):
        snap = TranscriptSnapshot(ai_title="Python 课")
        result = _fallback(snap, "Stop")
        assert result["topic"] == "Python 课"
        assert result["understanding"] == "unknown"

    def test_fallback_without_title(self):
        snap = TranscriptSnapshot(messages=[Message("user", "hi")])
        result = _fallback(snap, "Stop")
        assert "未命名" in result["topic"]
        assert "1 条对话" in result["progress"]


class TestAnalyze:
    @pytest.mark.asyncio
    async def test_no_api_key_returns_fallback(self):
        cfg = {"llm": {"enable_llm": True, "api_key": "", "model": "x", "api_base": "x"}}
        snap = TranscriptSnapshot(ai_title="测试")
        result = await analyze(cfg, snap, "Stop", "hi")
        assert isinstance(result, AnalysisOutcome)
        assert result.ok is True
        assert result.value["understanding"] == "unknown"
        assert result.error is None
        assert result.model == "fallback"
        assert len(result.prompt_hash) == 64
        assert result.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_llm_disabled_returns_fallback(self):
        cfg = {"llm": {"enable_llm": False, "api_key": "sk-x", "model": "x", "api_base": "x"}}
        snap = TranscriptSnapshot()
        result = await analyze(cfg, snap, "Stop", "hi")
        assert result.ok is True
        assert result.value["understanding"] == "unknown"
        assert result.error is None
        assert result.model == "fallback"

    @pytest.mark.asyncio
    async def test_provider_network_failure_returns_stable_error_without_exception_body(
        self, monkeypatch, caplog
    ):
        class FailingClient:
            def __init__(self, timeout):
                self.timeout = timeout

            async def __aenter__(self):
                raise TimeoutError("provider timed out")

            async def __aexit__(self, exc_type, exc, tb):
                return False

        monkeypatch.setattr("copilot.llm.httpx.AsyncClient", FailingClient)
        cfg = {
            "llm": {
                "enable_llm": True,
                "api_key": "sk-test",
                "model": "model",
                "api_base": "https://llm.example/v1",
            }
        }

        with caplog.at_level("ERROR", logger="copilot.llm"):
            result = await analyze(cfg, TranscriptSnapshot(ai_title="网络失败"), "Stop", "hi")

        assert isinstance(result, AnalysisOutcome)
        assert result.ok is False
        assert result.value["topic"] == "网络失败"
        assert result.error == "LLM provider TimeoutError"
        assert result.model == "model"
        assert len(result.prompt_hash) == 64
        assert result.latency_ms >= 0
        assert "provider timed out" not in caplog.text

    @pytest.mark.asyncio
    async def test_provider_http_failure_returns_failed_outcome_without_response_body(self, monkeypatch):
        request = httpx.Request("POST", "https://llm.example/v1/chat/completions")
        response = httpx.Response(503, request=request, text="secret provider response")

        class FakeResponse:
            def raise_for_status(self):
                raise httpx.HTTPStatusError(
                    "provider unavailable",
                    request=request,
                    response=response,
                )

        class FakeClient:
            def __init__(self, timeout):
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, json, headers):
                return FakeResponse()

        monkeypatch.setattr("copilot.llm.httpx.AsyncClient", FakeClient)
        cfg = {
            "llm": {
                "enable_llm": True,
                "api_key": "sk-test",
                "model": "model",
                "api_base": "https://llm.example/v1",
            }
        }

        result = await analyze(cfg, TranscriptSnapshot(), "Stop", "hi")

        assert result.ok is False
        assert result.error == "LLM provider HTTP 503"
        assert "secret provider response" not in (result.error or "")

    @pytest.mark.asyncio
    async def test_malformed_provider_json_returns_failed_outcome_but_keeps_display_fallback(self, monkeypatch):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": "not-json"}}]}

        class FakeClient:
            def __init__(self, timeout):
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, json, headers):
                return FakeResponse()

        monkeypatch.setattr("copilot.llm.httpx.AsyncClient", FakeClient)
        cfg = {
            "llm": {
                "enable_llm": True,
                "api_key": "sk-test",
                "model": "model",
                "api_base": "https://llm.example/v1",
            }
        }

        result = await analyze(cfg, TranscriptSnapshot(), "Stop", "hi")

        assert result.ok is False
        assert result.value["topic"] == "解析失败"
        assert "JSON" in (result.error or "")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("content", "expected_topic"),
        [
            ('{"topic":"valid","understanding":"high","diagnosis":"kept"}', "valid"),
            (
                '```json\n{"topic":"fenced","understanding":"medium","diagnosis":"kept"}\n```',
                "fenced",
            ),
        ],
    )
    async def test_valid_provider_json_returns_success_outcome_with_fields(
        self, monkeypatch, content, expected_topic
    ):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": content}}]}

        class FakeClient:
            def __init__(self, timeout):
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, json, headers):
                return FakeResponse()

        monkeypatch.setattr("copilot.llm.httpx.AsyncClient", FakeClient)
        cfg = {
            "llm": {
                "enable_llm": True,
                "api_key": "sk-test",
                "model": "model",
                "api_base": "https://llm.example/v1",
            }
        }

        result = await analyze(cfg, TranscriptSnapshot(), "Stop", "hi")

        assert result.ok is True
        assert result.value["topic"] == expected_topic
        assert result.value["diagnosis"] == "kept"
        assert result.error is None
        assert result.model == "model"
        assert len(result.prompt_hash) == 64


class TestAnswerQuestion:
    @pytest.mark.asyncio
    async def test_llm_disabled_returns_student_help_fallback(self):
        cfg = {"llm": {"enable_llm": False, "api_key": "sk-x", "model": "x", "api_base": "x"}}
        outcome = await answer_question(cfg, "pytest 为什么失败？", [])
        assert outcome.status == "degraded"
        assert outcome.error_code == "llm_disabled"
        assert "LLM" in outcome.answer
        assert "问题" in outcome.answer

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "llm_config",
        [
            {"enable_llm": True, "api_key": "", "model": "m", "api_base": "https://x"},
            {"enable_llm": True, "api_key": "key", "model": "", "api_base": "https://x"},
            {"enable_llm": True, "api_key": "key", "model": "m", "api_base": ""},
        ],
    )
    async def test_missing_provider_config_is_degraded(self, llm_config):
        outcome = await answer_question({"llm": llm_config}, "怎么排查？", [])

        assert outcome.status == "degraded"
        assert outcome.answer == question_fallback_answer()
        assert outcome.error_code == "llm_config_missing"

    @pytest.mark.asyncio
    async def test_nonempty_provider_answer_is_answered(self, monkeypatch):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": "先打印边界变量。"}}]}

        class FakeClient:
            def __init__(self, timeout):
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, json, headers):
                return FakeResponse()

        monkeypatch.setattr("copilot.llm.httpx.AsyncClient", FakeClient)
        outcome = await answer_question({
            "llm": {
                "enable_llm": True,
                "api_key": "key",
                "model": "question-model",
                "api_base": "https://llm.example/v1",
            }
        }, "怎么排查？", [])

        assert outcome.status == "answered"
        assert outcome.answer == "先打印边界变量。"
        assert outcome.error_code == ""

    @pytest.mark.asyncio
    async def test_http_failure_is_failed_without_body_leak(self, monkeypatch, caplog):
        request = httpx.Request("POST", "https://llm.example/v1/chat/completions")
        response = httpx.Response(503, request=request, text="secret-provider-body")

        class FakeResponse:
            def raise_for_status(self):
                raise httpx.HTTPStatusError(
                    "unavailable", request=request, response=response
                )

        class FakeClient:
            def __init__(self, timeout):
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, json, headers):
                return FakeResponse()

        monkeypatch.setattr("copilot.llm.httpx.AsyncClient", FakeClient)
        with caplog.at_level("ERROR", logger="copilot.llm"):
            outcome = await answer_question({
                "llm": {
                    "enable_llm": True,
                    "api_key": "key",
                    "model": "question-model",
                    "api_base": "https://llm.example/v1",
                }
            }, "怎么排查？", [])

        assert outcome.status == "failed"
        assert outcome.answer == question_fallback_answer()
        assert outcome.error_code == "llm_http_503"
        assert "secret-provider-body" not in caplog.text

    @pytest.mark.asyncio
    async def test_timeout_is_failed_with_stable_code(self, monkeypatch, caplog):
        request = httpx.Request("POST", "https://llm.example/v1/chat/completions")

        class TimeoutClient:
            def __init__(self, timeout):
                self.timeout = timeout

            async def __aenter__(self):
                raise httpx.ReadTimeout("secret-timeout-detail", request=request)

            async def __aexit__(self, exc_type, exc, tb):
                return False

        monkeypatch.setattr("copilot.llm.httpx.AsyncClient", TimeoutClient)
        with caplog.at_level("ERROR", logger="copilot.llm"):
            outcome = await answer_question({
                "llm": {
                    "enable_llm": True,
                    "api_key": "key",
                    "model": "question-model",
                    "api_base": "https://llm.example/v1",
                }
            }, "怎么排查？", [])

        assert outcome.status == "failed"
        assert outcome.error_code == "llm_timeout"
        assert "secret-timeout-detail" not in caplog.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("payload", "expected_code"),
        [
            ({"choices": []}, "llm_response_invalid"),
            ({"choices": [{"message": {"content": "   "}}]}, "llm_empty_response"),
            ({"choices": [{"message": {"content": None}}]}, "llm_response_invalid"),
            ({"choices": [{"message": {"content": 42}}]}, "llm_response_invalid"),
            ({"choices": [{"message": {"content": []}}]}, "llm_response_invalid"),
        ],
    )
    async def test_invalid_or_empty_response_is_failed(
        self, monkeypatch, payload, expected_code
    ):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return payload

        class FakeClient:
            def __init__(self, timeout):
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, json, headers):
                return FakeResponse()

        monkeypatch.setattr("copilot.llm.httpx.AsyncClient", FakeClient)
        outcome = await answer_question({
            "llm": {
                "enable_llm": True,
                "api_key": "key",
                "model": "question-model",
                "api_base": "https://llm.example/v1",
            }
        }, "怎么排查？", [])

        assert outcome.status == "failed"
        assert outcome.answer == question_fallback_answer()
        assert outcome.error_code == expected_code


class TestSummarizeReply:
    @pytest.mark.asyncio
    async def test_uses_summary_model_and_returns_content(self, monkeypatch):
        captured = {}

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "choices": [
                        {
                            "message": {
                                "content": "AI 先解释了报错原因，再给出最小复现和验证步骤。"
                            }
                        }
                    ]
                }

        class FakeClient:
            def __init__(self, timeout):
                captured["timeout"] = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, json, headers):
                captured["url"] = url
                captured["payload"] = json
                captured["headers"] = headers
                return FakeResponse()

        monkeypatch.setattr("copilot.llm.httpx.AsyncClient", FakeClient)

        cfg = {
            "llm": {
                "enable_llm": True,
                "api_key": "sk-test",
                "api_base": "https://llm.example/v1",
                "model": "expensive-model",
                "summary_model": "cheap-summary-model",
                "timeout": 12,
            }
        }

        result = await summarize_reply(cfg, "怎么定位边界？", "先打印 i。\n再检查 len(xs)。")

        assert result == "AI 先解释了报错原因，再给出最小复现和验证步骤。"
        assert captured["payload"]["model"] == "cheap-summary-model"
        system_prompt = captured["payload"]["messages"][0]["content"]
        assert "50-300字" in system_prompt
        assert "平均100字左右" in system_prompt
        assert "简单的回复就一两句话" in system_prompt
        assert "怎么定位边界？" in captured["payload"]["messages"][1]["content"]
        assert "先打印 i。" in captured["payload"]["messages"][1]["content"]
        assert captured["url"] == "https://llm.example/v1/chat/completions"
        assert captured["headers"]["Authorization"] == "Bearer sk-test"

    @pytest.mark.asyncio
    async def test_disabled_llm_returns_empty_summary(self):
        cfg = {"llm": {"enable_llm": False, "api_key": "sk-test", "api_base": "x"}}

        result = await summarize_reply(cfg, "问题", "回复")

        assert result == ""
