# SWSD3 主控 + Agent 智能辅助 + Hermes Grow-With-You 工业级稳定性 Review

- 评审日期：2026-06-24
- 评审分支：`codex/swsd-full-workflow-framework`
- 评审范围：`agent/swsd/**`、`gateway/platforms/websocket.py`（SWSD 集成路径）、`run_agent.py`（SWSD 上下文注入）
- 评审基准：**以 SWSD3 为生产主控**，不讨论 SWSD2/SWSD4 切换
- 测试状态：评审时 SWSD 相关用例全绿（`tests/agent/swsd/` + `tests/agent/test_swsd_core.py` + `tests/agent/test_swsd4_intent_field.py` 共 27 项；`tests/agent/swsd/experience/` + `tests/tools/test_pcb_nl_fanout.py` + `tests/gateway/test_websocket_pcb_flow.py` 共 157 项）

---

## 0. 复审状态更新（2026-06-24 当日，针对团队后续修改）

首轮报告提交后，团队在 `state_manager.py` / `decision_policy.py` / `recorder.py` / `websocket.py` 做了一批针对性修复。复审确认如下（复跑 189 项相关用例全绿）：

| ID | 原优先级 | 复审状态 | 证据 |
|----|----------|----------|------|
| F1 状态写失败静默 | P0 | ✅ **已修** | `_swsd_update` 改 `logger.warning` + `_record_swsd_health_error` 记入 `_swsd_health`，且失败返回 `False`（`websocket.py:4442-4453`）；recorder 改 `warning` + `EXPERIENCE_RECORD_FAILURE_COUNT` 计数 |
| S1 迁移未校验 | P1 | ✅ **已修** | 新增 `_swsd_transition_allowed` + `_swsd_guard_bypass_event`，`_swsd_update` 写前用 `workflow.next_transition` 校验；`swsd_transition_guard_mode` 支持 `warn`(默认)/`strict` 灰度（`websocket.py:4344-4379`） |
| C1 共享状态跨线程无锁 | P1 | 🟡 **部分修** | `WorkflowStateManager` 加 `threading.RLock` 保护 `_memory`/`_memory_checkpoints` 且读返回 copy（`state_manager.py`）；adapter 新增 `_session_state_lock` 保护 `_swsd_health`。但 `_session_*` 十余个业务缓存仍未统一纳锁 |
| S2 三步写入非原子 | P2 | 🟡 部分缓解 | rollback 现同步内存态（`state_manager.rollback`）；`_swsd_update` 三步仍跨独立事务，未包单事务 |
| R1 前端 body 驱动状态无守卫 | P2 | ✅ **已修** | 新增 `_allows_inbound_body_state_recovery` + `_workflow_state_rank` 单调性守卫，recovery 仅允许从更早/相等状态推进（`websocket.py:4293-4306, 4534, 4554`） |
| M1 内存态无上限 | P3 | ✅ **已修** | `max_memory_checkpoints`(默认 20)裁剪 + 新增 `clear_session`，已在 reset 路径调用（`websocket.py:4249`） |
| P2 checkpoint 事实来源分叉 | P2 | ✅ **已修** | persist 模式 rollback 后同步刷新 `_memory`，内存退为 cache |
| L1 `ActionCandidate` 未 import | — | ✅ **已修** | `decision_policy.py` 顶部已 `from agent.swsd.action_candidates import ActionCandidate` |

**复审结论**：原 P0/P1 上线 gate（F1、S1、R1）已收口，C1 完成关键的状态管理器一半。剩余开放项均为 P2/P3 非阻塞项：**C1 的 adapter 业务缓存纳锁、S2 单事务、A1/L2 advisory 接通、G1/G2/G3 Grow 闭环**。建议补两类测试：① `strict` 模式下非法迁移被拒 + `warn` 模式下放行并计数；② DB 写失败注入验证 `_swsd_health` 落账且响应降级。

---

## 1. 总体结论

核心业务逻辑（意图判定、控制动作、字段契约）是可靠的。真正的工业级风险集中在两类问题上：

