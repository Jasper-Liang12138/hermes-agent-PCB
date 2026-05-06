# PCB Reroute DRC Mock Flow Log

本文件记录一次独立的 mock WebSocket 闭环测试。完整原始 JSONL 在：

```text
test_client/reroute_drc_mock_flow.jsonl
```

本次记录不是手写样例，而是由下面命令真实运行后生成：

```bash
cd /mnt/e/Program/hermes-agent-PCB
python3 test_client/reroute_drc_flow_harness.py \
  --log-file test_client/reroute_drc_mock_flow.jsonl \
  --expect-drc-iterations 2 \
  --timeout 120
```

## 记录索引

本次 JSONL 共 14 条：

```text
1. client send     message
2. server recv     user_message
3. client recv     message
4. client recv     tool-calls
5. client send     tool-results
6. server internal drop_net_result
7. server internal reroute_generation_prompt
8. server internal mock_model_generate
9. server internal reroute_generation_prompt
10. server internal mock_model_generate
11. server internal drc_attempts_parsed
12. server internal reroute_result
13. server send    final_message_fields
14. client recv    message
```

新增的关键记录是：

- `reroute_generation_prompt`：记录真实代码 helper `_build_reroute_generation_prompts(...)` 组成的 system/user prompt；
- `drc_attempts_parsed`：记录每轮 DRC 原始返回 `rawDrcResult`，以及 Hermes 解析后加入下一轮 prompt 的 `parsedFailureSummary`。

## 测试结论

```json
{
  "drcPassed": true,
  "drcIterations": 2,
  "routedBoardDataFilePath": "/mnt/e/Program/hermes-agent-PCB/test_client/.hermes_reroute/reroute-drc-flow-mock_net13_iter2.kicad_pcb"
}
```

第 1 轮是真实 DRC 失败，不是 fill-in 失败：

- patch 成功回填；
- `evaluate_drc_score(..., check_mode="hard")` 返回 `ok=true`；
- hard DRC 返回 `pass=false`；
- 失败规则为 `HR_DRC_SEGMENT_CROSSING`；
- Hermes 将 hard issue 统计与 issue message 摘要成 `failureSummary`；
- 第 2 轮 prompt 的 “上一轮 DRC 失败反馈” 中包含该摘要；
- 第 2 轮 hard DRC 返回 `pass=true`。

## 第 1 轮模型输出

来源：JSONL 第 8 条 `server internal mock_model_generate`。

```json
{
  "feedback": [],
  "patch": "(segment (start 100 100) (end 110 100) (width 0.2) (layer F.Cu) (net 13))\n(segment (start 105 95) (end 105 105) (width 0.2) (layer F.Cu) (net 17))"
}
```

这个 patch 是合法 KiCad 对象，回填后会制造 net13 与 net17 在 `F.Cu` 上的交叉。

## 第 1 轮真实 DRC 原始结果

来源：JSONL 第 11 条 `server internal drc_attempts_parsed[0].rawDrcResult`。

```json
{
  "ok": true,
  "input": {
    "board_path": "/mnt/e/Program/hermes-agent-PCB/test_client/.hermes_reroute/reroute-drc-flow-mock_net13_iter1.kicad_pcb",
    "check_mode": "hard"
  },
  "score_name": "drc_hard_score",
  "score": 80.0,
  "pass": false,
  "details": {
    "hard_penalty": 4.0,
    "hard_issue_count": 1,
    "hard_rule_counts": {
      "HR_DRC_SEGMENT_CROSSING": 1
    }
  },
  "artifacts": {
    "issues": [
      {
        "issue_id": "HR_DRC_SEGMENT_CROSSING_SEG_0_net13|net17_F.Cu",
        "rule": "HR_DRC_SEGMENT_CROSSING",
        "severity": "ERROR",
        "message": "Segments SEG_0 (net13) and SEG_1 (net17) cross or overlap on layer F.Cu.",
        "obj1": "SEG_0",
        "obj2": "SEG_1",
        "net": "net13|net17",
        "layer": "F.Cu",
        "x": 105.0,
        "y": 100.0,
        "category": "drc",
        "suggestion": "Reroute one of the segments so different nets do not cross on the same copper layer.",
        "component": "",
        "pad_id": "",
        "extra": {
          "seg1_net": "net13",
          "seg2_net": "net17",
          "seg1_start": [100.0, 100.0],
          "seg1_end": [110.0, 100.0],
          "seg2_start": [105.0, 95.0],
          "seg2_end": [105.0, 105.0],
          "cell": [21, 20]
        }
      }
    ]
  },
  "error": null
}
```

## DRC 结果如何被解析

解析逻辑在：

```text
tools/pcb_reroute_drc.py::_summarize_drc_failure
tools/pcb_tools.py::_run_reroute_drc_iterations
```

真实解析输出如下，来源为 JSONL 第 11 条 `parsedFailureSummary`：

```text
hard_issue_count=1; hard_rule_counts={"HR_DRC_SEGMENT_CROSSING": 1}; issues=[{"rule": "HR_DRC_SEGMENT_CROSSING", "message": "Segments SEG_0 (net13) and SEG_1 (net17) cross or overlap on layer F.Cu.", "severity": "ERROR"}]; fill_detail={"segments_count": 1, "vias_count": 0, "other_lines_count": 0}
```

解析过程：

