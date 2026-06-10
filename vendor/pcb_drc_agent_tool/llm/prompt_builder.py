from typing import List

RULE_DESC = {
    # -------------------------
    # Hard rules
    # -------------------------
    "HR_CONNECT_PAD_NOT_ESCAPED": "BGA有效信号焊盘必须有初始逃逸连接；如果焊盘没有接出任何首段fanout走线，则判为未逃逸。",
    "HR_TOPO_MULTIPLE_ESCAPE": "每个BGA焊盘只允许一个初始逃逸方向/选择；如果一个焊盘同时接出多个首跳分支，则判为多重逃逸。",
    "HR_DRC_SEGMENT_CROSSING": "不同网络的同层铜线段不应发生交叉或重叠；若异网segment在同层相交，则判为布线冲突。",
    "HR_CONNECT_BRANCH_INCOMPLETE": "从BGA焊盘出发的逃逸分支应连续走出BGA区域；如果分支在BGA内部中断、悬空或未真正逃逸到外部，则判为逃逸不完整。",
    "HR_TOPO_ENDPOINT_NOT_UNIQUE": "单个BGA焊盘的逃逸路径应对应唯一外部终点；若一个焊盘到达多个外部终点，则说明拓扑不唯一。",
    "HR_CONNECT_ESCAPE_PATH_FORK": "BGA内部逃逸路径应保持单一路径；若路径在BGA区域内发生分叉，则说明逃逸拓扑存在fork。",

    # -------------------------
    # Optimization rules
    # -------------------------
    "OR_INNER_PAD_PREFER_VIA": "较内圈的BGA焊盘更适合采用via fanout换层逃逸；若深内圈焊盘没有使用可达fanout via，则给出优化建议。",
    "OR_VIA_NOT_AT_CELL_CENTER": "内圈BGA焊盘的fanout via应尽量落在相邻四焊盘形成的cell中心附近；若偏离过大，则说明via位置不理想。",
    "OR_VIA_NOT_45_DEG": "内圈BGA焊盘到fanout via的方向应尽量满足45°对角逃逸几何；若偏差过大，则说明逃逸角度不理想。",
    "OR_TOP_FANOUT_WIDTH_TOO_SMALL": "Top层首段fanout线宽不应小于该BGA pitch对应的建议最小值；若过窄，则会影响可制造性和布线质量。",
    "OR_FANOUT_VIA_SIZE_INVALID": "fanout via的drill/size应满足该BGA pitch对应的推荐尺寸；若过孔尺寸不匹配，则说明via规格不理想。",

    # -------------------------
    # Differential rules
    # -------------------------
    "DR_PAIR_MISSING_MATE": "目标BGA上的差分对应该同时具备P/N两侧网络；若只存在一侧，则说明差分对不完整。",
    "DR_PAIR_VIA_COUNT_MISMATCH": "差分对P/N两侧的过孔数量应尽量匹配；若via数量不一致，则可能导致层跳转策略不对称。",
}

def _format_issue(issue, idx: int) -> str:
    parts = [
        f"[{idx}]",
        f"rule={issue.rule}",
        f"severity={issue.severity}",
    ]

    if issue.net:
        parts.append(f"net={issue.net}")
    if issue.layer:
        parts.append(f"layer={issue.layer}")
    if issue.obj1:
        parts.append(f"obj1={issue.obj1}")
    if issue.obj2:
        parts.append(f"obj2={issue.obj2}")
    if issue.x is not None and issue.y is not None:
        parts.append(f"xy=({issue.x:.6f},{issue.y:.6f})")

    parts.append(f"message={issue.message}")
    return " | ".join(parts)


def build_issue_analysis_prompt(board, issues: List) -> str:
    lines = []

    lines.append("你是一个PCB DRC分析助手，专门分析BGA escape routing的违规结果。")
    lines.append("请基于给定的DRC issue列表，逐条解释问题、可能原因，并给出可执行的修改建议。")
    lines.append("你的回答要面向PCB工程调试，而不是泛泛而谈。")
    lines.append("")
    lines.append("背景：")
    lines.append("- 输入来自KiCad 5 PCB解析后的常规BGA escape DRC。")
    lines.append("- 当前重点是BGA逃逸布线，不是完整全板签核。")
    lines.append("- 需要关注pad、via、segment、layer、net之间的关系。")
    lines.append("")
    lines.append("当前已知规则说明：")
    for k, v in RULE_DESC.items():
        lines.append(f"- {k}: {v}")

    lines.append("")
    lines.append("板级对象统计：")
    lines.append(f"- nets: {len(board.nets)}")
    lines.append(f"- pads: {len(board.pads)}")
    lines.append(f"- vias: {len(board.vias)}")
    lines.append(f"- segments: {len(board.segments)}")
    lines.append("")

    if not issues:
        lines.append("当前没有发现任何DRC issue。")
        lines.append("请简要说明这意味着什么，并提醒这只是当前已实现规则范围内的结果。")
        return "\n".join(lines)

    lines.append("DRC issues 列表：")
    for i, issue in enumerate(issues, start=1):
        lines.append(_format_issue(issue, i))

    lines.append("")
    lines.append("请按以下格式输出：")
    lines.append("")
    lines.append("总览：")
    lines.append("1. 用几句话总结当前板子的主要问题类型。")
    lines.append("")
    lines.append("逐条分析：")
    lines.append("对每一条issue，输出以下字段：")
    lines.append("- Issue编号")
    lines.append("- 规则编号")
    lines.append("- 问题解释")
    lines.append("- 可能原因")
    lines.append("- 修复建议")
    lines.append("")
    lines.append("最后输出：")
    lines.append("- 优先修复顺序建议")
    lines.append("- 哪些问题可能是连锁导致的")
    lines.append("")
    lines.append("要求：")
    lines.append("- 使用中文")
    lines.append("- 解释要具体")
    lines.append("- 不要编造文件中不存在的对象")
    lines.append("- 不要输出JSON，直接输出可读文本")
    return "\n".join(lines)