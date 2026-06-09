"""Stage-specific model runtime helpers for PCB reroute/report flows."""

from __future__ import annotations

import configparser
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

STAGE_REROUTE = "reroute"
STAGE_EXPLAIN = "explain"
STAGE_TOOL_PLANNING_CHAT = "tool_planning_chat"

DEFAULT_BASE_URL = "https://wishub-x5.ctyun.cn/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_QWEN_MODEL = "qwen/qwen3.6-plus"
BUILTIN_OPENROUTER_API_KEY = (
    "sk-or-v1-"
    "b321af7637e3ee0b35a9402615d35b92d3175b339769b698f22350da2b136bd7"
)
DEFAULT_STAGE_MODELS = {
    STAGE_REROUTE: "",
    STAGE_EXPLAIN: "",
    STAGE_TOOL_PLANNING_CHAT: "",
}
DEFAULT_PROXY_BYPASS_HOSTS = ("wishub-x5.ctyun.cn",)

_OPENAI_CHAT_COMPLETIONS_SUFFIX_RE = re.compile(r"/chat/completions/?$", re.IGNORECASE)
_THINK_BLOCK_RE = re.compile(r"(?is)<think(?:ing)?\b[^>]*>.*?</think(?:ing)?>")
_LOCAL_ENV_LOADED = False


def normalize_openai_base_url(value: str) -> str:
    text = str(value or "").strip().strip("`'\"，,;；")
    text = _OPENAI_CHAT_COMPLETIONS_SUFFIX_RE.sub("", text.rstrip("/"))
    return text.rstrip("/")


def _candidate_project_config_paths() -> list[Path]:
    paths: list[Path] = []
    bundled_root = getattr(sys, "_MEIPASS", None)
    if bundled_root:
        paths.append(Path(bundled_root) / "config.ini")
    paths.append(Path(__file__).resolve().parents[1] / "config.ini")
    return paths


def _candidate_model_config_paths(project_config_paths: Iterable[Path] | None = None) -> list[Path]:
    paths: list[Path] = []
    env_path = os.getenv("PCB_AGENT_MODEL_CONFIG_TXT", "").strip()
    if env_path:
        paths.append(Path(env_path))
    for config_path in project_config_paths or _candidate_project_config_paths():
        paths.append(Path(config_path).parent / "model_config.txt")
    paths.append(Path(__file__).resolve().parents[1] / "model_config.txt")
    return list(dict.fromkeys(paths))


def _candidate_model_doc_paths(project_config_paths: Iterable[Path] | None = None) -> list[Path]:
    paths: list[Path] = []
    for config_path in project_config_paths or _candidate_project_config_paths():
        paths.append(Path(config_path).parent / "share" / "天翼云部署模型使用说明.md")
    return paths


def _candidate_local_env_paths() -> list[Path]:
    root = Path(__file__).resolve().parents[1]
    return [root / ".env.local", root / ".env"]


def _load_local_env_files_once() -> None:
    global _LOCAL_ENV_LOADED
    if _LOCAL_ENV_LOADED:
        return
    _LOCAL_ENV_LOADED = True
    if os.getenv("PCB_MODEL_RUNTIME_DISABLE_DOTENV", "").strip().lower() in ("1", "true", "yes", "on"):
        return
    for path in _candidate_local_env_paths():
        if not path.exists() or not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception as exc:
            logger.warning("Failed reading local env file %s: %s", path, exc)
            continue
        for line in lines:
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.split("=", 1)
            key = key.strip()
            if not key or key in os.environ:
                continue
            os.environ[key] = value.strip().strip("`'\"")


def _env_first(*names: str) -> str:
    _load_local_env_files_once()
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _looks_like_real_secret(value: str) -> bool:
    text = str(value or "").strip().strip("`'\"")
    if len(text) < 12:
        return False
    lowered = text.lower()
    return not any(
        token in lowered
        for token in (
            "<",
            ">",
            "your",
            "xxx",
            "placeholder",
            "示例",
            "这里填写",
            "os.getenv",
            "getenv",
            "app_key",
            "api_key",
        )
    )


def _looks_like_model_name(value: str) -> bool:
    text = str(value or "").strip().strip("`'\"")
    if not text:
        return False
    lowered = text.lower()
    return not any(token in lowered for token in ("os.getenv", "getenv", "placeholder", "示例"))


