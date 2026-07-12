from pcb_agent_langgraph.websocket.protocol import agent_message, parse_tool_result, parse_user_message, tool_call_message


# ====== 功能：验证前端 payload 包裹的用户消息可以被解析。 ======
def test_parse_payload_wrapped_user_message():
    msg = {"payload": {"sessionId": "", "projectID": "P1", "type": "message", "body": {"role": "user", "content": "拆线重布"}}}
    session_id, project_id, content = parse_user_message(msg)
    assert session_id == ""
    assert project_id == "P1"
    assert content == "拆线重布"


# ====== 功能：验证用户消息会携带前端按钮入口信号，且保留旧三元组解包兼容。 ======
def test_parse_user_message_carries_entry_signal():
    msg = {
        "payload": {
            "sessionId": "S1",
            "projectID": "P1",
            "type": "message",
            "body": {
                "role": "user",
                "content": "",
                "module": "fanout",
                "action": "enter",
                "selectedBGA": "U5",
            },
        }
    }
    parsed = parse_user_message(msg)
    session_id, project_id, content = parsed
    assert (session_id, project_id, content) == ("S1", "P1", "")
    assert parsed.entry_module == "global_fanout"
    assert parsed.entry_action == "enter"
    assert parsed.entry_payload["selectedBGA"] == "U5"


# ====== 功能：验证入口分流兼容 chain/workflow 等别名字段。 ======
def test_parse_user_message_normalizes_entry_aliases():
    msg = {
        "payload": {
            "sessionId": "S1",
            "projectID": "P1",
            "type": "message",
            "chain": "pcb_reroute_flow",
            "body": {"role": "user", "content": ""},
        }
    }
    parsed = parse_user_message(msg)
    assert parsed.entry_module == "reroute"


# ====== 功能：验证 content 对象内的结构化 BGA 选择可以被解析到 entry_payload。 ======
def test_parse_user_message_structured_bga_selection_content():
    msg = {
        "payload": {
            "sessionId": "S1",
            "projectID": "P1",
            "type": "message",
            "body": {
                "role": "user",
                "content": {
                    "module": "global_fanout",
                    "action": "select_bga",
                    "bga": {"componentId": "U5", "bgaType": "rectangular"},
                },
            },
        }
    }
    parsed = parse_user_message(msg)
    assert parsed.content == ""
    assert parsed.entry_module == "global_fanout"
    assert parsed.entry_action == "select_bga"
    assert parsed.entry_payload["bga"] == {"componentId": "U5", "bgaType": "rectangular"}


# ====== 功能：验证前端 payload 包裹且 result 为 JSON 字符串的工具结果可以被解析。 ======
def test_parse_payload_wrapped_tool_result_json_string():
    msg = {
        "payload": {
            "projectId": "P1",
            "type": "tool-results",
            "body": {"role": "tool", "content": {"id": "call-1", "result": "{\"projectData\":\"F:/demo/export.txt\",\"missing_routes\":[{\"net_name\":\"GND\"}]}"}},
        }
    }
    call_id, result = parse_tool_result(msg)
    assert call_id == "call-1"
    assert result["projectData"] == "F:/demo/export.txt"
    assert result["missing_routes"][0]["net_name"] == "GND"


# ====== 功能：验证 getProjectData 返回纯路径时能按工具结果透传。 ======
def test_parse_tool_result_plain_path():
    msg = {"payload": {"type": "tool-results", "body": {"role": "tool", "content": {"id": "call-path", "result": "F:/demo/export.txt"}}}}
    call_id, result = parse_tool_result(msg)
    assert call_id == "call-path"
    assert result == "F:/demo/export.txt"


# ====== 功能：验证非 final Agent 消息可用于前端流式状态。 ======
def test_agent_message_can_be_non_final_with_fixed_msg_id():
    message = agent_message("s1", "p1", "思考中...", isFinal=False, msgId="turn-1")
    assert message["body"]["msgId"] == "turn-1"
    assert message["body"]["isFinal"] is False
    assert message["body"]["content"] == "思考中..."


# ====== 功能：验证 getProjectData 按 v0.6 协议不携带 arguments。 ======
def test_get_project_data_tool_call_has_no_arguments():
    message = tool_call_message("s1", "p1", "call-1", "getProjectData", {"selectedBGA": "U5"})
    content = message["body"]["content"]
    assert message["projectID"] == "p1"
    assert content == {"id": "call-1", "name": "getProjectData"}


