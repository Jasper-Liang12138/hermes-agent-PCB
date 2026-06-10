import json

from collections import defaultdict

POWER_GROUND_KEYWORDS = [
    "GND",
    "GROUND",
    "PWR",
    "POWER",
    "VCC",
    "VDD",
    "VSS",
    "VBAT",
    "VIN",
    "VREF",
    "PGND",
    "AGND",
    "DGND",
    "+1V",
    "+1.",
    "+2V",
    "+2.",
    "+3V",
    "+3.",
    "+5V",
    "+5.",
    "+12V",
    "1V0",
    "1V1",
    "1V2",
    "1V5",
    "1V8",
    "2V5",
    "3V3",
    "5V",
    "0P9",
    "1P0",
    "1P1",
    "1P2",
    "1P5",
    "1P8",
    "2P5",
    "3P3",
    "5P0",
    "VCORE",
    "VTT",
]

POWER_GROUND_PREFIX_KEYWORDS = [
    "CORE",
]
def _is_named_net(net: str) -> bool:
    """判断 net 是否是一个有效的、非空的字符串。"""
    if net is None:
        return False
    if not isinstance(net, str):
        return False
    if net.strip() == "":
        return False
    return True


def _is_power_or_ground_net(net: str) -> bool:
    """判断 net 是否属于电源/地网络。大小写不敏感。"""
    if not _is_named_net(net):
        return False

    net_u = net.upper().strip()

    # 1) 普通关键字：子串匹配
    for kw in POWER_GROUND_KEYWORDS:
        if kw in net_u:
            return True

    # 2) 前缀关键字：例如 CORE*
    for prefix in POWER_GROUND_PREFIX_KEYWORDS:
        if net_u.startswith(prefix):
            return True

    return False

def _is_signal_net(net: str) -> bool:
    """判断 net 是否属于需要参与 escape / DRC 检查的信号网络。"""
    return _is_named_net(net) and (not _is_power_or_ground_net(net))


def _get_all_bga_pads(board):
    pads = []
    for p in board.pads:
        if getattr(p, "is_bga", False):
            pads.append(p)
    return pads

"""
def _get_valid_bga_pads(board,log_file=""):

    pads = []
    for p in board.pads:
        if not getattr(p, "is_bga", False):
            continue
        if not _is_signal_net(p.net):
            continue
        pads.append(p)

    return pads
"""

def get_signal_net_summary_from_external_file(board, net_roles_path: str):
    with open(net_roles_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    board_net_names = {n.name for n in board.nets if _is_named_net(n.name)}

    signal_nets = set(cfg.get("signal_nets", []))
    power_nets = set(cfg.get("power_nets", []))
    ground_nets = set(cfg.get("ground_nets", []))
    ignore_nets = set(cfg.get("ignore_nets", []))

    configured_nets = signal_nets | power_nets | ground_nets | ignore_nets
    unknown_configured_nets = sorted(configured_nets - board_net_names)

    # 外部文件里写的，但 PCB 中真实存在的部分
    explicit_signal_nets = signal_nets & board_net_names
    explicit_power_nets = power_nets & board_net_names
    explicit_ground_nets = ground_nets & board_net_names
    explicit_ignore_nets = ignore_nets & board_net_names

    # 未配置网络走原脚本兜底
    auto_summary = get_signal_net_summary(board)
    auto_signal = set(auto_summary["signal_nets"])
    auto_filtered = set(auto_summary["filtered_out_nets"])

    unconfigured_nets = board_net_names - configured_nets

    final_signal_nets = sorted(explicit_signal_nets | (unconfigured_nets & auto_signal))
    final_filtered_out_nets = sorted(
        explicit_power_nets
        | explicit_ground_nets
        | explicit_ignore_nets
        | (unconfigured_nets & auto_filtered)
    )

    return {
        "all_net_count": len(board_net_names),
        "signal_net_count": len(final_signal_nets),
        "filtered_out_net_count": len(final_filtered_out_nets),

        "signal_nets": final_signal_nets,
        "filtered_out_nets": final_filtered_out_nets,

        "power_nets": sorted(explicit_power_nets),
        "ground_nets": sorted(explicit_ground_nets),
        "ignore_nets": sorted(explicit_ignore_nets),

        "auto_signal_nets": sorted(unconfigured_nets & auto_signal),
        "auto_filtered_out_nets": sorted(unconfigured_nets & auto_filtered),
        "unknown_configured_nets": unknown_configured_nets,

        "source": "external_file_with_fallback",
    }

def _get_valid_bga_pads(board, log_file=""):
    pads = []
    target_bga = getattr(board, "target_bga", "") or ""

    signal_nets = set(getattr(board, "signal_nets", []) or [])
    filtered_out_nets = set(getattr(board, "filtered_out_nets", []) or [])

    for p in board.pads:
        if not getattr(p, "is_bga", False):
            continue

        if target_bga and p.component != target_bga:
            continue

        if not _is_named_net(p.net):
            continue

        # 优先使用 PreCheck / 外部 net_roles.json 的分类结果
        if signal_nets:
            if p.net not in signal_nets:
                continue
        else:
            # 没有 PreCheck 结果时，才退回原来的脚本过滤
            if not _is_signal_net(p.net):
                continue

        # 保险：如果明确在 filtered_out_nets 中，直接跳过
        if p.net in filtered_out_nets:
            continue

        pads.append(p)

    return pads

def get_signal_net_summary(board):
    all_net_names = [n.name for n in board.nets if _is_named_net(n.name)]
    signal_nets = []
    filtered_out_nets = []

    for name in all_net_names:
        if _is_signal_net(name):
            signal_nets.append(name)
        else:
            filtered_out_nets.append(name)

    return {
        "all_net_count": len(all_net_names),
        "signal_net_count": len(signal_nets),
        "filtered_out_net_count": len(filtered_out_nets),
        "signal_nets": signal_nets,
        "filtered_out_nets": filtered_out_nets,
    }