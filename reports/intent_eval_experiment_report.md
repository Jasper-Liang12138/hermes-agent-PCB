# SWSD/PCB Intent 500 条实验总报告

生成时间：2026-06-13  
数据集：`F:\doctor\hermes-agent\邮件\intent_training_500.jsonl`  
仓库：`F:\doctor\hermes-agent\hermes-agent-PCB`

## 一、总体结论

| 结论项 | 结果 | 说明 |
|---|---:|---|
| 规则基线 | 52.60% | 只靠 `_decide_route()` 规则，能覆盖一部分显式意图，但对流程上下文、模糊表达、脏数据不够稳 |
| 72B raw LLM | 75.00% | 旧 `qwen2.5-72b-instruct`，`max_tokens=256`，作为原始 LLM 对照 |
| 72B + SWSD2 | 88.60% | 当前最高结果，说明 SWSD2 结构化校准有效 |
| 35B 未真正关闭思考 raw | 34.11% | `qwen3.6-35b-a3b` 在旧调用下大量输出 Thinking Process，parser 误判/无法稳定解析 |
| 35B no-think raw | 74.80% | 顶层 `enable_thinking=false` + JSON 输出约束后，35B raw 基本追平 72B raw |
| 35B no-think + SWSD2 | 86.60% | 相比 35B raw 提升 11.80 个百分点；比 72B + SWSD2 低 2.00 个百分点 |
| 4k retry 路线 | 不推荐全量使用 | 能等到 JSON，但平均耗时过高，诊断价值大于工程价值 |

**核心判断：**

| 问题 | 判断 |
|---|---|
| 新 SWSD 是否有用？ | 有用。35B no-think 下从 74.80% 提升到 86.60%；72B 下从 75.00% 提升到 88.60% |
| 35B 是否可用？ | 可用，但必须真正关闭 thinking，并要求结构化 JSON 输出 |
| 之前 35B 准确率低的主因是什么？ | 不是 35B 能力明显不足，而是 endpoint 未按预期关闭思考，导致输出 Thinking Process 干扰解析 |
| 是否应该默认扩大到 4k tokens？ | 不建议。4k 能提高等到 JSON 的概率，但速度不可接受 |

## 二、主实验结果汇总

| 实验 | 模型/方法 | 样本数 | 通过数 | 准确率 | 平均耗时 | P95 耗时 | 解析/状态 | 结论 |
|---|---|---:|---:|---:|---:|---:|---|---|
| 规则基线 | `_decide_route()` | 500 | 263 | 52.60% | - | - | 规则直出 | 可作为兜底，不足以单独承担复杂流程意图 |
| 72B raw | `qwen2.5-72b-instruct`, `max_tokens=256` | 500 | 375 | 75.00% | - | - | LLM 原始解析 | 老模型 raw 表现稳定 |
| 72B + SWSD2 | raw LLM + SWSD2 policy | 500 | 443 | 88.60% | - | - | 结构化校准 | 当前最佳完整指标 |
| 35B raw，旧调用 | `qwen3.6-35b-a3b`, `max_tokens=256` | 475 | 162 | 34.11% | - | - | 大量 Thinking Process | 失败主因是输出控制，不是分类框架本身 |
| 35B + SWSD2，旧调用 | raw partial + SWSD2 policy | 500 | 421 | 84.20% | - | - | SWSD2 规则/状态兜底 | 即使 raw 很差，SWSD2 仍能修复大量样本 |
| 35B no-think smoke | 顶层 no-think + JSON，5 条 | 5 | 4 | 80.00% | 4.487s | 5.640s | 5/5 首轮解析 | no-think 参数有效，值得全量 |
| 35B no-think raw 全量 | 顶层 no-think + JSON，500 条 | 500 | 374 | 74.80% | 4.728s | 6.000s | 499 首轮解析，1 retry | 35B raw 基本追平 72B raw |
| 35B no-think + SWSD2 全量 | raw no-think + SWSD2 policy | 500 | 433 | 86.60% | 0s | 0s | 复用 raw 结果做校准 | 工程上可用，低于 72B+SWSD2 2 点 |

## 三、35B 输出控制实验

### 3.1 max_tokens smoke 对照