def _load_project_config_ini(project_config_paths: Iterable[Path] | None = None) -> configparser.ConfigParser | None:
    parser = configparser.ConfigParser()
    for path in project_config_paths or _candidate_project_config_paths():
        config_path = Path(path)
        if not config_path.exists():
            continue
        try:
            parser.read(config_path, encoding="utf-8-sig")
            return parser
        except Exception as exc:
            logger.warning("Failed reading project config.ini from %s: %s", config_path, exc)
    return None


def _split_config_list(value: str) -> list[str]:
    parts = re.split(r"[,，;\s]+", str(value or "").strip())
    return [part.strip().lower() for part in parts if part.strip()]


def _host_matches_bypass(host: str, patterns: Iterable[str]) -> bool:
    normalized_host = str(host or "").strip().lower().strip(".")
    if not normalized_host:
        return False
    for pattern in patterns:
        normalized_pattern = str(pattern or "").strip().lower().strip(".")
        if not normalized_pattern:
            continue
        if normalized_pattern == "*":
            return True
        if normalized_pattern.startswith("*."):
            suffix = normalized_pattern[1:]
            if normalized_host.endswith(suffix):
                return True
            continue
        if normalized_host == normalized_pattern or normalized_host.endswith(f".{normalized_pattern}"):
            return True
    return False


def _network_config() -> dict[str, Any]:
    parser = _load_project_config_ini()
    section = "network"
    proxy_mode = "auto"
    bypass_hosts = list(DEFAULT_PROXY_BYPASS_HOSTS)
    http_proxy = ""
    https_proxy = ""
    if parser is not None and parser.has_section(section):
        configured_mode = parser.get(section, "proxy_mode", fallback=proxy_mode).strip().lower()
        if configured_mode in ("auto", "direct", "proxy"):
            proxy_mode = configured_mode
        configured_bypass = _split_config_list(parser.get(section, "bypass_hosts", fallback=""))
        if configured_bypass:
            bypass_hosts.extend(configured_bypass)
        http_proxy = parser.get(section, "http_proxy", fallback="").strip()
        https_proxy = parser.get(section, "https_proxy", fallback="").strip()
    return {
        "proxy_mode": proxy_mode,
        "bypass_hosts": list(dict.fromkeys(host.lower() for host in bypass_hosts if host)),
        "http_proxy": http_proxy,
        "https_proxy": https_proxy,
    }


def _open_chat_request(req: urlrequest.Request, *, timeout_s: float, base_url: str):
    config = _network_config()
    host = urlparse(base_url).hostname or urlparse(req.full_url).hostname or ""
    mode = str(config.get("proxy_mode") or "auto").lower()
    proxies = {
        scheme: proxy
        for scheme, proxy in (
            ("http", str(config.get("http_proxy") or "").strip()),
            ("https", str(config.get("https_proxy") or "").strip()),
        )
        if proxy
    }

    use_direct = mode == "direct" or (
        mode == "auto" and _host_matches_bypass(host, config.get("bypass_hosts") or ())
    )
    if use_direct:
        opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))
        return opener.open(req, timeout=timeout_s)

    if proxies:
        opener = urlrequest.build_opener(urlrequest.ProxyHandler(proxies))
        return opener.open(req, timeout=timeout_s)

    return urlrequest.urlopen(req, timeout=timeout_s)


def _stage_doc_section(text: str, stage: str) -> str:
    if stage == STAGE_EXPLAIN:
        patterns = (
            r"(?is)##\s*2\.\s*可解释.*?(?=\n##\s*\d+\.|\Z)",
            r"(?is)##[^\n]*(?:explain|报告|可解释)[^\n]*.*?(?=\n##\s*\d+\.|\Z)",
        )
    elif stage == STAGE_REROUTE:
        patterns = (
            r"(?is)##\s*1\.\s*拆线重布.*?(?=\n##\s*\d+\.|\Z)",
            r"(?is)##[^\n]*(?:reroute|重布|拆线)[^\n]*.*?(?=\n##\s*\d+\.|\Z)",
        )
    else:
        patterns = ()

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(0)
    return text


