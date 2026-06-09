from __future__ import annotations

import configparser
import json

from tools import pcb_model_runtime


def _clear_model_env(monkeypatch):
    for name in (
        "CTYUN_MODEL",
        "CTYUN_BASE_URL",
        "CTYUN_APP_KEY",
        "CTYUN_API_KEY",
        "OPENAI_MODEL",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "CTYUN_REROUTE_MODEL",
        "CTYUN_REROUTE_BASE_URL",
        "CTYUN_REROUTE_APP_KEY",
        "CTYUN_REROUTE_API_KEY",
        "PCB_REROUTE_MODEL",
        "PCB_REROUTE_BASE_URL",
        "PCB_REROUTE_API_KEY",
        "CTYUN_EXPLAIN_MODEL",
        "CTYUN_EXPLAIN_BASE_URL",
        "CTYUN_EXPLAIN_APP_KEY",
        "CTYUN_EXPLAIN_API_KEY",
        "PCB_EXPLAIN_MODEL",
        "PCB_EXPLAIN_BASE_URL",
        "PCB_EXPLAIN_API_KEY",
        "EXPLAIN_MODEL",
        "EXPLAIN_BASE_URL",
        "EXPLAIN_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENROUTER_MODEL",
        "OPENROUTER_BASE_URL",
        "PCB_OPENROUTER_QWEN_MODEL",
        "PCB_DISABLE_OPENROUTER_QWEN",
        "PCB_REROUTE_DISABLE_THINKING_KWARGS",
        "PCB_EXPLAIN_DISABLE_THINKING_KWARGS",
        "PCB_MODEL_DISABLE_THINKING_KWARGS",
        "PCB_REROUTE_USE_NO_THINK_PREFIX",
        "PCB_EXPLAIN_USE_NO_THINK_PREFIX",
        "PCB_MODEL_USE_NO_THINK_PREFIX",
        "PCB_REROUTE_TOKEN_PARAMETER",
        "PCB_EXPLAIN_TOKEN_PARAMETER",
        "PCB_MODEL_TOKEN_PARAMETER",
        "PCB_FORCE_OPENROUTER_QWEN",
        "PCB_MODEL_RUNTIME_DISABLE_DOTENV",
        "PCB_AGENT_MODEL_CONFIG_TXT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PCB_MODEL_RUNTIME_DISABLE_DOTENV", "1")
    pcb_model_runtime._LOCAL_ENV_LOADED = False


def _config_with_network(**values):
    parser = configparser.ConfigParser()
    parser.add_section("network")
    for key, value in values.items():
        parser.set("network", key, str(value))
    return parser


def test_stage_doc_parser_keeps_reroute_and_explain_models_separate(monkeypatch, tmp_path):
    _clear_model_env(monkeypatch)
    monkeypatch.setattr(pcb_model_runtime, "_runtime_from_global_config", lambda: {})
    doc_path = tmp_path / "天翼云部署模型使用说明.md"
    doc_path.write_text(
        "# 天翼云部署模型使用说明\n\n"
        "## 1. 拆线重布模型已确认信息\n\n"
        "| 字段 | 值 |\n"
        "|---|---|\n"
        "| OpenAI base_url | `https://wishub-x5.ctyun.cn/v1` |\n"
        "| modelId | `reroute-doc-model` |\n"
        "| App Key | reroute-secret-value |\n\n"
        "## 2. 可解释模型 explain 已确认信息\n\n"
        "| 字段 | 值 |\n"
        "|---|---|\n"
        "| OpenAI base_url | `https://wishub-x5.ctyun.cn/v1` |\n"
        "| modelId | `explain-doc-model` |\n"
        "| App Key | explain-secret-value |\n",
        encoding="utf-8",
    )

    reroute = pcb_model_runtime.resolve_model_runtime(
        pcb_model_runtime.STAGE_REROUTE,
        project_config_paths=[tmp_path / "missing.ini"],
        doc_paths=[doc_path],
        require_api_key=True,
    )
    explain = pcb_model_runtime.resolve_model_runtime(
        pcb_model_runtime.STAGE_EXPLAIN,
        project_config_paths=[tmp_path / "missing.ini"],
        doc_paths=[doc_path],
        require_api_key=True,
    )

    assert reroute == {
        "model": "reroute-doc-model",
        "base_url": "https://wishub-x5.ctyun.cn/v1",
        "api_key": "reroute-secret-value",
    }
    assert explain == {
        "model": "explain-doc-model",
        "base_url": "https://wishub-x5.ctyun.cn/v1",
        "api_key": "explain-secret-value",
    }


