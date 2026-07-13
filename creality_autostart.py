"""
creality_autostart.py

Automates the two manual clicks in Creality Scan (Preview/Setup -> Start)
using image matching, since the app's buttons are custom-drawn (Tier B:
not visible to the Windows accessibility layer).

REQUIREMENTS (run once, on the Windows machine that runs Creality Scan):
    pip install pyautogui opencv-python pygetwindow pillow

BEFORE YOU RUN THIS: capture the button template images (see "TEMPLATE
SETUP" below). The script cannot work without them.

WHAT IT DOES
    1. Finds and focuses the CrealityScan window.
    2. Locates and clicks the "Preview" button (Scan Settings screen).
    3. Polls the screen for the "Please click [Start] to scan" banner -
       the app's own readiness signal - instead of guessing a fixed delay
       or relying on the Start button icon alone (which looks nearly
       identical to the Preview icon and can false-match mid-transition).
    4. Clicks "Start".
    5. Logs each step so you can see exactly where it succeeded or failed.

WHAT IT DELIBERATELY DOES NOT DO YET (per the brief)
    - Stopping the scan / saving the STL / moving the file - later step.
    - Coordinating with Node-RED for pan/tilt motion - later step.
    - Filling in Setup dialog fields - add if your workflow needs it once
      you confirm Setup requires no manual input.
"""

import sys
import time
import logging

import pyautogui
import pygetwindow as gw

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("creality_autostart")

# ---------------------------------------------------------------------------
# CONFIG - tune these for your machine
# ---------------------------------------------------------------------------

WINDOW_TITLE_SUBSTRING = "CrealityScan"   # matches the title bar text

# Template images - see "TEMPLATE SETUP" below for how to create these.
TEMPLATE_PREVIEW_BUTTON = "templates/preview_button.png"
TEMPLATE_START_BUTTON = "templates/start_button.png"

# "Please click [Start] to scan" - the banner text that appears top-center
# once the app is actually ready. This is a much more reliable readiness
# signal than the Start button icon, since the icon is a cyan play-triangle
# that looks nearly identical to the Preview icon (same shape, same toolbar
# slot) and can false-match mid-transition. This text string only appears
# in one state.
TEMPLATE_READY_TEXT = "templates/ready_text.png"

# How sure a match needs to be (0-1). Lower this if a real match isn't
# being found; raise it if it's clicking the wrong thing. Requires
# opencv-python to be installed for the `confidence` parameter to work.
MATCH_CONFIDENCE = 0.85

# How long to wait for each UI transition before giving up.
CLICK_TIMEOUT_SECONDS = 20
POLL_INTERVAL_SECONDS = 0.5

# Safety: moving the mouse to a screen corner aborts the script.
pyautogui.FAILSAFE = True


class AutomationError(RuntimeError):
    """Raised when a required UI element can't be found in time."""


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def focus_creality_window() -> None:
    """Find the Creality Scan window and bring it to the foreground.

    Image matching is done against the whole screen, so if the window is
    minimized, behind another window, or off-screen, matching will fail
    (or worse, match a stale image cached from before). Focusing first
    removes an entire class of flaky failures.
    """
    matches = [w for w in gw.getAllTitles() if WINDOW_TITLE_SUBSTRING.lower() in w.lower()]
    if not matches:
        raise AutomationError(
            f"No window found with title containing '{WINDOW_TITLE_SUBSTRING}'. "
            "Is Creality Scan running?"
        )

    win = gw.getWindowsWithTitle(matches[0])[0]
    if win.isMinimized:
        win.restore()
    win.activate()
    time.sleep(0.5)  # brief settle time after window activation, not a UI-state wait
    log.info("Focused window: %s", win.title)


def wait_for_and_click(template_path: str, label: str, timeout: float = CLICK_TIMEOUT_SECONDS):
    """Poll the screen for a button image and click its center as soon as it appears.

    This replaces a fixed sleep with an actual state check, per the
    reconnaissance notes: Start only becomes clickable once Setup/Preview
    has finished processing, and that duration isn't fixed.
    """
    log.info("Waiting for '%s' button (timeout %.0fs)...", label, timeout)
    deadline = time.time() + timeout

    while time.time() < deadline:
        location = pyautogui.locateCenterOnScreen(
            template_path, confidence=MATCH_CONFIDENCE
        )
        if location is not None:
            pyautogui.moveTo(location, duration=0.2)
            pyautogui.click()
            log.info("Clicked '%s' at %s", label, location)
            return location
        time.sleep(POLL_INTERVAL_SECONDS)

    raise AutomationError(
        f"Timed out after {timeout}s waiting for the '{label}' button. "
        f"Possible causes: an unexpected dialog is covering it, the "
        f"template image ({template_path}) no longer matches (theme/"
        f"resolution/scaling changed), or the app is in an error state. "
        f"Take a screenshot now and compare it to {template_path}."
    )


def wait_for_element(template_path: str, label: str, timeout: float = CLICK_TIMEOUT_SECONDS):
    """Poll the screen for a template until it appears. Does not click anything.

    Used for the ready-text banner, which is a state signal, not a button.
    """
    log.info("Waiting for '%s' to appear (timeout %.0fs)...", label, timeout)
    deadline = time.time() + timeout

    while time.time() < deadline:
        location = pyautogui.locateCenterOnScreen(template_path, confidence=MATCH_CONFIDENCE)
        if location is not None:
            log.info("'%s' detected.", label)
            return location
        time.sleep(POLL_INTERVAL_SECONDS)

    raise AutomationError(
        f"Timed out after {timeout}s waiting for '{label}' to appear. "
        f"Take a screenshot now and compare it to {template_path} - the "
        f"crop, confidence, or app state may have changed."
    )


def run_scan_start_sequence() -> None:
    focus_creality_window()

    # Step 1: click Preview (this is the "Setup" click - it moves the app
    # from the Scan Settings screen into the live camera/calibration view).
    wait_for_and_click(TEMPLATE_PREVIEW_BUTTON, "Preview")

    # Step 2: wait for the "Please click [Start] to scan" banner - the
    # actual readiness signal from the app - before touching Start.
    wait_for_element(TEMPLATE_READY_TEXT, "ready-to-scan banner")

    # Step 3: now that we've confirmed readiness via the banner, the Start
    # button click is safe - no ambiguity left about which screen we're on.
    wait_for_and_click(TEMPLATE_START_BUTTON, "Start", timeout=5)

    log.info("Scan start sequence complete.")


if __name__ == "__main__":
    try:
        run_scan_start_sequence()
    except AutomationError as exc:
        log.error(str(exc))
        sys.exit(1)
    except Exception:
        log.exception("Unexpected error during automation")
        sys.exit(1)