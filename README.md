# PCB Agent LangGraph

基于 LangGraph 的 PCB 智能体，负责 PCB 工程问答、Fanout、局部重布等流程控制，同时保留已验证的前端 WebSocket 协议、PCB 工具调用方式和外部布线工具边界。

## 能力范围

- PCB 工程问答
- Global Fanout 工作流
- 局部拆线重布 Reroute 工作流
- WebSocket 协议兼容 `message` / `tool-calls` / `tool-results` / `error`
- 统一 `PCBState`，节点之间只通过 State 交换信息
- Live Evaluation 入口，直接调用正式 LangGraph Agent
- Trace Logging 与 Replay 数据结构预留

## 运行

Windows 首次启动：

```powershell
cd F:\doctor\hermes-agent\PCB_AGENT_LangGraph
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pcb_agent_langgraph.websocket.server --config .\config.ini
```

已有虚拟环境时：

```powershell
cd F:\doctor\hermes-agent\PCB_AGENT_LangGraph
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pcb_agent_langgraph.websocket.server
```

默认读取当前项目目录下的 `config.ini`。如需显式指定其它配置文件：

```powershell
.\.venv\Scripts\python.exe -m pcb_agent_langgraph.websocket.server --config .\config.ini
```

默认 WebSocket 地址是：

```text
ws://127.0.0.1:7074
```

如果前端在同局域网其它机器上，需要在 `config.ini` 中允许外部连接：

```ini
[server]
host = 0.0.0.0
port = 7074
```

## 模型调用

系统只使用一个模型，运行时统一命名为 `pcb-model`。

模型参数从本项目 `config.ini` 的 `[reroute-model]` 读取：

```ini
[reroute-model]
api_key = <your-api-key>
model = <your-model-id>
base_url = <openai-compatible-base-url>
max_tokens = 65536
```

模型接口采用 OpenAI-compatible `/chat/completions`：

```text
POST {base_url}/chat/completions
Authorization: Bearer {api_key}
model: {model}
```

可复制 `config.example.ini` 为 `config.ini` 后填入真实模型配置。本项目默认只读取 `PCB_AGENT_LangGraph\config.ini`；也可以通过 `--config` 显式指定配置文件。不会自动回退到其他目录。

VSEA reroute 的 hard DRC 依赖外部 `DRC_0623_v2\agent_package`。示例配置默认使用相对路径，部署时请把外部 DRC 包放到对应目录，或在 `config.ini` 的 `[reroute_loop]` 中按需改成其它路径：

```ini
drc_agent_package = .\external_drc\DRC_0623_v2\agent_package
# agent_drc_python 留空时使用运行环境默认 Python
agent_drc_python =
```

`agent_drc_python` 留空时使用运行环境默认 Python；Windows 可按实际环境填 `python`，macOS/Linux 通常填 `python3` 或虚拟环境解释器路径。

## Live Evaluation

真实/普通评测：

```powershell
.\.venv\Scripts\python.exe -m pcb_agent_langgraph.evaluation.runner .\eval_dataset_live.json --config .\config.ini --output-dir eval_runs\live
```

模拟端到端评测，使用正式 LangGraph Agent，但模拟 EDA 前端和外部 PCB 工具返回：

```powershell
.\.venv\Scripts\python.exe -m pcb_agent_langgraph.evaluation.runner .\eval_dataset_sim_e2e.json --config .\config.ini --output-dir eval_runs\sim_e2e --simulate-tools
```

## 目录

```text
pcb_agent_langgraph/
  graph/          LangGraph State、节点和图构建
  planner/        意图识别和工具规划
  models/         pcb-model 统一封装
  tools/          LangChain Tool 风格工具封装
  websocket/      EDA 前端 WebSocket 协议适配
  evaluation/     Live Evaluation、Trace、Replay、Report
  utils/          配置、日志、JSON 工具
tests/
```

## 配置

外部程序路径读取 `config.ini`：

- `[router] work_dir`
- `[router] arc_dir`
- `[router] 135_dir`
- `[router] rl_root_dir`

若真实可执行程序未配置，工具会返回明确失败状态，不会伪造成功结果。


## 真实工具资产

### DRC / AI-PCB-Eval

`vendor/AI-PCB-Eval` 是独立 DRC 与 PCB 评测工具链。当前 LangGraph 项目通过 `tools/pcb_reroute_drc.py` 调用其中的：

- `patch_kicad_from_raw_standalone.py`：把模型/布线器输出回填为完整中间数据格式
- `drc_backend/api.py`：执行 hard-rule DRC 检查并返回结构化结果

配置入口：

```ini
[drc]
enabled = 1
tool_path = .\tools\pcb_reroute_drc.py
eval_root = .\vendor\AI-PCB-Eval
```

### 可解释性模型

可解释性模型资产放在：

```text
explain_model/
  model/best.pt
  explain_code/
```

可解释性模型需要一个带 `torch/torchvision/pillow/numpy/matplotlib` 的 Python runtime。`python_executable` 可以临时指向本机已有环境，但不要把个人机器路径当成交付默认值：

```ini
[explain_model]
enabled = 1
# 示例路径，仅限本机；远端开发者需要改成自己的 runtime，或用下面脚本构建项目内 runtime。
python_executable = F:\PCB_QYF\PCB_Builder\cust_tools\PCBCopilot_dev\PCB-AGENT\python_runtime\python.exe
code_dir = .\explain_model\explain_code
checkpoint_path = .\explain_model\model\best.pt
```

封装 exe 或离线交付时，建议把 runtime 构建到项目目录。两个脚本都采用同一策略：优先复制已有 runtime；如果没有可复制 runtime，则可以创建项目内 venv 并安装 `requirements-explain.txt`。

