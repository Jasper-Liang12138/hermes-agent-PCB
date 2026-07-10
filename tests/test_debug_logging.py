from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from pcb_agent_langgraph.agent import PCBLangGraphAgent
from pcb_agent_langgraph.debug_logging import AgentDebugLogger, agent_debug_context
from pcb_agent_langgraph.graph.nodes import GraphNodes
from pcb_agent_langgraph.models.pcb_model import PCBModel
from pcb_agent_langgraph.tools.base import invoke_tool
from pcb_agent_langgraph.utils.config import AppConfig, DebugLogConfig, ModelConfig


def _events(root: Path) -> list[dict[str, Any]]:
    files = sorted(root.rglob("*.jsonl"))
    assert files
    rows: list[dict[str, Any]] = []
    for line in files[0].read_text(encoding="utf-8").splitlines():
        rows.append(json.loads(line))
    return rows


def test_agent_turn_writes_turn_and_node_events(tmp_path: Path):
    config = AppConfig(root=tmp_path, debug_log=DebugLogConfig(enabled=True, print=False, dir=str(tmp_path / "logs")))

    class FakePlanner:
        def plan(self, state):
            return {"intent": "qa", "workflow": "pcb_qa_flow", "action": "chat", "tool_calls": [], "response": "ok"}

    class FakeGraph:
        async def ainvoke(self, state):
            nodes = GraphNodes(FakePlanner(), {})
            state = {**state, **await nodes.intent(state)}
            state = {**state, **await nodes.plan(state)}
            state = {**state, **await nodes.reflect(state)}
            state = {**state, **await nodes.finish(state)}
            return state

    agent = PCBLangGraphAgent.__new__(PCBLangGraphAgent)
    agent.config = config
    agent.graph = FakeGraph()
    agent._session_states = {}

    state = asyncio.run(agent.ainvoke("session-1", "project-1", "hello"))

    assert state["final_response"]
    rows = _events(tmp_path / "logs")
    names = [row["event"] for row in rows]
    assert "turn.start" in names
    assert "turn.end" in names
    assert "node.intent.start" in names
    assert "node.intent.end" in names
    start = next(row for row in rows if row["event"] == "turn.start")
    assert start["session_id"] == "session-1"
    assert start["project_id"] == "project-1"
    assert start["payload"]["user_input"] == "hello"
    node_start = next(row for row in rows if row["event"] == "node.intent.start")
    assert node_start["payload"]["state"]["user_input"] == "hello"


def test_tool_logging_records_success_and_error(tmp_path: Path):
    logger = AgentDebugLogger(
        DebugLogConfig(enabled=True, print=False, dir=str(tmp_path / "logs")),
        run_id="run-tool",
        session_id="session-tool",
        project_id="project-tool",
    )

    class GoodTool:
        name = "good_tool"

        async def ainvoke(self, arguments: dict[str, Any], context: dict[str, Any]) -> Any:
            return {"echo": arguments["value"], "session": context["session_id"]}

    class BadTool:
        name = "bad_tool"

        async def ainvoke(self, arguments: dict[str, Any], context: dict[str, Any]) -> Any:
            raise RuntimeError("boom")

    async def scenario():
        with agent_debug_context(logger):
            ok = await invoke_tool(
                GoodTool(),
                {"id": "call-ok", "name": "good_tool", "arguments": {"value": 7}},
                {"session_id": "session-tool", "timeout": 1.0},
            )
            failed = await invoke_tool(
                BadTool(),
                {"id": "call-bad", "name": "bad_tool", "arguments": {}},
                {"session_id": "session-tool", "timeout": 1.0},
            )
            return ok, failed

    ok, failed = asyncio.run(scenario())

    assert ok["ok"] is True
    assert failed["ok"] is False
    rows = _events(tmp_path / "logs")
    names = [row["event"] for row in rows]
    assert "tool.start" in names
    assert "tool.end" in names
    assert "tool.error" in names
    tool_end = next(row for row in rows if row["event"] == "tool.end")
    assert tool_end["payload"]["result"]["echo"] == 7


def test_model_logging_records_full_request_response_and_redacts_secrets(tmp_path: Path, monkeypatch):
    config = ModelConfig(api_key="SECRET_API_KEY", model="demo-model", base_url="https://example.test/v1")
    logger = AgentDebugLogger(
        DebugLogConfig(enabled=True, print=False, dir=str(tmp_path / "logs"), redact_secrets=True),
        run_id="run-model",
        session_id="session-model",
        project_id="project-model",
    )

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [{"message": {"content": "{\"action\":\"finish\",\"response\":\"model says hi\"}"}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 5},
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        assert b"hello model" in request.data
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with agent_debug_context(logger):
        result = PCBModel(config, timeout=1.0).complete([{"role": "user", "content": "hello model"}])

    assert result.content == "{\"action\":\"finish\",\"response\":\"model says hi\"}"
    rows = _events(tmp_path / "logs")
    names = [row["event"] for row in rows]
    assert "model.request" in names
    assert "model.response" in names
    request_event = next(row for row in rows if row["event"] == "model.request")
    response_event = next(row for row in rows if row["event"] == "model.response")
    assert request_event["payload"]["body"]["messages"][0]["content"] == "hello model"
    assert response_event["payload"]["raw"]["usage"]["prompt_tokens"] == 3
    serialized = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
    assert "SECRET_API_KEY" not in serialized
    assert "<redacted>" in serialized


def test_debug_logging_can_be_disabled(tmp_path: Path):
    logger = AgentDebugLogger(
        DebugLogConfig(enabled=False, print=False, dir=str(tmp_path / "logs")),
        run_id="run-disabled",
        session_id="session-disabled",
        project_id="project-disabled",
    )

    with agent_debug_context(logger):
        logger.log("turn.start", {"user_input": "hidden"})

    assert not list((tmp_path / "logs").rglob("*.jsonl"))
