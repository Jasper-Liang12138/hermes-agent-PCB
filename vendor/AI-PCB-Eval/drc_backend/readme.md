# DRC 评分接口说明

## 1. 接口目的

该接口用于对单个 PCB 文件进行 DRC 评分，输出可用于联合测评系统的结构化结果。

当前接口主要提供：

* 单板 DRC 可行性评分
* hard rule 违规统计
* 详细 issue 列表
* timing 信息
* 标准化返回格式，便于与其他评分器联合加权

---

## 2. 适用场景

适用于以下场景：

* 联合测评系统调用 DRC 子评分器
* 批量评测脚本调用
* 上层前端或服务接口调用
* 大模型生成 PCB 后自动评分

---

## 3. 输入输出说明

### 输入

输入对象为一个 **完整的 `.kicad_pcb` 文件路径**。

### 输出

输出为一个 Python `dict`，包含：

* 执行状态
* DRC 分数
* pass/fail
* 详细统计
* issue 明细
* timing

---

## 4. 接口列表

### 4.1 `evaluate_board`

#### 功能

执行单板 DRC 评测，返回较完整的内部结果。

#### 函数签名

```python
evaluate_board(board_path: str, check_mode: str = "hard") -> dict
```

#### 参数说明

| 参数名          | 类型    | 说明                   |
| ------------ | ----- | -------------------- |
| `board_path` | `str` | `.kicad_pcb` 文件路径    |
| `check_mode` | `str` | 评测模式，当前推荐使用 `"hard"` |

#### 返回结果

返回一个字典，典型结构如下：

```python
{
    "board_name": "fault_center.kicad_pcb",
    "board_path": "D:/pcb_drc_demo/samples/fault_center.kicad_pcb",
    "check_mode": "hard",
    "hard_score": {
        "hard_pass": False,
        "hard_penalty": 15.0,
        "hard_score": 25.0,
        "hard_issue_count": 4,
        "hard_rule_counts": {
            "HR_CONNECT_BRANCH_INCOMPLETE": 1,
            "HR_DRC_SEGMENT_CROSSING": 2,
            "HR_TOPO_MULTIPLE_ESCAPE": 1
        }
    },
    "hard_issues": [...],
    "opt_issues": [...],
    "all_issues": [...],
    "timing": {
        "parse_time": 0.014,
        "hard_time": 0.001,
        "opt_time": 0.0,
        "total_check_time": 0.001,
        "whole_program_time": 0.015
    }
}
```

---

### 4.2 `evaluate_drc_score`

#### 功能

执行标准化 DRC 评分接口，供外部评测系统调用。

#### 函数签名

```python
evaluate_drc_score(board_path: str, check_mode: str = "hard") -> dict
```

#### 参数说明

| 参数名          | 类型    | 说明                   |
| ------------ | ----- | -------------------- |
| `board_path` | `str` | `.kicad_pcb` 文件路径    |
| `check_mode` | `str` | 评测模式，当前推荐使用 `"hard"` |

#### 返回结果

返回统一 scorer 格式，典型结构如下：

```python
{
    "ok": True,
    "input": {
        "board_path": "D:/pcb_drc_demo/samples/fault_center.kicad_pcb",
        "check_mode": "hard"
    },
    "score_name": "drc_hard_score",
    "score": 25.0,
    "pass": False,
    "details": {
        "hard_penalty": 15.0,
        "hard_issue_count": 4,
        "hard_rule_counts": {
            "HR_CONNECT_BRANCH_INCOMPLETE": 1,
            "HR_DRC_SEGMENT_CROSSING": 2,
            "HR_TOPO_MULTIPLE_ESCAPE": 1
        }
    },
    "artifacts": {
        "issues": [...],
        "timing": {
            "parse_time": 0.014,
            "hard_time": 0.001,
            "opt_time": 0.0,
            "total_check_time": 0.001,
            "whole_program_time": 0.015
        }
    },
    "error": None
}
```

#### 字段说明

| 字段名          | 说明                     |
| ------------ | ---------------------- |
| `ok`         | 接口是否成功执行               |
| `score_name` | 当前评分器名称                |
| `score`      | DRC 分数，范围为 `0~100`     |
| `pass`       | 是否通过 hard rule 基础检查    |
| `details`    | 评分细节与规则统计              |
| `artifacts`  | issue 明细与 timing 等附属信息 |
| `error`      | 当执行失败时返回异常信息           |

---

## 5. 分数定义

当前 DRC 评分主要基于 **hard rule**。

### 5.1 hard rule 权重

当前默认权重如下：

