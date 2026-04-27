"""PCB long-context board analysis tool.

Uses the pcb_chunk_service wheel as the primary PCB analysis path.
The external tool name remains ``pcb_extract_bga`` to minimize flow changes,
but the tool now returns structured board-analysis output:

- ``selection``: BGA candidate list
- ``boardSummary``: stackup / package / net summary
- ``fanoutContext``: controlled context for later fanout parameter generation

If the long-context model path fails, the implementation falls back to the
original rule-based BGA extraction path.

Toggle via config.yaml:
    pcb:
      use_long_context_module: true
"""

from __future__ import annotations

import configparser
import importlib
import json
import logging
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from tools.registry import registry

logger = logging.getLogger(__name__)


def _candidate_vendor_dirs() -> list[Path]:
    dirs: list[Path] = []
    bundled_root = getattr(sys, "_MEIPASS", None)
    if bundled_root:
        dirs.append(Path(bundled_root) / "vendor")
    dirs.append(Path(__file__).resolve().parents[1] / "vendor")
    return dirs


def _candidate_project_config_paths() -> list[Path]:
    paths: list[Path] = []
    bundled_root = getattr(sys, "_MEIPASS", None)
    if bundled_root:
        paths.append(Path(bundled_root) / "config.ini")
    paths.append(Path(__file__).resolve().parents[1] / "config.ini")
    return paths


def _load_project_config_ini() -> configparser.ConfigParser | None:
    parser = configparser.ConfigParser()
    for path in _candidate_project_config_paths():
        if not path.exists():
            continue
        try:
            parser.read(path, encoding="utf-8")
            return parser
        except Exception as exc:
            logger.warning("Failed reading project config.ini from %s: %s", path, exc)
    return None


def _find_vendor_wheel() -> Optional[Path]:
    for vendor_dir in _candidate_vendor_dirs():
        if not vendor_dir.exists():
            continue
        matches = sorted(vendor_dir.glob("pcb_chunk_service-*.whl"), reverse=True)
        if matches:
            return matches[0]
    return None


def _load_pcb_chunk_service():
    try:
        module = importlib.import_module("pcb_chunk_service")
        return module.PCBChunkService, "installed"
    except ImportError:
        pass

    wheel_path = _find_vendor_wheel()
    if wheel_path:
        wheel_str = str(wheel_path)
        if wheel_str not in sys.path:
            sys.path.insert(0, wheel_str)
        try:
            module = importlib.import_module("pcb_chunk_service")
            logger.info("Loaded pcb_chunk_service from vendor wheel: %s", wheel_path)
            return module.PCBChunkService, "vendor"
        except ImportError as exc:
            logger.warning(
                "Failed loading pcb_chunk_service from vendor wheel %s: %s",
                wheel_path,
                exc,
            )

    logger.warning(
        "pcb_chunk_service not installed and vendor wheel not loadable; "
        "pcb_extract_bga tool disabled"
    )
    return None, None


def _load_pcb_chunk_dependencies() -> dict[str, Any]:
    if not _AVAILABLE:
        return {}
    try:
        return {
            "chunker": importlib.import_module("pcb_chunk_service.chunker"),
            "converter": importlib.import_module("pcb_chunk_service.converter"),
            "schemas": importlib.import_module("pcb_chunk_service.schemas"),
            "adapter": importlib.import_module(
                "pcb_chunk_service.adapters.openai_compatible"
            ),
        }
    except Exception as exc:
        logger.warning("Failed loading pcb_chunk_service dependencies: %s", exc)
        return {}


_PCBChunkService, _LOAD_SOURCE = _load_pcb_chunk_service()
_AVAILABLE = _PCBChunkService is not None
_service = _PCBChunkService() if _AVAILABLE else None
_DEPS = _load_pcb_chunk_dependencies()

_chunker = _DEPS.get("chunker")
_converter = _DEPS.get("converter")
_schemas = _DEPS.get("schemas")
_adapter_mod = _DEPS.get("adapter")

