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

def _get_valid_bga_pads(board,log_file=""):
    """获取所有有效的 BGA pad，即满足以下条件"""
    pads = []
    for p in board.pads:
        if not getattr(p, "is_bga", False):
            continue
        if not _is_signal_net(p.net):
            continue
        pads.append(p)

    return pads

