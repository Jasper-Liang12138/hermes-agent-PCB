# PCB Agent 顺序图（SWSD 主控 + Agent 智能辅助版）

编写者：港科广-梁锦彬

## 核心职责边界

```text
WebSocket Adapter = 协议适配层
Intent Model = 动作候选与实体候选生成层
Agent Loop = 理解、补全、解释、建议、总结的 advisory 层
SWSD WorkflowController = 唯一流程主控与动作仲裁层
SWSD RuntimeBridge = 唯一工具 side-effect 执行层
SWSD ResponseBuilder = 唯一事实输出层
Hermes Experience Layer = memory / user-project model / skills 持续成长层
```

关键约束：

```text
Intent Model / Agent Loop 可以建议，但不能直接推进状态。
Intent Model / Agent Loop 可以解释，但不能发明最终事实字段。
Intent Model / Agent Loop 可以总结经验，但不能直接改 workflow 硬约束。
RuntimeBridge 才能执行 PCB 工具。
ResponseBuilder 才能生成前端结构化 final body。
```

## 1. 总体架构顺序图

```mermaid
sequenceDiagram
    autonumber
    participant FE as 前端 / 用户
    participant WS as WebSocket Adapter
    participant SWSD as SWSD WorkflowController
    participant IM as Intent Model
    participant AG as Agent Loop Advisory
    participant POL as SWSD DecisionPolicy
    participant RB as RuntimeBridge
    participant TOOLS as PCB Tools / EDA Tools
    participant RESP as SWSD ResponseBuilder
    participant EXP as Hermes Experience Layer

    FE->>WS: WebSocket message / body / tool-results
    WS->>SWSD: SWSDTurnEvent / SWSDFrontendBodyEvent / SWSDToolResultEvent
    SWSD->>IM: 请求 action candidates 和 entities
    IM-->>SWSD: IntentCandidateSet
    opt 需要语义补全/解释/建议
        SWSD->>AG: AgentAssistRequest(advisory only)
        AG-->>SWSD: AgentAssistResult(candidates / narrative / advice)
    end
    SWSD->>POL: 当前 state + candidates + experience hints + tool result
    POL-->>SWSD: 唯一 workflow action
    SWSD->>RB: 执行合法 workflow action
    RB->>TOOLS: 调用确定性 PCB / EDA 工具
    TOOLS-->>RB: tool result
    RB-->>SWSD: action result / runtime facts
    SWSD->>RESP: 生成结构化事实输出
    RESP-->>SWSD: fanoutParams / rerouteResult / checkReport / explanation
    SWSD->>EXP: 记录 memory facts / trace / skill candidate
    SWSD-->>WS: FrontendFrame
    WS-->>FE: WebSocket message
```

## 2. Intent Candidate 仲裁图

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户文本
    participant SWSD as SWSD Controller
    participant IM as Intent Model
    participant EXP as Experience Resolver
    participant POL as DecisionPolicy

    U->>SWSD: 当前输入 + active workflow state
    SWSD->>IM: 生成状态内 action candidates
    IM-->>SWSD: candidateActions(action, confidence, entities, reason)
    SWSD->>EXP: 查询 memory / project model / skill hints
    EXP-->>SWSD: hints / defaults / recovery candidates
    SWSD->>POL: state + 显式输入 + candidates + hints
    POL-->>SWSD: accepted action 或追问

    Note over POL: 优先级：显式 body/协议 > 当前 state > 控制词/hashtag > 合法 transition > intent candidate > experience hints > chat
