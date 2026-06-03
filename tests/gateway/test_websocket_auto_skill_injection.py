from gateway.config import Platform
from gateway.run import _should_inject_auto_skill_for_turn
from gateway.session import SessionSource


def _source(platform: Platform) -> SessionSource:
    return SessionSource(
        platform=platform,
        chat_id="session-1",
        user_id="session-1",
        chat_type="dm",
    )


def test_websocket_reroute_auto_skill_injects_for_existing_session():
    assert _should_inject_auto_skill_for_turn(
        is_new_session=False,
        auto_skill="hardware/pcb-reroute",
        source=_source(Platform.WEBSOCKET),
    )


def test_websocket_pcb_auto_skill_injects_for_existing_session():
    assert _should_inject_auto_skill_for_turn(
        is_new_session=False,
        auto_skill="hardware/pcb-intelligence",
        source=_source(Platform.WEBSOCKET),
    )
    assert _should_inject_auto_skill_for_turn(
        is_new_session=True,
        auto_skill="hardware/pcb-intelligence",
        source=_source(Platform.WEBSOCKET),
    )


def test_non_websocket_auto_skill_still_waits_for_new_session():
    assert not _should_inject_auto_skill_for_turn(
        is_new_session=False,
        auto_skill="hardware/pcb-reroute",
        source=_source(Platform.TELEGRAM),
    )
