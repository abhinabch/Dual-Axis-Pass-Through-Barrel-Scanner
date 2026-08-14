"""
Debug helper: capture current screen and test template matching for ready_text.png
Run from repository root:
    python debug_template_test.py

Creates a `debug/` folder with screenshots and any crops of found matches.
"""
import os
import time
import pyautogui

ROOT = os.path.abspath(os.path.dirname(__file__))
DEBUG_DIR = os.path.join(ROOT, "debug")
TEMPLATES_DIR = os.path.join(ROOT, "templates")
TEMPLATE_NAME = "ready_text.png"

os.makedirs(DEBUG_DIR, exist_ok=True)

# Save a full-screen screenshot
screenshot_path = os.path.join(DEBUG_DIR, "current_screen.png")
print("Saving full-screen screenshot to:", screenshot_path)
pyautogui.screenshot(screenshot_path)

# Find template files under templates
matches = []
for dirpath, dirnames, filenames in os.walk(TEMPLATES_DIR):
    if TEMPLATE_NAME in filenames:
        matches.append(os.path.join(dirpath, TEMPLATE_NAME))

if not matches:
    print("No ready_text.png templates found under templates/ — please confirm path.")
    raise SystemExit(1)

print(f"Found {len(matches)} template(s):")
for m in matches:
    print(" -", m)

# Try matching each template at several confidences and grayscale on/off
confidences = [0.95, 0.9, 0.85, 0.8, 0.75, 0.7]
for tpl in matches:
    print("\nTesting template:", tpl)
    for conf in confidences:
        for grayscale in (False, True):
            try:
                print(f"  Trying confidence={conf}, grayscale={grayscale} ...", end=" ")
                loc = pyautogui.locateOnScreen(tpl, confidence=conf, grayscale=grayscale)
                if loc:
                    print("FOUND at", loc)
                    # Save crop of matched area for inspection
                    im = pyautogui.screenshot(region=(loc.left, loc.top, loc.width, loc.height))
                    crop_path = os.path.join(DEBUG_DIR, f"match_{os.path.basename(tpl)}_c{int(conf*100)}_{'g' if grayscale else 'c'}.png")
                    im.save(crop_path)
                    print("    Saved crop ->", crop_path)
                else:
                    print("no match")
            except Exception as e:
                print("error:", e)

print("\nDone. Inspect files in the debug/ folder.")
