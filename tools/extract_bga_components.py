import argparse
import json
import re
from pathlib import Path

BGA_PIN_THRESHOLD = 200

COMPONENT_RE = re.compile(r'^\s*\(component\s+"([^"]*)"')
PART_RE = re.compile(r'^\s*\(part\s+"([^"]*)"')
FOOTPRINT_RE = re.compile(r'^\s*\(footprint\s+"([^"]*)"')
CLASS_RE = re.compile(r'^\s*\(class\s+"([^"]*)"')
PINCOUNT_RE = re.compile(r'^\s*\(pincount\s+(\d+)')
BGA_CLASS_RE = re.compile(
    r'DFA_DEV_CLASS\s*(?::|=)\s*BGA|propname\s+"DFA_DEV_CLASS".*propvalue\s+"BGA"',
    re.IGNORECASE,
)


def paren_delta(line: str) -> int:
    return line.count("(") - line.count(")")


def iter_components(path: Path):
    current = None
    depth = 0

    with path.open("r", encoding="utf-8-sig", errors="ignore") as source:
        for line_number, line in enumerate(source, start=1):
            if current is None:
                component_match = COMPONENT_RE.match(line)
                if not component_match:
                    continue

                current = {
                    "refdes": component_match.group(1),
                    "part": "",
                    "footprint": "",
                    "class": "",
                    "pincount": None,
                    "line": line_number,
                    "match_reasons": [],
                }
                depth = paren_delta(line)

            part_match = PART_RE.match(line)
            if part_match and not current["part"]:
                current["part"] = part_match.group(1)

            footprint_match = FOOTPRINT_RE.match(line)
            if footprint_match and not current["footprint"]:
                current["footprint"] = footprint_match.group(1)

            if BGA_CLASS_RE.search(line) and "DFA_DEV_CLASS=BGA" not in current["match_reasons"]:
                current["match_reasons"].append("DFA_DEV_CLASS=BGA")

            if line_number != current["line"]:
                depth += paren_delta(line)

            if depth <= 0:
                yield current
                current = None
                depth = 0


def parse_part_defs(path: Path):
    part_defs = {}
    current = None
    depth = 0

    with path.open("r", encoding="utf-8-sig", errors="ignore") as source:
        for line_number, line in enumerate(source, start=1):
            if current is None:
                part_match = PART_RE.match(line)
                if not part_match:
                    continue

                current = {
                    "part": part_match.group(1),
                    "footprint": "",
                    "class": "",
                    "pincount": None,
                    "line": line_number,
                }
                depth = paren_delta(line)
            else:
                footprint_match = FOOTPRINT_RE.match(line)
                if footprint_match and not current["footprint"]:
                    current["footprint"] = footprint_match.group(1)

                class_match = CLASS_RE.match(line)
                if class_match and not current["class"]:
                    current["class"] = class_match.group(1)

                pincount_match = PINCOUNT_RE.match(line)
                if pincount_match and current["pincount"] is None:
                    current["pincount"] = int(pincount_match.group(1))

                depth += paren_delta(line)

            if current is not None and depth <= 0:
                existing = part_defs.get(current["part"])
                if existing is None or current["pincount"] is not None:
                    part_defs[current["part"]] = current
                current = None
                depth = 0

    return part_defs


def enrich_components(components, part_defs):
    for component in components:
        part_def = part_defs.get(component["part"]) or part_defs.get(component["footprint"])
        if not part_def:
            continue

        if not component["footprint"]:
            component["footprint"] = part_def["footprint"]
        if not component["class"]:
            component["class"] = part_def["class"]
        if component["pincount"] is None:
            component["pincount"] = part_def["pincount"]


def extract_bga_components(path: Path):
    components = list(iter_components(path))
    part_defs = parse_part_defs(path)
    enrich_components(components, part_defs)

    bga_components = []
    seen_refdes = set()
    for component in components:
        if "DFA_DEV_CLASS=BGA" in component["match_reasons"]:
            bga_components.append(component)
            seen_refdes.add(component["refdes"])

    for component in components:
        if (
            component["refdes"].upper().startswith("U")
            and (component["pincount"] or 0) > BGA_PIN_THRESHOLD
        ):
            reason = f"U component over {BGA_PIN_THRESHOLD} pins"
            if reason not in component["match_reasons"]:
                component["match_reasons"].append(reason)
            if component["refdes"] not in seen_refdes:
                bga_components.append(component)
                seen_refdes.add(component["refdes"])

    return bga_components


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Extract BGA components by DFA_DEV_CLASS or high pin-count rules."
        )
    )
    parser.add_argument("input_file", help="Path to the source text file")
    parser.add_argument(
        "-o",
        "--output",
        help="Output JSON file. If omitted, JSON is printed to the console.",
    )
    args = parser.parse_args()

    input_path = Path(args.input_file)
    bga_components = extract_bga_components(input_path)
    result = {
        "source": str(input_path),
        "match_rule": (
            "component has DFA_DEV_CLASS=BGA, and all U components over "
            f"{BGA_PIN_THRESHOLD} pins are included"
        ),
        "count": len(bga_components),
        "components": bga_components,
    }

    json_text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(json_text + "\n", encoding="utf-8")
    else:
        print(json_text)


if __name__ == "__main__":
    main()