_PromptBundle = getattr(_schemas, "PromptBundle", None)
_GenerationConfig = getattr(_schemas, "GenerationConfig", None)
_OpenAICompatibleChatAdapter = getattr(
    _adapter_mod, "OpenAICompatibleChatAdapter", None
)

_parse_txt_to_board_model = getattr(_converter, "parse_txt_to_board_model", None)
_txt_to_kicad = getattr(_converter, "txt_to_kicad", None)
_parse_board_objects = getattr(_chunker, "parse_board_objects", None)
_render_context_chunks = getattr(_chunker, "render_context_chunks", None)
_pack_objects = getattr(_chunker, "_pack_objects", None)
_limit_chunks = getattr(_chunker, "_limit_chunks", None)
_GLOBAL_KINDS = set(getattr(_chunker, "GLOBAL_KINDS", set()))

_POWER_NET_RE = re.compile(
    r"(?:^|[_/-])(vcc|vdd|vss|vin|vout|vbat|avdd|dvdd|pvdd|pp\d*|pwr|power)(?:$|[_/-])",
    re.IGNORECASE,
)
_GROUND_NET_RE = re.compile(r"(?:^|[_/-])(gnd|ground|agnd|dgnd|pgnd)(?:$|[_/-])", re.IGNORECASE)
_CLOCK_NET_RE = re.compile(
    r"(clk|clock|refclk|mclk|bclk|lrck|osc|xtal|pcie_refclk|sclk)",
    re.IGNORECASE,
)
_NC_NET_RE = re.compile(r"^(nc|n/c|no[_-]?connect|floating|none)$", re.IGNORECASE)

_BOARD_ANALYSIS_SYSTEM_PROMPT = """你是一名资深 PCB 板级分析工程师，负责从超长版图上下文中提取可执行的 BGA 分析结果。

只输出 JSON，不要输出 Markdown、解释性段落或代码块。
不要编造版图中不存在的数据；不确定时使用保守表述，并把缺失项留空数组或空字符串。

返回 JSON 对象，字段必须符合以下结构：
{
  "selection": [{"label": "U22", "detail": "BGA 400pin"}],
  "boardSummary": {
    "stackupSummary": ["Top: signal", "Gnd02: plane"],
    "packageHints": ["U22: BGA 400pin"],
    "netSummary": {
      "powerNets": ["VDD3V3"],
      "groundNets": ["GND"],
      "clockNets": ["MCLK"],
      "signalNetCount": 0,
      "ncNetCount": 0
    }
  },
  "fanoutContext": {
    "recommendedEscapeLayers": ["Top", "Art03"],
    "recommendedLineWidth": 4,
    "recommendedLineSpacing": 3,
    "prioritySuggestion": ["ground", "power", "clock", "signal"],
    "rationale": "简短说明"
  }
}
"""


def _config_enabled() -> bool:
    local_cfg = _load_project_config_ini()
    if local_cfg and local_cfg.has_section("pcb"):
        raw = local_cfg.get("pcb", "use_long_context_module", fallback="true").strip().lower()
        return raw not in ("false", "0", "no", "off")
    try:
        from hermes_cli.config import load_config

        return bool(load_config().get("pcb", {}).get("use_long_context_module", True))
    except Exception:
        return True


def _long_context_ready() -> bool:
    return all(
        (
            _service,
            _PromptBundle,
            _GenerationConfig,
            _OpenAICompatibleChatAdapter,
            _parse_txt_to_board_model,
            _parse_board_objects,
            _render_context_chunks,
            _pack_objects,
            _limit_chunks,
        )
    )


def _safe_int(value: Any, default: int) -> int:
    try:
        if value in ("", None):
            raise ValueError("empty")
        return int(float(value))
    except Exception:
        return default


