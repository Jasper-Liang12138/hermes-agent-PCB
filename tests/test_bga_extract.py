from pcb_agent_langgraph.tools.external import AnalysisTool
from pcb_agent_langgraph.utils.config import load_config


# ====== 功能：验证 BGA 提取必须走独立脚本而不是模型或内部正则。 ======
def test_pcb_extra_bga_uses_script(tmp_path):
    config = load_config("config.example.ini")
    tool = AnalysisTool("pcb_extra_bga", config)
    board_data = '''
(component "U9"
  (part "PBGA256")
)
(part "PBGA256"
  (footprint "PBGA256")
  (class "IC")
  (pincount 256)
)
'''

    import asyncio

    result = asyncio.run(tool.ainvoke({"boardData": board_data}, {"session_id": "test_bga_script"}))

    assert result["status"] == "ok"
    assert result["source"] == "script"
    assert result["components"][0]["refdes"] == "U9"
    assert result["script_path"].endswith("extract_bga_components.py")
