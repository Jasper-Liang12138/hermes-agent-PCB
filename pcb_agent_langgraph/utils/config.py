from __future__ import annotations

from configparser import ConfigParser
from dataclasses import dataclass, field
import os
from pathlib import Path
import sys
from typing import Any


@dataclass(slots=True)
# ====== 功能：保存模型服务相关配置。 ======
class ModelConfig:
    api_key: str = ""
    model: str = ""
    base_url: str = ""
    max_tokens: int = 65536
    public_name: str = "pcb-model"


@dataclass(slots=True)
# ====== 功能：保存真实 router 和局部布线器相关配置。 ======
class RouterConfig:
    # router 路径和命令全部显式配置，避免从旧 Hermes 目录隐式加载逻辑。
    work_dir: str = ".\\router_work"
    arc_dir: str = ".\\routers\\弧形走线"
    dir_135: str = ".\\routers\\135度走线"
    rule_arc_dir: str = ".\\routers\\弧形走线"
    rule_135_dir: str = ".\\routers\\135度走线"
    rl_root_dir: str = ".\\routers"
    layer_assign_command: str = ""
    escape_order_command: str = ""
    rule_135_command: str = ""
    rule_arc_command: str = ""
    pcbrouter_bin: str = ".\\tools\\reroute_helper\\pcbrouter.exe"
    pcbrouter_timeout_seconds: int = 300
    rule_timeout_seconds: int = 1800
    rl_timeout_seconds: int = 1800


@dataclass(slots=True)
# ====== 功能：保存 BGA 提取脚本相关配置。 ======
class BgaExtractConfig:
    tool_path: str = ".\\tools\\extract_bga_components.py"
    timeout_seconds: int = 120


@dataclass(slots=True)
# ====== 功能：保存 DRC 工具链相关配置。 ======
class DrcConfig:
    enabled: bool = False
    tool_path: str = ".\\tools\\pcb_reroute_drc.py"
    eval_root: str = ".\\vendor\\AI-PCB-Eval"
    timeout_seconds: int = 360
    work_dir: str = ".\\drc_work"


@dataclass(slots=True)
# ====== 功能：保存可解释性模型运行环境和权重配置。 ======
class ExplainModelConfig:
    # 默认外部 runtime 路径仅作为本机开发示例；远端使用者应在 config.ini 改成自己的路径或项目内 runtime。
    enabled: bool = False
    python_executable: str = "F:\\PCB_QYF\\PCB_Builder\\cust_tools\\PCBCopilot_dev\\PCB-AGENT\\python_runtime\\python.exe"
    code_dir: str = ".\\explain_model\\explain_code"
    checkpoint_path: str = ".\\explain_model\\model\\best.pt"
    timeout_seconds: int = 600


@dataclass(slots=True)
# ====== 功能：保存 reroute 兜底 help_planner 策略配置。 ======
class RerouteHelpConfig:
    enabled: bool = True
    max_drc_failures: int = 3
    max_elapsed_seconds: int = 900


@dataclass(slots=True)
# ====== 功能：保存主 reroute loop 提供方配置。 ======
class RerouteLoopConfig:
    enabled: bool = True
    provider: str = "vsea"
    pipeline_root: str = ".\\vendor\\VSEA-PCB"
    ai_pcb_eval_path: str = ".\\vendor\\AI-PCB-Eval"
    drc_agent_package: str = "..\\external_drc\\DRC_0623_v2\\agent_package"
    agent_drc_python: str = ""
    max_rounds: int = 2
    samples: int = 2
    repair_samples: int = 2
    repair_retries: int = 2
    timeout_seconds: int = 900


@dataclass(slots=True)
# ====== 功能：保存 WebSocket 服务监听配置。 ======
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 7074


@dataclass(slots=True)
# ====== 功能：保存 Agent 全链路调试日志配置。 ======
class DebugLogConfig:
    enabled: bool = True
    print: bool = True
    dir: str = ".\\agent_logs"
    redact_secrets: bool = True


