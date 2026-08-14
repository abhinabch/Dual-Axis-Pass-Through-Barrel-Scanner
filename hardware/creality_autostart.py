"""
creality_autostart.py

Automates Creality Scan UI interaction (Preview/Setup -> Start -> Stop -> Export/Save)
using multi-resolution image matching and Windows automation.

REQUIREMENTS (run once on Windows machine running Creality Scan):
    pip install pyautogui opencv-python pygetwindow pillow pyperclip

WHAT IT DOES
    1. Detects multi-resolution template sets.
    2. Focuses the CrealityScan window.
    3. Handles Preview -> Ready Banner -> Start sequence.
    4. Handles Stop sequence and warning dialogs.
    5. Automates Windows file picker dialog to save .obscan files to a known path.
"""

import os
import sys
import time
import datetime
import logging
from typing import Optional, List, Tuple

import pyautogui
import pygetwindow as gw

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("creality_autostart")

WINDOW_TITLE_SUBSTRING = "CrealityScan"

# Safety: moving the mouse to a screen corner aborts PyAutoGUI
pyautogui.FAILSAFE = True

# Disable PyAutoGUI raising ImageNotFoundException if image not found (return None instead)
try:
    pyautogui.useImageNotFoundException(False)
except AttributeError:
    pass


class AutomationError(RuntimeError):
    """Raised when a required UI element can't be found in time or automation fails."""


