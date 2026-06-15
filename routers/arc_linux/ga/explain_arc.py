#!/usr/bin/env python3

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import rl_arc_core as core


BASE_OP_LABELS = {
    "op0": "顺序提前",
    "op1": "顺序后移",
    "op2": "还原到初始位置",
}

def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _delta_int(best: int, initial: int) -> str:
    delta = best - initial
    return f"{delta:+d}"


def _delta_float(best: float, initial: float) -> str:
    delta = best - initial
    return f"{delta:+.2f}"


def _layer_names(initial_pairs: list[core.PairEntry], best_pairs: list[core.PairEntry]) -> list[str]:
    names: list[str] = []
    for pair in [*initial_pairs, *best_pairs]:
        if pair.layer not in names:
            names.append(pair.layer)
    return names


def _layer_counts(pairs: list[core.PairEntry]) -> Counter[str]:
    return Counter(pair.layer for pair in pairs)


def _pair_changes(
    initial_pairs: list[core.PairEntry],
    best_pairs: list[core.PairEntry],
) -> dict[str, Any]:
    initial_by_key = {pair.key: pair for pair in initial_pairs}
    best_by_key = {pair.key: pair for pair in best_pairs}
    changed = []
    layer_changed = 0
    order_changed = 0
    order_deltas: list[int] = []

    for key, initial in initial_by_key.items():
        best = best_by_key.get(key)
        if best is None:
            continue
        layer_delta = best.layer != initial.layer
        order_delta = best.order - initial.order
        if layer_delta:
            layer_changed += 1
        if order_delta:
            order_changed += 1
            order_deltas.append(abs(order_delta))
        if layer_delta or order_delta:
            changed.append((key, initial.layer, initial.order, best.layer, best.order, abs(order_delta)))

    changed.sort(key=lambda item: (item[5], item[0]), reverse=True)
    avg_abs_order_delta = sum(order_deltas) / len(order_deltas) if order_deltas else 0.0
    return {
        "changed": changed,
        "layer_changed": layer_changed,
        "order_changed": order_changed,
        "avg_abs_order_delta": avg_abs_order_delta,
    }


def _op_label(raw_op: str, layer_names: list[str]) -> str:
    if raw_op in BASE_OP_LABELS:
        return BASE_OP_LABELS[raw_op]
    if raw_op.startswith("op"):
        try:
            op = int(raw_op[2:])
        except ValueError:
            return raw_op
        layer_index = op - 3
        if 0 <= layer_index < len(layer_names):
            return f"换到层 {layer_names[layer_index]}"
    return raw_op


def _source_explanation(source: str | None, layer_names: list[str]) -> str:
    if not source:
        return "最佳候选没有记录来源动作。"
    parts = source.split(":")
    if source.startswith("seed_swap:") and len(parts) >= 3:
        return f"最佳候选来自种子同槽换层探测，交换 {parts[1]} 的第 {parts[2].replace('slot', '')} 个 pair 槽位。"
    if source.startswith("deterministic_probe:") and len(parts) >= 3:
        return f"最佳候选来自确定性探测，对差分对 {parts[1]} 执行“{_op_label(parts[2], layer_names)}”。"
    if source.startswith("ga_crossover:"):
        return "最佳候选来自 GA 交叉，说明多个较好候选中的差分对层/顺序片段组合后表现更好。"
    if source.startswith("ga_diversity_restart:"):
        return "最佳候选来自 GA 多样性重启，说明跳出原有局部搜索区域后找到了更好的 pair 层/顺序组合。"
    if "mut" in source or source.startswith("ga_initial_mutation:"):
        return "最佳候选来自 GA 变异，说明少量随机 pair 层/顺序扰动改善了路由评价。"
    if source == "baseline":
        return "最佳候选仍是初始方案，搜索没有找到指标更好的替代方案。"
    return f"最佳候选来源为 `{source}`。"


def _reason_lines(initial: Any, best: Any, changes: dict[str, Any], layer_counts_same: bool) -> list[str]:
    lines: list[str] = []
    routed_delta = best.stats.routed_nets - initial.stats.routed_nets
    missing_initial = initial.stats.total_nets - initial.stats.routed_nets
    missing_best = best.stats.total_nets - best.stats.routed_nets
    wire_delta = best.stats.total_wire_length - initial.stats.total_wire_length

    if routed_delta > 0:
        lines.append(f"核心改善是布通数量增加 {routed_delta} 条，未布通网络从 {missing_initial} 条降到 {missing_best} 条。")
    elif routed_delta == 0:
        lines.append("布通数量与初始方案相同，因此优劣主要由总线长决定。")
    else:
        lines.append(f"最佳候选的布通数量少了 {-routed_delta} 条；它仅在当前保存规则下作为 primary best 使用，需要重点复核。")

    if wire_delta < -1e-9:
        lines.append(f"总线长减少 {-wire_delta:.2f}，说明新的 pair 顺序减少了绕行或弧线补偿。")
    elif wire_delta > 1e-9 and routed_delta > 0:
        lines.append(f"总线长增加 {wire_delta:.2f}，但这是为换取更多网络成功布通的结果。")
    elif wire_delta > 1e-9:
        lines.append(f"总线长增加 {wire_delta:.2f}，如果布通数没有提升，应优先检查 `best_wire_full`。")

    if changes["layer_changed"] > 0:
        if layer_counts_same:
            lines.append(
                f"{changes['layer_changed']} 个差分对的分配层发生变化，同时每层 pair 数保持不变；"
                "这符合弧形流程的安全动作单位，可以改变冲突关系但不破坏层负载约束。"
            )
        else:
            lines.append(f"{changes['layer_changed']} 个差分对的分配层发生变化，改变了层负载分布。")
    if changes["order_changed"] > 0:
        lines.append(
            f"{changes['order_changed']} 个差分对调整了同层顺序，平均绝对位移 {changes['avg_abs_order_delta']:.1f}；"
            "顺序变化会改变同层 pair 的通道占用先后，从而影响后续弧线是否被阻挡。"
        )
    lines.append("弧形流程的主排序是布通网络数优先、总线长其次；过孔数会记录，但不是该流程的主要优化目标。")
    return lines


