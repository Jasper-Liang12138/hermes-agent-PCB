import re
import os
import sys

# ================== 参数检查 ==================
if len(sys.argv) < 3:
    print("❌ 参数不足，请提供两个参数：输入文件 目标器件名")
    print("用法: python 脚本名.py <layout_file> <component_name>")
    sys.exit(1)

INPUT_FILE = sys.argv[1]  # PCB layout 文件
TARGET_COMP = sys.argv[2]  # 目标器件名
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DIFF_OUTPUT = os.path.join(BASE_DIR, "net_list.txt")


# ================== 功能函数 ==================
def extract_block(content, keyword):
    pattern = re.search(r'\(' + keyword + r'\b', content)
    if not pattern:
        return None
    start = pattern.start()
    bracket_count = 0
    in_string = False
    for i in range(start, len(content)):
        char = content[i]
        if char == '"' and (i == 0 or content[i - 1] != "\\"):
            in_string = not in_string
        if in_string:
            continue
        if char == "(":
            bracket_count += 1
        elif char == ")":
            bracket_count -= 1
            if bracket_count == 0:
                return content[start:i + 1]
    return None


def find_matching_paren(s, start_idx):
    depth = 0
    for i in range(start_idx, len(s)):
        c = s[i]
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return i
    return -1


def parse_wire_widths(content):
    """解析 wires 部分，提取每个 net 的线宽"""
    net_widths = {}

    # 先找到 wires 块
    wires_block = extract_block(content, "wires")
    if not wires_block:
        print("⚠️ 没有找到 wires 块")
        return net_widths

    idx = 0
    while True:
        idx = wires_block.find('(wire ', idx)
        if idx == -1:
            break

        end_idx = find_matching_paren(wires_block, idx)
        if end_idx == -1:
            break

        wire_block = wires_block[idx:end_idx + 1]

        # 提取 net 名称
        net_match = re.search(r'\(net\s+"([^"]+)"', wire_block)
        if not net_match:
            idx = end_idx + 1
            continue

        netname = net_match.group(1)

        # 提取第一个 lineseg 中的 w 值
        w_match = re.search(r'\(lineseg[^)]*\(w\s+(\d+)\)', wire_block)
        if not w_match:
            # 尝试另一种格式
            w_match = re.search(r'\(w\s+(\d+)\)', wire_block)

        if w_match:
            width_raw = int(w_match.group(1))
            width = width_raw / 100.0

            # 记录线宽（如果有多个线段，取第一个）
            if netname not in net_widths:
                net_widths[netname] = width

        idx = end_idx + 1

    print(f"📊 从 wires 块中提取到 {len(net_widths)} 个网络的线宽信息")
    return net_widths


def is_diff_net(net_name):
    return "_N_" in net_name or "_P_" in net_name


def group_and_sort_diff_pairs(diff_pairs):
    groups = {}
    for net, p1, p2, width in diff_pairs:
        parts = net.split("_")
        if len(parts) >= 2:
            match = re.match(r'(RX|TX)', parts[1])
            group_key = f"{parts[0]}_{match.group(1)}" if match else f"{parts[0]}_{parts[1]}"
        else:
            group_key = net
        if group_key not in groups:
            groups[group_key] = []
        groups[group_key].append((net, p1, p2, width))

    def sort_key(item):
        net = item[0]
        parts = net.split("_")
        ch_match = re.search(r'\d+', parts[1])
        ch = int(ch_match.group()) if ch_match else 0
        pn = 0 if "_P_" in net else 1
        return (ch, pn)

    for key in groups:
        groups[key].sort(key=sort_key)

    return dict(sorted(groups.items()))


def find_pair_pins(nets_block, net_widths):
    results = []
    net_pattern = re.finditer(r'\(net\s+"([^"]+)"', nets_block)
    for net_match in net_pattern:
        net_name = net_match.group(1)
        start = net_match.start()
        bracket = 0
        net_block = ""
        for i in range(start, len(nets_block)):
            if nets_block[i] == "(":
                bracket += 1
            elif nets_block[i] == ")":
                bracket -= 1
            net_block += nets_block[i]
            if bracket == 0:
                break
        comps = re.findall(r'\(comp\s+"([^"]+)"\s*\(\s*pin\s+"([^"]+)"[^\)]*\)', net_block)
        if not any(c == TARGET_COMP for c, p in comps):
            continue
        if len(comps) != 2:
            continue
        p1 = f"{comps[0][0]}.{comps[0][1]}"
        p2 = f"{comps[1][0]}.{comps[1][1]}"

        # 获取线宽
        width = net_widths.get(net_name, None)

        results.append((net_name, p1, p2, width))
    return results


# ================== 主函数 ==================
def main():
    print("========== Python nets 提取开始 ==========")
    print("目标器件:", TARGET_COMP)
    print("输入文件:", INPUT_FILE)

    if not os.path.exists(INPUT_FILE):
        print("❌ 找不到输入文件:", INPUT_FILE)
        sys.exit(1)

    with open(INPUT_FILE, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    # 提取线宽信息
    net_widths = parse_wire_widths(content)

    nets_block = extract_block(content, "nets")
    if not nets_block:
        print("❌ 没有找到 nets 块")
        sys.exit(1)

    pairs = find_pair_pins(nets_block, net_widths)
    print("共找到", len(pairs), "条连接")

    diff_pairs = [p for p in pairs if is_diff_net(p[0])]
    groups = group_and_sort_diff_pairs(diff_pairs)

    # 统计线宽信息
    nets_with_width = sum(1 for p in diff_pairs if p[3] is not None)
    nets_without_width = len(diff_pairs) - nets_with_width

    with open(DIFF_OUTPUT, "w", encoding="utf-8") as f:
        last_group = None
        for group in sorted(groups):
            if len(groups[group]) < 3:
                continue
            if last_group is not None and group != last_group:
                f.write("\n")
            for net, p1, p2, width in groups[group]:
                # 格式化线宽
                if width is not None:
                    width_str = f"{width:.2f}"
                else:
                    width_str = "N/A"
                f.write(f"{net} ; {p2} {p1} ; {width_str}\n")
            last_group = group

    print("差分信号数量:", len(diff_pairs))
    print(f"📊 线宽统计: 找到 {nets_with_width} 个网络的线宽, {nets_without_width} 个未找到")
    print("✅ 差分对输出 ->", DIFF_OUTPUT)
    print("========== Python 结束 ==========\n")


if __name__ == "__main__":
    main()