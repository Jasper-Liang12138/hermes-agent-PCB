# PCB Intent Dataset Evaluation

Dataset: `F:\doctor\hermes-agent\邮件\intent_training_500.jsonl`
Generated: 2026-06-13 03:08:20

## Overall

| Evaluator | Passed | Total | Accuracy |
| --- | ---: | ---: | ---: |
| swsd2 | 421 | 500 | 84.20% |

## Validation

- Dataset validation failures: 0

## swsd2 By Category

| Category | Passed | Total | Accuracy |
| --- | ---: | ---: | ---: |
| cancel | 18 | 20 | 90.00% |
| chat_analysis | 18 | 30 | 60.00% |
| chat_consultation | 94 | 100 | 94.00% |
| chat_general | 28 | 30 | 93.33% |
| edge_fill | 4 | 5 | 80.00% |
| flow_confirm | 24 | 25 | 96.00% |
| flow_invalid | 13 | 15 | 86.67% |
| flow_modify | 15 | 15 | 100.00% |
| flow_router | 13 | 25 | 52.00% |
| flow_select | 18 | 25 | 72.00% |
| pcb_entry_fanout | 60 | 75 | 80.00% |
| pcb_entry_negation | 23 | 25 | 92.00% |
| pcb_reroute_operational | 80 | 80 | 100.00% |
| unclear_fuzzy | 13 | 30 | 43.33% |

## swsd2 Failure Samples

- {"id": "T0001", "text": "嗯，我不确定要不要做reroute，先帮我分析利弊（仅说明）", "expected": {"intent": "chat", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "pcb_entry", "route_mode": "pcb", "bootstrap_get_project": true, "reason": "swsd2_fanout_entry", "task_intent": "chat", "control_intent": "defer_execution", "meta_intent": "no_tool_call", "invalid_intent": ""}, "error": ""}
- {"id": "T0012", "text": "麻烦你U42（不要调用工具）", "expected": {"intent": "pcb_select_target", "route_mode": "pcb", "bootstrap_get_project": false}, "actual": {"intent": "unclear", "route_mode": "pcb", "bootstrap_get_project": false, "reason": "swsd2_invalid_selection_turn", "task_intent": "chat", "control_intent": "defer_execution", "meta_intent": "no_tool_call", "invalid_intent": ""}, "error": ""}
- {"id": "T0014", "text": "麻烦你这个区域不太好看（仅说明）", "expected": {"intent": "unclear", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "chat", "route_mode": "chat", "bootstrap_get_project": false, "reason": "swsd2_chat", "task_intent": "chat", "control_intent": "defer_execution", "meta_intent": "no_tool_call", "invalid_intent": ""}, "error": ""}
- {"id": "T0019", "text": "请问，再想想（先别执行）", "expected": {"intent": "unclear", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "chat", "route_mode": "chat", "bootstrap_get_project": false, "reason": "swsd2_chat", "task_intent": "chat", "control_intent": "defer_execution", "meta_intent": "no_tool_call", "invalid_intent": ""}, "error": ""}
- {"id": "T0020", "text": "现在不要解释，直接开始PCB BGA逃逸布线（仅说明）", "expected": {"intent": "pcb_entry", "route_mode": "pcb", "bootstrap_get_project": true}, "actual": {"intent": "chat", "route_mode": "chat", "bootstrap_get_project": false, "reason": "swsd2_preserve_consultation_chat", "task_intent": "chat", "control_intent": "defer_execution", "meta_intent": "no_tool_call", "invalid_intent": ""}, "error": ""}
- {"id": "T0026", "text": "能否U256。", "expected": {"intent": "pcb_select_target", "route_mode": "pcb", "bootstrap_get_project": false}, "actual": {"intent": "unclear", "route_mode": "pcb", "bootstrap_get_project": false, "reason": "swsd2_invalid_selection_turn", "task_intent": "chat", "control_intent": "", "meta_intent": "", "invalid_intent": ""}, "error": ""}
- {"id": "T0027", "text": "走线太密了，能重布吗（先别执行）", "expected": {"intent": "chat", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "pcb_reroute_selected", "route_mode": "pcb", "bootstrap_get_project": false, "reason": "swsd2_reroute_entry", "task_intent": "chat", "control_intent": "defer_execution", "meta_intent": "no_tool_call", "invalid_intent": ""}, "error": ""}
- {"id": "T0033", "text": "帮忙（先别执行）", "expected": {"intent": "unclear", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "chat", "route_mode": "chat", "bootstrap_get_project": false, "reason": "swsd2_chat", "task_intent": "chat", "control_intent": "defer_execution", "meta_intent": "no_tool_call", "invalid_intent": ""}, "error": ""}
- {"id": "T0055", "text": "麻烦PCB布线谢谢", "expected": {"intent": "pcb_entry", "route_mode": "pcb", "bootstrap_get_project": true}, "actual": {"intent": "chat", "route_mode": "chat", "bootstrap_get_project": false, "reason": "swsd2_chat", "task_intent": "chat", "control_intent": "", "meta_intent": "", "invalid_intent": ""}, "error": ""}
- {"id": "T0057", "text": "帮我调用getProjectData看看板子，别扇出（先别执行）", "expected": {"intent": "chat", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "pcb_entry", "route_mode": "pcb", "bootstrap_get_project": true, "reason": "swsd2_fanout_entry", "task_intent": "chat", "control_intent": "defer_execution", "meta_intent": "no_tool_call", "invalid_intent": ""}, "error": ""}
- {"id": "T0065", "text": "现在arc（仅说明）", "expected": {"intent": "pcb_followup", "route_mode": "pcb", "bootstrap_get_project": false}, "actual": {"intent": "unclear", "route_mode": "pcb", "bootstrap_get_project": false, "reason": "swsd2_invalid_router_turn", "task_intent": "chat", "control_intent": "defer_execution", "meta_intent": "no_tool_call", "invalid_intent": ""}, "error": ""}
- {"id": "T0073", "text": "选arc。", "expected": {"intent": "pcb_followup", "route_mode": "pcb", "bootstrap_get_project": false}, "actual": {"intent": "unclear", "route_mode": "pcb", "bootstrap_get_project": false, "reason": "swsd2_invalid_router_turn", "task_intent": "chat", "control_intent": "", "meta_intent": "", "invalid_intent": ""}, "error": ""}
- {"id": "T0090", "text": "请问，不要解释，直接开始PCB BGA逃逸布线？", "expected": {"intent": "pcb_entry", "route_mode": "pcb", "bootstrap_get_project": true}, "actual": {"intent": "chat", "route_mode": "chat", "bootstrap_get_project": false, "reason": "swsd2_preserve_consultation_chat", "task_intent": "chat", "control_intent": "", "meta_intent": "", "invalid_intent": ""}, "error": ""}
- {"id": "T0093", "text": "帮我U55谢谢", "expected": {"intent": "pcb_select_target", "route_mode": "pcb", "bootstrap_get_project": false}, "actual": {"intent": "unclear", "route_mode": "pcb", "bootstrap_get_project": false, "reason": "swsd2_invalid_selection_turn", "task_intent": "chat", "control_intent": "", "meta_intent": "", "invalid_intent": ""}, "error": ""}
- {"id": "T0096", "text": "麻烦U128？", "expected": {"intent": "pcb_select_target", "route_mode": "pcb", "bootstrap_get_project": false}, "actual": {"intent": "unclear", "route_mode": "pcb", "bootstrap_get_project": false, "reason": "swsd2_invalid_selection_turn", "task_intent": "chat", "control_intent": "", "meta_intent": "", "invalid_intent": ""}, "error": ""}
