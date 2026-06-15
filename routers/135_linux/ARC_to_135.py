import re


def parse_parameter(filename):
    """解析 parameter.txt 获取设计参数"""
    params = {}
    with open(filename, 'r') as f:
        content = f.read()

        # 提取 origin
        origin_match = re.search(r'"origin"\s*:\s*\{[^}]*"x"\s*:\s*([\d.]+).*?"y"\s*:\s*([\d.]+)', content, re.DOTALL)
        if origin_match:
            params['origin_x'] = float(origin_match.group(1))
            params['origin_y'] = float(origin_match.group(2))

        # 提取 boundary (取第一个点)
        boundary_match = re.search(r'"boundary"\s*:\s*\[\s*\[\s*([\d.]+)\s*,\s*([\d.]+)\s*\]', content)
        if boundary_match:
            params['boundary_x'] = float(boundary_match.group(1))
            params['boundary_y'] = float(boundary_match.group(2))

        # 提取其他数值参数
        keys_map = {
            'pin_radius': 'pin_radius',
            'pin_spacing': 'pin_spacing',
            'pairspacing': 'pairspacing',
            'viaradius': 'viaradius',
            'vialinespacing': 'vialinespacing'
        }

        for key, param_name in keys_map.items():
            match = re.search(rf'"{key}"\s*:\s*([\d.]+)', content)
            if match:
                params[param_name] = float(match.group(1))

    return params