@dataclass(slots=True)
# ====== 功能：聚合当前项目运行所需的全部配置。 ======
class AppConfig:
    root: Path
    source_config: Path | None = None
    model: ModelConfig = field(default_factory=ModelConfig)
    router: RouterConfig = field(default_factory=RouterConfig)
    bga_extract: BgaExtractConfig = field(default_factory=BgaExtractConfig)
    drc: DrcConfig = field(default_factory=DrcConfig)
    explain_model: ExplainModelConfig = field(default_factory=ExplainModelConfig)
    reroute_help: RerouteHelpConfig = field(default_factory=RerouteHelpConfig)
    reroute_loop: RerouteLoopConfig = field(default_factory=RerouteLoopConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    debug_log: DebugLogConfig = field(default_factory=DebugLogConfig)
    board_data_use_file_path: bool = True


# ====== 功能：读取 config.ini 并生成应用配置对象。 ======
def load_config(path: str | Path | None = None) -> AppConfig:
    # 配置优先使用调用方传入路径，否则读取当前项目 config.ini。
    root = _runtime_root()
    candidates = []
    if path:
        candidates.append(Path(path))
    candidates.append(root / "config.ini")

    parser = ConfigParser()
    source: Path | None = None
    for candidate in candidates:
        candidate = candidate if candidate.is_absolute() else root / candidate
        if candidate.exists():
            parser.read(candidate, encoding="utf-8-sig")
            source = candidate
            break

    model = ModelConfig()
    # 统一 pcb-model：意图识别、工具规划和 reroute 都从 [reroute-model] 读取。
    if parser.has_section("reroute-model"):
        model.api_key = parser.get("reroute-model", "api_key", fallback="")
        model.model = parser.get("reroute-model", "model", fallback="")
        model.base_url = parser.get("reroute-model", "base_url", fallback="")
        model.max_tokens = parser.getint("reroute-model", "max_tokens", fallback=65536)
        model.public_name = "pcb-model"
    router = RouterConfig()
    if parser.has_section("router"):
        router.work_dir = parser.get("router", "work_dir", fallback=router.work_dir)
        router.arc_dir = parser.get("router", "arc_dir", fallback=router.arc_dir)
        router.dir_135 = parser.get("router", "135_dir", fallback=router.dir_135)
        router.rule_arc_dir = parser.get("router", "rule_arc_dir", fallback=router.rule_arc_dir)
        router.rule_135_dir = parser.get("router", "rule_135_dir", fallback=router.rule_135_dir)
        router.rl_root_dir = parser.get("router", "rl_root_dir", fallback=router.rl_root_dir)
        router.layer_assign_command = parser.get("router", "layer_assign_command", fallback=router.layer_assign_command)
        router.escape_order_command = parser.get("router", "escape_order_command", fallback=router.escape_order_command)
        router.rule_135_command = parser.get("router", "rule_135_command", fallback=router.rule_135_command)
        router.rule_arc_command = parser.get("router", "rule_arc_command", fallback=router.rule_arc_command)
        router.pcbrouter_bin = parser.get("router", "pcbrouter_bin", fallback=router.pcbrouter_bin)
        router.pcbrouter_timeout_seconds = parser.getint("router", "pcbrouter_timeout_seconds", fallback=router.pcbrouter_timeout_seconds)
        router.rule_timeout_seconds = parser.getint("router", "rule_timeout_seconds", fallback=router.rule_timeout_seconds)
        router.rl_timeout_seconds = parser.getint("router", "rl_timeout_seconds", fallback=router.rl_timeout_seconds)

    bga_extract = BgaExtractConfig()
    if parser.has_section("bga_extract"):
        bga_extract.tool_path = parser.get("bga_extract", "tool_path", fallback=bga_extract.tool_path)
        bga_extract.timeout_seconds = parser.getint("bga_extract", "timeout_seconds", fallback=bga_extract.timeout_seconds)

    drc = DrcConfig()
    if parser.has_section("drc"):
        drc.enabled = parser.getboolean("drc", "enabled", fallback=drc.enabled)
        drc.tool_path = parser.get("drc", "tool_path", fallback=drc.tool_path)
        drc.eval_root = parser.get("drc", "eval_root", fallback=drc.eval_root)
        drc.timeout_seconds = parser.getint("drc", "timeout_seconds", fallback=drc.timeout_seconds)
        drc.work_dir = parser.get("drc", "work_dir", fallback=drc.work_dir)

    explain_model = ExplainModelConfig()
    if parser.has_section("explain_model"):
        explain_model.enabled = parser.getboolean("explain_model", "enabled", fallback=explain_model.enabled)
        explain_model.python_executable = parser.get("explain_model", "python_executable", fallback=explain_model.python_executable)
        explain_model.code_dir = parser.get("explain_model", "code_dir", fallback=explain_model.code_dir)
        explain_model.checkpoint_path = parser.get("explain_model", "checkpoint_path", fallback=explain_model.checkpoint_path)
        explain_model.timeout_seconds = parser.getint("explain_model", "timeout_seconds", fallback=explain_model.timeout_seconds)

    reroute_help = RerouteHelpConfig()
    if parser.has_section("reroute_help"):
        reroute_help.enabled = parser.getboolean("reroute_help", "enabled", fallback=reroute_help.enabled)
        reroute_help.max_drc_failures = parser.getint("reroute_help", "max_drc_failures", fallback=reroute_help.max_drc_failures)
        reroute_help.max_elapsed_seconds = parser.getint("reroute_help", "max_elapsed_seconds", fallback=reroute_help.max_elapsed_seconds)

    reroute_loop = RerouteLoopConfig()
    if parser.has_section("reroute_loop"):
        reroute_loop.enabled = parser.getboolean("reroute_loop", "enabled", fallback=reroute_loop.enabled)
        reroute_loop.provider = parser.get("reroute_loop", "provider", fallback=reroute_loop.provider)
        reroute_loop.pipeline_root = parser.get("reroute_loop", "pipeline_root", fallback=reroute_loop.pipeline_root)
        reroute_loop.ai_pcb_eval_path = parser.get("reroute_loop", "ai_pcb_eval_path", fallback=reroute_loop.ai_pcb_eval_path)
        reroute_loop.drc_agent_package = parser.get("reroute_loop", "drc_agent_package", fallback=reroute_loop.drc_agent_package)
        reroute_loop.agent_drc_python = parser.get("reroute_loop", "agent_drc_python", fallback=reroute_loop.agent_drc_python)
        reroute_loop.max_rounds = parser.getint("reroute_loop", "max_rounds", fallback=reroute_loop.max_rounds)
        reroute_loop.samples = parser.getint("reroute_loop", "samples", fallback=reroute_loop.samples)
        reroute_loop.repair_samples = parser.getint("reroute_loop", "repair_samples", fallback=reroute_loop.repair_samples)
        reroute_loop.repair_retries = parser.getint("reroute_loop", "repair_retries", fallback=reroute_loop.repair_retries)
        reroute_loop.timeout_seconds = parser.getint("reroute_loop", "timeout_seconds", fallback=reroute_loop.timeout_seconds)

    server = ServerConfig()
    if parser.has_section("server"):
        server.host = parser.get("server", "host", fallback=server.host)
        server.port = parser.getint("server", "port", fallback=server.port)

    debug_log = DebugLogConfig()
    if parser.has_section("debug_log"):
        debug_log.enabled = parser.getboolean("debug_log", "enabled", fallback=debug_log.enabled)
        debug_log.print = parser.getboolean("debug_log", "print", fallback=debug_log.print)
        debug_log.dir = parser.get("debug_log", "dir", fallback=debug_log.dir)
        debug_log.redact_secrets = parser.getboolean("debug_log", "redact_secrets", fallback=debug_log.redact_secrets)
    env_enabled = os.environ.get("PCB_AGENT_DEBUG_LOG")
    if env_enabled is not None:
        debug_log.enabled = env_enabled.strip().lower() not in {"0", "false", "no", "off", ""}
    env_dir = os.environ.get("PCB_AGENT_DEBUG_LOG_DIR")
    if env_dir:
        debug_log.dir = env_dir

    board_data_use_file_path = parser.getboolean("model", "board_data_use_file_path", fallback=True) if parser.has_section("model") else True
    return AppConfig(
        root=root,
        source_config=source,
        model=model,
        router=router,
        bga_extract=bga_extract,
        drc=drc,
        explain_model=explain_model,
        reroute_help=reroute_help,
        reroute_loop=reroute_loop,
        server=server,
        debug_log=debug_log,
        board_data_use_file_path=board_data_use_file_path,
    )

# ====== 功能：定位源码运行或 PyInstaller 封装后的运行根目录。 ======
def _runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]

