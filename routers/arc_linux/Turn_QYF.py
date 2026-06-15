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


def parse_arc_output(file_path):
    """
    Parse ARC_output.txt

    LINE:
        layer!LINE!id!net!x1!y1!x2!y2!width

    ARC:
        layer!ARC!id!net!x1!y1!x2!y2!cx!cy!radius!width!direction
    """
    grouped = OrderedDict()

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

            else:
                print(f"[Skip] Line {line_no}: unknown type -> {elem_type}")

    return grouped


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


def insert_into_wires_group(source_file, output_file, wire_blocks_text):
    """
    Find existing (wires ... ) group and insert wire blocks before its closing ')'.
    Write to a new file, keep original file unchanged.
    """
    with open(source_file, "r", encoding="utf-8") as f:
        content = f.read()

    wires_start = content.find("(wires")
    if wires_start == -1:
        raise ValueError("Cannot find '(wires' group in target file.")

    wires_end = find_matching_paren(content, wires_start)
    if wires_end == -1:
        raise ValueError("Cannot find matching closing ')' for '(wires' group.")

    # Insert before the closing ')' of the wires group
    prefix = content[:wires_end].rstrip()
    suffix = content[wires_end:]

    new_content = prefix + "\n" + wire_blocks_text + suffix

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(new_content)


def main():
    parser = argparse.ArgumentParser(
        description='Parse ARC output and insert wire blocks into target file.',
        epilog='Example: python script.py 1234_1.txt arc_output.txt output.txt'
    )
    parser.add_argument(
        'target_file',
        help='Path to target file containing (wires ...) group (e.g., 1234_1.txt)'
    )
    parser.add_argument(
        'arc_file',
        help='Path to ARC output file containing LINE and ARC definitions (e.g., arc_output.txt)'
    )
    parser.add_argument(
        'output_file',
        help='Path to output file (e.g., output.txt)'
    )

    args = parser.parse_args()

    # Validate input files exist
    if not os.path.exists(args.target_file):
        print(f"Error: Cannot find target file: {args.target_file}")
        sys.exit(1)

    if not os.path.exists(args.arc_file):
        print(f"Error: Cannot find ARC file: {args.arc_file}")
        sys.exit(1)

    try:
        # Parse ARC output
        print(f"Parsing ARC file: {args.arc_file}")
        grouped_data = parse_arc_output(args.arc_file)

        # Build wire blocks
        print("Building wire blocks...")
        wire_blocks_text = build_wire_blocks(grouped_data)

        # Insert into target file
        print(f"Inserting into target file: {args.target_file}")
        insert_into_wires_group(args.target_file, args.output_file, wire_blocks_text)

        print("\nConversion complete!")
        print(f"Original file kept unchanged: {args.target_file}")
        print(f"New file created: {args.output_file}")

    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()