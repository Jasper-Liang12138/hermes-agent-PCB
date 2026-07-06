#!/usr/bin/env python3

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import rl_135_core as core


OP_LABELS = {
    "op0": "换层",
    "op1": "顺序提前",
    "op2": "顺序后移",
    "op3": "还原到初始位置",
    "flip": "换层",
    "earlier": "顺序提前",
    "later": "顺序后移",
    "restore": "还原到初始位置",
}

def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _delta_int(best: int, initial: int) -> str:
    delta = best - initial
    return f"{delta:+d}"


def _delta_float(best: float, initial: float) -> str:
    delta = best - initial
    return f"{delta:+.2f}"


def _layer_counts(entries: list[core.OrderEntry]) -> Counter[str]:
    return Counter(entry.layer for entry in entries)


def _layer_names(initial_entries: list[core.OrderEntry], best_entries: list[core.OrderEntry]) -> list[str]:
    names: list[str] = []
    for entry in [*initial_entries, *best_entries]:
        if entry.layer not in names:
            names.append(entry.layer)
    return names


def _transition_text(transitions: Counter[tuple[str, str]]) -> str:
    items = sorted(transitions.items(), key=lambda item: (-item[1], item[0]))
    return "，".join(f"{src}->{dst} {count} 条" for (src, dst), count in items[:6])


def _entry_changes(
    initial_entries: list[core.OrderEntry],
    best_entries: list[core.OrderEntry],
) -> dict[str, Any]:
    initial_by_net = {entry.net: entry for entry in initial_entries}
    best_by_net = {entry.net: entry for entry in best_entries}
    changed = []
    layer_changed = 0
    order_changed = 0
    top_to_bottom = 0
    bottom_to_top = 0
    layer_transitions: Counter[tuple[str, str]] = Counter()
    order_deltas: list[int] = []

    for net, initial in initial_by_net.items():
        best = best_by_net.get(net)
        if best is None:
            continue
        layer_delta = best.layer != initial.layer
        order_delta = best.order - initial.order
        if layer_delta:
            layer_changed += 1
            layer_transitions[(initial.layer, best.layer)] += 1
            if initial.layer == "TOP" and best.layer == "BOTTOM":
                top_to_bottom += 1
            elif initial.layer == "BOTTOM" and best.layer == "TOP":
                bottom_to_top += 1
        if order_delta:
            order_changed += 1
            order_deltas.append(abs(order_delta))
        if layer_delta or order_delta:
            changed.append((net, initial.layer, initial.order, best.layer, best.order, abs(order_delta)))

    changed.sort(key=lambda item: (item[5], item[0]), reverse=True)
    avg_abs_order_delta = sum(order_deltas) / len(order_deltas) if order_deltas else 0.0
    return {
        "changed": changed,
        "layer_changed": layer_changed,
        "order_changed": order_changed,
        "top_to_bottom": top_to_bottom,
        "bottom_to_top": bottom_to_top,
        "layer_transitions": layer_transitions,
        "avg_abs_order_delta": avg_abs_order_delta,
    }


def _source_explanation(source: str | None) -> str:
    if not source:
        return "最佳候选没有记录来源动作。"
    parts = source.split(":")
    if source.startswith("deterministic_probe:") and len(parts) >= 3:
        return f"最佳候选来自确定性探测，对网络 {parts[1]} 执行“{OP_LABELS.get(parts[2], parts[2])}”。"
    if source.startswith("ga_crossover:"):
        return "最佳候选来自 GA 交叉，说明多个较好候选中的层/顺序片段组合后表现更好。"
    if source.startswith("ga_diversity_restart:"):
        return "最佳候选来自 GA 多样性重启，说明跳出原有局部搜索区域后找到了更好的层/顺序组合。"
    if "mut" in source or source.startswith("ga_initial_mutation:"):
        return "最佳候选来自 GA 变异，说明少量随机层/顺序扰动改善了路由评价。"
    if source == "baseline":
        return "最佳候选仍是初始方案，搜索没有找到指标更好的替代方案。"
    return f"最佳候选来源为 `{source}`。"


