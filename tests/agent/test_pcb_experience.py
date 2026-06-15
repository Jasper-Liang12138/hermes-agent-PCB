from __future__ import annotations

from agent.swsd.experience.policies import alias_for_target, minimal_reroute_final, require_structured_final
from agent.swsd.experience.resolver import PCBExperienceResolver
from agent.swsd.experience.schema import PCBContextHints, PCBExperienceHint


def test_alias_for_target_prefers_seed_alias_when_candidate_exists():
    hints = PCBContextHints(
        session_id="s1",
        model_hints=(
            PCBExperienceHint(
                layer="user_project_model",
                key="alias:U27",
                value="U5",
                confidence=0.65,
            ),
        ),
    )

    assert alias_for_target(hints, "U27", ["U5"]) == "U5"


def test_alias_for_target_single_candidate_fallback():
    hints = PCBContextHints(session_id="s1")

    assert alias_for_target(hints, "U27", ["U5"]) == "U5"
    assert alias_for_target(hints, "U27", ["U5", "U6"]) == ""


def test_minimal_reroute_final_fills_required_fields():
    fields = minimal_reroute_final({"rerouteResult": {"routedLayoutTxtFilePath": "x.txt", "drcPassed": True}})

    assert fields["rerouteResult"]["status"] == "drc_passed_import_pending"
    assert fields["rerouteResult"]["drcPassed"] is True
    assert fields["checkReport"]["warnings"]
    assert fields["explanation"]


def test_resolver_provides_seed_model_hints():
    hints = PCBExperienceResolver().resolve(session_id="s1", query="U27 fanout", workflow_state="select_bga")

    assert hints.experience_used
    assert require_structured_final(hints) is True
    assert hints.hint_value("alias:U27") == "U5"
