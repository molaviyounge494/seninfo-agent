"""Deep judgement engine (PRD 4.2.2-4.2.4, design M3/M4).

Tool loop with budget accounting, untrusted-context tagging (assembly lives
in ``llm.prompts``), schema-enforced structured output, Critic review with
one reconsider round, and an exhaustive UNRESOLVED fallback map — nothing is
ever silently dismissed.
"""

from typing import Dict, List, Optional

from seninfo import __version__
from seninfo.config import BudgetConfig, Config
from seninfo.engine.lsp import LspManager
from seninfo.engine.tools import Workspace
from seninfo.llm.client import LLMError, LLMHttpError, LLMTimeoutError
from seninfo.llm.prompts import (
    SYSTEM_CRITIC,
    SYSTEM_JUDGE,
    build_critic_user,
    build_judge_init_user,
    build_reconsider_user,
    build_tool_result_user,
    estimate_tokens,
)
from seninfo.llm.schema import (
    CriticCall,
    JudgeCall,
    JudgeSubmit,
    JudgeToolRequest,
    SchemaValidationError,
    extract_json,
    parse_critic,
    parse_judge_call,
)
from seninfo.models import (
    Category,
    Judgment,
    NormalizedFinding,
    Verdict,
)
from seninfo.redaction import mask_secret

# reason codes recorded in evidence.detail.reason_code + manifest counters
UNRESOLVED_REASONS = {
    "no_llm_configured": "未配置模型 endpoint(Fast-Path 模式), 交人工",
    "degraded_no_workspace": "无 workspace, 深度研判不可用, 交人工",
    "model_timeout": "模型调用超时/网络错误, 交人工",
    "llm_http": "模型端点返回错误, 交人工",
    "schema_invalid": "模型输出不符合结构化 Schema, 交人工",
    "round_limit": "探索轮数超限, 交人工",
    "token_limit": "上下文 Token 预算超限, 交人工",
    "low_confidence": "模型置信度低于阈值, 交人工",
    "critic_disagreement": "对抗复核分歧且复议后仍不一致, 交人工",
    "parse_error": "输入记录无法解析, 交人工",
}


