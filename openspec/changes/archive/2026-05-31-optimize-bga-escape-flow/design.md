## Context

BGA 逃逸主链路由 `gateway/platforms/websocket.py` 的 adapter-controlled 状态机编排，前端通过 `getProjectData` 返回版图后，系统调用 `pcb_extract_bga` 生成 `selection`、`boardSummary`、`fanoutContext`，再进入目标选择、布线器选择、fanoutParams 确认和 `route_bga` 执行。

当前存在三个不一致点：

- 单 BGA 分析结果会被 adapter 自动写入 `_session_selected_targets`，直接进入布线器选择；多 BGA 才要求用户从 `selection` 列表选择。
- `wait_confirm` 阶段只把“确认/继续/执行”等自然语言识别为执行布线，前端回传的 fanoutParams JSON 不会更新 `_session_fanout_params`，也不会触发布线。
- BJUT arc 路径当前动态写 `constrain.txt`，但 arc router 包本身已经提供 `constrain.txt`；135 路径不需要该文件，应避免生成或沿用残留文件。

## Goals / Non-Goals

**Goals:**

- 统一单 BGA 和多 BGA 目标选择体验，让用户始终先确认 `selectedBGA`。
- 允许前端在参数确认页回传完整或部分 fanoutParams JSON，并以 `orderLines` 作为“同意本配置”的信号。
- 执行布线前使用用户回传参数刷新缓存，确保 `route_bga` 收到最终配置。
- 明确 arc/135 对 `constrain.txt` 的差异：arc 从 router profile 复制，135 不使用。
- 保持 `##PCB_FIELDS##` 协议和现有 `fanoutParams` 字段结构兼容。

**Non-Goals:**

- 不改变 BGA 识别算法、pin 数阈值或 `selection` 字段结构。
- 不新增 routerType 枚举。
- 不改变 `route_bga` 的外部 JSON schema。
- 不手工修改 router 输出文件或 native router 可执行文件。

## Decisions

**Decision 1: 单 BGA 也进入 `wait_selection`**

`_run_direct_bga_analysis` 只要拿到非空 `selection`，就缓存 labels 并进入 `_FLOW_WAIT_SELECTION`，不再因 `len(labels) == 1` 自动写入 `_session_selected_targets`。用户回复合法 label 后沿用现有 `_extract_selected_label` 进入 `_FLOW_WAIT_ROUTER_TYPE`。

理由：前端交互和用户心智统一，避免用户还没确认目标就开始选择布线器。替代方案是只在前端展示单 BGA 但后端仍自动选中，这会继续保留隐式状态，不利于后续参数回传校验。

**Decision 2: `wait_confirm` 识别 fanoutParams JSON 为确认**

在 `_decide_route` 的 `_FLOW_WAIT_CONFIRM` 分支中，先尝试从用户文本解析 JSON 并通过 `_collect_pcb_fields` / `_coerce_fanout_params` 提取 fanoutParams。当提取结果包含非空 `orderLines` 时，视为确认执行，不要求用户再发送“确认”文字。

理由：前端确认页回传的是结构化参数，而不是自然语言确认。`orderLines` 是执行布线所需的核心字段，可作为明确确认信号。替代方案是要求前端额外追加“确认”文字，但这会让协议冗余且容易与 JSON 解析耦合。

**Decision 3: 用户回传参数必须先归一化再缓存**

新增或复用 helper 将用户回传 fanoutParams 和当前 session 状态合并：

- `selectedBGA` 优先使用用户回传值，但必须在已识别 labels 中；缺失时使用当前 `_session_selected_targets`。
- `routerType` 优先使用用户回传值并归一化，缺失时使用当前 `_session_router_types`。
- `orderLines` 使用用户回传列表，并经过现有 `_normalize_order_lines` 排序、编号和字段过滤。
- `constraints` 使用现有 `_normalize_constraints`，缺失时沿用缓存或 fanoutContext 默认值。

合并后的参数写回 `_session_fanout_params`、`_session_selected_targets`、`_session_router_types`，然后走现有 `_run_cached_fanout_route`。

理由：保持 route 执行入口不变，同时防止前端参数覆盖成非法 BGA 或非法 routerType。替代方案是把用户 JSON 原样传给 `route_bga`，但会绕过 adapter 层已有防线。

**Decision 4: arc 复制 profile 里的 `constrain.txt`，135 不创建该文件**

在 `tools/pcb_bjut_router.py` 中将 arc family 的约束文件处理改为从 `resolve_router_dir(router_type)` 返回目录复制 `constrain.txt` 到 `work_dir/constrain.txt`。若缺失则报出明确错误。135 family 不调用该逻辑，`135_main` 参数仍保持 `[layout_name, order_input.txt]`。

`tools/pcb_tools.py` 的 legacy arc fallback 也应使用同样的复制策略；135 fallback 不写 `constrain.txt`。`_ROUTER_STALE_FILES` 应包含 `constrain.txt`，避免不同 routerType 连续运行时读取旧文件。

理由：arc README 声明 `constrain.txt` 是逃逸前已提供文件，且 router 包已经携带该文件；135 router 目录没有该文件，生成它只会造成误解。替代方案是继续根据 UI constraints 动态写 arc constrain，但会偏离 router profile 的固定约束来源。

## Risks / Trade-offs

- [前端流程步数增加] 单 BGA 多一次选择确认 -> 通过保持 `selection` 字段和提示语一致降低适配成本。
- [用户 JSON 解析失败] 非 JSON 或缺少 `orderLines` 的确认仍按原有提示处理 -> 保留“请回复确认或取消”的兜底。
- [用户回传非法参数] selectedBGA/routerType/orderLines 可能不合法 -> 使用现有归一化和 label/routerType 校验，失败时给出纠偏提示，不执行布线。
- [arc constrain 文件缺失] 部署包若漏带 `routers/arc_windows_0519/constrain.txt` 会导致 arc 失败 -> 错误信息直接指向缺失文件；Windows 打包清单需包含该文件。
- [残留工作目录] 上一次 arc 生成的 `constrain.txt` 可能影响后续 135 调试判断 -> 将其纳入 stale cleanup。

## Migration Plan

实现后无需数据迁移。部署时确认 Windows delivery 的 arc router 目录包含 `constrain.txt`；如需回滚，可恢复旧状态机自动选择单 BGA，并恢复动态写 arc 约束文件逻辑。

## Open Questions

None.
