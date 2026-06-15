# PCB Intent Dataset Evaluation

Dataset: `F:\doctor\hermes-agent\邮件\intent_training_500.jsonl`
Generated: 2026-06-13 18:36:04

## Overall

| Evaluator | Passed | Total | Accuracy |
| --- | ---: | ---: | ---: |
| llm | 4 | 5 | 80.00% |

## Validation

- Dataset validation failures: 0

## llm Diagnostics

- Avg elapsed: 4.487s
- P95 elapsed: 5.640s
- Parse sources: {"jsonish": 5}
- Statuses: {"pass1 parsed:jsonish": 5}

## llm By Category

| Category | Passed | Total | Accuracy |
| --- | ---: | ---: | ---: |
| chat_consultation | 1 | 1 | 100.00% |
| chat_general | 3 | 3 | 100.00% |
| unclear_fuzzy | 0 | 1 | 0.00% |

## llm Failure Samples

- {"id": "T0004", "text": "现在好的", "expected": {"intent": "unclear", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "unclear", "route_mode": "pcb", "bootstrap_get_project": false, "reason": "wait_confirm_ambiguous_response", "source": "jsonish"}, "error": ""}
