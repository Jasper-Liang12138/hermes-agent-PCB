import argparse
import json
import os
import sys

from main import (
    evaluate_board,
    build_board_result,
    build_issue_report,
    build_ui_payload,
)
from report_builder import build_prompt_text
from agent_payload_builder import build_agent_payload
from zh_report_builder import build_agent_ready_payload, build_chinese_payload

def parse_args():
    parser = argparse.ArgumentParser(
        description="Production CLI entry for BGA DRC evaluation"
    )
    parser.add_argument("pcb", help="Path to input .kicad_pcb file")
    parser.add_argument(
        "--check-mode",
        type=str,
        default="hard",
        help="Rule groups to run: hard / opt / diff / all / hard,opt ... Default is hard.",
    )
    parser.add_argument(
        "--target-bga",
        type=str,
        default="",
        help="Optional target BGA component, e.g. U67",
    )
    parser.add_argument(
        "--json-out",
        type=str,
        default="",
        #required=True,
        help="Path to output result json",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default="",
        help="Optional log file path",
    )
    parser.add_argument(
        "--debug-log",
        action="store_true",
        help="Enable verbose debug logging",
    )
    parser.add_argument(
        "--prompt-out",
        type=str,
        default="",
        help="Optional path to save prompt text for external LLM/agent"
    )
    parser.add_argument(
        "--agent-json-out",
        type=str,
        default="",
        help="Optional path to save slim json for external agent/LLM",
    )
    parser.add_argument(
        "--zh-json-out",
        type=str,
        default="",
        help="Optional path to save Chinese result json",
    )
    parser.add_argument(
        "--agent-zh-json-out",
        type=str,
        default="",
        help="Optional path to save Chinese agent-ready json",
    )
    return parser.parse_args()

def write_error_json(path: str, err_type: str, message: str):
    if not path:
        return

    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    payload = {
        "status": "failed",
        "error": {
            "type": err_type,
            "message": message,
        },
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def first_output_path(args) -> str:
    return args.agent_zh_json_out or args.zh_json_out or args.agent_json_out or args.json_out

def main():
    args = parse_args()

    if not args.json_out and not args.agent_json_out and not args.zh_json_out and not args.agent_zh_json_out:
        print("[ERROR] At least one output path must be provided.", file=sys.stderr)
        sys.exit(2)

    try:
        if args.log_file:
            os.makedirs(os.path.dirname(args.log_file), exist_ok=True) if os.path.dirname(args.log_file) else None
            open(args.log_file, "w", encoding="utf-8").close()

        out_dir = os.path.dirname(args.json_out)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        result = evaluate_board(
            pcb_path=args.pcb,
            check_mode=args.check_mode,
            log_file=args.log_file,
            debug_log=args.debug_log,
            target_bga=args.target_bga,
        )

        payload = {
            "board_result": build_board_result(result),
            "issue_report": build_issue_report(result),
            "ui_payload": build_ui_payload(result),
        }
        if args.prompt_out:
            prompt_dir = os.path.dirname(args.prompt_out)
            if prompt_dir:
                os.makedirs(prompt_dir, exist_ok=True)

            prompt_text = build_prompt_text(result)
            with open(args.prompt_out, "w", encoding="utf-8") as f:
                f.write(prompt_text)

            print(f"[OK] prompt written to: {args.prompt_out}")
        
        if args.agent_json_out:
            agent_dir = os.path.dirname(args.agent_json_out)
            if agent_dir:
                os.makedirs(agent_dir, exist_ok=True)

            agent_payload = build_agent_payload(result)
            with open(args.agent_json_out, "w", encoding="utf-8") as f:
                json.dump(agent_payload, f, indent=2, ensure_ascii=False)

            print(f"[OK] agent json written to: {args.agent_json_out}")

        if args.zh_json_out:
            zh_dir = os.path.dirname(args.zh_json_out)
            if zh_dir:
                os.makedirs(zh_dir, exist_ok=True)

            with open(args.zh_json_out, "w", encoding="utf-8") as f:
                json.dump(build_chinese_payload(result), f, indent=2, ensure_ascii=False)

            print(f"[OK] chinese json written to: {args.zh_json_out}")

        if args.agent_zh_json_out:
            agent_zh_dir = os.path.dirname(args.agent_zh_json_out)
            if agent_zh_dir:
                os.makedirs(agent_zh_dir, exist_ok=True)

            with open(args.agent_zh_json_out, "w", encoding="utf-8") as f:
                json.dump(build_agent_ready_payload(result), f, indent=2, ensure_ascii=False)

            print(f"[OK] agent chinese json written to: {args.agent_zh_json_out}")

        if args.json_out:
            debug_dir = os.path.dirname(args.json_out)
            if debug_dir:
                os.makedirs(debug_dir, exist_ok=True)

            with open(args.json_out, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)

            print(f"[OK] result written to: {args.json_out}")

        sys.exit(0)


    except FileNotFoundError as e:
        msg = f"File not found: {e}"
        print(f"[ERROR] {msg}", file=sys.stderr)
        write_error_json(first_output_path(args), "FileNotFoundError", msg)
        sys.exit(1)

    except ValueError as e:
        msg = f"Invalid argument or configuration: {e}"
        print(f"[ERROR] {msg}", file=sys.stderr)
        write_error_json(first_output_path(args), "ValueError", msg)
        sys.exit(2)

    except Exception as e:
        msg = f"Unexpected failure: {type(e).__name__}: {e}"
        print(f"[ERROR] {msg}", file=sys.stderr)
        write_error_json(first_output_path(args), type(e).__name__, msg)
        sys.exit(1)



if __name__ == "__main__":
    main()
