# seninfo-agent — Phase 1 引擎

范围依据 `docs/prd-v2.md`:只做"静态扫描输出 → 逐条研判 → 结构化裁决结果导出",不做人工审核 UI、不做缺陷流转、不阻断 CI。

## 目录结构

```
src/seninfo/
  models.py            # 归一化告警模型 / 裁决 Judgment / 分类枚举
  redaction.py         # 凭据半脱敏(首尾各 4 字符)
  entropy.py           # 香农熵(仅作 LLM 辅助特征,不单独裁决)
  config.py            # 配置(JSON / 环境变量);安全默认(endpoint 必须显式)
  rules.py             # 确定性规则(内置默认 + JSON 覆盖),短路判定
  exporter.py          # JSONL 结果 + manifest 统计
  parsers/
    base.py            # Parser 接口 + ParsedReport(含逐条 parse error)
    trufflehog.py      # TruffleHog JSONL
    gitleaks.py        # Gitleaks JSON / SARIF
  engine/
    fastpath.py        # 确定性短路(verified / 规则) → 0 Token
    tools.py           # 只读工作区:read_file_range / search_codebase + 路径越界防护
    lsp.py             # LSP 客户端(stdio JSON-RPC),goto_definition;注册表驱动,缺失回落
    preflight.py       # LSP 预检:报告语言→缺哪些→自动下载(默认)或 --no-lsp-download 关闭
    lsp_installer.py   # LSP 托管下载:npm --prefix 装到 ~/.cache/seninfo/lsp,默认开启
    deep.py            # 深度研判:工具循环 + Judge + Critic 复议 + UNRESOLVED 兜底
    aggregator.py      # 同密文多命中聚合(ACC-07)
    orchestrator.py    # 逐条流水线:fastpath → deep → aggregate → 统计
  llm/
    client.py          # OpenAI 兼容 /chat/completions(零依赖 urllib)
    schema.py          # Judge/Critic 结构化输出 Schema 强校验
    prompts.py         # 防注入上下文装配(<untrusted_code_context>)与提示词
  cli.py               # seninfo-agent scan(非阻断退出码: 0 正常 / 2 工具错误)
tests/
  test_core.py         # 解析/脱敏/规则/短路 单元
  test_phase1.py       # config/tools/deep/聚合/编排/LSP 用例
  run_all.py           # 零依赖测试运行器(无 pytest 环境)
```

## 实现状态

- ✅ 两家解析(JSONL / JSON / SARIF)、归一化、脱敏、熵计算
- ✅ 确定性短路(ACC-01/02/03/04/05,0 Token)
- ✅ 深度研判:受控工具循环 + LSP goto_definition + 防注入 + Schema 强约束 + Critic 复议(ACC-06/08/09)
- ✅ LSP:注册表驱动 **13+ 语言默认生效**(`src/seninfo/lsp_registry.py`),**缺服务器自动下载**(npm 缓存到 `~/.cache/seninfo/lsp`,默认开启;`SENINFO_LSP_AUTO=0`/`--no-lsp-download` 关闭);stdio JSON-RPC 客户端 + goto_definition;扫描前预检;配置不再含 tools.lsp
- ✅ 失败兜底:任何未决条目 → `UNRESOLVED`(sensitive,交人工),原因词表入 manifest(ACC-08/10/11),**不静默放行、不因结果失败退出**
- ✅ 聚合去重(ACC-07);编排并发;CLI 0/2 退出码
- ✅ 零运行时依赖;测试 = 44 通过(Fast-Path + 深度链路 + **ACC-01~12 端到端** + LSP 注册表/预检,见 `tests/run_all.py`)
- 🧪 真实模型定标手册:`docs/runbook-phase1-validation.md`(样例仓 `tests/fixtures/workspaces/acc-06-minimal/` 已就绪)

## 尚未落地(依赖真实模型/镜像环境)

- ⏳ 真实 LLM 端到端验证与 P95 < 6s 定标(需内网 vLLM/Ollama 端点,参考 `config.example.json`)
- ⏳ Token 基线对比(PRD 60% 削减口径)
- ⏳ Runner 镜像预装 pyright-langserver / clangd(否则 goto_definition 自动回落检索)

## 快速开始(零依赖)

```bash
# 1) Fast-Path 模式(不配模型端点,未短路条目全部 UNRESOLVED 交人工)
PYTHONPATH=src python3 -m seninfo scan \
  --input tests/fixtures/sample_trufflehog.jsonl \
  --tool trufflehog \
  --output /tmp/report.jsonl

# 2) 深度研判(配置内网模型端点;参考 config.example.json)
PYTHONPATH=src python3 -m seninfo scan \
  --input tests/fixtures/sample_trufflehog.jsonl \
  --tool trufflehog \
  --workspace-dir . \
  --config config.example.json \
  --output /tmp/report-deep.jsonl

# 3) 常规安装后使用入口
pip install -e .
seninfo-agent scan --input ... --tool trufflehog --output report.jsonl

# 4) 测试(无 pytest 时)
PYTHONPATH=src:tests python3 tests/run_all.py
```

## 文档

- **用户教程(统一版,含 C/C++/Python/Java LSP 部署)**:`docs/tutorial-user.md`
- 开发与协作说明:`CONTRIBUTING.md`
- 验收用例:`docs/acceptance-cases.md` ｜ 架构与模块设计:`docs/design-phase1.md` ｜ PRD:`docs/prd-v2.md` ｜ 真实环境验收:`docs/runbook-phase1-validation.md`
