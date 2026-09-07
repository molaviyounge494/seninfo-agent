"""LSP preflight: check what the report needs, auto-download by default.

The default registry (``src/seninfo/lsp_registry.py``) is the single source
of truth; config no longer carries LSP commands. Missing servers with an npm
source are downloaded automatically unless disabled (``SENINFO_LSP_AUTO=0`` /
``--no-lsp-download``); the rest keep the search fallback with a hint.
"""

from typing import List

from seninfo.engine.lsp_installer import ensure, resolve_command
from seninfo.lsp_registry import LSP_DEFAULTS, defaults_for
from seninfo.parsers.base import ParsedReport


def languages_in_report(report: ParsedReport) -> List[str]:
    """Distinct registry languages among finding file suffixes."""
    from seninfo.lsp_registry import builtin_language_for

    found: List[str] = []
    for f in report.findings:
        lang = builtin_language_for(f.file_path or "x.py")
        if lang and lang not in found:
            found.append(lang)
    return found


def server_status(language: str) -> dict:
    default = defaults_for(language)
    command = default.command if default else []
    return {
        "language": language,
        "binary": command[0] if command else "?",
        "command": list(command),
        "present": resolve_command(language) is not None,
        "npm_package": (default.npm_package if default else None),
        "hint": (default.hint if default else ""),
    }


def run_preflight(report: ParsedReport, download: bool = True) -> List[dict]:
    """Ensure servers for the report's languages; return final statuses.

    ``download`` is the default-ON auto-install switch (CLI/env can disable).
    """
    statuses = []
    for language in languages_in_report(report):
        if resolve_command(language) is not None:
            statuses.append(server_status(language))
            continue
        if download and defaults_for(language) is not None:
            ensure(language)  # logs failures; falls back gracefully
        statuses.append(server_status(language))
    return statuses


def print_preflight(statuses: List[dict]) -> None:
    if not statuses:
        return
    for s in statuses:
        print("  [lsp] %-8s %-28s %s" % (s["language"], s["binary"],
                                          "OK" if s["present"] else "缺失"))
    missing = [s for s in statuses if not s["present"]]
    if missing:
        print("  [lsp] 以下语言服务器不可用, 深度溯源将回落关键词检索:")
        for s in missing:
            print("    - %s: %s" % (s["binary"], s["hint"] or "(注册表未提供提示)"))
    installable = [s for s in missing if s["npm_package"]]
    if installable:
        langs = ", ".join(s["language"] for s in installable)
        print("  [lsp] 提示: %s 可自动下载(默认开启); 离线请设 SENINFO_LSP_AUTO=0"
              % langs)


def known_languages() -> List[str]:
    return sorted(LSP_DEFAULTS)