def extract_stage_runtime_config_from_doc(
    stage: str,
    *,
    doc_paths: Iterable[Path] | None = None,
    project_config_paths: Iterable[Path] | None = None,
) -> dict[str, str]:
    result = {"model": "", "base_url": "", "api_key": ""}
    paths = list(doc_paths or _candidate_model_doc_paths(project_config_paths))
    for path in paths:
        doc_path = Path(path)
        if not doc_path.exists():
            continue
        try:
            text = doc_path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed reading Tianyi model usage doc from %s: %s", doc_path, exc)
            continue

        section = _stage_doc_section(text, stage)
        model_match = re.search(r"\|\s*modelId\s*\|\s*`?([A-Za-z0-9._:-]+)`?", section)
        if model_match and _looks_like_model_name(model_match.group(1)):
            result["model"] = model_match.group(1).strip()
        if not result["model"]:
            env_name = "CTYUN_EXPLAIN_MODEL" if stage == STAGE_EXPLAIN else "CTYUN_MODEL"
            model_match = re.search(rf"(?im){env_name}\s*=\s*[`'\"]?([A-Za-z0-9._:-]+)", text)
            if model_match and _looks_like_model_name(model_match.group(1)):
                result["model"] = model_match.group(1).strip()
        if not result["model"]:
            generic_model_match = re.search(
                r"(?im)^\s*(?:model|OPENAI_MODEL|模型(?:名称)?)\s*[:=：]\s*[`'\"]?([A-Za-z0-9._:-]+)",
                section,
            )
            if generic_model_match and _looks_like_model_name(generic_model_match.group(1)):
                result["model"] = generic_model_match.group(1).strip()

        base_url_match = re.search(r"\|\s*OpenAI base_url\s*\|\s*`?(https?://[^|`\s]+)`?", section)
        if not base_url_match:
            env_name = "CTYUN_EXPLAIN_BASE_URL" if stage == STAGE_EXPLAIN else "CTYUN_BASE_URL"
            base_url_match = re.search(rf"(?im){env_name}\s*=\s*[`'\"]?(https?://[^\s`'\"]+)", text)
        if not base_url_match:
            base_url_match = re.search(r"https?://[^\s`'\"]+/v1(?:/chat/completions)?", section)
        if base_url_match:
            result["base_url"] = normalize_openai_base_url(base_url_match.group(1 if base_url_match.groups() else 0))

        key_match = re.search(r"\|\s*App Key\s*\|\s*([^|\s`'\"]+)", section)
        if not key_match:
            env_name = "CTYUN_EXPLAIN_APP_KEY" if stage == STAGE_EXPLAIN else "CTYUN_APP_KEY"
            key_match = re.search(rf"(?im){env_name}\s*=\s*[`'\"]?([^\s`'\"]+)", text)
        if key_match:
            candidate = key_match.group(1).strip().strip("`'\"")
            if _looks_like_real_secret(candidate):
                result["api_key"] = candidate

        return result
    return result


def _merge_config(target: dict[str, str], source: dict[str, str]) -> None:
    for key in ("model", "base_url", "api_key"):
        value = str(source.get(key) or "").strip()
        if value:
            target[key] = normalize_openai_base_url(value) if key == "base_url" else value


def _runtime_from_env(stage: str, *, stage_specific: bool) -> dict[str, str]:
    if stage == STAGE_EXPLAIN:
        if stage_specific:
            return {
                "model": _env_first("CTYUN_EXPLAIN_MODEL", "PCB_EXPLAIN_MODEL", "EXPLAIN_MODEL"),
                "base_url": _env_first("CTYUN_EXPLAIN_BASE_URL", "PCB_EXPLAIN_BASE_URL", "EXPLAIN_BASE_URL", "CTYUN_BASE_URL"),
                "api_key": _env_first("CTYUN_EXPLAIN_APP_KEY", "CTYUN_EXPLAIN_API_KEY", "PCB_EXPLAIN_API_KEY", "EXPLAIN_API_KEY"),
            }
        return {}

    if stage == STAGE_TOOL_PLANNING_CHAT and stage_specific:
        return {}

    if stage_specific:
        return {
            "model": _env_first("CTYUN_REROUTE_MODEL", "PCB_REROUTE_MODEL"),
            "base_url": _env_first("CTYUN_REROUTE_BASE_URL", "PCB_REROUTE_BASE_URL"),
            "api_key": _env_first("CTYUN_REROUTE_APP_KEY", "CTYUN_REROUTE_API_KEY", "PCB_REROUTE_API_KEY"),
        }
    return {
        "model": _env_first("CTYUN_MODEL", "OPENAI_MODEL"),
        "base_url": _env_first("CTYUN_BASE_URL", "OPENAI_BASE_URL"),
        "api_key": _env_first("CTYUN_APP_KEY", "CTYUN_API_KEY", "OPENAI_API_KEY"),
    }


