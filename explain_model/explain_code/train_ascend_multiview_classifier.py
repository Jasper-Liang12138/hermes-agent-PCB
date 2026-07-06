#!/usr/bin/env python3
import argparse
import json
import math
import random
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import torch
import torch.nn as nn
import torch.optim as optim
from PIL import Image
from torch.utils.data import DataLoader, Dataset
from torchvision import models, transforms

try:
    import torch_npu  # noqa: F401
except ImportError:
    torch_npu = None  # type: ignore[assignment]


def set_seed(seed: int) -> None:
    random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    if hasattr(torch, "npu") and torch.npu.is_available():
        torch.npu.manual_seed_all(seed)


def detect_device(device_arg: str) -> torch.device:
    if device_arg != "auto":
        device = torch.device(device_arg)
        if device.type == "npu" and hasattr(torch, "npu"):
            torch.npu.set_device(device)
        return device

    if hasattr(torch, "npu") and torch.npu.is_available():
        device = torch.device("npu:0")
        torch.npu.set_device(device)
        return device
    if torch.cuda.is_available():
        return torch.device("cuda:0")
    return torch.device("cpu")


def safe_json_dump(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def numeric_prefix_key(path: Path) -> Tuple[int, str]:
    stem = path.stem
    head = stem.split("_", 1)[0]
    if head.isdigit():
        return int(head), stem
    return 10**9, stem


@dataclass
class BoardSample:
    pair_id: str
    role: str
    label: int
    image_paths: List[Path]


def collect_board_samples(data_root: Path, require_overview: bool = True) -> List[BoardSample]:
    samples: List[BoardSample] = []
    for pair_dir in sorted(p for p in data_root.iterdir() if p.is_dir()):
        for role_name, label in (("positive", 1), ("negative", 0)):
            role_dir = pair_dir / role_name
            png_dir = role_dir / "png"
            overview_path = role_dir / "overview" / "overview_all_layers.png"
            if not png_dir.is_dir():
                continue

            layer_paths = sorted((p for p in png_dir.glob("*.png") if p.is_file()), key=numeric_prefix_key)
            if not layer_paths:
                continue
            if require_overview and not overview_path.is_file():
                continue

            image_paths = list(layer_paths)
            if overview_path.is_file():
                image_paths.append(overview_path)

            samples.append(
                BoardSample(
                    pair_id=pair_dir.name,
                    role=role_name,
                    label=label,
                    image_paths=image_paths,
                )
            )

    if not samples:
        raise RuntimeError(f"No board-view samples found under {data_root}")
    return samples


def summarize_board_samples(samples: Sequence[BoardSample]) -> Dict[str, int]:
    pos = sum(1 for sample in samples if sample.label == 1)
    neg = sum(1 for sample in samples if sample.label == 0)
    return {
        "samples": len(samples),
        "pairs": len({sample.pair_id for sample in samples}),
        "positive": pos,
        "negative": neg,
    }


def split_by_pair(samples: Sequence[BoardSample], val_ratio: float, seed: int) -> Tuple[List[BoardSample], List[BoardSample]]:
    pair_ids = sorted({sample.pair_id for sample in samples})
    rng = random.Random(seed)
    rng.shuffle(pair_ids)
    val_count = max(1, int(round(len(pair_ids) * val_ratio)))
    val_pairs = set(pair_ids[:val_count])
    train_samples = [sample for sample in samples if sample.pair_id not in val_pairs]
    val_samples = [sample for sample in samples if sample.pair_id in val_pairs]
    return train_samples, val_samples


class BoardViewDataset(Dataset):
    def __init__(self, samples: Sequence[BoardSample], image_size: int, train: bool):
        self.samples = list(samples)
        if train:
            self.transform = transforms.Compose([
                transforms.Resize((image_size, image_size)),
                transforms.RandomAffine(degrees=2, translate=(0.02, 0.02), scale=(0.97, 1.03)),
                transforms.ColorJitter(brightness=0.04, contrast=0.04, saturation=0.03),
                transforms.ToTensor(),
                transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
            ])
        else:
            self.transform = transforms.Compose([
                transforms.Resize((image_size, image_size)),
                transforms.ToTensor(),
                transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
            ])

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, idx: int):
        sample = self.samples[idx]
        images = []
        view_names = []
        for image_path in sample.image_paths:
            with Image.open(image_path) as img:
                img = img.convert("RGB")
                images.append(self.transform(img))
            view_names.append(image_path.name)
        return {
            "images": images,
            "label": sample.label,
            "pair_id": sample.pair_id,
            "role": sample.role,
            "view_names": view_names,
            "image_paths": [str(path) for path in sample.image_paths],
        }


