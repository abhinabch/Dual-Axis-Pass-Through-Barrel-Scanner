# Capturing the button templates

The script matches against small cropped screenshots of the buttons, so you
need to create two image files before running it:

```
templates/preview_button.png
templates/start_button.png
```

## How to capture them

1. Create a `templates` folder next to `creality_autostart.py`.
2. Open Creality Scan and get it to the **first** screen from your
   screenshots — the one with the "Scan Settings" panel and the big
   "Preview" icon in the top toolbar.
3. Use the Windows Snipping Tool (Win+Shift+S) to crop **just the Preview
   button** — the icon plus its "Preview" label, with a little padding but
   not the whole toolbar. Save it as `templates/preview_button.png`.
4. Click through to the **second** screen (where the toolbar now shows
   "Start" instead of "Preview"). Crop just that button the same way and
   save as `templates/start_button.png`.

## Why the crop matters

- Too tight (just the icon, no label) → more false matches elsewhere.
- Too loose (includes surrounding toolbar) → matching fails if the window
  is resized, since the surrounding pixels shift but the button doesn't.
- Capture at the same display scaling/resolution you'll run the script at.
  If you ever change monitor resolution or Windows display scaling,
  recapture both templates.

## Tuning MATCH_CONFIDENCE

`MATCH_CONFIDENCE = 0.85` in the script is a starting point.

- If the script times out even though the button is clearly visible,
  lower it in small steps (0.80, 0.75, ...).
- If it ever clicks the wrong thing, raise it instead.

## Quick test before trusting it unattended

Run this in a Python shell with Creality Scan open on the Preview screen:

```python
import pyautogui
print(pyautogui.locateCenterOnScreen("templates/preview_button.png", confidence=0.85))
```

If that prints a coordinate, matching works. If it prints `None`, the
crop or confidence needs adjusting before the full script will work.