def test_stage_specific_reroute_env_overrides_legacy_env(monkeypatch, tmp_path):
    _clear_model_env(monkeypatch)
    monkeypatch.setattr(pcb_model_runtime, "_runtime_from_global_config", lambda: {})
    monkeypatch.setenv("CTYUN_MODEL", "legacy-reroute-model")
    monkeypatch.setenv("CTYUN_BASE_URL", "https://legacy.example/v1/chat/completions")
    monkeypatch.setenv("CTYUN_APP_KEY", "legacy-secret-value")
    monkeypatch.setenv("CTYUN_REROUTE_MODEL", "stage-reroute-model")
    monkeypatch.setenv("CTYUN_REROUTE_BASE_URL", "https://stage.example/v1/chat/completions")
    monkeypatch.setenv("CTYUN_REROUTE_APP_KEY", "stage-secret-value")

    runtime = pcb_model_runtime.resolve_model_runtime(
        pcb_model_runtime.STAGE_REROUTE,
        project_config_paths=[tmp_path / "missing.ini"],
        doc_paths=[tmp_path / "missing.md"],
        require_api_key=True,
    )

    assert runtime == {
        "model": "stage-reroute-model",
        "base_url": "https://stage.example/v1",
        "api_key": "stage-secret-value",
    }


def test_model_config_txt_keeps_primary_and_reroute_models_separate(monkeypatch, tmp_path):
    _clear_model_env(monkeypatch)
    monkeypatch.setattr(pcb_model_runtime, "_runtime_from_global_config", lambda: {})
    monkeypatch.setenv("PCB_DISABLE_OPENROUTER_QWEN", "1")
    model_config = tmp_path / "model_config.txt"
    model_config.write_text(
        "[tool-planning-chat-model]\n"
        "api_key = primary-secret-value\n"
        "model = primary-model\n"
        "base_url = https://primary.example/v1/chat/completions\n\n"
        "[reroute-model]\n"
        "api_key = reroute-secret-value\n"
        "model = reroute-model\n"
        "base_url = https://reroute.example/v1/chat/completions\n",
        encoding="utf-8",
    )
    project_config = tmp_path / "config.ini"
    project_config.write_text(
        "[model]\n"
        "api_key = config-secret-value\n"
        "model = config-model\n"
        "base_url = https://config.example/v1\n",
        encoding="utf-8",
    )

    primary = pcb_model_runtime.resolve_model_runtime(
        pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT,
        project_config_paths=[project_config],
        require_api_key=True,
    )
    reroute = pcb_model_runtime.resolve_model_runtime(
        pcb_model_runtime.STAGE_REROUTE,
        project_config_paths=[project_config],
        require_api_key=True,
    )

    assert primary == {
        "model": "primary-model",
        "base_url": "https://primary.example/v1",
        "api_key": "primary-secret-value",
    }
    assert reroute == {
        "model": "reroute-model",
        "base_url": "https://reroute.example/v1",
        "api_key": "reroute-secret-value",
    }


def test_config_ini_keeps_primary_and_reroute_models_separate(monkeypatch, tmp_path):
    _clear_model_env(monkeypatch)
    monkeypatch.setattr(pcb_model_runtime, "_runtime_from_global_config", lambda: {})
    monkeypatch.setenv("PCB_DISABLE_OPENROUTER_QWEN", "1")
    project_config = tmp_path / "config.ini"
    project_config.write_text(
        "[model]\n"
        "api_key = legacy-secret-value\n"
        "model = legacy-model\n"
        "base_url = https://legacy.example/v1\n\n"
        "[tool-planning-chat-model]\n"
        "api_key = primary-secret-value\n"
        "model = primary-model\n"
        "base_url = https://primary.example/v1/chat/completions\n\n"
        "[reroute-model]\n"
        "api_key = reroute-secret-value\n"
        "model = reroute-model\n"
        "base_url = https://reroute.example/v1/chat/completions\n",
        encoding="utf-8",
    )

    primary = pcb_model_runtime.resolve_model_runtime(
        pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT,
        project_config_paths=[project_config],
        require_api_key=True,
    )
    reroute = pcb_model_runtime.resolve_model_runtime(
        pcb_model_runtime.STAGE_REROUTE,
        project_config_paths=[project_config],
        require_api_key=True,
    )

    assert primary == {
        "model": "primary-model",
        "base_url": "https://primary.example/v1",
        "api_key": "primary-secret-value",
    }
    assert reroute == {
        "model": "reroute-model",
        "base_url": "https://reroute.example/v1",
        "api_key": "reroute-secret-value",
    }


