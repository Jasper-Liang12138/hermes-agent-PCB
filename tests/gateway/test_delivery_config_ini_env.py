import configparser


def test_config_ini_overrides_stale_dotenv_model_env(monkeypatch):
    import gateway.run as gateway_run

    cfg = configparser.ConfigParser()
    cfg.read_string(
        """
[model]
api_key = 12345678901234567890123456789012
base_url = https://wishub-x5.ctyun.cn/api/v1/example/v1
board_data_use_file_path = 1

[router]
work_dir = F:/PCB/router_work
rl_root_dir = F:/PCB/routers/bk_routing
"""
    )

    monkeypatch.setenv("OPENAI_API_KEY", "template-key-000")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://your-endpoint/v1")
    monkeypatch.setenv("BOARD_DATA_USE_FILE_PATH", "0")

    gateway_run._apply_config_ini_env_overrides(cfg)

    assert gateway_run.os.environ["OPENAI_API_KEY"] == "12345678901234567890123456789012"
    assert gateway_run.os.environ["OPENAI_BASE_URL"] == "https://wishub-x5.ctyun.cn/api/v1/example/v1"
    assert gateway_run.os.environ["BOARD_DATA_USE_FILE_PATH"] == "1"
    assert gateway_run.os.environ["ROUTER_WORK_DIR"] == "F:/PCB/router_work"
    assert gateway_run.os.environ["ROUTER_RL_ROOT_DIR"] == "F:/PCB/routers/bk_routing"


def test_config_ini_override_ignores_dict_argument(monkeypatch):
    import gateway.run as gateway_run

    cfg = configparser.ConfigParser()
    cfg.read_string(
        """
[model]
api_key = fallback-config-key
base_url = https://wishub-x5.ctyun.cn/v1
"""
    )

    monkeypatch.setattr(gateway_run, "_config_ini_cfg", cfg)
    monkeypatch.setenv("OPENAI_API_KEY", "stale-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://your-endpoint/v1")

    gateway_run._apply_config_ini_env_overrides({"model": {"api_key": "dict-key"}})

    assert gateway_run.os.environ["OPENAI_API_KEY"] == "fallback-config-key"
    assert gateway_run.os.environ["OPENAI_BASE_URL"] == "https://wishub-x5.ctyun.cn/v1"
