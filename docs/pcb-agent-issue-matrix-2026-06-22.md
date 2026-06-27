# PCB Agent 问题对照与修复建议（2026-06-22）

这份文档是研发内部诊断表，不是对外汇报稿。  
本次更新基于 2026-06-23 最新代码、单测和 lab 结果。

判定原则：

- `已解决`：不只有代码分支，还要有测试或 lab 证明行为真的成立。
- `部分解决`：主流程已修通，但自然语言、解释层、工具层或真实交互层仍有明显缺口。
- `未解决`：问题本体还在，当前只是绕过、包装报错或没有覆盖到。
- `表面通过但结论不可靠`：测试或 lab 标记为 passed，但 transcript 显示用户真正关心的语义没有生效。

另外，这里默认把“流程层修好”和“算法/工具本体修好”分开判断，不混成一个结论。

当前最新验证基线：

- `tests/gateway/test_websocket_pcb_flow.py` -> `153 passed`
- `tests/agent/swsd/test_websocket_skill_admission.py` -> `3 passed`
- `workflow_controller_mixed_report.jsonl` -> `3/3 passed`
- `fanout_state_flex_report.jsonl` -> `4/4 passed`
- `fanout_state_flex_injected_report.jsonl` -> `4/4 passed`
- `reroute_mock_flow_report.jsonl` -> `3/3 passed`
- `reroute_multi_round_report.jsonl` -> `3/3 passed`

