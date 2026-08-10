# Frozen Validation Dataset (`data/validation_set/`)

This directory contains the frozen, versioned validation dataset for evaluating barrel reconstruction accuracy against physical ground truth.

## Directory Structure

```
data/validation_set/
├── scans/                    # Raw .obscan scan files (place files here)
├── validation_manifest.csv   # Frozen manifest listing all barrels and ground truth numbers
├── baseline_summary.csv      # Baseline summary scores for regression protection (Phase 5)
├── outputs/                  # Generated mesh outputs from validation runs
└── README.md                 # This guide
```

## Dataset Rules

1. **Ground Truth Data**: `gt_volume_L` comes from water-fill mass measurements, caliper profile analytic models, or certified cooper nominal capacity per `docs/GROUND_TRUTH_PROTOCOL.md`.
2. **Paired Comparisons**: Every `.obscan` file in `scans/` is run through all candidate cleanup methods (`legacy`, `rules`, `learned`).
3. **Frozen Manifest**: Once evaluation begins, barrels must not be selectively added or dropped from `validation_manifest.csv`.
4. **Held-Out Test Set**: Barrels flagged with `held_out = TRUE` in the manifest are reserved exclusively for Phase 6 final confirmation and must not be used during development or model hyperparameter tuning.

## How to Add Barrels

1. Place the `.obscan` file into `data/validation_set/scans/`.
2. Add a corresponding row to `validation_manifest.csv` with all required ground-truth measurements and metadata.
3. Commit the updated manifest to git version control.
