from __future__ import annotations

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
        "PCB_MODEL_RUNTIME_DISABLE_DOTENV",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PCB_MODEL_RUNTIME_DISABLE_DOTENV", "1")
    pcb_model_runtime._LOCAL_ENV_LOADED = False


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
    assert captured["payload"]["chat_template_kwargs"] == {"enable_thinking": False}
