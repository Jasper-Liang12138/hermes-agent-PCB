import re
import json
import os
import sys

# ================== 路径处理 ==================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ================== 参数处理 ==================
if len(sys.argv) < 2:
    print("❌ 参数错误！请按格式执行: python 脚本名.py <输入文件>")
    sys.exit(1)

INPUT_FILE = sys.argv[1]

# 可以打印确认
print("输入文件:", INPUT_FILE)

OUTPUT_FILE = os.path.join(BASE_DIR, "parameter.txt")


# ================== 工具函数 ==================
def extract_block(content, keyword):
    """提取第一个完整括号块"""
    m = re.search(r'\(' + keyword + r'\b', content)
    if not m:
        return None

    start = m.start()
    bracket = 0

    for i in range(start, len(content)):
        if content[i] == '(':
            bracket += 1
        elif content[i] == ')':
            bracket -= 1

        if bracket == 0:
            return content[start:i+1]

    return None


# ================== 主逻辑 ==================
def main():
    print("========== 参数提取开始 ==========")
    print("输入文件:", INPUT_FILE)

    if not os.path.exists(INPUT_FILE):
        print("❌ 输入文件不存在")
        return

    with open(INPUT_FILE, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()

    # -------------------------
    # 提取 linewidth
    # -------------------------
    linewidth = None

    pattern = re.search(
        r'\(wire.*?\(net\s+"(QSFPDD[^"]+)"\).*?\(w\s+(\d+)\)',
        text,
        re.S
    )

    if pattern:
        linewidth = int(pattern.group(2)) // 100
        print("✔ LineWidth =", linewidth)
    else:
        print("⚠ 未找到 LineWidth")

    # -------------------------
    # 提取 pairspacing
    # -------------------------
    pairspacing = None

    diff_block = extract_block(text, "diff")

    if diff_block:
        space_match = re.search(
            r'propname\s+"DIFFP_MIN_SPACE"\s+propvalue\s+"(\d+)',
            diff_block
        )

        if space_match:
            pairspacing = int(space_match.group(1))
            print("✔ PairSpacing =", pairspacing)
        else:
            print("⚠ 未找到 PairSpacing")
    else:
        print("⚠ 未找到 diff 块")

    # -------------------------
    # 默认值兜底（防止C++读0）
    # -------------------------
    if linewidth is None:
        linewidth = 3

    if pairspacing is None:
        pairspacing = 5

    # -------------------------
    # 生成参数
    # -------------------------
    result = {
        "linewidth": linewidth,
        "pairspacing": pairspacing,
        "viaradius": 8,
        "vialinespacing": 3,
        "keepoutlength": 8,
        "keepoutradius": 14.5
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=4)

    print("✅ 参数已写入:", OUTPUT_FILE)
    print("========== 结束 ==========\n")


# ================== 入口 ==================
if __name__ == "__main__":
    main()