def parse_pins_csv(filename, package_name):
    """解析 U22_pins.csv 获取引脚坐标，根据动态封装名称过滤"""
    pins = []
    prefix = f"{package_name}."

    with open(filename, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split(',')
            if len(parts) >= 5:
                pin_name = parts[0].strip()
                if not pin_name:
                    continue
                try:
                    x = float(parts[2])
                    y = float(parts[3])
                    pins.append((pin_name, x, y))
                except ValueError:
                    continue
    return pins


def parse_netlist(filename):
    """
    解析 net_list_out.txt
    根据空行分组，分成三组：
    - 第一组：信号线
    - 第二组：电源线
    - 第三组：封装名称（如 U22）
    """
    all_nets = {}
    power_nets_set = set()
    package_name = "U22"  # 默认值

    with open(filename, 'r') as f:
        content = f.read()

    # 按空行分组
    groups = re.split(r'\n\s*\n', content.strip())

    # 根据组数处理
    if len(groups) >= 3:
        signal_groups = groups[:-2]  # 第一组（可能有多组信号线）
        power_group = groups[-2]  # 第二组：电源线
        package_group = groups[-1]  # 第三组：封装名称
    elif len(groups) == 2:
        signal_groups = groups[:-1]  # 第一组：信号线
        power_group = groups[-1]  # 第二组：电源线
        package_group = ""
    else:
        signal_groups = groups
        power_group = ""
        package_group = ""

    # 从第三组提取封装名称
    if package_group.strip():
        # 第三组可能格式：PACKAGE_NAME;U22;0 或直接是 U22
        pkg_lines = package_group.strip().split('\n')
        for pkg_line in pkg_lines:
            pkg_line = pkg_line.strip()
            if not pkg_line:
                continue
            # 尝试解析格式：NAME;U22;WIDTH
            parts = re.split(r'\s*;\s*', pkg_line)
            if len(parts) >= 2:
                # 第二个字段可能是封装名称
                potential_pkg = parts[1].strip()
                if potential_pkg and not potential_pkg.replace('.', '').isdigit():
                    package_name = potential_pkg
                    break
            elif len(parts) == 1:
                # 直接就是封装名称
                package_name = parts[0].strip()
                break

    print(f"检测到封装名称：{package_name}")
    prefix = f"{package_name}."

    # 解析信号线组
    for group in signal_groups:
        for line in group.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            parts = re.split(r'\s*;\s*', line)
            if len(parts) < 3:
                continue
            net_name = parts[0].strip()
            pins_str = parts[1].strip()
            try:
                width = float(parts[2].strip())
            except ValueError:
                continue

            pin_tokens = pins_str.split()
            pkg_pins = [token.split('.')[1] for token in pin_tokens if token.startswith(prefix)]

            if pkg_pins:
                all_nets[net_name] = {'pins': pkg_pins, 'width': width, 'is_power': False}

    # 解析电源线组（第二组）
    for line in power_group.strip().split('\n'):
        line = line.strip()
        if not line:
            continue
        parts = re.split(r'\s*;\s*', line)
        if len(parts) < 3:
            continue
        net_name = parts[0].strip()
        pins_str = parts[1].strip()
        try:
            width = float(parts[2].strip())
        except ValueError:
            continue

        pin_tokens = pins_str.split()
        pkg_pins = [token.split('.')[1] for token in pin_tokens if token.startswith(prefix)]

        if pkg_pins:
            all_nets[net_name] = {'pins': pkg_pins, 'width': width, 'is_power': True}
            power_nets_set.add(net_name)

    return all_nets, power_nets_set, package_name


def parse_order(filename, package_name):
    """
    解析 order_out.txt
    返回层名到索引的映射、线网条目列表、层数和层名列表
    """
    layers_seen = []
    net_entries = []

    with open(filename, 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            parts = line.split()
            if len(parts) >= 3:
                net_name = parts[0]
                layer_name_raw = parts[1]
                order = int(parts[2])

                if layer_name_raw not in layers_seen:
                    layers_seen.append(layer_name_raw)

                net_entries.append((net_name, layer_name_raw, order))

    layer_order_map = {name: idx for idx, name in enumerate(layers_seen)}
    k = len(layers_seen)

    processed_entries = []
    for net_name, layer_raw, order_val in net_entries:
        layer_idx = layer_order_map[layer_raw]
        processed_entries.append((net_name, layer_idx, order_val))

    return layer_order_map, processed_entries, k, layers_seen


def main():
    # 1. 读取参数
    params = parse_parameter('parameter.txt')

    origin_x = params['origin_x'] * 100
    origin_y = params['origin_y'] * 100

    boundary_x_raw = params['boundary_x']
    boundary_y_raw = params['boundary_y']

    delta_x = abs(origin_x - (boundary_x_raw * 100))
    delta_y = abs(origin_y - (boundary_y_raw * 100))

    top_pair_spacing = params['pairspacing'] * 100
    bottom_pair_spacing = params['pairspacing'] * 100
    pin_line_spacing = params['pin_spacing'] * 100
    via_line_spacing = params['vialinespacing'] * 100
    top_diameter = params['pin_radius'] * 200
    via_diameter = params['viaradius'] * 200

    # 2. 读取线网数据（先读取以获取封装名称）
    all_nets, power_nets_set, package_name = parse_netlist('net_list.txt')

    # 3. 读取引脚数据（使用动态封装名称）
    pins_data = parse_pins_csv('U22_pins.csv', package_name)
    m = len(pins_data)

    # 4. 读取逃逸顺序数据
    layer_map, order_entries, k, layers_seen = parse_order('order_out.txt', package_name)

    # 5. 计算特定线宽
    # 电源线线宽
    max_power_width = 0
    for net_name in power_nets_set:
        if net_name in all_nets:
            w = all_nets[net_name]['width']
            if w > max_power_width:
                max_power_width = w
    power_line_width = max_power_width * 100

    # Top 层信号线宽 (layer_idx == 0) - 只考虑线宽 < 10 的
    max_top_signal_width = 0
    max_bottom_signal_width = 0

    for net_name, layer_idx, order_val in order_entries:
        if net_name not in all_nets:
            continue
        if all_nets[net_name]['is_power']:
            continue

        w = all_nets[net_name]['width']

        # 只考虑线宽 < 10 的信号线
        if w >= 10:
            continue

        if layer_idx == 0:
            if w > max_top_signal_width:
                max_top_signal_width = w
        elif layer_idx == 1:
            if w > max_bottom_signal_width:
                max_bottom_signal_width = w

    top_signal_width = max_top_signal_width * 100
    bottom_signal_width = max_bottom_signal_width * 100

    # 6. 构建输出列表 (n 行数据)
    output_lines_data = []

    for net_name, layer_idx, order_val in order_entries:
        if net_name not in all_nets:
            continue

        net_info = all_nets[net_name]
        pin_list = net_info['pins']

        # 如果线网是电源线，按电源线规则处理（逃逸层 = -1）
        if net_info['is_power']:
            pin_type = 0
            escape_layer = -1
            escape_order = 1
        else:
            pin_type = 1
            escape_layer = layer_idx
            escape_order = order_val

        for pin_name in pin_list:
            output_lines_data.append(f"{pin_name} {net_name} {pin_type} {escape_order} {escape_layer}")

    n = len(output_lines_data)

    # 7. 计算 m 行坐标数据
    coord_lines = []
    for pin_name, x_raw, y_raw in pins_data:
        x_scaled = x_raw * 100
        y_scaled = y_raw * 100
        rel_x = x_scaled - origin_x
        rel_y = y_scaled - origin_y
        coord_lines.append(f"{pin_name} {rel_x:.0f} {rel_y:.0f}")

    # 8. 生成最终输出文件
    output_filename = 'line.in'

    with open(output_filename, 'w') as f:
        # 第一行
        line1_parts = [
            str(k),
            f"{origin_x:.0f}",
            f"{origin_y:.0f}",
            f"{delta_x:.0f}",
            f"{delta_y:.0f}",
            f"{power_line_width:.0f}",
            f"{top_signal_width:.0f}",
            f"{bottom_signal_width:.0f}",
            f"{top_pair_spacing:.0f}",
            f"{bottom_pair_spacing:.0f}",
            f"{pin_line_spacing:.0f}",
            f"{via_line_spacing:.0f}",
            f"{top_diameter:.0f}",
            f"{via_diameter:.0f}",
            str(n)
        ]
        f.write(" ".join(line1_parts) + "\n")

        # 接下来 n 行
        for line in output_lines_data:
            f.write(line + "\n")

        # 第 n+2 行：m
        f.write(f"{m}\n")

        # 接下来 m 行
        for line in coord_lines:
            f.write(line + "\n")

        # 最后 k 行：层索引和层名称对应关系
        for idx, layer_name in enumerate(layers_seen):
            f.write(f"{idx} {layer_name}\n")

    print(f"成功生成文件：{output_filename}")
    print(f"封装名称：{package_name}")
    print(f"层数 k: {k}")
    print(f"线网总数 n: {n}")
    print(f"引脚总数 m: {m}")
    print(f"电源线网：{power_nets_set}")
    print(f"电源线线宽：{power_line_width}")
    print(f"Top 信号线宽：{top_signal_width}")
    print(f"Bottom 信号线宽：{bottom_signal_width}")
    print(f"层名称映射：{layers_seen}")


if __name__ == "__main__":
    main()