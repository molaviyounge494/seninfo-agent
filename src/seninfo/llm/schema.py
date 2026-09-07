"""LLM structured-output schema validation (PRD 4.2.3, design 5.1).

Judge emits one JSON object per turn with ``kind`` in
``tool_request | submit``; the Critic emits ``{agree, rationale}``.
Validation is stdlib-only (keeps the runner zero-dependency); the service
layer (Phase 2) may swap this for pydantic with identical field names.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from seninfo.models import Category, verdict_for, Verdict


class SchemaValidationError(ValueError):
    """Raised with a specific, model-actionable description."""


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    return m.group(1) if m else text


def _expect_object(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise SchemaValidationError("顶层必须是 JSON 对象")
    return payload


def _require_str(obj: Dict[str, Any], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SchemaValidationError("字段 %r 缺失或非字符串" % key)
    return value


ALLOWED_TOOLS = ("read_file_range", "goto_definition", "search_codebase")


@dataclass
class JudgeToolRequest:
    tool: str
    args: Dict[str, Any]
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class JudgeSubmit:
    verdict: Verdict
    category: Category
    confidence: float
    rationale: str
    evidence_refs: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class JudgeCall:
    kind: str  # "tool_request" | "submit"
    tool_request: Optional[JudgeToolRequest] = None
    submit: Optional[JudgeSubmit] = None


def _check_tool_args(tool: str, args: Any) -> Dict[str, Any]:
    if not isinstance(args, dict):
        raise SchemaValidationError("tool_request.args 必须是对象")
    if tool == "read_file_range":
        for k in ("file", "start", "end"):
            if k not in args:
                raise SchemaValidationError("read_file_range 缺少参数 %r" % k)
        if not isinstance(args["start"], int) or not isinstance(args["end"], int):
            raise SchemaValidationError("read_file_range.start/end 必须为整数")
    elif tool == "goto_definition":
        if "file" not in args or not isinstance(args.get("line"), int):
            raise SchemaValidationError("goto_definition 需要 file 与整数 line")
    elif tool == "search_codebase":
        if not isinstance(args.get("keyword"), str) or not args["keyword"].strip():
            raise SchemaValidationError("search_codebase 需要非空 keyword")
    else:
        raise SchemaValidationError(
            "未知工具 %r(允许: %s)" % (tool, ", ".join(ALLOWED_TOOLS))
        )
    return args


def _validate_submit(payload: Dict[str, Any]) -> JudgeSubmit:
    verdict_raw = _require_str(payload, "verdict")
    category_raw = _require_str(payload, "category")
    try:
        verdict = Verdict(verdict_raw)
    except ValueError:
        raise SchemaValidationError("verdict 必须是 sensitive/not_sensitive")
    try:
        category = Category(category_raw)
    except ValueError:
        raise SchemaValidationError("未知 category: %s" % category_raw)
    if category == Category.UNRESOLVED and verdict_raw == "not_sensitive":
        raise SchemaValidationError("UNRESOLVED 必须归 sensitive(交人工)")
    expected = verdict_for(category)
    if expected != verdict:
        raise SchemaValidationError(
            "category %s 与 verdict %s 不一致(应为 %s)"
            % (category_raw, verdict_raw, expected.value)
        )
    confidence = payload.get("confidence")
    if not isinstance(confidence, (int, float)):
        raise SchemaValidationError("confidence 缺失或非数值")
    confidence = float(confidence)
    if not 0.0 <= confidence <= 1.0:
        raise SchemaValidationError("confidence 必须在 [0,1]")
    rationale = _require_str(payload, "rationale")
    refs = payload.get("evidence_refs", [])
    if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
        raise SchemaValidationError("evidence_refs 必须是字符串数组")
    return JudgeSubmit(
        verdict=verdict,
        category=category,
        confidence=confidence,
        rationale=rationale,
        evidence_refs=refs,
        raw=payload,
    )


def parse_judge_call(text: str) -> JudgeCall:
    """Parse + validate one judge turn; raises SchemaValidationError."""
    try:
        payload = _expect_object(json.loads(_strip_fences(text)))
    except json.JSONDecodeError as exc:
        raise SchemaValidationError("不是合法 JSON: %s" % exc)
    kind = payload.get("kind")
    if kind == "tool_request":
        tool = _require_str(payload, "tool")
        args = _check_tool_args(tool, payload.get("args"))
        return JudgeCall(
            kind="tool_request",
            tool_request=JudgeToolRequest(tool=tool, args=args, raw=payload),
        )
    if kind == "submit":
        return JudgeCall(kind="submit", submit=_validate_submit(payload))
    raise SchemaValidationError("kind 必须是 tool_request 或 submit")


@dataclass
class CriticCall:
    agree: bool
    rationale: str


def parse_critic(text: str) -> CriticCall:
    try:
        payload = _expect_object(json.loads(_strip_fences(text)))
    except json.JSONDecodeError as exc:
        raise SchemaValidationError("不是合法 JSON: %s" % exc)
    agree = payload.get("agree")
    if not isinstance(agree, bool):
        raise SchemaValidationError("agree 必须为布尔值")
    rationale = payload.get("rationale", "")
    if not isinstance(rationale, str):
        raise SchemaValidationError("rationale 必须为字符串")
    return CriticCall(agree=agree, rationale=rationale)


def extract_json(text: str) -> str:
    """Best-effort: pull the first {...} block out of noisy model output."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise SchemaValidationError("输出中未找到 JSON 对象")
    return text[start : end + 1]
