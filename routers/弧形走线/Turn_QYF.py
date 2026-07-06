from collections import OrderedDict
from decimal import Decimal, ROUND_HALF_UP
import os
import sys
import argparse


def scale100(value):
    """
    Convert numeric string to integer after multiplying by 100.
    Example:
        4008.9 -> 400890
        3 -> 300
    """
    d = Decimal(str(value)) * Decimal("100")
    return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


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


def resolve_layer(raw_layer, net):
    """
    Route Inhibit shapes into the Inhibit Route layer family.
    Other data keeps the existing Conductor mapping.
    """
    if net.strip().lower() == "inhibit":
        return f"Inhibit Route/{raw_layer.strip().capitalize()}"
    return normalize_layer(raw_layer)


def build_circle_item(cx, cy, radius):
    """
    Build a full circle surface from center and radius.
    Use left/right points on the horizontal axis and two semicircle arc segments.
    """
    center_x = scale100(cx)
    center_y = scale100(cy)
    radius_value = scale100(radius)

    point_a = (center_x - radius_value, center_y)
    point_b = (center_x + radius_value, center_y)

    return {
        "type": "CIRCLE",
        "center": (center_x, center_y),
        "start": point_a,
        "mid": point_b,
    }


def build_rect_item(coords):
    """
    Build a rectangle/polygon surface from four input points.
    """
    points = []
    for i in range(0, len(coords), 2):
        points.append((scale100(coords[i]), scale100(coords[i + 1])))

    return {
        "type": "RECT",
        "points": points,
    }


def parse_arc_output(file_path):
    """
    Parse ARC_output.txt

    LINE:
        layer!LINE!id!net!x1!y1!x2!y2!width

    ARC:
        layer!ARC!id!net!x1!y1!x2!y2!cx!cy!radius!width!direction
    """
    grouped = OrderedDict()
    surfaces = []

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
            elem_type = parts[1].strip().upper()

            if elem_type == "LINE":
                if len(parts) != 9:
                    print(f"[Skip] Line {line_no}: LINE format error -> {line}")
                    continue

                _, _, obj_id, net, x1, y1, x2, y2, width = parts
                layer = resolve_layer(raw_layer, net)

                item = {
                    "type": "LINE",
                    "start": (scale100(x1), scale100(y1)),
                    "end": (scale100(x2), scale100(y2)),
                    "width": scale100(width),
                }

                key = (layer, net)
                grouped.setdefault(key, []).append(item)

            elif elem_type == "ARC":
                if len(parts) != 13:
                    print(f"[Skip] Line {line_no}: ARC format error -> {line}")
                    continue

                _, _, obj_id, net, x1, y1, x2, y2, cx, cy, radius, width, direction = parts
                layer = resolve_layer(raw_layer, net)

                item = {
                    "type": "ARC",
                    "start": (scale100(x1), scale100(y1)),
                    "end": (scale100(x2), scale100(y2)),
                    "center": (scale100(cx), scale100(cy)),
                    "radius": scale100(radius),
                    "width": scale100(width),
                    "rotate": normalize_rotate(direction),
                }

                key = (layer, net)
                grouped.setdefault(key, []).append(item)

            elif elem_type == "CIRCLE":
                if len(parts) < 7:
                    print(f"[Skip] Line {line_no}: CIRCLE format error -> {line}")
                    continue

                _, _, obj_id, net, cx, cy, radius, *rest = parts
                layer = resolve_layer(raw_layer, net)

                surfaces.append({
                    "layer": layer,
                    "net": "none",
                    "shape": build_circle_item(cx, cy, radius),
                })

            elif elem_type == "RECT":
                if len(parts) < 12:
                    print(f"[Skip] Line {line_no}: RECT format error -> {line}")
                    continue

                _, _, obj_id, net, *coords = parts
                coords = [value for value in coords if value != ""]
                if len(coords) != 8:
                    print(f"[Skip] Line {line_no}: RECT point count error -> {line}")
                    continue

                layer = resolve_layer(raw_layer, net)

                surfaces.append({
                    "layer": layer,
                    "net": "none",
                    "shape": build_rect_item(coords),
                })

            else:
                print(f"[Skip] Line {line_no}: unknown type -> {elem_type}")

    return grouped, surfaces


def build_wire_blocks(grouped_data):
    """
    Only build repeated (wire ...) blocks.
    DO NOT wrap with outer (wires), because they will be inserted into existing (wires) group.
    """
    lines = []

    for (layer, net), items in grouped_data.items():
        if not items:
            continue

        lines.append("    (wire")
        lines.append(f'        (net "{net}")')
        lines.append("        (path")
        lines.append('            (issamewidth "true")')

        first_start = items[0]["start"]
        first_width = items[0]["width"]

        # First start point
        lines.append("            (lineseg")
        lines.append(f"                (pt {first_start[0]} {first_start[1]})")
        lines.append(f"                (w {first_width})")
        lines.append("            )")

        for item in items:
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
        lines.append(f'            (layer "{layer}")')
        lines.append("        )")
        lines.append("    )")

    return "\n".join(lines) + "\n"


