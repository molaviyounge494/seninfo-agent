# 真实环境验收清单(Runbook)— 原生 DeepEngine 定标

> 目标:在有内网模型的环境下,把 ACC-06/07 深度链路 + 性能指标跑出真实数据。
> 前置:Python ≥3.9;内网模型服务(vLLM 或 Ollama,OpenAI 兼容 `/v1`);可选 `pyright-langserver`/`clangd`。
> 离线单测(无模型)随时可跑:`PYTHONPATH=src:tests python3 tests/run_all.py`(40 用例)。

## 0. 样例仓与输入(已就绪)

```
tests/fixtures/workspaces/acc-06-minimal/   # 最小生产样例仓
  config/prod.py        # 第 4 行硬编码 DB 口令(定义点)
  services/db.py        # 第 6 行同值口令(引用点)
tests/fixtures/inputs/acc-06_trufflehog.jsonl  # 指向 config/prod.py:4 的扫描告警
```

## 1. 起内网模型

- vLLM:`vllm serve <model> --port 8000`(原生 OpenAI 兼容 `/v1/chat/completions`),确认 `curl http://127.0.0.1:8000/v1/models`;
- Ollama:`ollama serve` + `ollama pull <model>`,用其 OpenAI 兼容 `/v1`(注意此端点不暴露 `/v1/models` 时,须在配置里显式写 `judge_model`)。

## 2. 配置

```bash
cp config.example.json /tmp/seninfo.json
# 编辑: model.endpoint = 内网地址; model.judge_model = 模型名(必填,除非 /v1/models 可探测)
```

## 3. 深度链路验收(ACC-06/07)

```bash
PYTHONPATH=src python3 -m seninfo scan \
  --input tests/fixtures/inputs/acc-06_trufflehog.jsonl \
  --tool trufflehog \
  --workspace-dir tests/fixtures/workspaces/acc-06-minimal \
  --config /tmp/seninfo.json \
  --output /tmp/acc06.jsonl
```

**通过标准**
1. 退出码 0;
2. `/tmp/acc06.jsonl` 单条记录:`verdict=sensitive`、`category=LIKELY_REAL`、`confidence≥0.6`(建议≥0.8);
3. `evidence` 含 `tool_trace`(模型确实调了工具)与 `model_reason`,最好含 `goto_definition`(装了 LSP 时);
4. manifest:`counts.by_category.LIKELY_REAL=1`,`unresolved_reasons` 为空或仅少量 `low_confidence`;
5. **脱敏**:输出不含完整密文(`grep sk-live-prod /tmp/acc06.jsonl` 只应命中 masked 形态)。

ACC-07(聚合):把同一密文的另一处命中(services/db.py:6)追加进输入再跑,应输出**单条**记录、`locations` 含两处、evidence 带 `aggregation`。

## 4. LSP 行为核对

- 未装 `pyright-langserver`:manifest `stages.lsp_fallback=1`(goto 自动回落),属预期;
- 装了之后重跑:`lsp_fallback=0`,evidence 里出现 `goto_definition` 轨迹。
- 语言服务器安装(供 Runner 镜像与本地):pyright(`npm i -g pyright-langserver`)/ pylsp(`pip install python-lsp-server`)二选一;clangd 由发行版包或 LLVM 提供。

## 5. 全量 ACC 冒烟(可选,建议)

用样例报告(gitleaks JSON/SARIF、trufflehog JSONL、混入 1 条坏行)各跑一遍 Fast-Path 与深度模式,核对 ACC-01~12 预期:
- 短路类(01-05)0 Token、即时返回;
- 深度类(06-09)在模型下给出与离线脚本一致的分类倾向(不强求逐字一致);
- 健壮性类(10-11):坏行占位 UNRESOLVED、无 workspace 时 manifest `degraded_mode=true` 且全部未决项交人工;
- 12(SARIF):已知限制——SARIF 不含密文,`secret_masked=""` 时按信息不足处理,不会静默放行。

## 6. 性能与成本定标(对照 PRD §6.1)

```bash
# 观察 manifest: stages.* 与 timings_ms.deep_p50/deep_p95
python3 -m json.tool /tmp/acc06.jsonl.manifest.json
```

| 指标 | PRD 目标 | 记录位置 |
|---|---|---|
| 短路单条 | <50ms | `timings_ms.fast_path_total`(整批) |
| 深度 P95 | <6s/条 | `timings_ms.deep_p95`(本版为按 finding 的均值近似,Phase 3 改逐条计时) |
| Token 削减 ≥60% | 未实现基线模式 | `token_usage` 当前未逐条记账(Phase 1 留白,Phase 3 eval 补) |

调参入口(重新定标用):`budget.read_radius`(上下文行数)、`tool_rounds`、`workers`、模型 `temperature=0`;低置信阈值(0.6)在 `engine/deep.py::_finalize`。

## 7. 常见问题

| 现象 | 原因/处理 |
|---|---|
| 大量 `schema_invalid` | 模型结构化输出弱:确认 `temperature=0`、换更强模型、或调大 `budget.schema_retries` |
| 大量 `low_confidence` | 正常保守行为;检查上下文是否足够(`read_radius`)、模型是否被半脱敏密钥误导(属预期,靠 detector+路径判) |
| `model_timeout` | endpoint/网络/超时:调 `model.timeout_s`;确认 `/v1` 前缀正确(vLLM 需含 `/v1`) |
| LSP 永远 fallback | 语言服务器未装或不在 PATH;`.ts/.js` 属 Phase 2(LSP 未覆盖→回落检索是设计内行为) |
| 输出仍见明文 | 违反 PRD 附录 A,视为 bug:检查 `--output` 与 manifest,上报 |

## 8. 记录与反馈

跑完把 `/tmp/acc06.jsonl.manifest.json` 与判定样例贴回,用于:① 校准默认规则命中率;② 决定 7.2 建议指标是否调整;③ 需要时再评估"AgentBackend + Pi 双后端 A/B"(当前决策:原生优先)。