```

## 3. Agent Advisory Mode 图

```mermaid
sequenceDiagram
    autonumber
    participant SWSD as SWSD Controller
    participant AG as Agent Loop
    participant POL as DecisionPolicy
    participant RESP as ResponseBuilder

    SWSD->>AG: AgentAssistRequest(purpose, facts, allowedOutputs, forbiddenActions)
    Note over AG: forbiddenActions 包括 call_tool / change_workflow_state / invent_structured_fields
    AG-->>SWSD: AgentAssistResult(candidates / narrativeText / recoveryAdvice)
    SWSD->>POL: 校验 candidate 是否符合当前 state 和 workflow graph
    POL-->>SWSD: 接受、拒绝或要求确认
    SWSD->>RESP: 用事实字段 + 可选 narrativeText 生成输出
```

Agent Loop 允许：

```text
语义补全：理解“之前那个更稳的方式”
参数候选：线宽、线距、router/module、orderLines
异常建议：arc 失败后建议 135+RL
叙事解释：restore/reroute/fanout 结果说明
经验总结：从 trace 提炼 memory/skill/pitfall
```

Agent Loop 禁止：

```text
直接推进 SWSD state
直接调用 PCB 工具
直接执行 deleteTracesForRerouting -> reroute
直接生成 routingResult/rerouteResult/checkReport/fanoutParams 作为事实
覆盖用户显式输入
```

## 4. Hermes Grow-With-You 闭环图

```mermaid
sequenceDiagram
    autonumber
    participant SWSD as SWSD Workflow
    participant REC as Experience Recorder
    participant MEM as Memory Facts
    participant MODEL as User / Project Model
    participant SKILL as Procedural Skill Bank
    participant DIS as Experience Distiller
    participant RES as Experience Resolver

    SWSD->>REC: workflow event / tool result / final outcome
    REC->>MEM: 写入短中期事实
    REC->>DIS: 异步提交 workflow trace
    DIS->>MODEL: 归纳稳定偏好 / 项目约定
    DIS->>SKILL: 生成或 patch skill candidate
    RES->>MEM: 查询 last selection / last draft / reroute context
    RES->>MODEL: 查询默认语言、router 偏好、输出协议
    RES->>SKILL: 查询 recovery/default/validation signals
    RES-->>SWSD: PCBContextHints
```

使用边界：

```text
Memory Facts 只提供恢复线索，不直接执行。
User / Project Model 只提供低优先级默认，不覆盖用户显式输入。
Procedural Skills 提供流程建议、pitfall、recovery rule，但必须经 SWSD action 生效。
Experience Distiller 异步执行，不阻塞用户响应。
```

## 5. Fanout 主链图

```mermaid
sequenceDiagram
    autonumber
    participant FE as 前端 / 用户
    participant WS as WebSocket Adapter
    participant SWSD as SWSD Controller
    participant IM as Intent Model
    participant AG as Agent Advisory
    participant RB as RuntimeBridge
    participant TOOLS as PCB Tools
    participant RESP as ResponseBuilder
    participant EXP as Experience

    FE->>WS: 对 U5 布线，线宽 30 / 修改参数 / 确认
    WS->>SWSD: SWSDTurnEvent
    SWSD->>IM: 生成 action candidates 和 entities
    IM-->>SWSD: select_target / modify_params / confirm_route candidates
    opt 语义不完整
        SWSD->>AG: semantic_completion request
        AG-->>SWSD: 补全候选，不执行工具
    end
    SWSD->>SWSD: 校验当前 state 与合法 transition
    alt generate_fanout_params
        SWSD->>RB: generate_fanout_params
        RB->>TOOLS: generateFanoutParams
        TOOLS-->>RB: fanoutParams
    else run_fanout_route
        SWSD->>RB: run_fanout_route
        RB->>TOOLS: route
        TOOLS-->>RB: routingResult / report
    else rerun_fanout
        SWSD->>RB: 清理旧结果，保留目标和显式约束
    end
    RB-->>SWSD: runtime facts
    SWSD->>RESP: 生成 fanout final fields
    RESP-->>SWSD: fanoutParams / routingResult / report
    SWSD->>EXP: 记录 draft / router choice / outcome
    SWSD-->>WS: FrontendFrame
    WS-->>FE: message + structured body