def multiview_collate(batch: Sequence[dict]) -> dict:
    batch_size = len(batch)
    max_views = max(len(item["images"]) for item in batch)
    channels, height, width = batch[0]["images"][0].shape

    images = torch.zeros((batch_size, max_views, channels, height, width), dtype=batch[0]["images"][0].dtype)
    mask = torch.zeros((batch_size, max_views), dtype=torch.bool)
    labels = torch.tensor([item["label"] for item in batch], dtype=torch.long)

    pair_ids: List[str] = []
    roles: List[str] = []
    image_paths: List[List[str]] = []
    view_names: List[List[str]] = []

    for batch_index, item in enumerate(batch):
        pair_ids.append(item["pair_id"])
        roles.append(item["role"])
        image_paths.append(item["image_paths"])
        view_names.append(item["view_names"])
        for view_index, image in enumerate(item["images"]):
            images[batch_index, view_index] = image
            mask[batch_index, view_index] = True

    return {
        "images": images,
        "mask": mask,
        "labels": labels,
        "pair_ids": pair_ids,
        "roles": roles,
        "image_paths": image_paths,
        "view_names": view_names,
    }


class SharedBackboneEncoder(nn.Module):
    def __init__(self, model_name: str, pretrained: bool, freeze_backbone: bool):
        super().__init__()
        if model_name == "resnet18":
            weights = models.ResNet18_Weights.IMAGENET1K_V1 if pretrained else None
            backbone = models.resnet18(weights=weights)
            feature_dim = backbone.fc.in_features
            backbone.fc = nn.Identity()
        elif model_name == "resnet50":
            weights = models.ResNet50_Weights.IMAGENET1K_V2 if pretrained else None
            backbone = models.resnet50(weights=weights)
            feature_dim = backbone.fc.in_features
            backbone.fc = nn.Identity()
        elif model_name == "convnext_tiny":
            weights = models.ConvNeXt_Tiny_Weights.IMAGENET1K_V1 if pretrained else None
            backbone = models.convnext_tiny(weights=weights)
            feature_dim = backbone.classifier[-1].in_features
            backbone.classifier = nn.Sequential(
                backbone.classifier[0],
                nn.Flatten(1),
                nn.LayerNorm(feature_dim, eps=1e-6),
            )
        else:
            raise ValueError(f"Unsupported model: {model_name}")

        if freeze_backbone:
            for param in backbone.parameters():
                param.requires_grad = False

        self.backbone = backbone
        self.feature_dim = feature_dim

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.backbone(x)


