"""
recapture_template.py

Regenerates a stale CrealityScan template PNG from a real screenshot.

Why this exists: CrealityAutomator (hardware/creality_autostart.py) matches
templates/<resolution>/*.png against the live screen via OpenCV template
matching. When the CrealityScan UI changes (new version, theme), the stored
template no longer matches anything and every click times out. On timeout,
CrealityAutomator now auto-saves a full-screen debug screenshot to debug/ --
this script turns one of those screenshots into a corrected template.

Usage:
    python tools/recapture_template.py <screenshot.png> <x> <y> <w> <h> <output_template.png>

<x> <y> <w> <h> describe a generous rectangular region around the button/banner
in the screenshot (eyeball it in an image viewer -- a bit of extra margin is
fine, the script tightens the crop automatically). Example:

    python tools/recapture_template.py debug/timeout_wait_for_and_click_Preview_123.png \
        140 85 60 45 templates/1280x800/preview_button.png

The script:
  1. Crops the given region.
  2. Auto-tightens the crop to just the non-background pixels (icon + label),
     using the crop's own top-left corner as the background color sample.
  3. Backs up any existing file at <output_template.png> into debug/ first.
  4. Saves the tight crop to <output_template.png>.
  5. Runs cv2.matchTemplate against the source screenshot and prints the
     confidence score -- a healthy capture scores ~0.99+. If it's much lower,
     the region bounds were probably off; re-run with adjusted x/y/w/h.

Requires opencv-python (already a project dependency).
"""
import sys
import os
import cv2
import numpy as np


def tighten_crop(roi, bg_sample_size=5, diff_threshold=30, pad=2):
    bg = roi[0:bg_sample_size, 0:bg_sample_size].reshape(-1, 3).mean(axis=0)
    diff = np.abs(roi.astype(int) - bg.astype(int)).sum(axis=2)
    mask = diff > diff_threshold
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return roi
    x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
    y0p, y1p = max(0, y0 - pad), min(roi.shape[0], y1 + pad + 1)
    x0p, x1p = max(0, x0 - pad), min(roi.shape[1], x1 + pad + 1)
    return roi[y0p:y1p, x0p:x1p]


def main():
    if len(sys.argv) != 7:
        print(__doc__)
        sys.exit(1)

    screenshot_path, x, y, w, h, out_path = sys.argv[1:]
    x, y, w, h = int(x), int(y), int(w), int(h)

    img = cv2.imread(screenshot_path)
    if img is None:
        print(f"Could not read screenshot: {screenshot_path}")
        sys.exit(1)

    roi = img[y:y + h, x:x + w]
    if roi.size == 0:
        print(f"Region ({x},{y},{w},{h}) is empty/out of bounds for a {img.shape[1]}x{img.shape[0]} screenshot.")
        sys.exit(1)

    tight = tighten_crop(roi)

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    debug_dir = os.path.join(repo_root, "debug")
    os.makedirs(debug_dir, exist_ok=True)

    if os.path.exists(out_path):
        old = cv2.imread(out_path)
        if old is not None:
            old_result = cv2.matchTemplate(img, old, cv2.TM_CCOEFF_NORMED)
            old_score = cv2.minMaxLoc(old_result)[1]
            print(f"Existing template's confidence against this screenshot: {old_score:.4f}")
        backup_name = f"{os.path.splitext(os.path.basename(out_path))[0]}_OLD_stale.png"
        backup_path = os.path.join(debug_dir, backup_name)
        if old is not None:
            cv2.imwrite(backup_path, old)
            print(f"Backed up existing template to {backup_path}")

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    if not cv2.imwrite(out_path, tight):
        print(f"Failed to write template to {out_path} -- check the path/permissions.")
        sys.exit(1)
    print(f"Saved new template to {out_path} (size {tight.shape[1]}x{tight.shape[0]})")

    result = cv2.matchTemplate(img, tight, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    print(f"New template self-match confidence: {max_val:.4f} at {max_loc}")
    if max_val < 0.98:
        print("WARNING: confidence lower than expected -- open the crop and the region "
              "bounds you passed and check for occlusion or a bad bounding box.")


if __name__ == "__main__":
    main()