def _normalize_string_list(value: Any, *, limit: int = 12) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _normalize_selection(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            label = str(item.get("label") or item.get("ref") or item.get("name") or "").strip()
            detail = str(
                item.get("detail")
                or item.get("package")
                or item.get("description")
                or ""
            ).strip()
        else:
            label = str(item).strip()
            detail = ""
        if not label or label in seen:
            continue
        seen.add(label)
        out.append({"label": label, "detail": detail})
    return out


def _merge_selection(primary: list[dict[str, str]], fallback: list[dict[str, str]]) -> list[dict[str, str]]:
    merged: dict[str, dict[str, str]] = {item["label"]: dict(item) for item in fallback if item.get("label")}
    for item in primary:
        label = item.get("label", "")
        if not label:
            continue
        merged[label] = {
            "label": label,
            "detail": item.get("detail") or merged.get(label, {}).get("detail", ""),
        }
    return list(merged.values())


def _classify_net(net_name: str) -> str:
    name = (net_name or "").strip().strip('"')
    if not name or _NC_NET_RE.fullmatch(name):
        return "nc"
    if _GROUND_NET_RE.search(name):
        return "ground"
    if _POWER_NET_RE.search(name):
        return "power"
    if _CLOCK_NET_RE.search(name):
        return "clock"
    return "signal"


def _is_signal_layer(kind: str, name: str) -> bool:
    kind_norm = (kind or "").strip().lower()
    name_norm = (name or "").strip().lower()
    if any(token in kind_norm for token in ("dielectric", "mask", "paste", "silk", "mechanical")):
        return False
    if any(token in kind_norm for token in ("signal", "copper", "conductor", "routing")):
        return True
    return any(token in name_norm for token in ("top", "bottom", "sig", "art"))


def _extract_rule_bga_selection(board_text: str) -> list[dict[str, str]]:
    text_fallback = _extract_text_bga_selection(board_text)
    if not _service:
        return text_fallback
    try:
        bga_list = _service.extract_bga_from_txt(qiyun_board_text=board_text)
    except Exception as exc:
        logger.warning("Rule-based BGA fallback failed: %s", exc)
        return text_fallback
    selection = [entry.to_dict() for entry in bga_list]
    return selection or text_fallback


def _extract_text_bga_selection(board_text: str) -> list[dict[str, str]]:
    """Lightweight fallback for Qiyun/Allegro-style component blocks."""
    if not board_text:
        return []

    component_matches = list(re.finditer(r'(?m)^\s*\(component\s+"([^"]+)"', board_text))
    selection: list[dict[str, str]] = []
    seen: set[str] = set()

    for index, match in enumerate(component_matches):
        label = match.group(1).strip()
        if not label or label in seen:
            continue
        end = _find_sexpr_end(board_text, match.start())
        if end is None:
            end = component_matches[index + 1].start() if index + 1 < len(component_matches) else len(board_text)
        block = board_text[match.start():end]
        part_match = re.search(r'\(part\s+"([^"]+)"', block)
        footprint_match = re.search(r'\(footprint\s+"([^"]+)"', block)
        part = part_match.group(1).strip() if part_match else ""
        footprint = footprint_match.group(1).strip() if footprint_match else ""
        package_text = " ".join(item for item in (footprint, part) if item)
        if "bga" not in package_text.lower():
            continue

        pin_count = len(re.findall(r'(?m)^\s*\(pin(?:\s|$)', block))
        package_name = footprint or part or "BGA"
        detail = f"{package_name} ({pin_count} pins)" if pin_count else package_name
        selection.append({"label": label, "detail": detail})
        seen.add(label)

    return selection


def _find_sexpr_end(text: str, start: int) -> Optional[int]:
    depth = 0
    in_string = False
    escaped = False
    saw_open = False

    for pos in range(start, len(text)):
        char = text[pos]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "(":
            depth += 1
            saw_open = True
            continue
        if char == ")" and saw_open:
            depth -= 1
            if depth == 0:
                return pos + 1
    return None


def _summarize_board_model(board_text: str) -> dict[str, Any]:
    if not _parse_txt_to_board_model:
        return {}

    board = _parse_txt_to_board_model(board_text)
    layer_items = []
    signal_layers = []
    for layer in getattr(board, "layers", []) or []:
        name = str(getattr(layer, "txt_name", "") or getattr(layer, "kicad_name", "")).strip()
        kind = str(getattr(layer, "kind", "")).strip()
        thickness = getattr(layer, "thickness_mil", None)
        item = {
            "name": name,
            "kind": kind,
            "thicknessMil": thickness,
        }
        layer_items.append(item)
        if _is_signal_layer(kind, name):
            signal_layers.append(name)

    package_counter: Counter[str] = Counter()
    bga_components: list[dict[str, Any]] = []
    for comp in getattr(board, "components", []) or []:
        footprint = str(getattr(comp, "footprint", "") or "").strip()
        part = str(getattr(comp, "part", "") or "").strip()
        ref = str(getattr(comp, "ref", "") or "").strip()
        pins = getattr(comp, "pins", []) or []
        package_key = footprint or part or "UNKNOWN"
        package_counter[package_key] += 1
        if "bga" in f"{footprint} {part}".lower():
            bga_components.append(
                {
                    "label": ref,
                    "detail": f"{package_key} ({len(pins)} pins)" if package_key else f"BGA {len(pins)}pin",
                    "pinCount": len(pins),
                    "locationMil": {"x": getattr(comp, "x", 0), "y": getattr(comp, "y", 0)},
                }
            )

    net_names = []
    try:
        net_names = list(board.all_net_names())
    except Exception:
        net_names = []

    power_nets = [net for net in net_names if _classify_net(net) == "power"][:20]
    ground_nets = [net for net in net_names if _classify_net(net) == "ground"][:20]
    clock_nets = [net for net in net_names if _classify_net(net) == "clock"][:20]
    signal_count = sum(1 for net in net_names if _classify_net(net) == "signal")
    nc_count = sum(1 for net in net_names if _classify_net(net) == "nc")

    return {
        "componentCount": len(getattr(board, "components", []) or []),
        "viaCount": len(getattr(board, "vias", []) or []),
        "wireCount": len(getattr(board, "wires", []) or []),
        "layerCount": len(layer_items),
        "layers": layer_items,
        "signalLayers": signal_layers,
        "topPackages": [
            {"name": name, "count": count}
            for name, count in package_counter.most_common(12)
        ],
        "bgaCandidates": bga_components,
        "netSummary": {
            "powerNets": power_nets,
            "groundNets": ground_nets,
            "clockNets": clock_nets,
            "signalNetCount": signal_count,
            "ncNetCount": nc_count,
        },
    }


def _build_board_context(board_text: str, token_counter: Any = None) -> dict[str, Any]:
    if not all((_service, _parse_board_objects, _pack_objects, _limit_chunks, _render_context_chunks)):
        raise RuntimeError("pcb_chunk_service chunking dependencies are unavailable")

    config = getattr(_service, "chunk_config", None)
    chunk_input_text = board_text
    if _txt_to_kicad:
        try:
            chunk_input_text = _txt_to_kicad(board_text, stem="board_context")
        except Exception as exc:
            logger.warning(
                "Failed converting PCB layout text to KiCad before chunking; "
                "falling back to raw board text: %s",
                exc,
            )
            chunk_input_text = board_text

    objects = list(_parse_board_objects(chunk_input_text))

    global_objects = []
    component_objects = []
    routing_objects = []
    other_objects = []

    for obj in objects:
        if obj.kind in _GLOBAL_KINDS or obj.kind == "net":
            global_objects.append(obj)
        elif obj.kind in {"module", "footprint"}:
            component_objects.append(obj)
        elif obj.kind in {"segment", "via", "zone", "arc"}:
            routing_objects.append(obj)
        else:
            other_objects.append(obj)

    chunk_chars = max(1024, int(getattr(config, "chunk_chars", 12000)))
    chunk_tokens = max(256, int(getattr(config, "chunk_tokens", 2048)))
    max_context_chars = max(chunk_chars, int(getattr(config, "max_context_chars", 60000)))
    max_context_tokens = max(chunk_tokens, int(getattr(config, "max_context_tokens", 14000)))
    max_chunks = max(1, int(getattr(config, "max_chunks", 8)))

    chunks = []
    if global_objects:
        chunks.extend(
            _pack_objects(
                name_prefix="global",
                reason="board header, layer stack, setup, and net declarations",
                objects=global_objects,
                chunk_chars=chunk_chars,
                chunk_tokens=chunk_tokens,
                token_counter=token_counter,
            )
        )
    if component_objects:
        chunks.extend(
            _pack_objects(
                name_prefix="components",
                reason="component and footprint definitions, including candidate BGAs",
                objects=component_objects,
                chunk_chars=chunk_chars,
                chunk_tokens=chunk_tokens,
                token_counter=token_counter,
            )
        )
    if routing_objects:
        chunks.extend(
            _pack_objects(
                name_prefix="routing",
                reason="vias, segments, arcs, and zones related to routing context",
                objects=routing_objects,
                chunk_chars=chunk_chars,
                chunk_tokens=chunk_tokens,
                token_counter=token_counter,
            )
        )
    if other_objects:
        chunks.extend(
            _pack_objects(
                name_prefix="other",
                reason="remaining top-level board objects",
                objects=other_objects,
                chunk_chars=chunk_chars,
                chunk_tokens=chunk_tokens,
                token_counter=token_counter,
            )
        )

    limited = _limit_chunks(
        chunks,
        max_context_chars=max_context_chars,
        max_context_tokens=max_context_tokens,
        max_chunks=max_chunks,
        token_counter=token_counter,
    )
    context_text = _render_context_chunks(limited)
    stats = {
        "topLevelObjectCount": len(objects),
        "globalObjectCount": len(global_objects),
        "componentObjectCount": len(component_objects),
        "routingObjectCount": len(routing_objects),
        "otherObjectCount": len(other_objects),
        "chunkCount": len(limited),
        "contextChars": sum(len(chunk.text) for chunk in limited),
        "contextTokens": sum((chunk.token_count or 0) for chunk in limited),
        "maxContextChars": max_context_chars,
        "maxContextTokens": max_context_tokens,
        "maxChunks": max_chunks,
        "chunkChars": chunk_chars,
        "chunkTokens": chunk_tokens,
    }
    return {
        "chunks": limited,
        "contextText": context_text,
        "stats": stats,
    }


def _resolve_model_runtime_config() -> dict[str, str]:
    base_url = ""
    api_key = ""
    model = ""

    try:
        from hermes_cli.config import load_config

        config = load_config() or {}
        model_config = config.get("model", {}) if isinstance(config, dict) else {}
        if isinstance(model_config, dict):
            model = str(model_config.get("default") or model_config.get("model") or "").strip()
            base_url = str(model_config.get("base_url") or base_url).strip()
            api_key = str(model_config.get("api_key") or api_key).strip()
    except Exception:
        pass

    env_base_url = os.getenv("OPENAI_BASE_URL", "").strip()
    env_api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if env_base_url:
        base_url = env_base_url
    if env_api_key:
        api_key = env_api_key

    local_cfg = _load_project_config_ini()
    if local_cfg and local_cfg.has_section("model"):
        local_model = local_cfg.get("model", "model", fallback="").strip()
        local_base_url = local_cfg.get("model", "base_url", fallback="").strip()
        local_api_key = local_cfg.get("model", "api_key", fallback="").strip()
        if local_model:
            model = local_model
        if local_base_url:
            base_url = local_base_url
        if local_api_key:
            api_key = local_api_key

    if not model:
        raise RuntimeError("model.default is not configured for pcb long-context analysis")
    if not base_url:
        raise RuntimeError("OPENAI_BASE_URL/model.base_url is not configured for pcb long-context analysis")

    return {
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
    }


def _build_board_analysis_prompt(
    *,
    parser_hints: dict[str, Any],
    rule_selection: list[dict[str, str]],
    context_text: str,
) -> Any:
    stackup_preview = []
    for layer in parser_hints.get("layers", [])[:12]:
        name = layer.get("name") or ""
        kind = layer.get("kind") or ""
        thickness = layer.get("thicknessMil")
        thickness_text = "" if thickness in ("", None) else f", {thickness} mil"
        stackup_preview.append(f"{name}: {kind}{thickness_text}")

    user_prompt = (
        "任务：基于下面的 PCB 长上下文，识别 BGA 候选、总结层叠/网络/封装特征，"
        "并给出用于后续 fanout 参数生成的受控上下文。\n\n"
        "要求：\n"
        "1. `selection` 只保留明确看起来是 BGA 的器件。\n"
        "2. `boardSummary.stackupSummary` 用简短中文或中英混合短句。\n"
        "3. `fanoutContext` 只给推荐层、线宽、间距、优先级和简短理由，不要生成 route 代码。\n"
        "4. 如果规则提取结果与上下文冲突，以版图上下文为准，但尽量保留可靠的 BGA 位号。\n\n"
        f"规则提取得到的 BGA 初筛：\n{json.dumps(rule_selection, ensure_ascii=False, indent=2)}\n\n"
        f"解析器结构化摘要：\n{json.dumps({'stackupPreview': stackup_preview, 'summary': parser_hints}, ensure_ascii=False, indent=2)}\n\n"
        f"长上下文分块：\n{context_text}\n"
    )
    return _PromptBundle(system=_BOARD_ANALYSIS_SYSTEM_PROMPT, user=user_prompt)


def _extract_first_json_object(text: str) -> dict[str, Any]:
    if not text or not text.strip():
        raise ValueError("model returned empty response")

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        value = json.loads(cleaned)
        if isinstance(value, dict):
            return value
    except Exception:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"unable to parse JSON object from response: {cleaned[:200]}")

    snippet = cleaned[start : end + 1]
    value = json.loads(snippet)
    if not isinstance(value, dict):
        raise ValueError("model response JSON is not an object")
    return value


