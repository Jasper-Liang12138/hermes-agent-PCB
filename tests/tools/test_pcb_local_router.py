from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import pcb_local_router
from tools import pcb_tools


def _fake_pcbrouter(path: Path) -> None:
    path.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        "assert len(args) == 3, args\n"
        "board, flag, csv_path = args\n"
        "assert flag == '-bga_local_route', args\n"
        "assert Path(board).is_file(), board\n"
        "assert Path(csv_path).is_file(), csv_path\n"
        "Path('output_routed').mkdir(exist_ok=True)\n"
        "Path('output_routed/output.fake.kicad_pcb').write_text(\n"
        "    Path(board).read_text(encoding='utf-8') + '\\n; fake local routed\\n',\n"
        "    encoding='utf-8',\n"
        ")\n"
        "Path('output_routed/output.fake.csv').write_text(\n"
        "    'wirelength,via,bend,has_drv,drv_total,open_nets\\n'\n"
        "    '0.00000,0,0,no,0,\"count=0,none\"\\n',\n"
        "    encoding='utf-8',\n"
        ")\n"
        "print('fake pcbrouter ok')\n",
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _restore_transport_state(monkeypatch):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    prev_session = transport.current_session_id
    prev_modes = dict(transport._session_modes)
    prev_cache = dict(transport._cached_project_data)
    prev_paths = dict(transport._cached_project_data_paths)
    prev_pending_fields = dict(transport._pending_pcb_fields)
    prev_adapter = transport._websocket_adapter
    prev_loop = transport._main_loop
    monkeypatch.delenv("PCBROUTER_BIN", raising=False)
    monkeypatch.delenv("PCB_ROUTER_BIN", raising=False)
    monkeypatch.delenv("PCB_LOCAL_ROUTER_BIN", raising=False)
    yield
    transport.current_session_id = prev_session
    transport._session_modes = prev_modes
    transport._cached_project_data = prev_cache
    transport._cached_project_data_paths = prev_paths
    transport._pending_pcb_fields = prev_pending_fields
    transport._websocket_adapter = prev_adapter
    transport._main_loop = prev_loop


def test_write_local_route_csv_maps_known_board_layers(tmp_path):
    project_data = (
        '(kicad_pcb\n'
        '  (layers\n'
        '    (0 "F.Cu" signal "Top")\n'
        '    (6 "In2.Cu" signal "Art03")\n'
        '    (12 "In5.Cu" signal "Art06")\n'
        '  )\n'
        ')\n'
    )
    csv_path = pcb_local_router.write_local_route_csv(
        fanout_params={
            "orderLines": [
                {"net": "NET_A", "layer": "Art03", "order": 1},
                {"net": "NET_B", "layer": "In5", "order": 2},
                {"net": "NET_C", "layer": "SIG99", "order": 3},
            ]
        },
        project_data=project_data,
        work_dir=tmp_path,
    )

    assert csv_path.read_text(encoding="utf-8").splitlines() == [
        "net,route_layer",
        "NET_A,In2.Cu",
        "NET_B,In5.Cu",
        "NET_C,",
    ]


def test_resolve_pcbrouter_binary_prefers_aarch64_on_linux(monkeypatch, tmp_path):
    bin_dir = tmp_path / "vendor" / "pcbrouter" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "pcbrouter").write_text("x86", encoding="utf-8")
    (bin_dir / "pcbrouter_aarch64").write_text("arm", encoding="utf-8")

    monkeypatch.setattr(pcb_local_router, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(pcb_local_router, "_config_paths", lambda: [])
    monkeypatch.setattr(pcb_local_router.sys, "platform", "linux")
    monkeypatch.setattr(pcb_local_router.platform, "machine", lambda: "aarch64")

    assert pcb_local_router.resolve_pcbrouter_binary() == bin_dir / "pcbrouter_aarch64"


def test_resolve_pcbrouter_binary_expands_relative_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PCBROUTER_BIN", "vendor/pcbrouter/bin/pcbrouter")

    assert pcb_local_router.resolve_pcbrouter_binary() == (
        tmp_path / "vendor" / "pcbrouter" / "bin" / "pcbrouter"
    )


def test_run_pcbrouter_local_route_with_fake_binary(monkeypatch, tmp_path):
    fake_binary = tmp_path / "fake_pcbrouter.py"
    _fake_pcbrouter(fake_binary)
    monkeypatch.setenv("PCBROUTER_BIN", str(fake_binary))

    source_board = tmp_path / "case.kicad_pcb"
    source_board.write_text("(kicad_pcb (layers (6 \"In2.Cu\" signal \"Art03\")))", encoding="utf-8")
    source_board.with_suffix(".kicad_dru").write_text("(version 1)\n", encoding="utf-8")

    outputs = pcb_local_router.run_pcbrouter_local_route(
        project_data="",
        source_board_path=str(source_board),
        fanout_params={"orderLines": [{"net": "NET_A", "layer": "Art03", "order": 1}]},
        work_dir=tmp_path / "work",
    )

    assert outputs.routing_result_path.name == "output.fake.kicad_pcb"
    assert outputs.routing_result_path.read_text(encoding="utf-8").endswith("; fake local routed\n")
    assert outputs.import_lines_path is None
    assert outputs.output_csv_path is not None
    assert outputs.output_csv_path.name == "output.fake.csv"
    assert outputs.input_csv_path.read_text(encoding="utf-8").splitlines() == [
        "net,route_layer",
        "NET_A,In2.Cu",
    ]
    assert "已使用规则文件 case.kicad_dru" in outputs.report
    assert "fake pcbrouter ok" in outputs.stdout_path.read_text(encoding="utf-8")


def test_route_bga_falls_back_to_pcbrouter_when_primary_router_fails(monkeypatch, tmp_path):
    fake_binary = tmp_path / "fake_pcbrouter.py"
    _fake_pcbrouter(fake_binary)
    monkeypatch.setenv("PCBROUTER_BIN", str(fake_binary))
    monkeypatch.setenv("ROUTER_WORK_DIR", str(tmp_path / "router_work"))
    monkeypatch.setenv("ROUTER_ARC_DIR", str(tmp_path / "missing_arc_runtime"))

    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "local-router-fallback"
    transport.set_session_mode("local-router-fallback", "pcb")
    transport._cached_project_data["local-router-fallback"] = (
        '(kicad_pcb\n'
        '  (layers (6 "In2.Cu" signal "Art03"))\n'
        '  (component "U1" (package "BGA"))\n'
        ')\n'
    )

    result = pcb_tools.route_bga(
        json.dumps(
            {
                "routerType": "arc",
                "selectedBGA": "U1",
                "orderLines": [{"net": "NET_A", "layer": "Art03", "order": 1}],
            }
        ),
        session_id="local-router-fallback",
    )

    assert "已切换到 pcbrouter 局部布线兜底" in result
    assert "已返回完整 KiCad PCB 文件，前端将跳过 importLines 导入" in result
    pending = pcb_tools._transport.pop_pending_pcb_fields("local-router-fallback")
    assert Path(pending["routingResult"]).name == "output.fake.kicad_pcb"
    assert "importLinesFilePath" not in pending
    assert "pcbrouter 局部布线兜底完成" in pending["report"]
