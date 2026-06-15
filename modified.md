# 修改说明

更新时间：2026-06-15 11:58 CST  
分支：`codex/drc-agent-content-report`  
同步来源：天翼云测试容器 `/work/home/hermes-agent-PCB-report`  
本地目标：`/Users/keke/coding/PCB-Agent/hermes-agent-PCB-report`  
目标远端：`origin git@github.com:Jasper-Liang12138/hermes-agent-PCB.git`

## 版本状态

### 天翼云测试容器

- 已完成 GA 接入、自然语言 fanout/布线入口、auto 路由策略、DRC 报告坐标改造的实现与初步测试。
- 测试容器架构为 `aarch64`。
- 上传并展开了新路由包 `MLM-PCB-Router-bk_routing.zip`，形成：
  - `routers/135_linux`
  - `routers/arc_linux`
- 云端保留了旧 Windows 路由目录作为历史兼容参考。
- 云端发现新 `.out` 路由二进制为 `x86_64` ELF，而容器是 `aarch64`，因此真实二进制执行会被架构检查拦截，避免出现 `Exec format error`。

### 本地 DRC 分支

- 已从天翼云同步 GA/auto/自然语言 fanout 实现到本地分支 `codex/drc-agent-content-report`。
- 已同步新增 Linux 路由目录：
  - `routers/135_linux`
  - `routers/arc_linux`
- `.DS_Store` 是本地已有工作区修改，未纳入本次提交。
- DRC vendor 更新和 DRC 坐标/报告改造保留在同一分支中。

## 自然语言布线

### 修改前

- 用户需要显式选择或暗示具体布线算法，例如 `135 + 北科大`。
- 对话层会等待用户明确选择 `arc`、`135`、`RL` 等 routerType。
- 自然语言中的网络层分配和布线顺序无法稳定传入 fanout 参数生成流程。

### 修改后

- 用户可以只说“给这个 BGA 布线”“把这些 net 走 SIG03/SIG04 后布线”等自然语言需求。
- Agent 会把用户原始自然语言作为 `userText` 传给 `generateFanoutParams`。
- 自然语言解析会提取：
  - 指定 BGA，例如 `U22`
  - 指定网络，例如 `NET_A`
  - 指定层，例如 `SIG03`、`ART03`、`Top`、`Bottom`
  - 线宽/线距约束
  - 显式顺序
- 若自然语言给出了层分配，生成的 `naturalLanguageOrderLines` 会覆盖或合并到 `orderLines`，随后 route 阶段按该顺序布线。
- 用户界面不需要暴露 GA/RL/北科大选择；内部自动选择候选方案。

## Fanout 兜底与自动选择

新增内部 routerType：

- `ga`
- `ga_135`
- `ga_arc`
- `auto`
- `auto_135`
- `auto_arc`

自动候选顺序：

- `auto_135`: `ga_135 -> rl_135 -> 135`
- `auto_arc`: `ga_arc -> rl_arc -> arc`
- `auto`: 按当前推断的 135/arc 家族选择对应 auto 队列

默认选择规则：

- 自然语言中出现 `SIGxx`、`ARTxx`、`Top`、`Bottom`、`顶层`、`底层` 时，默认内部使用 `auto_arc`。
- 其他普通 fanout/布线请求默认内部使用 `auto_135`。

执行合同：

- `generateFanoutParams` 阶段负责层分配和逃逸顺序搜索。
- `route` 阶段不再重复跑 layer assign / escape order，而是消费已经确认的 `fanoutParams.orderLines` 并执行主布线。
- `route_bga` 会拒绝 `routerType=auto*`，要求先生成 fanoutParams，拿到自动选择后的实际 routerType 后再 route。

## GA 接入

新增 GA 作为和 RL 同级的层分配/逃逸顺序搜索模块：

- 135 家族 GA：`routers/135_linux/ga/train_ga_135.py`
- arc 家族 GA：`routers/arc_linux/ga/train_ga_arc.py`

代码层改动：

- `tools/pcb_bjut_router.py`
  - 支持 GA routerType 和 auto routerType。
  - 支持 `ga_python`、`ga_eval_budget`、`ga_timeout_seconds`。
  - 将 GA/RL 抽象为搜索模块，统一候选评分和结果复制逻辑。
  - 支持 Linux 路由二进制名：135 用 `d.out/e.out/f.out`，arc 用 `a.out/b.out/c.out`。
  - 增加 ELF 架构检测，架构不匹配时判定不可用并给出明确错误。
- `config.ini`
  - 默认路由目录改为 `./routers/135_linux` 和 `./routers/arc_linux`。
  - 增加 `ga_root_dir`、`auto_root_dir`、`ga_python`、`ga_eval_budget`、`ga_timeout_seconds`。

## 自然语言拆线/重布状态

- 本次重点完成的是自然语言 fanout/布线，即“用户提出布线需求并描述网络层分配，Agent 理解后生成层分配和顺序，再进行布线”。
- 拆线重布入口已有自然语言 net 识别增强：
  - 可识别 `net13`、`NET_A1` 等显式网络名。
  - 若用户说要重布某个 net，Agent 可先进入获取版图/拆线重布流程。
  - 若没有明确 net，也仍可走前端框选线路的旧路径。
