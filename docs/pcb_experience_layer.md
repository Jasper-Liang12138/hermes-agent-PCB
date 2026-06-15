# PCB Experience Layer

This document describes the v1 "grow with you" loop for the PCB agent.

## Runtime Loop

The experience layer is deliberately a sidecar to the deterministic SWSD workflow:

```text
Workflow state/events + seeded project model + PCB skills
  -> PCBExperienceResolver
  -> PCBContextHints
  -> SWSD routing, workflow recovery, final body shaping
```

The hints are not allowed to bypass SWSD hard constraints or explicit user input.
They only provide recovery hints, defaults, and procedural recovery rules.

## Three Layers And How They Are Used

- Memory Facts: recent workflow facts recorded in `workflow_events` with
  `event_type="experience"`. They are used for state recovery and failure
  avoidance, such as restoring injected `selection`/`fanoutParams` or remembering
  that a requested BGA was resolved to a detected candidate.
- User/Project Model: seeded defaults in `agent/swsd/experience/model.py`.
  The v1 model captures Chinese concise interaction, structured final body
  requirements, conservative execution bias, and known project aliases such as
  `U27 -> U5`.
- Procedural Skills: retrieved from PCB skill files through
  `agent/swsd/experience/skill_bank.py` and `agent/swsd/skill_grounding.py`.
  They influence SWSD decisions as grounding and recovery guidance, not as direct
  tool execution.

## Code Structure

```text
agent/swsd/experience/
  schema.py      # event, hint, model, and context dataclasses
  recorder.py    # records PCB facts into workflow_events
  resolver.py    # builds PCBContextHints per PCB turn
  model.py       # seeded user/project model
  skill_bank.py  # PCB skill discovery
  policies.py    # safe hint consumption helpers
  distiller.py   # conservative trace-distillation hooks
```

Runtime integration points:

- `gateway/platforms/websocket.py` recovers inbound structured body fields,
  records experience events, applies target alias/single-candidate recovery, and
  fills minimal reroute final fields.
- `run_agent.py` appends `PCB Experience Hints` beside SWSD workflow context.
- `agent/swsd/skill_grounding.py` now retrieves from the PCB skill bank instead
  of only two fixed bundled skills.

## Delivery And Startup

Delivery mode is "seeded delivery + online growth":

- Day 0: seeded model and existing PCB skills are active; known lab failures can
  already be avoided.
- Week 1: workflow facts accumulate and improve state recovery/reentry.
- Week 2+: repeated successful traces can be distilled into project-specific
  procedural skills.

The v1 implementation is conservative: no vector database, no schema migration,
and no automatic skill patching in the hot path. Future automatic distillation
should run asynchronously and write reviewable skill patches only.

## Maintenance Rules

- Do not store raw board files or large artifacts in experience events.
- Keep project facts, user/project defaults, and procedural skills separate.
- Every automatic hint must include source and confidence.
- Low-confidence hints may recover defaults only when compatible with detected
  candidates; they must not override explicit user choices.
- If the experience layer causes trouble, disable the resolver path and SWSD
  continues to run from deterministic state.
