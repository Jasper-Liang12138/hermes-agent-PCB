import re
import csv
import os
import sys
# 示例：python get_pins.py 402Pin_08BGA_8L_S_01141700.txt U22
# ================== 参数处理 ==================
if len(sys.argv) < 3:
    print("❌ 参数错误！请按格式执行: python 脚本名.py <输入文件> <组件名>")
    sys.exit(1)

INPUT_FILE = sys.argv[1]
COMPONENT_NAME = sys.argv[2]

print("输入文件:", INPUT_FILE)
print("组件名:", COMPONENT_NAME)

OUTPUT_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{COMPONENT_NAME}_pins.csv")


# ================== 核心函数 ==================

def extract_component_block(content, start_pos):
    bracket_count = 0
    started = False
    in_string = False
    end_pos = start_pos

    for i in range(start_pos, len(content)):
        char = content[i]
        if char == '"' and (i == 0 or content[i - 1] != '\\'):
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == '(':
            bracket_count += 1
            started = True
        elif char == ')':
            bracket_count -= 1
            if started and bracket_count == 0:
                end_pos = i + 1
                break
    return content[start_pos:end_pos]


def find_component(content):
    pattern = re.compile(r'\(component\s+"([^"]+)"', re.IGNORECASE)
    for match in pattern.finditer(content):
        name = match.group(1)
        if COMPONENT_NAME.lower() in name.lower():
            print("找到 component:", name)
            start_pos = match.start()
            return extract_component_block(content, start_pos)
    return None


def extract_padstack(block):
    padstack_match = re.search(r'\(padstack\s+"([^"]+)"', block, re.S)
    return padstack_match.group(1) if padstack_match else ""


def extract_mirror_status(component_block):
    """ 提取镜像状态（mirrored） """
    mirror_match = re.search(r'\(mirrored\s+"(true|false)"', component_block, re.IGNORECASE)
    return "1" if mirror_match and mirror_match.group(1).lower() == "true" else "0"


def extract_pins(component_block):
    pins = []
    mirror_status = extract_mirror_status(component_block)  # 获取组件的镜像状态

    pin_blocks = re.findall(r'\(pin\b(.*?)\)\s*\)', component_block, re.S)

    for block in pin_blocks:
        pin_name_match = re.search(r'\(number\s+"([^"]+)"\)', block)
        xy = re.search(r'\(xy\s+([-\d]+)\s+([-\d]+)\)', block)
        rotate = re.search(r'\(rotation\s+([-\d]+)\)', block)
        padstack = extract_padstack(block)

        pin_name = pin_name_match.group(1) if pin_name_match else ""

        if xy:
            x_val = int(xy.group(1)) / 100
            y_val = int(xy.group(2)) / 100
        else:
            x_val = ""
            y_val = ""

        rotate_val = str(int(rotate.group(1)) / 100) if rotate else ""

        pins.append({
            "PinName": pin_name,
            "Padstack": padstack,
            "X": str(x_val) if x_val != "" else "",
            "Y": str(y_val) if y_val != "" else "",
            "Rotate": rotate_val,
            "Mirror": mirror_status  # 添加镜像状态列
        })

    return pins


# ================== 主函数 ==================
def main():
    print("========== Python 引脚提取开始 ==========")
    print("当前组件:", COMPONENT_NAME)
    print("输入文件:", INPUT_FILE)
    print("输出文件:", OUTPUT_CSV)

    if not os.path.exists(INPUT_FILE):
        print("❌ 找不到输入文件:", INPUT_FILE)
        return

    with open(INPUT_FILE, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()

    component_block = find_component(content)

    if component_block is None:
        print("❌ 没有找到 Component:", COMPONENT_NAME)
        return

    pins = extract_pins(component_block)
    print("找到", len(pins), "个引脚")

    # ===== 导出 CSV =====
    with open(OUTPUT_CSV, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        f.write("# If units not specified use current design units\n")
        f.write("Units mils\n\n")
        f.write("# Format for pin definition file (comma delineated)\n")
        writer.writerow(["PinNumber", "Padstack", "x", "y", "rotation", "mirror"])

        for pin in pins:
            writer.writerow([
                pin["PinName"],
                pin["Padstack"],
                pin["X"],
                pin["Y"],
                pin["Rotate"],
                pin["Mirror"]  # 写入镜像值到CSV
            ])

    print("✅ CSV导出完成:", OUTPUT_CSV)
    print("========== Python 结束 ==========\n")


if __name__ == "__main__":
    main()