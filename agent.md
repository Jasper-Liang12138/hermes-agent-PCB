# 预计改动计划：入口分流与 Fanout 链路

> 注意：本文档是后续改动计划和执行顺序说明，并不表示当前代码已经完成这些改动。  
> 当前代码仍是现有 `LangGraph` 分支实现；以下内容用于指导接下来逐步修改。

## 1. 背景

同门建议的方向：

```text
先改入口，前端是三个“按钮”，后端接收信号进入不同 chain。
然后一步步往下改，一个节点一个节点往下改。
先按 /Users/keke/coding/PCB-Agent/notes/开发文档0707.md 改 fanout 链路。
暂时不改 reroute。
```

当前代码状态：

```text
前端 WebSocket message
  -> server.py
  -> agent.py
  -> LangGraph
  -> planner 自动识别 qa / global_fanout / reroute
```

预计目标：

```text
前端三个按钮信号
  -> 后端显式识别入口 module / chain
  -> 写入 state
  -> planner 优先按入口信号进入对应 chain
  -> fanout 链路按文档逐节点改造
```

## 2. 改动原则

- 先改入口分流，再改 fanout 内部节点。
- 保留旧文本消息兼容，避免前端旧协议和现有测试直接失效。
- 暂时不改 reroute 内部链路。
- 如果必须触碰包含 reroute 分支的公共文件，只做入口分流或 fanout 分支改动，不改 reroute 行为。
- 每一步都配套测试，避免一次性大改。

## 3. 第一阶段：入口显式分流

目标：支持前端三个按钮显式传入任务类型。

按钮预计对应：

```text
qa              -> pcb_qa_flow
global_fanout   -> pcb_escape_flow
reroute         -> pcb_reroute_flow
```

需要设计兼容字段，例如：

```json
{
  "type": "message",
  "body": {
    "role": "user",
    "content": "",
    "module": "global_fanout",
    "action": "enter"
  }
}
```

或：

```json
{
  "type": "message",
  "body": {
    "role": "user",
    "content": "",
    "chain": "fanout"
  }
}
```

最终字段名需要和前端确认。后端实现时建议做多字段兼容：

```text
module / chain / taskType / workflow
```

预计涉及文件：

- `pcb_agent_langgraph/websocket/protocol.py`
  - 扩展 `parse_user_message` 返回结构，支持 module/chain/action。

- `pcb_agent_langgraph/websocket/server.py`
  - 把入口信号传入 agent。

- `pcb_agent_langgraph/agent.py`
  - `ainvoke` 接收入口信号。
  - 创建 state 时写入显式入口字段。

- `pcb_agent_langgraph/graph/state.py`
  - 增加可选字段，例如 `entry_module`、`entry_action`、`entry_payload`。

- `pcb_agent_langgraph/planner/planner.py`
  - planner 优先使用显式入口字段，而不是完全依赖文本识别。

预计测试：

- `tests/test_websocket_protocol.py`
- `tests/test_planner.py`

验收标准：

```text
前端点 QA 按钮       -> task_type=qa, workflow_id=pcb_qa_flow
前端点 Fanout 按钮   -> task_type=global_fanout, workflow_id=pcb_escape_flow
前端点 Reroute 按钮  -> task_type=reroute, workflow_id=pcb_reroute_flow
旧文本 fanout/reroute 仍能按原逻辑工作
```

## 4. 第二阶段：Fanout 链路入口节点

目标：按开发文档进入 fanout 后，先进入 BGA 相关数据准备和前端选择节点。

文档要求：

```text
用户选择“BGA 逃逸布线”
  -> agent 获得 BGA 列表，以及每一个 BGA 的类型返回给前端
  -> 前端提示用户在版图中选择目标 BGA
```

当前代码已有：

```text
getProjectData
-> pcb_extra_bga
-> selection
```

但当前 selection 主要包含：

```text
label
detail
pins/package/footprint 等摘要
```

预计改动：

- 如果 BGA 类型由前端提供，则后端不负责识别“矩形/交错”等类型。
- 后端需要定义协议字段，接收并透传/缓存前端传入的 BGA 类型。
- 如果后端脚本提取 BGA 候选，则只作为候选列表来源；类型字段优先使用前端传入。

