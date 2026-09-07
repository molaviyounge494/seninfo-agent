"""TruffleHog JSONL parser.

Line shape (v3)::

    {"SourceMetadata": {"Data": {"Github": {"file": "a.py", "line": 5}}},
     "DetectorName": "AWS", "Raw": "...", "Verified": true}
"""

import hashlib
import json
from typing import Any, Dict, Optional

from seninfo.entropy import shannon_entropy
from seninfo.models import NormalizedFinding
from seninfo.parsers.base import Parser, ParsedReport, ParseIssue


def _extract_location(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Grab the first provider entry carrying File/Line under Data.

    TruffleHog emits lowercase keys (``file``/``line``); tolerate the
    capitalised variants too.
    """
    inner = data.get("Data") if isinstance(data, dict) else None
    if not isinstance(inner, dict):
        return None
    for provider in inner.values():
        if not isinstance(provider, dict):
            continue
        file_key = provider.get("File") or provider.get("file")
        if file_key:
            return provider
    return None


def _build_id(tool: str, detector: str, path: str, line: Optional[int], secret: str) -> str:
    raw = "|".join([tool, detector, path, str(line), secret])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class TrufflehogParser(Parser):
    """Parse TruffleHog JSON Lines output (one JSON object per line)."""

    def parse(self, text: str) -> ParsedReport:
        report = ParsedReport()
        for idx, raw_line in enumerate(text.splitlines()):
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                report.issues.append(ParseIssue(idx, "invalid json: %s" % exc))
                continue
            finding = self._to_finding(obj)
            if finding is None:
                report.issues.append(
                    ParseIssue(idx, "missing DetectorName/Raw in record")
                )
                continue
            report.findings.append(finding)
        return report

    @staticmethod
    def _to_finding(obj: Dict[str, Any]) -> Optional[NormalizedFinding]:
        detector = obj.get("DetectorName")
        secret = obj.get("Raw")
        if not detector or not secret:
            return None
        loc = _extract_location(obj.get("SourceMetadata") or {})
        path = (loc or {}).get("File") or (loc or {}).get("file") or ""
        line_raw = (loc or {}).get("Line") or (loc or {}).get("line")
        if isinstance(line_raw, str) and line_raw.isdigit():
            line_raw = int(line_raw)
        line = line_raw if isinstance(line_raw, int) else None
        verified = obj.get("Verified")
        if isinstance(verified, str):  # tolerate "true"/"false" strings
            verified = verified.lower() == "true"
        f = NormalizedFinding(
            tool="trufflehog",
            detector=detector,
            file_path=Parser._norm_path(path),
            line=line if isinstance(line, int) else None,
            secret_raw=secret,
            verified=verified if isinstance(verified, bool) else None,
        )
        f.entropy = shannon_entropy(f.secret_raw)
        f.finding_id = _build_id(
            f.tool, f.detector, f.file_path, f.line, f.secret_raw
        )
        return f
