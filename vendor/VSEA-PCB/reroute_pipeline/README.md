# reroute_pipeline

Portable VSEA-PCB reroute sub-agent for external LangGraph workflows.

The package accepts a KiCad board context plus an explicit missing-route prompt,
generates KiCad `(segment ...)` / `(via ...)` routing objects, fills them back
into a complete `.kicad_pcb`, and verifies the result with the new agent hard
DRC package. A complete board is returned only when hard DRC passes.

## Minimal Usage

```python
from reroute_pipeline import RerouteAgent, RerouteInput

agent = RerouteAgent.from_env()
result = agent.run(
    RerouteInput(
        task_id="S0001",
        context_kicad=context_kicad_text,
        routing_task_prompt=missing_route_prompt,
        output_dir="outputs/reroute_pipeline/S0001",
    )
)

if result.success:
    print(result.completed_kicad_path)
else:
    print(result.status, result.error)
```

## LangGraph Node

```python
from reroute_pipeline.langgraph_node import vsea_reroute_node

state = vsea_reroute_node(
    {
        "task_id": "S0001",
        "context_kicad": context_kicad_text,
        "routing_task_prompt": missing_route_prompt,
        "output_dir": "outputs/reroute_pipeline/S0001",
    }
)
```

Successful states include `completed_kicad`, `completed_kicad_path`, and
`vsea_reroute`. Failed states include `vsea_reroute` and `error`.

## Required Environment

```text
REROUTE_LLM_API_KEY      OpenAI-compatible API key
REROUTE_LLM_BASE_URL     OpenAI-compatible base URL
REROUTE_MODEL            Model id
REROUTE_AI_PCB_EVAL_PATH Path to AI-PCB-Eval
REROUTE_DRC_AGENT_PACKAGE Path to external_drc/DRC_0623_v2/agent_package
REROUTE_SKILL_BANK_PATH  Optional JSONL skill bank
REROUTE_OUTPUT_DIR       Optional default output directory
REROUTE_TIMEOUT_SECONDS  Optional LLM/DRC timeout
REROUTE_ENABLE_DEBUG     Optional detailed internal debug, default off
```

Fallbacks are supported for `LLM_API_KEY`, `LLM_BASE_URL`, `MODEL_ID`,
`VSEA_PCB_AGENT_DRC_TOOL`, and `VSEA_PCB_AGENT_DRC_TARGET_BGA`.

## Frozen Skill Bank

The portable package includes a prebuilt new-DRC skill bank at:

```text
reroute_pipeline/assets/skill_bank.jsonl
```

Set `REROUTE_SKILL_BANK_PATH` to use a different frozen bank:

```bash
export REROUTE_SKILL_BANK_PATH=/path/to/skill_bank.jsonl
```

If no override is provided, the package uses `assets/skill_bank.jsonl`. The
runtime treats the bank as read-only and only retrieves cards for repair
prompts. The package does not include any bank construction or update code.

## Output Files

For each task, the agent writes:

```text
raw_patch/<task_id>.kicad_patch
filled_boards/<task_id>.kicad_pcb
drc_reports/<task_id>.json
debug/<task_id>.json
```

Debug files contain summaries by default. Full prompts, board context, and raw
LLM responses are not written unless private debug switches are explicitly
enabled for local troubleshooting.

The downstream format conversion stage should consume only
`completed_kicad_path` or `completed_kicad` when `success` is true.
