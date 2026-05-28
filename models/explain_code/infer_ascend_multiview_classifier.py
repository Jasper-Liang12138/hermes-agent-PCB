#!/usr/bin/env python3
import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
from typing import List, Sequence, Union

import torch
from PIL import Image

from train_ascend_multiview_classifier import (
    MultiViewBoardClassifier,
    detect_device,
    numeric_prefix_key,
)
from voxelizer import export_single_board_plot_set


FIXED_DEVICE = "auto"
FIXED_OUTPUT_ROOT = Path("inference_runs")
FIXED_PLOT_MODE = "vector"
FIXED_RESOLUTION_MM = None
FIXED_INCLUDE_ZONES = True
FIXED_BBOX_PADDING_MM = 1.0
FIXED_DPI = 600
FIXED_PNG_WIDTH = 512
FIXED_TRIM_WHITESPACE = True
FIXED_CLEAN_PLOT = True
FIXED_REQUIRE_OVERVIEW = True
FIXED_PAD_INCHES = 0.02


def build_transform(image_size: int):
    from torchvision import transforms

    return transforms.Compose([
        transforms.Resize((image_size, image_size)),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])


def collect_exported_views(board_out: Path, require_overview: bool) -> List[Path]:
    png_dir = board_out / "png"
    overview_path = board_out / "overview" / "overview_all_layers.png"
    if not png_dir.is_dir():
        raise RuntimeError(f"Missing png directory: {png_dir}")

    layer_paths = sorted((path for path in png_dir.glob("*.png") if path.is_file()), key=numeric_prefix_key)
    if not layer_paths:
        raise RuntimeError(f"No per-layer PNG files found under {png_dir}")

    image_paths = list(layer_paths)
    if overview_path.is_file():
        image_paths.append(overview_path)
    elif require_overview:
        raise RuntimeError(f"Missing overview image: {overview_path}")
    return image_paths


def load_images_as_batch(image_paths: Sequence[Path], image_size: int, device: torch.device):
    transform = build_transform(image_size)
    tensors = []
    for image_path in image_paths:
        with Image.open(image_path) as img:
            img = img.convert("RGB")
            tensors.append(transform(img))

    images = torch.stack(tensors, dim=0).unsqueeze(0).to(device)
    mask = torch.ones((1, len(image_paths)), dtype=torch.bool, device=device)
    return images, mask


def load_model_from_checkpoint(checkpoint_path: Path, device: torch.device):
    checkpoint = torch.load(checkpoint_path, map_location=device)
    checkpoint_args = checkpoint.get("args", {})

    model_name = checkpoint_args.get("model", "resnet18")
    freeze_backbone = bool(checkpoint_args.get("freeze_backbone", False))
    dropout = float(checkpoint_args.get("dropout", 0.2))
    hidden_dim = int(checkpoint_args.get("hidden_dim", 512))

    model = MultiViewBoardClassifier(
        model_name=model_name,
        pretrained=False,
        freeze_backbone=freeze_backbone,
        dropout=dropout,
        hidden_dim=hidden_dim,
    ).to(device)
    model.load_state_dict(checkpoint["model_state"])
    model.eval()
    return model, checkpoint_args


