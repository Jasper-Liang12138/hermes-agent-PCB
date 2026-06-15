from collections import OrderedDict
from decimal import Decimal, ROUND_HALF_UP
import os
import sys
import argparse
import re


def scale100(value):
    """
    Convert numeric string to integer after multiplying by 100.
    Example:
    4008.9 -> 400890
    3 -> 300
    """
    try:
        d = Decimal(str(value)) * Decimal("100")
        return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except:
        return 0


def normalize_rotate(direction):
    """
    Convert source direction to target rotate value.
    """
    direction = direction.strip().upper()
    if direction == "CLOCKWISE":
        return "CW"
    elif direction == "COUNTERCLOCKWISE":
        return "CCW"
    return direction


def normalize_layer(layer):
    """
    Convert input layer name to target format:
    bottom -> Conductor/Bottom
    top -> Conductor/Top
    sig03 -> Conductor/Sig03
    """
    layer = layer.strip()
    if not layer:
        return "Conductor/Unknown"
    return f"Conductor/{layer.capitalize()}"


def get_conductor_layers(file_content):
    """
    Parse the target PCB file content to extract all conductor/plane layer names from stackup.
    Returns a list of layer names (e.g., ["Top", "Gnd02", ..., "Bottom"]).
    """
    layers = []
    try:
        start_idx = file_content.find("(layermanager")
        if start_idx == -1:
            print("Warning: Cannot find (layermanager) section")
            return layers

        stackup_start = file_content.find("(stackup", start_idx)
        if stackup_start == -1:
            print("Warning: Cannot find (stackup) section")
            return layers

        depth = 0
        block_end = -1
        for i in range(stackup_start, len(file_content)):
            if file_content[i] == '(':
                depth += 1
            elif file_content[i] == ')':
                depth -= 1
                if depth == 0:
                    block_end = i
                    break

        if block_end != -1:
            stackup_content = file_content[stackup_start:block_end + 1]

            layer_pattern = r'\(layer\s+"([^"]*)"\s*\n\s*\(ltype\s+"([^"]+)"\)'
            matches = re.finditer(layer_pattern, stackup_content)

            for match in matches:
                layer_name = match.group(1).strip()
                layer_type = match.group(2).strip()

                if layer_name and layer_type in ["Conductor", "Plane"]:
                    layers.append(layer_name)

        if layers:
            print(f"Found {len(layers)} conductor layers: {layers}")
        else:
            print("Warning: No conductor layers found")

    except Exception as e:
        print(f"Error parsing layers: {e}")

    return layers


def clean_wires_vias_blocks(content):
    """
    Clean existing (wires) and (vias) block content, keep the structure.
    """

    def remove_block_content(match):
        return match.group(1) + "\n)\n"

    pattern = r'(\(wires|\(vias)\s*.*?^\)'
    cleaned_content = re.sub(pattern, remove_block_content, content, flags=re.DOTALL | re.MULTILINE)

    return cleaned_content