| 实验 | 参数 | 样本数 | 通过数 | 准确率 | 结果解读 |
|---|---|---:|---:|---:|---|
| 35B smoke `max_tokens=128` | lean prompt | 5 | 4 | 80.00% | 小样本看似可用，但不能说明全量稳定 |
| 35B smoke `max_tokens=256` | lean prompt | 5 | 4 | 80.00% | 与 128 smoke 持平，后续按与 72B 同口径选择 256 |
| 35B full `max_tokens=256`，旧调用 | lean prompt | 475 | 162 | 34.11% | 全量暴露问题：Thinking Process 破坏 raw 分类 |

### 3.2 strict raw / streaming / adaptive 诊断

| 实验 | 样本数 | 通过数 | 准确率 | 平均耗时 | P95 耗时 | 状态 | 结论 |
|---|---:|---:|---:|---:|---:|---|---|
| strict raw 256 smoke | 5 | 0 | 0.00% | 19.341s | 19.609s | 5 unparsed | 严格只收 JSON/KV 时，旧 endpoint 不合格 |
| adaptive stream smoke | 5 | 0 | 0.00% | 3.706s | 4.375s | 5 unparsed | 当前 endpoint 的 stream content 不可用 |
| adaptive stream smoke v2 | 5 | 0 | 0.00% | 3.475s | 3.703s | 5 unparsed | streaming 剪枝无法解决该 endpoint 的内容缺失 |
| adaptive stream fallback smoke | 5 | 0 | 0.00% | 58.200s | 59.375s | 5 unparsed | fallback 也不能稳定拿到结构化答案 |
| retry 4k probe | 3 | 3 | 100.00% | 140.692s | 192.500s | 3 retry parsed | 4k 能等到 JSON，但太慢 |
| retry 4k full partial | 5 | 4 | 80.00% | 208.572s | 284.859s | 5 retry parsed | 满足“能吐 JSON”，但不满足工程成本 |

**诊断结论：**

| 路线 | 是否解决问题 | 是否推荐 |
|---|---|---|
| 单纯扩大 `max_tokens` 到 4k | 部分解决解析问题 | 不推荐主线，耗时过高 |
| streaming early-stop | 未解决 | 不推荐用于当前 endpoint，因为 stream 不返回有效 content |
| strict raw 256 | 暴露问题有效 | 可作为评测约束，但旧调用下不可用 |
| 顶层 no-think + JSON response_format | 解决问题 | 推荐作为 35B intent 分类主线 |

## 四、no-thinking 参数探针

探针目标：确认 wishub 35B endpoint 到底支持哪种 per-request 无思考/JSON-only 控制参数。

| Variant | HTTP 成功 | JSON 有效 | 是否含 Thinking | 耗时 | 判定 |
|---|---:|---:|---:|---:|---|
| baseline | 是 | 是 | 是 | 10.860s | 不可用，仍有 Thinking |
| `/no_think` prefix | 是 | 是 | 是 | 12.734s | 不可用 |
| `chat_template_kwargs.enable_thinking=false` | 是 | 是 | 是 | 9.953s | 不可用 |
| prefix + chat_template_kwargs | 是 | 是 | 是 | 10.078s | 不可用 |
| 顶层 `enable_thinking=false` | 是 | 是 | 否 | 5.454s | 可用 |
| extra_body style | 是 | 是 | 否 | 3.093s | 可用 |
| `reasoning.enabled=false` | 是 | 是 | 是 | 9.953s | 不可用 |
| `reasoning.effort=none` | 是 | 是 | 是 | 10.110s | 不可用 |
| `response_format={"type":"json_object"}` | 是 | 是 | 否 | 3.047s | 可用 |
| response_format + chat_template_kwargs | 是 | 是 | 否 | 3.047s | 可用 |
| stop `Thinking Process` | 是 | 否 | 否 | 1.515s | 不可用，会截断答案 |
| assistant JSON prefill | 是 | 否 | 是 | 5.297s | 不可用 |

**可用参数优先级：**

| 优先级 | 参数组合 | 原因 |
|---:|---|---|
| 1 | `response_format={"type":"json_object"}` + 顶层 `enable_thinking=false` | 最稳，既限制输出格式，又关闭思考 |
| 2 | 顶层 `enable_thinking=false` | 明确解决 Thinking Process |
| 3 | 单独 `response_format={"type":"json_object"}` | 也能压住 Thinking，但语义上不如显式 no-thinking 完整 |

## 五、SWSD2 收益对比

| 模型/调用方式 | Raw 准确率 | SWSD2 准确率 | 绝对提升 | 相对说明 |
|---|---:|---:|---:|---|
| 72B `qwen2.5-72b-instruct` | 75.00% | 88.60% | +13.60 | SWSD2 在强 raw 模型上仍有明显收益 |
| 35B 旧调用 | 34.11% | 84.20% | +50.09 | raw 被 Thinking Process 严重污染，SWSD2 靠规则/状态大量修复 |
| 35B no-think | 74.80% | 86.60% | +11.80 | 输出控制恢复后，SWSD2 提供稳定结构化增益 |

