from pathlib import Path

import convert


def test_kicad_layer_ids_use_standard_numbering():
    board = convert.BoardModel(
        stem="layer_ids",
        layers=[
            convert.LayerSpec("Top", "Top", "signal"),
            convert.LayerSpec("Gnd02", "Gnd02", "signal"),
            convert.LayerSpec("Sig03", "Sig03", "signal"),
            convert.LayerSpec("Bottom", "Bottom", "signal"),
        ],
    )

    assigned = convert.assign_kicad_copper_layers(board)

    assert [(layer.kicad_name, layer.kicad_id) for layer in assigned] == [
        ("F.Cu", 0),
        ("In1.Cu", 1),
        ("In2.Cu", 2),
        ("B.Cu", 31),
    ]
    user_layer_ids = {name: layer_id for layer_id, name, *_ in convert.STANDARD_KICAD_USER_LAYERS}
    assert min(user_layer_ids.values()) >= 32
    assert user_layer_ids["Edge.Cuts"] == 44


def test_txt_to_kicad_does_not_report_unused_donor(tmp_path, monkeypatch):
    input_file = tmp_path / "board.txt"
    input_file.write_text("placeholder", encoding="utf-8")
    output_dir = tmp_path / "out"
    donor_dir = tmp_path / "donor"
    donor_dir.mkdir()
    (donor_dir / "board.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")

    monkeypatch.setattr(convert, "parse_txt_board", lambda _path: convert.BoardModel(stem="board"))
    monkeypatch.setattr(convert, "write_kicad", lambda _board: "(kicad_pcb)\n")
    monkeypatch.setattr(convert, "write_kicad_dru", lambda _board: "")

    result = convert.convert_one("txt_to_kicad", input_file, output_dir, donor_dir)

    assert Path(result["output"]).is_file()
    assert result["donor"] == ""
