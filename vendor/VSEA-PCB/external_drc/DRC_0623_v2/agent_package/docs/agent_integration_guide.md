# PCB DRC Agent 使用说明

## 1. 功能

本工具用于 Agent 生成 KiCad PCB 布线结果后，自动执行 BGA Hard DRC 检查并输出结构化 JSON。

默认检查范围为目标 BGA 区域。该区域由目标 BGA 焊盘包围框向外扩展半个 BGA pitch 得到。

当前启用规则：

- `HR_CONNECT_PAD_NOT_ESCAPED`：目标 BGA 信号焊盘没有初始逃逸连接。
- `HR_DRC_SEGMENT_CROSSING`：目标 BGA 区域内同层异网线段交叉或重叠。
- `HR_DRC_PAD_SEGMENT_CROSSING`：目标 BGA 区域内异网焊盘与线段实体重叠。
- `HR_DRC_PAD_ARC_CROSSING`：目标 BGA 区域内异网焊盘与圆弧走线实体重叠。
- `HR_DRC_PAD_VIA_CROSSING`：目标 BGA 焊盘与异网过孔在共同铜层实体重叠。
- `HR_DRC_VIA_TRACK_CROSSING`：目标 BGA 区域内异网过孔与直线或圆弧走线实体重叠。
- `HR_CONNECT_BRANCH_INCOMPLETE`：目标 BGA 逃逸分支没有连续走出 BGA 区域。

## 2. 推荐命令

指定目标 BGA，并输出 Agent 中文 JSON：

```powershell
python prod_main.py "输入文件.kicad_pcb" `
  --target-bga U67 `
  --agent-zh-json-out "out\drc_agent.json"
```

不指定 `--target-bga` 时，工具自动选择 BGA 焊盘数量最多的器件：

```powershell
python prod_main.py "输入文件.kicad_pcb" `
  --agent-zh-json-out "out\drc_agent.json"
```

同时输出调试日志：

```powershell
python prod_main.py "输入文件.kicad_pcb" `
  --target-bga U67 `
  --agent-zh-json-out "out\drc_agent.json" `
  --log-file "out\drc.log" `
  --debug-log
```

默认检查模式已经是 `hard`，通常不需要传 `--check-mode hard`。

## 3. Agent 调用示例

```python
import json
import subprocess


def run_pcb_drc(pcb_path: str, output_json: str, target_bga: str = ""):
    command = [
        "python",
        "prod_main.py",
        pcb_path,
        "--agent-zh-json-out",
        output_json,
    ]
    if target_bga:
        command.extend(["--target-bga", target_bga])

    completed = subprocess.run(
        command,
        cwd=r"D:\pcb_drc_demo_v4",
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout)

    with open(output_json, "r", encoding="utf-8") as file:
        return json.load(file)


result = run_pcb_drc(
    pcb_path=r"out\routed_board.kicad_pcb",
    output_json=r"out\drc_agent.json",
    target_bga="U67",
)

if result["result"]["hard_issue_count"] > 0:
    print(result["message_zh"])
    for issue in result["issues"]:
        print(issue["rule"], issue["location"])
```

## 4. Agent JSON

`--agent-zh-json-out` 输出结构：

```json
{
  "schema_version": "drc_agent_v3",
  "language": "zh-CN",
  "tool": {},
  "input": {
    "check_mode": "hard",
    "target_bga": "U67",
    "check_scope": "target_bga_region"
  },
  "scope": {
    "type": "target_bga_region",
    "target_bga": "U67",
    "bga_bbox": [10.0, 30.0, 20.0, 40.0]
  },
  "enabled_rules": [],
  "message_zh": "...",
  "result": {},
  "board_info": {},
  "routing_metrics": {},
  "precheck": {},
  "issues": []
}
```

Agent 建议优先读取：

- `result.hard_issue_count`：Hard 错误数量。
- `result.conclusion`：检查结论。
- `scope.target_bga`：实际检查的 BGA。
- `scope.bga_bbox`：交叉检查区域。
- `enabled_rules`：本次实际执行的规则。
- `issues`：结构化错误列表。
- `message_zh`：可直接加入报告的中文说明。

## 5. Issue 字段

每个错误包含：

- `rule`：规则编号。
- `severity`：严重程度。
- `location.obj1`、`location.obj2`：冲突对象。
- `location.net`：相关网络。
- `location.layer`：铜层。
- `location.x`、`location.y`：问题坐标。
- `extra.target_bga`：目标 BGA。
- `extra.bga_bbox`：BGA 检查区域。

Pad 与 Segment 冲突还包含：

- `extra.pad_shape`
- `extra.pad_size`
- `extra.pad_rotation`
- `extra.segment_start`
- `extra.segment_end`
- `extra.segment_width`

## 6. 退出码

- `0`：工具成功执行。是否通过 DRC 由 JSON 中的 `hard_issue_count` 判断。
- `1`：文件不存在或运行异常。
- `2`：参数或配置错误。

Agent 应先检查进程退出码，再读取 JSON。
