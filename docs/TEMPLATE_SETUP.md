# Capturing Button Templates & Multi-Resolution Setup

The `CrealityAutomator` matches against small cropped screenshots of buttons and UI indicators.

## Template Directory Structure & Multi-Resolution Support

To support multiple resolutions or Windows display scaling settings across different environments:
- Root `templates/` folder (default fallback)
- Optional resolution subfolders (e.g. `templates/1920x1080/`, `templates/2560x1440/`, `templates/4k/`)

The automator automatically tests candidate folders against the active screen to select the matching template set.

### Required Template Images

Place the following files in `templates/` or your resolution subfolder:

```
templates/
  preview_button.png      # Preview / Setup button on Scan Settings screen
  ready_text.png          # "Please click [Start] to scan" banner text
  start_button.png        # Start scan button
  stop_button.png         # Stop scan button
  export_button.png       # Process / Export / Save button
  warning_ok_button.png   # OK button on warning/confirmation dialogs (optional)
```

## How to Capture Templates

1. Open **Creality Scan** on the target machine.
2. Use Windows Snipping Tool (`Win + Shift + S`) to crop each element:
   - **Preview Button**: Crop the icon + "Preview" text label with slight padding. Save as `preview_button.png`.
   - **Ready Banner**: Crop the "Please click [Start] to scan" banner text. Save as `ready_text.png`.
   - **Start Button**: Crop the "Start" button icon + label. Save as `start_button.png`.
   - **Stop Button**: Crop the "Stop" button. Save as `stop_button.png`.
   - **Export / Save Button**: Crop the "Export" or "Save" button. Save as `export_button.png`.
   - **Warning OK Button**: Crop the "OK" button on popups. Save as `warning_ok_button.png`.

## Crop Guidelines

- **Balanced padding**: Include icon + label, but avoid surrounding background windows or toolbar edges.
- **Display Scaling**: Capture at 100% or 125% scaling matching your production machine. If running at different resolutions, save crops in resolution-named folders (e.g. `templates/1920x1080/`).

## Confidence Tuning

In `CrealityAutomator(confidence=0.85)`:
- If a visible button times out: lower confidence in steps (e.g. `0.80`, `0.75`).
- If false matches occur: raise confidence (e.g. `0.90`).

## Quick Test Script

Run this snippet in Python with Creality Scan open:

```python
from hardware.creality_autostart import CrealityAutomator

automator = CrealityAutomator()
print("Window available:", automator.is_window_available())
print("Detected resolution set:", automator.detect_resolution())
```
