"""Seeded PCB user/project model."""

from __future__ import annotations

from agent.swsd.experience.schema import PCBExperienceHint, PCBProjectModel


DEFAULT_PROJECT_MODEL = PCBProjectModel(
    interaction_preference={
        "language": "zh",
        "verbosity": "concise",
        "executionBias": "prefer_execute_when_explicit",
    },
    pcb_preference={
        "defaultRouterType": "arc",
        "defaultLayerOrderModule": "RL",
        "requireStructuredFinalBody": True,
        "rerouteFinalFirst": True,
        "maxImportWaitBeforeFinalSec": 15,
    },
    project_aliases={
        "U27": "U5",
    },
)


def load_project_model(project_id: str = "") -> PCBProjectModel:
    """Return the seeded project model.

    v1 intentionally keeps this deterministic and local. Future versions can
    overlay profile/project-specific conclusions from MEMORY.md or Honcho.
    """

    return DEFAULT_PROJECT_MODEL


def model_to_hints(model: PCBProjectModel) -> list[PCBExperienceHint]:
    hints: list[PCBExperienceHint] = [
        PCBExperienceHint(
            layer="user_project_model",
            key="interactionPreference",
            value=model.interaction_preference,
            source="seed",
            confidence=0.85,
            reason="Seeded PCB delivery preference.",
        ),
        PCBExperienceHint(
            layer="user_project_model",
            key="pcbPreference",
            value=model.pcb_preference,
            source="seed",
            confidence=0.9,
            reason="Seeded PCB workflow output and recovery defaults.",
        ),
    ]
    for alias, canonical in model.project_aliases.items():
        hints.append(
            PCBExperienceHint(
                layer="user_project_model",
                key=f"alias:{alias}",
                value=canonical,
                source="seed",
                confidence=0.65,
                reason="Project alias from seed model; use only when compatible with detected candidates.",
            )
        )
    return hints
