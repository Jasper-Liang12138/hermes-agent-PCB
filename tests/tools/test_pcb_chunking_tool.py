from __future__ import annotations

import json
import types

from tools import pcb_chunking_tool


def test_find_vendor_wheel_returns_latest_match(monkeypatch, tmp_path):
    vendor_dir = tmp_path / "vendor"
    vendor_dir.mkdir()
    older = vendor_dir / "pcb_chunk_service-0.1.0-py3-none-any.whl"
    newer = vendor_dir / "pcb_chunk_service-0.2.0-py3-none-any.whl"
    older.write_text("", encoding="utf-8")
    newer.write_text("", encoding="utf-8")

    monkeypatch.setattr(
        pcb_chunking_tool,
        "_candidate_vendor_dirs",
        lambda: [vendor_dir],
    )

    assert pcb_chunking_tool._find_vendor_wheel() == newer


def test_load_pcb_chunk_service_from_vendor_wheel(monkeypatch, tmp_path):
    wheel_path = tmp_path / "vendor" / "pcb_chunk_service-0.1.0-py3-none-any.whl"
    wheel_path.parent.mkdir()
    wheel_path.write_text("", encoding="utf-8")

    class _FakeService:
        pass

    fake_module = types.SimpleNamespace(PCBChunkService=_FakeService)
    calls = {"count": 0}

    def _fake_import_module(name: str):
        assert name == "pcb_chunk_service"
        calls["count"] += 1
        if calls["count"] == 1:
            raise ImportError("not installed")
        return fake_module

    monkeypatch.setattr(pcb_chunking_tool.importlib, "import_module", _fake_import_module)
    monkeypatch.setattr(pcb_chunking_tool, "_find_vendor_wheel", lambda: wheel_path)
    monkeypatch.setattr(pcb_chunking_tool.sys, "path", [])

    service_cls, load_source = pcb_chunking_tool._load_pcb_chunk_service()

    assert service_cls is _FakeService
    assert load_source == "vendor"
    assert str(wheel_path) in pcb_chunking_tool.sys.path


def test_resolve_model_runtime_config_prefers_project_config_ini(monkeypatch, tmp_path):
    project_cfg = tmp_path / "config.ini"
    project_cfg.write_text(
        "[model]\n"
        "model = qwen3.6-flash\n"
        "base_url = https://example.com/v1\n"
        "api_key = sk-local-key\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        pcb_chunking_tool,
        "_candidate_project_config_paths",
        lambda: [project_cfg],
    )
    monkeypatch.setenv("OPENAI_BASE_URL", "https://env.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-key")

    fake_config_module = types.SimpleNamespace(
        load_config=lambda: {
            "model": {
                "default": "qwen3.6-plus-2026-04-02",
                "base_url": "https://global.example/v1",
                "api_key": "sk-global-key",
            }
        }
    )
    monkeypatch.setitem(pcb_chunking_tool.sys.modules, "hermes_cli.config", fake_config_module)

    runtime = pcb_chunking_tool._resolve_model_runtime_config()

    assert runtime == {
        "model": "qwen3.6-flash",
        "base_url": "https://example.com/v1",
        "api_key": "sk-local-key",
    }


def test_extract_bga_prefers_long_context_analysis(monkeypatch):
    class _FakeBGA:
        def __init__(self, label: str, detail: str):
            self.label = label
            self.detail = detail

        def to_dict(self):
            return {"label": self.label, "detail": self.detail}

    fake_service = types.SimpleNamespace(
        extract_bga_from_txt=lambda **kwargs: [_FakeBGA("U22", "BGA 400pin")]
    )

    monkeypatch.setattr(pcb_chunking_tool, "_service", fake_service)
    monkeypatch.setattr(
        pcb_chunking_tool,
        "_summarize_board_model",
        lambda board_text: {
            "layers": [{"name": "Top", "kind": "signal"}],
            "signalLayers": ["Top", "Art03"],
            "topPackages": [{"name": "BGA", "count": 1}],
            "netSummary": {"powerNets": ["VDD"], "groundNets": ["GND"], "clockNets": ["MCLK"], "signalNetCount": 12, "ncNetCount": 3},
        },
    )
    monkeypatch.setattr(
        pcb_chunking_tool,
        "_analyze_board_with_model",
        lambda board_text: {
            "selection": [],
            "boardSummary": {
                "stackupSummary": ["Top: signal", "Art03: signal"],
                "packageHints": ["U22: BGA 400pin"],
                "netSummary": {
                    "powerNets": ["VDD"],
                    "groundNets": ["GND"],
                    "clockNets": ["MCLK"],
                    "signalNetCount": 12,
                    "ncNetCount": 3,
                },
            },
            "fanoutContext": {
                "recommendedEscapeLayers": ["Top", "Art03"],
                "recommendedLineWidth": 4,
                "recommendedLineSpacing": 3,
                "prioritySuggestion": ["ground", "power", "clock", "signal"],
                "rationale": "use top plus first inner signal layer",
            },
            "source": "llm_long_context",
            "fallbackUsed": False,
        },
    )

    result = json.loads(pcb_chunking_tool._extract_bga("(pcb demo)"))

    assert result["source"] == "llm_long_context"
    assert result["fallbackUsed"] is False
    assert result["selection"] == [{"label": "U22", "detail": "BGA 400pin"}]
    assert result["boardSummary"]["stackupSummary"] == ["Top: signal", "Art03: signal"]
    assert result["fanoutContext"]["recommendedEscapeLayers"] == ["Top", "Art03"]