# ====== 功能：将配置对象转换为可序列化字典。 ======
def as_dict(config: AppConfig) -> dict[str, Any]:
    return {
        "root": str(config.root),
        "source_config": str(config.source_config or ""),
        "model": {
            "model": config.model.model,
            "base_url": config.model.base_url,
            "public_name": config.model.public_name,
            "max_tokens": config.model.max_tokens,
        },
        "router": {
            "work_dir": config.router.work_dir,
            "arc_dir": config.router.arc_dir,
            "135_dir": config.router.dir_135,
            "rule_arc_dir": config.router.rule_arc_dir,
            "rule_135_dir": config.router.rule_135_dir,
            "rl_root_dir": config.router.rl_root_dir,
            "layer_assign_command": config.router.layer_assign_command,
            "escape_order_command": config.router.escape_order_command,
            "rule_135_command": config.router.rule_135_command,
            "rule_arc_command": config.router.rule_arc_command,
            "pcbrouter_bin": config.router.pcbrouter_bin,
            "pcbrouter_timeout_seconds": config.router.pcbrouter_timeout_seconds,
        },
        "bga_extract": {
            "tool_path": config.bga_extract.tool_path,
            "timeout_seconds": config.bga_extract.timeout_seconds,
        },
        "drc": {
            "enabled": config.drc.enabled,
            "tool_path": config.drc.tool_path,
            "eval_root": config.drc.eval_root,
            "timeout_seconds": config.drc.timeout_seconds,
            "work_dir": config.drc.work_dir,
        },
        "explain_model": {
            "enabled": config.explain_model.enabled,
            "python_executable": config.explain_model.python_executable,
            "code_dir": config.explain_model.code_dir,
            "checkpoint_path": config.explain_model.checkpoint_path,
            "timeout_seconds": config.explain_model.timeout_seconds,
        },
        "reroute_help": {
            "enabled": config.reroute_help.enabled,
            "max_drc_failures": config.reroute_help.max_drc_failures,
            "max_elapsed_seconds": config.reroute_help.max_elapsed_seconds,
        },
        "reroute_loop": {
            "enabled": config.reroute_loop.enabled,
            "provider": config.reroute_loop.provider,
            "pipeline_root": config.reroute_loop.pipeline_root,
            "ai_pcb_eval_path": config.reroute_loop.ai_pcb_eval_path,
            "drc_agent_package": config.reroute_loop.drc_agent_package,
            "agent_drc_python": config.reroute_loop.agent_drc_python,
            "max_rounds": config.reroute_loop.max_rounds,
            "samples": config.reroute_loop.samples,
            "repair_samples": config.reroute_loop.repair_samples,
            "repair_retries": config.reroute_loop.repair_retries,
            "timeout_seconds": config.reroute_loop.timeout_seconds,
        },
        "server": {"host": config.server.host, "port": config.server.port},
        "debug_log": {
            "enabled": config.debug_log.enabled,
            "print": config.debug_log.print,
            "dir": config.debug_log.dir,
            "redact_secrets": config.debug_log.redact_secrets,
        },
        "board_data_use_file_path": config.board_data_use_file_path,
    }