1. **“状态机”在运行期没有强制约束** —— `registry.py` 定义的迁移表在写入路径上完全不参与校验。
2. **持久化 / 并发 / 失败处理的边界没有按 7×24 多会话场景收口** —— 异常被静默吞掉、共享状态跨线程无锁、内存态无上限。（注：SQLite 连接模型本身已确认是工业级安全的，详见 P1。）

当前测试覆盖的是“逻辑正确性”，没有覆盖“运行期一致性”（并发、断线重连、DB 写失败、进程重启恢复）。上线前建议优先补这类场景测试。

设计上值得肯定的点（保持，不动）：

- 决策权 / 建议权分离落地扎实：`AgentAssistRequest.forbidden_actions = ("call_tool", "change_workflow_state", "invent_structured_fields")` 把契约编码进了数据结构。
- 事实与叙述分离：`SWSDResponseBuilder` 始终从 tool/runtime 字段派生结构化输出，LLM 文字只能作为 `narrativeText` 附加。
- 经验层“辅助而非覆盖”边界清晰：alias 仅在候选兼容时替换。
- reroute 失败链路（`runtime_bridge.handle_reroute_delete_result` 的 except 分支）是结构化失败处理的正面样板。

---

## 2. Issue 处理清单（按上线优先级排序）

| ID | 优先级 | 主题 | 位置 | 影响 | 修复成本 |
|----|--------|------|------|------|----------|
| F1 | P0 必须 | 状态写失败被静默吞掉，仅 debug 日志 | `gateway/platforms/websocket.py:4294` | 持久态静默落后；重启后从错误状态恢复且无感知 | 低 |
| P1 | P2 中（已降级） | 双 `SessionDB` 实例同写一库 | `websocket.py:410`、`run_agent.py:8986` | 连接模型本身安全；剩余风险仅为多实例 WAL 写竞争 | 低 |
| S1 | P1 高 | 状态机迁移在写入时未校验，registry 迁移表运行期不生效 | `websocket.py:4250` `_swsd_update` | 非法迁移无防线；graph 退化为注释 | 中 |
| C1 | P1 高 | 共享 `_session_*` / `_swsd_state._memory` 跨线程无锁 | `websocket.py:380-398`、`state_manager.py:17` | 并发读-改-写状态错乱，难复现 | 中 |
| S2 | P2 中 | `_swsd_update` 三步写入（state/event/checkpoint）非原子 | `websocket.py:4264-4293` | 部分失败导致审计缺失或回退点错位 | 中 |
| R1 | P2 中 | 前端 body 可单方面驱动状态机前进，无防回退守卫 | `websocket.py:4347` `_recover_experience_from_inbound_body` | stale body 把会话从 `import` 拽回 `review` | 低 |
| C2 | P2 中 | 单 worker drain-until-empty 存在 lost-wakeup 边界 | `websocket.py:806` | 理论上消息滞留队列直到下条消息到来 | 中 |
| P2 | P2 中 | 内存 checkpoint 与 DB checkpoint 语义/事实来源可能分叉 | `state_manager.py:125-160` | “回到上一步”行为在两种模式下不一致 | 中 |
| M1 | P3 低 | `_memory` / `_memory_checkpoints` 无上限、无淘汰 | `state_manager.py:17-18` | 长跑内存单调增长 | 低 |

> 逻辑层补充项（来自首轮 review，非本次工业级主轴，单独排期）：
> - L1 `decision_policy.py` 运行期实例化 `ActionCandidate` 但未 import（`from __future__ import annotations` 掩盖，tool_result 分支会 NameError）。
> - L2 `decide_workflow_action(experience_actions=...)` 生产路径未接通，目前为死链路。
> - L3 `intent.py` / `control_signals.py` / `intent_policy.py` 三处控制正则重复且不一致。
> - L4 `experience/model.py` 硬编码全局 alias `U27→U5`，建议挪到配置/fixture。

### 功能 / 流程完整性补充项（第二轮，回答“能否满足工业级”三连问）

