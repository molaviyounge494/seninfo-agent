# CONTRIBUTING — seninfo-agent 开发与协作说明

> 定位:仓库级开发说明。产品范围见 `docs/prd-v2.md`,架构/模块/演进见 `docs/design-phase1.md`,验收用例见 `docs/acceptance-cases.md`,用户操作见 `docs/tutorial-user.md`。

## 1. 这是什么 / 不是什么

**做**:静态扫描报告 → 逐条研判(确定性短路 + 深度溯源 + LLM 裁决)→ 结构化裁决结果导出。
**不做**:人工审核 UI、缺陷流转、CI 阻断、在线凭据验证。

## 2. 代码仓布局

```
src/seninfo/
  models.py            # 归一化告警 / 裁决 / 分类枚举(领域契约)
  redaction.py entropy.py rules.py config.py exporter.py
  parsers/             # 各家扫描器 → NormalizedFinding(Parser 接口)
  engine/
    fastpath.py        # 确定性短路(0 token)
    deep.py            # 工具循环 + Judge + Critic + UNRESOLVED 兜底
    tools.py           # 只读工作区(越界防护唯一实现点)
    lsp.py             # 极简 LSP 客户端(definition)
    lsp_registry.py    # 语言注册表(后缀→命令→安装源)
    lsp_installer.py   # LSP 默认自动下载(npm --prefix → 缓存)
    preflight.py       # 扫描前预检 + 自动补装
    aggregator.py      # 同密文聚合
    orchestrator.py    # 逐条流水线与统计
  llm/                 # client/schema/prompts(模型接入层)
docs/ tests/ config.example.json
```

## 3. 开发环境(零依赖)

```bash
# 不需要 pip install:纯标准库,直接跑
PYTHONPATH=src python3 -m seninfo scan --input tests/fixtures/sample_trufflehog.jsonl \
  --tool trufflehog --output /tmp/report.jsonl

# 跑全部测试(无 pytest 也可;有 pytest 时 `pytest tests/` 等价)
PYTHONPATH=src:tests python3 tests/run_all.py     # 期望: N passed, 0 failed
```

设计目标:**零运行时依赖**。新增代码除非有充分理由(如 Phase 2 服务层引入 pydantic/FastAPI),否则只用标准库。

## 4. 不可破坏的纪律(评审红线)

1. **确定性走代码,语义不确定才走 LLM**:解析/脱敏/短路/聚合/导出/预算不得 agent 化。
2. **绝不静默放行**:任何无法判定的条目 → `sensitive/UNRESOLVED`,原因词表入 evidence.detail.reason_code;退出码与判定解耦(0=成功,2=工具错误)。
3. **只读沙盒**:文件访问必须经 `Workspace.resolve`(路径越界防护唯一实现点);不新增 shell/写/任意网络工具。
4. **脱敏前置**:密文进日志/模型/存储/导出前一律 `mask_secret`;密钥只走环境变量,禁止明文进配置与代码。
5. **Schema 强校验**:LLM 输出必须过 `llm/schema.py` 校验,失败→重试→UNRESOLVED。
6. **配置不含 LSP/密钥**:语言服务器由 `lsp_registry.py` 注册表 + 自动下载管理;`config.json` 已 gitignore。

## 5. 常见扩展点(改哪)

| 需求 | 改哪里 | 注意 |
|---|---|---|
| 支持新扫描器 | `parsers/` 新增 Parser 并注册进 `PARSERS` | 产出同构 `NormalizedFinding`;解析失败逐条进 `issues` |
| 支持新语言 LSP | `lsp_registry.py` 加一条 `LspDefaults` | 含 language_id/默认命令/suffixes/npm 源/提示 |
| 改内置规则 | `rules.py` 的 `DEFAULT_RULES` | 规则必须可解释(evidence 带 rule_id);路径/关键字/正则三类 |
| 加分类/裁决语义 | `models.py` + `llm/schema.py` 联动 | category↔verdict 一致性校验同步改 |
| 加工具 | `engine/tools.py`/`deep.py` `_dispatch` + 提示词 | 保持只读;预算记账同步 |
| 加 ACC 用例 | `tests/e2e_acc.py` + `docs/acceptance-cases.md` | 深度场景用 `FakeClient` 脚本驱动,离线可跑 |

## 6. 测试要求

- 任何改动后必跑 `tests/run_all.py`,保持全绿;新增功能必须带测试。
- ACC-01~12(`tests/e2e_acc.py`)对应验收用例文档,短路类断言分类,深度类断言证据与兜底。
- 深度用例不依赖真实模型(脚本化 LLM);真实模型定标走 `docs/runbook-phase1-validation.md`。

## 7. 提交规范

- 提交信息用描述性标题 + 要点列表(中文或英文皆可,保持简洁):
  `seninfo-agent: <简述>` 开头。
- **提交前自查**:
  ```bash
  git diff --cached --name-only          # 不应出现 config.json / *.pyc
  git diff --cached --check              # 无空白错误
  # 密钥自查(示例):
  git grep -nE "sk-[A-Za-z0-9]{20,}"     # 无真实 key(测试 fixture 的假 key 除外)
  ```
- `config.json`、`__pycache__/`、`*.pyc` 已被 .gitignore;不要把个人配置/密钥/产物提交上去。

## 8. 分支与 PR(建议)

- 主干 `main` 直接推送或 PR 均可(仓库规模小);建议小步提交、一事一提交。
- 破坏纪律第 4 节的行为会在 review 阶段打回。

## 9. 已知边界(别踩坑)

- LSP 真实联调需要先装语言服务器:自动下载默认开(npm 源),离线设 `SENINFO_LSP_AUTO=0`。
- 本环境无法装 pytest,测试走 `tests/run_all.py`;装了 pytest 后两者等价,用例均为无参纯函数。
- Windows 下 `node_modules/.bin` 解析为 `.cmd/.exe` 变体,已兼容;clangd/jdtls/gopls 等无 npm 源的语言仍需手动安装(预检会提示)。