def _build_fallback_board_summary(parser_hints: dict[str, Any]) -> dict[str, Any]:
    layers = parser_hints.get("layers", []) or []
    package_items = parser_hints.get("topPackages", []) or []
    net_summary = parser_hints.get("netSummary", {}) or {}
    return {
        "stackupSummary": [
            f"{layer.get('name')}: {layer.get('kind')}"
            for layer in layers[:12]
            if layer.get("name")
        ],
        "packageHints": [
            f"{item.get('name')} x{item.get('count')}"
            for item in package_items[:10]
            if item.get("name")
        ],
        "netSummary": {
            "powerNets": _normalize_string_list(net_summary.get("powerNets"), limit=20),
            "groundNets": _normalize_string_list(net_summary.get("groundNets"), limit=20),
            "clockNets": _normalize_string_list(net_summary.get("clockNets"), limit=20),
            "signalNetCount": _safe_int(net_summary.get("signalNetCount"), 0),
            "ncNetCount": _safe_int(net_summary.get("ncNetCount"), 0),
        },
    }


def _build_fallback_fanout_context(parser_hints: dict[str, Any]) -> dict[str, Any]:
    signal_layers = _normalize_string_list(parser_hints.get("signalLayers"), limit=4)
    if len(signal_layers) < 2:
        layer_names = [
            layer.get("name")
            for layer in parser_hints.get("layers", [])
            if _is_signal_layer(layer.get("kind", ""), layer.get("name", ""))
        ]
        signal_layers = _normalize_string_list(layer_names, limit=4)

    escape_layers = signal_layers[:2] if signal_layers else []
    return {
        "recommendedEscapeLayers": escape_layers,
        "recommendedLineWidth": 4,
        "recommendedLineSpacing": 3,
        "prioritySuggestion": ["ground", "power", "clock", "signal"],
        "rationale": "基于解析到的层叠与线网类型生成的保守默认建议。",
    }