| ID | 优先级 | 主题 | 位置 | 结论 |
|----|--------|------|------|------|
| G1 | P2 中 | Grow-With-You 的“蒸馏/复利”臂是断的 | `experience/distiller.py` | `should_distill_trace` / `summarize_trace_for_skill` 全仓库零引用（含测试），经验只进 event 不沉淀为可复用 skill |
| G2 | P2 中 | 项目模型不随项目/会话成长，是静态种子 | `experience/model.py:27` | `load_project_model` 忽略 `project_id` 永远返回 `DEFAULT_PROJECT_MODEL`，注释自承 v1；“Grow-With-You”当前只在 session 内闭环 |
| G3 | P3 低 | resolver 读取 `final_fields` 但无人写入 | `resolver.py:65` vs 全仓库 | recorder 只写 `body_fields`/`target_resolution`/`fanout_version`；`final_fields` 是预留 kind，当前永远命中不到 |
| A1 | P2 中 | “Agent 智能辅助”advisory 契约未接通 | `action_candidates.py` 全套 vs 全仓库 | `AgentAssist*`/`IntentCandidateSet`/`ActionCandidate` 仅 re-export，主路径走 `apply_swsd3_policy` 字典-candidate；详见 §8 |
| K1 | 设计正确（非缺陷） | 通用 skill 注册：发现是自动的，但需手动登记两处 | `skill_commands.py::scan_skill_commands`、`websocket_skill_admission.py` | 见 §7，未来开发路径清晰，但有两个易踩的隐式约束 |

---

## 6. 三连问的明确回答（功能 / 流程层面）

### 6.1 这份代码功能上、流程上能满足工业级稳定性要求吗？

**分两层看：**

- **正确性层面：可以。** 主链路（意图判定 → 控制动作 → 工具执行 → 结构化回包）逻辑自洽，失败有结构化兜底（reroute 链路是样板），断线重连有 outbound 缓冲，DB 写有 WAL + 锁 + jitter 重试。184 个用例全绿。
- **运行期韧性层面：上线前需先收口 §2 的 F1 / S1 / C1。** 这三条不是功能缺陷而是“看不见的失败”——状态写失败静默、非法迁移无防线、并发读-改-写无锁。它们在低并发演示环境不会暴露，但正是 7×24 多会话场景下最难定位的一类问题。**结论：功能完整可上线，但 F1/S1/C1 是“工业级”这三个字的门槛，建议作为上线 gate。**

### 6.2 之前提到的正则规则耦合（L3）是真问题吗？

是，但**是“演进负债”而非“当前 bug”**。`intent_policy.py` 的 `apply_swsd2_policy` / `classify_execution_intent` 叠了约 25 个交叉引用的正则，且 `intent.py`、`control_signals.py` 各自又维护了一份 `_CANCEL_RE`/`_CONFIRM_RE`/`_ROLLBACK_RE`，三处定义不完全一致（如 `intent.py` 的 cancel 正则没有拼音 token，`intent_policy.py` 的有）。

- **现状能跑对**：靠 `test_swsd_core.py` 的快照式断言兜底。
- **风险**：每加一个 case 容易碰坏另一个，回归风险随规则数线性上升；三处重复定义是“改一处忘两处”的经典坑。
- **建议**：(a) 控制信号正则统一收敛到 `control_signals.py` 单一来源（消除 L3 的重复）；(b) 中长期把规则层降级为 SWSD4 soft-intent 的 `candidate_prior` 兜底，不要继续在 swsd2/3 里堆正则。这与项目已有的 SWSD4 方向一致。

### 6.3 Hermes Grow-With-You 是否真的生效？

**部分生效——“读经验 / 用经验 / 记经验”三步闭环成立，但“蒸馏复利 / 跨会话成长”两步是断的。**

已确认生效的部分（有据）：
- **记**：`recorder.record_body_fields` 把前端结构化字段写成 `experience` event；`fanout_versions` 把每轮 fanout 落盘并记 `fanout_version` event。
- **读 + 用**：`resolver.resolve` 把 memory/model/skill 三类 hints 汇成 `PCBContextHints`，经 `run_agent.py:9004 build_experience_context_block` 注入 prompt；alias 解析（`websocket.py:1268,4896`）和默认 router（`_experience_default_router_type`）确实消费了经验。