- 本次没有把“拆完线后自动接 fanout/GA/auto 完整闭环”作为独立真实板级执行验证项；后续如要扩展，需要结合前端删除线路返回结果和 route 参数生成上下文再做端到端验证。

## DRC 报告与坐标

DRC 报告：

- 仍然沿用原来的 WebSocket/API 字段，不新增 `contentFormat`、`reportFormat` 等字段。
- 将原来的纯文本报告改为 Markdown 内容，直接写入既有 `body.content` / `report`。
- 新报告包含：
  - `## DRC 分析`
  - DRC 状态
  - txt 输出状态
  - 失败回填 txt 状态
  - importLines 是否允许
  - DRC 规则检查段落
  - 本地布线质量分类模型报告

坐标：

- `line.out` 增量导入坐标不再使用手写的单轴换算。
- 改为复用 `convert.py` 的 DBU 舍入和 outline-only translation 规则：
  - `kicad_mm_point_to_txt_dbu`
  - `dbu_to_mil`
  - `kicad_board_uses_outline_only_translation`
- 对 outline-only 版图和原生 KiCad 坐标分别保持一致转换。

DRC vendor 更新：

- 替换并保留了新版 DRC 规则/解析相关文件。
- 新增 pad/segment 几何辅助：
  - `vendor/pcb_drc_agent_tool/rules/rule_helpers/pad_segment_geometry.py`
- parser/model/rules 增加对 pad 尺寸、层、旋转、arc 等信息的支持。

## 重要文件

主要代码：

- `tools/pcb_bjut_router.py`
- `tools/pcb_tools.py`
- `tools/pcb_fanout_memory.py`
- `tools/pcb_nl_fanout.py`
- `gateway/platforms/websocket.py`
- `run_agent.py`
- `convert.py`

主要测试：

- `tests/tools/test_pcb_bjut_router.py`
- `tests/tools/test_pcb_nl_fanout.py`
- `tests/tools/test_pcb_tools_mode_guard.py`
- `tests/gateway/test_pcb_nl_fanout_websocket.py`
- `tests/gateway/test_websocket_pcb_flow.py`
- `tests/run_agent/test_pcb_nl_fanout_shim.py`

路由包：

- `routers/135_linux`
- `routers/arc_linux`

## 测试结果

本地测试环境：

- Python: Codex 桌面内置 Python `3.12.13`
- 临时虚拟环境：`/tmp/pcb_agent_testvenv`
- 为测试安装的临时包包括 `pytest`、`pytest-asyncio`、`aiohttp`、`openai` 等；未写入仓库。

已通过：

```bash
python3 -m py_compile \
  tools/pcb_bjut_router.py tools/pcb_tools.py tools/pcb_fanout_memory.py \
  tools/pcb_nl_fanout.py gateway/platforms/websocket.py run_agent.py convert.py
```

结果：通过。

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /tmp/pcb_agent_testvenv/bin/python -m pytest -q -o addopts="" \
  tests/tools/test_pcb_bjut_router.py \
  tests/tools/test_pcb_nl_fanout.py \
  tests/gateway/test_pcb_nl_fanout_websocket.py \
  tests/run_agent/test_pcb_nl_fanout_shim.py
```

结果：`26 passed, 1 skipped, 1 warning`。

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /tmp/pcb_agent_testvenv/bin/python -m pytest -q -o addopts="" \
  tests/tools/test_pcb_tools_mode_guard.py
```

结果：`69 passed, 4 skipped, 1 warning`。

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /tmp/pcb_agent_testvenv/bin/python -m pytest -q -o addopts="" \
  -p pytest_asyncio.plugin \
  tests/gateway/test_websocket_pcb_flow.py tests/tools/test_pcb_tools_mode_guard.py \
  -k "not import_fanout_result and not cached_fanout_route and not agent_loop and not forced_fanout and not handle_user_message and not frontend_confirmed and (fanout or router_type or selection_step or generate_fanout_params or route_requires_router_type or reroute)"
```

结果：`105 passed, 4 skipped, 99 deselected, 1 warning`。

```bash
git diff --check -- . ':(exclude).DS_Store'
```

结果：通过。

## 已知限制

- 天翼云测试容器是 `aarch64`，当前同步的 Linux `.out` 路由二进制是 `x86_64` ELF，因此真实二进制在该容器不能直接执行。
- 代码已加入架构检测，架构不匹配时会将路由器判定为不可用，避免运行时报 `Exec format error`。
- 若要在天翼云真实执行 GA/RL/北科大二进制，需要提供 `aarch64` 版本路由二进制，或改用 x86_64 测试容器。
- 本地 macOS 也不会直接执行 Linux ELF；本地测试使用 mock/subprocess 替身验证适配逻辑和调用参数。

## 同步结论

- 天翼云实现已同步到本地 `codex/drc-agent-content-report` 分支。
- 自然语言 fanout/布线、GA 接入、auto 兜底、DRC Markdown 报告、`line.out` 坐标转换均已纳入同一提交范围。
- 提交时会排除本地已有的 `.DS_Store` 修改。