def generate_explanation(
    run_dir: Path,
    summary: Any,
    initial_candidate: Any,
    best_candidate: Any,
) -> Path:
    run_dir = Path(run_dir)
    report_path = run_dir / "explanation.md"

    initial_pairs = list(initial_candidate.pairs)
    best_pairs = list(best_candidate.pairs)
    layer_names = _layer_names(initial_pairs, best_pairs)
    changes = _pair_changes(initial_pairs, best_pairs)
    initial_counts = _layer_counts(initial_pairs)
    best_counts = _layer_counts(best_pairs)
    layer_counts_same = all(initial_counts.get(layer, 0) == best_counts.get(layer, 0) for layer in layer_names)
    recovered = sorted(set(initial_candidate.missing_nets) - set(best_candidate.missing_nets))
    new_missing = sorted(set(best_candidate.missing_nets) - set(initial_candidate.missing_nets))
    source = getattr(best_candidate, "source", None)

    layer_count_text = "，".join(
        f"{layer} {initial_counts.get(layer, 0)} -> {best_counts.get(layer, 0)}"
        for layer in layer_names
    )

    lines = [
        "# 弧形走线优化解释",
        "",
        "## 指标对比",
        "| 指标 | 初始方案 | 最佳方案 | 变化 |",
        "| --- | ---: | ---: | ---: |",
        (
            f"| 布通网络 | {initial_candidate.stats.routed_nets}/{initial_candidate.stats.total_nets} | "
            f"{best_candidate.stats.routed_nets}/{best_candidate.stats.total_nets} | "
            f"{_delta_int(best_candidate.stats.routed_nets, initial_candidate.stats.routed_nets)} |"
        ),
        (
            f"| 完成率 | {_pct(initial_candidate.stats.completion_rate)} | "
            f"{_pct(best_candidate.stats.completion_rate)} | "
            f"{(best_candidate.stats.completion_rate - initial_candidate.stats.completion_rate) * 100:+.1f} pp |"
        ),
        (
            f"| 总线长 | {initial_candidate.stats.total_wire_length:.2f} | "
            f"{best_candidate.stats.total_wire_length:.2f} | "
            f"{_delta_float(best_candidate.stats.total_wire_length, initial_candidate.stats.total_wire_length)} |"
        ),
        f"| 过孔数 | {initial_candidate.stats.vias} | {best_candidate.stats.vias} | {_delta_int(best_candidate.stats.vias, initial_candidate.stats.vias)} |",
        "",
        "## 层和顺序变化",
        f"- 层负载：{layer_count_text}。",
        (
            f"- 变化规模：分配层变化 {changes['layer_changed']} 个差分对，顺序变化 {changes['order_changed']} 个差分对，"
            f"总变化 {len(changes['changed'])} 个差分对。"
        ),
    ]

    if changes["changed"]:
        examples = []
        for key, old_layer, old_order, new_layer, new_order, _delta in changes["changed"][:8]:
            examples.append(f"{key}: {old_layer}#{old_order} -> {new_layer}#{new_order}")
        lines.append(f"- 代表性变化：{'; '.join(examples)}。")
    else:
        lines.append("- 代表性变化：层和顺序与初始方案一致。")

    lines.extend(["", "## 为什么这个结果更好"])
    lines.append(f"- {_source_explanation(source, layer_names)}")
    lines.extend(f"- {item}" for item in _reason_lines(initial_candidate, best_candidate, changes, layer_counts_same))

    lines.extend(["", "## 未布通网络"])
    if best_candidate.missing_nets:
        lines.append(f"- 最佳方案仍有 {len(best_candidate.missing_nets)} 条未布通：{', '.join(best_candidate.missing_nets[:12])}")
    else:
        lines.append("- 最佳方案已全布通。")
    if recovered:
        lines.append(f"- 相比初始方案新增布通：{', '.join(recovered[:12])}")
    if new_missing:
        lines.append(f"- 相比初始方案新增未布通：{', '.join(new_missing[:12])}")

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path