def test_extract_bga_falls_back_to_rule_path(monkeypatch):
    class _FakeBGA:
        def __init__(self, label: str, detail: str):
            self.label = label
            self.detail = detail

        def to_dict(self):
            return {"label": self.label, "detail": self.detail}

    fake_service = types.SimpleNamespace(
        extract_bga_from_txt=lambda **kwargs: [_FakeBGA("U35", "BGA 256pin")]
    )

    monkeypatch.setattr(pcb_chunking_tool, "_service", fake_service)
    monkeypatch.setattr(
        pcb_chunking_tool,
        "_summarize_board_model",
        lambda board_text: {
            "layers": [
                {"name": "Top", "kind": "signal"},
                {"name": "Gnd02", "kind": "plane"},
                {"name": "Art03", "kind": "signal"},
            ],
            "signalLayers": ["Top", "Art03"],
            "topPackages": [{"name": "BGA", "count": 1}],
            "netSummary": {"powerNets": ["VCC"], "groundNets": ["GND"], "clockNets": [], "signalNetCount": 8, "ncNetCount": 1},
        },
    )

    def _raise(*args, **kwargs):
        raise RuntimeError("mock model failure")

    monkeypatch.setattr(pcb_chunking_tool, "_analyze_board_with_model", _raise)

    result = json.loads(pcb_chunking_tool._extract_bga("(pcb demo)"))

    assert result["source"] == "rule_fallback"
    assert result["fallbackUsed"] is True
    assert result["selection"] == [{"label": "U35", "detail": "BGA 256pin"}]
    assert result["boardSummary"]["stackupSummary"] == [
        "Top: signal",
        "Gnd02: plane",
        "Art03: signal",
    ]
    assert result["fanoutContext"]["recommendedEscapeLayers"] == ["Top", "Art03"]
    assert "已回退到规则提取" in result["message"]


def test_resolve_board_text_uses_websocket_cache_for_sentinel():
    from tools.pcb_tools import WebSocketTransportSingleton

    transport = WebSocketTransportSingleton.get_instance()
    transport.cache_project_data("(pcb cached board)", session_id="sess-cache-bga")

    try:
        assert (
            pcb_chunking_tool._resolve_board_text(
                "__CACHED_PROJECT_DATA__",
                session_id="sess-cache-bga",
            )
            == "(pcb cached board)"
        )
        assert (
            pcb_chunking_tool._resolve_board_text("", session_id="sess-cache-bga")
            == "(pcb cached board)"
        )
    finally:
        transport.clear_session("sess-cache-bga")


def test_text_bga_selection_detects_qiyun_component_blocks():
    board_text = """
(layout
  (components
    (component "U1"
      (part "JBGA608-40-2727")
      (footprint "JBGA608-40-2727"
        (pins
          (pin (number "A1"))
          (pin (number "A2"))
        )
      )
    )
    (component "R1"
      (part "RES0402")
      (footprint "RES0402")
    )
  )
)
"""

    assert pcb_chunking_tool._extract_text_bga_selection(board_text) == [
        {"label": "U1", "detail": "JBGA608-40-2727 (2 pins)"}
    ]


def test_build_board_context_converts_layout_text_before_chunking(monkeypatch):
    seen: dict[str, object] = {}

    def _fake_txt_to_kicad(board_text: str, *, stem: str = "board"):
        seen["converted_from"] = board_text
        seen["stem"] = stem
        return "(kicad_pcb (footprint U1))"

    def _fake_parse_board_objects(board_text: str):
        seen["parsed_text"] = board_text
        return [types.SimpleNamespace(kind="footprint")]

    monkeypatch.setattr(
        pcb_chunking_tool,
        "_service",
        types.SimpleNamespace(
            chunk_config=types.SimpleNamespace(
                chunk_chars=12000,
                chunk_tokens=2048,
                max_context_chars=60000,
                max_context_tokens=14000,
                max_chunks=8,
            )
        ),
    )
    monkeypatch.setattr(pcb_chunking_tool, "_txt_to_kicad", _fake_txt_to_kicad)
    monkeypatch.setattr(pcb_chunking_tool, "_parse_board_objects", _fake_parse_board_objects)
    monkeypatch.setattr(
        pcb_chunking_tool,
        "_pack_objects",
        lambda **kwargs: [types.SimpleNamespace(text="chunk-text", token_count=11)],
    )
    monkeypatch.setattr(pcb_chunking_tool, "_limit_chunks", lambda chunks, **kwargs: chunks)
    monkeypatch.setattr(pcb_chunking_tool, "_render_context_chunks", lambda chunks: "rendered-context")
    monkeypatch.setattr(pcb_chunking_tool, "_GLOBAL_KINDS", set())

    result = pcb_chunking_tool._build_board_context("(layout (foo bar))")

    assert seen == {
        "converted_from": "(layout (foo bar))",
        "stem": "board_context",
        "parsed_text": "(kicad_pcb (footprint U1))",
    }
    assert result["contextText"] == "rendered-context"
    assert result["stats"]["topLevelObjectCount"] == 1
    assert result["stats"]["componentObjectCount"] == 1
    assert result["stats"]["chunkCount"] == 1