class MultiViewBoardClassifier(nn.Module):
    def __init__(
        self,
        model_name: str,
        pretrained: bool = True,
        freeze_backbone: bool = False,
        dropout: float = 0.2,
        hidden_dim: int = 512,
    ):
        super().__init__()
        self.encoder = SharedBackboneEncoder(
            model_name=model_name,
            pretrained=pretrained,
            freeze_backbone=freeze_backbone,
        )
        feature_dim = self.encoder.feature_dim
        self.view_projection = nn.Sequential(
            nn.LayerNorm(feature_dim),
            nn.Linear(feature_dim, hidden_dim),
            nn.GELU(),
        )
        self.classifier = nn.Sequential(
            nn.LayerNorm(hidden_dim * 2),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim * 2, hidden_dim),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, 2),
        )

    def forward(self, images: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
        batch_size, num_views, channels, height, width = images.shape
        encoded = self.encoder(images.reshape(batch_size * num_views, channels, height, width))
        projected = self.view_projection(encoded).reshape(batch_size, num_views, -1)

        float_mask = mask.unsqueeze(-1).float()
        masked_projected = projected * float_mask
        pooled_mean = masked_projected.sum(dim=1) / float_mask.sum(dim=1).clamp_min(1.0)

        max_fill = torch.finfo(projected.dtype).min
        pooled_max = projected.masked_fill(~mask.unsqueeze(-1), max_fill).max(dim=1).values
        pooled = torch.cat([pooled_mean, pooled_max], dim=1)
        return self.classifier(pooled)


def compute_accuracy(logits: torch.Tensor, labels: torch.Tensor) -> float:
    preds = torch.argmax(logits, dim=1)
    return (preds == labels).float().mean().item()


def build_autocast_context(device: torch.device, use_amp: bool):
    if not use_amp:
        return nullcontext()
    if not hasattr(torch, "autocast"):
        return nullcontext()
    if device.type not in {"cuda", "npu"}:
        return nullcontext()
    return torch.autocast(device_type=device.type, dtype=torch.float16)


def run_epoch(model, loader, criterion, optimizer, device, train: bool, use_amp: bool, log_interval: int):
    model.train() if train else model.eval()
    total_loss = 0.0
    total_acc = 0.0
    total_count = 0
    start_time = time.time()

    grad_context = torch.enable_grad() if train else torch.no_grad()
    with grad_context:
        for step, batch in enumerate(loader, start=1):
            images = batch["images"].to(device, non_blocking=True)
            mask = batch["mask"].to(device, non_blocking=True)
            labels = batch["labels"].to(device, non_blocking=True)

            with build_autocast_context(device, use_amp):
                logits = model(images, mask)
                loss = criterion(logits, labels)

            if train:
                optimizer.zero_grad(set_to_none=True)
                loss.backward()
                optimizer.step()

            batch_size = labels.shape[0]
            batch_acc = compute_accuracy(logits, labels)
            total_loss += loss.item() * batch_size
            total_acc += batch_acc * batch_size
            total_count += batch_size

            if log_interval and step % log_interval == 0:
                elapsed = time.time() - start_time
                avg_loss = total_loss / total_count
                avg_acc = total_acc / total_count
                mode = "train" if train else "val"
                lr = optimizer.param_groups[0]["lr"] if optimizer is not None else 0.0
                print(
                    f"  [{mode}] step={step:04d}/{len(loader):04d} "
                    f"batch_loss={loss.item():.4f} batch_acc={batch_acc:.4f} "
                    f"avg_loss={avg_loss:.4f} avg_acc={avg_acc:.4f} "
                    f"lr={lr:.2e} elapsed={elapsed:.1f}s"
                )

    elapsed = time.time() - start_time
    return total_loss / total_count, total_acc / total_count, elapsed


def evaluate_with_details(model, loader, device):
    model.eval()
    rows = []
    total_acc = 0.0
    total_count = 0

    with torch.no_grad():
        for batch in loader:
            images = batch["images"].to(device, non_blocking=True)
            mask = batch["mask"].to(device, non_blocking=True)
            labels = batch["labels"].to(device, non_blocking=True)
            logits = model(images, mask)
            probs = torch.softmax(logits, dim=1)[:, 1]
            preds = torch.argmax(logits, dim=1)

            total_acc += (preds == labels).float().sum().item()
            total_count += labels.shape[0]

            for index in range(labels.shape[0]):
                rows.append({
                    "pair_id": batch["pair_ids"][index],
                    "role": batch["roles"][index],
                    "label": int(labels[index].item()),
                    "pred": int(preds[index].item()),
                    "prob_positive": float(probs[index].item()),
                    "image_paths": batch["image_paths"][index],
                    "view_names": batch["view_names"][index],
                })

    return total_acc / total_count, rows


def save_checkpoint(path: Path, model, optimizer, args, history):
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "model_state": model.state_dict(),
            "optimizer_state": optimizer.state_dict(),
            "args": vars(args),
            "history": history,
        },
        path,
    )


