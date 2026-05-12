from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from tools import pcb_tools


@pytest.fixture(autouse=True)
def _restore_transport_state():
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    prev_session = transport.current_session_id
    prev_modes = dict(transport._session_modes)
    prev_cache = dict(transport._cached_project_data)
    prev_adapter = transport._websocket_adapter
    prev_loop = transport._main_loop
    yield
    transport.current_session_id = prev_session
    transport._session_modes = prev_modes
    transport._cached_project_data = prev_cache
    transport._websocket_adapter = prev_adapter
    transport._main_loop = prev_loop


def _write_step(path: Path, body: str) -> None:
    path.write_text(
        "import pathlib, sys\n"
        "cwd = pathlib.Path.cwd()\n"
        f"{body}\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _pin_csv(component: str) -> str:
    return f"PinNumber,Net\n1,{component}.NET1\n"


def _assert_route_summary(result: str, report: str, routing_path: Path, session_id: str) -> None:
    assert result.startswith("布线完成")
    assert report in result
    assert str(routing_path) in result
    assert pcb_tools._transport.pop_pending_pcb_fields(session_id) == {"routingResult": str(routing_path)}


def test_arc_adapter_e2e_with_fake_router(monkeypatch, tmp_path):
    router_dir = tmp_path / "arc_runtime"
    work_dir = tmp_path / "arc_work"
    router_dir.mkdir()
    work_dir.mkdir()

    _write_step(router_dir / "a.out", "assert sys.argv[1:] == ['layout_input.txt', 'component_input.txt']; (cwd / 'U27_pins.csv').write_text('PinNumber,Net\\n1,U27.NET1\\n', encoding='utf-8'); (cwd / 'layer_input.txt').write_text('layers', encoding='utf-8')")
    _write_step(router_dir / "b.out", "assert sys.argv[1:] == ['layer_input.txt', 'layout_input.txt']; (cwd / 'order_input.txt').read_text(encoding='utf-8')")
    _write_step(router_dir / "c.out", "assert sys.argv[1:] == ['order_input.txt', 'layout_input.txt', 'constrain.txt']; (cwd / 'ARC_output.txt').write_text('arc', encoding='utf-8')")
    _write_step(router_dir / "Turn_QYF.py", "assert sys.argv[1:] == ['layout_input.txt', 'ARC_output.txt', 'routing_input.txt']; (cwd / 'routing_input.txt').write_text('(arc full chain)', encoding='utf-8'); (cwd / 'data.txt').write_text('arc report', encoding='utf-8')")

    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "arc-e2e"
    transport.set_session_mode("arc-e2e", "pcb")
    transport._cached_project_data["arc-e2e"] = '(pcb_data (component (name "U27") (package "BGA")))'

    monkeypatch.setenv("ROUTER_WORK_DIR", str(work_dir))
    monkeypatch.setenv("ROUTER_ARC_DIR", str(router_dir))

    result = pcb_tools.route_bga(json.dumps({
        "routerType": "arc",
        "selectedBGA": "U27",
        "orderLines": [{"net": "GND", "layer": "SIG03", "order": 1}],
        "constraints": {"LineWidth": 3, "LineSpacing": 4.5},
    }))

    _assert_route_summary(result, "arc report", work_dir / "routing_input.txt", "arc-e2e")
    assert (work_dir / "component_input.txt").read_text(encoding="utf-8") == "U27\n"
    assert (work_dir / "constrain.txt").read_text(encoding="utf-8") == "LineWidth:3\nLineSpacing:4.5\n"


def test_135_adapter_e2e_with_fake_router(monkeypatch, tmp_path):
    router_dir = tmp_path / "runtime135"
    work_dir = tmp_path / "work135"
    router_dir.mkdir()
    work_dir.mkdir()

    _write_step(router_dir / "d.out", "assert sys.argv[1:] == ['layout_input.txt', 'component_input.txt']; (cwd / 'U22_pins.csv').write_text('PinNumber,Net\\n1,U22.NET1\\n', encoding='utf-8'); (cwd / 'net_list.txt').write_text('U22.NET1; layer; 4\\n', encoding='utf-8')")
    _write_step(router_dir / "e.out", "assert sys.argv[1:] == ['net_list.txt', 'layout_input.txt']; (cwd / 'order_out.txt').write_text('order', encoding='utf-8')")
    _write_step(router_dir / "f.out", "assert sys.argv[1:] == ['order_out.txt', 'layout_input.txt']; (cwd / 'line.in').write_text('line in', encoding='utf-8'); (cwd / 'line.out').write_text('TOP!LINE!0!NET1!1!2!3!4!4\\n', encoding='utf-8')")
    _write_step(router_dir / "Turn_135_QYF.py", "assert sys.argv[1:] == ['layout_input.txt', 'line.out', 'routing_input.txt']; (cwd / 'routing_input.txt').write_text('(135 full chain)', encoding='utf-8'); (cwd / 'data.txt').write_text('135 report', encoding='utf-8')")

    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "135-e2e"
    transport.set_session_mode("135-e2e", "pcb")
    transport._cached_project_data["135-e2e"] = '(pcb_data (component (name "U22") (package "BGA")))'

    monkeypatch.setenv("ROUTER_WORK_DIR", str(work_dir))
    monkeypatch.setenv("ROUTER_135_DIR", str(router_dir))

    result = pcb_tools.route_bga(json.dumps({
        "routerType": "135",
        "selectedBGA": "U22",
        "orderLines": [{"net": "VCC", "layer": "SIG04", "order": 2}],
    }))

    _assert_route_summary(result, "135 report", work_dir / "routing_input.txt", "135-e2e")
    assert (work_dir / "component_input.txt").read_text(encoding="utf-8") == "U22\n"
    assert (work_dir / "order_input.txt").read_text(encoding="utf-8") == "VCC SIG04 2\n\nU22"


def test_router_selection_arc_135_full_chain(monkeypatch, tmp_path):
    arc_work = tmp_path / "arc_work"
    work135 = tmp_path / "work135"
    arc_runtime = tmp_path / "arc_runtime"
    runtime135 = tmp_path / "runtime135"
    for path in (arc_work, work135, arc_runtime, runtime135):
        path.mkdir()

    for name, body in {
        "a.out": "assert sys.argv[1:] == ['layout_input.txt', 'component_input.txt']; (cwd / 'U1_pins.csv').write_text('PinNumber,Net\\n1,U1.NET1\\n', encoding='utf-8'); (cwd / 'layer_input.txt').write_text('layers', encoding='utf-8')",
        "b.out": "assert sys.argv[1:] == ['layer_input.txt', 'layout_input.txt']; (cwd / 'order_input.txt').read_text(encoding='utf-8')",
        "c.out": "assert sys.argv[1:] == ['order_input.txt', 'layout_input.txt', 'constrain.txt']; (cwd / 'ARC_output.txt').write_text('arc', encoding='utf-8')",
        "Turn_QYF.py": "assert sys.argv[1:] == ['layout_input.txt', 'ARC_output.txt', 'routing_input.txt']; (cwd / 'routing_input.txt').write_text('(arc selected)', encoding='utf-8'); (cwd / 'data.txt').write_text('arc report', encoding='utf-8')",
    }.items():
        _write_step(arc_runtime / name, body)
    for name, body in {
        "d.out": "assert sys.argv[1:] == ['layout_input.txt', 'component_input.txt']; (cwd / 'U1_pins.csv').write_text('PinNumber,Net\\n1,U1.NET1\\n', encoding='utf-8'); (cwd / 'net_list.txt').write_text('U1.NET1; layer; 4\\n', encoding='utf-8')",
        "e.out": "assert sys.argv[1:] == ['net_list.txt', 'layout_input.txt']; (cwd / 'order_out.txt').write_text('order', encoding='utf-8')",
        "f.out": "assert sys.argv[1:] == ['order_out.txt', 'layout_input.txt']; (cwd / 'line.in').write_text('line in', encoding='utf-8'); (cwd / 'line.out').write_text('TOP!LINE!0!NET1!1!2!3!4!4\\n', encoding='utf-8')",
        "Turn_135_QYF.py": "assert sys.argv[1:] == ['layout_input.txt', 'line.out', 'routing_input.txt']; (cwd / 'routing_input.txt').write_text('(135 selected)', encoding='utf-8'); (cwd / 'data.txt').write_text('135 report', encoding='utf-8')",
    }.items():
        _write_step(runtime135 / name, body)

    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    base_payload = {
        "selectedBGA": "U1",
        "orderLines": [{"net": "NET1", "layer": "SIG03", "order": 1}],
        "constraints": {"LineWidth": 3, "LineSpacing": 4.5},
    }

    cases = [
        ("arc-selection", arc_work, {"routerType": "arc"}, "(arc selected)", "arc report", {"ROUTER_ARC_DIR": str(arc_runtime)}),
        ("135-selection", work135, {"routerType": "135"}, "(135 selected)", "135 report", {"ROUTER_135_DIR": str(runtime135)}),
    ]

    for session_id, work_dir, override, expected_result, expected_report, extra_env in cases:
        transport.current_session_id = session_id
        transport.set_session_mode(session_id, "pcb")
        transport._cached_project_data[session_id] = '(pcb_data (component (name "U1") (package "BGA")))'
        monkeypatch.setenv("ROUTER_WORK_DIR", str(work_dir))
        for key, value in extra_env.items():
            monkeypatch.setenv(key, value)

        result = pcb_tools.route_bga(json.dumps({**base_payload, **override}))
        _assert_route_summary(result, expected_report, work_dir / "routing_input.txt", session_id)
        assert (work_dir / "order_input.txt").read_text(encoding="utf-8") == "NET1 SIG03 1\n\nU1"