可能字段：

```json
{
  "selectedBGA": "U5",
  "bgaType": "rectangular",
  "bgaLayoutType": "staggered",
  "algorithm": "rule_135"
}
```

字段名需和前端确认。

预计涉及文件：

- `pcb_agent_langgraph/planner/planner.py`
  - fanout 入口和 selection 输出字段。

- `pcb_agent_langgraph/graph/nodes.py`
  - `selection` / `fanout_params` 返回前端字段。
  - `_update_cache_from_tool` 写入 BGA 候选和后续参数。

- `pcb_agent_langgraph/agent.py`
  - 缓存前端传入的 BGA 类型、算法选择等结构化字段。

- `pcb_agent_langgraph/websocket/protocol.py`
  - 解析前端选择结果。

- `tools/extract_bga_components.py`
  - 仅在需要后端补充候选信息时调整。
  - 如果类型完全由前端传入，则不一定需要改。

预计测试：

- `tests/test_planner.py`
- `tests/test_bga_extract.py`
- `tests/test_websocket_protocol.py`

## 5. 第三阶段：Fanout 选择 BGA 节点

目标：前端选择目标 BGA 后，后端明确进入下一节点，而不是靠自然语言猜。

当前代码支持用户回复：

```text
U5
135
确认
```

预计改动后支持结构化选择：

```json
{
  "module": "global_fanout",
  "action": "select_bga",
  "selectedBGA": "U5",
  "bgaType": "rectangular"
}
```

预计涉及文件：

- `pcb_agent_langgraph/agent.py`
  - `_cache_for_turn` 支持结构化 payload。

- `pcb_agent_langgraph/planner/planner.py`
  - fanout 分支读取 `selectedBGA`、`bgaType`。

- `pcb_agent_langgraph/websocket/protocol.py`
  - parse 前端选择结果。

- `pcb_agent_langgraph/graph/state.py`
  - 如有必要，补充结构化入口/动作字段。

验收标准：

```text
前端传 selectedBGA 后，planner 不再停在 select_bga
如果 routerType/algorithm 未选择，则进入 router_type_prompt 或算法选择节点
```

## 6. 第四阶段：Fanout 参数收集与人工确认节点

文档要求前端收集：

```text
BGA 信息
版图信息
网络信息
规则管理器配置
BGA 类型
算法选择
```

当前代码已有：

```text
fanoutParams
selectedBGA
targetBGAs
routerType
constraints
orderLines
```

预计改动：

- 定义文档字段到当前 `fanoutParams` 的映射关系。
- 明确 `ruleManagerConfig` 是由前端直接传入，还是后端只保存引用 ID。
- 明确 BGA 类型和算法选择字段进入 `fanoutParams` 或 `fanoutEntities`。
- 保留现有 JSON 参数确认兼容逻辑。

预计涉及文件：

- `pcb_agent_langgraph/agent.py`
  - `_parse_fanout_param_confirmation`
  - `_merge_fanout_params`

- `pcb_agent_langgraph/planner/planner.py`
  - fanout 参数确认阶段。

- `pcb_agent_langgraph/graph/nodes.py`
  - `reflect` 返回 `fanoutParams` 给前端。

- `pcb_agent_langgraph/tools/external.py`
  - `_generate_fanout_params`
  - `_escape_order_result`

预计测试：

- `tests/test_planner.py`

## 7. 第五阶段：Layer Assign 与 Escape Order 节点

目标：按文档生成层分配方案和逃逸顺序，并进入人工确认界面。

当前代码：

```text
layer_assign
-> escape_order
-> fanout_params_review
```

预计改动：

- 确认外部工具输入是否需要新增：
  - BGA 类型
  - 算法选择
  - 规则管理器配置
  - 网络信息
- 确认 `fanoutParams` 返回前端字段是否满足人工确认 UI。
- 输出层分配和逃逸顺序的结构化摘要，避免前端只拿到文本行。

预计涉及文件：

- `pcb_agent_langgraph/tools/external.py`
  - `_generate_fanout_params`
  - `_escape_order_result`