def _runtime_from_openrouter_env() -> dict[str, str]:
    api_key = _env_first("OPENROUTER_API_KEY")
    if not api_key:
        return {}
    return {
        "model": _env_first("PCB_OPENROUTER_QWEN_MODEL", "OPENROUTER_MODEL") or OPENROUTER_QWEN_MODEL,
        "base_url": _env_first("OPENROUTER_BASE_URL") or OPENROUTER_BASE_URL,
        "api_key": api_key,
    }


def _runtime_from_builtin_openrouter() -> dict[str, str]:
    if not BUILTIN_OPENROUTER_API_KEY:
        return {}
    return {
        "model": OPENROUTER_QWEN_MODEL,
        "base_url": OPENROUTER_BASE_URL,
        "api_key": BUILTIN_OPENROUTER_API_KEY,
    }


def _runtime_from_project_config(
    stage: str,
    project_config_paths: Iterable[Path] | None = None,
) -> dict[str, str]:
    parser = _load_project_config_ini(project_config_paths)
    if not parser:
        return {}

    if stage == STAGE_TOOL_PLANNING_CHAT:
        sections = [
            "model",
            "tool-planning-chat-model",
            "tool_planning_chat_model",
            "tool_planning_chat",
        ]
    elif stage == STAGE_REROUTE:
        sections = [
            "model",
            "reroute-model",
            "reroute_model",
            "reroute",
        ]
    elif stage == STAGE_EXPLAIN:
        sections = [
            "explain-model",
            "explain_model",
            "explain",
        ]
    else:
        sections = [f"{stage}_model", stage]
    result = {"model": "", "base_url": "", "api_key": ""}
    for section in sections:
        if not parser.has_section(section):
            continue
        model = parser.get(section, "model", fallback="").strip()
        base_url = parser.get(section, "base_url", fallback="").strip()
        api_key = parser.get(section, "api_key", fallback="").strip()
        _merge_config(result, {"model": model, "base_url": base_url, "api_key": api_key})
    return result


def _runtime_from_model_config_txt(
    stage: str,
    project_config_paths: Iterable[Path] | None = None,
) -> dict[str, str]:
    section_map = {
        STAGE_TOOL_PLANNING_CHAT: (
            "tool-planning-chat-model",
            "tool_planning_chat_model",
            "tool_planning_chat",
            "model",
        ),
        STAGE_REROUTE: (
            "reroute-model",
            "reroute_model",
            "reroute",
        ),
        STAGE_EXPLAIN: (
            "explain-model",
            "explain_model",
            "explain",
        ),
    }
    parser = configparser.ConfigParser()
    for path in _candidate_model_config_paths(project_config_paths):
        if not path.is_file():
            continue
        try:
            parser.read(path, encoding="utf-8-sig")
        except Exception as exc:
            logger.warning("Failed reading PCB model_config.txt from %s: %s", path, exc)
            continue
        result = {"model": "", "base_url": "", "api_key": ""}
        for section in section_map.get(stage, ()):
            if not parser.has_section(section):
                continue
            _merge_config(
                result,
                {
                    "model": parser.get(section, "model", fallback=""),
                    "base_url": parser.get(section, "base_url", fallback=""),
                    "api_key": parser.get(section, "api_key", fallback=""),
                },
            )
        if any(result.values()):
            return result
    return {}


def _runtime_from_global_config() -> dict[str, str]:
    try:
        from hermes_cli.config import load_config

        config = load_config() or {}
    except Exception:
        return {}
    model_config = config.get("model", {}) if isinstance(config, dict) else {}
    if not isinstance(model_config, dict):
        return {}
    return {
        "model": str(model_config.get("default") or model_config.get("model") or "").strip(),
        "base_url": str(model_config.get("base_url") or "").strip(),
        "api_key": str(model_config.get("api_key") or "").strip(),
    }