def main():
    parser = argparse.ArgumentParser(
        description="Train a board-level multi-view classifier from per-layer PNGs + merged overview PNG."
    )
    parser.add_argument("--data-root", default="voxelized_layer_exports", help="Root folder containing pair directories")
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--image-size", type=int, default=224)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--weight-decay", type=float, default=1e-4)
    parser.add_argument("--val-ratio", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--num-workers", type=int, default=4)
    parser.add_argument("--output-dir", default="training_runs/ascend_multiview")
    parser.add_argument("--model", choices=["resnet18", "resnet50", "convnext_tiny"], default="resnet18")
    parser.add_argument("--no-pretrained", action="store_true")
    parser.add_argument("--freeze-backbone", action="store_true")
    parser.add_argument("--dropout", type=float, default=0.2)
    parser.add_argument("--hidden-dim", type=int, default=512)
    parser.add_argument("--device", default="auto", help='Device to use, e.g. "auto", "npu:0", "cuda:0", "cpu"')
    parser.add_argument("--log-interval", type=int, default=10)
    parser.add_argument("--amp", action="store_true", help="Enable autocast on CUDA/NPU")
    parser.add_argument("--require-overview", action="store_true", help="Skip samples missing overview_all_layers.png")
    args = parser.parse_args()

    set_seed(args.seed)
    data_root = Path(args.data_root)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    samples = collect_board_samples(data_root, require_overview=args.require_overview)
    train_samples, val_samples = split_by_pair(samples, args.val_ratio, args.seed)

    total_summary = summarize_board_samples(samples)
    train_summary = summarize_board_samples(train_samples)
    val_summary = summarize_board_samples(val_samples)

    print(f"Total samples: {total_summary['samples']} pairs={total_summary['pairs']}")
    print(f"Train samples: {train_summary['samples']} positive={train_summary['positive']} negative={train_summary['negative']}")
    print(f"Val samples: {val_summary['samples']} positive={val_summary['positive']} negative={val_summary['negative']}")

    train_ds = BoardViewDataset(train_samples, args.image_size, train=True)
    val_ds = BoardViewDataset(val_samples, args.image_size, train=False)

    device = detect_device(args.device)
    pin_memory = device.type == "cuda"

    train_loader = DataLoader(
        train_ds,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=args.num_workers,
        collate_fn=multiview_collate,
        pin_memory=pin_memory,
    )
    val_loader = DataLoader(
        val_ds,
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=args.num_workers,
        collate_fn=multiview_collate,
        pin_memory=pin_memory,
    )

    print(f"Using device: {device}")
    if device.type == "cuda":
        print(f"CUDA device count: {torch.cuda.device_count()}")
        print(f"Current CUDA device: {torch.cuda.get_device_name(device)}")
    if device.type == "npu" and hasattr(torch, "npu"):
        try:
            print(f"NPU device count: {torch.npu.device_count()}")
            print(f"Current NPU device: {torch.npu.current_device()}")
        except Exception:
            pass

    model = MultiViewBoardClassifier(
        model_name=args.model,
        pretrained=not args.no_pretrained,
        freeze_backbone=args.freeze_backbone,
        dropout=args.dropout,
        hidden_dim=args.hidden_dim,
    ).to(device)

    total_params = sum(param.numel() for param in model.parameters())
    trainable_params = sum(param.numel() for param in model.parameters() if param.requires_grad)
    print(f"Model params: total={total_params:,} trainable={trainable_params:,}")
    print(
        f"Config: model={args.model} epochs={args.epochs} batch_size={args.batch_size} "
        f"image_size={args.image_size} lr={args.lr} weight_decay={args.weight_decay} amp={args.amp}"
    )

    criterion = nn.CrossEntropyLoss()
    optimizer = optim.AdamW(
        filter(lambda param: param.requires_grad, model.parameters()),
        lr=args.lr,
        weight_decay=args.weight_decay,
    )
    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs)

    best_val_acc = -math.inf
    history: List[dict] = []

    for epoch in range(1, args.epochs + 1):
        print(f"\nEpoch {epoch:03d}/{args.epochs:03d}")
        train_loss, train_acc, train_time = run_epoch(
            model, train_loader, criterion, optimizer, device, train=True, use_amp=args.amp, log_interval=args.log_interval
        )
        val_loss, val_acc, val_time = run_epoch(
            model, val_loader, criterion, optimizer, device, train=False, use_amp=False, log_interval=max(0, args.log_interval * 2)
        )
        scheduler.step()

        row = {
            "epoch": epoch,
            "train_loss": train_loss,
            "train_acc": train_acc,
            "val_loss": val_loss,
            "val_acc": val_acc,
            "lr": scheduler.get_last_lr()[0],
            "train_time_sec": train_time,
            "val_time_sec": val_time,
        }
        history.append(row)

        print(
            f"Epoch {epoch:03d} done | "
            f"train_loss={train_loss:.4f} train_acc={train_acc:.4f} | "
            f"val_loss={val_loss:.4f} val_acc={val_acc:.4f} | "
            f"lr={scheduler.get_last_lr()[0]:.2e} train_time={train_time:.1f}s val_time={val_time:.1f}s"
        )

        save_checkpoint(output_dir / "last.pt", model, optimizer, args, history)
        if val_acc > best_val_acc:
            best_val_acc = val_acc
            save_checkpoint(output_dir / "best.pt", model, optimizer, args, history)
            print(f"  New best checkpoint saved: {output_dir / 'best.pt'}")

    final_val_acc, details = evaluate_with_details(model, val_loader, device)
    safe_json_dump(output_dir / "history.json", history)
    safe_json_dump(output_dir / "val_predictions.json", details)

    print(f"Best val acc: {best_val_acc:.4f}")
    print(f"Final val acc: {final_val_acc:.4f}")
    print(f"Saved outputs to: {output_dir}")


if __name__ == "__main__":
    main()
