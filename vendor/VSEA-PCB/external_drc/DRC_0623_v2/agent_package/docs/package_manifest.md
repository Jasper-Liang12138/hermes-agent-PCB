# Agent 运行文件清单

当前不提供压缩包，也不维护 `delivery` 交付副本。Agent 直接从项目目录调用：

```powershell
python prod_main.py "输入文件.kicad_pcb" --target-bga U67 --agent-zh-json-out "out\drc_agent.json"
```

运行依赖：

- `prod_main.py`
- `main.py`
- `agent_payload_builder.py`
- `zh_report_builder.py`
- `report_builder.py`
- `engine/`
- `geometry/`
- `model/`
- `parser/`
- `rules/`
- `loader/`

完整用法见 `docs/agent_integration_guide.md`。
