# PCB WebSocket 意图识别与 Skill 注入流程

本文说明 PCB WebSocket 请求从用户消息进入 Hermes 后，如何完成模式切换、意图识别、skill 匹配、上下文拼接和工具执行。

## 1. 模式切换入口

WebSocket 新 session 默认进入 `chat` 模式：

- 代码位置：`gateway/platforms/websocket.py`
- 关键函数：连接消息处理处初始化 `_session_modes`

```python
if session_id not in self._session_modes:
    self._set_session_mode(session_id, _ROUTE_MODE_CHAT, lock_seconds=0.0)
if session_id not in self._session_flow_states:
    self._set_flow_state(session_id, _FLOW_IDLE)
```

每条用户消息进入 `_handle_user_message()` 后，先做意图识别：

```python
llm_intent = await self._classify_route_intent_with_llm(
    session_id=session_id,
    user_text=user_text,
    project_id=project_id,
)
decision = self._decide_route(session_id, user_text, llm_intent=llm_intent)
```

`decision.mode` 会写入本轮 `turn_options["route_mode"]`，后续用于切换工具集。

## 2. 意图识别的几种模式

当前 PCB WebSocket 路由不是单一规则，而是分层识别。

### 2.1 LLM 意图识别

默认优先使用辅助模型识别用户意图。

- 代码位置：`gateway/platforms/websocket.py`
- 函数：`_classify_route_intent_with_llm()`
- Prompt 构造：`_build_route_intent_prompt()`

可输出的 intent 包括：

```text
chat
pcb_entry
pcb_select_target
pcb_confirm_route
pcb_modify_params
pcb_reroute_selected
cancel
unclear
```

拆线重布的模型判定说明在 prompt 中：

```python
"- 明确要求对文本中指定 net 做拆线重布、删除后重走、reroute，判 pcb_reroute_selected。\n"
```

期望模型输出示例：

```json
{
  "intent": "pcb_reroute_selected",
  "route_mode": "pcb",
  "confidence": 0.88,
  "should_call_get_project_data": false,
  "reason_code": "local_reroute_request"
}
```

### 2.2 安全硬规则

少数安全规则优先于模型：

- 取消、中止、退出当前流程；
- 明确说“不要执行 / 只解释 / 不调用工具”。

对应逻辑：

```python
if _CANCEL_RE.search(text):
    return _INTENT_CANCEL
if self._is_explicit_no_operation(text):
    return _INTENT_CHAT
```

这些规则用于防止用户明确否定操作时仍误触发工具。

### 2.3 LLM 结果裁决

`_validate_route_intent()` 会优先采纳高置信度 LLM 结果：

```python
if route_intent.intent == _INTENT_CHAT and route_intent.confidence >= 0.70:
    if _REROUTE_RE.search(text) and _PCB_DOMAIN_RE.search(text):
        return _INTENT_CHAT
    if not self._is_strong_pcb_intent(text):
        return _INTENT_CHAT
if route_intent.intent == _INTENT_PCB_REROUTE_SELECTED:
    if route_intent.confidence >= 0.70:
        return _INTENT_PCB_REROUTE_SELECTED
```

这意味着：拆线重布场景中，即使文本包含“拆线、重布、net”等词，只要 LLM 高置信判定为 `chat`，也不会进入拆线重布 skill。

全局 BGA fanout 仍保留强规则保护：如果文本是明确的强执行请求，LLM 误判为 `chat` 时不会直接覆盖为普通聊天。

简化理解：

```python
if llm says chat with high confidence and request looks like reroute:
    return _INTENT_CHAT
if llm says reroute with high confidence:
    return _INTENT_PCB_REROUTE_SELECTED
if no valid llm decision:
    fall back to keyword rules
```

### 2.4 关键词兜底

当 LLM 不可用、未返回有效 JSON、或置信度不足时，才走关键词兜底：

```python
if _REROUTE_RE.search(text) and _PCB_DOMAIN_RE.search(text):
    return _INTENT_PCB_REROUTE_SELECTED
if self._is_strong_pcb_intent(text):
    return _INTENT_PCB_ENTRY
```

关键词兜底保障离线或模型识别失败时，明确操作请求仍能进入 PCB 流程。

### 2.5 流程状态识别

当 session 已经处于 PCB 流程中，还会根据状态处理后续轮次：

- `wait_selection`：用户选择 BGA；
- `wait_confirm`：用户确认执行布线；
- `routing`：正在执行，回复等待提示；
- `idle`：普通空闲状态。

