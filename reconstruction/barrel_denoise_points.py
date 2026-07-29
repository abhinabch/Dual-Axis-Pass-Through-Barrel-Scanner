"""Point-level outlier classifier (pre-binning) using PointNet.

Operates directly on raw point cloud (P, N) before build_rho_grid to flag stray
floaters and bung interior points so they don't corrupt the per-cell robust median.

Features per point (7):
  0,1,2: local relative position (P - kNN_mean)
  3,4,5: unit normal (N)
  6: local PCA curvature score from barrel_features

Usage:
    python barrel_denoise_points.py --test
"""
import os
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np

DEFAULT_POINT_CHECKPOINT = os.path.join(
    os.path.dirname(__file__), "..", "models", "point_classifier_best.pt"
)


# ── PointNet Architecture ──────────────────────────────────────────────────────

class PointNetClassifier(nn.Module):
    """Lightweight PointNet per-point classification model."""

    def __init__(self, in_channels=7, hidden_dim=64):
        super().__init__()
        # Per-point feature extraction
        self.conv1 = nn.Conv1d(in_channels, hidden_dim, 1)
        self.conv2 = nn.Conv1d(hidden_dim, hidden_dim * 2, 1)
        self.conv3 = nn.Conv1d(hidden_dim * 2, hidden_dim * 4, 1)

        self.bn1 = nn.BatchNorm1d(hidden_dim)
        self.bn2 = nn.BatchNorm1d(hidden_dim * 2)
        self.bn3 = nn.BatchNorm1d(hidden_dim * 4)

        # Global feature aggregation + per-point classification head
        self.conv4 = nn.Conv1d(hidden_dim * 8, hidden_dim * 2, 1)
        self.conv5 = nn.Conv1d(hidden_dim * 2, hidden_dim, 1)
        self.conv6 = nn.Conv1d(hidden_dim, 1, 1)

        self.bn4 = nn.BatchNorm1d(hidden_dim * 2)
        self.bn5 = nn.BatchNorm1d(hidden_dim)

    def forward(self, x):
        # x shape: (B, in_channels, N_pts)
        B, C, N = x.shape

        h1 = F.leaky_relu(self.bn1(self.conv1(x)), 0.2)
        h2 = F.leaky_relu(self.bn2(self.conv2(h1)), 0.2)
        h3 = F.leaky_relu(self.bn3(self.conv3(h2)), 0.2)  # (B, 256, N)

        # Global feature via max pooling across points
        global_feat = torch.max(h3, dim=2, keepdim=True)[0]  # (B, 256, 1)
        global_feat_expanded = global_feat.repeat(1, 1, N)    # (B, 256, N)

        # Concatenate local + global features
        combined = torch.cat([h3, global_feat_expanded], dim=1)  # (B, 512, N)

        h4 = F.leaky_relu(self.bn4(self.conv4(combined)), 0.2)
        h5 = F.leaky_relu(self.bn5(self.conv5(h4)), 0.2)
        logits = self.conv6(h5).squeeze(1)  # (B, N)

        return logits


# ── Inference & Pre-Binning Filtering ──────────────────────────────────────────

def filter_points_pre_binning(P, N, pt_curv=None, threshold=0.5,
                              model=None, checkpoint_path=DEFAULT_POINT_CHECKPOINT,
                              device="cpu"):
    """Filter raw point cloud P, N before build_rho_grid runs.

    Parameters
    ----------
    P : ndarray (N_pts, 3) — point positions
    N : ndarray (N_pts, 3) — point normals
    pt_curv : ndarray (N_pts,) optional — per-point curvature
    threshold : float — probability cutoff for dropping points

    Returns
    -------
    keep_mask : ndarray (N_pts,) bool — True for valid points to keep
    probs : ndarray (N_pts,) float — predicted outlier probability per point
    """
    from barrel_features import compute_point_curvature

    N_total = len(P)
    if N_total == 0:
        return np.zeros(0, dtype=bool), np.zeros(0)

    if pt_curv is None:
        pt_curv = compute_point_curvature(P, k=20)

    # Load model if not passed
    if model is None:
        model = PointNetClassifier(in_channels=7, hidden_dim=64)
        if os.path.exists(checkpoint_path):
            state = torch.load(checkpoint_path, map_location=device, weights_only=True)
            model.load_state_dict(state)
        model.to(device)
        model.eval()

    # Process in spatial chunks of ~4096 points for memory tractability
    chunk_size = 4096
    probs = np.zeros(N_total, dtype=np.float32)

    with torch.no_grad():
        for start in range(0, N_total, chunk_size):
            end = min(start + chunk_size, N_total)
            P_chunk = P[start:end]
            N_chunk = N[start:end]
            c_chunk = pt_curv[start:end]

            # Local centering
            center = P_chunk.mean(axis=0, keepdims=True)
            P_rel = P_chunk - center

            # Construct 7-channel input: [x_rel, y_rel, z_rel, nx, ny, nz, curv]
            feat = np.column_stack([P_rel, N_chunk, c_chunk]).T.astype(np.float32)  # (7, Chunk)
            inp_t = torch.from_numpy(feat).unsqueeze(0).to(device)  # (1, 7, Chunk)

            logits = model(inp_t)
            prob_chunk = torch.sigmoid(logits).squeeze(0).cpu().numpy()
            probs[start:end] = prob_chunk

    keep_mask = probs < threshold
    return keep_mask, probs


def _self_test():
    """Test point-level classifier module."""
    print("=" * 60)
    print("barrel_denoise_points self-test")
    print("=" * 60)

    from barrel_synth import generate_barrel

    b = generate_barrel(seed=42, n_points=10_000, add_bung=True, add_floaters=True)
    P, N = b["P_noisy"], b["N_noisy"]
    labels = b["labels"]

    print("Running pre-binning point filter on %d points..." % len(P))
    keep_mask, probs = filter_points_pre_binning(P, N)

    n_dropped = int((~keep_mask).sum())
    print("Dropped %d points (%.2f%%)" % (n_dropped, 100 * n_dropped / len(P)))

    floater_dropped = int((~keep_mask & (labels == 2)).sum())
    floater_total = int((labels == 2).sum())
    print("Floater detection: %d / %d floaters dropped" % (floater_dropped, floater_total))

    clean_dropped = int((~keep_mask & (labels == 0)).sum())
    clean_total = int((labels == 0).sum())
    print("Clean false positive drop: %d / %d clean points (%.2f%%)" %
          (clean_dropped, clean_total, 100 * clean_dropped / (clean_total + 1e-8)))

    print("Self-test passed!")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Point-level outlier classifier")
    parser.add_argument("--test", action="store_true", help="Run self-test")
    args = parser.parse_args()

    if args.test:
        _self_test()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
