from __future__ import annotations

import configparser
import sys
import types

import pytest

from tools import pcb_explain_report


def test_generate_explain_report_runs_local_classifier(monkeypatch, tmp_path):
    board_path = tmp_path / "candidate.kicad_pcb"
    checkpoint_path = tmp_path / "best.pt"
    output_root = tmp_path / "explain_runs"
    board_path.write_text("(kicad_pcb)", encoding="utf-8")
    checkpoint_path.write_text("checkpoint", encoding="utf-8")

    captured = {}

    def _fake_infer_file(input_path, checkpoint, *, output_root):
        captured["input_path"] = input_path
        captured["checkpoint"] = checkpoint
        captured["output_root"] = output_root
        return "可解释性分析报告\n================\n\n预测结果: 布线较好"

    fake_module = types.SimpleNamespace(infer_file=_fake_infer_file)
    monkeypatch.setitem(
        sys.modules,
        "tools.pcb_explain_classifier.infer_ascend_multiview_classifier",
        fake_module,
    )

    report = pcb_explain_report.generate_explain_report(
        board_file_path=str(board_path),
        checkpoint_path=str(checkpoint_path),
        output_root=str(output_root),
    )

    assert "可解释性分析报告" in report
    assert captured["input_path"] == board_path
    assert captured["checkpoint"] == checkpoint_path
    assert captured["output_root"] == output_root


def test_resolve_checkpoint_from_config(monkeypatch, tmp_path):
    monkeypatch.delenv("PCB_EXPLAIN_CHECKPOINT", raising=False)
    parser = configparser.ConfigParser()
    parser.add_section("explain")
    parser.set("explain", "checkpoint_path", str(tmp_path / "custom.pt"))

    assert pcb_explain_report.resolve_checkpoint_path(parser) == tmp_path / "custom.pt"


def test_missing_checkpoint_error_does_not_expose_board_path(tmp_path):
    board_path = tmp_path / "secret_board.kicad_pcb"
    board_path.write_text("(kicad_pcb)", encoding="utf-8")

    with pytest.raises(RuntimeError) as excinfo:
        pcb_explain_report.generate_explain_report(
            board_file_path=str(board_path),
            checkpoint_path=str(tmp_path / "missing.pt"),
        )

    message = str(excinfo.value)
    assert "PCB_EXPLAIN_CHECKPOINT" in message
    assert str(board_path) not in message