- `pcb_agent_langgraph/graph/nodes.py`
  - `_update_cache_from_tool`
  - `reflect`

- `pcb_agent_langgraph/reports/markdown.py`
  - layer/escape summary。

- `tests/test_planner.py`

## 8. 第六阶段：Fanout Route、导入与结果报告

目标：用户确认参数后调用布线器，完成 fanout route，返回结果和报告。

当前代码：

```text
fanout_route
-> importLines
-> result_review
```

预计改动：

- 对齐文档里的结果字段：
  - routeRate
  - routedNetCount
  - totalNetCount
  - failedNetCount
  - layoutResultId / reportId 如果前端需要
- 当前报告已有 `build_fanout_route_report`，需要确认字段是否满足前端。
- 重新布线入口只处理 fanout，不触碰 reroute。

预计涉及文件：

- `pcb_agent_langgraph/planner/planner.py`
  - fanout route 后的状态推进。

- `pcb_agent_langgraph/tools/external.py`
  - `_run_fanout_route`

- `pcb_agent_langgraph/graph/nodes.py`
  - `reflect`
  - `_update_cache_from_tool`

- `pcb_agent_langgraph/reports/markdown.py`
  - `build_fanout_route_report`

- `tests/test_planner.py`

## 9. 暂不改动范围

暂时不改 reroute 内部逻辑：

- `pcb_agent_langgraph/tools/reroute_context.py`
- `tools/pcb_local_router.py`
- `tools/pcb_reroute_drc.py`
- `pcb_agent_langgraph/tools/external.py` 中：
  - `_prepare_reroute_inputs`
  - `_compress_reroute_context`
  - `_run_reroute`
  - `_run_help_planner`
  - `_run_drc`
  - `_run_explain_model`

可能会轻微触碰公共文件：

- `planner.py`
- `state.py`
- `server.py`
- `protocol.py`
- `agent.py`

但目的仅限入口分流和 fanout 链路，避免改变 reroute 行为。

## 10. 建议第一批提交范围

第一批只做入口显式分流，不改 fanout 工具内部：

```text
protocol.py
server.py
agent.py
state.py
planner.py
tests/test_websocket_protocol.py
tests/test_planner.py
```

第一批完成后的目标：

```text
三个按钮能稳定进入三个 workflow
旧文本入口仍兼容
fanout 内部流程暂时还是旧实现
reroute 行为不变
```

之后再从 fanout 的第一个节点开始，按文档逐节点改。

## 11. 风险点

- 前端按钮字段名还未确认，需要先约定协议。
- 文档里的 `规则管理器配置`、`BGA类型`、`算法选择` 当前代码没有完整结构化字段，需要定义映射。
- 当前代码把 fanout 和 reroute 分支写在同一个 `planner.py`，修改 fanout 时要避免误伤 reroute。
- 当前 `selection` 只返回简单 label/detail，如果前端 UI 需要更多字段，需要扩展但保持兼容。
- 如果 BGA 类型由前端传入，后端不应重复识别，只需要接收、校验、缓存和传给后续 fanout 工具。

## 12. 本次已实际改动：入口显式分流 + 测试

> 本节记录已经真实落地的改动，区别于上面的预计计划。  
> 本次只做入口显式分流，不改 fanout 内部节点，也不改 reroute 内部链路。

已改动文件：

- `pcb_agent_langgraph/entry.py`
  - 新增入口归一化模块。
  - 统一支持 `qa`、`fanout/global_fanout/pcb_escape_flow`、`reroute/pcb_reroute_flow` 等别名。
  - 统一映射到：
    - `qa -> pcb_qa_flow`
    - `global_fanout -> pcb_escape_flow`
    - `reroute -> pcb_reroute_flow`

- `pcb_agent_langgraph/websocket/protocol.py`
  - `parse_user_message` 从旧三元组扩展为 `UserMessage` 对象。
  - 仍兼容旧写法：

```python
session_id, project_id, content = parse_user_message(msg)
```

  - 新增读取前端入口字段：
    - `module`
    - `chain`
    - `taskType`
    - `task_type`
    - `workflow`
    - `workflow_id`
  - 新增读取 `action/entryAction/entry_action`。
  - 新增 `entry_payload`，保留结构化附加信息，例如后续可用的 `selectedBGA`、`bgaType` 等字段。

