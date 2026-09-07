"""seninfo-agent scan: full Phase 1 pipeline (PRD 5.1).

Exit codes are decoupled from findings:
  0  tool ran to completion (regardless of sensitive findings)
  2  tool/config error (input unreadable, rules invalid, LLM probe failed, ...)
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from seninfo import __version__
from seninfo.config import Config, load_config
from seninfo.engine.orchestrator import ScanResult, run_scan
from seninfo.engine.tools import Workspace
from seninfo.exporter import build_manifest, write_jsonl, write_manifest
from seninfo.llm.client import ChatClient, LLMError
from seninfo.parsers import PARSERS
from seninfo.rules import load_rules

EXIT_OK = 0
EXIT_ERROR = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seninfo-agent",
        description="敏感信息智能研判(仅研判,不阻断/不流转)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="对扫描器报告逐条研判并导出结果")
    scan.add_argument("--input", required=True, help="扫描器报告路径")
    scan.add_argument(
        "--tool",
        required=True,
        choices=sorted(PARSERS),
        help="报告来源(trufflehog | gitleaks)",
    )
    scan.add_argument("--workspace-dir", default=None, help="仓库根目录(缺省=降级模式)")
    scan.add_argument("--rules", default=None, help="项目级规则文件(JSON)")
    scan.add_argument("--config", default=None, help="配置文件(JSON), 见 docs/design-phase1.md §7")
    scan.add_argument(
        "--no-lsp-download",
        action="store_true",
        help="关闭 LSP 自动下载(默认开启; 离线环境用 SENINFO_LSP_AUTO=0 亦可)",
    )
    scan.add_argument("--output", required=True, help="结果文件路径(JSONL)")
    scan.add_argument("--format", default="jsonl", choices=["jsonl"])
    return parser


def _print_summary(result: ScanResult, llm_used: bool) -> None:
    by_verdict = result.by_verdict
    print("seninfo-agent scan 完成(%s)" % ("深度研判" if llm_used else "Fast-Path 模式"))
    print("  total            : %d" % sum(by_verdict.values()))
    print("  sensitive        : %d" % by_verdict.get("sensitive", 0))
    print("  not_sensitive    : %d" % by_verdict.get("not_sensitive", 0))
    print("  unresolved(交人工) : %d" % result.by_category.get("UNRESOLVED", 0))
    print("  short_circuited  : %d (0 token)" % result.short_circuited)
    print("  deep_judged      : %d" % result.deep_judged)
    print("  parse_errors     : %d" % result.parse_errors)
    if result.lsp_fallback:
        print("  lsp_fallback     : %d (goto_definition 不可用, 已回落检索)" % result.lsp_fallback)
    if result.reason_counts:
        print("  unresolved 原因: %s" % json.dumps(result.reason_counts, ensure_ascii=False))


def _probe_error_hint(exc: Exception, cfg: Config) -> str:
    """Actionable hint for common probe failures (auth / key missing)."""
    from seninfo.llm.client import LLMHttpError, LLMTimeoutError

    if isinstance(exc, LLMHttpError) and exc.status in (401, 403):
        if cfg.model.api_key:
            return "(鉴权失败: 检查 key 是否正确/是否有权限)"
        return (
            "(401/403: 端点需要 API key。请设置环境变量 SENINFO_LLM_API_KEY,"
            "或在配置 model.api_key_env 指向你网关的 key 环境变量名)"
        )
    if isinstance(exc, LLMTimeoutError):
        return "(连接/超时: 确认 endpoint 可达且 /v1 前缀正确, 或调大 model.timeout_s)"
    return ""


def run_scan_cli(args: argparse.Namespace) -> int:
    started = datetime.now(timezone.utc)
    overrides = {"workspace": args.workspace_dir}
    cfg: Config = load_config(args.config, overrides=overrides)

    try:
        rules = load_rules(args.rules or cfg.rules_file)
    except Exception as exc:
        print("规则文件错误: %s" % exc, file=sys.stderr)
        return EXIT_ERROR

    try:
        text = Path(args.input).read_text(encoding="utf-8")
    except OSError as exc:
        print("输入文件不可读: %s" % exc, file=sys.stderr)
        return EXIT_ERROR

    report = PARSERS[args.tool]().parse(text)

    # LLM availability (design D5): explicit endpoint must pass a probe.
    client = None
    if cfg.llm_configured:
        try:
            client = ChatClient(cfg.model)
            client.probe()
        except LLMError as exc:
            hint = _probe_error_hint(exc, cfg)
            print("模型端点不可达: %s%s" % (exc, hint), file=sys.stderr)
            return EXIT_ERROR
    else:
        print(
            "[warn] 未配置 model.endpoint: 仅 Fast-Path 模式, 未短路条目全部交人工",
            file=sys.stderr,
        )

    workspace = Workspace(cfg.workspace, cfg.budget) if cfg.workspace else None
    if client is not None and workspace is not None:
        from seninfo.engine.preflight import print_preflight, run_preflight

        print("[lsp] 预检(报告涉及的语言):")
        statuses = run_preflight(report, download=not args.no_lsp_download)
        print_preflight(statuses)
    result = run_scan(report, cfg, rules, client=client, workspace=workspace)

    write_jsonl(args.output, result.judgments)
    manifest = build_manifest(
        cfg=cfg,
        by_verdict=result.by_verdict,
        by_category=result.by_category,
        reason_counts=result.reason_counts,
        parse_errors=result.parse_errors,
        short_circuited=result.short_circuited,
        deep_judged=result.deep_judged,
        lsp_fallback=result.lsp_fallback,
        degraded=result.degraded,
        model_id=result.model_id,
        rules_version="builtin" if not (args.rules or cfg.rules_file) else "custom",
        fast_ms=result.fast_ms,
        deep_timings_ms=result.deep_timings_ms,
        started=started,
    )
    manifest_path = str(
        Path(args.output).with_suffix(Path(args.output).suffix + ".manifest.json")
    )
    write_manifest(manifest_path, manifest)
    _print_summary(result, llm_used=client is not None)
    return EXIT_OK


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "scan":
        try:
            return run_scan_cli(args)
        except Exception as exc:
            print("工具错误: %s" % exc, file=sys.stderr)
            return EXIT_ERROR
    parser.print_help()
    return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
