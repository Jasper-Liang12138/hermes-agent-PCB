# SWSD 意图决策架构 Review 报告

日期：2026-06-24
范围：`docs/agent顺序图_dev.md` 描述的「SWSD 主控 + Agent 智能辅助」架构 vs 当前代码实现
关注点：前端实测流程不稳，控制词（如「回到上一步」）靠规则层硬识别，不智能、不泛化。目标是让**模型先理解并输出决策分类**，SWSD 依据分类 + 当前状态做流程跳转，WebSocket 只承担输入输出协议。

---

## 一、结论先行

顺序图描述的分层职责在代码里**部分存在、但没有真正落地**：

1. **模型没有进入实时决策环路。** LLM 意图分类器 `_classify_route_intent_with_llm` 已实现，但生产路径 `websocket.py:1047` 调用时写死 `llm_intent=None`，分类器**只在测试里被调用**。线上每一轮都直接跌落到正则规则。这是「不智能、不泛化」的根因。
2. **控制词识别是三套重复的硬编码正则**，分散在 `control_signals.py`、`intent.py`、`intent_policy.py`，且 `websocket.py` 自己又有第四套。「回到上一步」「拆线重布」「135+RL」全靠 `re.compile` 字符串匹配。
3. **WebSocket 层承担了大量业务主控**：路由决策、状态机跳转、控制词识别、工具调用决策、skill 选择，全在 `websocket.py` 里。它不是协议适配层，而是事实上的流程主控。
4. **DecisionPolicy 不消费「模型决策分类」**。它消费的是 `IntentFieldOutput`（概率分布）+ 阈值，最终仍回落到正则驱动的 `apply_swsd2_policy`。顺序图里「POL 是唯一动作仲裁点」在结构上成立，但仲裁输入不是模型分类，而是规则。
5. **Agent Advisory 契约（`AgentAssistRequest/Result` + forbiddenActions）已定义但未接线**，没有任何实时路径把它接进 WorkflowController。

顺序图里设计正确的部分：第二轮 reroute 确实不回 LLM（`runtime_bridge.py` 直连 `pcb_tools.reroute`），ResponseBuilder 不发明字段。这两点不是问题。

---

## 二、逐条核对（对照顺序图第 272-283 行「当前验证重点」）

| # | 顺序图要求 | 实际 | 证据 |
|---|---|---|---|
| 1 | WebSocket 不再作为业务流程主控 | ❌ 不达标 | `websocket.py:4945-5124` `_decide_route` 是 180 行业务状态机；`2628-2640` 直接 `_set_flow_state` + `_swsd_update` |
| 2 | Intent Model 输出 action candidates，不直接写状态 | ⚠️ 部分 | Intent 输出 candidate，但**模型分类未进线上环路**（`websocket.py:1047` `llm_intent=None`） |
| 3 | Agent Loop 只在 advisory 边界工作 | ❌ 未接线 | 契约在 `action_candidates.py:60-81`，但无实时调用方 |
| 4 | DecisionPolicy 是唯一动作仲裁点 | ⚠️ 部分 | `decision_policy.py:83` 仲裁结构存在，但输入是概率+正则，非模型分类 |
| 5 | RuntimeBridge 是唯一工具 side-effect 执行点 | ⚠️ 部分 | reroute 在 `runtime_bridge.py:129`；但 `deleteTracesForRerouting` 在 `websocket.py:2655` 单独发起 |
| 6 | ResponseBuilder 是唯一结构化输出点 | ✅ 达标 | `response_builder.py:18-37` 只从工具结果组装，不发明字段 |
| 7 | Experience Layer 只提供 hints/defaults | ✅ 达标（未越权） | — |
| 8 | 第二轮 reroute 不回 LLM，无 401 打断 | ✅ 达标 | `runtime_bridge.py:129-157` 直连工具 → report，不重入 LLM |

---

## 三、根因详解

### 问题 1：模型分类器存在但线上从不调用（最高优先级）

生产路径：

```python
# gateway/platforms/websocket.py:1047
decision = self._decide_route(session_id, raw_user_text, llm_intent=None)  # ← 写死 None
```

`_decide_route` 进而调用 `_validate_route_intent(session_id, text, llm_intent)`：

```python
# websocket.py:4070
route_intent = self._coerce_route_intent(llm_intent)   # None → None
if route_intent is not None:                            # 跳过整个 LLM 分支
    ...
# 落到下面的纯规则 policy
```

