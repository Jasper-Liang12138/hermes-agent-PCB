# PCB Intent Dataset Evaluation

Dataset: `F:\doctor\hermes-agent\邮件\intent_training_500.jsonl`
Generated: 2026-06-14 01:22:29

## Overall

| Evaluator | Passed | Total | Accuracy |
| --- | ---: | ---: | ---: |
| swsd4 | 391 | 500 | 78.20% |

## Validation

- Dataset validation failures: 0

## swsd4 Diagnostics

- Avg elapsed: 0.000s
- P95 elapsed: 0.000s
- Parse sources: {"": 500}
- Statuses: {"": 500}

## swsd4 By Category

| Category | Passed | Total | Accuracy |
| --- | ---: | ---: | ---: |
| cancel | 1 | 20 | 5.00% |
| chat_analysis | 19 | 30 | 63.33% |
| chat_consultation | 82 | 100 | 82.00% |
| chat_general | 25 | 30 | 83.33% |
| edge_fill | 3 | 5 | 60.00% |
| flow_confirm | 16 | 25 | 64.00% |
| flow_invalid | 15 | 15 | 100.00% |
| flow_modify | 13 | 15 | 86.67% |
| flow_router | 23 | 25 | 92.00% |
| flow_select | 17 | 25 | 68.00% |
| pcb_entry_fanout | 64 | 75 | 85.33% |
| pcb_entry_negation | 21 | 25 | 84.00% |
| pcb_reroute_operational | 73 | 80 | 91.25% |
| unclear_fuzzy | 19 | 30 | 63.33% |

## swsd4 Failure Samples

- {"id": "T0001", "text": "嗯，我不确定要不要做reroute，先帮我分析利弊（仅说明）", "expected": {"intent": "chat", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "unclear", "route_mode": "chat", "bootstrap_get_project": false, "reason": "swsd4_uncertain", "intent_field": {}, "skill_grounding_count": 2, "tool_misuse_flag": false}, "error": ""}
- {"id": "T0016", "text": "嗯，中止", "expected": {"intent": "cancel", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "unclear", "route_mode": "chat", "bootstrap_get_project": false, "reason": "swsd4_meta_defer", "intent_field": {}, "skill_grounding_count": 2, "tool_misuse_flag": false}, "error": ""}
- {"id": "T0022", "text": "麻烦你停止。", "expected": {"intent": "cancel", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "unclear", "route_mode": "chat", "bootstrap_get_project": false, "reason": "swsd4_meta_defer", "intent_field": {}, "skill_grounding_count": 2, "tool_misuse_flag": false}, "error": ""}
- {"id": "T0027", "text": "走线太密了，能重布吗（先别执行）", "expected": {"intent": "chat", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "pcb_reroute_selected", "route_mode": "pcb", "bootstrap_get_project": false, "reason": "swsd4_execute_swsd2", "intent_field": {}, "skill_grounding_count": 2, "tool_misuse_flag": true}, "error": ""}
- {"id": "T0029", "text": "麻烦你拆线重布一般分几步？", "expected": {"intent": "chat", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "pcb_reroute_selected", "route_mode": "pcb", "bootstrap_get_project": false, "reason": "swsd4_execute_swsd2", "intent_field": {}, "skill_grounding_count": 2, "tool_misuse_flag": true}, "error": ""}
- {"id": "T0031", "text": "帮忙？", "expected": {"intent": "unclear", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "chat", "route_mode": "chat", "bootstrap_get_project": false, "reason": "swsd4_discussion", "intent_field": {}, "skill_grounding_count": 2, "tool_misuse_flag": false}, "error": ""}
- {"id": "T0038", "text": "帮我先获取版图，再对U48扇出（仅说明）", "expected": {"intent": "pcb_entry", "route_mode": "pcb", "bootstrap_get_project": true}, "actual": {"intent": "chat", "route_mode": "chat", "bootstrap_get_project": false, "reason": "swsd4_discussion", "intent_field": {}, "skill_grounding_count": 2, "tool_misuse_flag": false}, "error": ""}
- {"id": "T0039", "text": "嗯，布线（先别执行）", "expected": {"intent": "pcb_entry", "route_mode": "pcb", "bootstrap_get_project": true}, "actual": {"intent": "unclear", "route_mode": "chat", "bootstrap_get_project": false, "reason": "swsd4_uncertain", "intent_field": {}, "skill_grounding_count": 2, "tool_misuse_flag": false}, "error": ""}
- {"id": "T0045", "text": "先只分析BGA列表，不要布线（仅说明）", "expected": {"intent": "chat", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "pcb_entry", "route_mode": "pcb", "bootstrap_get_project": false, "reason": "swsd4_execute_swsd2", "intent_field": {}, "skill_grounding_count": 2, "tool_misuse_flag": true}, "error": ""}
- {"id": "T0054", "text": "嗯，你好谢谢", "expected": {"intent": "chat", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "unclear", "route_mode": "chat", "bootstrap_get_project": false, "reason": "swsd4_uncertain", "intent_field": {}, "skill_grounding_count": 2, "tool_misuse_flag": false}, "error": ""}
- {"id": "T0061", "text": "嗯，exit谢谢", "expected": {"intent": "cancel", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "unclear", "route_mode": "chat", "bootstrap_get_project": false, "reason": "swsd4_meta_defer", "intent_field": {}, "skill_grounding_count": 2, "tool_misuse_flag": false}, "error": ""}
- {"id": "T0063", "text": "现在获取当前版图并找出可布线BGA（仅说明）", "expected": {"intent": "pcb_entry", "route_mode": "pcb", "bootstrap_get_project": true}, "actual": {"intent": "chat", "route_mode": "chat", "bootstrap_get_project": false, "reason": "swsd4_discussion", "intent_field": {}, "skill_grounding_count": 2, "tool_misuse_flag": false}, "error": ""}
- {"id": "T0093", "text": "帮我U55谢谢", "expected": {"intent": "pcb_select_target", "route_mode": "pcb", "bootstrap_get_project": false}, "actual": {"intent": "unclear", "route_mode": "pcb", "bootstrap_get_project": false, "reason": "swsd4_uncertain", "intent_field": {}, "skill_grounding_count": 2, "tool_misuse_flag": false}, "error": ""}
- {"id": "T0097", "text": "麻烦reroute selected traces（先别执行）", "expected": {"intent": "pcb_reroute_selected", "route_mode": "pcb", "bootstrap_get_project": false}, "actual": {"intent": "unclear", "route_mode": "chat", "bootstrap_get_project": false, "reason": "swsd4_meta_defer", "intent_field": {}, "skill_grounding_count": 2, "tool_misuse_flag": false}, "error": ""}
- {"id": "T0098", "text": "帮我cancel。", "expected": {"intent": "cancel", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "unclear", "route_mode": "chat", "bootstrap_get_project": false, "reason": "swsd4_meta_defer", "intent_field": {}, "skill_grounding_count": 2, "tool_misuse_flag": false}, "error": ""}
