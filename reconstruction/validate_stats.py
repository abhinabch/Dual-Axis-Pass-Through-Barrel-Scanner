"""Statistical Comparison Module for Barrel Reconstruction Validation.

Performs rigorous paired statistical significance tests and effect size estimations:
  - Paired absolute % error differences (learned vs rules, learned vs legacy)
  - Normality assessment (Shapiro-Wilk test)
  - Wilcoxon signed-rank test / Paired t-test
  - Effect size (Cohen's d and Hodges-Lehmann median difference) with 95% CI
  - Regression identification (flagging barrels where learned is worse than rules)

Usage:
    python reconstruction/validate_stats.py
    python reconstruction/validate_stats.py --results data/validation_set/validation_results.csv
"""

import argparse
import csv
import os
import sys
import numpy as np
from scipy import stats

RECON_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(RECON_DIR)

DEFAULT_RESULTS_CSV = os.path.join(REPO_ROOT, "data", "validation_set", "validation_results.csv")
DEFAULT_STATS_OUTPUT = os.path.join(REPO_ROOT, "data", "validation_set", "validation_stats.csv")


def load_results(csv_path=DEFAULT_RESULTS_CSV):
    """Load detailed validation results CSV."""
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"Results CSV not found at {csv_path}")

    results = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cleaned = {k.strip(): v.strip() for k, v in row.items() if k}
            results.append(cleaned)
    return results


def bootstrap_ci(arr, func=np.mean, n_boot=2000, ci=95, seed=42):
    """Compute bootstrap confidence interval for a statistic."""
    if len(arr) == 0:
        return np.nan, np.nan
    rng = np.random.default_rng(seed)
    boots = [func(rng.choice(arr, size=len(arr), replace=True)) for _ in range(n_boot)]
    low = np.percentile(boots, (100 - ci) / 2.0)
    high = np.percentile(boots, 100 - (100 - ci) / 2.0)
    return float(low), float(high)


def paired_compare(results, method_a="learned", method_b="rules"):
    """Perform paired statistical comparison between two methods."""
    # Organize by barrel_id
    by_barrel = {}
    for r in results:
        bid = r["barrel_id"]
        m = r["method"]
        st = r["status"]
        if st != "OK":
            continue
        err = r.get("vol_abs_pct_err")
        if err is not None and err != "" and err != "nan":
            by_barrel.setdefault(bid, {})[m] = float(err)

    paired_a = []
    paired_b = []
    barrel_ids = []

    for bid, mdict in by_barrel.items():
        if method_a in mdict and method_b in mdict:
            paired_a.append(mdict[method_a])
            paired_b.append(mdict[method_b])
            barrel_ids.append(bid)

    n_pairs = len(paired_a)
    if n_pairs < 2:
        return {
            "comparison": f"{method_a} vs {method_b}",
            "n_pairs": n_pairs,
            "status": "INSUFFICIENT_DATA",
            "msg": "Fewer than 2 paired observations available."
        }

    a_arr = np.array(paired_a)
    b_arr = np.array(paired_b)
    diff = a_arr - b_arr  # negative means method_a (learned) has LOWER error (improvement)

    # Normality test on paired differences
    if n_pairs >= 3:
        shapiro_stat, shapiro_p = stats.shapiro(diff)
        is_normal = bool(shapiro_p > 0.05)
    else:
        shapiro_stat, shapiro_p = np.nan, np.nan
        is_normal = False

    # Significance test
    if is_normal:
        test_type = "Paired t-test"
        t_stat, p_val = stats.ttest_rel(a_arr, b_arr)
    else:
        test_type = "Wilcoxon signed-rank"
        try:
            t_stat, p_val = stats.wilcoxon(a_arr, b_arr)
        except Exception:
            t_stat, p_val = np.nan, 1.0

    # Effect size metrics
    mean_diff = float(np.mean(diff))
    median_diff = float(np.median(diff))
    mean_ci = bootstrap_ci(diff, np.mean)
    median_ci = bootstrap_ci(diff, np.median)

    # Cohen's d (paired)
    std_diff = np.std(diff, ddof=1) if len(diff) > 1 else 1e-6
    cohen_d = float(mean_diff / (std_diff + 1e-12))

    # Regressions: count barrels where method_a error > method_b error
    regressions = []
    for bid, val_a, val_b in zip(barrel_ids, a_arr, b_arr):
        if val_a > val_b + 0.01:  # 0.01% threshold for practical regression
            regressions.append({
                "barrel_id": bid,
                f"{method_a}_err_pct": val_a,
                f"{method_b}_err_pct": val_b,
                "delta_pct": val_a - val_b
            })

    return {
        "comparison": f"{method_a} vs {method_b}",
        "n_pairs": n_pairs,
        "mean_a_err_pct": float(np.mean(a_arr)),
        "mean_b_err_pct": float(np.mean(b_arr)),
        "mean_diff_pct": mean_diff,
        "mean_diff_ci_95": mean_ci,
        "median_diff_pct": median_diff,
        "median_diff_ci_95": median_ci,
        "cohen_d": cohen_d,
        "is_normal": is_normal,
        "test_type": test_type,
        "stat_val": float(t_stat) if np.isfinite(t_stat) else np.nan,
        "p_value": float(p_val) if np.isfinite(p_val) else np.nan,
        "significant_005": bool(p_val < 0.05),
        "n_regressions": len(regressions),
        "regressions": regressions
    }