# ====== 功能：验证 deleteTracesForRerouting 按 v0.6 协议不携带 arguments。 ======
def test_delete_traces_for_rerouting_tool_call_has_no_arguments():
    message = tool_call_message("s1", "p1", "call-2", "deleteTracesForRerouting", {"unused": True})
    assert message["body"]["content"] == {"id": "call-2", "name": "deleteTracesForRerouting"}


# ====== 功能：验证 importLines 按 v0.6 协议补齐导入参数。 ======
def test_import_lines_tool_call_normalizes_arguments():
    message = tool_call_message("s1", "p1", "call-3", "importLines", {"filePath": "F:/demo/line.out", "requireApproval": False})
    assert message["body"]["content"]["arguments"] == {
        "filePath": "F:/demo/line.out",
        "successPins": [],
        "failedPins": [],
        "requireApproval": False,
    }


# ====== 功能：提供 WebSocket 单测用的异步假连接。 ======
class _FakeWebSocket:
    # ====== 功能：初始化异步输入输出队列。 ======
    def __init__(self):
        import asyncio

        self.incoming = asyncio.Queue()
        self.sent = []

    # ====== 功能：模拟前端向 Agent 发送一条消息。 ======
    async def push(self, message):
        import json

        await self.incoming.put(json.dumps(message, ensure_ascii=False))

    # ====== 功能：关闭异步迭代。 ======
    async def close(self):
        await self.incoming.put(None)

    # ====== 功能：返回异步迭代器自身。 ======
    def __aiter__(self):
        return self

    # ====== 功能：读取下一条前端消息。 ======
    async def __anext__(self):
        item = await self.incoming.get()
        if item is None:
            raise StopAsyncIteration
        return item

    # ====== 功能：记录 Agent 发给前端的消息。 ======
    async def send(self, raw):
        import json

        self.sent.append(json.loads(raw))


# ====== 功能：验证 handler 不阻塞读取 tool-results，避免 getProjectData 死锁。 ======
def test_handler_keeps_reading_tool_results_during_agent_turn():
    import asyncio

    from pcb_agent_langgraph.websocket.server import PCBWebSocketServer

    async def scenario():
        server = PCBWebSocketServer.__new__(PCBWebSocketServer)
        server._pending = {}
        server._connections = {}
        server._connection_sessions = {}
        server._active_message_ids = {}
        server._last_progress = {}
        server._session_pending_tools = {}
        server._active_turn_tasks = {}
        server._websocket_sessions = {}

        async def fake_ainvoke(session_id, project_id, user_input, **kwargs):
            result = await server.send_tool_call(session_id, "call-get-project", {"__tool_name__": "getProjectData"}, 1.0)
            return {"final_response": f"loaded {result}", "markdown_report": "", "report_payload": {}, "selection": None}

        server.agent = type("FakeAgent", (), {"ainvoke": staticmethod(fake_ainvoke)})()
        ws = _FakeWebSocket()
        handler_task = asyncio.create_task(server.handler(ws))
        await ws.push({"payload": {"sessionId": "", "projectID": "P1", "type": "message", "body": {"role": "user", "content": "帮我做逃逸布线"}}})

        for _ in range(50):
            await asyncio.sleep(0.01)
            tool_call = next((item for item in ws.sent if item.get("type") == "tool-calls"), None)
            if tool_call:
                break
        assert tool_call is not None
        assert tool_call["body"]["content"] == {"id": "call-get-project", "name": "getProjectData"}

        await ws.push({"payload": {"type": "tool-results", "body": {"role": "tool", "content": {"id": "call-get-project", "result": "F:/demo/export.txt"}}}})
        for _ in range(50):
            await asyncio.sleep(0.01)
            final = next((item for item in ws.sent if item.get("type") == "message" and item.get("body", {}).get("isFinal") is True), None)
            if final:
                break
        assert final is not None
        assert final["body"]["content"] == "loaded F:/demo/export.txt"
        await ws.close()
        await handler_task

    asyncio.run(scenario())


