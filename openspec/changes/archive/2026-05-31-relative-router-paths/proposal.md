## Why

config.ini 中布线器路径（`arc_dir`、`135_dir`、`rl_*_dir` 等）当前使用硬编码的 `D:/Develop/...` 绝对路径，导致项目目录移动或跨机器部署后布线器不可用。同时 `work_dir` 等配置项虽可使用相对路径，但相对路径基于 CWD 而非 config.ini 所在目录，换目录启动就会找不到文件。

## What Changes

- 修改 `tools/pcb_bjut_router.py` 的 `_expand_path` / `load_router_config` 函数，使相对路径基于 config.ini 所在目录解析
- 新增 `config_dir()` 函数，返回第一个找到的 config.ini 所在目录
- 所有通过 config.ini 读取的路径均由 config.ini 目录作为基准 resolve
- `config.ini` 中的布线器目录可采用 `../routers/arc_windows_0519` 等相对路径

## Capabilities

### New Capabilities

- `config-relative-paths`: config.ini 中的路径支持基于 config.ini 所在目录的相对路径解析

### Modified Capabilities

（无，不改动现有 spec 级别行为）

## Impact

- `tools/pcb_bjut_router.py`: `_expand_path` 和 `load_router_config` 是核心变更点
- `tools/pcb_tools.py`: 间接受益，其通过 `resolve_router_dir` 获取路径
- `tools/pcb_model_runtime.py`: 同样使用 `config.ini` 加载模型配置，如涉及路径也应统一处理
- `config.ini`: 用户可将 `arc_dir`、`135_dir`、`rl_*_dir` 改为相对路径
- 向后兼容：绝对路径行为不变；已在 config.ini 同级目录启动的 CWD 相对路径行为也基本不变
