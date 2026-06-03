## 1. WebSocket BGA Flow

- [x] 1.1 修改 `gateway/platforms/websocket.py` 的 `_run_direct_bga_analysis`，使单 BGA 和多 BGA 都返回 `selection` 并进入 `_FLOW_WAIT_SELECTION`
- [x] 1.2 调整目标选择阶段提示文案，确保单 BGA 场景回复“确认”时提示先选择目标 BGA
- [x] 1.3 在 `wait_confirm` 判定中识别用户回传的 fanoutParams JSON；当解析结果包含非空 `orderLines` 时返回 `confirm_route`
- [x] 1.4 新增或复用 helper 将用户回传 fanoutParams 与 session 状态合并，更新 `_session_fanout_params`、`_session_selected_targets` 和 `_session_router_types`
- [x] 1.5 对用户回传参数执行 selectedBGA、routerType、orderLines、constraints 校验和归一化，非法时返回纠偏提示且不执行布线

## 2. Router Constraint Files

- [x] 2.1 在 `tools/pcb_bjut_router.py` 中将 arc family 的 `constrain.txt` 处理改为从 resolved router directory 复制到 work directory
- [x] 2.2 确保 `tools/pcb_bjut_router.py` 的 135 family 不创建、不传递、不依赖 `constrain.txt`
- [x] 2.3 在 `tools/pcb_tools.py` legacy arc fallback 中采用同样的 profile `constrain.txt` 复制策略
- [x] 2.4 确保 `tools/pcb_tools.py` legacy 135 fallback 不写入 `constrain.txt`
- [x] 2.5 将 `constrain.txt` 加入 router work stale file 清理列表，避免 arc/135 连续运行残留
- [x] 2.6 arc profile 缺少 `constrain.txt` 时返回明确错误，错误信息包含缺失文件路径或 router directory

## 3. Tests

- [x] 3.1 更新 `tests/gateway/test_websocket_pcb_flow.py`：单 BGA 首轮返回 `selection`，用户选择后才提示布线器类型
- [x] 3.2 新增 WebSocket 测试：`wait_confirm` 阶段收到包含 `orderLines` 的 JSON 时更新参数并调用 `route_bga`
- [x] 3.3 新增 WebSocket 测试：确认阶段 JSON 缺少 `orderLines` 或 selectedBGA 非法时不执行布线
- [x] 3.4 新增 router adapter 测试：135 family 不创建/使用 `constrain.txt`
- [x] 3.5 新增 router adapter 测试：arc family 从 router directory 复制 `constrain.txt`，且用户 constraints 不重写文件内容
- [x] 3.6 新增 router adapter 测试：arc family 缺少 `constrain.txt` 时失败信息清晰

## 4. Validation

- [x] 4.1 运行 `python -m pytest tests/gateway/test_websocket_pcb_flow.py -v`
- [x] 4.2 运行 router 相关 pytest 测试文件，覆盖 `tools/pcb_bjut_router.py` 和 `tools/pcb_tools.py`
- [x] 4.3 在 Windows 当前仓库环境下运行限定 PCB 回归：`uv run --native-tls --extra dev --extra messaging python -m pytest tests/gateway/test_websocket_pcb_flow.py -q` 和 `uv run --native-tls --extra dev --extra messaging python -m pytest tests/tools/test_pcb_bjut_router.py tests/tools/test_pcb_tools_mode_guard.py tests/tools/test_pcb_router_adapters_e2e.py -q`
- [x] 4.4 检查 Windows delivery/package 配置，确认 `routers/arc_windows_0519/constrain.txt` 会随 arc router 目录一起交付
