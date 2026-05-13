# PCB Selected-Trace Reroute Test Report

Date: 2026-05-10

## Scope

Validate the new local rip-up/reroute flow:

1. `getSelectedElements` with `PFindType="TRACES"`
2. Reject empty or over-40 selections
3. `deleteTracesById` with selected trace ids
4. `getProjectData` after successful deletion
5. `reroute` using cached `selectedTraceIds`
6. WebSocket mock client round trip with JSONL interaction log

## Environment Notes

- Windows Python was checked first but lacked `pytest` and `aiohttp`.
- WSL system Python is used for test execution.
- WSL command access requires elevated execution in this Codex session.

## Results

### 1. WSL Environment Check

Command:

```bash
cd /mnt/e/Program/hermes-agent-PCB
which python3
python3 -m pytest --version
```

Result:

```text
/usr/bin/python3
pytest 9.0.3
```

### 2. Syntax Check

Command:

```bash
python3 -m py_compile tools/pcb_tools.py test_client/reroute_mock_client.py test_client/reroute_drc_flow_harness.py
```

Result: passed.

### 3. Tool-Layer Tests

Command:

```bash
python3 -m pytest tests/tools/test_pcb_tools_mode_guard.py -q
```

Result:

```text
20 passed, 12 warnings in 20.22s
```

Covered:

- `drop_net` calls `getSelectedElements` with `{"PFindType": "TRACES"}`.
- `drop_net` calls `deleteTracesById` with selected trace ids.
- `drop_net` calls `getProjectData` only after successful deletion.
- More than 40 selected trace ids stop before deletion.
- Non-JSON Python-list strings such as `['2386476278']` are rejected by strict JSON parsing.
- `reroute` accepts cached `selectedTraceIds` without requiring `selectedNets`.

Output log:

```text
test_client/reroute_selected_trace_pytest_tools.log
```

### 4. WebSocket Mock Closed Loop

Command:

```bash
python3 test_client/reroute_drc_flow_harness.py \
  --log-file test_client/reroute_selected_trace_flow_review.jsonl \
  --timeout 120 \
  --connect-retries 20 \
  --connect-retry-delay 0.2
```

Result: passed, exit code 0.

Observed interaction order:

```text
1. client send message
2. server recv user_message
3. client recv message
4. client recv tool-calls getSelectedElements
5. client send tool-results
6. client recv tool-calls deleteTracesById
7. client send tool-results
8. client recv tool-calls getProjectData
9. client send tool-results
10. server internal drop_net_result
11. server internal reroute_generation_prompt
12. server internal mock_model_generate
13. server internal reroute_generation_prompt
14. server internal mock_model_generate
15. server internal drc_attempts_parsed
16. server internal reroute_result
17. server send final_message_fields
18. client recv message
```

Final result highlights:

```text
mode = selected_traces_after_delete
selectedTraceIds = ["2386476278", "3424247826"]
drcPassed = true
drcIterations = 2
```

Output log:

```text
test_client/reroute_selected_trace_harness.log
```

### 5. Toolset And WebSocket Regression

Command:

```bash
python3 -m pytest tests/test_toolsets.py tests/gateway/test_websocket_pcb_flow.py -q
```

Result:

```text
53 passed, 12 warnings in 13.59s
```

Output log:

```text
test_client/reroute_selected_trace_pytest_gateway_toolsets.log
```

## Interaction Logs

Planned JSONL log:

```text
test_client/reroute_selected_trace_flow_review.jsonl
```

The JSONL file contains the full WebSocket request/response and server internal events for review.
