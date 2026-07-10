from pathlib import Path
from types import SimpleNamespace

import tools.pcb_local_router as local_router


def test_pcbrouter_local_route_invokes_helper_with_relative_input_paths(monkeypatch, tmp_path):
    binary = tmp_path / "pcbrouter.exe"
    binary.write_bytes(b"MZstub")
    source_board = tmp_path / "export.kicad_pcb"
    source_board.write_text(
        '(kicad_pcb (version 20240108) (layers (0 "F.Cu" signal) (31 "B.Cu" signal)) (net 1 R_NAND_RDY0))',
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(local_router, "resolve_pcbrouter_binary", lambda: binary)
    monkeypatch.setattr(local_router, "_native_binary_usable", lambda _path: True)
    monkeypatch.setattr(local_router, "_pcbrouter_binary_args", lambda _binary, *args: [str(_binary), *args])

    def fake_run_process(args, work_dir, _timeout):
        captured["args"] = args
        captured["work_dir"] = work_dir
        output_dir = Path(work_dir) / "output_routed"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "routed.kicad_pcb").write_text(source_board.read_text(encoding="utf-8"), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(local_router, "_run_process", fake_run_process)

    result = local_router.run_pcbrouter_local_route(
        project_data=source_board.read_text(encoding="utf-8"),
        route_params={"selectedNets": ["R_NAND_RDY0"]},
        work_dir=tmp_path / "work",
        source_board_path=str(source_board),
    )

    args = captured["args"]
    assert args[1] == "export.kicad_pcb"
    assert args[3] == "local_route_input.csv"
    assert result.input_board_path == Path(captured["work_dir"]) / args[1]
    assert result.input_csv_path == Path(captured["work_dir"]) / args[3]


def test_local_route_csv_uses_display_layer_names(tmp_path):
    board_text = '(kicad_pcb (version 20240108) (layers (0 "F.Cu" signal) (31 "B.Cu" signal)))'

    csv_path = local_router.write_local_route_csv(
        route_params={
            "orderLines": [
                {
                    "net_name": "R_NAND_RDY0",
                    "route_layer": "B.Cu",
                    "end": {"layer": "B.Cu", "x": 47.3, "y": 68.3},
                }
            ]
        },
        project_data=board_text,
        work_dir=tmp_path,
    )

    assert csv_path.read_text(encoding="utf-8").splitlines() == [
        "net,route_layer,target_x_mm,target_y_mm,target_layer",
        "R_NAND_RDY0,Bottom,47.3,68.3,Bottom",
    ]


def test_local_route_csv_uses_display_inner_layer_names(tmp_path):
    board_text = '(kicad_pcb (version 20240108) (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (31 "B.Cu" signal)))'

    csv_path = local_router.write_local_route_csv(
        route_params={
            "orderLines": [
                {
                    "net_name": "R_NAND_RDY0",
                    "route_layer": "In1.Cu",
                    "end": {"layer": "In1.Cu", "x": 47.3, "y": 68.3},
                }
            ]
        },
        project_data=board_text,
        work_dir=tmp_path,
    )

    assert csv_path.read_text(encoding="utf-8").splitlines() == [
        "net,route_layer,target_x_mm,target_y_mm,target_layer",
        "R_NAND_RDY0,In1.Cu,47.3,68.3,In1.Cu",
    ]


def test_local_route_csv_accepts_conductor_layer_names(tmp_path):
    board_text = '(kicad_pcb (version 20240108) (layers (0 "F.Cu" signal) (31 "B.Cu" signal)))'

    csv_path = local_router.write_local_route_csv(
        route_params={
            "orderLines": [
                {
                    "net_name": "R_NAND_RDY0",
                    "route_layer": "Conductor/Top",
                    "end": {"layer": "Conductor/Top", "x": 47.3, "y": 68.3},
                }
            ]
        },
        project_data=board_text,
        work_dir=tmp_path,
    )

    assert csv_path.read_text(encoding="utf-8").splitlines() == [
        "net,route_layer,target_x_mm,target_y_mm,target_layer",
        "R_NAND_RDY0,Top,47.3,68.3,Top",
    ]