def test_builtin_openrouter_fallback_makes_stages_ready_without_export(monkeypatch, tmp_path):
    _clear_model_env(monkeypatch)
    monkeypatch.setattr(pcb_model_runtime, "_runtime_from_global_config", lambda: {})

    reroute = pcb_model_runtime.resolve_model_runtime(
        pcb_model_runtime.STAGE_REROUTE,
        project_config_paths=[tmp_path / "missing.ini"],
        doc_paths=[tmp_path / "missing.md"],
        require_api_key=True,
    )
    explain = pcb_model_runtime.resolve_model_runtime(
        pcb_model_runtime.STAGE_EXPLAIN,
        project_config_paths=[tmp_path / "missing.ini"],
        doc_paths=[tmp_path / "missing.md"],
        require_api_key=True,
    )

    assert reroute == {
        "model": "qwen/qwen3.6-plus",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": pcb_model_runtime.BUILTIN_OPENROUTER_API_KEY,
    }
    assert explain == reroute


def test_openrouter_key_routes_both_stages_to_qwen_plus(monkeypatch, tmp_path):
    _clear_model_env(monkeypatch)
    monkeypatch.setattr(pcb_model_runtime, "_runtime_from_global_config", lambda: {})
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-secret-value")

    reroute = pcb_model_runtime.resolve_model_runtime(
        pcb_model_runtime.STAGE_REROUTE,
        project_config_paths=[tmp_path / "missing.ini"],
        doc_paths=[tmp_path / "missing.md"],
        require_api_key=True,
    )
    explain = pcb_model_runtime.resolve_model_runtime(
        pcb_model_runtime.STAGE_EXPLAIN,
        project_config_paths=[tmp_path / "missing.ini"],
        doc_paths=[tmp_path / "missing.md"],
        require_api_key=True,
    )

    assert reroute == {
        "model": "qwen/qwen3.6-plus",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "or-secret-value",
    }
    assert explain == reroute


def test_local_env_file_supplies_openrouter_key(monkeypatch, tmp_path):
    _clear_model_env(monkeypatch)
    monkeypatch.delenv("PCB_MODEL_RUNTIME_DISABLE_DOTENV", raising=False)
    env_path = tmp_path / ".env.local"
    env_path.write_text("OPENROUTER_API_KEY=or-local-secret\n", encoding="utf-8")
    monkeypatch.setattr(pcb_model_runtime, "_candidate_local_env_paths", lambda: [env_path])
    monkeypatch.setattr(pcb_model_runtime, "_runtime_from_global_config", lambda: {})
    pcb_model_runtime._LOCAL_ENV_LOADED = False

    runtime = pcb_model_runtime.resolve_model_runtime(
        pcb_model_runtime.STAGE_EXPLAIN,
        project_config_paths=[tmp_path / "missing.ini"],
        doc_paths=[tmp_path / "missing.md"],
        require_api_key=True,
    )

    assert runtime == {
        "model": "qwen/qwen3.6-plus",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "or-local-secret",
    }


def test_chat_completion_text_sends_stage_specific_payload(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        pcb_model_runtime,
        "_load_project_config_ini",
        lambda project_config_paths=None: _config_with_network(proxy_mode="proxy"),
    )

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "id": "resp-explain-1",
                    "choices": [{"message": {"content": "<think>hide</think>报告文本"}}],
                    "usage": {"total_tokens": 9},
                }
            ).encode("utf-8")

    def _fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        captured["authorization"] = req.headers.get("Authorization")
        return _FakeResponse()

    monkeypatch.setattr(pcb_model_runtime.urlrequest, "urlopen", _fake_urlopen)

    text, meta = pcb_model_runtime.chat_completion_text(
        stage=pcb_model_runtime.STAGE_EXPLAIN,
        runtime={
            "model": "explain-stage-model",
            "base_url": "https://wishub-x5.ctyun.cn/v1/chat/completions",
            "api_key": "explain-secret-value",
        },
        messages=[{"role": "user", "content": "生成报告"}],
        max_tokens=1024,
        temperature=0.2,
        timeout_s=88,
        require_api_key=True,
    )

    assert text == "报告文本"
    assert meta["stage"] == pcb_model_runtime.STAGE_EXPLAIN
    assert meta["response_id"] == "resp-explain-1"
    assert captured["url"] == "https://wishub-x5.ctyun.cn/v1/chat/completions"
    assert captured["timeout"] == 88
    assert captured["authorization"] == "Bearer explain-secret-value"
    assert captured["payload"]["model"] == "explain-stage-model"
    assert captured["payload"]["messages"][0]["content"].startswith("/no_think\n")
    assert "chat_template_kwargs" not in captured["payload"]