def _metric_reason_lines(initial: Any, best: Any, changes: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    routed_delta = best.stats.routed_nets - initial.stats.routed_nets
    missing_initial = initial.stats.total_nets - initial.stats.routed_nets
    missing_best = best.stats.total_nets - best.stats.routed_nets
    wire_delta = best.stats.total_wire_length - initial.stats.total_wire_length
    via_delta = best.stats.vias - initial.stats.vias

    if routed_delta > 0:
        lines.append(f"核心改善是布通数量增加 {routed_delta} 条，未布通网络从 {missing_initial} 条降到 {missing_best} 条。")
    elif routed_delta == 0:
        lines.append("布通数量与初始方案相同，因此优劣主要由线长和过孔数决定。")
    else:
        lines.append(f"最佳候选的布通数量少了 {-routed_delta} 条；它仅在当前保存规则下作为 primary best 使用，需要重点复核。")

    if wire_delta < -1e-9:
        lines.append(f"总线长减少 {-wire_delta:.2f}，说明新的顺序减少了绕行或重复占道。")
    elif wire_delta > 1e-9 and routed_delta > 0:
        lines.append(f"总线长增加 {wire_delta:.2f}，但这是为换取更多网络成功布通的结果。")
    elif wire_delta > 1e-9:
        lines.append(f"总线长增加 {wire_delta:.2f}，如果布通数没有提升，应优先检查是否还有更短的 full/wire 候选。")

    if via_delta < 0:
        lines.append(f"过孔数减少 {-via_delta} 个，层切换成本更低。")
    elif via_delta > 0 and routed_delta > 0:
        lines.append(f"过孔数增加 {via_delta} 个，但换来了更高布通率。")

    if changes["layer_changed"] > 0:
        transition_text = _transition_text(changes["layer_transitions"])
        transition_part = f"（{transition_text}）" if transition_text else ""
        lines.append(f"{changes['layer_changed']} 条网络的分配层发生变化{transition_part}；这表示搜索改变了这些网络与其它网络的通道竞争关系。")
    if changes["order_changed"] > 0:
        lines.append(
            f"{changes['order_changed']} 条网络调整了同层顺序，平均绝对位移 {changes['avg_abs_order_delta']:.1f}；"
            "顺序变化会改变先占通道的网络，从而影响后续网络是否被阻挡。"
        )
    if not lines:
        lines.append("最佳方案与初始方案的可观测指标差异很小，当前报告无法给出更强结论。")
    return lines


def generate_explanation(
    run_dir: Path,
    summary: Any,
    initial_candidate: Any,
    best_candidate: Any,
) -> Path:
    run_dir = Path(run_dir)
    report_path = run_dir / "explanation.md"

    initial_entries = list(initial_candidate.entries)
    best_entries = list(best_candidate.entries)
    changes = _entry_changes(initial_entries, best_entries)
    initial_counts = _layer_counts(initial_entries)
    best_counts = _layer_counts(best_entries)
    layer_names = _layer_names(initial_entries, best_entries)
    recovered = sorted(set(initial_candidate.missing_nets) - set(best_candidate.missing_nets))
    new_missing = sorted(set(best_candidate.missing_nets) - set(initial_candidate.missing_nets))
    source = getattr(best_candidate, "source", None)
    layer_count_text = "，".join(
        f"{layer} {initial_counts.get(layer, 0)} -> {best_counts.get(layer, 0)}"
        for layer in layer_names
    )

    lines = [
        "# 135 走线优化解释",
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
            f"- 变化规模：分配层变化 {changes['layer_changed']} 条，顺序变化 {changes['order_changed']} 条，"
            f"总变化 {len(changes['changed'])} 条。"
        ),
    ]

    if changes["changed"]:
        examples = []
        for net, old_layer, old_order, new_layer, new_order, _delta in changes["changed"][:8]:
            examples.append(f"{net}: {old_layer}#{old_order} -> {new_layer}#{new_order}")
        lines.append(f"- 代表性变化：{'; '.join(examples)}。")
    else:
        lines.append("- 代表性变化：层和顺序与初始方案一致。")

    lines.extend(["", "## 为什么这个结果更好"])
    lines.append(f"- {_source_explanation(source)}")
    lines.extend(f"- {item}" for item in _metric_reason_lines(initial_candidate, best_candidate, changes))

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
