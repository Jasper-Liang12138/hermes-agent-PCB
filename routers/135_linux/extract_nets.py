#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""从 Altium/ARC 风格文本中提取 net，并输出 comp.pin 列表

功能：
1. 只输出包含指定封装（如 U22）的信号网
2. 电源/地网单独放到最后
3. 每条 net 中，将目标封装（如 U22.xxx）放到最后
4. 添加线宽信息

用法：
    python script.py 输入文件 封装名 [输出文件]

示例：
    python script.py 402Pin.txt U22
    python script.py 402Pin.txt U22 result.txt
"""

import re
import sys


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


def extract_comp_pin_from_comp_block(comp_text):
    m = re.search(r'\(comp\s+"([^"]+)"', comp_text)
    if not m:
        return None
    comp = m.group(1)

    m2 = re.search(r'\(pin\s+"([^"]+)"', comp_text)
    if not m2:
        m2 = re.search(r'\(pin\s+([^\)\s]+)', comp_text)
        if not m2:
            return None

    pin = m2.group(1)
    return f"{comp}.{pin}"


def parse_nets(text):
    nets = []
    idx = 0

    while True:
        idx = text.find('(net ', idx)
        if idx == -1:
            break

        end_idx = find_matching_paren(text, idx)
        if end_idx == -1:
            break

        net_block = text[idx:end_idx + 1]

        m = re.search(r'\(net\s+"([^"]+)"', net_block)
        if not m:
            idx = end_idx + 1
            continue

        netname = m.group(1)

        comps = []
        cidx = 0

        while True:
            cidx = net_block.find('(comp ', cidx)
            if cidx == -1:
                break

            c_end = find_matching_paren(net_block, cidx)
            if c_end == -1:
                break

            comp_block = net_block[cidx:c_end + 1]
            cp = extract_comp_pin_from_comp_block(comp_block)

            if cp:
                comps.append(cp)

            cidx = c_end + 1

        nets.append((netname, comps))
        idx = end_idx + 1

    return nets


def parse_wire_widths(text):
    """解析 wires 部分，提取每个 net 的线宽"""
    net_widths = {}

    idx = 0
    while True:
        idx = text.find('(wire ', idx)
        if idx == -1:
            break

        end_idx = find_matching_paren(text, idx)
        if end_idx == -1:
            break

        wire_block = text[idx:end_idx + 1]

        # 提取 net 名称
        net_match = re.search(r'\(net\s+"([^"]+)"', wire_block)
        if not net_match:
            idx = end_idx + 1
            continue

        netname = net_match.group(1)

        # 提取线宽
        w_match = re.search(r'\(lineseg[^)]*\(w\s+(\d+)\)', wire_block)
        if not w_match:
            w_match = re.search(r'\(w\s+(\d+)\)', wire_block)

        if w_match:
            width_raw = int(w_match.group(1))
            width = width_raw / 100.0

            if netname not in net_widths:
                net_widths[netname] = width

        idx = end_idx + 1

    return net_widths


def is_power_or_ground(netname: str) -> bool:
    if not netname:
        return False

    s = re.sub(r'[^a-z0-9]', '', netname.lower())

    power_tokens = {
        'vcc', 'vdd', 'avdd', 'dvdd', 'vcore', 'vio', 'vtt', '1v8', '3v3', '5v0'
    }
    ground_tokens = {'gnd', 'agnd', 'dgnd', 'vss', 'ground'}

    for t in power_tokens.union(ground_tokens):
        if s == t or s.startswith(t) or s.endswith(t):
            return True

    return False


def contains_target_comp(comps, target):
    for cp in comps:
        if cp.startswith(target + "."):
            return True
    return False


def move_target_to_end(comps, target):
    """把目标封装（如U22）引脚移动到最后"""
    target_pins = []
    other_pins = []

    for cp in comps:
        if cp.startswith(target + "."):
            target_pins.append(cp)
        else:
            other_pins.append(cp)

    return other_pins + target_pins


def main():
    # ===== 参数处理 =====
    if len(sys.argv) < 3:
        print("❌ 用法: python script.py <输入文件> <封装名> [输出文件]")
        print("示例: python script.py 402Pin.txt U22")
        return

    INPUT_FILE = sys.argv[1]
    TARGET_COMP = sys.argv[2]
    OUTPUT_FILE = sys.argv[3] if len(sys.argv) > 3 else "net_list.txt"

    # ===== 读取文件 =====
    try:
        with open(INPUT_FILE, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
    except Exception as e:
        print(f"❌ 无法读取文件: {INPUT_FILE}")
        print(e)
        return

    # ===== 解析 =====
    nets = parse_nets(text)
    net_widths = parse_wire_widths(text)

    # ===== 统计 =====
    nets_with_width = 0
    nets_without_width = 0
    written = 0
    skipped_pkg = 0
    power_nets = []

    # ===== 写文件 =====
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as fo:

        # --- 信号网 ---
        for netname, comps in nets:
            if not comps:
                continue

            if is_power_or_ground(netname):
                power_nets.append((netname, comps))
                continue

            if not contains_target_comp(comps, TARGET_COMP):
                skipped_pkg += 1
                continue

            ordered = move_target_to_end(comps, TARGET_COMP)

            width = net_widths.get(netname)
            if width:
                width_str = f"{width:.2f}"
                nets_with_width += 1
            else:
                width_str = "N/A"
                nets_without_width += 1

            fo.write(f"{netname} ; {' '.join(ordered)} ; {width_str}\n")
            written += 1

        fo.write("\n")

        # --- 电源/地 ---
        for netname, comps in power_nets:
            if not contains_target_comp(comps, TARGET_COMP):
                continue

            ordered = move_target_to_end(comps, TARGET_COMP)

            width = net_widths.get(netname)
            width_str = f"{width:.2f}" if width else "N/A"

            fo.write(f"{netname} ; {' '.join(ordered)} ; {width_str}\n")


        fo.write("\n")
        fo.write(f"{TARGET_COMP}\n")

    # ===== 输出统计 =====
    print(f"✅ 写入文件: {OUTPUT_FILE}")
    print(f"📂 输入文件: {INPUT_FILE}")
    print(f"🎯 目标封装: {TARGET_COMP}")
    print(f"总 net 数: {len(nets)}")
    print(f"信号网写出: {written}")
    print(f"电源/地数量: {len(power_nets)}")
    print(f"跳过非 {TARGET_COMP}: {skipped_pkg}")
    print(f"找到线宽的 nets: {nets_with_width}")
    print(f"未找到线宽的 nets: {nets_without_width}")


if __name__ == '__main__':
    main()