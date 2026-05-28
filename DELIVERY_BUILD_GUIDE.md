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
4. `logs\` 和 `router_work\` 只保留空目录，不要放测试日志或布线输出。
5. 检查 `config.ini`：

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

## 4. 本地验证

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

## 5. 压缩交付版

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

## 6. 常见问题

- 前端连不上：先确认 `config.ini` 端口是 `7073`，再确认 `agent.exe` 正在监听该端口。
- 修改 `config.ini` 后不生效：重启 `agent.exe`。
- `importLines` 没触发：确认布线器实际生成了可导入结果文件。`135` 系使用 `line.out`，`arc` 系使用 `ARC_output.txt`。
- 不要手工修改布线器输出文件内容，导入解析问题应优先在前端解析侧处理。
