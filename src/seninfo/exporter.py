"""Result export: JSONL records + manifest (PRD 5.1, manifest per §6.3)."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from seninfo import __version__
from seninfo.config import Config
from seninfo.models import Judgment


def write_jsonl(path: str, judgments: List[Judgment]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        for judgment in judgments:
            fh.write(json.dumps(judgment.to_record(), ensure_ascii=False) + "\n")


def _pctile(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    idx = int(q * (len(ordered) - 1))
    return round(ordered[idx], 2)


def build_manifest(
    cfg: Config,
    by_verdict: Dict[str, int],
    by_category: Dict[str, int],
    reason_counts: Dict[str, int],
    parse_errors: int,
    short_circuited: int,
    deep_judged: int,
    lsp_fallback: int,
    degraded: bool,
    model_id: str,
    rules_version: str,
    fast_ms: float,
    deep_timings_ms: List[float],
    started: datetime,
) -> dict:
    counts = {
        "total": sum(by_verdict.values()),
        "by_verdict": by_verdict,
        "by_category": by_category,
        "unresolved_reasons": reason_counts,
    }
    p95 = _pctile(deep_timings_ms, 0.95)
    p50 = _pctile(deep_timings_ms, 0.5)
    return {
        "engine_version": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(
            (datetime.now(timezone.utc) - started).total_seconds(), 3
        ),
        "mode": (
            "degraded(无 workspace)"
            if degraded
            else "deep(fast-path + LLM 深度研判)"
        ),
        "degraded_mode": degraded,
        "model_id": model_id or None,
        "rules_version": rules_version,
        "counts": counts,
        "stages": {
            "short_circuited": short_circuited,
            "deep_judged": deep_judged,
            "parse_errors": parse_errors,
            "lsp_fallback": lsp_fallback,
        },
        "timings_ms": {
            "fast_path_total": round(fast_ms, 2),
            "deep_p50": p50,
            "deep_p95": p95,
        },
        "token_usage": {"prompt": 0, "completion": 0, "note": "Phase 1 未逐条记账"},
    }


def write_manifest(path: str, data: dict) -> None:
    Path(path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