- `pcb_agent_langgraph/websocket/server.py`
  - `_start_agent_turn` 和 `_run_agent_turn` 新增 `entry_module`、`entry_action`、`entry_payload` 参数。
  - WebSocket handler 会把 `parse_user_message` 解析出的入口信号传给 `PCBLangGraphAgent.ainvoke`。

- `pcb_agent_langgraph/agent.py`
  - `PCBLangGraphAgent.ainvoke` 新增可选关键字参数：
    - `entry_module`
    - `entry_action`
    - `entry_payload`
  - 有显式入口时写入 state：
    - `entry_module`
    - `entry_action`
    - `entry_payload`
  - 同时预设：
    - `task_type`
    - `workflow_id`
  - 如果显式入口和上一轮 workflow 不同，会把 `workflow_state` 重置为 `idle`，避免旧流程状态误导新按钮入口。

- `pcb_agent_langgraph/graph/state.py`
  - `PCBState` 新增入口字段：
    - `entry_module`
    - `entry_action`
    - `entry_payload`
  - `initial_state` 默认填入空入口字段。

- `pcb_agent_langgraph/planner/planner.py`
  - planner 在调用模型之前先检查显式入口。
  - 有显式入口时直接走确定性规则状态机，`planner_source=entry`。
  - 显式入口优先级高于自然语言文本推断，避免按钮是 fanout 但文本误触 reroute，或按钮是 QA 但文本误触 fanout。
  - 只改变入口选择，不改 reroute 内部执行顺序。

- `tests/test_websocket_protocol.py`
  - 新增入口信号解析测试。
  - 新增入口别名归一化测试。
  - 新增 WebSocket handler 传递入口信号到 Agent 的测试。
  - 更新旧 fake agent，使其兼容 `ainvoke(..., **kwargs)`。

- `tests/test_planner.py`
  - 新增 Fanout 按钮空文本进入 `pcb_escape_flow` 的测试。
  - 新增 QA 按钮优先于文本意图的测试。
  - 新增 Fanout 按钮优先于冲突 reroute 文本的测试。
  - 新增 Reroute 按钮空文本进入 `pcb_reroute_flow` 的测试。
  - 新增模型 planner 开启时，显式入口仍跳过模型猜测的测试。

本次入口协议目前可接受示例：

```json
{
  "type": "message",
  "body": {
    "role": "user",
    "content": "",
    "module": "fanout",
    "action": "enter"
  }
}
```

或：

```json
{
  "type": "message",
  "chain": "pcb_reroute_flow",
  "body": {
    "role": "user",
    "content": ""
  }
}
```

验证情况：

- 已通过 `python -m compileall pcb_agent_langgraph tests/test_websocket_protocol.py tests/test_planner.py`。
- 已手动验证：
  - `qa -> pcb_qa_flow`
  - `fanout -> pcb_escape_flow -> getProjectData`
  - `pcb_reroute_flow -> pcb_reroute_flow -> deleteTracesForRerouting`
  - WebSocket handler 会把 `chain: fanout` 传成 `entry_module=global_fanout`。
- 未能运行 `pytest tests/test_websocket_protocol.py tests/test_planner.py`，原因是当前本地 Python 环境没有安装 `pytest`：

```text
/opt/miniconda3/bin/python: No module named pytest
```

## 13. 本次调试补充：使用 `pcb_agent` 环境跑通测试

本次没有新建 `pcb-agent` 环境，按要求改用本机已有 conda 环境：

```text
pcb_agent -> Python 3.11.15, pytest 9.0.3
```

调试中额外修复了完整测试暴露出的三个小问题：

- `pcb_agent_langgraph/planner/planner.py`
  - 模型 planner 只返回 fanout 参数时，`_repair_model_calls` 生成的 `getProjectData` 调用现在保留 `_fanout_args(entities)`。
  - 这样 `selectedBGA`、`routerType`、`constraints` 不会在第一步规划结果里丢失。
  - 前端协议层仍会按 v0.6 对 `getProjectData` 去掉实际发送的 `arguments`，所以不改变前端工具消息格式。