**没生效 / 名不副实的部分（有据）：**
- **G1 蒸馏臂是死的**：`distiller.py` 的 `should_distill_trace` / `summarize_trace_for_skill` 在全仓库（含测试）**零引用**。即“把成功轨迹沉淀为可复用 skill”这一“成长”的核心动作从未被调用。当前经验只能以离散 event 形式被下一轮读取，不会压缩成稳定知识。
- **G2 项目模型不成长**：`load_project_model(project_id)` 忽略入参恒返回 `DEFAULT_PROJECT_MODEL`（注释已自承 v1 deterministic）。所谓“项目级偏好”当前是全局静态种子，不随项目历史演化。“Grow-With-You”目前实质是 **session 内的状态恢复**，而非跨会话/跨项目的能力积累。
- **G3 预留未接**：`resolver` 读取 `final_fields` kind，但没有任何 recorder 写入它。

**结论**：Grow-With-You 的“With-You”（会话内带着经验跑）成立且有用；“Grow”（跨会话越用越强）目前是骨架——接口齐全、闭环未接通。这不是 bug，是 roadmap 落差，但**文档（`temp_swsd_doc.md` §6.8）把蒸馏描述为已具备能力，与代码现状不符**，建议同步修正描述或排期接通 G1/G2。

---

## 7. 通用 Skill 注册（未来开发）评估 — K1

`docs/pcb_skill_tool_extension_guide.md` 已系统化描述了扩展路径，机制设计是**正确且克制的**（skill 积极扩、tool 保守扩、不为 skill 放宽 PCB 主流程 toolset 边界）。从代码侧确认两个未来开发者**容易踩的隐式约束**：

1. **新增 tool 必须手动登记 `model_tools.py::_discover_tools`**：`registry.register(...)` 写在工具模块里，但只有被 `_discover_tools` import 才会触发注册。漏掉这一步的表现是“工具写了却永远不可见”，且无报错——建议在 `_discover_tools` 加一条注释/断言提示，或改为按目录自动发现。
2. **PCB WebSocket auto-skill admission 有多重隐式过滤**（`websocket_skill_admission.py`）：一个新 skill 要被 PCB 主流程自动注入，必须同时满足 `websocket_pcb.enabled=true`、`category=hardware`(或路径含 hardware/pcb)、`mode=inject_only`、`intents` 命中当前 turn。任一不满足就静默跳过。建议在该函数加 debug 日志说明“为何某 skill 未被 admit”，否则未来开发者排查“我的 skill 为什么没加载”会很痛。

**结论**：通用 skill 注册路径对未来开发是通的、文档齐全，无阻塞性问题；K1 的两点是“可观测性 / 防呆”增强项，非缺陷。

---

## 8. “Agent 智能辅助”臂的现状 — A1

`action_candidates.py` 定义了一套完整且设计精良的建议契约：`AgentAssistRequest`（带 `forbidden_actions=("call_tool","change_workflow_state","invent_structured_fields")`）、`AgentAssistResult`、`IntentCandidateSet`、`ActionCandidate`。这是“LLM 出建议、SWSD 做决策”最核心的数据结构。

**但经全仓库检索确认（含 tests）：这四个类只在 `agent/swsd/__init__.py` 被 re-export，没有任何生产代码或测试实例化或消费它们。** `decide_workflow_action`（唯一会用 `ActionCandidate` 的函数）也仅被 `test_advisory_policy.py` 调用，生产路径走的是 `apply_swsd3_policy`。

**含义**：当前 SWSD3 主控里，“Agent 智能辅助”实际是通过 `_decide_route` 里那个 `candidate = {...}` 字典 + `apply_swsd3_policy` 实现的——LLM 给出 `route_intent`，policy 用规则校准。设计文档描述的“agent 给出带 confidence 的 candidate 集合、SWSD 用 `decide_workflow_action` 择一并保留 rejected 审计”这条更结构化的链路**目前是预留接口，未接通**。

**这不是 bug**（系统用更简单的字典-candidate 跑得通），但与 `action_candidates.py` 文件头注释和设计意图存在落差。建议：要么把 `decide_workflow_action` + `AgentAssist*` 接进主决策路径（让 advisory 契约真正生效、获得 rejected 审计能力），要么在文件头注明“v1 预留，主路径走 apply_swsd3_policy”。与 L2 是同一处落差的两个表现。