def parse_arc_output(file_path):
    """
    Parse ARC_output.txt
    LINE:
        layer!LINE!id!net!x1!y1!x2!y2!width
    ARC:
        layer!ARC!id!net!x1!y1!x2!y2!cx!cy!radius!width!direction
    CIRCLE (Via):
        layer!CIRCLE!id!net!x!y!size

    Returns: wires_list (each element is independent), vias_list, via_count
    """
    wires_list = []
    vias_list = []
    via_count = 0

    with open(file_path, "r", encoding="utf-8") as f:
        for line_no, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue

            parts = line.split("!")
            if len(parts) < 2:
                print(f"[Skip] Line {line_no}: invalid format")
                continue

            raw_layer = parts[0].strip()
            layer = normalize_layer(raw_layer)
            elem_type = parts[1].strip().upper()

            if elem_type == "LINE":
                if len(parts) != 9:
                    print(f"[Skip] Line {line_no}: LINE format error -> {line}")
                    continue

                _, _, obj_id, net, x1, y1, x2, y2, width = parts

                item = {
                    "type": "LINE",
                    "layer": layer,
                    "net": net.strip(),
                    "start": (scale100(x1), scale100(y1)),
                    "end": (scale100(x2), scale100(y2)),
                    "width": scale100(width),
                }
                wires_list.append(item)

            elif elem_type == "ARC":
                if len(parts) != 13:
                    print(f"[Skip] Line {line_no}: ARC format error -> {line}")
                    continue

                _, _, obj_id, net, x1, y1, x2, y2, cx, cy, radius, width, direction = parts

                item = {
                    "type": "ARC",
                    "layer": layer,
                    "net": net.strip(),
                    "start": (scale100(x1), scale100(y1)),
                    "end": (scale100(x2), scale100(y2)),
                    "center": (scale100(cx), scale100(cy)),
                    "radius": scale100(radius),
                    "width": scale100(width),
                    "rotate": normalize_rotate(direction),
                }
                wires_list.append(item)

            elif elem_type == "CIRCLE":
                if len(parts) < 6:
                    print(f"[Skip] Line {line_no}: CIRCLE format error -> {line}")
                    continue

                _, _, obj_id, net, x, y, size = parts[:7]

                via_item = {
                    "net": net.strip(),
                    "xy": (scale100(x), scale100(y)),
                }
                vias_list.append(via_item)
                via_count += 1

            else:
                pass

    return wires_list, vias_list, via_count


def build_wire_blocks(wires_list):
    """
    Build independent (wire ...) blocks for each element.
    Each wire segment gets its own (wire ...) block, even if same net.
    """
    lines = []
    for item in wires_list:
        lines.append("    (wire")
        lines.append(f'        (net  "{item["net"]}")')
        lines.append("        (path")
        lines.append('            (issamewidth  "true")')

        lines.append("            (lineseg")
        lines.append(f"                (pt {item['start'][0]} {item['start'][1]})")
        lines.append(f"                (w {item['width']})")
        lines.append("            )")

        if item["type"] == "LINE":
            lines.append("            (lineseg")
            lines.append(f"                (pt {item['end'][0]} {item['end'][1]})")
            lines.append(f"                (w {item['width']})")
            lines.append("            )")

        elif item["type"] == "ARC":
            lines.append("            (arcseg")
            lines.append(f"                (pt {item['end'][0]} {item['end'][1]})")
            lines.append(f"                (w {item['width']})")
            lines.append(f"                (xy {item['center'][0]} {item['center'][1]})")
            lines.append(f"                (rotate {item['rotate']})")
            lines.append("            )")

        lines.append("            (props)")
        lines.append(f'            (layer  "{item["layer"]}")')
        lines.append("        )")
        lines.append("    )")

    return "\n".join(lines) + "\n"


def build_via_blocks(vias_data, layer_list, padstack_name="VIA16D8"):
    """
    Build (via ...) blocks for all parsed circles.
    """
    lines = []

    if not layer_list:
        print("Warning: No layer list provided, using default Top/Bottom")
        layer_list = ["Top", "Bottom"]

    connection_lines = []
    for layer_name in layer_list:
        connection_lines.append(f'                (layer "{layer_name}"')
        connection_lines.append('                    (paduse "connect")')
        connection_lines.append('                )')

    connection_block = "\n".join(connection_lines)

    for via in vias_data:
        net = via["net"]
        x, y = via["xy"]

        lines.append("    (via")
        lines.append(f'        (net "{net}")')
        lines.append(f"        (xy {x} {y})")
        lines.append('        (rotation 0)')
        lines.append('        (mirrored "false")')
        lines.append('        (testpoint "")')
        lines.append(f'        (padstack "{padstack_name}"')
        lines.append("            (connection")
        lines.append(connection_block)
        lines.append("            )")
        lines.append("        )")
        lines.append("        (props)")
        lines.append("    )")

    return "\n".join(lines) + "\n"


