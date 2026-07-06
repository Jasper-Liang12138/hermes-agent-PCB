# PCB Escape Routing Eval

本目录提供一个面向 PCB 逃逸布线大模型的三阶段评测 Pipeline：

1. 语义相似度评分 `s1`
2. KiCad 回填
3. DRC 规则评分 `s2`

最终得分定义为：

```text
final_score = alpha * s1 + (1 - alpha) * s2
```

其中 `alpha` 默认为 `0.5`。

---

## 目录结构

- [__main__.py](/H:/Program/MindSpeed-LLM/eval/__main__.py): 命令行入口
- [api.py](/H:/Program/MindSpeed-LLM/eval/api.py): 直接调用入口
- [pipeline.py](/H:/Program/MindSpeed-LLM/eval/pipeline.py): 总评测流程
- [batch_loader.py](/H:/Program/MindSpeed-LLM/eval/batch_loader.py): batch 输入装载
- [semantic.py](/H:/Program/MindSpeed-LLM/eval/semantic.py): KiCad 语义相似度评分
- [fill.py](/H:/Program/MindSpeed-LLM/eval/fill.py): 回填封装
- [patch_kicad_from_raw_standalone.py](/H:/Program/MindSpeed-LLM/eval/patch_kicad_from_raw_standalone.py): 真实回填后端
- [drc.py](/H:/Program/MindSpeed-LLM/eval/drc.py): DRC 封装
- [drc_backend/api.py](/H:/Program/MindSpeed-LLM/eval/drc_backend/api.py): 真实 DRC 评分后端
- [types.py](/H:/Program/MindSpeed-LLM/eval/types.py): 输入输出数据结构
- [sample_batch](/H:/Program/MindSpeed-LLM/eval/sample_batch): 最小 batch 样例

---

## 当前实现逻辑

当前版本已经接入真实后端：

- 回填阶段直接调用 [patch_kicad_from_raw_standalone.py](/H:/Program/MindSpeed-LLM/eval/patch_kicad_from_raw_standalone.py)
- DRC 阶段直接调用 [drc_backend/api.py](/H:/Program/MindSpeed-LLM/eval/drc_backend/api.py)

主流程是：

1. `semantic.py` 计算 `s1`
2. `fill.py` 调用真实 patch backend 生成完整 `.kicad_pcb`
3. `drc.py` 调用真实 DRC backend 生成 `s2`
4. `pipeline.py` 聚合最终得分

---

## 输入方式

现在支持两种输入方式：

1. 目录 batch 模式
2. 直接函数调用模式

两种模式底层都走同一套 `PCBEvalPipeline`。

---

## 模式一：目录 Batch 模式

### 目录要求

评测直接读取三组目录：

- `incomplete-dir`: 原始不完整 KiCad 文件
- `prediction-dir`: 模型原始回复
- `label-dir`: 标准答案

三组目录按“相对路径去后缀”后进行匹配。

例如：

```text
eval/sample_batch/
  incomplete/
    demo-1.kicad_pcb
    demo-2.kicad_pcb
  prediction/
    demo-1.txt
    demo-2.txt
  label/
    demo-1.txt
    demo-2.txt
```

这里 `demo-1`、`demo-2` 就是样本 ID。

更复杂的层级也支持，例如：

```text
incomplete/a/demo-1.kicad_pcb
prediction/a/demo-1.txt
label/a/demo-1.txt
```

会匹配成同一个样本 `a/demo-1`。

### Quickstart

```bash
python -m eval \
  --incomplete-dir eval/sample_batch/incomplete \
  --prediction-dir eval/sample_batch/prediction \
  --label-dir eval/sample_batch/label \
  --output-dir eval_out
```

Windows PowerShell 示例：

```powershell
$env:PYTHONPATH = "H:\Program\MindSpeed-LLM"
python -m eval `
  --incomplete-dir eval/sample_batch/incomplete `
  --prediction-dir eval/sample_batch/prediction `
  --label-dir eval/sample_batch/label `
  --output-dir eval_out
```

### 输出结果

输出目录中会生成：

- `results.jsonl`: 每条样本的详细结果
- `summary.json`: 汇总统计