def resolve_model_runtime(
    stage: str,
    *,
    project_config_paths: Iterable[Path] | None = None,
    doc_paths: Iterable[Path] | None = None,
    require_api_key: bool = False,
) -> dict[str, str]:
    if stage not in (STAGE_REROUTE, STAGE_EXPLAIN, STAGE_TOOL_PLANNING_CHAT):
        raise ValueError(f"unsupported PCB model stage: {stage}")

    runtime = {"model": "", "base_url": "", "api_key": ""}
    if stage in (STAGE_REROUTE, STAGE_TOOL_PLANNING_CHAT):
        _merge_config(runtime, _runtime_from_global_config())
        _merge_config(runtime, _runtime_from_env(stage, stage_specific=False))
    _merge_config(runtime, _runtime_from_project_config(stage, project_config_paths))
    _merge_config(runtime, _runtime_from_model_config_txt(stage, project_config_paths))
    _merge_config(runtime, _runtime_from_env(stage, stage_specific=True))

    doc_config = extract_stage_runtime_config_from_doc(
        stage,
        doc_paths=doc_paths,
        project_config_paths=project_config_paths,
    )
    for key in ("model", "base_url", "api_key"):
        if not runtime.get(key) and doc_config.get(key):
            runtime[key] = doc_config[key]

    disable_openrouter = _env_first("PCB_DISABLE_OPENROUTER_QWEN").lower() in ("1", "true", "yes", "on")
    force_openrouter = _env_first("PCB_FORCE_OPENROUTER_QWEN").lower() in ("1", "true", "yes", "on")
    openrouter_runtime = _runtime_from_openrouter_env()
    if openrouter_runtime and not disable_openrouter and (force_openrouter or not runtime.get("model") or not runtime.get("api_key")):
        _merge_config(runtime, openrouter_runtime)
    elif not disable_openrouter and not runtime.get("model") and not runtime.get("api_key"):
        _merge_config(runtime, _runtime_from_builtin_openrouter())

    if not runtime["model"] and DEFAULT_STAGE_MODELS.get(stage):
        runtime["model"] = DEFAULT_STAGE_MODELS[stage]
    if not runtime["base_url"]:
        runtime["base_url"] = DEFAULT_BASE_URL
    runtime["base_url"] = normalize_openai_base_url(runtime["base_url"])

    if not runtime["model"]:
        if stage == STAGE_EXPLAIN:
            model_hint = "CTYUN_EXPLAIN_MODEL"
        elif stage == STAGE_REROUTE:
            model_hint = "CTYUN_REROUTE_MODEL/CTYUN_MODEL"
        else:
            model_hint = "CTYUN_MODEL/OPENAI_MODEL"
        raise RuntimeError(f"{stage} model is not configured; set {model_hint} or provide it in controlled config")
    if require_api_key and not runtime["api_key"]:
        env_hint = "CTYUN_EXPLAIN_APP_KEY" if stage == STAGE_EXPLAIN else "CTYUN_APP_KEY"
        raise RuntimeError(f"{env_hint} is not configured")
    return runtime


def runtime_disables_thinking(stage: str, base_url: str) -> bool:
    stage_env = "PCB_EXPLAIN_DISABLE_THINKING" if stage == STAGE_EXPLAIN else "PCB_REROUTE_DISABLE_THINKING"
    override = _env_first(stage_env, "PCB_MODEL_DISABLE_THINKING").lower()
    if override:
        return override not in ("0", "false", "no", "off")
    normalized = str(base_url or "").lower()
    return "ctyun.cn" in normalized or "wishub-x5" in normalized


def runtime_sends_disable_thinking_kwargs(stage: str) -> bool:
    stage_env = (
        "PCB_EXPLAIN_DISABLE_THINKING_KWARGS"
        if stage == STAGE_EXPLAIN
        else "PCB_REROUTE_DISABLE_THINKING_KWARGS"
    )
    override = _env_first(stage_env, "PCB_MODEL_DISABLE_THINKING_KWARGS").lower()
    return override in ("1", "true", "yes", "on")


