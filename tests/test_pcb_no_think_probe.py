from __future__ import annotations

import json

from scripts import probe_pcb_no_think_modes as probe


def test_probe_variants_cover_no_thinking_payload_shapes():
    variants = probe.build_probe_variants()
    by_name = {item["name"]: item for item in variants}

    assert len(variants) == 12
    assert by_name["no_think_prefix"]["messages"][1]["content"].startswith("/no_think\n")
    assert by_name["chat_template_kwargs"]["extra"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert by_name["top_level_enable_thinking"]["extra"] == {"enable_thinking": False}
    assert by_name["reasoning_effort_none"]["extra"] == {"reasoning": {"effort": "none"}}
    assert by_name["response_format_json_object"]["extra"] == {"response_format": {"type": "json_object"}}
    assert by_name["json_prefill"]["messages"][-1] == {"role": "assistant", "content": "{"}


def test_probe_json_detection_requires_expected_fields():
    text = json.dumps(probe.EXPECTED_JSON, ensure_ascii=False)
    parsed = probe._parse_json_from_text("Final:\n" + text)

    assert parsed == probe.EXPECTED_JSON
    assert probe._json_matches_expected(parsed)
    assert not probe.THINKING_RE.search(text)


def test_probe_thinking_detection_catches_reasoning_text():
    text = "Thinking Process:\n1. Analyze the Request\n" + json.dumps(probe.EXPECTED_JSON)
    parsed = probe._parse_json_from_text(text)

    assert parsed == probe.EXPECTED_JSON
    assert probe.THINKING_RE.search(text)
