import re
import os
import sys

# ================== 参数处理 ==================
if len(sys.argv) < 3:
    print("❌ 参数不足，请提供两个参数: 输入文件路径 组件名")
    sys.exit(1)

INPUT_FILE = sys.argv[1]
TARGET_COMP = sys.argv[2]

# ================== 输出文件 ==================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DIFF_OUTPUT = os.path.join(BASE_DIR, "layer_input.txt")

# ================== 层名定义 ==================
LAYER_NAMES = [
    "SIG03", "SIG05", "SIG07", "SIG09",
    "SIG11", "Pwr13", "Pwr15", "Pwr16"
]

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


def is_diff_net(net_name):
    return "_N_" in net_name or "_P_" in net_name


def group_and_sort_diff_pairs(diff_pairs):
    groups = {}
    for net, p1, p2 in diff_pairs:
        parts = net.split("_")
        if len(parts) > 2:
            match = re.match(r'(RX|TX)', parts[1])
            if match:
                group_key = f"{parts[0]}_{match.group(1)}"
            else:
                group_key = f"{parts[0]}_{parts[1]}"
        else:
            group_key = net

        groups.setdefault(group_key, []).append((net, p1, p2))

    def sort_key(item):
        net = item[0]
        parts = net.split("_")
        ch_match = re.search(r'\d+', parts[1])
        ch = int(ch_match.group()) if ch_match else 0
        pn = 0 if "_N_" in net else 1
        return (ch, pn)

    for key in groups:
        groups[key].sort(key=sort_key)

    return dict(sorted(groups.items()))


def find_pair_pins(nets_block):
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
        comps = re.findall(
            r'\(comp\s+"([^"]+)"\s*\(\s*pin\s+"([^"]+)"[^\)]*\)',
            net_block
        )
        if not any(c == TARGET_COMP for c, p in comps):
            continue
        if len(comps) != 2:
            continue
        p1 = f"{comps[0][0]}.{comps[0][1]}"
        p2 = f"{comps[1][0]}.{comps[1][1]}"
        results.append((net_name, p1, p2))
    return results


# ================== 主函数 ==================

def main():
    print("========== Python nets 提取开始 ==========")
    print("目标器件:", TARGET_COMP)
    print("输入文件:", INPUT_FILE)

    if not os.path.exists(INPUT_FILE):
        print("❌ 找不到输入文件:", INPUT_FILE)
        return

    with open(INPUT_FILE, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    nets_block = extract_block(content, "nets")
    if not nets_block:
        print("❌ 没有找到 nets 块")
        return

    pairs = find_pair_pins(nets_block)
    print("共找到", len(pairs), "条连接")

    diff_pairs = [p for p in pairs if is_diff_net(p[0])]
    groups = group_and_sort_diff_pairs(diff_pairs)

    # ================== 输出 ==================
    with open(DIFF_OUTPUT, "w", encoding="utf-8") as f:
        last_group = None
        layer_idx = 0
        for group in sorted(groups):
            if len(groups[group]) < 3:
                continue
            if last_group is not None and group != last_group:
                f.write("\n")
                layer_idx += 1
                if layer_idx >= len(LAYER_NAMES):
                    layer_idx = len(LAYER_NAMES) - 1
            layer = LAYER_NAMES[layer_idx]
            for net, _, _ in groups[group]:
                f.write(f"{net} {layer}\n")
            last_group = group
        f.write("\n")
        f.write(f"{TARGET_COMP}\n")
    print("差分信号数量:", len(diff_pairs))
    print("✅ 差分对输出 ->", DIFF_OUTPUT)
    print("========== Python 结束 ==========\n")


# ================== 入口 ==================
if __name__ == "__main__":
    main()