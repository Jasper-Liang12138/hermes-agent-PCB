"""Probe no-thinking / JSON-only payload variants for PCB tool-planning models."""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from tools import pcb_model_runtime


EXPECTED_JSON = {
    "intent": "chat",
    "route_mode": "chat",
    "should_call_get_project_data": False,
    "reason_code": "probe",
}
PROBE_PROMPT = (
    "Return only this JSON:\n"
    + json.dumps(EXPECTED_JSON, ensure_ascii=False, separators=(",", ":"))
)
THINKING_RE = re.compile(r"(?is)(thinking process|<think\b|analy[sz]e the request|思考过程|推理过程)")


def build_probe_variants() -> list[dict[str, Any]]:
    base_messages = [
        {"role": "system", "content": "You are a JSON-only classifier. Do not explain."},
        {"role": "user", "content": PROBE_PROMPT},
    ]
    prefixed_messages = [
        base_messages[0],
        {"role": "user", "content": "/no_think\n" + PROBE_PROMPT},
    ]
    json_prefill_messages = [
        *base_messages,
        {"role": "assistant", "content": "{"},
    ]
    return [
        {"name": "baseline", "messages": base_messages, "extra": {}},
        {"name": "no_think_prefix", "messages": prefixed_messages, "extra": {}},
        {
            "name": "chat_template_kwargs",
            "messages": base_messages,
            "extra": {"chat_template_kwargs": {"enable_thinking": False}},
        },
        {
            "name": "prefix_plus_chat_template",
            "messages": prefixed_messages,
            "extra": {"chat_template_kwargs": {"enable_thinking": False}},
        },
        {"name": "top_level_enable_thinking", "messages": base_messages, "extra": {"enable_thinking": False}},
        {
            "name": "extra_body_style",
            "messages": base_messages,
            "extra": {"enable_thinking": False, "chat_template_kwargs": {"enable_thinking": False}},
        },
        {"name": "reasoning_disabled", "messages": base_messages, "extra": {"reasoning": {"enabled": False}}},
        {"name": "reasoning_effort_none", "messages": base_messages, "extra": {"reasoning": {"effort": "none"}}},
        {
            "name": "response_format_json_object",
            "messages": base_messages,
            "extra": {"response_format": {"type": "json_object"}},
        },
        {
            "name": "response_format_plus_chat_template",
            "messages": base_messages,
            "extra": {
                "response_format": {"type": "json_object"},
                "chat_template_kwargs": {"enable_thinking": False},
            },
        },
        {
            "name": "stop_thinking_process",
            "messages": base_messages,
            "extra": {"stop": ["Thinking Process", "<think>", "```"]},
        },
        {"name": "json_prefill", "messages": json_prefill_messages, "extra": {}},
    ]


def _extract_text(response_data: dict[str, Any]) -> str:
    choices = response_data.get("choices") if isinstance(response_data, dict) else None
    if not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message") or {}
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content or "")