## 六、关键工程发现

| 发现 | 证据 | 工程含义 |
|---|---|---|
| `chat_template_kwargs.enable_thinking=false` 对当前 35B endpoint 不够 | 探针中该 variant 仍含 Thinking | 不能只依赖模板参数 |
| 顶层 `enable_thinking=false` 有效 | 探针中 Thinking=false，且全量 500 条 499 条首轮解析 | 应作为 tool-planning intent 请求的关键参数 |
| JSON response_format 有效 | 探针中 response_format 相关 variant 均无 Thinking | intent 评测应强制结构化输出 |
| 4k retry 能提高解析率但极慢 | 3 条 probe 平均 140.692s，full partial 平均 208.572s | 只适合诊断，不适合作为线上或大规模评测默认策略 |
| SWSD2 能处理 raw 模型噪声 | 35B 旧调用 raw 34.11%，SWSD2 84.20% | State-constrained policy 和 ambiguity resolver 有实际价值 |

## 七、当前推荐方案

| 场景 | 推荐配置 | 理由 |
|---|---|---|
| 日常 intent 评测 | 35B no-think + JSON + `max_tokens=256` | 速度快，raw 74.80%，可复现 |
| 最终系统指标 | 35B no-think + SWSD2 | 86.60%，成本低，接近 72B+SWSD2 |
| 追求最高准确率 | 72B + SWSD2 | 88.60%，目前最高 |
| 诊断 endpoint 输出问题 | no-think probe | 能快速定位是模型能力问题还是输出控制问题 |
| 兜底实验 | 4k retry 小样本 probe | 仅判断“是否最终能吐 JSON”，不建议全量 |

## 八、后续建议

| 优先级 | 建议 | 预期收益 |
|---:|---|---|
| P0 | 保持 tool-planning intent 路径使用顶层 `enable_thinking=false` 和 JSON 输出约束 | 避免 35B 回到 Thinking Process 污染状态 |
| P0 | SWSD2 报告中保留 raw source：`json/kv/unparsed/rule_fallback` | 便于区分模型错、解析错、policy 错 |
| P1 | 对 35B no-think + SWSD2 的 67 条失败样本做分桶 | 找出是否集中在 `flow_invalid`、`unclear_fuzzy`、`wait_confirm` 等类别 |
| P1 | 对 SWSD2 policy 做小步迭代，而不是继续扩大 token | 成本更低，解释性更好 |
| P2 | 如果要追 72B+SWSD2 的 88.60%，优先优化状态约束和歧义澄清样本 | 当前差距只有 2 个点，可能主要来自边界规则 |
| P2 | 保留 4k retry 为手工诊断开关 | 避免全量评测被极慢样本拖垮 |

## 九、报告来源文件

| 内容 | 文件 |
|---|---|
| 72B rule/raw/SWSD2 | `reports/intent_eval/rule_eval.json`, `reports/intent_eval/llm_eval.json`, `reports/intent_eval/swsd2_eval.json` |
| 35B 旧调用 full | `reports/intent_eval_qwen36_35b_a3b_mt256/llm_eval.json`, `reports/intent_eval_qwen36_35b_a3b_mt256/swsd2_eval.json` |
| 35B smoke | `reports/intent_eval_qwen36_35b_a3b_mt128_smoke5/llm_eval.json`, `reports/intent_eval_qwen36_35b_a3b_mt256_smoke5/llm_eval.json` |
| strict/stream/adaptive 诊断 | `reports/intent_eval_qwen36_35b_a3b_strict256_smoke5/`, `reports/intent_eval_qwen36_35b_a3b_adaptive_stream_smoke5*/` |
| 4k retry 诊断 | `reports/intent_eval_qwen36_35b_a3b_retry4k_probe/llm_eval.json`, `reports/intent_eval_qwen36_35b_a3b_retry4k_full/llm_eval.partial.json` |
| no-think 探针 | `reports/no_think_probe/qwen36_35b_probe.json`, `reports/no_think_probe/qwen36_35b_probe.md` |
| 35B no-think 全量 | `reports/intent_eval_35b_nothink_full/llm_eval.json`, `reports/intent_eval_35b_nothink_full/swsd2_eval.json` |

