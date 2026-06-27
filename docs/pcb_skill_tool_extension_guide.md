# PCB Agent Skill / Tool 扩展指南

本文面向后续维护 `PCB-Agent` 的开发人员，说明如何手动新增 `skill` 和 `tool`，并保证不破坏当前 PCB WebSocket 主流程。

## 1. 先理解边界

当前系统分三层：

- `toolset` 决定模型能不能调用某类工具
- `skill` 决定模型会不会得到某类流程说明
- `websocket PCB auto-skill admission` 决定某个 skill 能不能在 PCB 主流程里被自动预加载

**重要原则：**

- 新增 `skill` 可以积极一些
- 新增 `tool` 要保守一些
- 不要通过新增 `skill` 去间接突破 `hermes-websocket-pcb` 的工具边界

## 2. 新增 Skill 的目录放哪

支持三类来源：

1. 仓库内置 skill  
   放到：

   - `F:\doctor\hermes-agent\hermes-agent-PCB\skills\hardware\...`
   - 或 `F:\doctor\hermes-agent\hermes-agent-PCB\skills\pcb\...`

2. 用户侧本地 skill  
   放到：

   - `%USERPROFILE%\.hermes\skills\hardware\...`
   - 或 `%USERPROFILE%\.hermes\skills\pcb\...`

3. 额外外部 skill 目录  
   在 `~/.hermes/config.yaml` 中配置：

   ```yaml
   skills:
     external_dirs:
       - D:\my-pcb-skills
   ```

   然后把 skill 放到：

   - `D:\my-pcb-skills\hardware\...`
   - 或 `D:\my-pcb-skills\pcb\...`

## 3. Skill 的目录结构

最小结构：

```text
your-skill/
├── SKILL.md
├── references/   # 可选
├── templates/    # 可选
├── scripts/      # 可选
└── assets/       # 可选
```

当前加载入口：

- 发现与扫描：`agent/skill_commands.py::scan_skill_commands`
- 读取 skill：`tools/skills_tool.py::skill_view`
- auto-load 组装：`agent/skill_commands.py::_load_skill_payload`
- supporting files 提示：`agent/skill_commands.py::_build_skill_message`

## 4. 想让 Skill 自动进入 PCB WebSocket 主流程，要写什么 frontmatter

`SKILL.md` 必须有下面这段：

```yaml
---
name: bga-routing-history
description: Reuse historical BGA routing decisions for PCB websocket flow
metadata:
  hermes:
    category: hardware
    websocket_pcb:
      enabled: true
      intents: [fanout, reroute, mixed]
      priority: 300
      mode: inject_only
      persistent_websocket_session: true
---
```

字段含义：

- `enabled`: 是否允许进入 websocket PCB auto-skill
- `intents`: 适用阶段，可选 `fanout` / `reroute` / `mixed`
- `priority`: 越大越靠前注入
- `mode`: 当前只支持 `inject_only`
- `persistent_websocket_session`: 是否允许在非新 session 的 PCB turn 重复注入

当前 admission 入口：

- `agent/swsd/websocket_skill_admission.py::iter_admitted_pcb_websocket_skills`
- `agent/swsd/websocket_skill_admission.py::resolve_auto_skills_for_pcb_turn`
- `agent/swsd/websocket_skill_admission.py::auto_skill_persists_for_websocket`

## 5. 哪些 Skill 会自动被 PCB WebSocket 预加载

调用链如下：

1. `gateway/platforms/websocket.py::_handle_user_message`
2. 判断进入 `route_mode=pcb`
3. 调用 `resolve_auto_skills_for_pcb_turn(...)`
4. 把结果写入 `MessageEvent.auto_skill`
5. `gateway/run.py::_should_inject_auto_skill_for_turn(...)`
6. `gateway/run.py` 调 `agent/skill_commands.py::_load_skill_payload(...)`
7. skill 内容被拼进当前 turn 的 prompt

也就是说：

- **skill 是预加载进 prompt**
- **不是自动执行 skill 目录下的脚本**

## 6. Skill 里的 scripts/ 现在怎么用

当前语义是：

- `scripts/` 可以存在
- `skill_view(name, file_path)` 可以读取这些脚本
- auto-skill 预加载时会告诉模型这些脚本可读
- **不会自动执行**

不要假设：

