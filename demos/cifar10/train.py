"""CIFAR-10 training demo instrumented with nebo.

Run:  python train.py            # one run into group "cifar10", ~4 min CPU
      python train.py --smoke    # tiny subset, ~30 s
See README.md for the sweep and the alert-rule walkthrough.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision
import torchvision.transforms as T
from torch.utils.data import DataLoader, Subset

import nebo as nb

CLASSES = ["plane", "car", "bird", "cat", "deer", "dog", "frog", "horse", "ship", "truck"]
MEAN = (0.4914, 0.4822, 0.4465)
STD = (0.247, 0.243, 0.261)
DATA_DIR = Path(__file__).parent / "data"

nb.md("""
# CIFAR-10 training

A ~160k-parameter CNN (three conv blocks, two linear layers) trains on
real CIFAR-10 — 32x32 photos in ten everyday classes — and logs its
whole life here.

**How to read this run**

- **`epoch/loss`** carries both curves in one chart via tags: `train`
  (epoch average over batches) vs `val`. Watch the gap between them —
  it opens as the model starts memorizing. `train/batch_loss` is the
  same story at batch resolution, noisy on purpose.
- **`val/per_class_accuracy`** is a snapshot bar re-emitted every epoch,
  so it always shows the latest state. Cats and dogs stay hardest —
  they're where the confusion flows.
- **`weights/distributions`** overlays each layer's weight histogram:
  tight init spikes early, spreading as features form.
- **`val/embeddings`** is a PCA of the penultimate 128-d features after
  the final epoch, one colored series per class. Clusters that separate
  are classes the model tells apart; overlaps predict the mistakes in
  the misclassified stream.
- **`val/misclassified/<true>-as-<pred>`** image streams collect the
  errors, named by what happened (`cat-as-dog`), a few per epoch —
  scrub steps to watch the mistakes change as training progresses.