def test_chat_completion_text_can_opt_into_disable_thinking_kwargs(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        pcb_model_runtime,
        "_load_project_config_ini",
        lambda project_config_paths=None: _config_with_network(proxy_mode="proxy"),
    )

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"id": "resp-1", "choices": [{"message": {"content": '{"ok": true}'}}]}).encode("utf-8")

    def _fake_urlopen(req, timeout):
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse()

    monkeypatch.setenv("PCB_REROUTE_DISABLE_THINKING_KWARGS", "1")
    monkeypatch.setattr(pcb_model_runtime.urlrequest, "urlopen", _fake_urlopen)

    text, _meta = pcb_model_runtime.chat_completion_text(
        stage=pcb_model_runtime.STAGE_REROUTE,
        runtime={
            "model": "reroute-stage-model",
            "base_url": "https://wishub-x5.ctyun.cn/v1",
            "api_key": "secret",
        },
        messages=[{"role": "user", "content": "生成 patch"}],
    )

    assert text == '{"ok": true}'
    assert captured["payload"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_chat_completion_text_bypasses_wishub_proxy_by_default(monkeypatch):
    captured = {}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")

    class _FakeOpener:
        def open(self, req, timeout):
            captured["url"] = req.full_url
            captured["timeout"] = timeout
            return _FakeResponse()

    def _fake_build_opener(*handlers):
        captured["proxies"] = getattr(handlers[0], "proxies", None)
        return _FakeOpener()

    monkeypatch.setattr(pcb_model_runtime, "_load_project_config_ini", lambda project_config_paths=None: configparser.ConfigParser())
    monkeypatch.setattr(pcb_model_runtime.urlrequest, "build_opener", _fake_build_opener)
    monkeypatch.setattr(
        pcb_model_runtime.urlrequest,
        "urlopen",
        lambda req, timeout: (_ for _ in ()).throw(AssertionError("urlopen should not be used for wishub auto bypass")),
    )

    text, _meta = pcb_model_runtime.chat_completion_text(
        stage=pcb_model_runtime.STAGE_REROUTE,
        runtime={"model": "m", "base_url": "https://wishub-x5.ctyun.cn/v1", "api_key": "secret"},
        messages=[{"role": "user", "content": "x"}],
        timeout_s=12,
    )

    assert text == "ok"
    assert captured["url"] == "https://wishub-x5.ctyun.cn/v1/chat/completions"
    assert captured["timeout"] == 12
    assert captured["proxies"] == {}


def test_chat_completion_text_non_bypass_host_keeps_urlopen(monkeypatch):
    captured = {}
    monkeypatch.setattr(pcb_model_runtime, "_load_project_config_ini", lambda project_config_paths=None: configparser.ConfigParser())

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")

    def _fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(pcb_model_runtime.urlrequest, "urlopen", _fake_urlopen)

    text, _meta = pcb_model_runtime.chat_completion_text(
        stage=pcb_model_runtime.STAGE_REROUTE,
        runtime={"model": "m", "base_url": "https://model.example/v1", "api_key": "secret"},
        messages=[{"role": "user", "content": "x"}],
        timeout_s=13,
    )

    assert text == "ok"
    assert captured == {"url": "https://model.example/v1/chat/completions", "timeout": 13}


def test_chat_completion_text_direct_mode_bypasses_proxy_for_any_host(monkeypatch):
    captured = {}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")

    class _FakeOpener:
        def open(self, req, timeout):
            return _FakeResponse()

    def _fake_build_opener(*handlers):
        captured["proxies"] = getattr(handlers[0], "proxies", None)
        return _FakeOpener()

    monkeypatch.setattr(
        pcb_model_runtime,
        "_load_project_config_ini",
        lambda project_config_paths=None: _config_with_network(proxy_mode="direct"),
    )
    monkeypatch.setattr(pcb_model_runtime.urlrequest, "build_opener", _fake_build_opener)

    text, _meta = pcb_model_runtime.chat_completion_text(
        stage=pcb_model_runtime.STAGE_REROUTE,
        runtime={"model": "m", "base_url": "https://model.example/v1", "api_key": "secret"},
        messages=[{"role": "user", "content": "x"}],
    )

    assert text == "ok"
    assert captured["proxies"] == {}


def test_chat_completion_text_proxy_mode_uses_configured_proxy(monkeypatch):
    captured = {}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")

    class _FakeOpener:
        def open(self, req, timeout):
            return _FakeResponse()

    def _fake_build_opener(*handlers):
        captured["proxies"] = getattr(handlers[0], "proxies", None)
        return _FakeOpener()

    monkeypatch.setattr(
        pcb_model_runtime,
        "_load_project_config_ini",
        lambda project_config_paths=None: _config_with_network(
            proxy_mode="proxy",
            http_proxy="http://127.0.0.1:7897",
            https_proxy="http://127.0.0.1:7897",
        ),
    )
    monkeypatch.setattr(pcb_model_runtime.urlrequest, "build_opener", _fake_build_opener)

    text, _meta = pcb_model_runtime.chat_completion_text(
        stage=pcb_model_runtime.STAGE_REROUTE,
        runtime={"model": "m", "base_url": "https://wishub-x5.ctyun.cn/v1", "api_key": "secret"},
        messages=[{"role": "user", "content": "x"}],
    )

    assert text == "ok"
    assert captured["proxies"] == {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