而真正的模型分类器 `_classify_route_intent_with_llm`（`websocket.py:4015`）只在 `tests/gateway/test_websocket_pcb_flow.py:142` 等测试里被 await。**线上没有任何 `await self._classify_route_intent_with_llm(...)`，也没有把结果回填给 `_decide_route`。**

后果：测试全绿（测试直接喂 `llm_intent=...`），但真机跑的是规则兜底，所以「测试过、前端不过」。

### 问题 2：控制词靠四套重复硬编码正则

- `control_signals.py:8-16`：`_ROLLBACK_RE = 回到上一步|上一步|rollback|回退` 等
- `intent.py:13-17`：又一套 `_ROLLBACK_RE / _REROUTE_RE / _CONFIRM_RE`
- `intent_policy.py:13-69`：第三套，更长的 `_REROUTE_RE / _ROUTER_RE / _TARGET_RE`
- `websocket.py:175-199`：第四套 `_REROUTE_RE / _PCB_ACTION_RE / _CONFIRM_RE`

「回到上一步」只能命中字面词。用户说「退回去刚才那个参数」「还是用上一版吧」就漏判。这正是「不泛化」。

### 问题 3：纯数字 1/2/3/4 没有防护

顺序图第 207 行要求「纯数字 1/2/3/4 不触发 router 选择」。`intent_policy.py` 的 `_ROUTER_RE` 不匹配裸数字，但 `intent_policy.py:267` 在 `FLOW_WAIT_ROUTER_TYPE` 状态下，只要 `raw_intent == INTENT_PCB_FOLLOWUP` 就接受为 router 选择。模型若把「1」分类成 followup，会被当 router 选择放行。无显式 guard。

### 问题 4：DecisionPolicy 仲裁输入错位

`decide_with_intent_field`（`decision_policy.py:137-184`）消费的是 `IntentFieldOutput`（chat/analyze/execute/meta 四个概率 + uncertainty），靠阈值（0.35 / 0.55 / 0.65）路由，命中后仍调用正则驱动的 `apply_swsd2_policy`（line 162）。它不是在仲裁「模型给出的离散决策类型」，而是在给概率分桶。顺序图想要的「模型输出决策分类 → SWSD 按分类+状态跳转」并不存在。

---

## 四、修改建议

核心思路：**把「决策分类」做成模型的一等输出，让它成为 DecisionPolicy 的主输入；规则降级为兜底而非主路径；WebSocket 退回纯协议层。**

### 建议 1：定义统一的决策分类契约（DecisionClass）

新建 `agent/swsd/decision_class.py`，定义一个**与状态无关的离散决策枚举**，作为模型输出的唯一目标 schema：

```python
class DecisionClass(str, Enum):
    CONFIRM          = "confirm"            # 确认/继续/执行
    REJECT           = "reject"             # 拒绝/跳过
    CANCEL           = "cancel"             # 取消/退出流程
    ROLLBACK         = "rollback"           # 回到上一步/恢复上一版
    REROUTE          = "reroute"            # 拆线重布
    RERUN            = "rerun"              # 重新生成参数/重新布线（活跃 fanout 态默认）
    SELECT_TARGET    = "select_target"      # 选 BGA/器件
    SET_ROUTER       = "set_router"         # 指定 router/算法（arc/rl/135...）
    SET_PARAM        = "set_param"          # 改线宽/线距等参数
    CHAT             = "chat"               # 闲聊/提问
    UNCLEAR          = "unclear"            # 需追问

@dataclass(frozen=True)
class DecisionClassification:
    decision: DecisionClass
    confidence: float
    entities: dict[str, Any]   # {"router":"rl_135","width":30,"target":"U5",...}
    reason: str                # 模型自述依据，便于排查
```

`decision` 表达「用户想干什么」，与「当前在哪个状态、是否合法」解耦——后者交给 SWSD。这样模型不需要懂状态机，只需要懂语义。

### 建议 2：让模型输出 DecisionClassification，规则做兜底

改造 `intent.py:classify_intent_with_planning_model`，把 system prompt 改成强约束的分类任务（输出上面 schema 的 JSON），并把现有 `classify_intent_rules` 降级为**仅当模型超时/解析失败时的 fallback**，且在返回里标注 `source="rules"` 以便监控降级率。

关键：让模型看到 few-shot，覆盖「退回去刚才那个」「还是用上一版」「再来一次但用折角」这类泛化说法 → 都映射到 `ROLLBACK / RERUN+SET_ROUTER`。

### 建议 3：把分类器真正接进线上路径（修复问题 1）

