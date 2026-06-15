import csv
import math

def parse_pin_csv(csv_file):
    """解析 U22_pins.csv，返回引脚名到坐标的映射"""
    pin_coords = {}
    with open(csv_file, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or row[0].startswith('#') or row[0].startswith('Units'):
                continue
            # 跳过空引脚号的行（如开头两行）
            if not row[0] or row[0].strip() == '':
                continue
            pin_name = row[0].strip()
            try:
                x = float(row[2].strip())
                y = float(row[3].strip())
                pin_coords[pin_name] = (x, y)
            except (ValueError, IndexError):
                continue
    return pin_coords

def parse_netlist(netlist_file):
    """解析 netlist 文件，返回信号名列表、信号到引脚的映射、以及封装名行（如果有）"""
    nets = []  # 保持顺序
    net_to_pin = {}
    footprint_line = None  # 保存封装名行
    
    with open(netlist_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    # 检查最后一行是否是封装名（不包含分号，不是信号行）
    if lines:
        last_line = lines[-1].strip()
        # 判断是否是封装名行：不包含分号且非空
        if last_line and ';' not in last_line:
            footprint_line = last_line
            lines = lines[:-1]  # 移除最后一行，不参与处理
        
        for line in lines:
            line = line.strip()
            if not line or ';' not in line:
                continue
            parts = line.split(';')
            if len(parts) < 2:
                continue
            net_name = parts[0].strip()
            pin_part = parts[1].strip()

            nets.append(net_name)

            # 引脚格式可能是 U22.V3 或 R349.2 U22.U1 等，提取 U22.xxx 部分
            pins = []
            for token in pin_part.split():
                if token.startswith('U22.'):
                    pin = token.replace('U22.', '')
                    pins.append(pin)
            # 如果只有一个引脚，直接存；多个引脚则存列表
            if len(pins) == 1:
                net_to_pin[net_name] = pins[0]
            elif len(pins) > 1:
                net_to_pin[net_name] = pins  # 多个引脚的情况

    return nets, net_to_pin, footprint_line

def get_pin_rank(pin_name, pin_coords):
    """获取引脚在 x 和 y 方向上的排名，返回是否在最外面两圈"""
    if pin_name not in pin_coords:
        return False
    x, y = pin_coords[pin_name]

    # 收集所有引脚的 x 和 y 坐标
    xs = sorted(set([c[0] for c in pin_coords.values()]))
    ys = sorted(set([c[1] for c in pin_coords.values()]))

    if len(xs) < 4 or len(ys) < 4:
        # 如果坐标种类太少，使用原来的边界判断
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        tolerance = 1.0
        return (abs(x - min_x) <= tolerance or abs(x - max_x) <= tolerance or
                abs(y - min_y) <= tolerance or abs(y - max_y) <= tolerance)

    # 获取最外面两圈的边界值
    min_x_1st = xs[0]  # 最外圈最小 x
    min_x_2nd = xs[1]  # 第二圈最小 x
    max_x_1st = xs[-1]  # 最外圈最大 x
    max_x_2nd = xs[-2]  # 第二圈最大 x
    min_y_1st = ys[0]  # 最外圈最小 y
    min_y_2nd = ys[1]  # 第二圈最小 y
    max_y_1st = ys[-1]  # 最外圈最大 y
    max_y_2nd = ys[-2]  # 第二圈最大 y

    tolerance = 1.0

    # 判断是否在最外面两圈
    is_outer_ring = (abs(x - min_x_1st) <= tolerance or abs(x - min_x_2nd) <= tolerance or
                     abs(x - max_x_1st) <= tolerance or abs(x - max_x_2nd) <= tolerance or
                     abs(y - min_y_1st) <= tolerance or abs(y - min_y_2nd) <= tolerance or
                     abs(y - max_y_1st) <= tolerance or abs(y - max_y_2nd) <= tolerance)

    return is_outer_ring

def get_pin_for_net(net, net_to_pin):
    """获取信号对应的引脚（如果是多引脚，返回第一个）"""
    pin_info = net_to_pin.get(net)
    if pin_info is None:
        return None
    if isinstance(pin_info, list):
        return pin_info[0]  # 多引脚时取第一个用于排序
    return pin_info

def sort_nets_clockwise(nets, net_to_pin, pin_coords):
    """按引脚坐标顺时针排序信号列表，从最右下角开始"""
    if not nets:
        return nets
    # 获取每个信号对应的引脚坐标
    net_coords = []
    for net in nets:
        pin = get_pin_for_net(net, net_to_pin)
        if pin and pin in pin_coords:
            net_coords.append((net, pin_coords[pin][0], pin_coords[pin][1]))
        else:
            # 如果没有坐标信息，放到最后
            net_coords.append((net, float('inf'), float('inf')))

    # 按顺时针排序，从最右下角开始（最大 x，最小 y）
    # 先按 x 降序，再按 y 升序，这样最右下角（最大 x，最小 y）会排在最前面
    net_coords.sort(key=lambda item: (-item[1], item[2]))

    return [item[0] for item in net_coords]

def main():
    # 读取 U22_pins.csv
    pin_coords = parse_pin_csv('U22_pins.csv')
    # 读取 netlist 文件
    nets_in_order, net_to_pin, footprint_line = parse_netlist('net_list.txt')

    # 分类：最外面两圈的引脚归为 TOP，其余为 BOTTOM
    top_nets = []
    bottom_nets = []

    for net in nets_in_order:
        pin_info = net_to_pin.get(net)
        if pin_info is None:
            bottom_nets.append(net)
            continue

        # 处理多个引脚的情况
        if isinstance(pin_info, list):
            # 多引脚信号，只要有一个引脚在最外面两圈就算 TOP
            is_top = False
            for pin in pin_info:
                if pin in pin_coords and get_pin_rank(pin, pin_coords):
                    is_top = True
                    break
            if is_top:
                top_nets.append(net)
            else:
                bottom_nets.append(net)
        else:
            # 单引脚
            pin = pin_info
            if pin in pin_coords and get_pin_rank(pin, pin_coords):
                top_nets.append(net)
            else:
                bottom_nets.append(net)

    # 组内排序（顺时针，从最右下角开始）
    top_nets_sorted = sort_nets_clockwise(top_nets, net_to_pin, pin_coords)
    bottom_nets_sorted = sort_nets_clockwise(bottom_nets, net_to_pin, pin_coords)

    # 输出到文件
    with open('order_out.txt', 'w', encoding='utf-8') as f:
        # 输出 TOP 组
        for idx, net in enumerate(top_nets_sorted, 1):
            f.write(f"{net} TOP {idx}\n")

        # 组间空一行
        f.write("\n")

        # 输出 BOTTOM 组
        for idx, net in enumerate(bottom_nets_sorted, 1):
            f.write(f"{net} BOTTOM {idx}\n")
        
        # 如果有封装名行，追加到最后一行（不参与排序）
        if footprint_line:
            f.write(f"\n{footprint_line}\n")

    print(f"结果已保存到 order_out.txt")
    print(f"TOP 组：{len(top_nets_sorted)} 个信号")
    print(f"BOTTOM 组：{len(bottom_nets_sorted)} 个信号")
    if footprint_line:
        print(f"封装名已保留：{footprint_line}")

if __name__ == "__main__":
    main()