- `pcb_agent_langgraph/reports/markdown.py`
  - `build_markdown_report("global_fanout", ...)` 的标题从通用 `PCB Report` 修为 `PCB Fanout Report`。

- `pcb_agent_langgraph/tools/external.py`
  - `_resolve_path` 现在会把配置中的 Windows 风格相对路径如 `.\\tools\\extract_bga_components.py` 归一化为当前系统可用路径。
  - `pcb_extra_bga` 脚本提取结果统一返回 `source="script"`。
  - cache 命中和脚本失败后 cache 兜底也会保留 `source="script"` 和 `script_path`，避免缓存路径与脚本来源契约不一致。

最终验证：

```text
conda run -n pcb_agent python -m pytest
66 passed in 0.21s
```

## 14. 本次已实际改动：planner 显式分流和 fanout 第一个节点

> 本节记录继续按 `/Users/keke/coding/PCB-Agent/notes/开发文档0707.md` 推进的 fanout 第一段。  
> 本次仍不改 reroute 内部链路，也不进入 fanout 的层分配、逃逸顺序、布线器内部改造。

目标对应文档中的 fanout 第一段：

```text
用户选择“BGA 逃逸布线”
        ↓
agent 获得 BGA 列表，以及每一个 BGA 的类型返回给前端
        ↓
前端提示用户在版图中选择目标 BGA
```

已改动文件：

- `pcb_agent_langgraph/planner/planner.py`
  - 显式 fanout 入口在已有 `projectData` 后，会进入第一个业务节点 `pcb_extra_bga`。
  - `select_bga` 返回的 `selection` 从简单 `label/detail` 扩展为结构化候选项。
  - 每个 BGA 候选现在包含：
    - `label`
    - `value`
    - `componentId`
    - `refdes`
    - `pinCount`
    - `pincount`
    - `footprint`
    - `package`
    - `part`
    - `bgaType`
    - `bgaLayoutType`
    - `typeSource`
  - `bgaType/bgaLayoutType` 支持归一化：
    - `矩形/rect/rectangular -> rectangular`
    - `交错/stagger/staggered -> staggered`
    - 未提供时为 `unknown`
  - `fanout` 工具参数白名单增加 `bgaType` 和 `bgaLayoutType`。

- `pcb_agent_langgraph/agent.py`
  - fanout 显式入口的 `entry_payload` 会写入 `intermediate_cache`。
  - 当前支持缓存：
    - `selectedBGA`
    - `targetBGAs`
    - `bgaType`
    - `bgaLayoutType`
    - `routerType/algorithm`
    - `constraints`
    - `bgaCandidates/bgaList/bgaComponents/components`
  - 这样如果前端已经传入 BGA 类型或 BGA 列表，后端不会丢失。

- `pcb_agent_langgraph/graph/nodes.py`
  - fanout 工具调用参数合并时透传 `bgaType/bgaLayoutType`。
  - `layer_assign` 结果回写 `fanoutEntities` 时保留 `bgaType/bgaLayoutType`。

- `pcb_agent_langgraph/tools/external.py`
  - 生成 `fanoutParams` 时保留 `bgaType/bgaLayoutType`，为后续 fanout 节点继续使用。

- `tests/test_planner.py`
  - 新增测试：显式 fanout 入口在已有 `projectData` 后进入 `pcb_extra_bga`。
  - 新增测试：`select_bga` 的 selection 包含 BGA 类型和候选元数据。
  - 新增测试：前端结构化 `entry_payload` 会写入 fanout cache。

当前第一段链路结果：

```text
Fanout 按钮
  -> planner_source=entry
  -> getProjectData
  -> pcb_extra_bga
  -> select_bga(selection 带 BGA 类型字段)
```

验证：

```text
conda run -n pcb_agent python -m pytest
69 passed in 0.20s
```

## 15. 本次已实际改动：Fanout 结构化选择 BGA 与参数字段映射

> 本节继续对应预计计划中的第三阶段和第四阶段前半段。  
> 本次仍不改 reroute 内部链路，也不进入 fanout route/导入/报告阶段改造。

