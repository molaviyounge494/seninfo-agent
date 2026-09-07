# 验收测试用例(可执行基准)— seninfo-agent

> 关联文档:`docs/prd-v2.md` §7.1(12 个典型场景)。本文件把每个场景展开为**可执行的测试用例规格**:输入构造、仓库前置、期望输出与断言点。
>
> 约定:每个用例最终应固化为 pytest 端到端测试(CLI + fixtures),fixture 目录约定见文末。

---

## 用例总表(ACC-01 ~ ACC-12)

| ID | PRD §7.1 | 场景 | Phase |
|---|---|---|---|
| ACC-01 | #1 | TruffleHog `Verified: true` 直通 | 1 |
| ACC-02 | #2 | 规则库占位符关键字 | 1 |
| ACC-03 | #3 | 测试目录 mock key | 1 |
| ACC-04 | #4 | README / 文档示例 | 1 |
| ACC-05 | #5 | 前端构建哈希指纹 | 1 |
| ACC-06 | #6 | 生产代码硬编码真凭据(需溯源) | 1 |
| ACC-07 | #7 | 跨文件引用聚合 | 1 |
| ACC-08 | #8 | 模型端点超时 → UNRESOLVED | 1 |
| ACC-09 | #9 | 无 LSP 语言回落 search_codebase | 2 |
| ACC-10 | #10 | 损坏输入行 → 占位 UNRESOLVED | 1 |
| ACC-11 | #11 | 无 `--workspace-dir` 降级模式 | 1 |
| ACC-12 | #12 | Gitleaks SARIF 输入 | 1 |

---

## 公共断言(所有用例通用)

1. 退出码 = 0(除非标注"工具错误用例");判定结果**不**影响退出码。
2. 输出每条含:PRD 4.3.1 全部必填字段(`verdict/category/confidence/evidence/locations/secret_masked`…),`secret_masked` 不含密文主体(仅首尾各 4 字符)。
3. `manifest` 统计完整:`total / sensitive / not_sensitive / unresolved / short_circuited / token_usage / duration`。
4. 日志与结果文件中不出现完整明文(检查 `secret_raw` 不出现在任何输出载体)。
5. `evidence[]` 非空,短路用例须含命中规则 ID 或 `verified=true` 条目。

---

## ACC-01:TruffleHog Verified 直通

- **输入**:TruffleHog JSONL,1 条 `AWS` detector,`Verified: true`,`Raw=AKIAIOSFODNN7EXAMPLE`,文件 `src/secrets_prod.py:5`。
- **前置**:无需仓库。
- **期望**:
  - `verdict=sensitive`,`category=VERIFIED_LIVE`,`confidence=1.0`
  - `token_usage=0`,`short_circuited=1`
  - `evidence` 含类型 `verified` 条目
- **边界**:`Verified: false` 不得走此分支(须进入规则/深度链路)。

## ACC-02:规则库占位符关键字

- **输入**:Gitleaks JSON,`Secret="sk-test-your_token_here-abc"`,`README.md:12`。
- **期望**:`verdict=not_sensitive`,`category=DOCUMENTATION`,0 Token;`evidence` 含命中规则 ID(如 `docs-placeholder-keyword`)。
- **补充**:断言"命中规则但模型可能误判为真"的用例同样输出 DOCUMENTATION(规则优先于模型)。

## ACC-03:测试目录 mock key

- **输入**:TruffleHog JSONL,`tests/fixtures/api_keys.json:3`,`Raw="dummy-token-1234"`,`Verified: false`。
- **仓库前置**:fixtures 工作区含该测试目录(可选)。
- **期望**:`not_sensitive / TEST_OR_MOCK`;路径类规则优先于关键字类规则,`evidence` 记录命中的路径规则 ID。
- **边界**:若同值密钥同时出现在 `tests/` 与 `prod/config.py`,不得仅凭 tests 命中放行(聚合场景见 ACC-07)。

## ACC-04:README 文档示例

- **输入**:`docs/api-example.md`,`Raw="https://user:pass@example.com"` 或 `sk-...example` 形态。
- **期望**:`not_sensitive / DOCUMENTATION`。
- **边界**:`examples/` 目录 + `.env.example` 文件须归 DOCUMENTATION,而非 FALSE_POSITIVE。

## ACC-05:前端构建哈希指纹

- **输入**:`dist/app.8f3a2c9e.js` 或 `.next/static/...`,命中"高熵哈希"类告警。
- **期望**:`not_sensitive / FALSE_POSITIVE`。
- **边界**:同形态哈希出现在 `src/lib/hash_utils.ts` 内联常量时不得放行(真场景依赖语义,不能只靠形态规则)。

