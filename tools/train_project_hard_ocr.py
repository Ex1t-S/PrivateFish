r"""Build a small Projekt Hard digit OCR dataset/model from saved bubble crops.

The model is intentionally simple: extract the orange digit mask, normalize it,
augment it with small shifts, and save KNN-ready feature vectors to assets.
Run from the repo root:

    .venv\Scripts\python.exe tools\train_project_hard_ocr.py
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
import re
import shutil
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fishing_bot import FishingBot  # noqa: E402


ASSETS = ROOT / "assets"
DATASET_DIR = ASSETS / "projekt_hard_ocr_dataset"
MODEL_PATH = ASSETS / "projekt_hard_number_knn.npz"
MANUAL_LABELS_PATH = ASSETS / "projekt_hard_manual_labels.csv"


def parse_timestamp(path: Path) -> datetime | None:
    match = re.search(r"_(20\d{6}_\d{6})_", path.name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def load_manual_labels() -> tuple[dict[str, int], set[str], list[tuple[datetime, int]]]:
    exact: dict[str, int] = {}
    skip: set[str] = set()
    timeline: list[tuple[datetime, int]] = []
    if not MANUAL_LABELS_PATH.exists():
        return exact, skip, timeline

    with MANUAL_LABELS_PATH.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            filename = (row.get("filename") or "").strip()
            label_text = (row.get("label") or "").strip()
            if not filename:
                continue
            if label_text not in {"1", "2", "3"}:
                skip.add(filename)
                continue
            label = int(label_text)
            exact[filename] = label
            timestamp = parse_timestamp(Path(filename))
            if timestamp is not None:
                timeline.append((timestamp, label))

    timeline.sort(key=lambda item: item[0])
    return exact, skip, timeline


def manual_label_for_path(path: Path, exact: dict[str, int], timeline: list[tuple[datetime, int]]) -> int:
    if path.name in exact:
        return exact[path.name]
    timestamp = parse_timestamp(path)
    if timestamp is None:
        return 0

    best_label = 0
    best_delta = None
    for sample_time, label in timeline:
        delta = (timestamp - sample_time).total_seconds()
        if 0.0 <= delta <= 4.0 and (best_delta is None or delta < best_delta):
            best_label = label
            best_delta = delta
    return best_label


def parse_guess(path: Path) -> int:
    match = re.search(r"_guess([123])_", path.name)
    return int(match.group(1)) if match else 0


def parse_scores(path: Path) -> tuple[int, float, float]:
    match = re.search(
        r"scores1-([0-9]+(?:\.[0-9]+)?)_2-([0-9]+(?:\.[0-9]+)?)_3-([0-9]+(?:\.[0-9]+)?)",
        path.name,
    )
    if not match:
        guess = parse_guess(path)
        return guess, 1.0 if guess else 0.0, 1.0
    scores = {idx + 1: float(value) for idx, value in enumerate(match.groups())}
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best_label, best_score = ordered[0]
    second_score = ordered[1][1]
    return best_label, best_score, best_score - second_score


def normalize_mask(mask: np.ndarray) -> np.ndarray | None:
    if mask is None or mask.size == 0:
        return None
    binary = (mask > 0).astype(np.uint8) * 255
    ys, xs = np.where(binary > 0)
    if len(xs) < 18:
        return None

    x1 = max(0, int(xs.min()) - 2)
    x2 = min(binary.shape[1], int(xs.max()) + 3)
    y1 = max(0, int(ys.min()) - 2)
    y2 = min(binary.shape[0], int(ys.max()) + 3)
    binary = binary[y1:y2, x1:x2]
    if binary.size == 0:
        return None

    out = np.zeros((64, 48), dtype=np.uint8)
    h, w = binary.shape
    scale = min(40.0 / max(1, w), 56.0 / max(1, h))
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    resized = cv2.resize(binary, (new_w, new_h), interpolation=cv2.INTER_NEAREST)
    ox = (48 - new_w) // 2
    oy = (64 - new_h) // 2
    out[oy : oy + new_h, ox : ox + new_w] = resized
    return out


def feature_from_mask(mask: np.ndarray) -> np.ndarray | None:
    norm = normalize_mask(mask)
    if norm is None:
        return None
    small = cv2.resize(norm, (24, 32), interpolation=cv2.INTER_AREA)
    binary = (small > 40).astype(np.float32)
    features = [binary.reshape(-1)]
    features.append(binary.sum(axis=0) / 32.0)
    features.append(binary.sum(axis=1) / 24.0)
    grid = []
    for gy in range(4):
        for gx in range(3):
            cell = binary[gy * 8 : (gy + 1) * 8, gx * 8 : (gx + 1) * 8]
            grid.append(float(cell.mean()))
    features.append(np.array(grid, dtype=np.float32))
    vector = np.concatenate(features).astype(np.float32)
    norm_value = float(np.linalg.norm(vector))
    if norm_value > 0:
        vector /= norm_value
    return vector


def shifted_masks(mask: np.ndarray) -> list[np.ndarray]:
    masks = [mask]
    for dy, dx in ((-2, 0), (2, 0), (0, -2), (0, 2), (-2, -2), (2, 2)):
        shifted = np.zeros_like(mask)
        h, w = mask.shape
        src_x1 = max(0, -dx)
        src_x2 = min(w, w - dx)
        dst_x1 = max(0, dx)
        dst_x2 = min(w, w + dx)
        src_y1 = max(0, -dy)
        src_y2 = min(h, h - dy)
        dst_y1 = max(0, dy)
        dst_y2 = min(h, h + dy)
        if src_x1 < src_x2 and src_y1 < src_y2:
            shifted[dst_y1:dst_y2, dst_x1:dst_x2] = mask[src_y1:src_y2, src_x1:src_x2]
            masks.append(shifted)
    return masks


def iter_labeled_images() -> list[tuple[Path, int]]:
    items: list[tuple[Path, int]] = []
    manual_exact, manual_skip, manual_timeline = load_manual_labels()

    for path in sorted(ASSETS.glob("projekt_hard_number_[123]*.png")):
        match = re.search(r"projekt_hard_number_([123])", path.name)
        if match:
            items.append((path, int(match.group(1))))

    for path in sorted((ASSETS / "projekt_hard_detections").glob("ph_detect_*.png")):
        manual_label = manual_label_for_path(path, manual_exact, manual_timeline)
        if manual_label:
            items.append((path, manual_label))
            continue
        label, score, margin = parse_scores(path)
        if label and (score >= 0.38 or margin >= 0.08):
            items.append((path, label))

    for path in sorted((ASSETS / "projekt_hard_samples").glob("ph_bubble_*.png")):
        if path.name in manual_skip:
            continue
        manual_label = manual_label_for_path(path, manual_exact, manual_timeline)
        if manual_label:
            items.append((path, manual_label))
            continue
        label = parse_guess(path)
        if label:
            items.append((path, label))

    return items


def extract_mask(bot: FishingBot, image: np.ndarray) -> np.ndarray | None:
    loc, shape, color_conf = bot._find_projekt_hard_bubble_by_color(image)
    if loc is not None and shape is not None and color_conf >= 0.35:
        x, y = loc
        h, w = shape
        image = image[y : y + h, x : x + w]
    return bot._extract_projekt_hard_number_mask(image)


def build_dataset(reset: bool = True) -> tuple[np.ndarray, np.ndarray, dict[int, int]]:
    bot = FishingBot.__new__(FishingBot)
    bot.config = {}
    bot.on_status_update = None
    bot.bot_id = 0

    if reset and DATASET_DIR.exists():
        shutil.rmtree(DATASET_DIR)
    for label in (1, 2, 3):
        (DATASET_DIR / str(label)).mkdir(parents=True, exist_ok=True)

    features: list[np.ndarray] = []
    labels: list[int] = []
    counts = {1: 0, 2: 0, 3: 0}

    for source, label in iter_labeled_images():
        image = cv2.imread(str(source))
        if image is None:
            continue
        mask = extract_mask(bot, image)
        normalized = normalize_mask(mask)
        if normalized is None:
            continue

        counts[label] += 1
        clean_name = f"ph_digit_{label}_{counts[label]:04d}_{source.stem[:80]}.png"
        cv2.imwrite(str(DATASET_DIR / str(label) / clean_name), normalized)

        for augmented in shifted_masks(mask):
            vector = feature_from_mask(augmented)
            if vector is None:
                continue
            features.append(vector)
            labels.append(label)

    if not features:
        raise RuntimeError("No training samples were extracted")

    x = np.vstack(features).astype(np.float32)
    y = np.array(labels, dtype=np.int32)
    np.savez_compressed(MODEL_PATH, features=x, labels=y)
    return x, y, counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-existing", action="store_true", help="Do not clear the generated dataset folder first")
    args = parser.parse_args()

    x, y, counts = build_dataset(reset=not args.keep_existing)
    print(f"Saved dataset: {DATASET_DIR}")
    print(f"Saved model: {MODEL_PATH}")
    print(f"Source masks by label: {counts}")
    print(f"Training vectors: {len(y)} | feature width: {x.shape[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
