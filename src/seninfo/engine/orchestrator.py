"""Scan orchestrator (design M6): fast-path -> deep -> aggregate -> stats.

Pipeline guarantees (PRD 4.2.4): any finding that cannot be adjudicated ends
up sensitive/UNRESOLVED with a machine-readable reason; the process exit
code stays decoupled from findings.
"""

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from seninfo import __version__
from seninfo.config import Config
from seninfo.engine.aggregator import aggregate
from seninfo.engine.deep import DeepEngine
from seninfo.engine.fastpath import short_circuit, unresolved_judgment
from seninfo.engine.lsp import LspManager
from seninfo.engine.tools import Workspace
from seninfo.models import Category, Judgment, NormalizedFinding, Verdict
from seninfo.parsers.base import ParsedReport


@dataclass
class ScanResult:
    judgments: List[Judgment] = field(default_factory=list)
    by_verdict: Dict[str, int] = field(default_factory=dict)
    by_category: Dict[str, int] = field(default_factory=dict)
    reason_counts: Dict[str, int] = field(default_factory=dict)
    parse_errors: int = 0
    short_circuited: int = 0
    deep_judged: int = 0
    lsp_fallback: int = 0
    degraded: bool = False
    fast_ms: float = 0.0
    deep_timings_ms: List[float] = field(default_factory=list)
    model_id: str = ""


def run_scan(
    report: ParsedReport,
    cfg: Config,
    rules: list,
    client=None,
    workspace: Optional[Workspace] = None,
) -> ScanResult:
    result = ScanResult(degraded=workspace is None)
    pairs: List[tuple] = []
    pending: List[NormalizedFinding] = []
    t0 = time.perf_counter()

    for finding in report.findings:
        judgment = short_circuit(finding, rules)
        if judgment is not None:
            pairs.append((finding, judgment))
            result.short_circuited += 1
        else:
            pending.append(finding)
    result.fast_ms = (time.perf_counter() - t0) * 1000

    deep_judgments: Dict[str, Judgment] = {}
    engines: List[DeepEngine] = []
    if pending:
        if client is None or workspace is None:
            reason = (
                "degraded_no_workspace"
                if workspace is None
                else "no_llm_configured"
            )
            for finding in pending:
                deep_judgments[finding.finding_id] = _unresolved_with_reason(
                    finding, reason
                )
        else:
            result.model_id = getattr(client, "model", "") or ""
            manager = LspManager(workspace)
            engines = [DeepEngine(workspace, client, cfg, manager) for _ in pending]
            t1 = time.perf_counter()
            with ThreadPoolExecutor(max_workers=max(1, cfg.budget.workers)) as pool:
                futures = {}
                for engine, finding in zip(engines, pending):
                    futures[pool.submit(engine.judge, finding)] = (engine, finding)
                for future, (_engine, finding) in futures.items():
                    try:
                        deep_judgments[finding.finding_id] = future.result()
                    except Exception as exc:  # judge must not raise
                        deep_judgments[finding.finding_id] = _unresolved_with_reason(
                            finding, "llm_http", detail=str(exc)
                        )
                    result.deep_timings_ms.append(
                        (time.perf_counter() - t1) * 1000 / max(1, len(futures))
                    )
            result.deep_judged = len(deep_judgments)
            result.lsp_fallback = sum(1 for e in engines if e.lsp_fallback)
            manager.close_all()

    for finding in pending:
        pairs.append((finding, deep_judgments[finding.finding_id]))

    result.judgments = aggregate(pairs)

    # parse-error placeholders are never aggregated (ACC-10)
    for issue in report.issues:
        result.judgments.append(
            Judgment(
                finding_id="parse-error:%d" % issue.index,
                verdict=Verdict.SENSITIVE,
                category=Category.UNRESOLVED,
                confidence=None,
                secret_masked="",
                locations=[],
                evidence=[
                    {
                        "type": "unresolved",
                        "summary": "记录无法解析, 交人工核对原始报告",
                        "detail": {
                            "reason_code": "parse_error",
                            "index": issue.index,
                            "error": issue.message,
                        },
                    }
                ],
                engine_version=__version__,
            )
        )
    result.parse_errors = len(report.issues)

    _tally(result)
    return result


def _tally(result: ScanResult) -> None:
    by_verdict: Dict[str, int] = {}
    by_category: Dict[str, int] = {}
    reasons: Dict[str, int] = {}
    for judgment in result.judgments:
        by_verdict[judgment.verdict.value] = by_verdict.get(judgment.verdict.value, 0) + 1
        by_category[judgment.category.value] = by_category.get(judgment.category.value, 0) + 1
        for e in judgment.evidence:
            code = (e.get("detail") or {}).get("reason_code")
            if code:
                reasons[code] = reasons.get(code, 0) + 1
    result.by_verdict = by_verdict
    result.by_category = by_category
    result.reason_counts = reasons


def _unresolved_with_reason(
    finding: NormalizedFinding, reason_code: str, detail: str = ""
) -> Judgment:
    summary = {
        "no_llm_configured": "未配置模型 endpoint(Fast-Path 模式), 交人工",
        "degraded_no_workspace": "无 workspace, 深度研判不可用, 交人工",
        "llm_http": "深度研判异常, 交人工",
    }.get(reason_code, reason_code)
    judgment = unresolved_judgment(finding, summary)
    judgment.evidence = [
        {
            "type": "unresolved",
            "summary": summary,
            "detail": {"reason_code": reason_code, "detail": detail or None},
        }
    ]
    return judgment