Run `sweep.py` for four configs side by side in
[cifar10/sweep](nebo://group/cifar10/sweep) — the comparison view
overlays their accuracy curves in run colors.
""")
nb.ui(view="flat", tracker="step")


class SmallCNN(nn.Module):
    def __init__(self, width: int = 32):
        super().__init__()
        w = width
        self.conv1 = nn.Conv2d(3, w, 3, padding=1)
        self.conv2 = nn.Conv2d(w, w, 3, padding=1)
        self.conv3 = nn.Conv2d(w, 2 * w, 3, padding=1)
        self.fc1 = nn.Linear(2 * w * 4 * 4, 128)
        self.fc2 = nn.Linear(128, 10)

    def features(self, x):
        x = F.max_pool2d(F.relu(self.conv1(x)), 2)
        x = F.max_pool2d(F.relu(self.conv2(x)), 2)
        x = F.max_pool2d(F.relu(self.conv3(x)), 2)
        return F.relu(self.fc1(torch.flatten(x, 1)))

    def forward(self, x):
        return self.fc2(self.features(x))


@nb.fn(ui={"default_tab": "metrics"})
def prepare_data(train_size: int, batch_size: int):
    """Download CIFAR-10 and build train/val loaders."""
    tfm = T.Compose([T.ToTensor(), T.Normalize(MEAN, STD)])
    train = torchvision.datasets.CIFAR10(str(DATA_DIR), train=True, download=True, transform=tfm)
    test = torchvision.datasets.CIFAR10(str(DATA_DIR), train=False, download=True, transform=tfm)
    idx = torch.randperm(len(train), generator=torch.Generator().manual_seed(0))[:train_size]
    subset = Subset(train, idx.tolist())
    counts = np.bincount([train.targets[i] for i in subset.indices], minlength=10)
    nb.log_pie("data/class_distribution", {CLASSES[i]: int(counts[i]) for i in range(10)})
    nb.log_text("data/status", f"{len(subset)} train / {len(test)} val images")
    return (
        DataLoader(subset, batch_size=batch_size, shuffle=True),
        DataLoader(test, batch_size=256),
    )


@nb.fn(ui={"default_tab": "text"})
def build_model(width: int):
    """Construct the SmallCNN."""
    model = SmallCNN(width)
    n = sum(p.numel() for p in model.parameters())
    nb.log_text("model/summary", f"SmallCNN(width={width}): {n:,} parameters")
    return model


@nb.fn(ui={"default_tab": "metrics"})
def train_epoch(model, optimizer, train_loader, epoch: int) -> float:
    """One pass over the training set."""
    model.train()
    losses = []
    for i, (x, y) in enumerate(nb.track(train_loader, name=f"epoch {epoch}")):
        optimizer.zero_grad()
        loss = F.cross_entropy(model(x), y)
        loss.backward()
        optimizer.step()
        losses.append(loss.item())
        if i % 10 == 0:
            nb.log_line("train/batch_loss", loss.item(), step=epoch * len(train_loader) + i)
    avg = float(np.mean(losses))
    return avg


@nb.fn(ui={"default_tab": "metrics"})
def evaluate(model, test_loader, epoch: int, train_loss: float) -> float:
    """Validation pass: loss/accuracy, per-class bar, misclassified images."""
    model.eval()
    correct = np.zeros(10)
    total = np.zeros(10)
    losses = []
    shown = 0
    mean_t = torch.tensor(MEAN)[:, None, None]
    std_t = torch.tensor(STD)[:, None, None]
    with torch.no_grad():
        for x, y in test_loader:
            logits = model(x)
            losses.append(F.cross_entropy(logits, y).item())
            pred = logits.argmax(1)
            for c in range(10):
                sel = y == c
                total[c] += int(sel.sum())
                correct[c] += int((pred[sel] == c).sum())
            for j in (pred != y).nonzero().flatten().tolist():
                if shown >= 6:
                    break
                img = (x[j] * std_t + mean_t).clamp(0, 1)
                img = (img * 255).byte().permute(1, 2, 0).numpy()
                img = img.repeat(4, axis=0).repeat(4, axis=1)  # 32px -> 128px
                nb.log_image(
                    img,
                    name=f"val/misclassified/{CLASSES[y[j]]}-as-{CLASSES[pred[j]]}",
                    step=epoch,
                )
                shown += 1
    acc = float(100.0 * correct.sum() / total.sum())
    nb.log_line("epoch/loss", train_loss, step=epoch, tags=["train"])
    nb.log_line("epoch/loss", float(np.mean(losses)), step=epoch, tags=["val"])
    nb.log_line("epoch/accuracy", acc, step=epoch, tags=["val"])
    nb.log_bar(
        "val/per_class_accuracy",
        {CLASSES[c]: round(100.0 * correct[c] / max(total[c], 1), 1) for c in range(10)},
    )
    return acc


@nb.fn(ui={"default_tab": "metrics"})
def log_weight_histograms(model, epoch: int) -> None:
    """Per-layer weight distributions, overlaid in one histogram."""
    rng = np.random.default_rng(0)
    dists = {}
    for lname in ("conv1", "conv2", "conv3", "fc1", "fc2"):
        w = getattr(model, lname).weight.detach().flatten().numpy()
        if w.size > 2000:
            w = rng.choice(w, 2000, replace=False)
        dists[lname] = w.tolist()
    nb.log_histogram("weights/distributions", dists, colors=True)


@nb.fn(ui={"default_tab": "metrics"})
def embedding_scatter(model, test_loader) -> None:
    """PCA of penultimate features on ~1000 val images, one series per class."""
    model.eval()
    feats, labels = [], []
    seen = 0
    with torch.no_grad():
        for x, y in test_loader:
            feats.append(model.features(x))
            labels.append(y)
            seen += len(y)
            if seen >= 1000:
                break
    f = torch.cat(feats)[:1000].numpy()
    lab = torch.cat(labels)[:1000].numpy()
    centered = f - f.mean(0)
    _, _, vt = np.linalg.svd(centered, full_matrices=False)
    proj = centered @ vt[:2].T
    scatter = {}
    for c in range(10):
        pts = proj[lab == c][:60]
        if len(pts):
            scatter[CLASSES[c]] = [(round(float(px), 3), round(float(py), 3)) for px, py in pts]
    nb.log_scatter("val/embeddings", scatter, colors=True)


@nb.fn()
def run_training(cfg: dict) -> float:
    """Full training loop; returns final val accuracy (%)."""
    nb.log_cfg(cfg)
    train_loader, test_loader = prepare_data(cfg["train_size"], cfg["batch_size"])
    model = build_model(cfg["width"])
    optimizer = torch.optim.Adam(model.parameters(), lr=cfg["lr"])
    acc = 0.0
    for epoch in nb.track(range(cfg["epochs"]), name="epochs"):
        train_avg = train_epoch(model, optimizer, train_loader, epoch)
        acc = evaluate(model, test_loader, epoch, train_avg)
        log_weight_histograms(model, epoch)
    embedding_scatter(model, test_loader)
    nb.log_text("summary", f"final val accuracy {acc:.1f}%")
    return acc


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--width", type=int, default=32)
    ap.add_argument("--batch-size", type=int, default=128)
    ap.add_argument("--train-size", type=int, default=20000)
    ap.add_argument("--group", default="cifar10")
    ap.add_argument("--name", default=None)
    ap.add_argument("--smoke", action="store_true", help="tiny subset, ~30 s")
    args = ap.parse_args(argv)
    if args.smoke:
        args.epochs, args.train_size = 1, 2000
    return args


def main(argv=None) -> float:
    args = parse_args(argv)
    cfg = {
        "epochs": args.epochs,
        "lr": args.lr,
        "width": args.width,
        "batch_size": args.batch_size,
        "train_size": args.train_size,
    }
    name = args.name or f"lr={args.lr:g}-w{args.width}"
    with nb.start_run(name=name, config=cfg, group=args.group):
        return run_training(cfg)


if __name__ == "__main__":
    main()
