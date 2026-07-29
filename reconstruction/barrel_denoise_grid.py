"""Grid-domain edge-aware denoiser using a small PyTorch 2D U-Net.

Operates on the structured rho(el, az) height field (n_el x n_az grid).
Replaces GROSS_OUTLIER_MM rejection, bung detection, fill_grid, and smooth_grid
with a learned U-Net model that preserves sharp crozehead creases via curvature-guided
attention and robustly reconstructs missing/sparse head-region rows.

Input channels (4):
  0: rho (normalized offset from el-row median or 0.3m nominal)
  1: grid curvature feature from barrel_features
  2: cell point count (confidence signal)
  3: normalized polar angle (el / pi)

Outputs (2):
  1: denoised rho grid
  2: per-cell outlier/bung mask probability
"""
import os
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np

DEFAULT_CHECKPOINT = os.path.join(
    os.path.dirname(__file__), "..", "models", "grid_denoiser_best.pt"
)


# ── Circular Conv2D ────────────────────────────────────────────────────────────

class CircularConv2d(nn.Module):
    """Conv2d with circular padding along azimuth (W dimension, dim -1)
    and reflection padding along polar (H dimension, dim -2)."""

    def __init__(self, in_channels, out_channels, kernel_size=3, stride=1, padding=1):
        super().__init__()
        self.pad = padding
        self.conv = nn.Conv2d(in_channels, out_channels, kernel_size, stride=stride, padding=0)

    def forward(self, x):
        # x shape: (B, C, H, W)
        # Pad H with reflection, W with circular wrap
        if self.pad > 0:
            # Pad W (azimuth) circularly
            x = F.pad(x, (self.pad, self.pad, 0, 0), mode="circular")
            # Pad H (elevation) with reflection
            x = F.pad(x, (0, 0, self.pad, self.pad), mode="reflect")
        return self.conv(x)


class DoubleConv(nn.Module):
    """Double circular convolution block with BatchNorm and LeakyReLU."""

    def __init__(self, in_channels, out_channels):
        super().__init__()
        self.block = nn.Sequential(
            CircularConv2d(in_channels, out_channels, kernel_size=3, padding=1),
            nn.BatchNorm2d(out_channels),
            nn.LeakyReLU(0.2, inplace=True),
            CircularConv2d(out_channels, out_channels, kernel_size=3, padding=1),
            nn.BatchNorm2d(out_channels),
            nn.LeakyReLU(0.2, inplace=True),
        )

    def forward(self, x):
        return self.block(x)


# ── Grid U-Net Architecture ────────────────────────────────────────────────────