```

关键语义：

```text
“重新生成参数/重新布线”在 fanout 活跃态默认是 rerun_fanout。
“拆线重布/reroute/删除框选走线重布”才切 reroute。
“回到上一步，用 135+RL”先 rollback，再用显式 router choice 覆盖历史参数。
纯数字 1/2/3/4 不触发 router 选择。
```

## 6. Reroute 多轮续跑图

```mermaid
sequenceDiagram
    autonumber
    participant FE as 前端 / 用户
    participant WS as WebSocket Adapter
    participant SWSD as SWSD Controller
    participant RB as RuntimeBridge
    participant EDA as 前端 EDA Tool
    participant TOOLS as pcb_tools.reroute / 局部布线完善
    participant RESP as ResponseBuilder
    participant EXP as Experience

    FE->>WS: 拆线重布 / 再 reroute 一次
    WS->>SWSD: SWSDTurnEvent
    SWSD->>SWSD: action = reroute_entry / reroute_again
    SWSD->>RB: request_delete_traces
    RB-->>WS: ToolCallRequest(deleteTracesForRerouting)
    WS-->>FE: tool-calls: deleteTracesForRerouting
    FE->>WS: tool-results: selected traces deleted
    WS->>SWSD: SWSDToolResultEvent(deleteTracesForRerouting.result)
    SWSD->>SWSD: action = complete_reroute
    SWSD->>RB: complete_reroute
    RB->>TOOLS: pcb_tools.reroute(session_id)
    TOOLS-->>RB: reroute payload / local_completion_passed
    RB-->>SWSD: runtime facts
    SWSD->>RESP: build_reroute_final
    RESP-->>SWSD: rerouteResult / checkReport / explanation
    SWSD->>EXP: 记录 reroute context 和 outcome
    SWSD-->>WS: FrontendFrame
    WS-->>FE: final structured message

    Note over SWSD,TOOLS: 第二轮、第三轮都复用同一条 complete_reroute 链，不回到 provider/LLM 续跑。
```

## 7. Restore 解释图

```mermaid
sequenceDiagram
    autonumber
    participant FE as 前端 / 用户
    participant WS as WebSocket Adapter
    participant SWSD as SWSD Controller
    participant STORE as Fanout Version Store
    participant AG as Agent Advisory
    participant RESP as ResponseBuilder

    FE->>WS: 恢复上一版 / 恢复第 N 版
    WS->>SWSD: SWSDTurnEvent
    SWSD->>STORE: 读取 params version 或 layout checkpoint
    STORE-->>SWSD: restored facts
    SWSD->>RESP: 生成 changedFields / previousValues / currentValues
    opt 需要更自然说明
        SWSD->>AG: explanation request with facts
        AG-->>SWSD: narrativeText only
    end
    SWSD->>RESP: attach narrativeText
    RESP-->>SWSD: restore summary + structured fields
    SWSD-->>WS: FrontendFrame
    WS-->>FE: 恢复说明和结构化字段
```

## 当前验证重点

```text
1. WebSocket 不再作为业务流程主控。
2. Intent Model 输出 action candidates，不直接写状态。
3. Agent Loop 只在 advisory/chat 边界内工作。
4. SWSD DecisionPolicy 是唯一动作仲裁点。
5. RuntimeBridge 是唯一 PCB 工具 side-effect 执行点。
6. ResponseBuilder 是唯一结构化 final body 生成点。
7. Experience Layer 只提供 hints/defaults/skills，不越权执行。
8. 第二轮 reroute 不再回到 LLM/provider 续跑，因此不应再出现 401 打断流程。
```

## 一句话理解这版

```text
SWSD 开车，RuntimeBridge 执行，ResponseBuilder 报告事实；Intent Model 和 Agent Loop 坐副驾驶，负责理解、补全、解释、建议和总结；Hermes Experience 负责让系统越用越懂项目和用户。
```
