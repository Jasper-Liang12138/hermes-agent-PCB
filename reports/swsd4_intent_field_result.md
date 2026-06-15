# SWSD4 Intent Field + Skill Grounding 实现结果

生成时间：2026-06-14

## 实现概览

| 模块 | 状态 | 说明 |
|---|---|---|
| Intent Field Model | 已实现 | `chat/analyze/execute/meta/uncertainty` 概率输出 |
| Semantic Encoder | 已实现 | 使用 `[tool-planning-chat-model]` 和 `STAGE_TOOL_PLANNING_CHAT` |
| Skill Memory Grounding | 已实现 | 首版读取本地 PCB hardware skills |
| Probabilistic Decision Policy | 已实现 | 只使用概率、workflow state、skill grounding |
| WebSocket 接入 | 已实现 | 默认 `swsd4`，支持回退 `swsd3/swsd2` |
| 评测脚本 | 已实现 | 新增 `--mode swsd4` 和 `--mode compare-policies` |

## 架构链路

```text
Raw Route Candidate
↓
Intent Field Estimate
↓
Skill Grounding
↓
Probabilistic Decision Policy
↓
Existing Workflow Adapter
```

## 验证结果

| 验证项 | 结果 |
|---|---|
| SWSD4/core/eval/WebSocket/runtime 聚焦测试 | 178 passed |
| SWSD4 fallback 500 条评测 | 391/500 = 78.20% |
| SWSD4 encoder smoke 5 条 | 4/5 = 80.00% |

## 重要说明

| 现象 | 解释 |
|---|---|
| fallback 500 条只有 78.20% | 该模式没有调用 semantic encoder，只用 raw candidate prior，是兜底基线，不代表 SWSD4 完整能力 |
| encoder smoke 有 timeout | 当前 endpoint 对额外 intent-field 调用仍不稳定，5 条中 3 条走了 timeout fallback |
| SWSD3 仍保留 | 作为 legacy fallback 和原 500 条高分回归线，避免 SWSD4 首版影响线上安全 |

## 当前推荐

| 用途 | 推荐 |
|---|---|
| 线上安全默认 | `swsd4` + encoder 失败回退 `swsd3` |
| 原 500 条指标展示 | 继续展示 SWSD3 `499/500 = 99.80%` |
| 泛化研究指标 | 使用 SWSD4，并新增 paraphrase/OOD 数据集 |
| 后续优化 | 降低 encoder timeout，或让 raw LLM 一次性同时输出 route candidate + intent field |