---

## 9. PCB 工具边界收口 — 确认有效（正面结论）

`gateway/run.py::_apply_turn_toolset_overrides`（`run.py:5085`）确认是**真实且严格**的强制收口：

- WebSocket + `route_mode=chat` → 直接返回 `[]`（无工具）。
- WebSocket + `route_mode=pcb` → 从 enabled 集合中剔除 `web/browser/terminal/file/code_execution/skills/session_search/clarify/delegation/cronjob/messaging/tts/image_gen/moa/homeassistant/rl/hermes-websocket`，强制只留 `hermes-websocket-pcb`。

这意味着即使某个 auto-skill 试图诱导模型调用通用执行类工具，toolset 层也会在 turn 级别物理剔除这些工具。**这是整套设计里防止“agent 被 skill 越权变成通用执行器”的关键防线，确认有效，符合扩展指南 §8 的承诺。** 未来新增 PCB skill 不会因此突破工具边界——这点对工业级安全是加分项。

---

## 10. 最终结论（综合两轮 + 功能完整性）

| 维度 | 结论 |
|------|------|
| 主链路正确性 | ✅ 自洽，184 用例全绿 |
| 失败兜底 | 🟡 reroute 链路是样板，但 `_swsd_update` 静默吞异常（F1）需先修 |
| 状态机约束 | 🔴 registry 迁移表运行期不 enforce（S1），需加守卫 |
| 并发安全 | 🟡 DB 层安全（WAL+锁+重试，P1 已确认）；adapter 层共享 dict 跨线程无锁（C1） |
| 持久化 | ✅ 连接模型工业级安全；🟡 三步写入非原子（S2）、双实例（P1）非阻塞 |
| 长稳/资源 | 🟡 内存态无上限（M1） |
| Grow-With-You | 🟡 记/读/用闭环成立；蒸馏复利/跨会话成长是骨架（G1/G2/G3） |
| Agent 智能辅助 | 🟡 advisory 契约设计精良但未接通（A1/L2），主路径走 apply_swsd3_policy |
| 通用 skill 注册 | ✅ 路径通、文档齐；建议加防呆日志（K1） |
| 工具边界收口 | ✅ 强制有效，安全加分 |

**一句话**：SWSD3 主控在功能与正确性上达到了可上线水平，工具边界与 DB 并发是真正的工业级设计；上线 gate 是 F1 / S1 / C1 三条运行期韧性收口。“Agent 智能辅助”与“Grow-With-You”的高级形态（结构化 advisory、跨会话蒸馏）目前是接口齐全、闭环未接通的骨架，属 roadmap 落差而非缺陷，但需同步修正过度承诺的设计文档（`temp_swsd_doc.md`、`action_candidates.py` 头注释）。

---

## 3. 详细问题描述与建议修复

### F1 — 状态写失败被静默吞掉（P0）

`_swsd_update` 末尾：

```python
except Exception:
    logger.debug("SWSD update failed: session=%s workflow=%s state=%s", ..., exc_info=True)
```

`PCBExperienceRecorder.record`（`recorder.py:32`）同样 `logger.debug(..., exc_info=True)`。

**风险**：若持久化 DB 持续写失败（磁盘满 / SQLite 锁 / schema 漂移），状态机继续在内存推进，持久态静默落后，生产默认日志级别看不到。进程重启后从陈旧/错误持久态恢复，用户无感知。

**建议**：
- 状态写失败：升级为 `logger.warning` + 失败计数指标；本轮响应降级为“可恢复错误”，不要假装成功。
- 经验记录失败：可保持 best-effort（容忍），但建议加计数指标用于观测。

### P1 — SQLite 连接模型（已确认，从 P0 降级为 P2）

**结论（已读 `hermes_state.py:156-255, 1035-1241` 确认）：SessionDB 的连接模型是工业级安全的，原 P0 风险不成立。**

确认的实现事实：

