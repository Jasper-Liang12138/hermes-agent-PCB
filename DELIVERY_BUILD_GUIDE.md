# PCB Agent EXE 交付版封装说明

本文档用于源码 Demo 包的交接，说明如何从源码重新封装 Windows EXE 交付版。

## 1. 环境准备

- Windows 10/11
- Python 3.11
- 源码目录：`hermes-agent-PCB`
- 推荐虚拟环境目录：`.venv311`

如果已有 `.venv311`，直接激活：

```bat
call .venv311\Scripts\activate.bat
```

如果没有虚拟环境：

```bat
py -3.11 -m venv .venv311
call .venv311\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -e .
python -m pip install pyinstaller
```

## 2. 封装 agent.exe

在源码根目录执行：

```bat
call .venv311\Scripts\activate.bat
python -m PyInstaller agent-gateway.spec --noconfirm --clean
```

成功后产物位于：

```text
dist\agent-gateway\
```

其中：

- `dist\agent-gateway\agent.exe` 是主程序
- `dist\agent-gateway\_internal\` 是运行依赖目录，必须和 `agent.exe` 一起交付

## 3. 组装交付目录

建议交付目录名固定为：

```text
PCB-AGENT\
```

目录结构：

```text
PCB-AGENT\
  agent.exe
  _internal\
  config.ini
  install.bat
  start.bat
  sync_config.ps1
  uninstall.bat
  template-config.yaml
  template.env
  VERSION.txt
  memories\
    intention_memory.md
  model\
    best.pt
  routers\
  skills\
  logs\
  router_work\
```

操作方式：

1. 从 `dist\agent-gateway\` 复制 `agent.exe` 和 `_internal\` 到 `PCB-AGENT\`。
2. 从上一版交付包复制 `routers\`、`skills\`、安装脚本和模板配置文件。
3. 复制可解释性模型权重到 `PCB-AGENT\model\best.pt`。当前权重可从本地模型目录或交付负责人提供的位置复制。
4. 复制默认意图识别 memory 到 `PCB-AGENT\memories\intention_memory.md`。源码中默认文件位于 `.github\delivery\memories\intention_memory.md`。
5. `logs\` 和 `router_work\` 只保留空目录，不要放测试日志或布线输出。
6. 检查 `config.ini`：

```ini
[explain]
checkpoint_path = model/best.pt

[router]
work_dir = .\router_work
arc_dir = .\routers\arc_windows_0519
135_dir = .\routers\135_windows_0519
rl_root_dir = .\routers
rl_arc_dir = .\routers\arc_windows_0519
rl_135_dir = .\routers\135_windows_0519

[server]
host = 0.0.0.0
port = 7073
```

## 4. Memory 使用方式

PCB Agent 的意图识别经验规则通过 Hermes built-in memory 注入 system prompt。默认规则文件位于源码：

```text
.github\delivery\memories\intention_memory.md
```

当前默认 memory 包含 PCB 意图识别规则，例如：

- 配置、端口、日志、打包、Git、前端调试问题属于 support-chat，不调用 `getProjectData`
- BGA 主链路固定为 `getProjectData -> pcb_extract_bga -> generateFanoutParams -> route`
- 已有 `fanoutParams` 且用户确认后，下一步只能调用 `route`

### 4.1 源码启动

源码启动时不会自动运行交付包的 `install.bat`，因此需要在启动前把默认 memory 复制到 Hermes home。

默认 Hermes home 是：

```text
%USERPROFILE%\.hermes
```

如果设置了 `HERMES_HOME` 环境变量，则以 `HERMES_HOME` 为准。

在源码根目录执行：

```powershell
$hermes = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:USERPROFILE ".hermes" }
New-Item -ItemType Directory -Force -Path "$hermes\memories" | Out-Null

if (-not (Test-Path "$hermes\memories\MEMORY.md")) {
  Copy-Item ".github\delivery\memories\intention_memory.md" "$hermes\memories\MEMORY.md"
}
```

然后再启动源码版：

```bat
call .venv311\Scripts\activate.bat
python delivery_gateway_main.py
```

如果用户机器上已经有 `MEMORY.md`，不要直接覆盖，应人工合并 `.github\delivery\memories\intention_memory.md` 中的 PCB 规则。

### 4.2 EXE 交付版启动

EXE 交付包中应包含：

```text
PCB-AGENT\memories\intention_memory.md
```

其中 `intention_memory.md` 是交付包里的默认规则文件名；`MEMORY.md` 是 Hermes 运行时固定读取的文件名。

安装脚本会把：

```text
PCB-AGENT\memories\intention_memory.md
```

复制为运行时文件：

```text
%USERPROFILE%\.hermes\memories\MEMORY.md
```

安装脚本不会覆盖用户已有的 `MEMORY.md`。如果客户机器已有 memory，需要人工合并新增规则。

`template-config.yaml` 中应显式启用 memory：

```yaml
memory:
  memory_enabled: true
  user_profile_enabled: true
```

注意：Hermes memory 是 agent/gateway 启动时读取的 system prompt 快照。安装或修改 `MEMORY.md` 后，需要重启 `agent.exe` 或源码 gateway，当前已运行 session 不会立刻生效。

## 5. 本地验证

在 `PCB-AGENT\` 下执行：

```bat
start.bat
```

确认端口监听：

```powershell
Get-NetTCPConnection -LocalPort 7073
```

确认进程：

```powershell
Get-Process -Name agent
```

## 6. 压缩交付版

压缩时顶层必须是 `PCB-AGENT\`，不要直接把目录内容散在 zip 根目录。

建议命名：

```text
PCB-Agent_delivery_vX.Y.zip
```

压缩前确认不要包含：

- `logs\` 下的历史日志
- `router_work\` 下的测试输入输出
- Python 缓存：`__pycache__`
- 临时调试目录

## 7. 常见问题

- 前端连不上：先确认 `config.ini` 端口是 `7073`，再确认 `agent.exe` 正在监听该端口。
- 修改 `config.ini` 后不生效：重启 `agent.exe`。
- `importLines` 没触发：确认布线器实际生成了可导入结果文件。`135` 系使用 `line.out`，`arc` 系使用 `ARC_output.txt`。
- 不要手工修改布线器输出文件内容，导入解析问题应优先在前端解析侧处理。
- Memory 不生效：确认 `MEMORY.md` 已在启动前放入 `%USERPROFILE%\.hermes\memories\` 或 `HERMES_HOME\memories\`，并重启 agent/gateway。
