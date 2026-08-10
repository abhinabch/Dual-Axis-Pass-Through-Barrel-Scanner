# Learned Cleanup Model Promotion Rule & Versioning Criteria

**Purpose**: Define strict, repeatable quantitative conditions required before promoting a new ML model checkpoint (`models/*.pt`) or making `--cleanup learned` the default pipeline mode in `barrel_reconstruct.py`.

---

## 1. Quantitative Promotion Criteria

A candidate `--cleanup learned` model checkpoint is approved for promotion to **production default** if and only if all of the following conditions are met:

### Criterion 1: Accuracy Superiority
- The **Mean Absolute Percent Error (MAPE)** across the frozen validation dataset (`data/validation_set/validation_manifest.csv`, non-held-out subset) must be **statistically lower** than the current `--cleanup rules` baseline by at least **0.5% absolute**.
- **Statistical Significance**: The paired comparison test (Wilcoxon signed-rank or paired t-test) must yield $p < 0.05$.

### Criterion 2: No Gross Outlier Degenerations
- The maximum single-barrel volume error ($E_{\text{max}}$) for `--cleanup learned` across the validation set must not exceed **2.5 Litres** (or 1.2% volume error).
- **Regression Limit**: There must be **zero barrels** where `--cleanup learned` regresses by more than **1.0 Litres** compared to `--cleanup rules`.

### Criterion 3: Topology & Watertightness Guarantee
- **100% Watertightness**: 100% of reconstructed meshes produced by `--cleanup learned` must be 2-manifold closed meshes with 0 non-manifold edges (`watertight == True`).

### Criterion 4: Held-Out Confirmation
- Evaluation on the never-touched held-out test subset (`held_out == TRUE`) must confirm the accuracy gain without performance degradation vs the non-held-out validation set.

---

## 2. Checkpoint Versioning Protocol

Whenever a model checkpoint is promoted or updated:

1. **Model Checkpoint File**: Save the PyTorch `.pt` file under `models/` with timestamp tag, e.g. `grid_denoiser_v1_20260810.pt` and update symbolic link / default copy `grid_denoiser_best.pt`.
2. **Paper Trail CSV**: Save the corresponding `validation_summary.csv` and `validation_results.csv` into `models/checkpoints_history/YYYYMMDD_v1/`.
3. **Manifest Snapshot**: Record git commit hash of `data/validation_set/validation_manifest.csv` used for justification.

---

## 3. Automated CI & Pre-Merge Gate

Run `python reconstruction/check_regression.py` as part of CI before merging any PR modifying model architecture or reconstruction pipeline logic:
- Compares new pipeline output against `data/validation_set/baseline_summary.csv`.
- Fails with exit code 1 if mean absolute percent error regresses by > 0.1% or if watertightness drops.
