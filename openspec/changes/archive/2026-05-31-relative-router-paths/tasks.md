## 1. Core: pcb_bjut_router.py path resolution

- [x] 1.1 修改 `_expand_path` 接受可选的 `base_dir: Path | None` 参数，当路径非绝对且 base_dir 非 None 时以 base_dir 为基准 resolve
- [x] 1.2 修改 `_config_path` 接受并传递 `base_dir` 参数
- [x] 1.3 修改 `load_router_config` 返回 `tuple[ConfigParser, Path | None]`（parser + config_dir），`_config_paths()` 同时返回实际找到 config.ini 的路径
- [x] 1.4 修改 `resolve_router_dir` 适配新的 `load_router_config` 返回签名，将 config_dir 传递给 `_config_path` 调用

## 2. Core: pcb_model_runtime.py path resolution

- [x] 2.1 修改 `_load_project_config_ini` 返回 `tuple[ConfigParser | None, Path | None]`（parser + config_dir）
- [x] 2.2 对从 config.ini 读取路径的调用方（如 checkpoint_path），基于 config_dir 进行 resolve

## 3. Tests

- [x] 3.1 新增单元测试：验证绝对路径在 base_dir 存在时保持不变
- [x] 3.2 新增单元测试：验证相对路径在 base_dir 存在时正确 resolve
- [x] 3.3 新增单元测试：验证环境变量 `%VAR%` 展开后是绝对路径时不再二次 resolve
- [x] 3.4 运行现有测试套件确认无回归：`pytest tests/tools/ -x -q`

## 4. Validation

- [ ] 4.1 手动验证：将 `config.ini` 中 `arc_dir`、`135_dir` 等改为 `../routers/` 相对路径，启动服务确认布线器可用 ⬅️ 需手动执行
