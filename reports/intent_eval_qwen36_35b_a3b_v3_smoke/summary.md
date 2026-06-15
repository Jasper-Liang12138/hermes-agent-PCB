# PCB Intent Dataset Evaluation

Dataset: `F:\doctor\hermes-agent\邮件\intent_training_500.jsonl`
Generated: 2026-06-12 20:40:51

## Overall

| Evaluator | Passed | Total | Accuracy |
| --- | ---: | ---: | ---: |
| llm | 0 | 20 | 0.00% |

## Validation

- Dataset validation failures: 0

## llm By Category

| Category | Passed | Total | Accuracy |
| --- | ---: | ---: | ---: |
| cancel | 0 | 1 | 0.00% |
| chat_consultation | 0 | 4 | 0.00% |
| chat_general | 0 | 4 | 0.00% |
| flow_confirm | 0 | 1 | 0.00% |
| flow_router | 0 | 1 | 0.00% |
| flow_select | 0 | 1 | 0.00% |
| pcb_entry_fanout | 0 | 1 | 0.00% |
| pcb_entry_negation | 0 | 2 | 0.00% |
| pcb_reroute_operational | 0 | 2 | 0.00% |
| unclear_fuzzy | 0 | 3 | 0.00% |

## llm Failure Samples

- {"id": "T0001", "text": "嗯，我不确定要不要做reroute，先帮我分析利弊（仅说明）", "expected": {"intent": "chat", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "", "route_mode": "", "bootstrap_get_project": false, "reason": ""}, "error": "TimeoutError: The read operation timed out"}
- {"id": "T0002", "text": "请问，帮我写一段Python读取Gerber。", "expected": {"intent": "chat", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "", "route_mode": "", "bootstrap_get_project": false, "reason": ""}, "error": "TimeoutError: The read operation timed out"}
- {"id": "T0003", "text": "什么是阻抗匹配（仅说明）", "expected": {"intent": "chat", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "", "route_mode": "", "bootstrap_get_project": false, "reason": ""}, "error": "TimeoutError: The read operation timed out"}
- {"id": "T0004", "text": "现在好的", "expected": {"intent": "unclear", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "", "route_mode": "", "bootstrap_get_project": false, "reason": ""}, "error": "TimeoutError: The read operation timed out"}
- {"id": "T0005", "text": "能否你能做什么可以吗", "expected": {"intent": "chat", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "", "route_mode": "", "bootstrap_get_project": false, "reason": ""}, "error": "TimeoutError: The read operation timed out"}
- {"id": "T0006", "text": "能否fanout U7，不要reroute。", "expected": {"intent": "pcb_entry", "route_mode": "pcb", "bootstrap_get_project": true}, "actual": {"intent": "", "route_mode": "", "bootstrap_get_project": false, "reason": ""}, "error": "TimeoutError: The read operation timed out"}
- {"id": "T0007", "text": "先先别动板子，我们聊聊（不要调用工具）", "expected": {"intent": "chat", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "", "route_mode": "", "bootstrap_get_project": false, "reason": ""}, "error": "TimeoutError: The read operation timed out"}
- {"id": "T0008", "text": "现在执行拆线重布（不要调用工具）", "expected": {"intent": "pcb_reroute_selected", "route_mode": "pcb", "bootstrap_get_project": false}, "actual": {"intent": "", "route_mode": "", "bootstrap_get_project": false, "reason": ""}, "error": "TimeoutError: The read operation timed out"}
- {"id": "T0009", "text": "现在BGA扇出和BGA扇出有什么区别。", "expected": {"intent": "chat", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "", "route_mode": "", "bootstrap_get_project": false, "reason": ""}, "error": "TimeoutError: The read operation timed out"}
- {"id": "T0010", "text": "现在删除我框选的线重新布线（先别执行）", "expected": {"intent": "pcb_reroute_selected", "route_mode": "pcb", "bootstrap_get_project": false}, "actual": {"intent": "", "route_mode": "", "bootstrap_get_project": false, "reason": ""}, "error": "TimeoutError: The read operation timed out"}
- {"id": "T0011", "text": "先arc + RL", "expected": {"intent": "pcb_followup", "route_mode": "pcb", "bootstrap_get_project": false}, "actual": {"intent": "", "route_mode": "", "bootstrap_get_project": false, "reason": ""}, "error": "TimeoutError: The read operation timed out"}
- {"id": "T0012", "text": "麻烦你U42（不要调用工具）", "expected": {"intent": "pcb_select_target", "route_mode": "pcb", "bootstrap_get_project": false}, "actual": {"intent": "", "route_mode": "", "bootstrap_get_project": false, "reason": ""}, "error": "TimeoutError: The read operation timed out"}
- {"id": "T0013", "text": "帮我做BGA逃逸，不是拆线重布（仅说明）", "expected": {"intent": "pcb_entry", "route_mode": "pcb", "bootstrap_get_project": true}, "actual": {"intent": "", "route_mode": "", "bootstrap_get_project": false, "reason": ""}, "error": "TimeoutError: The read operation timed out"}
- {"id": "T0014", "text": "麻烦你这个区域不太好看（仅说明）", "expected": {"intent": "unclear", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "", "route_mode": "", "bootstrap_get_project": false, "reason": ""}, "error": "TimeoutError: The read operation timed out"}
- {"id": "T0015", "text": "能否继续执行？", "expected": {"intent": "pcb_confirm_route", "route_mode": "pcb", "bootstrap_get_project": false}, "actual": {"intent": "", "route_mode": "", "bootstrap_get_project": false, "reason": ""}, "error": "TimeoutError: The read operation timed out"}
