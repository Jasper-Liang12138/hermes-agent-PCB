# PCB Intent Dataset Evaluation

Dataset: `F:\doctor\hermes-agent\邮件\intent_training_500.jsonl`
Generated: 2026-06-14 01:24:02

## Overall

| Evaluator | Passed | Total | Accuracy |
| --- | ---: | ---: | ---: |
| swsd4 | 4 | 5 | 80.00% |

## Validation

- Dataset validation failures: 0

## swsd4 Diagnostics

- Avg elapsed: 0.000s
- P95 elapsed: 0.000s
- Parse sources: {"": 5}
- Statuses: {"": 2, "encoder_error_fallback": 3}

## swsd4 By Category

| Category | Passed | Total | Accuracy |
| --- | ---: | ---: | ---: |
| chat_consultation | 1 | 1 | 100.00% |
| chat_general | 3 | 3 | 100.00% |
| unclear_fuzzy | 0 | 1 | 0.00% |

## swsd4 Failure Samples

- {"id": "T0004", "text": "现在好的", "expected": {"intent": "unclear", "route_mode": "chat", "bootstrap_get_project": false}, "actual": {"intent": "chat", "route_mode": "chat", "bootstrap_get_project": false, "reason": "swsd4_discussion", "intent_field": {"chat": 0.92, "analyze": 0.03, "execute": 0.02, "meta": 0.03, "uncertainty": 0.11, "rationale": "", "source": "llm"}, "skill_grounding_count": 2, "tool_misuse_flag": false}, "error": ""}