1. `validate_kicad_patch_with_drc(...)` 将模型输出回填到原始 KiCad board 副本；
2. 调用 `evaluate_drc_score(filled_path, check_mode="hard")`；
3. 判断 `ok=true` 且 `pass=true` 才算通过；
4. 第 1 轮 `pass=false`，进入 `_summarize_drc_failure(...)`；
5. 摘取 `details.hard_issue_count`；
6. 摘取 `details.hard_rule_counts`；
7. 摘取 `artifacts.issues` 的前 5 条中的 `rule/message/severity`；
8. 附加 fill-in 统计 `segments_count/vias_count/other_lines_count`；
9. 将这个字符串 append 到 `feedback`，传给下一轮 `_generate_reroute_with_model(..., drc_feedback=feedback)`。

## 第 2 轮真实 Prompt

来源：JSONL 第 9 条 `server internal reroute_generation_prompt.userPrompt`。

这里摘录的是实际组成的 user prompt 前半部分，尤其是 DRC feedback 段：

```text
请生成如下 JSON 结构：
{
  "rerouteResult": {"type": "local_reroute", "mode": "selected_nets_after_drop", "selectedNets": [], "operations": []},
  "kicadPatch": "(segment ... )\n(via ...)",
  "checkReport": {"passed": true, "checks": []},
  "explanation": "简短中文说明"
}

selectedNets:
[
  "net13"
]

constraints:
{}

droppedObjects:
[
  {
    "net": "net13",
    "mockRemoved": true
  }
]

localContext:
{
  "source": "reroute_mock_client",
  "boardDataFilePath": "/mnt/e/Program/hermes-agent-PCB/test_client/mock_reroute_board.kicad_pcb",
  "note": "MOCK 客户端暂时把原始 KiCad 版图文件作为拆线后版图返回"
}

originalBoardDataFilePath: /mnt/e/Program/hermes-agent-PCB/test_client/mock_reroute_board.kicad_pcb

droppedBoardDataFilePath: /mnt/e/Program/hermes-agent-PCB/test_client/mock_reroute_board.kicad_pcb

chunkStats:
{
  "topLevelObjectCount": 12,
  "globalObjectCount": 7,
  "componentObjectCount": 2,
  "routingObjectCount": 0,
  "otherObjectCount": 3,
  "chunkCount": 3,
  "contextChars": 1305,
  "contextTokens": 0,
  "maxContextChars": 60000,
  "maxContextTokens": 14000,
  "maxChunks": 8,
  "chunkChars": 12000,
  "chunkTokens": 2048
}

上一轮 DRC 失败反馈（如有）:
- hard_issue_count=1; hard_rule_counts={"HR_DRC_SEGMENT_CROSSING": 1}; issues=[{"rule": "HR_DRC_SEGMENT_CROSSING", "message": "Segments SEG_0 (net13) and SEG_1 (net17) cross or overlap on layer F.Cu.", "severity": "ERROR"}]; fill_detail={"segments_count": 1, "vias_count": 0, "other_lines_count": 0}

拆线后版图分块上下文:
上下文已按 KiCad 顶层对象分块；这些内容来自 *_incomplete.kicad_pcb，不包含 plan.txt 的补全代码。
```

这说明下一轮 prompt 并不是只告诉模型“DRC 失败”，而是把具体 hard rule、交叉描述、严重级别和 fill-in 统计都塞进了 prompt。

完整 prompt 包括后续 KiCad 分块上下文，请直接查看：

```text
test_client/reroute_drc_mock_flow.jsonl
label = "reroute_generation_prompt"
payload.iteration = 2
payload.userPrompt
```

## 第 2 轮模型输出

来源：JSONL 第 10 条 `server internal mock_model_generate`。

```json
{
  "feedback": [
    "hard_issue_count=1; hard_rule_counts={\"HR_DRC_SEGMENT_CROSSING\": 1}; issues=[{\"rule\": \"HR_DRC_SEGMENT_CROSSING\", \"message\": \"Segments SEG_0 (net13) and SEG_1 (net17) cross or overlap on layer F.Cu.\", \"severity\": \"ERROR\"}]; fill_detail={\"segments_count\": 1, \"vias_count\": 0, \"other_lines_count\": 0}"
  ],
  "patch": "(segment (start 100 100) (end 110 100) (width 0.2) (layer \"F.Cu\") (net 13))"
}
```

## 第 2 轮真实 DRC 原始结果

来源：JSONL 第 11 条 `server internal drc_attempts_parsed[1].rawDrcResult`。

```json
{
  "ok": true,
  "input": {
    "board_path": "/mnt/e/Program/hermes-agent-PCB/test_client/.hermes_reroute/reroute-drc-flow-mock_net13_iter2.kicad_pcb",
    "check_mode": "hard"
  },
  "score_name": "drc_hard_score",
  "score": 100.0,
  "pass": true,
  "details": {
    "hard_penalty": 0.0,
    "hard_issue_count": 0,
    "hard_rule_counts": {}
  },
  "artifacts": {
    "issues": []
  },
  "error": null
}
```

最终返回路径：

```text
/mnt/e/Program/hermes-agent-PCB/test_client/.hermes_reroute/reroute-drc-flow-mock_net13_iter2.kicad_pcb
```

## 说明

本次仍未调用真实远端 LLM。原因是本测试要确定性验证 DRC 迭代机制，而不是验证模型质量。

但以下部分均为真实执行：

- WebSocket tool-call；
- mock 客户端工具结果；
- `drop_net` session 缓存；
- `reroute` 迭代控制；
- KiCad patch 回填；
- AI-PCB-Eval `evaluate_drc_score(..., check_mode="hard")`；
- DRC 原始返回解析；
- 下一轮 prompt 组装。