class DeepEngine:
    """Per-finding tool + LLM + Critic loop. One finding per instance call."""

    def __init__(
        self,
        workspace: Workspace,
        client,
        cfg: Config,
        lsp_manager: Optional[LspManager] = None,
    ):
        self.ws = workspace
        self.client = client
        self.cfg = cfg
        self.budget: BudgetConfig = cfg.budget
        self.lsp = lsp_manager or LspManager(workspace)
        self.lsp_fallback = False  # sticky flag reported to orchestrator

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def judge(self, finding: NormalizedFinding) -> Judgment:
        if not self.client:
            return self._unresolved(finding, "no_llm_configured")
        masked = mask_secret(finding.secret_raw)
        initial = self.ws.initial_slice(finding.file_path, finding.line)
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": SYSTEM_JUDGE},
            {"role": "user", "content": build_judge_init_user(finding, masked, initial)},
        ]
        trail: List[str] = []
        evidence: List[dict] = []
        tool_calls = 0
        schema_errors = 0
        max_turns = self.budget.tool_rounds + self.budget.schema_retries + 8

        for _ in range(max_turns):
            if self._over_budget(messages):
                return self._unresolved(
                    finding, "token_limit", trail=trail, extra=evidence
                )
            try:
                text = self.client.complete(messages)
            except LLMError as exc:
                return self._unresolved(
                    finding, self._llm_reason(exc), trail=trail, extra=evidence,
                    detail=str(exc),
                )
            try:
                call = self._parse_judge(text)
            except SchemaValidationError as exc:
                schema_errors += 1
                if schema_errors > self.budget.schema_retries:
                    return self._unresolved(
                        finding, "schema_invalid", trail=trail, extra=evidence,
                        detail=str(exc),
                    )
                messages.append({"role": "assistant", "content": text})
                messages.append(
                    {
                        "role": "user",
                        "content": "[system] 输出不符合 schema: %s。请只重新输出合法 JSON。" % exc,
                    }
                )
                continue

            messages.append({"role": "assistant", "content": text})

            if call.kind == "tool_request":
                if tool_calls >= self.budget.tool_rounds:
                    return self._unresolved(
                        finding, "round_limit", trail=trail, extra=evidence
                    )
                tool_calls += 1
                result, ok = self._dispatch(finding, call.tool_request)
                trail.append(
                    "round%d: %s(%s) -> %s"
                    % (
                        tool_calls,
                        call.tool_request.tool,
                        self._args_summary(call.tool_request.args),
                        "ok" if ok else "err",
                    )
                )
                evidence.append(
                    {
                        "type": "tool_trace",
                        "tool": call.tool_request.tool,
                        "args": call.tool_request.args,
                        "ok": ok,
                        "summary": result[:500],
                    }
                )
                messages.append(
                    {"role": "user", "content": build_tool_result_user([result])}
                )
                continue

            # kind == "submit"
            return self._finalize(finding, call.submit, messages, trail, evidence)

        return self._unresolved(
            finding, "round_limit", trail=trail, extra=evidence,
            detail="模型往返次数超限",
        )

    # ------------------------------------------------------------------
    # Submit / Critic / reconsider
    # ------------------------------------------------------------------

    def _finalize(
        self,
        finding: NormalizedFinding,
        submit: JudgeSubmit,
        messages: List[Dict[str, str]],
        trail: List[str],
        evidence: List[dict],
    ) -> Judgment:
        if submit.confidence < 0.6:  # design 5.4: skip critic on low confidence
            return self._unresolved(
                finding, "low_confidence", trail=trail, extra=evidence,
                detail="confidence=%.2f" % submit.confidence,
            )
        critic = self._run_critic(finding, submit, trail)
        if critic is None:
            return self._unresolved(
                finding, "schema_invalid", trail=trail, extra=evidence,
                detail="Critic 输出多次不符合 schema",
            )
        if critic.agree:
            return self._build(finding, submit, trail, evidence, critic=critic)
        # reconsider once (design 5.4)
        reconsider = self._reconsider(finding, submit, messages, critic, evidence)
        if reconsider is None:
            return self._unresolved(
                finding, "critic_disagreement", trail=trail, extra=evidence,
                detail="复议输出不符合 schema",
            )
        same = (
            reconsider.category == submit.category
            and reconsider.verdict == submit.verdict
        )
        if same:
            return self._unresolved(
                finding, "critic_disagreement", trail=trail, extra=evidence,
                detail="复议后仍坚持原裁决",
            )
        # judge changed its mind after the critic's objection -> accept
        return self._build(
            finding, reconsider, trail, evidence,
            critic=critic, reconsidered=True,
        )

    def _run_critic(
        self, finding: NormalizedFinding, submit: JudgeSubmit, trail: List[str]
    ) -> Optional[CriticCall]:
        messages = [
            {"role": "system", "content": SYSTEM_CRITIC},
            {
                "role": "user",
                "content": build_critic_user(
                    finding,
                    mask_secret(finding.secret_raw),
                    submit.category.value,
                    submit.verdict.value,
                    submit.confidence,
                    "",  # rationale intentionally withheld (anti-anchoring)
                    trail,
                ),
            },
        ]
        model = getattr(self.client, "critic_model", None) or None
        for _ in range(self.budget.schema_retries + 1):
            try:
                text = self.client.complete(messages, model=model)
                return parse_critic(extract_json(text))
            except LLMError:
                return None  # surfaced as schema_invalid by caller? no: map below
            except SchemaValidationError:
                continue
        return None

    def _reconsider(
        self,
        finding: NormalizedFinding,
        submit: JudgeSubmit,
        messages: List[Dict[str, str]],
        critic: CriticCall,
        evidence: List[dict],
    ) -> Optional[JudgeSubmit]:
        messages = list(messages) + [
            {"role": "user", "content": build_reconsider_user(critic.rationale)}
        ]
        for _ in range(self.budget.schema_retries + 1):
            try:
                text = self.client.complete(messages)
                call = self._parse_judge(text)
                if call.kind == "submit":
                    return call.submit
                raise SchemaValidationError("复议要求 submit")
            except LLMError:
                return None
            except SchemaValidationError:
                continue
        return None

    # ------------------------------------------------------------------
    # Tools dispatch
    # ------------------------------------------------------------------

    def _dispatch(
        self, finding: NormalizedFinding, req: JudgeToolRequest
    ) -> tuple:
        """Execute one read-only tool call. Never raises; returns (text, ok)."""
        tool = req.tool
        args = req.args
        try:
            if tool == "read_file_range":
                offset, lines = self.ws.read_file_range(
                    str(args["file"]), int(args["start"]), int(args["end"])
                )
                return self.ws.render_range(offset, lines), offset is not None
            if tool == "search_codebase":
                hits = self.ws.search_codebase(str(args["keyword"]))
                return self.ws.render_hits(hits), True
            if tool == "goto_definition":
                return self._goto_definition(
                    str(args["file"]), int(args["line"]), int(args.get("col", 1))
                )
        except ValueError as exc:  # traversal guard / bad path
            return "(工具错误: %s)" % exc, False
        except Exception as exc:  # pragma: no cover - unexpected tool bug
            return "(工具内部错误: %s)" % exc, False
        return "(工具错误: 未知调用)", False

    def _goto_definition(self, rel_path: str, line: int, col: int) -> tuple:
        language = self.lsp.language_for(rel_path)
        session = self.lsp.session_for(language) if language else None
        if session is None:
            self.lsp_fallback = True
            return (
                "(LSP 不可用, 无法 goto_definition; 请改用 read_file_range/"
                "search_codebase 调查)",
                False,
            )
        text = self.ws.read_all_text(rel_path)
        if text is None:
            return "(文件不可读)", False
        try:
            target = session.goto_definition(
                rel_path,
                line,
                col,
                text,
                language_id=self.lsp.language_id_for(language),
            )
        except Exception as exc:  # includes LspUnavailableError
            self.lsp_fallback = True
            return "(goto_definition 失败: %s; 建议改用 search_codebase)" % exc, False
        if not target:
            return "(goto_definition: 未找到定义)", True
        uri = target["uri"]
        if uri.startswith("file://"):
            from pathlib import Path as _Path
            try:
                target_path = _Path(uri[7:]).resolve()
                rel = target_path.relative_to(self.ws.root)
            except ValueError:
                return (
                    "(goto_definition: 定义点 %s 在仓库外, 忽略)" % uri, True
                )
            offset, lines = self.ws.read_file_range(
                str(rel), target["line"] - 10, target["line"] + 10
            )
            body = self.ws.render_range(offset, lines)
            return "定义位于 %s:%s\n%s" % (rel, target["line"], body), True
        return "(goto_definition: 未知目标 %s)" % uri, False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_judge(text: str) -> JudgeCall:
        try:
            return parse_judge_call(text)
        except SchemaValidationError:
            return parse_judge_call(extract_json(text))

    @staticmethod
    def _args_summary(args: dict) -> str:
        return ",".join("%s=%s" % (k, v) for k, v in args.items())

    @staticmethod
    def _llm_reason(exc: LLMError) -> str:
        if isinstance(exc, LLMTimeoutError):
            return "model_timeout"
        if isinstance(exc, LLMHttpError):
            return "llm_http"
        return "llm_http"

    def _over_budget(self, messages: List[Dict[str, str]]) -> bool:
        total = sum(estimate_tokens(m["content"]) for m in messages)
        return total > self.budget.context_tokens

    def _build(
        self,
        finding: NormalizedFinding,
        submit: JudgeSubmit,
        trail: List[str],
        evidence: List[dict],
        critic: Optional[CriticCall] = None,
        reconsidered: bool = False,
    ) -> Judgment:
        extra = list(evidence)
        extra.append(
            {
                "type": "model_reason",
                "summary": submit.rationale,
                "detail": {"confidence": submit.confidence,
                           "reconsidered": reconsidered},
            }
        )
        if critic is not None:
            extra.append(
                {
                    "type": "critic_review",
                    "summary": "同意裁决" if critic.agree else "质疑后复议",
                    "detail": {"agree": critic.agree, "rationale": critic.rationale},
                }
            )
        return Judgment(
            finding_id=finding.finding_id,
            verdict=submit.verdict,
            category=submit.category,
            confidence=submit.confidence,
            secret_masked=mask_secret(finding.secret_raw),
            locations=[{"file": finding.file_path, "line": finding.line}],
            evidence=extra,
            tool=finding.tool,
            detector=finding.detector,
            verified=finding.verified,
            entropy=finding.entropy,
            engine_version=__version__,
        )

    def _unresolved(
        self,
        finding: NormalizedFinding,
        reason_code: str,
        trail: Optional[List[str]] = None,
        extra: Optional[List[dict]] = None,
        detail: str = "",
    ) -> Judgment:
        evidence = [
            {
                "type": "unresolved",
                "summary": UNRESOLVED_REASONS.get(reason_code, reason_code),
                "detail": {"reason_code": reason_code, "detail": detail or None},
            }
        ]
        if extra:
            evidence = extra + evidence
        if trail and reason_code not in ("no_llm_configured", "degraded_no_workspace"):
            evidence.insert(
                0,
                {
                    "type": "tool_trace_log",
                    "summary": "调查轨迹",
                    "detail": {"rounds": len(trail), "trace": trail[-10:]},
                },
            )
        return Judgment(
            finding_id=finding.finding_id,
            verdict=Verdict.SENSITIVE,
            category=Category.UNRESOLVED,
            confidence=None,
            secret_masked=mask_secret(finding.secret_raw),
            locations=[{"file": finding.file_path, "line": finding.line}],
            evidence=evidence,
            tool=finding.tool,
            detector=finding.detector,
            verified=finding.verified,
            entropy=finding.entropy,
            engine_version=__version__,
        )
