from agent.swsd.experience.fanout_versions import FanoutVersionStore, parse_fanout_version_request


def test_parse_fanout_version_restore_request():
    request = parse_fanout_version_request("恢复第 1 版参数")

    assert request["isVersionRequest"] is True
    assert request["intent"] == "restore_params"
    assert request["restoredFromVersion"] == 1


def test_fanout_version_store_roundtrip(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    store = FanoutVersionStore(configured_dir=str(tmp_path / "versions"))

    store.bind_project("s1", "p1")
    store.record_initial_layout("s1", project_id="p1", layout_text="(board)")
    route = store.record_route_version(
        "s1",
        fanout_params={
            "selectedBGA": "U5",
            "routerType": "arc",
            "orderLines": [{"net": "GND", "layer": "SIG01", "order": 1}],
        },
        routed_layout_path="",
        import_lines_path="",
        report="ok",
    )

    params, version = store.latest_fanout_params("s1")
    layout, layout_version, _path = store.resolve_layout_text("s1", "current")

    assert route["version"] == 1
    assert version == 1
    assert params["selectedBGA"] == "U5"
    assert layout == "(board)"
    assert layout_version == 0