| 问题来源 | 问题描述 | 当前状态 | 证据 | 真实结论 | 下一步修复建议 |
|---|---|---|---|---|---|
| docx 原问题 | 拒绝布线是否成功 | 部分解决 | 代码：`agent/swsd/workflow_controller.py` 已统一走 `reject_route / reject_import`。<br>测试：`tests/gateway/test_websocket_pcb_flow.py` 已覆盖 review/import 拒绝以及 `jujue` 拼音拒绝。<br>lab：本轮 5 组实验没有专门跑“拒绝布线”真实对话链路。 | 流程层已经接进 SWSD3，控制词也收口了，但还缺一条真实前端场景来证明“拒绝后继续修改/恢复/再执行”整链稳定。 | 新增真实场景回归用例：`拒绝布线`、`拒绝导入`、`jvjue`、`不要布线`，并要求拒绝后状态仍可继续操作。 |
| docx 原问题 | 逃逸布线配置识别速度可以，但配置能力不够 | 部分解决 | 代码：`gateway/platforms/websocket.py` 已有统一 router descriptor、draft mutator、`GA / Auto` 提示同步。<br>测试：`tests/gateway/test_websocket_pcb_flow.py` 已覆盖 `GA / Auto` 提示、review/injected 修改。<br>lab：`fanout_state_flex_report.jsonl`、`fanout_state_flex_injected_report.jsonl` 均全过。 | 配置入口和状态闭环比旧版明显完整，但“支持的自然语言编辑范围”还在持续扩展中，当前已够稳定，不等于产品面完全封顶。 | 继续补真实场景回归：组合修改 `routerType + 线宽 + 线距 + orderLines`，并为复杂改参补说明文案。 |
| docx 原问题 | 无法识别线宽需求、无法修改线宽 | 已解决 | 代码：`tools/pcb_nl_fanout.py` 已支持 `改成/改为/调成/调到` 形式的线宽线距解析；`gateway/platforms/websocket.py` 与 `agent/swsd/workflow_controller.py` 已把约束 patch 接入统一 draft mutator。<br>测试：`tests/gateway/test_websocket_pcb_flow.py` 已覆盖 review 和 injected 场景的 `线宽改成 5`。<br>lab：`fanout_review_modify_params`、`fanout_injected_modify_params` 已通过。 | 这条在当前 workflow 主链上已经修通，不再是“首次能识别、二次改不了”的状态。 | 下一步从“能改”转向“解释更好”：在回包里更明确说明哪些约束被改了。 |
| docx 原问题 | 能识别 `buxian`，但识别不了 `queren / jvjue / jixu` 等拼音控制词 | 部分解决 | 代码：新增 `agent/swsd/control_signals.py`，并接入 `intent.py`、`intent_policy.py`、`workflow_controller.py`、`gateway/platforms/websocket.py`。<br>测试：`tests/gateway/test_websocket_pcb_flow.py` 已覆盖 `queren`、`jujue`。<br>lab：当前 5 组实验仍以中文路径为主，没有单独跑拼音控制词。 | 拼音控制词在代码层和单测层已打通，但真实前端链路还缺专门 lab 证明，所以不宜直接判成完全关闭。 | 补一组真实场景/lab：`queren / jixu / jvjue / jujue / buyao / rollback`，验证不会掉出 PCB 流程。 |
| docx 原问题 | 数字菜单行为异常 | 部分解决 | 代码：`gateway/platforms/websocket.py` 已按当前 workflow state 解释数字菜单，算法阶段和模块阶段分离。<br>测试：现有 gateway 测试已覆盖 router prompt 和 `GA / Auto` 提示，但没有单独打数字菜单场景。 | 设计和代码都已经收口到正确方向，但缺少专门的数字菜单回归证据。 | 新增 gateway + lab 场景：算法阶段输入 `1/2/3`、模块阶段输入 `1/2/3/4`，并断言只在当前菜单上下文生效。 |
| docx 原问题 | `release.v5` 历史版本回退后，拆线重布补不上、DRC 失败；回退后解释不清参数变化 | 部分解决 | 代码：`agent/swsd/workflow_controller.py` 已把恢复纳入 `restore_params_version / restore_layout_checkpoint`，并接入 `agent/swsd/restore_renderer.py` 生成稳定说明。<br>测试：`tests/gateway/test_websocket_pcb_flow.py` 已覆盖恢复参数版、恢复版图版以及解释文本。<br>lab：5 组实验主链已过，但没有一组专门验证“恢复后再 reroute + 解释 changedFields”的完整真实对话。 | 流程层和解释层都比旧版更完整了，但“恢复后 reroute 成功”仍受外部工具/模型质量影响，不能把工具本体问题一起算成已修好。 | 补真实场景回归：恢复参数版后再 reroute、恢复版图版后再导入，并让 transcript 断言版本说明与字段变化摘要。 |
| docx 原问题 | `arc` 布线器错误 | 未解决 | 代码：当前更多是错误包装、前置检查和 graceful fallback；没有修改 `arc` 本体。<br>lab：现有 5 组实验通过，证明流程层能兜住，不证明 `arc` 工具本体变稳。<br>现象：`arc` 仍可能因输入/约束/输出文件问题失败。 | 这条仍是工具本体问题。当前修好的是“失败时别把流程打烂”，不是“arc 自己不再错”。 | 补 router/tool fallback 和工具级回归，单独把 `constrain.txt`、空输出、转换失败等问题做最小复现验证。 |
| 本轮补充隐患 | `fanout_state_flex_injected` 在 lab 通过，但 `换成 U55 / 改成 arc` 并未真正改动注入参数 | 已解决 | 代码：`gateway/platforms/websocket.py` 已取消 injected fanout 的过早短路回显，并接入统一 draft mutator。<br>测试：`tests/gateway/test_websocket_pcb_flow.py` 新增 injected `换成 U55 / 改成 arc / 线宽改成 5` 断言。<br>lab：`fanout_state_flex_injected_report.jsonl` 已 4/4 passed。 | 这条之前确实是“表面通过”，本轮已经真修好，不再只是回显旧状态。 | 下一步保持回归保护，避免后续再把 injected 路径分叉回 adapter 旁路。 |
| 本轮补充隐患 | reroute 多轮重入在主流程层通过，但 transcript 仍可能直接掉到外部模型 `401` | 部分解决 | 代码：`gateway/platforms/websocket.py` 已新增 reroute terminal normalizer，把 `401 / modelGenerationFailure / finalize 类失败` 归一成结构化 final。<br>测试：`tests/gateway/test_websocket_pcb_flow.py` 已新增 auth failure 归一化断言。<br>lab：`reroute_mock_flow_report.jsonl`、`reroute_multi_round_report.jsonl` 本轮已全过。 | 用户可见输出层已经补稳，不会轻易掉成裸错误串；但 `401` 的根因是外部能力/鉴权问题，本体仍然存在。 | 将这类失败继续纳入 degraded final 契约，并补一条真实外部失败 transcript 回归，确保前端长期稳定消费。 |
| 本轮补充隐患 | 版本恢复已进入 SWSD3 正式 action，但恢复后对参数变化的自然语言解释没有真正打透 | 部分解决 | 代码：新增 `agent/swsd/restore_renderer.py`，`workflow_controller.py` 已用结构化字段生成解释。<br>测试：恢复参数版、恢复版图版测试已要求包含版本和状态摘要。<br>lab：当前没有专门的恢复解释 transcript 回归。 | 结构化字段和基础说明已经补上，但“真实对话里是否足够清楚、足够像人话”还需要真实场景继续磨。 | 增加真实场景回归，并根据 transcript 再迭代说明模板。 |
| 本轮补充隐患 | fanout 线宽/线距在首次参数生成可解析，但 review 阶段口头二次修改未形成完整闭环 | 已解决 | 代码：review 阶段已通过统一 draft mutator 消费约束 patch。<br>测试：`test_route_decision_supports_escape_constraint_modify_from_review_state` 已通过。<br>lab：`fanout_review_modify_params` 已通过。 | 这条在当前支持的自然语言约束修改范围内已经闭环。 | 下一步补更复杂的多字段联改场景和解释文本。 |
| 本轮补充隐患 | 拼音控制词缺口仍会影响真实前端可控性，不能因为中文路径通了就判已解决 | 部分解决 | 代码：本轮已新增统一控制词归一化层。<br>测试：已有拼音确认/拒绝断言。<br>lab：仍没有拼音专门实验。 | 这条已经不再是“完全没做”，但也还没到“真实前端稳了就能结案”的程度。 | 直接补拼音版 lab 集，不再只靠 gateway 单测。 |

## 收口判断

这轮之后，之前最典型的两个“看起来通过、其实没真修好”的问题已经明显分化：

1. `fanout_state_flex_injected` 的注入状态修改  
   - 已经从“表面通过”转成“代码、单测、lab 三线闭环”。
2. reroute 多轮重入后的外部失败回退  
   - 输出层已经补稳，但外部模型/鉴权本体问题依然存在，所以只能算“部分解决”。

当前下一轮最值得继续盯的，不是再补更多分支，而是：

- 数字菜单真实场景回归
- 拼音控制词真实场景回归
- restore 解释的人话程度
- `arc` 与外部 reroute 能力的工具本体稳定性