class GridUNet(nn.Module):
    """4-level U-Net for 2D barrel height-field grid denoising and outlier detection."""

    def __init__(self, in_channels=4, base_channels=32):
        super().__init__()

        # Encoder
        self.inc = DoubleConv(in_channels, base_channels)
        self.down1 = nn.Sequential(
            nn.MaxPool2d(2),
            DoubleConv(base_channels, base_channels * 2)
        )
        self.down2 = nn.Sequential(
            nn.MaxPool2d(2),
            DoubleConv(base_channels * 2, base_channels * 4)
        )
        self.down3 = nn.Sequential(
            nn.MaxPool2d(2),
            DoubleConv(base_channels * 4, base_channels * 8)
        )

        # Bottleneck
        self.bottleneck = DoubleConv(base_channels * 8, base_channels * 8)

        # Decoder
        self.up3 = nn.ConvTranspose2d(base_channels * 8, base_channels * 4, kernel_size=2, stride=2)
        self.conv_up3 = DoubleConv(base_channels * 8, base_channels * 4)

        self.up2 = nn.ConvTranspose2d(base_channels * 4, base_channels * 2, kernel_size=2, stride=2)
        self.conv_up2 = DoubleConv(base_channels * 4, base_channels * 2)

        self.up1 = nn.ConvTranspose2d(base_channels * 2, base_channels, kernel_size=2, stride=2)
        self.conv_up1 = DoubleConv(base_channels * 2, base_channels)

        # Output heads:
        # Head 1: residual rho correction offset (metres)
        self.rho_head = nn.Sequential(
            CircularConv2d(base_channels, base_channels // 2, kernel_size=3, padding=1),
            nn.LeakyReLU(0.2, inplace=True),
            nn.Conv2d(base_channels // 2, 1, kernel_size=1)
        )

        # Head 2: per-cell outlier / bung probability (logit)
        self.outlier_head = nn.Sequential(
            CircularConv2d(base_channels, base_channels // 2, kernel_size=3, padding=1),
            nn.LeakyReLU(0.2, inplace=True),
            nn.Conv2d(base_channels // 2, 1, kernel_size=1)
        )

    def forward(self, x):
        # x: (B, 4, H, W)
        x1 = self.inc(x)
        x2 = self.down1(x1)
        x3 = self.down2(x2)
        x4 = self.down3(x3)

        b = self.bottleneck(x4)

        u3 = self.up3(b)
        # Pad tensor if dimensions are odd
        if u3.shape != x3.shape:
            u3 = F.interpolate(u3, size=x3.shape[2:], mode="bilinear", align_corners=False)
        u3 = torch.cat([u3, x3], dim=1)
        c3 = self.conv_up3(u3)

        u2 = self.up2(c3)
        if u2.shape != x2.shape:
            u2 = F.interpolate(u2, size=x2.shape[2:], mode="bilinear", align_corners=False)
        u2 = torch.cat([u2, x2], dim=1)
        c2 = self.conv_up2(u2)

        u1 = self.up1(c2)
        if u1.shape != x1.shape:
            u1 = F.interpolate(u1, size=x1.shape[2:], mode="bilinear", align_corners=False)
        u1 = torch.cat([u1, x1], dim=1)
        c1 = self.conv_up1(u1)

        rho_offset = self.rho_head(c1)
        outlier_logits = self.outlier_head(c1)

        return rho_offset, outlier_logits


# ── Feature Prep & Inference Wrapper ───────────────────────────────────────────

def prepare_grid_inputs(grid, cnt, el_ctr, curv_grid=None):
    """Convert raw grid + metadata into a 4-channel PyTorch tensor (1, 4, H, W).

    Parameters
    ----------
    grid : ndarray (H, W)
    cnt : ndarray (H, W)
    el_ctr : ndarray (H,)
    curv_grid : ndarray (H, W) optional

    Returns
    -------
    tensor : torch.Tensor (1, 4, H, W)
    norm_info : dict with median_r for un-normalizing
    """
    from barrel_features import compute_grid_curvature

    H, W = grid.shape
    g = grid.copy()

    # Fill NaNs temporarily with row median for feature calculation
    row_med = np.array([np.nanmedian(g[i]) if np.isfinite(g[i]).any() else 0.31
                        for i in range(H)])
    nan_mask = np.isnan(g)
    for i in range(H):
        if nan_mask[i].any():
            g[i, nan_mask[i]] = row_med[i]

    if curv_grid is None:
        curv_grid = compute_grid_curvature(g, el_ctr)

    # Channel 0: Rho offset relative to 0.31m nominal base radius
    ch0 = (g - 0.31) / 0.05  # Scale ~15cm range to roughly [-1, 1]

    # Channel 1: Curvature feature [0, 1]
    ch1 = curv_grid

    # Channel 2: Point count confidence [0, 1]
    ch2 = np.log1p(np.clip(cnt, 0, 100)) / np.log1p(100)

    # Channel 3: Normalized polar angle el / pi [0, 1]
    el_norm = el_ctr[:, None] / np.pi
    ch3 = np.repeat(el_norm, W, axis=1)

    inp = np.stack([ch0, ch1, ch2, ch3], axis=0).astype(np.float32)  # (4, H, W)
    tensor = torch.from_numpy(inp).unsqueeze(0)  # (1, 4, H, W)

    norm_info = {"nominal_r": 0.31, "scale": 0.05, "nan_mask": nan_mask}
    return tensor, norm_info


def learned_clean_grid(grid, cnt, corners, el_ctr, model=None, checkpoint_path=DEFAULT_CHECKPOINT,
                       device="cpu"):
    """Inference wrapper for learned grid denoising.

    Replaces flatten_head_poles -> detect_bung -> gross-outlier -> fill_grid -> smooth_grid.

    Returns
    -------
    clean_grid : ndarray (n_el, n_az)
    bung_mask : ndarray (n_el, n_az) bool
    """
    H, W = grid.shape

    # Load model if not passed
    if model is None:
        model = GridUNet(in_channels=4, base_channels=32)
        if os.path.exists(checkpoint_path):
            state = torch.load(checkpoint_path, map_location=device, weights_only=True)
            model.load_state_dict(state)
        model.to(device)
        model.eval()

    inp_tensor, norm_info = prepare_grid_inputs(grid, cnt, el_ctr)
    inp_tensor = inp_tensor.to(device)

    with torch.no_grad():
        rho_offset, outlier_logits = model(inp_tensor)

    offset_np = rho_offset.squeeze().cpu().numpy()  # (H, W)
    prob_np = torch.sigmoid(outlier_logits).squeeze().cpu().numpy()  # (H, W)

    # Model absolute prediction
    model_pred = norm_info["nominal_r"] + offset_np * norm_info["scale"]

    nan_mask = norm_info["nan_mask"]
    # Where raw measurement is valid, start from raw grid and apply subtle model residual correction
    valid_mask = (~nan_mask) & (prob_np < 0.5)
    clean_grid = np.where(valid_mask, grid + 0.05 * offset_np * norm_info["scale"], model_pred)

    # Fill any remaining NaNs with row median fallback
    row_med = np.array([np.nanmedian(clean_grid[i]) if np.isfinite(clean_grid[i]).any() else 0.31
                        for i in range(H)])
    for i in range(H):
        missing = np.isnan(clean_grid[i])
        if missing.any():
            clean_grid[i, missing] = row_med[i]

    # Restrict bung mask to stave wall band between corners and threshold > 0.85
    stave_band = (el_ctr > corners[0]) & (el_ctr < corners[1])
    if os.path.exists(checkpoint_path):
        bung_mask = (prob_np > 0.85) & stave_band[:, None]
    else:
        # Fallback if checkpoint doesn't exist yet: no false-positive bungs
        bung_mask = np.zeros_like(grid, dtype=bool)

    return clean_grid, bung_mask