目标对应计划：

```text
前端选择目标 BGA
        ↓
后端明确进入下一节点，而不是靠自然语言猜
        ↓
如果未选择算法/路由器，则进入 router_type_prompt
如果已选择算法/路由器，则进入 layer_assign

前端提交 BGA / layout / rules / routingPlan 等结构化字段
        ↓
后端缓存并映射到 fanoutParams / fanoutEntities
```

已改动文件：

- `pcb_agent_langgraph/websocket/protocol.py`
  - 支持 `body.content` 为结构化对象时解析入口字段。
  - 示例：

```json
{
  "type": "message",
  "body": {
    "role": "user",
    "content": {
      "module": "global_fanout",
      "action": "select_bga",
      "bga": {
        "componentId": "U5",
        "bgaType": "rectangular"
      }
    }
  }
}
```

- `pcb_agent_langgraph/agent.py`
  - `entry_payload` 的结构化 fanout 字段会进入 cache。
  - `selectedBGA / targetBGA / bga.componentId / bga.refdes` 会归一化为：
    - `fanoutEntities.selectedBGA`
    - `fanoutEntities.targetBGAs`
    - `fanoutEntities.bgaSelectionConfirmed`
  - `bgaType / bgaLayoutType` 会归一化为：
    - `rectangular`
    - `staggered`
  - `algorithm / routerType / routeType` 会归一化为：
    - `rule_135`
    - `rule_arc`
  - 文档字段会映射进 `fanoutParams`：
    - `bga`
    - `layout`
    - `rules`
    - `ruleManagerConfigId`
    - `routingPlan`
    - `layerAssignment`
    - `escapeOrder`
    - `orderLines`
    - `networkInfo`
    - `taskId`
    - `constraints`
  - `action` 属于以下值时会标记 `fanoutParamsConfirmed=True`：
    - `confirm`
    - `confirm_params`
    - `submit_params`
    - `parameter_config_result`
    - `start_routing`
    - `start_route`
    - `start_fanout`

- `pcb_agent_langgraph/planner/planner.py`
  - 沿用上一阶段显式入口优先的状态机。
  - 当前结构化选择效果：

```text
action=select_bga + selectedBGA + no routerType
  -> router_type_prompt

action=select_algorithm/select_router + selectedBGA + algorithm
  -> layer_assign
```

  - 这里没有新增 reroute 内部逻辑。

- `tests/test_websocket_protocol.py`
  - 新增测试：`body.content` 结构化对象中的 `module/action/bga` 能解析到 `entry_payload`。

- `tests/test_planner.py`
  - 新增测试：结构化选择 BGA 后进入 `router_type_prompt`。
  - 新增测试：结构化选择 BGA + 算法后进入 `layer_assign`，且携带 `bgaType/routerType`。
  - 新增测试：开发文档中的 `bga/layout/rules/routingPlan/networkInfo/taskId` 映射到 `fanoutParams`，并在 `start_routing` 时标记 `fanoutParamsConfirmed=True`。

当前可支持的结构化选择示例：

```json
{
  "module": "global_fanout",
  "action": "select_bga",
  "bga": {
    "componentId": "U5",
    "bgaType": "rectangular"
  }
}
```

当前可支持的结构化参数/启动示例：

```json
{
  "module": "bga_escape_routing",
  "action": "start_routing",
  "taskId": "task_1",
  "bga": {
    "componentId": "U5",
    "name": "BGA_XXX",
    "pinCount": 256,
    "bgaType": "rectangular"
  },
  "layout": {
    "boardId": "board_1",
    "snapshotId": "snap_1"
  },
  "rules": {
    "ruleManagerConfigId": "rule_1",
    "constraints": {
      "LineSpacing": 3
    }
  },
  "routingPlan": {
    "algorithm": "arc",
    "layerAssignment": [],
    "escapeOrder": []
  }
}
```

自查结果：

```text
conda run -n pcb_agent python -m pytest tests/test_websocket_protocol.py tests/test_planner.py -q
72 passed in 0.25s

conda run -n pcb_agent python -m pytest
73 passed in 0.26s
```