- skill 被 auto-load 了，脚本就会自动跑
- 往 `scripts/` 里塞一个 Python 文件，就等于系统新增了执行能力

如果某个 skill 需要真正执行逻辑，正确做法是：

1. 先把流程固化成 `skill`
2. 如果稳定需要某个动作，再单独新增 `tool`

## 7. 新增 Tool 怎么做

Hermes 原生接入步骤固定三处：

### 第一步：新增工具文件

在：

- `F:\doctor\hermes-agent\hermes-agent-PCB\tools\your_tool.py`

参考现有模式实现：

- `registry.register(...)`
- `handler`
- `check_fn`

注册中心：

- `tools/registry.py::ToolRegistry.register`

### 第二步：让工具被发现

在：

- `model_tools.py::_discover_tools`

加入对应 import，让模块导入时触发 `registry.register(...)`

### 第三步：挂到 toolset

在：

- `toolsets.py`

决定它属于哪个 toolset。

## 8. 哪些 Tool 不要直接加进 PCB 主流程

不要直接把下面这些能力因为某个 skill 需要，就塞进 `hermes-websocket-pcb`：

- `terminal`
- `process`
- `read_file`
- `write_file`
- `patch`
- `browser_*`
- `delegate_task`
- `execute_code`

当前 PCB 主流程 toolset 在：

- `toolsets.py::TOOLSETS["hermes-websocket-pcb"]`

当前 WebSocket PCB 的强制收口在：

- `gateway/run.py::_apply_turn_toolset_overrides`

这里会主动剔除：

- `skills`
- `web`
- `browser`
- `terminal`
- `file`
- `delegation`
- `code_execution`

这层**不要轻易改**。

## 9. 如果一定要新增 PCB 专用 Tool，推荐怎么做

推荐顺序：

1. 新增 `tool` 文件，例如 `tools/pcb_history_recover.py`
2. 在 `model_tools.py::_discover_tools` 注册导入
3. 在 `toolsets.py` 里只把它加入：
   - `hermes-websocket`
   - 或新建一个更小的 PCB toolset
4. 确认不会破坏现有流程后，再评估是否加入：
   - `TOOLSETS["hermes-websocket-pcb"]`

判断标准：

- 是否只依赖 PCB 当前会话上下文
- 是否不会越权访问本地系统
- 是否不会把 agent 变成通用执行器
- 是否对 fanout / reroute 主链路有稳定收益

## 10. 当前关键函数索引

### Skill 相关

- skill 扫描：`agent/skill_commands.py::scan_skill_commands`
- skill 载入：`agent/skill_commands.py::_load_skill_payload`
- skill prompt 组装：`agent/skill_commands.py::_build_skill_message`
- skill_view：`tools/skills_tool.py::skill_view`
- 外部目录读取：`agent/skill_utils.py::get_external_skills_dirs`
- 全部 skill 根目录：`agent/skill_utils.py::get_all_skills_dirs`

### PCB WebSocket auto-skill admission

- admission 主入口：`agent/swsd/websocket_skill_admission.py::resolve_auto_skills_for_pcb_turn`
- admission 扫描：`agent/swsd/websocket_skill_admission.py::iter_admitted_pcb_websocket_skills`
- 非新 session 重复注入判断：`agent/swsd/websocket_skill_admission.py::auto_skill_persists_for_websocket`
- websocket turn 接入点：`gateway/platforms/websocket.py::_handle_user_message`
- gateway auto-load 注入点：`gateway/run.py::_should_inject_auto_skill_for_turn`

### Tool 相关

- 工具注册：`tools/registry.py::ToolRegistry.register`
- 工具发现：`model_tools.py::_discover_tools`
- toolset 定义：`toolsets.py::TOOLSETS`
- websocket PCB toolset 收口：`gateway/run.py::_apply_turn_toolset_overrides`

## 11. 推荐的扩展顺序

最稳的顺序是：

1. 先加 `skill`
2. 让它进入 websocket PCB admission
3. 验证它只影响理解、恢复、默认值、recovery
4. 如果反复需要某个真实动作，再单独抽一个 `tool`
5. 最后再决定这个 `tool` 是否进入 `hermes-websocket-pcb`

一句话总结：

**先扩 skill，后扩 tool；先扩 prompt 能力，后扩执行能力；不要为了一个 skill 去放宽整个 PCB 主流程的工具边界。**