状态更新入口：

```python
def _update_route_state_from_fields(self, session_id: str, pcb_fields: Dict[str, Any]) -> None:
    ...
    if "fanoutParams" in pcb_fields:
        self._set_flow_state(session_id, _FLOW_WAIT_CONFIRM)
    if "selection" in pcb_fields:
        self._set_flow_state(session_id, _FLOW_WAIT_SELECTION)
```

## 3. Skill 匹配规则

在 `_handle_user_message()` 中，根据 `decision.intent` 设置 `auto_skill`：

```python
auto_skill = None
if decision.mode == _ROUTE_MODE_PCB:
    auto_skill = (
        "hardware/pcb-reroute"
        if decision.intent == _INTENT_PCB_REROUTE_SELECTED
        else "hardware/pcb-intelligence"
    )
```

对应关系：

| intent | auto_skill |
| --- | --- |
| `pcb_reroute_selected` | `hardware/pcb-reroute` |
| `pcb_entry` / BGA fanout 相关 followup | `hardware/pcb-intelligence` |
| `chat` | 不加载 PCB skill |

## 4. Skill 如何拼进上下文

Gateway 主循环在新 session 且存在 `event.auto_skill` 时加载 skill：

- 代码位置：`gateway/run.py`
- 关键逻辑：`_load_skill_payload()` + `_build_skill_message()`

```python
_auto = getattr(event, "auto_skill", None)
if _is_new_session and _auto:
    _loaded = _load_skill_payload(_sname, task_id=_quick_key)
    ...
    _part = _build_skill_message(_loaded_skill, _skill_dir, _note)
```

最终拼接方式：

```python
_combined_parts.append(event.text)
event.text = "\n\n".join(_combined_parts)
```

也就是说，skill 不是被“执行”的 Python 代码，而是作为模型可见的指令前缀拼到用户消息前面。

## 5. 拼接后的上下文示例

用户原始输入：

```text
请帮我针对版图数据中的 BGA U2 的 net13、net17 拆线后重新布线
```

WebSocket router 先注入 project id：

```text
[projectid: proj-reroute-001]
请帮我针对版图数据中的 BGA U2 的 net13、net17 拆线后重新布线
```

auto skill 加载后，送入 Agent 的 `event.text` 近似如下：

```text
[SYSTEM: The "pcb-reroute" skill is auto-loaded. Follow its instructions for this session.]

# PCB Reroute Skill - 局部拆线重布

## 目标

本技能用于局部拆线重布场景。用户在自然语言中明确给出需要处理的 net 名称，智能体从文本中提取这些 net，调用 EDA 侧 MOCK 拆线工具，再生成局部重布结果包返回给 EDA。

...

## 工作流程

1. 判断用户是否明确要求局部拆线重布。
2. 调用 `drop_net(userText, projectID)`。
3. 如果 `drop_net` 返回 `error` 或 `selectedNets` 为空，提示用户明确写出 net 名称。
4. 调用 `reroute()`，优先使用 `drop_net` 的 session 缓存。
5. 将 `rerouteResult`、`checkReport`、`explanation` 放入 `##PCB_FIELDS##` 返回。

...

[projectid: proj-reroute-001]
请帮我针对版图数据中的 BGA U2 的 net13、net17 拆线后重新布线
```

模型随后根据 skill 指令调用：

1. `drop_net(userText=..., projectID=...)`
2. `reroute()`
3. 输出 `##PCB_FIELDS##` 包裹的结构化结果

## 6. 工具执行链路

模型发出工具调用后进入：

```python
handle_function_call(...) -> registry.dispatch(...) -> tool handler
```

关键位置：

- `model_tools.py::handle_function_call()`
- `tools/registry.py::dispatch()`
- `tools/pcb_tools.py::drop_net()`
- `tools/pcb_tools.py::reroute()`

`drop_net` 会通过 WebSocket 发给 EDA：

```json
{
  "type": "tool-calls",
  "body": {
    "content": {
      "name": "drop_net_mock",
      "arguments": {
        "projectID": "proj-reroute-001",
        "nets": ["net13", "net17"],
        "userText": "..."
      }
    }
  }
}
```

`reroute` 读取 `drop_net` 缓存，生成：

```json
{
  "rerouteResult": {},
  "checkReport": {},
  "explanation": "..."
}
```

最终 WebSocket adapter 从 `##PCB_FIELDS##` 中提取字段并放入响应 body。