def build_surface_blocks(surface_items):
    """
    Build repeated (surface ...) blocks to be inserted into an existing (surfaces) group.
    """
    lines = []

    for item in surface_items:
        shape = item["shape"]

        lines.append("    (surface")
        lines.append(f'        (net "{item["net"]}")')
        lines.append("        (boundary")
        lines.append("            (path")

        if shape["type"] == "CIRCLE":
            start_x, start_y = shape["start"]
            mid_x, mid_y = shape["mid"]
            center_x, center_y = shape["center"]

            lines.append("                (lineseg")
            lines.append(f"                    (pt {start_x} {start_y})")
            lines.append("                    (w 0)")
            lines.append("                )")
            lines.append("                (arcseg")
            lines.append(f"                    (pt {mid_x} {mid_y})")
            lines.append("                    (w 0)")
            lines.append(f"                    (xy {center_x} {center_y})")
            lines.append("                    (rotate CW)")
            lines.append("                )")
            lines.append("                (arcseg")
            lines.append(f"                    (pt {start_x} {start_y})")
            lines.append("                    (w 0)")
            lines.append(f"                    (xy {center_x} {center_y})")
            lines.append("                    (rotate CW)")
            lines.append("                )")

        elif shape["type"] == "RECT":
            points = shape["points"]
            closed_points = points + [points[0]]
            for pt_x, pt_y in closed_points:
                lines.append("                (lineseg")
                lines.append(f"                    (pt {pt_x} {pt_y})")
                lines.append("                    (w 0)")
                lines.append("                )")

        lines.append("            )")
        lines.append("        )")
        lines.append("        (voids)")
        lines.append("        (props)")
        lines.append(f'        (layer "{item["layer"]}")')
        lines.append("    )")

    return "\n".join(lines) + ("\n" if lines else "")


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


def insert_into_group(content, group_name, block_text):
    """
    Find existing target group and insert blocks before its closing ')'.
    """
    group_start = content.find(f"({group_name}")
    if group_start == -1:
        raise ValueError(f"Cannot find '({group_name}' group in target file.")

    group_end = find_matching_paren(content, group_start)
    if group_end == -1:
        raise ValueError(f"Cannot find matching closing ')' for '({group_name}' group.")

    prefix = content[:group_end].rstrip()
    suffix = content[group_end:]

    return prefix + "\n" + block_text + suffix


def write_output_with_insertions(source_file, output_file, wire_blocks_text, surface_blocks_text):
    """
    Insert new wire blocks into (wires) and new surface blocks into (surfaces).
    Write to a new file, keep original file unchanged.
    """
    with open(source_file, "r", encoding="utf-8") as f:
        content = f.read()

    if wire_blocks_text.strip():
        content = insert_into_group(content, "wires", wire_blocks_text)

    if surface_blocks_text.strip():
        content = insert_into_group(content, "surfaces", surface_blocks_text)

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(content)


def main():
    parser = argparse.ArgumentParser(
        description='Parse source data and insert wire/surface blocks into target file.',
        epilog='Example: python script.py 1234_1.txt arc_output.txt'
    )
    parser.add_argument(
        'target_file',
        help='Path to target file containing (wires ...) group (e.g., 1234_1.txt)'
    )
    parser.add_argument(
        'arc_file',
        help='Path to source data file containing LINE/ARC/CIRCLE/RECT definitions (e.g., arc_output.txt)'
    )
    parser.add_argument(
        'output_file',
        nargs='?',
        help='Optional output file path. If omitted, overwrite target file directly.'
    )

    args = parser.parse_args()

    # Validate input files exist
    if not os.path.exists(args.target_file):
        print(f"Error: Cannot find target file: {args.target_file}")
        sys.exit(1)

    if not os.path.exists(args.arc_file):
        print(f"Error: Cannot find ARC file: {args.arc_file}")
        sys.exit(1)

    output_file = args.output_file or args.target_file

    try:
        # Parse source data
        print(f"Parsing ARC file: {args.arc_file}")
        grouped_data, surface_items = parse_arc_output(args.arc_file)

        # Build output blocks
        print("Building wire blocks...")
        wire_blocks_text = build_wire_blocks(grouped_data)
        print("Building surface blocks...")
        surface_blocks_text = build_surface_blocks(surface_items)

        # Insert into target file
        print(f"Inserting into target file: {args.target_file}")
        write_output_with_insertions(
            args.target_file,
            output_file,
            wire_blocks_text,
            surface_blocks_text,
        )

        print("\nConversion complete!")
        if output_file == args.target_file:
            print(f"Target file overwritten: {args.target_file}")
        else:
            print(f"Original file kept unchanged: {args.target_file}")
            print(f"New file created: {output_file}")

    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