```python
HARD_RULE_WEIGHTS = {
    "HR_CONNECT_PAD_NOT_ESCAPED": 5.0,
    "HR_TOPO_MULTIPLE_ESCAPE": 3.0,
    "HR_DRC_SEGMENT_CROSSING": 4.0,
    "HR_CONNECT_BRANCH_INCOMPLETE": 4.0,
}
```

### 5.2 评分方式

先统计每条 hard rule 的违规次数，再按权重计算总惩罚：

[
hard_penalty = \sum (rule_count \times rule_weight)
]

再映射为 hard score：

[
hard_score = \max(0, 100 - 5 \times hard_penalty)
]

### 5.3 pass 判定

当前规则为：

```python
hard_pass = (hard_issue_count == 0)
```

---

## 6. 使用方式

### 6.1 方式一：Python 直接调用

#### 示例代码

```python
from drc_backend.api import evaluate_drc_score

pcb_path = r"D:\pcb_drc_demo\samples\fault_center.kicad_pcb"
res = evaluate_drc_score(pcb_path, check_mode="hard")

print("ok:", res["ok"])
print("score:", res["score"])
print("pass:", res["pass"])
print("issue_count:", res["details"]["hard_issue_count"])
```

---

### 6.2 方式二：联合测评系统调用

建议在联合测评系统中按如下方式接入：

```python
from drc_backend.api import evaluate_drc_score

drc_result = evaluate_drc_score(board_path, check_mode="hard")

if not drc_result["ok"]:
    s2 = 0.0
else:
    s2 = drc_result["score"] / 100.0
```

说明：

* 本接口输出的 `score` 范围为 `0~100`
* 若联合评测总流程要求 `s2 ∈ [0,1]`，则应做归一化：

  ```python
  s2 = score / 100.0
  ```

---

### 6.3 与其他评分器联合加权

若系统中还有其他评分器，例如语义相似度评分 `s1`，则可以按如下方式融合：

```python
final_score = alpha * s1 + (1 - alpha) * s2
```

其中：

* `s1`：语义相似度得分，范围 `[0,1]`
* `s2`：DRC 归一化得分，范围 `[0,1]`
* `alpha`：融合权重

示例：

```python
alpha = 0.5
final_score = alpha * semantic_score + (1 - alpha) * drc_score_norm
```

---

## 7. 错误处理

当接口执行失败时，返回：

```python
{
    "ok": False,
    "input": {...},
    "score_name": "drc_hard_score",
    "score": 0.0,
    "pass": False,
    "details": {},
    "artifacts": {},
    "error": {
        "type": "ExceptionType",
        "message": "具体错误信息"
    }
}
```

调用方应首先检查：

```python
if not result["ok"]:
    ...
```

---

## 8. issue 字段说明

`artifacts["issues"]` 中每个 issue 为一个字典，常见字段如下：

| 字段名             | 说明         |
| --------------- | ---------- |
| `issue_id`      | issue 唯一标识 |
| `rule`          | 触发的规则名     |
| `severity`      | 严重程度       |
| `message`       | 人类可读的错误说明  |
| `obj1` / `obj2` | 相关对象标识     |
| `net`           | 相关网络       |
| `layer`         | 所在层        |
| `x`, `y`        | 相关坐标       |
| `category`      | issue 类别   |
| `suggestion`    | 修复建议       |
| `component`     | 元件名        |
| `pad_id`        | pad 标识     |
| `extra`         | 附加上下文信息    |

---

## 9. 依赖说明

调用该接口前，需要保证：

* `.kicad_pcb` 文件路径可访问
* `drc_backend` 包已加入 Python 可导入路径
* 依赖模块完整，包括：

  * `model/`
  * `parser/`
  * `engine/`
  * `rules/`
  * `geometry/`

---

## 10. 推荐调用规范

建议外部系统按以下顺序调用：

1. 提供完整 `.kicad_pcb` 文件路径
2. 调用 `evaluate_drc_score(board_path, check_mode="hard")`
3. 检查 `ok`
4. 获取 `score`
5. 若需要融合，则归一化：

   ```python
   s2 = score / 100.0
   ```
6. 与其他评分器结果加权融合

---

## 11. 当前版本说明

当前版本主要提供：

* hard rule 基础可行性评测
* hard score 计算
* issue 明细输出

后续可扩展：

* optimization rule 评分
* differential rule 评分
* batch 接口

---

# 最简调用示例

```python
from drc_backend.api import evaluate_drc_score

res = evaluate_drc_score(r"D:\pcb_drc_demo\samples\fault_center.kicad_pcb")

if res["ok"]:
    s2 = res["score"] / 100.0
    print("DRC normalized score:", s2)
else:
    print("DRC failed:", res["error"])
```

