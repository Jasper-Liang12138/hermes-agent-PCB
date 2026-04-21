"""PCB Chunking Tool - BGA List Extraction

Uses the pcb_chunk_service wheel to parse 启云方 format PCB layout data
and extract BGA components. All format conversion and chunking strategy
are encapsulated inside the wheel and not exposed externally.

Toggle via config.yaml:
    pcb:
      use_long_context_module: true   # set to false to disable
"""
import json
import logging

from tools.registry import registry

logger = logging.getLogger(__name__)

try:
    from pcb_chunk_service import PCBChunkService as _PCBChunkService
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False
    logger.warning("pcb_chunk_service not installed; pcb_extract_bga tool disabled")

_service = _PCBChunkService() if _AVAILABLE else None


def _config_enabled() -> bool:
    try:
        from hermes_cli.config import load_config
        return bool(load_config().get("pcb", {}).get("use_long_context_module", True))
    except Exception:
        return True


def _extract_bga(board_text: str) -> str:
    if not board_text or not board_text.strip():
        return json.dumps({"error": "board_text is empty"}, ensure_ascii=False)
    try:
        bga_list = _service.extract_bga_from_txt(qiyun_board_text=board_text)
        if not bga_list:
            return json.dumps(
                {"selection": [], "message": "未识别到 BGA 封装元件"},
                ensure_ascii=False,
            )
        return json.dumps(
            {"selection": [b.to_dict() for b in bga_list]},
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error("pcb_extract_bga failed: %s", e)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


registry.register(
    name="pcb_extract_bga",
    toolset="pcb",
    schema={
        "name": "pcb_extract_bga",
        "description": (
            "从启云方格式 PCB 版图数据中提取所有 BGA 封装元件列表。"
            "在 getProjectData 获取到版图数据后调用此工具，"
            "返回 selection 数组，每项包含 label（位号）和 detail（封装描述）。"
            "提取结果用 ##PCB_FIELDS## 标记包裹后返回给用户选择。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "board_text": {
                    "type": "string",
                    "description": "getProjectData 返回的启云方格式 PCB 版图 S 表达式文本",
                }
            },
            "required": ["board_text"],
        },
    },
    handler=lambda args, **kwargs: _extract_bga(args.get("board_text", "")),
    check_fn=lambda: _AVAILABLE and _config_enabled(),
)
