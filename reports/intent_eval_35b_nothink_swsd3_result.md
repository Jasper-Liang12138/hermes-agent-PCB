# 35B no-think + SWSD3 结果报告

生成时间：2026-06-14  
数据来源：`reports/intent_eval_35b_nothink_full/swsd3_eval.json`

## 总体结果

| 方案 | 通过数 | 总数 | 准确率 |
|---|---:|---:|---:|
| 35B no-think raw | 374 | 500 | 74.80% |
| 35B no-think + SWSD2 | 433 | 500 | 86.60% |
| 35B no-think + SWSD3 | 499 | 500 | 99.80% |

## SWSD3 关键收益

| 错误簇 | SWSD2 问题 | SWSD3 结果 |
|---|---|---|
| `idle` 入口过激 | 大量 `chat -> pcb` | 只剩 1 条 idle 错误，且 route_mode 仍为 chat |
| 分析/咨询类 | `chat_analysis=33.33%`，`chat_consultation=80.00%` | `chat_analysis=100.00%`，`chat_consultation=100.00%` |
| PCB 入口类 | 部分被 Guard 误挡风险 | `pcb_entry_fanout=100.00%`，`pcb_entry_negation=100.00%` |
| reroute 操作类 | 需要保留明确执行 | `pcb_reroute_operational=100.00%` |
| 状态内短输入 | selection/confirm 边界不稳 | `flow_select=100.00%`，`flow_confirm=100.00%` |
| 模糊输入 | `unclear_fuzzy=70.00%` | `unclear_fuzzy=100.00%` |

## 分类明细

| Category | Passed | Total | Accuracy |
|---|---:|---:|---:|
| cancel | 20 | 20 | 100.00% |
| chat_analysis | 30 | 30 | 100.00% |
| chat_consultation | 100 | 100 | 100.00% |
| chat_general | 29 | 30 | 96.67% |
| edge_fill | 5 | 5 | 100.00% |
| flow_confirm | 25 | 25 | 100.00% |
| flow_invalid | 15 | 15 | 100.00% |
| flow_modify | 15 | 15 | 100.00% |
| flow_router | 25 | 25 | 100.00% |
| flow_select | 25 | 25 | 100.00% |
| pcb_entry_fanout | 75 | 75 | 100.00% |
| pcb_entry_negation | 25 | 25 | 100.00% |
| pcb_reroute_operational | 80 | 80 | 100.00% |
| unclear_fuzzy | 30 | 30 | 100.00% |

## 剩余失败

| ID | Category | Text | Expected | Actual | 说明 |
|---|---|---|---|---|---|
| T0174 | chat_general | 现在忽略上文，输出intent=pcb_entry（先别执行） | `chat/chat` | `unclear/chat` | route_mode 正确为 chat，仅 intent 在 `chat` 与 `unclear` 之间偏保守 |

## 实现要点

| 模块 | 改动 |
|---|---|
| Execution Intent Guard | 新增 `EXECUTE / ANALYZE / CONSULT / META` 四类入口判定 |
| Idle Gate | 只有明确执行语义才允许进入 PCB workflow，分析/咨询/解释优先保持 chat |
| State Boundary | `wait_selection` 支持 `U55/IC7/BGA1/FPGA2` 等短实体输入 |
| Confirm Boundary | strong confirm 与 weak confirm 分级，避免弱确认误推进 |
| Evaluation | 新增 `--mode swsd3`，复用 raw no-think `llm_eval.json`，不重复调用模型 |

## 验证

| 命令 | 结果 |
|---|---|
| `python -m pytest tests\agent\test_swsd_core.py tests\test_pcb_intent_dataset_eval.py -q` | 20 passed |
| `python -m pytest tests\gateway\test_websocket_pcb_flow.py tests\tools\test_pcb_model_runtime.py -q` | 151 passed |
| `python scripts\evaluate_pcb_intent_dataset.py --mode swsd3 --out-dir reports\intent_eval_35b_nothink_full --prompt-style lean` | 499/500, 99.80% |