- `SessionDB.__init__`（`hermes_state.py:185`）显式 `check_same_thread=False` + `timeout=1.0` + `isolation_level=None`，并启用 `PRAGMA journal_mode=WAL` + `PRAGMA foreign_keys=ON`。跨线程复用单 connection 是被显式允许的，不会抛 `check_same_thread` 异常。
- 所有写经 `_execute_write`（`hermes_state.py:205`）：`with self._lock`（进程内 `threading.Lock` 串行化）+ `BEGIN IMMEDIATE`（启动即拿 WAL 写锁）+ 失败 `rollback`。`database is locked` 时释放 Python 锁、随机 20–150ms jitter、最多重试 15 次，专门规避 SQLite 内置确定性退避的 convoy 效应。
- 四个 workflow 写方法全部走 `_execute_write`：`upsert_workflow_state:1060`、`append_workflow_event:1119`、`write_workflow_checkpoint:1171`、`rollback_workflow_checkpoint`（其内部级联写也复用同一把锁的方法）。
- 读方法（`get_workflow_state:1063`、`list_workflow_events`、`list_workflow_checkpoints`、`rollback_workflow_checkpoint:1206`）同样 `with self._lock` 后再 `execute`，读写互斥。
- 因此 `to_thread` 工具线程经 adapter 的 `_swsd_db` 写库是**安全的**：单 connection + 进程内锁 + WAL，跨线程写被锁串行化。原报告基于调用拓扑推断的“跨线程直接抛异常 / database is locked”不成立。

**剩余的真实风险（降级后）**：仅在于 `websocket.py:410` 与 `run_agent.py:8986` 可能创建**两个独立的 `SessionDB` 实例**指向同一 `state.db`。两个实例各有独立的 `threading.Lock` 和独立 connection——进程内锁无法跨实例串行化，此时退化为纯 WAL 多写者竞争。WAL 单写者语义本身能保证正确性（不会损坏数据），由 `_execute_write` 的 jitter 重试兜底，但高并发下仍有写竞争与重试开销，且两实例的内存 `_write_count` / checkpoint 节奏各自独立。

**建议（降级为 P2，非上线阻塞）**：
- 让 `run_agent.py` 复用 adapter 注入的同一个 `SessionDB`（即保证 `self._session_db` 已设置，避免落到 `or SessionDB()` 新建分支），使进程内所有 SWSD 写共享同一把 `_lock`。
- 在部署文档注明：多 hermes 进程（gateway + CLI + worktree agents）共享一个 `state.db` 是被支持的并发写场景，由 WAL + 应用层 jitter 重试保障。

> 注：这同时部分缓解了 S2 —— `rollback_workflow_checkpoint` 内部的“写 state + 写 event”已经是各自的 `_execute_write` 事务，但**跨方法**仍非单一事务（见 S2）。`_swsd_update` 的三步写入横跨三个独立 `_execute_write`，原子性问题依然存在。

### S1 — 状态机迁移未校验（P1）

`registry.py` 定义了完整 `Transition` 表，`graph.py` 提供 `WorkflowDef.next_transition` 校验，但生产写入 `_swsd_update` 直接接收调用方给定的目标 `state` 并无条件覆盖 `current_state`。全链路无一处用迁移表校验合法性。`graph.validate()` 仅在导入时查“状态名是否存在”。`transition.py` 仅被 SWSD4 路径使用，SWSD3 主路径不走它。

**风险**：几十处硬编码 `_swsd_update(..., "review"/"import"/"rip_up", ...)` 写错没有防线；registry 迁移表运行期不生效。

**建议**：在 `_swsd_update` 增加迁移守卫——当 `intent` 非空且 `action_type == "normal"` 时，用 `WorkflowDef.next_transition(from_state, intent)` 校验 `from→to` 是否在表内：
- 灰度期：非法迁移打 `warning` + 指标，不阻断。
- 稳定期：非法迁移拒绝写入并降级响应。

### C1 — 共享状态跨线程无锁（P1）

`_session_*` 十余个裸 `dict` 与 `WorkflowStateManager._memory` 既被事件循环线程读写，也可能被 `to_thread` 工具线程经回调间接触及。GIL 保证单次 dict 操作不崩，但“读-改-写”序列（如 `_refresh_fanout_params_draft` 先读 draft 再回写、`setdefault` 后 `update`）跨线程无锁保护。