def runtime_uses_no_think_prefix(stage: str, base_url: str) -> bool:
    stage_env = (
        "PCB_EXPLAIN_USE_NO_THINK_PREFIX"
        if stage == STAGE_EXPLAIN
        else "PCB_REROUTE_USE_NO_THINK_PREFIX"
    )
    override = _env_first(stage_env, "PCB_MODEL_USE_NO_THINK_PREFIX").lower()
    if override:
        return override in ("1", "true", "yes", "on")
    if stage == STAGE_REROUTE:
        return False
    return runtime_disables_thinking(stage, base_url)


def runtime_token_parameter(stage: str, base_url: str) -> str:
    stage_env = (
        "PCB_EXPLAIN_TOKEN_PARAMETER"
        if stage == STAGE_EXPLAIN
        else "PCB_REROUTE_TOKEN_PARAMETER"
    )
    override = _env_first(stage_env, "PCB_MODEL_TOKEN_PARAMETER").strip()
    if override in ("max_tokens", "max_completion_tokens"):
        return override
    normalized = str(base_url or "").lower()
    if stage == STAGE_REROUTE and ("ctyun.cn" in normalized or "wishub-x5" in normalized):
        return "max_completion_tokens"
    return "max_tokens"


def ensure_no_think_prefix(text: str) -> str:
    stripped = str(text or "").lstrip()
    if stripped.startswith(("/no_think", "/nothink")):
        return text
    return f"/no_think\n{text}"


def strip_think_blocks(text: str) -> str:
    cleaned = _THINK_BLOCK_RE.sub("", str(text or ""))
    cleaned = re.sub(r"(?is)</think(?:ing)?>", "", cleaned)
    cleaned = re.sub(r"(?is)<think(?:ing)?\b[^>]*>.*", "", cleaned)
    return cleaned.strip()


def _normalize_message_content_for_no_think(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    normalized = [dict(message) for message in messages]
    for index in range(len(normalized) - 1, -1, -1):
        if normalized[index].get("role") != "user":
            continue
        normalized[index]["content"] = ensure_no_think_prefix(str(normalized[index].get("content") or ""))
        break
    return normalized


def chat_completion_text(
    *,
    stage: str,
    messages: list[dict[str, str]],
    runtime: dict[str, str] | None = None,
    max_tokens: int = 2048,
    temperature: float | None = 0.2,
    top_p: float | None = None,
    timeout_s: float = 180,
    stream: bool = False,
    require_api_key: bool = False,
) -> tuple[str, dict[str, Any]]:
    resolved = runtime or resolve_model_runtime(stage, require_api_key=require_api_key)
    if require_api_key and not resolved.get("api_key"):
        env_hint = "CTYUN_EXPLAIN_APP_KEY" if stage == STAGE_EXPLAIN else "CTYUN_APP_KEY"
        raise RuntimeError(f"{env_hint} is not configured")
    base_url = normalize_openai_base_url(resolved["base_url"])
    disable_thinking = runtime_disables_thinking(stage, base_url)
    outgoing_messages = (
        _normalize_message_content_for_no_think(messages)
        if runtime_uses_no_think_prefix(stage, base_url)
        else messages
    )

    payload: dict[str, Any] = {
        "model": resolved["model"],
        "messages": outgoing_messages,
        "stream": stream,
    }
    payload[runtime_token_parameter(stage, base_url)] = max_tokens
    if temperature is not None:
        payload["temperature"] = temperature
    if top_p is not None:
        payload["top_p"] = top_p
    if disable_thinking and runtime_sends_disable_thinking_kwargs(stage):
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    headers = {"Content-Type": "application/json"}
    if resolved.get("api_key"):
        headers["Authorization"] = f"Bearer {resolved['api_key']}"
    req = urlrequest.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with _open_chat_request(req, timeout_s=timeout_s, base_url=base_url) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urlerror.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        if detail:
            raise RuntimeError(f"{stage} model HTTP {exc.code}: {detail}") from exc
        raise RuntimeError(f"{stage} model HTTP {exc.code}") from exc

    data = json.loads(raw)
    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices:
        raise RuntimeError(f"{stage} model returned no choices: {data}")
    message = choices[0].get("message") if isinstance(choices[0], dict) else {}
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, list):
        text = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    else:
        text = str(content or "")
    text = strip_think_blocks(text)
    meta = {
        "stage": stage,
        "base_url": base_url,
        "model": resolved["model"],
        "usage": data.get("usage") if isinstance(data, dict) else {},
        "response_id": data.get("id") if isinstance(data, dict) else None,
    }
    return text, meta
