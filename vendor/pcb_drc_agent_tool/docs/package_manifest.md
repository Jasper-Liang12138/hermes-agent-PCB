# DRC工具交付压缩包建议

## 必须包含

- `prod_main.py`
- `main.py`
- `zh_report_builder.py`
- `agent_payload_builder.py`
- `report_builder.py`
- `engine/`
- `geometry/`
- `model/`
- `parser/`
- `rules/`
- `loader/`
- `llm/`
- `docs/agent_integration_guide.md`

## 建议包含

- `samples/prediction.kicad_pcb`
- `samples/100_0018_prediction.kicad_pcb`
- `drc_tool.md`
- `design_rules.md`

## 不建议包含

- `.git/`
- `.vscode/`
- `__pycache__/`
- `build/`
- `dist/`，除非重新打包并验证过
- `out/`
- `result.json`
- `result1.json`
- `result_900.json`
- `diff.log`
- `*.pyc`
- `*.lck`

## 推荐压缩包命名

```text
pcb_drc_agent_tool_hard_only_YYYYMMDD.zip
```

## 交付前自测命令

```bash
python -m py_compile prod_main.py main.py zh_report_builder.py
python prod_main.py samples/prediction.kicad_pcb --agent-zh-json-out out/drc_agent_zh.json
```

自测通过后，确认 `out/drc_agent_zh.json` 中存在：

- `message_zh`
- `result.escape_completion_rate_text`
- `board_info.layer_count`
- `routing_metrics`
- `issues`