class CrealityAutomator:
    """Class encapsulating state and methods for Creality Scan GUI automation.
    
    Supports multi-resolution template sets, start/stop sequences,
    and Windows export dialog automation.
    """

    def __init__(
        self,
        template_base_dir: str = "templates",
        confidence: float = 0.85,
        click_timeout: float = 20.0,
        poll_interval: float = 0.5,
        save_dir: str = "saved_creality_files",
    ):
        self.template_base_dir = os.path.abspath(template_base_dir)
        self.confidence = confidence
        self.click_timeout = click_timeout
        self.poll_interval = poll_interval
        self.save_dir = os.path.abspath(save_dir)
        self.active_resolution_folder: Optional[str] = None
        self.last_export_path: Optional[str] = None

        os.makedirs(self.save_dir, exist_ok=True)

    def is_window_available(self) -> bool:
        """Check if Creality Scan window is currently open."""
        try:
            matches = [w for w in gw.getAllTitles() if WINDOW_TITLE_SUBSTRING.lower() in w.lower()]
            return len(matches) > 0
        except Exception:
            return False

    def focus_window(self) -> None:
        """Find the Creality Scan window and bring it to the foreground."""
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
        time.sleep(0.5)
        log.info("Focused window: %s", win.title)

    def _get_candidate_dirs(self) -> List[str]:
        """Return list of template folders to search (subdirectories first, then base)."""
        candidate_dirs = []
        if os.path.exists(self.template_base_dir):
            for entry in os.listdir(self.template_base_dir):
                full_path = os.path.join(self.template_base_dir, entry)
                if os.path.isdir(full_path):
                    candidate_dirs.append(full_path)
            candidate_dirs.append(self.template_base_dir)
        return candidate_dirs

    def detect_resolution(self) -> str:
        """Calibrate/detect which resolution template set matches the active window.
        
        Tests available candidate folders by searching for known UI templates.
        """
        self.focus_window()
        candidate_dirs = self._get_candidate_dirs()

        # Key elements to test for resolution matching
        test_templates = ["preview_button.png", "ready_text.png", "start_button.png", "stop_button.png"]

        for cdir in candidate_dirs:
            for t_name in test_templates:
                t_path = os.path.join(cdir, t_name)
                if os.path.exists(t_path):
                    loc = pyautogui.locateCenterOnScreen(t_path, confidence=self.confidence)
                    if loc is not None:
                        self.active_resolution_folder = cdir
                        log.info("Detected active template set at: %s", cdir)
                        return cdir

        # Default fallback to base directory
        self.active_resolution_folder = self.template_base_dir
        log.warning(
            "No specific template set matched screen. Defaulting to base template dir: %s",
            self.template_base_dir,
        )
        return self.template_base_dir

    def resolve_template(self, template_name: str) -> str:
        """Locate template file path using active resolution folder or fallback search."""
        search_dirs = []
        if self.active_resolution_folder:
            search_dirs.append(self.active_resolution_folder)
        search_dirs.extend(self._get_candidate_dirs())

        for sdir in search_dirs:
            candidate = os.path.join(sdir, template_name)
            if os.path.exists(candidate):
                return candidate

        # Fallback to direct path in base dir
        return os.path.join(self.template_base_dir, template_name)

    def wait_for_and_click(
        self,
        template_name: str,
        label: str,
        timeout: Optional[float] = None,
        confidence: Optional[float] = None,
    ) -> Tuple[int, int]:
        """Poll the screen for a button image and click its center."""
        timeout = timeout if timeout is not None else self.click_timeout
        conf = confidence if confidence is not None else self.confidence
        template_path = self.resolve_template(template_name)

        log.info("Waiting for '%s' button (%s, timeout %.0fs)...", label, template_name, timeout)
        deadline = time.time() + timeout

        while time.time() < deadline:
            location = pyautogui.locateCenterOnScreen(template_path, confidence=conf)
            if location is not None:
                pyautogui.moveTo(location, duration=0.2)
                pyautogui.click()
                log.info("Clicked '%s' at %s", label, location)
                return location
            time.sleep(self.poll_interval)

        raise AutomationError(
            f"Timed out after {timeout}s waiting for '{label}' button. "
            f"Template searched: {template_path}. "
            "Verify display scaling, resolution, or window occlusion."
        )

    def wait_for_element(
        self,
        template_name: str,
        label: str,
        timeout: Optional[float] = None,
        confidence: Optional[float] = None,
    ) -> Tuple[int, int]:
        """Poll screen for an element to appear without clicking."""
        timeout = timeout if timeout is not None else self.click_timeout
        conf = confidence if confidence is not None else self.confidence
        template_path = self.resolve_template(template_name)

        log.info("Waiting for '%s' (%s, timeout %.0fs)...", label, template_name, timeout)
        deadline = time.time() + timeout

        while time.time() < deadline:
            location = pyautogui.locateCenterOnScreen(template_path, confidence=conf)
            if location is not None:
                log.info("Detected '%s' at %s", label, location)
                return location
            time.sleep(self.poll_interval)

        raise AutomationError(
            f"Timed out after {timeout}s waiting for '{label}'. "
            f"Template searched: {template_path}."
        )

    def start_scan(self) -> None:
        """Execute the scan start sequence: Preview -> Banner Ready -> Start."""
        self.focus_window()
        if not self.active_resolution_folder:
            self.detect_resolution()

        # Step 1: Click Preview / Setup
        self.wait_for_and_click("preview_button.png", "Preview")

        # Step 2: Wait for ready text banner
        self.wait_for_element("ready_text.png", "Ready Banner")

        # Step 3: Click Start button
        self.wait_for_and_click("start_button.png", "Start", timeout=10)
        log.info("Scan start sequence executed successfully.")

    def stop_scan(self) -> None:
        """Execute the scan stop sequence."""
        self.focus_window()
        log.info("Stopping scan sequence...")

        try:
            self.wait_for_and_click("stop_button.png", "Stop", timeout=15)
        except AutomationError:
            log.warning("Stop button template match timed out. Attempting fallback hotkey (Space/Esc)...")
            pyautogui.press("space")

        # Check for confirmation/warning dialogs
        time.sleep(1.0)
        warning_ok = self.resolve_template("warning_ok_button.png")
        if os.path.exists(warning_ok):
            try:
                loc = pyautogui.locateCenterOnScreen(warning_ok, confidence=self.confidence)
                if loc is not None:
                    pyautogui.click(loc)
                    log.info("Dismissed warning dialog via OK button.")
            except Exception as e:
                log.debug("Warning dialog check exception: %s", e)

        log.info("Scan stop sequence complete.")

    def export_scan(self, barrel_id: Optional[str] = None) -> str:
        """Export/Save the completed scan to an .obscan file and return absolute path."""
        self.focus_window()
        log.info("Initiating scan export/save sequence...")

        # Formulate output file path
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = f"barrel_{barrel_id}" if barrel_id else "scan"
        filename = f"{prefix}_{ts}.obscan"
        target_path = os.path.abspath(os.path.join(self.save_dir, filename))

        # Look for Export / Save / Finish button or use Ctrl+S fallback
        export_clicked = False
        for btn_name in ["export_button.png", "save_button.png", "finish_button.png"]:
            t_path = self.resolve_template(btn_name)
            if os.path.exists(t_path):
                try:
                    self.wait_for_and_click(btn_name, "Export/Save", timeout=5)
                    export_clicked = True
                    break
                except AutomationError:
                    continue

        if not export_clicked:
            log.info("No matching export button template found. Using hotkey Ctrl+S.")
            pyautogui.hotkey("ctrl", "s")

        time.sleep(1.5)  # Wait for Windows "Save As" file dialog to open

        # Automate file dialog input
        log.info("Writing file path to save dialog: %s", target_path)
        
        # Paste or type exact filepath
        try:
            import pyperclip
            pyperclip.copy(target_path)
            pyautogui.hotkey("ctrl", "v")
        except ImportError:
            pyautogui.write(target_path, interval=0.02)

        time.sleep(0.5)
        pyautogui.press("enter")
        time.sleep(2.0)  # Settle time for file save completion

        self.last_export_path = target_path
        log.info("Export sequence complete. Expected output file: %s", target_path)
        return target_path

    def run_full_sequence(self, barrel_id: Optional[str] = None) -> str:
        """Convenience method to execute full scan lifecycle."""
        self.start_scan()
        log.info("Scan active. (Call stop_scan() and export_scan() as needed by application)")
        return self.save_dir


# Helper backward-compatible functions
def focus_creality_window() -> None:
    automator = CrealityAutomator()
    automator.focus_window()


def run_scan_start_sequence() -> None:
    automator = CrealityAutomator()
    automator.start_scan()


if __name__ == "__main__":
    try:
        log.info("Testing CrealityAutomator execution...")
        automator = CrealityAutomator()
        if automator.is_window_available():
            automator.detect_resolution()
            automator.start_scan()
        else:
            log.error("Creality Scan window not found. Please start Creality Scan to test.")
            sys.exit(1)
    except AutomationError as exc:
        log.error(str(exc))
        sys.exit(1)
    except Exception:
        log.exception("Unexpected error during automation execution")
        sys.exit(1)