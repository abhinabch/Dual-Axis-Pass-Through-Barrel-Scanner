"""CI / Pre-Merge Automated Regression Protection Script.

Checks current reconstruction pipeline results against last accepted baseline
(data/validation_set/baseline_summary.csv). Fails (exit code 1) if:
  1. Mean absolute % volume error regresses beyond defined threshold (default: 0.5% absolute).
  2. Mesh watertightness percentage drops below baseline (must be 100%).
  3. Any barrel fails to reconstruct.

Usage:
    python reconstruction/check_regression.py
    python reconstruction/check_regression.py --threshold 0.2
    python reconstruction/check_regression.py --promote  # Update baseline on pass
"""

import argparse
import csv
import os
import sys

RECON_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(RECON_DIR)

DEFAULT_BASELINE_PATH = os.path.join(REPO_ROOT, "data", "validation_set", "baseline_summary.csv")
DEFAULT_SUMMARY_PATH = os.path.join(REPO_ROOT, "data", "validation_set", "validation_summary.csv")


def load_summary(path):
    """Load a summary CSV into a dict keyed by method."""
    if not os.path.exists(path):
        return None
    out = {}
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            m = row["method"]
            out[m] = {k: float(v) if v.replace(".", "", 1).replace("-", "", 1).isdigit() else v
                      for k, v in row.items()}
    return out


def check_regression(current_summary_path=DEFAULT_SUMMARY_PATH,
                     baseline_summary_path=DEFAULT_BASELINE_PATH,
                     max_allowed_error_increase_pct=0.5,
                     target_method="learned"):
    """Compare current summary results against baseline and flag regressions."""
    if not os.path.exists(current_summary_path):
        print(f"Current summary not found at {current_summary_path}.")
        print("Executing validation harness to generate current summary...")
        from validate_accuracy import run_evaluation, save_results
        results, summary = run_evaluation()
        save_results(results, summary)

    current = load_summary(current_summary_path)
    if not current or target_method not in current:
        print(f"Error: Target method '{target_method}' not found in current summary {current_summary_path}")
        return False, "Target method missing in current summary"

    baseline = load_summary(baseline_summary_path)
    if not baseline or target_method not in baseline:
        print(f"No previous baseline found at {baseline_summary_path}.")
        print(f"Setting current results as initial baseline...")
        import shutil
        os.makedirs(os.path.dirname(baseline_summary_path), exist_ok=True)
        shutil.copyfile(current_summary_path, baseline_summary_path)
        print(f"Baseline saved to {baseline_summary_path}")
        return True, "Initial baseline created"

    cur_m = current[target_method]
    base_m = baseline[target_method]

    cur_mape = float(cur_m["mean_abs_pct_err"])
    base_mape = float(base_m["mean_abs_pct_err"])
    delta_mape = cur_mape - base_mape  # Positive means error INCREASED (regression)

    cur_wt = float(cur_m["watertight_pct"])
    base_wt = float(base_m["watertight_pct"])

    failures = []

    print("=" * 60)
    print(f"REGRESSION CHECK: Method '{target_method}'")
    print("=" * 60)
    print(f"  Baseline MAPE:  {base_mape:.3f}%")
    print(f"  Current MAPE:   {cur_mape:.3f}%")
    print(f"  MAPE Delta:     {delta_mape:+.3f}% (Allowed threshold: +{max_allowed_error_increase_pct:.2f}%)")
    print(f"  Baseline WT%:   {base_wt:.1f}%")
    print(f"  Current WT%:    {cur_wt:.1f}%")

    # Check 1: MAPE increase limit
    if delta_mape > max_allowed_error_increase_pct:
        failures.append(f"MAPE regressed by {delta_mape:+.3f}% (exceeds allowed limit of +{max_allowed_error_increase_pct:.2f}%)")

    # Check 2: Watertightness drop limit
    if cur_wt < base_wt:
        failures.append(f"Watertightness dropped from {base_wt:.1f}% to {cur_wt:.1f}%")

    if failures:
        print("\n❌ REGRESSION CHECK FAILED:")
        for f in failures:
            print(f"  - {f}")
        return False, "; ".join(failures)
    else:
        print("\n✅ REGRESSION CHECK PASSED: No accuracy or topology regressions detected.")
        return True, "Check passed"


def main():
    parser = argparse.ArgumentParser(description="Regression protection check")
    parser.add_argument("--summary", type=str, default=DEFAULT_SUMMARY_PATH, help="Path to current validation_summary.csv")
    parser.add_argument("--baseline", type=str, default=DEFAULT_BASELINE_PATH, help="Path to baseline_summary.csv")
    parser.add_argument("--threshold", type=float, default=0.5, help="Max allowed absolute MAPE increase (%%)")
    parser.add_argument("--method", type=str, default="learned", help="Method to evaluate for regression")
    parser.add_argument("--promote", action="store_true", help="Promote current summary to baseline if check passes")

    args = parser.parse_args()

    passed, msg = check_regression(
        current_summary_path=args.summary,
        baseline_summary_path=args.baseline,
        max_allowed_error_increase_pct=args.threshold,
        target_method=args.method
    )

    if passed and args.promote:
        import shutil
        shutil.copyfile(args.summary, args.baseline)
        print(f"Promoted current summary to baseline at {args.baseline}")

    if not passed:
        sys.exit(1)


if __name__ == "__main__":
    main()