方式一：从已有 runtime 复制。`-SourceRuntime` 是机器本地路径，GitHub 拉取代码的人员必须改成自己的路径；如果源路径不存在，脚本会自动回退到创建 venv，除非额外传入 `-CopyOnly`。

```powershell
.\scripts\build_explain_runtime.ps1 `
  -SourceRuntime <你的可解释性模型Python运行环境路径> `
  -TargetRuntime .\runtime\explain_python `
  -Force
```

方式二：没有可复制 runtime 时，从源码创建 venv 并安装 `requirements-explain.txt`。这是远端开发者更可移植的方式，但可能需要网络或本地 pip wheel 源。`-CreateVenv` 可以显式表达创建意图；不传 `-SourceRuntime` 时脚本也会默认创建。

```powershell
.\scripts\build_explain_runtime.ps1 `
  -CreateVenv `
  -Python python `
  -TargetRuntime .\runtime\explain_python `
  -Force
```

构建完成后在 `config.ini` 中使用项目内 runtime。venv 模式通常是 `Scripts\python.exe`，复制嵌入式 runtime 时可能是根目录 `python.exe`：

```ini
[explain_model]
python_executable = .\runtime\explain_python\Scripts\python.exe
```

封装脚本也支持同样的策略。推荐统一使用下面这一条命令：

```powershell
.\scripts\package-windows-lite.ps1 `
  -OutputDir "F:\PCB_QYF\PCB_Builder\cust_tools\PCBCopilot_dev\PCB-AGENT" `
  -Python ".\.venv\Scripts\python.exe" `
  -Config ".\config.live.ini" `
  -CreateExplainRuntime `
  -ExplainRuntimePython "python" `
  -InstallRequirements `
  -InstallPyInstaller `
  -Clean
```

这条命令会优先使用项目内 `runtime\explain_python`；如果不存在，则现场创建 explain runtime 并安装 `requirements-explain.txt`。如果主运行环境缺少依赖或 PyInstaller，会分别由 `-InstallRequirements` 和 `-InstallPyInstaller` 安装。

如果本机已有验证过的可解释性模型 runtime，可以在同一条命令里增加 `-SourceExplainRuntime`，脚本会优先复制该 runtime；如果源路径不存在且保留 `-CreateExplainRuntime`，则回退为现场创建：

```powershell
.\scripts\package-windows-lite.ps1 `
  -OutputDir "F:\PCB_QYF\PCB_Builder\cust_tools\PCBCopilot_dev\PCB-AGENT" `
  -Python ".\.venv\Scripts\python.exe" `
  -Config ".\config.live.ini" `
  -SourceExplainRuntime "F:\path\to\existing\explain_python" `
  -CreateExplainRuntime `
  -ExplainRuntimePython "python" `
  -InstallRequirements `
  -InstallPyInstaller `
  -Clean
```

统一封装命令参数说明：

| 参数 | 作用 | 什么时候需要 |
|---|---|---|
| `-OutputDir` | 指定最终交付包输出目录，脚本会把 PyInstaller 产物、配置、工具、routers、vendor、runtime 等复制到这里。 | 每次封装建议显式指定，避免输出到默认旧路径。 |
| `-Python` | 指定用于构建主程序的 Python。这个环境需要能运行项目，并用于执行 PyInstaller。 | 本机有项目虚拟环境时填 `.\.venv\Scripts\python.exe`；如果路径不同，改成实际路径。 |
| `-Config` | 指定打包进交付包的配置文件，复制后会变成输出目录里的 `config.ini`。 | 正式交付通常用 `config.live.ini`；没有时会退回 `config.example.ini`。 |
| `-CreateExplainRuntime` | 当项目内没有 `runtime\explain_python`，也没有可复制的 `-SourceExplainRuntime` 时，现场创建可解释性模型 runtime。 | 另一台机器首次封装、没有现成 runtime 时使用。可能需要网络或本地 wheel 源安装 torch 等依赖。 |
| `-ExplainRuntimePython` | 指定创建 explain runtime 时使用的 Python 命令。它只负责 `python -m venv` 创建可解释性模型环境。 | 创建 runtime 时使用；常见值是 `python`，也可以是某个 Python 绝对路径。 |
| `-SourceExplainRuntime` | 指定一个已有、验证过的可解释性模型 runtime 源目录，脚本会优先复制它到项目内 runtime。 | 有现成 runtime 时使用，速度最快；如果源路径不存在且传了 `-CreateExplainRuntime`，会自动回退创建。 |
| `-InstallRequirements` | 如果主程序构建 Python 缺少 `requirements.txt` 里的依赖，允许脚本自动安装。 | 新机器或新虚拟环境首次封装时建议开启。 |
| `-InstallPyInstaller` | 如果主程序构建 Python 没有 PyInstaller，允许脚本自动安装。 | 新机器首次封装时建议开启。 |
| `-Clean` | 打包前清理 `dist\pyinstaller`、`build\pyinstaller` 和输出目录，避免旧文件混入。 | 正式交付包建议开启。 |
| `-SkipRouters` | 不复制 `routers/`。 | 只验证 agent 主程序、不需要真实布线器时使用。 |
| `-SkipDrcVendor` | 不复制 `vendor/`。 | 不需要 DRC/AI-PCB-Eval 时使用。 |
| `-SkipExplainModel` | 不复制 `explain_model/`。 | 不需要可解释性模型代码和权重时使用。 |
| `-SkipExplainRuntime` | 不复制或创建 explain runtime。 | 完全不启用可解释性模型，或交付后另行配置 runtime 时使用。 |

注意：`-Python` 是主程序打包环境，`-ExplainRuntimePython` 是创建可解释性模型 runtime 的环境，二者可以相同，也可以不同。