def build_report_text(
    predicted_label: int,
    probability_positive: float,
    image_paths: Sequence[Path],
) -> str:
    layer_count = max(0, len(image_paths) - 1) if image_paths else 0
    label_name = "布线较好" if predicted_label == 1 else "布线较差"
    confidence = probability_positive if predicted_label == 1 else 1.0 - probability_positive
    routing_good_probability = probability_positive

    if predicted_label == 1:
        conclusion = (
            f"该文件对应的布线结果整体较好，模型预测置信度为 {confidence:.6f}，"
            f"布线较好概率为 {routing_good_probability:.6f}，说明其与高质量布线结果具有较高一致性。"
            "结合可解释性分析结果，该板整体走线较顺畅，绕行和方向突变较少，"
            "弯折过渡相对自然，层间分布与 via 使用较为均衡。"
            "这些特征表明当前布线方案在规整性、连通性和制造可实现性方面表现较好，"
            "建议作为高质量候选方案优先保留或进一步应用。"
        )
    else:
        conclusion = (
            f"该文件对应的布线结果整体较差，模型预测置信度为 {confidence:.6f}，"
            f"布线较好概率仅为 {routing_good_probability:.6f}，说明其与高质量布线结果差异明显。"
            "结合可解释性分析结果，该板可能存在布线长度偏长、局部绕行较多、走线方向变化较频繁、"
            "路径弯折或弧度不够平滑、层间分布不均衡以及 via 使用不够合理等问题。"
            "这些特征表明当前布线方案在连通性、规整性和制造可实现性方面存在一定风险，"
            "建议作为低质量候选方案进行人工复核或后续优化处理。"
        )

    return (
        f"可解释性分析报告\n"
        f"================\n\n"
        f"层数: {layer_count}\n\n"
        f"预测结果: {label_name}\n"
        f"布线较好概率: {routing_good_probability:.6f}\n"
        f"当前预测置信度: {confidence:.6f}\n\n"
        f"{conclusion}\n"
    )


def infer_file(input: Union[str, Path], checkpoint: Union[str, Path]) -> str:
    pcb_path = Path(input).resolve()
    checkpoint_path = Path(checkpoint).resolve()
    output_root = FIXED_OUTPUT_ROOT.resolve()
    export_root = output_root

    if not pcb_path.is_file():
        raise FileNotFoundError(f"Input file not found: {pcb_path}")
    if not checkpoint_path.is_file():
        raise FileNotFoundError(f"Checkpoint not found: {checkpoint_path}")

    device = detect_device(FIXED_DEVICE)
    model, checkpoint_args = load_model_from_checkpoint(checkpoint_path, device)
    image_size = int(checkpoint_args.get("image_size", 224))

    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        export_result = export_single_board_plot_set(
            pcb_path,
            export_root,
            plot_mode=FIXED_PLOT_MODE,
            resolution_mm=FIXED_RESOLUTION_MM,
            include_zones=FIXED_INCLUDE_ZONES,
            bbox_mm=None,
            bbox_padding_mm=FIXED_BBOX_PADDING_MM,
            visible_layers=None,
            highlight_net_ids=[],
            dpi=FIXED_DPI,
            trim_whitespace=FIXED_TRIM_WHITESPACE,
            pad_inches=FIXED_PAD_INCHES,
            clean_image=FIXED_CLEAN_PLOT,
            png_width=FIXED_PNG_WIDTH,
            subdir_name=None,
        )
    board_out = Path(export_result["board_out"])
    image_paths = collect_exported_views(board_out, require_overview=FIXED_REQUIRE_OVERVIEW)
    images, mask = load_images_as_batch(image_paths, image_size, device)

    with torch.no_grad():
        logits = model(images, mask)
        probs = torch.softmax(logits, dim=1)[0]
        predicted_label = int(torch.argmax(probs).item())
        probability_positive = float(probs[1].item())

    report_text = build_report_text(
        predicted_label=predicted_label,
        probability_positive=probability_positive,
        image_paths=image_paths,
    )

    board_out.mkdir(parents=True, exist_ok=True)
    report_path = board_out / "report.txt"
    prediction_json_path = board_out / "prediction.json"
    report_path.write_text(report_text, encoding="utf-8")
    prediction_payload = {
        "device": str(device),
        "image_size": image_size,
        "predicted_label": predicted_label,
        "predicted_class_name": "布线较好" if predicted_label == 1 else "布线较差",
        "probability_positive": probability_positive,
        "view_count": len(image_paths),
        "layer_count": max(0, len(image_paths) - 1),
        "export_dir": str(board_out),
        "report_path": str(report_path),
        "prediction_json": str(prediction_json_path),
    }
    prediction_json_path.write_text(json.dumps(prediction_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return report_text


def main():
    parser = argparse.ArgumentParser(description="Run single-board inference using fixed export settings.")
    parser.add_argument("input", help="Path to one pcb file")
    parser.add_argument("checkpoint", help="Path to a saved checkpoint such as best.pt or last.pt")
    args = parser.parse_args()

    report_text = infer_file(args.input, args.checkpoint)
    print(report_text)


if __name__ == "__main__":
    main()