def run_statistical_analysis(csv_path=DEFAULT_RESULTS_CSV):
    """Run statistical analysis on validation results CSV."""
    results = load_results(csv_path)

    pairs_to_test = [
        ("learned", "rules"),
        ("learned", "legacy"),
        ("rules", "legacy"),
    ]

    out_stats = []
    print("=" * 60)
    print("PAIRED STATISTICAL COMPARISON REPORT")
    print("=" * 60)

    for mA, mB in pairs_to_test:
        comp = paired_compare(results, mA, mB)
        out_stats.append(comp)

        print(f"\nComparison: {comp['comparison']} (N={comp.get('n_pairs', 0)})")
        if comp.get("status") == "INSUFFICIENT_DATA":
            print(f"  Status: {comp['msg']}")
            continue

        print(f"  Mean Error:   {mA}={comp['mean_a_err_pct']:.2f}%, {mB}={comp['mean_b_err_pct']:.2f}%")
        print(f"  Mean Delta:   {comp['mean_diff_pct']:+.2f}% (95% CI: [{comp['mean_diff_ci_95'][0]:+.2f}%, {comp['mean_diff_ci_95'][1]:+.2f}%])")
        print(f"  Median Delta: {comp['median_diff_pct']:+.2f}% (95% CI: [{comp['median_diff_ci_95'][0]:+.2f}%, {comp['median_diff_ci_95'][1]:+.2f}%])")
        print(f"  Cohen's d:    {comp['cohen_d']:.3f}")
        print(f"  Test:         {comp['test_type']} (stat={comp['stat_val']:.3f}, p={comp['p_value']:.4f})")
        print(f"  Significant:  {'YES (p < 0.05)' if comp['significant_005'] else 'NO (p >= 0.05)'}")
        print(f"  Regressions:  {comp['n_regressions']} barrel(s) where {mA} performed worse than {mB}")

        if comp['regressions']:
            for reg in comp['regressions']:
                print(f"    - Barrel {reg['barrel_id']}: {mA}={reg[f'{mA}_err_pct']:.2f}%, {mB}={reg[f'{mB}_err_pct']:.2f}% (Δ={reg['delta_pct']:+.2f}%)")

    return out_stats


def main():
    parser = argparse.ArgumentParser(description="Statistical comparison harness")
    parser.add_argument("--results", type=str, default=DEFAULT_RESULTS_CSV, help="Path to validation_results.csv")
    args = parser.parse_args()

    try:
        run_statistical_analysis(args.results)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        print("Run `python reconstruction/validate_accuracy.py` first to generate validation results.")


if __name__ == "__main__":
    main()
