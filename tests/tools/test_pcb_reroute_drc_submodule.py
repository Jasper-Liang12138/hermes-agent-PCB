from __future__ import annotations

from pathlib import Path

from tools.pcb_reroute_drc import _eval_root, validate_kicad_patch_with_drc


def test_ai_pcb_eval_submodule_contains_required_helpers():
    root = _eval_root()

    assert (root / "patch_kicad_from_raw_standalone.py").is_file()
    assert (root / "drc_backend" / "api.py").is_file()


def test_validate_kicad_patch_with_real_submodule_smoke(tmp_path):
    board = (Path(__file__).resolve().parents[2] / "test_client" / "mock_reroute_board.kicad_pcb").read_text(
        encoding="utf-8"
    )

    attempt = validate_kicad_patch_with_drc(
        original_board_data=board,
        model_output_text='(segment (start 100 100) (end 110 100) (width 0.2) (layer "F.Cu") (net 13))',
        output_dir=tmp_path,
        sample_id="submodule_smoke",
        iteration=1,
    )

    assert attempt.iteration == 1
    assert attempt.passed is True
    assert attempt.fill_detail["segments_count"] == 1
    assert Path(attempt.filled_board_data_file_path).is_file()
    assert attempt.drc_result["ok"] is True
    assert attempt.drc_result["pass"] is True