# ====== 功能：验证同一 session 任务未完成时会返回忙碌提示。 ======
def test_handler_rejects_parallel_turn_for_same_session():
    import asyncio

    from pcb_agent_langgraph.websocket.server import PCBWebSocketServer

    async def scenario():
        server = PCBWebSocketServer.__new__(PCBWebSocketServer)
        server._pending = {}
        server._connections = {}
        server._connection_sessions = {}
        server._active_message_ids = {}
        server._last_progress = {}
        server._session_pending_tools = {}
        server._active_turn_tasks = {}
        server._websocket_sessions = {}
        event = asyncio.Event()

        async def fake_ainvoke(session_id, project_id, user_input, **kwargs):
            await event.wait()
            return {"final_response": "done", "markdown_report": "", "report_payload": {}, "selection": None}

        server.agent = type("FakeAgent", (), {"ainvoke": staticmethod(fake_ainvoke)})()
        ws = _FakeWebSocket()
        handler_task = asyncio.create_task(server.handler(ws))
        msg = {"payload": {"sessionId": "S1", "projectID": "P1", "type": "message", "body": {"role": "user", "content": "fanout"}}}
        await ws.push(msg)
        await asyncio.sleep(0.05)
        await ws.push(msg)
        for _ in range(50):
            await asyncio.sleep(0.01)
            busy = next((item for item in ws.sent if "仍在执行" in item.get("body", {}).get("content", "")), None)
            if busy:
                break
        assert busy is not None
        event.set()
        await asyncio.sleep(0.05)
        await ws.close()
        await handler_task

    asyncio.run(scenario())


# ====== 功能：验证 WebSocket server 会把入口信号传入 Agent。 ======
def test_handler_passes_entry_signal_to_agent():
    import asyncio

    from pcb_agent_langgraph.websocket.server import PCBWebSocketServer

    async def scenario():
        server = PCBWebSocketServer.__new__(PCBWebSocketServer)
        server._pending = {}
        server._connections = {}
        server._connection_sessions = {}
        server._active_message_ids = {}
        server._last_progress = {}
        server._session_pending_tools = {}
        server._active_turn_tasks = {}
        server._websocket_sessions = {}
        received = {}

        async def fake_ainvoke(session_id, project_id, user_input, **kwargs):
            received.update({"session_id": session_id, "project_id": project_id, "user_input": user_input, **kwargs})
            return {"final_response": "ok", "markdown_report": "", "report_payload": {}, "selection": None}

        server.agent = type("FakeAgent", (), {"ainvoke": staticmethod(fake_ainvoke)})()
        ws = _FakeWebSocket()
        handler_task = asyncio.create_task(server.handler(ws))
        await ws.push({"payload": {"sessionId": "S1", "projectID": "P1", "type": "message", "body": {"role": "user", "content": "", "module": "fanout", "action": "enter"}}})

        for _ in range(50):
            await asyncio.sleep(0.01)
            if received:
                break
        assert received["entry_module"] == "global_fanout"
        assert received["entry_action"] == "enter"
        assert received["entry_payload"]["entry_module"] == "global_fanout"
        await ws.close()
        await handler_task

    asyncio.run(scenario())


def test_import_lines_defaults_to_approval_for_legacy_callers():
    message = tool_call_message("s1", "p1", "call-legacy", "importLines", {"filePath": "F:/demo/line.out"})
    assert message["body"]["content"]["arguments"]["requireApproval"] is True

def test_parse_bga_escape_routing_protocol_message():
    parsed = parse_user_message({"projectid": "P1", "type": "BGA_Escape_Routing", "body": {"decision": "RETRY", "stage": "SETTING_PARAMS", "bga": "U5", "algorithm": "135", "type": "RL"}})
    assert parsed.entry_module == "global_fanout"
    assert parsed.entry_action == "retry"
    assert parsed.entry_payload["decision"] == "RETRY"
    assert parsed.entry_payload["stage"] == "SETTING_PARAMS"
    assert parsed.entry_payload["routingParamType"] == "RL"


def test_restore_tool_call_preserves_arguments():
    arguments = {"workflow": "fanout", "reason": "retry_routing", "snapshotId": "snap-1"}
    message = tool_call_message("s1", "p1", "call-restore", "restoreFanoutSnapshot", arguments)
    assert message["body"]["content"]["arguments"] == arguments


def test_all_entry_fields_and_required_aliases_are_supported():
    aliases = {"qa": "qa", "pcb_qa_flow": "qa", "global_fanout": "global_fanout", "fanout": "global_fanout", "bga_escape_routing": "global_fanout", "pcb_escape_flow": "global_fanout", "reroute": "reroute", "local_reroute": "reroute", "pcb_reroute_flow": "reroute"}
    fields = ("entry_module", "entryModule", "module", "chain", "taskType", "task_type", "workflow", "workflow_id")
    for alias, expected in aliases.items():
        for field in fields:
            parsed = parse_user_message({"type": "message", "body": {"role": "user", "content": "", field: alias}})
            assert parsed.entry_module == expected