这是收益最大的一步。在 `_handle_message` 里，路由前先 await 分类器，并把结果传进 `_decide_route`：

```python
# websocket.py ~1047 替换
llm_intent = None
if self._route_intent_llm_enabled and not is_slash_command:
    llm_intent = await self._classify_route_intent_with_llm(
        session_id=session_id, user_text=raw_user_text, project_id=project_id,
    )
decision = self._decide_route(session_id, raw_user_text, llm_intent=llm_intent)
```

仅此一行改动，就能让已存在但闲置的模型分类生效。建议同时加一条监控日志：分类来源（model/rules）、降级率。

### 建议 4：DecisionPolicy 以 DecisionClass 为主输入做状态仲裁

新增 `decide_from_classification(classification, workflow_context)`，逻辑为「分类语义 × 当前状态 × 合法 transition」的查表/仲裁，替代「概率分桶 + 正则」：

```python
def decide_from_classification(c, ctx):
    allowed = set(ctx.allowed_transitions)
    intended = MAP[c.decision]          # DecisionClass → 候选 workflow action（可能多个）
    # 例：RERUN 在 fanout 活跃态 → rerun_fanout；REROUTE → reroute_entry
    for action in intended:
        if action in allowed:
            return SWSDDecision(action, c.confidence, reason="classification_accepted")
    # 不合法：按状态给出追问，而不是硬塞
    return SWSDDecision("", c.confidence, requires_confirmation=True, reason="not_allowed_in_state")
```

这样顺序图第 83 行的优先级（显式 body > 当前 state > 控制词 > 合法 transition > intent candidate > experience hints > chat）能真正在一个地方表达，而不是散在四处正则里。

### 建议 5：补 1/2/3/4 防护（修复问题 3）

在 `decide_from_classification` 里，若 `c.decision == SET_ROUTER` 但 `entities.router` 为空且原文 `re.fullmatch(r"\s*\d+\s*", text)`，降级为 `UNCLEAR` 并追问，落实顺序图第 207 行约束。

### 建议 6：WebSocket 退回协议层

把 `websocket.py` 里的 `_decide_route` 业务状态机、控制词正则（175-199）、`_run_direct_*` 直接动作、skill 选择，迁移到 WorkflowController + DecisionPolicy。WebSocket 只保留：解析 body / sessionId / projectId → 构造 `SWSDTurnEvent`（原文 + 元数据）→ 交给 SWSD → 把 SWSD 产出的 FrontendFrame 发回。这一步工作量大，建议放在建议 1-5 之后，作为结构性收尾。

### 建议 7：合并四套正则为单一兜底模块

把 `control_signals.py / intent.py / intent_policy.py / websocket.py` 的重复正则收敛到 `control_signals.py` 一处，仅作为「模型不可用时的 fallback 分类器」，消除三处漂移。

---

## 五、落地顺序（按收益/风险）

1. **建议 3（接线模型分类）** —— 一行级改动，立刻让线上用上模型，验证「不智能」是否缓解。低风险。
2. **建议 1 + 2（DecisionClass 契约 + 模型输出 + 规则兜底）** —— 中等改动，泛化能力的主体。
3. **建议 4 + 5（DecisionPolicy 以分类为主输入 + 数字防护）** —— 中等改动，把仲裁逻辑收口。
4. **建议 7（合并正则）** —— 低风险清理。
5. **建议 6（WebSocket 瘦身）** —— 大改动，结构收尾，放最后。

每一步都应补：分类来源/降级率日志，以及「测试喂 llm_intent」之外的**真机回归用例**（覆盖泛化说法），避免再次出现「测试过、前端不过」。

---

## 六、关键证据索引

- 模型分类闲置：`gateway/platforms/websocket.py:1047`（`llm_intent=None`）、`4015`（分类器定义）、`4070`（None 即跳过 LLM 分支）
- 四套正则：`control_signals.py:8-16`、`intent.py:13-17`、`intent_policy.py:13-69`、`websocket.py:175-199`
- 规则兜底：`intent.py:64-71`（解析失败回 `classify_intent_rules`）
- 概率仲裁非分类：`decision_policy.py:137-184`
- 数字无防护：`intent_policy.py:267`
- WebSocket 业务主控：`websocket.py:4945-5124`、`2628-2640`、`2655`
- 达标项：`runtime_bridge.py:129-157`（reroute 不回 LLM）、`response_builder.py:18-37`（不发明字段）、`action_candidates.py:60-81`（advisory 契约已定义）
