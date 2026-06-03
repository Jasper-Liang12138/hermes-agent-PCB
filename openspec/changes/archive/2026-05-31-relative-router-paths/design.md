## Context

当前 `tools/pcb_bjut_router.py` 中 `_expand_path` 函数仅做 `os.path.expandvars` + `os.path.expanduser`，然后直接包进 `Path()`。相对路径因此相对 CWD 而非 config.ini 所在目录。`config.ini` 中布线器目录被迫使用硬编码绝对路径（如 `D:/Develop/JBZ_AI/...`），导致项目无法跨机器部署。

`tools/pcb_model_runtime.py` 也有独立的 `_candidate_project_config_paths` 函数加载 config.ini，但路径解析逻辑类似，未做相对路径基准处理。

PyInstaller 打包场景（`sys._MEIPASS`）下 config.ini 位于临时解压目录，相对路径需要以该目录为基准。

## Goals / Non-Goals

**Goals:**
- config.ini 中的任何文件路径，若为相对路径，均以 config.ini 所在目录为基准 resolve
- 绝对路径行为不变（`Path.is_absolute()` 时不做额外处理）
- 环境变量路径仍被展开（`expandvars`/`expanduser` 之后再做基准 resolve）
- 兼容 PyInstaller 打包场景（`sys._MEIPASS` 下的 config.ini）

**Non-Goals:**
- 不改动 `config.ini` 文件本身的内容（用户自行决定是否改相对路径）
- 不改动 `work_dir` 的读取逻辑（当前 `work_dir` 由环境变量 `ROUTER_WORK_DIR` 控制，与 config.ini 的 `work_dir` 不关联）
- 不引入新的第三方依赖

## Decisions

**Decision 1: 在 `load_router_config` 返回 parser 时附带 config_dir**

修改 `load_router_config` 使其同时返回找到的 config.ini 所在目录。调用方 `resolve_router_dir` 拿到 `config_dir` 后传给 `_expand_path`。

理由：config.ini 可能来自多个位置（`_MEIPASS` 打包目录或 `_repo_root()`），`load_router_config` 已经遍历这些候选路径，是唯一知道实际用了哪个 config.ini 的地方。

**Decision 2: 将 `_expand_path` 改为接受可选的 `base_dir` 参数**

当传入的相对路径且 `base_dir` 不为 None 时，用 `base_dir / value` 做 resolve。绝对路径和已包含环境变量展开的路径不做额外处理。

```python
def _expand_path(value: str, base_dir: Path | None = None) -> Path:
    expanded = Path(os.path.expandvars(os.path.expanduser(value.strip())))
    if not expanded.is_absolute() and base_dir is not None:
        return (base_dir / expanded).resolve()
    return expanded
```

**Decision 3: `pcb_model_runtime.py` 采用独立但一致的处理方式**

`pcb_model_runtime.py` 有自己的配置加载路径 `_candidate_project_config_paths`，其中 checkpoint_path 等路径需要同样的基准解析。在 `_load_project_config_ini` 中同时返回 config_dir，由调用方在读取路径时使用。

## Risks / Trade-offs

- [兼容性风险] 现有在项目根目录启动的用户，config.ini 中如有相对路径（如 `work_dir = ./router_work`），解析后路径不变（CWD 和 config_dir 通常是同一个目录）。但如果用户在非项目根目录运行且之前依赖 CWD 相对路径 → **Mitigation**: 该场景下原来的行为本身就是不可靠的（换目录就坏），改进后反而变得更可预测。
- [PyInstaller 打包] `_MEIPASS` 路径通常是临时目录，`resolve()` 后的路径可能不持久 → **Mitigation**: 布线器目录在打包时已复制到 `_internal/` 下，路径 resolve 后指向实际文件即正常工作。
