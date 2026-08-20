"""Training script for GridUNet denoiser model.

Trains on synthetic barrels on-the-fly, generating (P, N) point clouds,
binning them into grids via barrel_reconstruct.build_rho_grid/make_el_sampling,
and optimizing against synthetic ground-truth grids with curvature weighting
and head-pole dropout emphasis.

Usage:
    python train_grid_denoiser.py --epochs 30 --batch-size 4
"""
import argparse
import os
import sys
import time

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim

from barrel_synth import generate_barrel, generate_gt_grid
from barrel_features import compute_grid_curvature
from barrel_reconstruct import (
    spherical_coords, make_el_sampling, build_rho_grid, N_AZ, _frame
)
from barrel_denoise_grid import GridUNet, prepare_grid_inputs, DEFAULT_CHECKPOINT


def generate_train_sample(seed, n_points=100_000, head_dropout_rate=None,
                          dropout_range=(0.2, 0.95)):
    """Generate one synthetic training pair (inputs, target_rho, target_outlier).

    head_dropout_rate=None (the training default) draws a fresh dropout rate
    uniformly from dropout_range for EVERY sample, keyed off `seed` so it's
    reproducible. This is a deliberate curriculum: a single fixed dropout rate
    (the previous default of 0.4) under-represents the near-total emptiness
    real single-pass head/pole rows actually have, which is exactly the
    regime learned_clean_grid() was found to collapse in (see
    notebooks/05_rules_vs_learned_volume_accuracy.ipynb). Pass a fixed float
    to reproduce the old fixed-rate behavior (evaluate_model() below does
    this deliberately, for an honest, non-circular held-out check).
    """
    if head_dropout_rate is None:
        drop_rng = np.random.default_rng(seed * 7919 + 104729)  # decorrelated from generate_barrel's own seed
        head_dropout_rate = float(drop_rng.uniform(*dropout_range))

    b = generate_barrel(seed=seed, n_points=n_points, add_bung=True, add_floaters=True,
                        head_dropout_rate=head_dropout_rate)
    P, N = b["P_noisy"], b["N_noisy"]
    labels = b["labels"]

    a = np.array([1.0, 0.0, 0.0])
    centre = np.zeros(3)
    u, w = _frame(a)
    az, el, rho = spherical_coords(P, centre, a, u, w)

    # Bin into grid using actual reconstruction pipeline
    el_ctr, el_edges, corners = make_el_sampling(az, el, rho, N_AZ)
    grid, cnt = build_rho_grid(az, el, rho, N_AZ, el_edges)

    # Compute ground-truth rho grid
    gt_grid = generate_gt_grid(b["params"], el_ctr)

    # Target outlier mask (cells containing bung or floaters)
    # Cell is outlier if > 50% of its points are bung/floater
    ei = np.clip(np.digitize(el, el_edges) - 1, 0, len(el_ctr) - 1)
    ai = np.clip(((az + np.pi) / (2 * np.pi) * N_AZ).astype(int), 0, N_AZ - 1)
    outlier_points = (labels == 1) | (labels == 2)

    H, W = grid.shape
    outlier_count = np.zeros((H, W), dtype=np.float32)
    np.add.at(outlier_count, (ei[outlier_points], ai[outlier_points]), 1)
    cell_total = np.zeros((H, W), dtype=np.float32)
    np.add.at(cell_total, (ei, ai), 1)
    target_outlier = (outlier_count / (cell_total + 1e-8)) > 0.3

    # Feature input
    curv_grid = compute_grid_curvature(gt_grid, el_ctr)
    inp_tensor, _ = prepare_grid_inputs(grid, cnt, el_ctr, curv_grid=curv_grid)

    # Ground truth offset channel relative to 0.31 base, scale 0.05
    target_rho_offset = (gt_grid - 0.31) / 0.05

    return (inp_tensor.squeeze(0), target_rho_offset, target_outlier.astype(np.float32),
            curv_grid, el_ctr, corners, cnt, head_dropout_rate)


def train_epoch(model, optimizer, criterion_bce, batch_size=2, samples_per_epoch=10, device="cpu", epoch=1):
    model.train()
    total_loss = 0.0
    total_rho_loss = 0.0
    total_bce_loss = 0.0

    inps, targets_rho, targets_out, curvs, sparsities = [], [], [], [], []

    for idx in range(samples_per_epoch):
        seed = epoch * 1000 + idx
        inp, target_rho, target_out, curv, el_ctr, corners, cnt, dropout_used = generate_train_sample(seed=seed)

        # Per-cell sparsity weight: emptier cells (few/no raw points) get more loss
        # weight, wherever they happen to fall -- not just a fixed top/bottom-15%
        # row band. This directly targets the near-empty-cell collapse behavior
        # regardless of exactly how far into the head/pole region it occurs.
        sparsity = 1.0 / (1.0 + np.log1p(np.clip(cnt, 0, 50)))  # ~1.0 when empty, ~0.15 when well-sampled
        sparsities.append(torch.from_numpy(sparsity.astype(np.float32)).unsqueeze(0))

        inps.append(inp)
        targets_rho.append(torch.from_numpy(target_rho.astype(np.float32)).unsqueeze(0))
        targets_out.append(torch.from_numpy(target_out.astype(np.float32)).unsqueeze(0))
        curvs.append(torch.from_numpy(curv.astype(np.float32)).unsqueeze(0))

        if len(inps) == batch_size or idx == samples_per_epoch - 1:
            b_inp = torch.stack(inps).to(device)           # (B, 4, H, W)
            b_trho = torch.stack(targets_rho).to(device)   # (B, 1, H, W)
            b_tout = torch.stack(targets_out).to(device)   # (B, 1, H, W)
            b_curv = torch.stack(curvs).to(device)         # (B, 1, H, W)
            b_sparse = torch.stack(sparsities).to(device)  # (B, 1, H, W)

            optimizer.zero_grad()
            pred_rho_offset, pred_outlier_logits = model(b_inp)

            # Curvature-weighted L1 reconstruction loss (emphasize creases), plus
            # sparsity-weighted emphasis (emphasize cells the model has to mostly
            # extrapolate rather than measure directly -- this is where the
            # wall-radius collapse failure mode lives).
            weight = 1.0 + 3.0 * b_curv + 4.0 * b_sparse

            rho_loss = torch.mean(weight * torch.abs(pred_rho_offset - b_trho))
            bce_loss = criterion_bce(pred_outlier_logits, b_tout)

            loss = rho_loss + 0.5 * bce_loss
            loss.backward()
            optimizer.step()

            total_loss += loss.item() * len(inps)
            total_rho_loss += rho_loss.item() * len(inps)
            total_bce_loss += bce_loss.item() * len(inps)

            inps, targets_rho, targets_out, curvs, sparsities = [], [], [], [], []

    return (total_loss / samples_per_epoch,
            total_rho_loss / samples_per_epoch,
            total_bce_loss / samples_per_epoch)