def _parse_json_from_text(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    candidates = [raw]
    fenced = re.findall(r"```(?:json)?\s*([\s\S]*?)\s*```", raw, flags=re.IGNORECASE)
    candidates.extend(item.strip() for item in fenced if item.strip())
    starts = [match.start() for match in re.finditer(r"\{", raw)]
    for start in reversed(starts):
        end = raw.rfind("}")
        if end > start:
            candidates.append(raw[start : end + 1].strip())
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _json_matches_expected(parsed: dict[str, Any] | None) -> bool:
    if not isinstance(parsed, dict):
        return False
    return all(parsed.get(key) == value for key, value in EXPECTED_JSON.items())


def _redact_runtime(runtime: dict[str, str]) -> dict[str, str]:
    redacted = dict(runtime)
    api_key = redacted.get("api_key", "")
    if api_key:
        redacted["api_key"] = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else "***"
    return redacted


def run_probe_variant(
    variant: dict[str, Any],
    *,
    runtime: dict[str, str],
    max_tokens: int,
    timeout_s: float,
) -> dict[str, Any]:
    base_url = pcb_model_runtime.normalize_openai_base_url(runtime["base_url"])
    payload: dict[str, Any] = {
        "model": runtime["model"],
        "messages": variant["messages"],
        "stream": False,
        "max_tokens": max_tokens,
        "temperature": 0,
        "top_p": 1,
    }
    payload.update(variant.get("extra") or {})
    headers = {"Content-Type": "application/json"}
    if runtime.get("api_key"):
        headers["Authorization"] = f"Bearer {runtime['api_key']}"
    req = urlrequest.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    started = time.monotonic()
    raw_response = ""
    error = ""
    status_ok = False
    usage: dict[str, Any] = {}
    text = ""
    response_id = None
    try:
        with pcb_model_runtime._open_chat_request(req, timeout_s=timeout_s, base_url=base_url) as resp:
            raw_response = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw_response)
        response_id = data.get("id") if isinstance(data, dict) else None
        usage = data.get("usage") if isinstance(data, dict) and isinstance(data.get("usage"), dict) else {}
        text = _extract_text(data)
        status_ok = True
    except urlerror.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        error = f"HTTPError {exc.code}: {detail or exc.reason}"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    elapsed_s = round(time.monotonic() - started, 3)
    parsed = _parse_json_from_text(text)
    has_thinking = bool(THINKING_RE.search(text))
    valid_json = _json_matches_expected(parsed)
    return {
        "name": variant["name"],
        "ok": status_ok,
        "elapsed_s": elapsed_s,
        "error": error,
        "response_id": response_id,
        "usage": usage,
        "completion_tokens": usage.get("completion_tokens"),
        "has_thinking": has_thinking,
        "has_think_tag": bool(re.search(r"(?is)<think\b|</think>", text)),
        "has_thinking_process": bool(re.search(r"(?is)thinking process", text)),
        "has_analyze_request": bool(re.search(r"(?is)analy[sz]e the request", text)),
        "valid_json": valid_json,
        "parsed_json": parsed,
        "json_matches_expected": valid_json,
        "raw_preview": text[:500],
        "raw_length": len(text),
        "usable_no_thinking": bool(valid_json and not has_thinking and elapsed_s <= 20),
    }


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# PCB No-Thinking Probe",
        "",
        f"Generated: {report['generated_at']}",
        f"Runtime: `{json.dumps(report['runtime'], ensure_ascii=False)}`",
        "",
        "| Variant | OK | Usable | JSON | Thinking | Elapsed | Tokens | Error |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in report["results"]:
        lines.append(
            "| {name} | {ok} | {usable} | {json_ok} | {thinking} | {elapsed:.3f} | {tokens} | {error} |".format(
                name=item["name"],
                ok="yes" if item["ok"] else "no",
                usable="yes" if item["usable_no_thinking"] else "no",
                json_ok="yes" if item["valid_json"] else "no",
                thinking="yes" if item["has_thinking"] else "no",
                elapsed=float(item["elapsed_s"]),
                tokens=item.get("completion_tokens") if item.get("completion_tokens") is not None else "-",
                error=str(item.get("error") or "").replace("|", "\\|")[:120],
            )
        )
    lines.extend(["", "## Previews", ""])
    for item in report["results"]:
        lines.extend(
            [
                f"### {item['name']}",
                "",
                f"- usable: `{item['usable_no_thinking']}`",
                f"- valid_json: `{item['valid_json']}`",
                f"- has_thinking: `{item['has_thinking']}`",
                "",
                "```text",
                str(item.get("raw_preview") or ""),
                "```",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe no-thinking payload variants for PCB tool-planning model.")
    parser.add_argument("--out-dir", type=Path, default=Path("reports/no_think_probe"), help="Output directory")
    parser.add_argument("--timeout", type=float, default=60.0, help="Per-variant timeout seconds")
    parser.add_argument("--max-tokens", type=int, default=128, help="Max tokens per variant")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    runtime = pcb_model_runtime.resolve_model_runtime(
        pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT,
        require_api_key=True,
    )
    results = []
    for variant in build_probe_variants():
        print(f"[probe] {variant['name']} ...", flush=True)
        result = run_probe_variant(
            variant,
            runtime=runtime,
            max_tokens=args.max_tokens,
            timeout_s=args.timeout,
        )
        results.append(result)
        print(
            f"[probe] {variant['name']} ok={result['ok']} json={result['valid_json']} "
            f"thinking={result['has_thinking']} elapsed={result['elapsed_s']}s",
            flush=True,
        )
    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "runtime": _redact_runtime(runtime),
        "max_tokens": args.max_tokens,
        "timeout_s": args.timeout,
        "expected_json": EXPECTED_JSON,
        "results": results,
        "usable_variants": [item["name"] for item in results if item["usable_no_thinking"]],
    }
    json_path = args.out_dir / "qwen36_35b_probe.json"
    md_path = args.out_dir / "qwen36_35b_probe.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(md_path, report)
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {md_path}")
    print("Usable variants: " + (", ".join(report["usable_variants"]) or "none"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