## ACC-06:生产代码硬编码真凭据(深度链路核心)

- **输入**:`config/prod.py:41` 硬编码 DB 口令字符串,dB 口令被 `services/db.py:12` import 使用。
- **仓库前置**:最小样例仓(两个文件 + import 关系)。
- **期望**:`sensitive / LIKELY_REAL`,`confidence≥0.8`;`evidence` 必须含 `goto_definition` 或等价溯源轨迹(证明"不是测试/文档路径")。
- **自动化关注点**:该用例是引擎准确率的主基准,人工标注为"真实敏感"。

## ACC-07:跨文件引用聚合

- **输入**:同一 `Raw` 值在 `config/prod.py:41`(定义)与 `services/db.py:12`(引用)各命中 1 条。
- **期望**:输出**一条**裁决记录,`locations` 含两处;溯源链标记引用点 `via=goto_definition`。
- **校验**:总记录数 = 1(去重生效),而非 2。

## ACC-08:模型端点超时

- **输入**:任意一条需深度研判的告警;mock LLM 端点返回超时。
- **期望**:`verdict=sensitive`,`category=UNRESOLVED`,`evidence` 标注原因 `model_timeout`;退出码 0;manifest 记录 `unresolved` 计数与原因分布。
- **原则断言**:任何失败路径绝不产生 `not_sensitive`(不静默放行)。

## ACC-09:无 LSP 语言回落(Phase 2)

- **输入**:`services/payment.ts` 内 TS 密钥;Phase 2 未配 TS LSP(模拟)。
- **期望**:引擎回落 `search_codebase` 仍产出裁决;manifest 标记语言回落(`lsp_fallback=true`)。
- **自动化关注点**:回落路径与 LSP 路径结论一致性抽样。

## ACC-10:损坏输入行

- **输入**:JSONL 5 行中第 3 行非合法 JSON。
- **期望**:第 3 行产出 1 条 `UNRESOLVED` 占位(`finding_id` 以 `parse-error:` 前缀),其余 4 行正常;`manifest.parse_errors=1`;退出码 0。

## ACC-11:无 `--workspace-dir` 降级模式

- **输入**:ACC-06 同款告警,但不传 workspace。
- **期望**:不做溯源;`manifest.degraded_mode=true`;仅凭报告上下文可确定者出结果,其余 `UNRESOLVED`。
- **原则断言**:降级模式比完整模式**更保守**(不因缺上下文而放行)。

## ACC-12:Gitleaks SARIF 输入

- **输入**:Gitleaks `--report-format sarif` 产物(≥1 条 result)。
- **期望**:与 Gitleaks JSON 输入产出同构的 `NormalizedFinding`(字段映射见 PRD 附录 C),后续判定链路一致。
- **已知限制**:SARIF 通常不含 Secret 明文,`secret_raw` 为空时按"信息不足"处理(不静默放行,走 UNRESOLVED 或依赖 message 解析——实现时按附录 C 标注)。

---

## 工具错误用例(补充,不属于 §7.1 但须覆盖)

| ID | 场景 | 期望 |
|---|---|---|
| ACC-E1 | `--input` 文件不存在 | 退出码 2,stderr 说明,无部分输出污染 |
| ACC-E2 | `--tool` 与文件内容不匹配 | 解析 0 条 + 错误信息,退出码 2 |
| ACC-E3 | `--rules` 文件非法 JSON | 退出码 2,明确规则文件错误 |
| ACC-E4 | 输出目录不可写 | 退出码 2 |

---

## Fixture 与目录约定(供自动化实现)

```
tests/fixtures/
  inputs/            # 各家扫描器原始输出: trufflehog/*.jsonl, gitleaks/*.json, gitleaks/*.sarif
  workspaces/        # 样例仓库: acc-06-minimal/ 等(含 import 关系、mock、dist 产物)
  rules/             # 自定义规则: custom_rules.json
  golden/            # Phase 3: 人工标注 golden set(verdict/category/evidence 要求)
tests/e2e/           # CLI 端到端用例(每条 ACC 一个 *_test.py)
tests/unit/          # 单元: 脱敏/熵/解析映射/规则匹配/聚合
```

- 每个 ACC 用例的断言同时覆盖:输出字段、manifest 统计、脱敏约束、退出码、Token(短路用例 = 0)。
- ACC-06/07 依赖最小样例仓,其余用例尽量用"纯输入"构造以降低维护成本。
