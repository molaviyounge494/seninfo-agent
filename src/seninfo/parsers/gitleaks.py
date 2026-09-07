"""Gitleaks parser: JSON report (array) and SARIF.

Field mapping per PRD Appendix C. Gitleaks has no native network verification,
so ``verified`` stays None.
"""

import hashlib
import json
from typing import Any, Dict, List, Optional

from seninfo.entropy import shannon_entropy
from seninfo.models import NormalizedFinding
from seninfo.parsers.base import Parser, ParsedReport, ParseIssue


def _build_id(tool: str, detector: str, path: str, line: Optional[int], secret: str) -> str:
    raw = "|".join([tool, detector, path, str(line), secret])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class GitleaksParser(Parser):
    """Parse Gitleaks ``--report-format json`` (array) or ``sarif`` output."""

    def parse(self, text: str) -> ParsedReport:
        stripped = text.lstrip()
        if stripped.startswith("{"):
            try:
                obj = json.loads(text)
            except json.JSONDecodeError as exc:
                report = ParsedReport()
                report.issues.append(ParseIssue(0, "invalid json: %s" % exc))
                return report
            if isinstance(obj, dict) and "runs" in obj:
                return self._parse_sarif(obj)
            # single-finding JSON object
            return self._parse_json([obj])
        if stripped.startswith("["):
            try:
                return self._parse_json(json.loads(text))
            except json.JSONDecodeError as exc:
                report = ParsedReport()
                report.issues.append(ParseIssue(0, "invalid json: %s" % exc))
                return report
        report = ParsedReport()
        report.issues.append(ParseIssue(0, "unrecognized gitleaks report shape"))
        return report

    # -- JSON array --------------------------------------------------------

    def _parse_json(self, items: List[Dict[str, Any]]) -> ParsedReport:
        report = ParsedReport()
        for idx, item in enumerate(items):
            secret = item.get("Secret")
            detector = item.get("RuleID")
            if not secret or not detector:
                report.issues.append(
                    ParseIssue(idx, "missing RuleID/Secret in record")
                )
                continue
            f = NormalizedFinding(
                tool="gitleaks",
                detector=detector,
                file_path=Parser._norm_path(item.get("File", "")),
                line=item.get("StartLine")
                if isinstance(item.get("StartLine"), int)
                else None,
                secret_raw=secret,
                match_context=item.get("Match"),
            )
            f.entropy = shannon_entropy(f.secret_raw)
            f.finding_id = _build_id(
                f.tool, f.detector, f.file_path, f.line, f.secret_raw
            )
            report.findings.append(f)
        return report

    # -- SARIF -------------------------------------------------------------

    def _parse_sarif(self, obj: Dict[str, Any]) -> ParsedReport:
        report = ParsedReport()
        runs = obj.get("runs") or []
        if not runs:
            report.issues.append(ParseIssue(0, "sarif: no runs"))
            return report
        run = runs[0]
        driver = (run.get("tool") or {}).get("driver") or {}
        rules = {
            i: r.get("id", "")
            for i, r in enumerate(driver.get("rules") or [])
        }
        for idx, result in enumerate(run.get("results") or []):
            rule_id = result.get("ruleId") or rules.get(result.get("ruleIndex", -1), "")
            loc = self._first_location(result)
            # Known limitation (PRD Appendix C / ACC-12): SARIF normally does
            # NOT carry the plaintext secret. message.text is a best-effort
            # fallback; empty secrets are handled downstream (never silently
            # dismissed).
            secret = result.get("secret") or self._message_text(result) or ""
            if not rule_id or not loc:
                report.issues.append(
                    ParseIssue(idx, "sarif: missing ruleId/location")
                )
                continue
            f = NormalizedFinding(
                tool="gitleaks",
                detector=rule_id,
                file_path=Parser._norm_path(loc.get("uri", "")),
                line=loc.get("startLine"),
                secret_raw=secret,
                match_context=self._message_text(result),
            )
            f.entropy = shannon_entropy(f.secret_raw)
            f.finding_id = _build_id(
                f.tool, f.detector, f.file_path, f.line, f.secret_raw
            )
            report.findings.append(f)
        return report

    @staticmethod
    def _first_location(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        locations = result.get("locations") or []
        if not locations:
            return None
        physical = (locations[0].get("physicalLocation") or {}).get(
            "artifactLocation"
        )
        region = (locations[0].get("physicalLocation") or {}).get("region") or {}
        if not physical:
            return None
        return {"uri": physical.get("uri", ""), "startLine": region.get("startLine")}

    @staticmethod
    def _message_text(result: Dict[str, Any]) -> str:
        message = result.get("message") or {}
        text = message.get("text")
        return text if isinstance(text, str) else ""
