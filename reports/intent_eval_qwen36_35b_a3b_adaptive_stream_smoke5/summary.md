# PCB Intent Dataset Evaluation

Dataset: `F:\doctor\hermes-agent\邮件\intent_training_500.jsonl`
Generated: 2026-06-13 13:52:48

## Overall

| Evaluator | Passed | Total | Accuracy |
| --- | ---: | ---: | ---: |
| llm | 0 | 5 | 0.00% |

## Validation

- Dataset validation failures: 0

## llm Diagnostics

- Avg elapsed: 3.706s
- P95 elapsed: 4.375s
- Parse sources: {"": 5}
- Statuses: {"unparsed": 5}

## llm By Category

| Category | Passed | Total | Accuracy |
| --- | ---: | ---: | ---: |
| chat_consultation | 0 | 1 | 0.00% |
| chat_general | 0 | 3 | 0.00% |
| unclear_fuzzy | 0 | 1 | 0.00% |

## llm Failure Samples

- {"id": "T0001", "text": "嗯，我不确定要不要做reroute，先帮我分析利弊（仅说明）", "expected": {"intent": "chat", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "", "route_mode": "", "bootstrap_get_project": false, "reason": "", "source": ""}, "error": "unparsed_output"}
- {"id": "T0002", "text": "请问，帮我写一段Python读取Gerber。", "expected": {"intent": "chat", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "", "route_mode": "", "bootstrap_get_project": false, "reason": "", "source": ""}, "error": "unparsed_output"}
- {"id": "T0003", "text": "什么是阻抗匹配（仅说明）", "expected": {"intent": "chat", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "", "route_mode": "", "bootstrap_get_project": false, "reason": "", "source": ""}, "error": "unparsed_output"}
- {"id": "T0004", "text": "现在好的", "expected": {"intent": "unclear", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "", "route_mode": "", "bootstrap_get_project": false, "reason": "", "source": ""}, "error": "unparsed_output"}
- {"id": "T0005", "text": "能否你能做什么可以吗", "expected": {"intent": "chat", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "", "route_mode": "", "bootstrap_get_project": false, "reason": "", "source": ""}, "error": "unparsed_output"}
