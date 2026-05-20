# Windows 联调说明 — fanout router 分支

**分支名：** `feature/fanout-router-integration`  
**负责人：** 晨媛（Mac 开发，无 Windows，需 Windows 同事协助验收）  
**更新时间：** 2026-05-20

---

## 改了什么

1. **新模块** `tools/pcb_bjut_router.py`  
   - 北科大 0518 三步流水线：`layer_assign_cpp` → `escape_order_cpp` → `135_main`/`arc_main`  
   - `generate_fanout_params()`：层分配 + 逃逸顺序 → `fanoutParams`  
   - `run_bjut_route()`：执行布线，输出 `routingResult` / `importLinesFilePath` / `report`

2. **`tools/pcb_tools.py`**  
   - `route_bga` 优先走 BJUT adapter；不可用时回退旧 arc/135 `.out` 链路

3. **`gateway/platforms/websocket.py`**  
   - 用户选完 `arc`/`135`/`rl`/`rl_arc` 后，优先调用北科大工具生成 `fanoutParams`  
   - 失败时回退 LLM / 规则生成

4. **`config.ini`**  
   - 示例路径（Windows 上请改为本机绝对路径）

5. **测试** `tests/tools/test_pcb_bjut_router.py`（不依赖 exe，Mac 可跑）

---

## Windows 环境准备

### 1. 拉分支

```powershell
cd F:\path\to\hermes-agent-PCB
git fetch origin
git checkout feature/fanout-router-integration
```

### 2. 目录布局（建议）

```text
F:\doctor\hermes-agent\
├── hermes-agent-PCB\          ← 源码
├── 0518\                      ← 北科大 0518（或从 Desktop 拷贝）
│   ├── 135度走线\
│   └── 弧形走线\
└── bk_routing\                ← 嘉栋 RL（可选，测 rl 时用）
    ├── 135_windows_0519\
    └── arc_windows_0519\
```

### 3. 修改 `config.ini`

```ini
[router]
work_dir = F:\doctor\hermes-agent\router_work
arc_dir  = F:\doctor\hermes-agent\0518\弧形走线
135_dir  = F:\doctor\hermes-agent\0518\135度走线
rl_root_dir = F:\doctor\hermes-agent\bk_routing
rl_arc_dir  = F:\doctor\hermes-agent\bk_routing\arc_windows_0519
rl_135_dir  = F:\doctor\hermes-agent\bk_routing\135_windows_0519
```

### 4. 启动 Agent

```powershell
cd F:\path\to\hermes-agent-PCB
.\.venv311\Scripts\python.exe delivery_gateway_main.py
```

或 delivery 包：`.\start.bat`

前端 WebSocket 端口与 `config.ini` 中 `[server] port` 一致。

---

## 验收测试（请逐项勾选）

### T1 — arc 链路

1. 前端连接 WebSocket  
2. 发送：`开始 PCB BGA 逃逸布线，获取当前版图数据并返回可选 BGA 列表。`  
3. 应收到 `getProjectData` tool-calls → 回传版图  
4. 应收到 `selection`  
5. 回复：`选择 U22`（或实际 BGA 位号）  
6. 回复：`arc`  
7. 应收到 `fanoutParams`（**优先来自北科大 layer_assign + escape_order**，非纯 LLM 瞎写）  
8. 回复：`确认`  
9. 应收到 `routingResult`（文件路径）、`importLinesFilePath`、`report`  
10. 确认 `routingResult` 指向的文件存在且可导入  

### T2 — 135 链路

同上，第 6 步改为：`135`

### T3 — rl（可选，需 bk_routing）

第 6 步改为：`rl` 或 `rl_135`，`config.ini` 指向 `135_windows_0519`

---

## 验收标准（分工文档）

- [ ] 用户选择布线器正确  
- [ ] `fanoutParams` 正确发前端  
- [ ] 布线器真实执行，输出文件存在  
- [ ] `routingResult` / `report` / `importLinesFilePath` 完整  
- [ ] arc / 135 / 新布线器至少各一条链路通过  

---

## 常见问题

| 现象 | 排查 |
|------|------|
| fanoutParams 仍是 LLM 生成的 | 检查 `arc_dir`/`135_dir` 下是否有 `layer_assign_cpp.exe`、`escape_order_cpp.exe` |
| route 报 Windows exe 错误 | 确认在 Windows 本机运行，且路径无中文乱码 |
| 有 routingResult 但版图无变化 | 前端是否调用 `importLines` 导入 `importLinesFilePath` |
| 135 adapter 不可用但想做 reroute | 那是珂珂的 reroute 链路，与 BGA fanout 无关 |

---

## Mac 开发说明

晨媛侧无 Windows，已在 Mac 完成 adapter 代码与解析单元测试。**真实 exe 联调依赖本 Windows 文档。**

联调结果请反馈：通过 / 失败 + 日志或 `logs/pcb_websocket_trace.jsonl` 片段。