> 旁证：`fanout_versions.FanoutVersionStore` 已用 `threading.RLock` 保护磁盘历史，说明该风险已被识别，但 adapter 层 session 缓存未做同等保护。

**建议（二选一）**：
- 约定所有 `_session_*` 仅在事件循环线程读写；工具线程只通过返回值回传，由 loop 线程统一落库。
- 或对关键“读-改-写”缓存加锁。

### S2 — 三步写入非原子（P2）

`_swsd_update` 中 state / event / checkpoint 三步分别落库无事务包裹。部分失败导致：event 缺失（审计断裂）或 checkpoint 缺失（“回到上一步”回退过远）。

**建议**：将三步包进 SessionDB 单事务（`with db.transaction(): ...`）。

### R1 — 前端 body 可单方面前进状态（P2）

`_recover_experience_from_inbound_body`（`websocket.py:4347`）只要 inbound body 含 `fanoutParams` 就 `_set_flow_state(WAIT_CONFIRM)` 并把 SWSD 推到 `review`，未比较当前状态是否已更靠后。

**风险**：用户已在 `import` 态时，一条带 stale `fanoutParams` 的 body 把会话拽回 `review`。

**建议**：加状态单调性守卫——recovery 仅允许从更早或相等状态推进，禁止回退覆盖更晚状态。前端数据视为不可信输入。

### C2 — 单 worker lost-wakeup 边界（P2）

`_process_session_queue` 在 `queue.empty()` 时 `return`（L806）；`_enqueue` 先 `put` 再判断 `worker.done()` 决定重启。理论上存在“入队后 worker 刚退出且未重启”的窗口，消息滞留至下条消息到来。

**建议**：worker 用 `while True: item = await queue.get()` 永不因 empty 退出；或退出与重启用同一标志位保护。补“连续两条消息快速到达 + worker 正在退出”的并发测试。

### P2 — checkpoint 事实来源分叉（P2）

`WorkflowStateManager` persist=True 走 `db.rollback_workflow_checkpoint`，persist=False 走 `_memory_checkpoints[-2]`；`_memory` 与 DB 并行维护，仅 `load` 时优先读 DB，可能分叉。

**建议**：明确单一事实来源——persist 模式下内存仅作 cache，checkpoint 一律以 DB 为准。

### M1 — 内存态无上限（P3）

`_memory` / `_memory_checkpoints` 按 `(session_id, workflow_id)` 累积，checkpoint `list.append` 永不裁剪。session 级 `_session_*` 在 reset/disconnect 有 `pop` 清理（`websocket.py:4204-4219`），但 `_swsd_state` 内存态无对应清理。

**建议**：reset / 会话结束时一并清理 `_swsd_state` 内存条目；或给 checkpoint list 设上限（保留最近 N 个）。

---

## 4. 建议执行顺序

1. **F1**（状态写失败静默）—— 改日志级别 + 失败降级。改动最小、收益最大。
2. **S1**（迁移未校验）—— 给 `_swsd_update` 加 transition 守卫，灰度期 warning。
3. **C1**（共享字典跨线程）—— 明确线程访问约定。
4. **R1 / S2 / M1**（守卫、原子性、内存上限）—— 稳定期逐步收口。
5. **P1**（双 SessionDB 实例）—— 让 `run_agent.py` 复用 adapter 的 `SessionDB`，非阻塞。
6. 逻辑层 L1–L4 单独排期（L1 优先，存在潜在 NameError）。

> P1 经确认连接模型安全后已从 P0 降级；原列首位的 P0 数据库风险不再成立。

---

## 5. 上线前测试缺口

现有测试覆盖逻辑正确性，建议补充以下运行期一致性场景：

- DB 写失败注入（mock SessionDB 抛异常）→ 验证响应降级而非静默成功。
- 并发多消息同会话 → 验证状态无错乱、worker 无 lost-wakeup。
- 断线重连 + stale body recovery → 验证状态单调性守卫（R1 修复后）。
- 进程重启恢复 → 验证从持久态恢复的 current_state 与 checkpoint 一致。
- 非法迁移注入 → 验证 S1 守卫生效。
