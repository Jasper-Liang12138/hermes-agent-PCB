from __future__ import annotations

from pathlib import Path

from tools import pcb_local_router


def _fake_pcbrouter(path: Path) -> None:
    path.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "board, flag, csv_path = sys.argv[1:]\n"
        "assert flag == '-bga_local_route'\n"
        "assert Path(board).is_file()\n"
        "assert Path(csv_path).is_file()\n"
        "Path('output_routed').mkdir(exist_ok=True)\n"
        "Path('output_routed/output.fake.kicad_pcb').write_text(\n"
        "    Path(board).read_text(encoding='utf-8') + '\\n; local route completion\\n',\n"
        "    encoding='utf-8',\n"
        ")\n"
        "Path('output_routed/output.fake.csv').write_text('wirelength,via\\n0,0\\n', encoding='utf-8')\n"
        "print('fake pcbrouter ok')\n",
        encoding="utf-8",
    )


def test_write_local_route_csv_maps_known_board_layers(tmp_path):
    project_data = (
        '(kicad_pcb\n'
        '  (layers\n'
        '    (0 "F.Cu" signal "Top")\n'
        '    (6 "In2.Cu" signal "Art03")\n'
        '  )\n'
        ')\n'
    )

    csv_path = pcb_local_router.write_local_route_csv(
        route_params={
            "orderLines": [
                {"net": "NET_A", "layer": "Art03", "order": 1},
                {"net": "NET_B", "layer": "In5", "order": 2},
            ]
        },
        project_data=project_data,
        work_dir=tmp_path,
    )

    assert csv_path.read_text(encoding="utf-8").splitlines() == [
        "net,route_layer",
        "NET_A,In2.Cu",
        "NET_B,In5.Cu",
    ]


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
        route_params={"orderLines": [{"net": "NET_A", "layer": "Art03", "order": 1}]},
        work_dir=tmp_path / "work",
    )

    assert outputs.routing_result_path.name == "output.fake.kicad_pcb"
    assert outputs.routing_result_path.read_text(encoding="utf-8").endswith("; local route completion\n")
    assert outputs.import_lines_path is None
    assert outputs.output_csv_path is not None
    assert outputs.input_csv_path.read_text(encoding="utf-8").splitlines() == [
        "net,route_layer",
        "NET_A,In2.Cu",
    ]
    assert "局部布线完善完成" in outputs.report
    assert "fake pcbrouter ok" in outputs.stdout_path.read_text(encoding="utf-8")
