"""Parser registry. Add a new scanner by implementing Parser (PRD 4.1.1)."""

from seninfo.parsers.base import Parser, ParsedReport, ParseIssue
from seninfo.parsers.gitleaks import GitleaksParser
from seninfo.parsers.trufflehog import TrufflehogParser

PARSERS = {
    "trufflehog": TrufflehogParser,
    "gitleaks": GitleaksParser,
}

__all__ = ["Parser", "ParsedReport", "ParseIssue", "PARSERS"]