def evaluate_model(model, n_eval=5, device="cpu", eval_dropout_rate=0.3):
    """Evaluate current model on held-out synthetic barrels.

    Uses a FIXED, realistic dropout rate (default 0.3, matching
    barrel_synth.generate_barrel()'s own production default) rather than the
    random training-time curriculum, so this is an honest held-out check and
    not circular with what train_epoch() is being scored on. Also reports the
    head/pole-row error separately from the wall-row error, since that split
    is exactly where learned mode's collapse was diagnosed (see
    notebooks/05_rules_vs_learned_volume_accuracy.ipynb) -- an overall RMS
    can look fine while the head-row error stays bad.
    """
    model.eval()
    gt_errors_mm = []
    head_errors_mm = []
    wall_errors_mm = []

    with torch.no_grad():
        for idx in range(n_eval):
            seed = 9999 + idx
            (inp, target_rho, target_out, curv, el_ctr, corners,
             cnt, dropout_used) = generate_train_sample(seed=seed, head_dropout_rate=eval_dropout_rate)
            inp_t = inp.unsqueeze(0).to(device)

            pred_offset, _ = model(inp_t)
            pred_offset_np = pred_offset.squeeze().cpu().numpy()

            # Convert offset back to metres
            pred_grid = 0.31 + pred_offset_np * 0.05
            gt_grid = 0.31 + target_rho * 0.05

            err_mm = np.sqrt(((pred_grid - gt_grid) ** 2).mean()) * 1000.0
            gt_errors_mm.append(err_mm)

            head_rows = (np.asarray(el_ctr) < corners[0]) | (np.asarray(el_ctr) > corners[1])
            if head_rows.any():
                head_errors_mm.append(np.sqrt(((pred_grid[head_rows] - gt_grid[head_rows]) ** 2).mean()) * 1000.0)
            if (~head_rows).any():
                wall_errors_mm.append(np.sqrt(((pred_grid[~head_rows] - gt_grid[~head_rows]) ** 2).mean()) * 1000.0)

    return {
        "overall_mm": float(np.mean(gt_errors_mm)),
        "head_mm": float(np.mean(head_errors_mm)) if head_errors_mm else float("nan"),
        "wall_mm": float(np.mean(wall_errors_mm)) if wall_errors_mm else float("nan"),
    }


def main():
    parser = argparse.ArgumentParser(description="Train GridUNet denoiser")
    parser.add_argument("--epochs", type=int, default=15)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--samples-per-epoch", type=int, default=20)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--output", type=str, default=DEFAULT_CHECKPOINT)
    parser.add_argument("--select-on", choices=["overall", "head"], default="head",
                        help="Which held-out metric to select the best checkpoint on. "
                             "'head' (default) targets the diagnosed collapse failure "
                             "mode directly instead of letting a good wall score hide it.")
    args = parser.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print("Training GridUNet on device: %s" % device)

    model = GridUNet(in_channels=4, base_channels=32).to(device)
    optimizer = optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    criterion_bce = nn.BCEWithLogitsLoss()

    best_eval_err = float("inf")
    print("\nStarting training for %d epochs..." % args.epochs)
    print("Epoch | Loss   | Rho L1 | BCE    | Val Overall (mm) | Val Head (mm) | Val Wall (mm) | Time")
    print("-" * 95)

    for epoch in range(1, args.epochs + 1):
        t0 = time.time()
        loss, rho_loss, bce_loss = train_epoch(
            model, optimizer, criterion_bce, batch_size=args.batch_size,
            samples_per_epoch=args.samples_per_epoch, device=device, epoch=epoch
        )
        t_elapsed = time.time() - t0

        val = evaluate_model(model, n_eval=3, device=device)
        select_metric = val["head_mm"] if args.select_on == "head" else val["overall_mm"]

        marker = ""
        if select_metric < best_eval_err:
            best_eval_err = select_metric
            torch.save(model.state_dict(), args.output)
            marker = " [saved]"

        print("%5d | %.4f | %.4f | %.4f | %17.3f | %13.3f | %13.3f | %.1fs%s" %
              (epoch, loss, rho_loss, bce_loss, val["overall_mm"], val["head_mm"], val["wall_mm"],
               t_elapsed, marker))

    print("\nTraining complete! Best validation %s RMS: %.3f mm" % (args.select_on, best_eval_err))
    print("Checkpoint saved to: %s" % args.output)


if __name__ == "__main__":
    main()
