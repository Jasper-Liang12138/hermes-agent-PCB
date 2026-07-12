from rules.rule_helpers.board_filters import _get_valid_bga_pads


ESCAPE_FAILURE_RULES = {
    "HR_CONNECT_PAD_NOT_ESCAPED",
    "HR_TOPO_MULTIPLE_ESCAPE",
    "HR_CONNECT_BRANCH_INCOMPLETE",
}


def compute_escape_completion_rate(board, hard_issues):
    signal_pads = _get_valid_bga_pads(board)

    total_signal_pads = len(signal_pads)

    failed_pad_ids = set()

    for issue in hard_issues:
        if issue.rule not in ESCAPE_FAILURE_RULES:
            continue

        pad_id = issue.extra.get("pad_id") if issue.extra else None

        if not pad_id:
            pad_id = issue.obj1

        if pad_id:
            failed_pad_ids.add(pad_id)

    failed_escape_pad_count = len(failed_pad_ids)

    valid_escape_pad_count = max(
        0,
        total_signal_pads - failed_escape_pad_count
    )

    escape_completion_rate = (
        valid_escape_pad_count / total_signal_pads * 100
        if total_signal_pads
        else 0.0
    )

    return {
        "signal_pad_count": total_signal_pads,
        "failed_escape_pad_count": failed_escape_pad_count,
        "valid_escape_pad_count": valid_escape_pad_count,
        "escape_completion_rate": round(
            escape_completion_rate,
            2
        ),
    }