---

## 模式二：直接函数调用

如果你已经在上游拿到了三组内存数据，不想再写临时文件，可以直接导入函数调用。

### 入口函数

```python
from eval import evaluate_samples, EvalConfig
```

### 输入要求

直接传三组等长 list：

- `incomplete_kicad_list`
- `prediction_raw_list`
- `label_list`

可选传入：

- `sample_ids`
- `config`

### 示例

```python
from eval import EvalConfig, evaluate_samples

incomplete_kicad_list = [
    "(kicad_pcb\n  (version 20171130)\n)",
    "(kicad_pcb\n  (version 20171130)\n)",
]

prediction_raw_list = [
    "```kicad\n(segment (start 1 1) (end 2 2) (width 0.2) (layer Top) (net 1))\n```",
    "```kicad\n(via (at 10 12) (size 0.6) (drill 0.3) (layers Top Bottom) (net 2))\n```",
]

label_list = [
    "(segment (start 1 1) (end 2 2) (width 0.2) (layer Top) (net 1))",
    "(via (at 10 12) (size 0.6) (drill 0.3) (layers Top Bottom) (net 2))",
]

results, summary = evaluate_samples(
    incomplete_kicad_list,
    prediction_raw_list,
    label_list,
    sample_ids=["case-1", "case-2"],
    config=EvalConfig(alpha=0.5),
)
```

### 返回值

- `results`: `list[dict]`，每条样本的详细结果
- `summary`: `dict`，整批样本的汇总结果

---

## 内部输入结构

无论是目录模式还是函数模式，最终都会构造成内部 `SampleInput`：

- `sample_id`
- `context_kicad`
- `prediction_raw`
- `label`
- `prompt`
- `meta`

当前 `prompt` 为空，`meta` 只存附加信息。

---

## 语义评分 `s1`

语义评分由 [semantic.py](/H:/Program/MindSpeed-LLM/eval/semantic.py) 完成。

当前流程：

1. 从 `prediction_raw` 中提取代码块
2. 判断是否包含 KiCad 风格代码
3. 对预测和 `label` 做 KiCad 感知归一化
4. 计算多指标加权相似度

当前使用的指标：

- `sequence_ratio`
- `token_jaccard`
- `token_overlap`
- `number_score`
- `feature_score`

默认要求回复中包含 KiCad 代码；如果没有，则 `s1 = 0`。

此外，当前版本也会额外计算一个轻量普通文本分支：

- 从 `prediction_raw` 和 `label` 中去掉 code block
- 对剩余普通文本做简单归一化
- 基于字符串相似度和词级重合度得到 `text_score`

最终语义分会按以下方式融合：

```text
semantic_score = semantic_kicad_weight * kicad_score
               + semantic_text_weight * text_score
```

默认权重为：

- `semantic_kicad_weight = 0.85`
- `semantic_text_weight = 0.15`

如果 `label` 中没有普通文本，则最终语义分直接退化为 `kicad_score`，不会因为模型多写了一些解释性文字而被强行拉低。

如果希望允许纯文本回复参与评分，可以在 `EvalConfig` 中设置：

```python
EvalConfig(require_kicad_code=False)
```

或命令行加：

```bash
--allow-non-kicad
```

---

## 回填阶段

回填由 [fill.py](/H:/Program/MindSpeed-LLM/eval/fill.py) 封装，底层直接调用：

```python
fill_incomplete_board_from_raw_text(raw_model_text, incomplete_board_text)
```

当前实现特点：

- 直接使用 `sample.prediction_raw`
- 直接使用 `sample.context_kicad`
- 从原始模型输出中抽取 `(segment ...)` 和 `(via ...)`
- 忽略其他解释性文字
- 将抽取出的对象插回不完整板文件

回填结果 detail 中会记录：

- `segments_count`
- `vias_count`
- `other_lines_count`
- `fill_backend`

---

## DRC 阶段

DRC 由 [drc.py](/H:/Program/MindSpeed-LLM/eval/drc.py) 封装，底层直接调用：

```python
evaluate_drc_score(board_path, check_mode="hard")
```

DRC backend 返回的 `score` 范围为 `0~100`，本框架统一映射为：