def _normalize_board_analysis(
    analysis: dict[str, Any],
    *,
    parser_hints: dict[str, Any],
    rule_selection: list[dict[str, str]],
    source: str,
    fallback_used: bool,
    model_meta: dict[str, Any] | None = None,
    context_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fallback_summary = _build_fallback_board_summary(parser_hints)
    fallback_fanout = _build_fallback_fanout_context(parser_hints)

    normalized_selection = _merge_selection(
        _normalize_selection(analysis.get("selection")),
        rule_selection,
    )

    raw_board_summary = analysis.get("boardSummary") if isinstance(analysis.get("boardSummary"), dict) else {}
    raw_net_summary = raw_board_summary.get("netSummary") if isinstance(raw_board_summary.get("netSummary"), dict) else {}
    board_summary = {
        "stackupSummary": _normalize_string_list(
            raw_board_summary.get("stackupSummary") or fallback_summary["stackupSummary"],
            limit=16,
        ),
        "packageHints": _normalize_string_list(
            raw_board_summary.get("packageHints") or fallback_summary["packageHints"],
            limit=16,
        ),
        "netSummary": {
            "powerNets": _normalize_string_list(
                raw_net_summary.get("powerNets") or fallback_summary["netSummary"]["powerNets"],
                limit=20,
            ),
            "groundNets": _normalize_string_list(
                raw_net_summary.get("groundNets") or fallback_summary["netSummary"]["groundNets"],
                limit=20,
            ),
            "clockNets": _normalize_string_list(
                raw_net_summary.get("clockNets") or fallback_summary["netSummary"]["clockNets"],
                limit=20,
            ),
            "signalNetCount": _safe_int(
                raw_net_summary.get("signalNetCount"),
                fallback_summary["netSummary"]["signalNetCount"],
            ),
            "ncNetCount": _safe_int(
                raw_net_summary.get("ncNetCount"),
                fallback_summary["netSummary"]["ncNetCount"],
            ),
        },
    }

    raw_fanout = analysis.get("fanoutContext") if isinstance(analysis.get("fanoutContext"), dict) else {}
    fanout_context = {
        "recommendedEscapeLayers": _normalize_string_list(
            raw_fanout.get("recommendedEscapeLayers")
            or raw_fanout.get("escapeLayers")
            or fallback_fanout["recommendedEscapeLayers"],
            limit=4,
        ),
        "recommendedLineWidth": _safe_int(
            raw_fanout.get("recommendedLineWidth") or raw_fanout.get("lineWidth"),
            fallback_fanout["recommendedLineWidth"],
        ),
        "recommendedLineSpacing": _safe_int(
            raw_fanout.get("recommendedLineSpacing") or raw_fanout.get("lineSpacing"),
            fallback_fanout["recommendedLineSpacing"],
        ),
        "prioritySuggestion": _normalize_string_list(
            raw_fanout.get("prioritySuggestion") or fallback_fanout["prioritySuggestion"],
            limit=8,
        ),
        "rationale": str(raw_fanout.get("rationale") or fallback_fanout["rationale"]).strip(),
    }

    result = {
        "selection": normalized_selection,
        "boardSummary": board_summary,
        "fanoutContext": fanout_context,
        "source": source,
        "fallbackUsed": fallback_used,
    }
    if context_stats:
        result["contextStats"] = context_stats
    if model_meta:
        result["model"] = {
            "adapter": "openai-compatible",
            "name": str(model_meta.get("model") or model_meta.get("name") or ""),
            "responseId": model_meta.get("response_id") or model_meta.get("responseId"),
            "usage": model_meta.get("usage") or {},
        }
    return result


def _analyze_board_with_model(board_text: str) -> dict[str, Any]:
    if not _long_context_ready():
        raise RuntimeError("pcb long-context analysis dependencies are unavailable")

    rule_selection = _extract_rule_bga_selection(board_text)
    parser_hints = _summarize_board_model(board_text)
    runtime = _resolve_model_runtime_config()

    adapter = _OpenAICompatibleChatAdapter(
        base_url=runtime["base_url"],
        model=runtime["model"],
        api_key=runtime["api_key"],
        timeout_s=300,
    )
    token_counter = adapter.get_token_counter()
    context_result = _build_board_context(board_text, token_counter=token_counter)
    prompt_bundle = _build_board_analysis_prompt(
        parser_hints=parser_hints,
        rule_selection=rule_selection,
        context_text=context_result["contextText"],
    )
    generation_config = _GenerationConfig(max_new_tokens=1800, temperature=0.1)
    raw_text, model_meta = adapter.generate(prompt_bundle, generation_config)
    analysis = _extract_first_json_object(raw_text)
    return _normalize_board_analysis(
        analysis,
        parser_hints=parser_hints,
        rule_selection=rule_selection,
        source="llm_long_context",
        fallback_used=False,
        model_meta=model_meta,
        context_stats=context_result["stats"],
    )


_CACHED_PROJECT_DATA_SENTINELS = {
    "__CACHED_PROJECT_DATA__",
    "[CACHED_PROJECT_DATA]",
    "CACHED_PROJECT_DATA",
}


def _resolve_board_text(board_text: str, session_id: Optional[str] = None) -> str:
    candidate = (board_text or "").strip()
    if candidate and candidate not in _CACHED_PROJECT_DATA_SENTINELS:
        return board_text

    try:
        from tools.pcb_tools import WebSocketTransportSingleton

        cached = WebSocketTransportSingleton.get_instance().get_cached_project_data(
            session_id=session_id
        )
        if cached:
            logger.info(
                "pcb_extract_bga loaded board_text from websocket cache: session=%s chars=%d",
                session_id,
                len(cached),
            )
            return cached
    except Exception as exc:
        logger.warning("Failed to load cached board_text for pcb_extract_bga: %s", exc)

    return board_text or ""


def _extract_bga(board_text: str, session_id: Optional[str] = None) -> str:
    board_text = _resolve_board_text(board_text, session_id=session_id)
    if not board_text or not board_text.strip():
        return json.dumps({"error": "board_text is empty"}, ensure_ascii=False)

    parser_hints: dict[str, Any] = {}
    rule_selection = _extract_rule_bga_selection(board_text)
    try:
        parser_hints = _summarize_board_model(board_text)
    except Exception as exc:
        logger.warning("Failed to build parser hints for pcb_extract_bga: %s", exc)

    try:
        result = _analyze_board_with_model(board_text)
        result = _normalize_board_analysis(
            result if isinstance(result, dict) else {},
            parser_hints=parser_hints,
            rule_selection=rule_selection,
            source=str(result.get("source") or "llm_long_context") if isinstance(result, dict) else "llm_long_context",
            fallback_used=bool(result.get("fallbackUsed")) if isinstance(result, dict) else False,
            model_meta=result.get("model", {}) if isinstance(result, dict) else None,
            context_stats=result.get("contextStats") if isinstance(result, dict) else None,
        )
        if not result.get("selection") and not result.get("boardSummary"):
            raise RuntimeError("board analysis returned empty payload")
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        logger.error("pcb_extract_bga long-context analysis failed, fallback to rule path: %s", exc)
        fallback = _normalize_board_analysis(
            {},
            parser_hints=parser_hints,
            rule_selection=rule_selection,
            source="rule_fallback",
            fallback_used=True,
        )
        if not fallback["selection"]:
            fallback["message"] = "未识别到 BGA 封装元件"
        else:
            fallback["message"] = f"长上下文分析失败，已回退到规则提取：{exc}"
        return json.dumps(fallback, ensure_ascii=False)


registry.register(
    name="pcb_extract_bga",
    toolset="pcb",
    schema={
        "name": "pcb_extract_bga",
        "description": (
            "对启云方格式 PCB 版图文本执行长上下文板分析，主链路返回 BGA 候选 selection、"
            "层叠/封装/网络摘要 boardSummary，以及 fanoutContext。"
            "如长上下文分析失败，会自动回退到规则 BGA 提取。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "board_text": {
                    "type": "string",
                    "description": (
                        "getProjectData 返回的启云方格式 PCB 版图 S 表达式文本。"
                        "如果系统提示版图已缓存，可传 __CACHED_PROJECT_DATA__ 或省略该字段。"
                    ),
                }
            },
            "required": [],
        },
    },
    handler=lambda args, **kwargs: _extract_bga(
        args.get("board_text", ""),
        session_id=kwargs.get("session_id"),
    ),
    check_fn=lambda: _AVAILABLE and _config_enabled(),
)