def find_matching_paren(text, open_pos):
    """
    Given position of '(', find its matching ')'
    """
    depth = 0
    for i in range(open_pos, len(text)):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def insert_into_group(content, group_name, insert_text):
    """
    Find existing (group_name ...) group and insert text before its closing ')'.
    """
    search_pattern = f"({group_name}"
    group_start = content.find(search_pattern)

    if group_start == -1:
        print(f"Warning: Cannot find '({group_name}' group in target file. Skipping {group_name} insertion.")
        return content

    group_end = find_matching_paren(content, group_start)
    if group_end == -1:
        print(f"Error: Cannot find matching closing ')' for '({group_name}' group.")
        return content

    prefix = content[:group_end].rstrip()
    suffix = content[group_end:]

    new_content = prefix + "\n" + insert_text + suffix
    return new_content


def main():
    parser = argparse.ArgumentParser(
        description='Clean and parse ARC output, then insert wire/via blocks into target file.',
        epilog='Example: python Turn_QYF.py 1234_1.txt arc_output.txt output.txt'
    )
    parser.add_argument(
        'target_file',
        help='Path to target file containing (wires ...) and (vias ...) groups (e.g., 402Pin_...txt)'
    )
    parser.add_argument(
        'arc_file',
        help='Path to ARC output file containing LINE, ARC, and CIRCLE definitions (e.g., arc_output.txt)'
    )
    parser.add_argument(
        'output_file',
        help='Path to output file (e.g., output.txt)'
    )
    args = parser.parse_args()

    if not os.path.exists(args.target_file):
        print(f"Error: Cannot find target file: {args.target_file}")
        sys.exit(1)

    if not os.path.exists(args.arc_file):
        print(f"Error: Cannot find ARC file: {args.arc_file}")
        sys.exit(1)

    try:
        # ========== Step 1: Read and clean target file ==========
        print(f"Reading target file: {args.target_file}")
        with open(args.target_file, "r", encoding="utf-8") as f:
            content = f.read()

        print("Cleaning existing (wires) and (vias) blocks...")
        content = clean_wires_vias_blocks(content)

        # ========== Step 2: Get Layer Stackup ==========
        print(f"Parsing layer stackup from: {args.target_file}")
        layer_list = get_conductor_layers(content)
        if not layer_list:
            print("Warning: No conductor layers found. Via connections might be empty.")
        else:
            print(f"Found {len(layer_list)} conductor layers: {layer_list}")

        # ========== Step 3: Parse ARC output ==========
        print(f"Parsing ARC file: {args.arc_file}")
        wires_list, vias_list, via_count = parse_arc_output(args.arc_file)

        # ========== Step 4: Build blocks ==========
        wire_blocks_text = ""
        wire_count = 0
        if wires_list:
            print("Building wire blocks (each segment independent)...")
            wire_blocks_text = build_wire_blocks(wires_list)
            wire_count = len(wires_list)

        via_blocks_text = ""
        if vias_list:
            print(f"Building via blocks (using padstack: VIA16D8)...")
            via_blocks_text = build_via_blocks(vias_list, layer_list, "VIA16D8")

        # ========== Step 5: Insert blocks ==========
        wires_start = content.find("(wires")
        vias_start = content.find("(vias")

        wires_end = -1
        vias_end = -1

        if wires_start != -1:
            wires_end = find_matching_paren(content, wires_start)
        if vias_start != -1:
            vias_end = find_matching_paren(content, vias_start)

        # Insert from bottom to top to avoid index shifting
        if wires_end > vias_end and wires_end != -1:
            if wire_blocks_text:
                print("Inserting wire blocks...")
                content = insert_into_group(content, "wires", wire_blocks_text)
            if via_blocks_text:
                print("Inserting via blocks...")
                content = insert_into_group(content, "vias", via_blocks_text)
        else:
            if via_blocks_text:
                print("Inserting via blocks...")
                content = insert_into_group(content, "vias", via_blocks_text)
            if wire_blocks_text:
                print("Inserting wire blocks...")
                content = insert_into_group(content, "wires", wire_blocks_text)

        # ========== Step 6: Write output ==========
        with open(args.output_file, "w", encoding="utf-8") as f:
            f.write(content)

        print("\n" + "=" * 50)
        print("Conversion complete!")
        print("=" * 50)
        print(f"  Wire segments processed: {wire_count}")
        print(f"  Via count: {via_count}")
        print("=" * 50)
        print(f"Original file kept unchanged: {args.target_file}")
        print(f"New file created: {args.output_file}")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()