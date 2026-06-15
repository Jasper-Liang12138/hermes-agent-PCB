import json
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


@pytest.fixture()
def agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        instance = AIAgent(
            api_key="test-key-1234567890",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        instance.client = MagicMock()
        return instance


def test_pcb_tool_call_shim_passes_natural_language_to_generate(agent):
    agent.valid_tool_names = {"getProjectData", "pcb_extract_bga", "generateFanoutParams", "route"}
    agent.tools = _make_tool_defs(*agent.valid_tool_names)
    user_text = "对 U22 开始布线，用 135 + 北科大，NET_A 走 SIG03，NET_B 走 SIG04"

    calls = agent._pcb_tool_call_shim_override([
        {"role": "user", "content": "帮我进行BGA逃逸布线"},
        {"role": "tool", "name": "getProjectData", "content": "(pcb_data)"},
        {
            "role": "tool",
            "name": "pcb_extract_bga",
            "content": json.dumps({"selection": [{"label": "U22", "detail": "BGA-400"}]}, ensure_ascii=False),
        },
        {"role": "user", "content": user_text},
    ])

    assert calls[0].function.name == "generateFanoutParams"
    assert json.loads(calls[0].function.arguments) == {
        "selectedBGA": "U22",
        "routerType": "135",
        "userText": user_text,
    }


def test_pcb_tool_call_shim_routes_after_natural_language_fanout(agent):
    agent.valid_tool_names = {"getProjectData", "pcb_extract_bga", "generateFanoutParams", "route"}
    agent.tools = _make_tool_defs(*agent.valid_tool_names)
    fanout = {
        "selectedBGA": "U22",
        "routerType": "135",
        "orderLines": [{"net": "NET_A", "layer": "SIG03", "order": 1}],
        "naturalLanguageOrderLines": [{"net": "NET_A", "layer": "SIG03", "order": 1}],
    }

    calls = agent._pcb_tool_call_shim_override([
        {"role": "user", "content": "帮我进行BGA逃逸布线"},
        {"role": "tool", "name": "getProjectData", "content": "(pcb_data)"},
        {"role": "tool", "name": "pcb_extract_bga", "content": "{}"},
        {"role": "tool", "name": "generateFanoutParams", "content": json.dumps({"fanoutParams": fanout}, ensure_ascii=False)},
        {"role": "user", "content": "按刚才指定网络层分配开始布线"},
    ])

    assert calls[0].function.name == "route"
    assert json.loads(json.loads(calls[0].function.arguments)["userData"]) == fanout
