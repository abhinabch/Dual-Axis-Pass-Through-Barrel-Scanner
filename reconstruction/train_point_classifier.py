"""Training script for PointNet point-level outlier classifier.

Trains PointNetClassifier to distinguish clean surface points (0)
from outlier points (bung=1, floater=2).

Usage:
    python train_point_classifier.py --epochs 10
"""
import argparse
import os
import time

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim

from barrel_synth import generate_barrel
from barrel_features import compute_point_curvature
from barrel_denoise_points import PointNetClassifier, DEFAULT_POINT_CHECKPOINT


def generate_point_sample(seed, n_points=20_000):
    """Generate one synthetic training chunk of points (7, N) and binary labels."""
    b = generate_barrel(seed=seed, n_points=n_points, add_bung=True, add_floaters=True)
    P, N = b["P_noisy"], b["N_noisy"]
    labels = b["labels"]

    # Target: 1 for outlier (bung or floater), 0 for clean
    target_outlier = (labels == 1) | (labels == 2)

    curv = compute_point_curvature(P, k=20)
    center = P.mean(axis=0, keepdims=True)
    P_rel = P - center

    feat = np.column_stack([P_rel, N, curv]).T.astype(np.float32)  # (7, N)
    return feat, target_outlier.astype(np.float32)


def main():
    parser = argparse.ArgumentParser(description="Train PointNet point classifier")
    parser.add_argument("--epochs", type=int, default=10)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--output", type=str, default=DEFAULT_POINT_CHECKPOINT)
    args = parser.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print("Training PointNetClassifier on device: %s" % device)

    model = PointNetClassifier(in_channels=7, hidden_dim=64).to(device)
    optimizer = optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    criterion = nn.BCEWithLogitsLoss()

    best_loss = float("inf")
    print("\nStarting training for %d epochs..." % args.epochs)
    print("Epoch | Loss   | Precision | Recall | Time")
    print("-" * 45)

    for epoch in range(1, args.epochs + 1):
        t0 = time.time()
        model.train()

        epoch_loss = 0.0
        n_samples = 10
        total_tp, total_fp, total_fn = 0, 0, 0

        for idx in range(n_samples):
            seed = epoch * 500 + idx
            feat, target = generate_point_sample(seed=seed, n_points=2048)

            inp_t = torch.from_numpy(feat).unsqueeze(0).to(device)  # (1, 7, N)
            target_t = torch.from_numpy(target).unsqueeze(0).to(device)  # (1, N)

            optimizer.zero_grad()
            logits = model(inp_t)
            loss = criterion(logits, target_t)
            loss.backward()
            optimizer.step()

            epoch_loss += loss.item()

            with torch.no_grad():
                preds = (torch.sigmoid(logits) > 0.5).squeeze(0).cpu().numpy()
                tp = int((preds & (target == 1)).sum())
                fp = int((preds & (target == 0)).sum())
                fn = int((~preds & (target == 1)).sum())
                total_tp += tp
                total_fp += fp
                total_fn += fn

        avg_loss = epoch_loss / n_samples
        prec = total_tp / (total_tp + total_fp + 1e-8)
        rec = total_tp / (total_tp + total_fn + 1e-8)
        t_elapsed = time.time() - t0

        marker = ""
        if avg_loss < best_loss:
            best_loss = avg_loss
            torch.save(model.state_dict(), args.output)
            marker = " [saved]"

        print("%5d | %.4f | %9.3f | %6.3f | %.1fs%s" %
              (epoch, avg_loss, prec, rec, t_elapsed, marker))

    print("\nTraining complete! Checkpoint saved to: %s" % args.output)


if __name__ == "__main__":
    main()
