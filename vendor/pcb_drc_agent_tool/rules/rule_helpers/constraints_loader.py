import json
from pathlib import Path


def _load_json(path: str) -> dict:
    if not path:
        return {}

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Constraint file not found: {path}")

    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_external_constraints(
    diff_pairs_path: str = "",
    routing_rules_path: str = "",
    diff_groups_path: str = "",
) -> dict:
    diff_pairs_data = _load_json(diff_pairs_path)
    routing_rules_data = _load_json(routing_rules_path)
    diff_groups_data = _load_json(diff_groups_path)

    return {
        "diff_pairs": diff_pairs_data.get("diff_pairs", []),
        "single_rules": routing_rules_data.get("single_rules", []),
        "diff_rules": routing_rules_data.get("diff_rules", []),
        "diff_groups": diff_groups_data.get("diff_groups", []),
    }


def build_constraint_indexes(constraints: dict) -> dict:
    diff_pairs = constraints.get("diff_pairs", [])
    single_rules = constraints.get("single_rules", [])
    diff_rules = constraints.get("diff_rules", [])
    diff_groups = constraints.get("diff_groups", [])

    pair_by_name = {}
    net_to_pair = {}
    single_rule_by_net = {}
    diff_rule_by_pair = {}
    group_by_pair = {}

    for pair in diff_pairs:
        name = pair.get("name", "")
        p_net = pair.get("p_net", "")
        n_net = pair.get("n_net", "")

        if not name:
            continue

        pair_by_name[name] = pair

        if p_net:
            net_to_pair[p_net] = name
        if n_net:
            net_to_pair[n_net] = name

    for rule in single_rules:
        rule_name = rule.get("name", "")
        for net in rule.get("nets", []):
            single_rule_by_net[net] = rule_name

    for rule in diff_rules:
        rule_name = rule.get("name", "")
        for pair_name in rule.get("pairs", []):
            diff_rule_by_pair[pair_name] = rule_name

    for group in diff_groups:
        group_name = group.get("name", "")
        for pair_name in group.get("pairs", []):
            group_by_pair[pair_name] = group_name

    return {
        "pair_by_name": pair_by_name,
        "net_to_pair": net_to_pair,
        "single_rule_by_net": single_rule_by_net,
        "diff_rule_by_pair": diff_rule_by_pair,
        "group_by_pair": group_by_pair,
    }

def validate_constraints_against_board(board) -> list:
    issues = []

    board_net_names = {n.name for n in board.nets}
    pair_by_name = board.constraint_indexes.get("pair_by_name", {})

    for pair in board.diff_pairs:
        pair_name = pair.get("name", "")
        p_net = pair.get("p_net", "")
        n_net = pair.get("n_net", "")

        if p_net and p_net not in board_net_names:
            issues.append(f"Diff pair {pair_name}: p_net '{p_net}' not found in board.")
        if n_net and n_net not in board_net_names:
            issues.append(f"Diff pair {pair_name}: n_net '{n_net}' not found in board.")

    for rule in board.single_rules:
        rule_name = rule.get("name", "")
        for net in rule.get("nets", []):
            if net not in board_net_names:
                issues.append(f"Single rule {rule_name}: net '{net}' not found in board.")

    for rule in board.diff_rules:
        rule_name = rule.get("name", "")
        for pair_name in rule.get("pairs", []):
            if pair_name not in pair_by_name:
                issues.append(f"Diff rule {rule_name}: pair '{pair_name}' not found in diff_pairs.json.")

    for group in board.diff_groups:
        group_name = group.get("name", "")
        for pair_name in group.get("pairs", []):
            if pair_name not in pair_by_name:
                issues.append(f"Diff group {group_name}: pair '{pair_name}' not found in diff_pairs.json.")

    return issues

def attach_constraints_to_board(board, constraints: dict):
    constraints = constraints or {}

    board.constraints = constraints
    board.diff_pairs = constraints.get("diff_pairs", [])
    board.single_rules = constraints.get("single_rules", [])
    board.diff_rules = constraints.get("diff_rules", [])
    board.diff_groups = constraints.get("diff_groups", [])
    board.constraint_indexes = build_constraint_indexes(constraints)

    return board