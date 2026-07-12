from pathlib import Path
from pcb_agent_langgraph.agent import _apply_result_decision
from pcb_agent_langgraph.tools.registry import build_tool_registry
from pcb_agent_langgraph.utils.config import AppConfig


def test_accept_marks_result_complete_without_restore():
    cache = _apply_result_decision({"importLinesResult": {"status": "ok"}}, "fanout", "ACCEPT", "")
    assert cache["resultAccepted"] is True
    assert "pendingRestore" not in cache


def test_retry_records_restore_without_clearing_imported_result():
    original = {"fanout_routeResult": {"status": "ok"}, "importLinesResult": {"status": "ok"}}
    cache = _apply_result_decision(original, "fanout", "RETRY", "SETTING_PARAMS")
    assert cache["pendingRestore"]["reason"] == "retry_params"
    assert cache["importLinesResult"] == {"status": "ok"}


def test_restore_frontend_tools_are_registered():
    registry = build_tool_registry(AppConfig(root=Path.cwd()))
    assert "restoreFanoutSnapshot" in registry
    assert "restoreRerouteSnapshot" in registry


def test_restore_failure_preserves_pending_decision():
    from pcb_agent_langgraph.graph.nodes import _update_cache_from_tool

    cache = {"pendingRestore": {"workflow": "reroute", "reason": "discard"}, "importLinesResult": {"status": "ok"}}
    _update_cache_from_tool(cache, "restoreRerouteSnapshot", {"success": False, "status": "failed"})
    assert "pendingRestore" in cache
    assert "importLinesResult" in cache


def test_restore_success_applies_stage_reset():
    from pcb_agent_langgraph.graph.nodes import _update_cache_from_tool

    cache = {
        "pendingRestore": {"workflow": "fanout", "reason": "retry_params"},
        "fanoutEntities": {"selectedBGA": "U5"},
        "fanoutParams": {"selectedBGA": "U5"},
        "layerAssignResult": {"status": "ok"},
        "fanout_routeResult": {"status": "ok"},
        "importLinesResult": {"status": "ok"},
    }
    _update_cache_from_tool(cache, "restoreFanoutSnapshot", {"success": True, "status": "ok"})
    assert cache["fanoutEntities"]["selectedBGA"] == "U5"
    assert "fanoutParams" not in cache
    assert "fanout_routeResult" not in cache
    assert "importLinesResult" not in cache
    assert "pendingRestore" not in cache


def test_all_fanout_retry_stages_map_to_restore_reasons():
    expected = {
        "CHOOSING_BGA": "retry_choose_bga",
        "SETTING_PARAMS": "retry_params",
        "ROUTING": "retry_routing",
    }
    for stage, reason in expected.items():
        cache = _apply_result_decision({"importLinesResult": {"status": "ok"}}, "fanout", "RETRY", stage)
        assert cache["pendingRestore"]["reason"] == reason


def test_cancel_discard_and_rerun_map_to_restore_reasons():
    fanout = _apply_result_decision({"importLinesResult": {"status": "ok"}}, "fanout", "CANCEL")
    discard = _apply_result_decision({"importLinesResult": {"status": "ok"}}, "reroute", "DISCARD")
    rerun = _apply_result_decision({"importLinesResult": {"status": "ok"}}, "reroute", "RERUN")
    assert fanout["pendingRestore"]["reason"] == "cancel"
    assert discard["pendingRestore"]["reason"] == "discard"
    assert rerun["pendingRestore"]["reason"] == "rerun"


def test_fanout_entry_payload_maps_v21_fields():
    from pcb_agent_langgraph.agent import PCBLangGraphAgent

    cache = PCBLangGraphAgent._cache_for_entry_payload({}, {"bga": "BGA1", "algorithm": "135", "routingParamType": "RL"})
    assert cache["fanoutEntities"]["selectedBGA"] == "BGA1"
    assert cache["fanoutParams"]["routerType"] == "rule_135"
    assert cache["fanoutParams"]["routingParamType"] == "RL"


def test_unimplemented_restore_tool_preserves_pending_decision():
    from pcb_agent_langgraph.graph.nodes import _update_cache_from_tool

    cache = {"pendingRestore": {"workflow": "fanout", "reason": "cancel"}, "importLinesResult": {"status": "ok"}}
    _update_cache_from_tool(cache, "restoreFanoutSnapshot", {"status": "unavailable", "reason": "not implemented"})
    assert cache["pendingRestore"]["reason"] == "cancel"
    assert cache["importLinesResult"]["status"] == "ok"