```python
s2 = score / 100.0
```

当前 detail 中会保留：

- `score_name`
- `pass`
- `hard_penalty`
- `hard_issue_count`
- `hard_rule_counts`
- `timing`
- `drc_backend`

---

## 最终输出字段

单条样本结果主要包含：

- `sample_id`
- `s1`
- `s2`
- `final_score`
- `status`
- `prediction_code`
- `has_kicad_code`
- `semantic_detail`
- `fill_detail`
- `drc_detail`
- `error_message`

其中：

- `semantic_detail` 保存语义评分细节
- `fill_detail` 保存回填后端细节
- `drc_detail` 保存 DRC 后端细节

---

## 命令行参数

主要参数如下：

- `--incomplete-dir`: 不完整 KiCad 文件目录
- `--prediction-dir`: 模型原始回复目录
- `--label-dir`: 标准答案目录
- `--output-dir`: 输出目录
- `--alpha`: 最终得分中 `s1` 的权重
- `--allow-non-kicad`: 允许无 KiCad 代码回复参与评分
- `--fill-placeholder`: 兼容保留字段，当前真实回填后端不依赖该参数
- `--drc-command`: 兼容保留字段，当前默认走内置 DRC backend
- `--drc-timeout-sec`: 兼容保留字段，当前内置 DRC backend 不依赖该参数

---

## 当前状态

当前版本已经完成：

- KiCad 感知的语义相似度评分
- 面向模型输出的目录 batch 输入
- 面向三组 list 的直接函数调用输入
- 真实回填后端接入
- 真实 DRC 后端接入
- 最终得分聚合

后续仍值得继续增强的部分：

- 更细粒度的 DRC 错误权重
- 更强的 KiCad 结构归一化
- batch 级错误分析报告
- 更正式的 benchmark 输入集与回归集

---

## 测试

当前 `eval` 目录下提供了两类测试：

- [test_semantic.py](/H:/Program/MindSpeed-LLM/eval/tests/test_semantic.py): 语义相似度评分测试
- [test_pipeline.py](/H:/Program/MindSpeed-LLM/eval/tests/test_pipeline.py): 整体 pipeline 测试
- [test_pipeline_integration.py](/H:/Program/MindSpeed-LLM/eval/tests/test_pipeline_integration.py): 真实回填 + 真实 DRC 的集成测试

测试使用 Python 标准库 `unittest`，不依赖额外安装 `pytest`。

### 运行语义评分测试

```powershell
$env:PYTHONPATH = "H:\Program\MindSpeed-LLM"
& 'C:\Program Files\AutoClaw\resources\python\python.exe' -m unittest eval.tests.test_semantic -v
```

### 运行整体 Pipeline 测试

```powershell
$env:PYTHONPATH = "H:\Program\MindSpeed-LLM"
& 'C:\Program Files\AutoClaw\resources\python\python.exe' -m unittest eval.tests.test_pipeline -v
```

### 运行真实集成测试

```powershell
$env:PYTHONPATH = "H:\Program\MindSpeed-LLM"
& 'C:\Program Files\AutoClaw\resources\python\python.exe' -m unittest eval.tests.test_pipeline_integration -v
```

这个测试会：

- 使用 `sample_batch` 中的 batch=2 样例
- 使用真实回填后端
- 使用真实 DRC backend
- 打通整条 pipeline
- 重点验证流程能跑通，不强求具体分数

### 一起运行

```powershell
$env:PYTHONPATH = "H:\Program\MindSpeed-LLM"
& 'C:\Program Files\AutoClaw\resources\python\python.exe' -m unittest eval.tests.test_semantic eval.tests.test_pipeline eval.tests.test_pipeline_integration -v
```

### 当前测试覆盖内容

- 语义评分是否同时考虑 KiCad 代码和普通文本
- 要求 KiCad 代码但回复中缺失代码时是否正确置零
- pipeline 是否正确聚合 `s1`、`s2` 和 `final_score`
- 回填失败时是否正确将 `s2` 置零并返回失败状态
- 真实回填与真实 DRC 是否能在 batch=2 样例上打通整条流程
