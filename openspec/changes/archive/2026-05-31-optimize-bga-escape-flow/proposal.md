## Why

当前 BGA 逃逸流程在单 BGA 场景会自动选择目标器件，和多 BGA 场景的用户确认路径不一致；同时参数确认阶段只接受“确认”等文字，无法使用前端回传的已编辑 fanoutParams 作为最终配置。135 与 arc 布线器对 `constrain.txt` 的依赖也需要收敛为明确协议，避免 135 模式误依赖约束文件、arc 模式使用错误来源。

## What Changes

- 单 BGA 和多 BGA 使用一致的目标选择交互：识别到一个 BGA 时也先返回 `selection` 列表，用户选择目标后再选择布线器类型。
- `wait_confirm` 阶段支持用户回传 fanout 参数 JSON：只要内容包含 `orderLines`，即视为同意本配置，并用回传参数更新缓存后执行布线。
- 回传参数中的 `selectedBGA`、`routerType`、`orderLines`、`constraints` 经现有校验/归一化后进入 `route_bga`，避免沿用旧缓存。
- 135 执行族不需要、不生成、不传递 `constrain.txt`。
- arc 执行族需要 `constrain.txt`，文件来自 arc 布线器目录同级的 `constrain.txt`，并复制到 router work 目录供 `arc_main` 使用。
- 更新 WebSocket BGA 流程测试和 router adapter 测试，覆盖单 BGA 选择、参数 JSON 确认、arc/135 约束文件行为。

## Capabilities

### New Capabilities
- `bga-escape-workflow-confirmation`: BGA 逃逸 WebSocket 流程中的目标选择、布线器选择和参数确认协议。
- `bga-router-constraint-files`: BGA 逃逸布线器对 `constrain.txt` 的生成、复制和使用规则。

### Modified Capabilities
- None.

## Impact

- `gateway/platforms/websocket.py`: BGA 状态机、单 BGA selection 分支、`wait_confirm` 参数 JSON 解析与缓存更新。
- `tools/pcb_bjut_router.py`: arc/135 执行族的 `constrain.txt` 处理。
- `tools/pcb_tools.py`: fallback arc/135 adapter 的约束文件处理和 router work 清理。
- `tests/gateway/test_websocket_pcb_flow.py`、router 相关测试：新增和更新行为覆盖。
- WebSocket 前端协议保持 `##PCB_FIELDS##` JSON 结构不变，但单 BGA 场景的用户交互步骤会增加一次目标选择确认。
