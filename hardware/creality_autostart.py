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
        # Each poll grabs the screen and runs template matching. Backing this off
        # from 0.5s leaves headroom for the Tk main loop and the Modbus threads
        # sharing this process; UI elements we wait on appear on human timescales,
        # so a 1s cadence costs nothing in practice.
        poll_interval: float = 1.0,
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

        # Debug output folder (repo_root/debug) -- timeout screenshots land here so a
        # failed match can be inspected against the template it was compared to.
        self.debug_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "debug")
        )
        os.makedirs(self.debug_dir, exist_ok=True)

    @staticmethod
    def _find_window_title() -> Optional[str]:
        """Find the real CrealityScan window title among all open windows.

        A plain substring match on "CrealityScan" also matches unrelated windows
        whose title happens to contain it -- e.g. an editor with a file named
        "CrealityScan_notes.md" open produces a title like "CrealityScan_notes.md
        - ... - Visual Studio Code". Since getAllTitles() order isn't guaranteed,
        such a window can sort before the real app and get focused/maximized
        instead of it. The real app's title is exactly "CrealityScan", so prefer
        an exact (case-insensitive) match and only fall back to substring
        matching if no exact match exists.
        """
        titles = gw.getAllTitles()
        for t in titles:
            if t.strip().lower() == WINDOW_TITLE_SUBSTRING.lower():
                return t
        for t in titles:
            if WINDOW_TITLE_SUBSTRING.lower() in t.lower():
                return t
        return None

    def is_window_available(self) -> bool:
        """Check if Creality Scan window is currently open."""
        try:
            return self._find_window_title() is not None
        except Exception:
            return False

    @staticmethod
    def _foreground_state(win):
        """True/False if the window is/isn't foreground, None if undeterminable."""
        try:
            active = gw.getActiveWindow()
        except Exception:
            return None
        if active is None:
            return None
        try:
            return active.title == win.title
        except Exception:
            return None

    def focus_window(self) -> None:
        """Find the Creality Scan window and bring it to the foreground.

        pygetwindow's activate() calls Win32 SetForegroundWindow and raises
        whenever it returns falsy -- including when the window is ALREADY in the
        foreground, which surfaces as the self-contradictory "Error code from
        Windows: 0 - The operation completed successfully." Windows also refuses
        foreground changes outright under its focus-stealing rules.

        Treating that exception as fatal meant stop_scan() and export_scan() threw
        before doing anything, so a completed scan was never saved and the pipeline
        fell through to the placeholder fallback.obscan. Judge by whether the
        window actually ended up foreground, not by whether activate() threw.
        """
        match = self._find_window_title()
        if match is None:
            raise AutomationError(
                f"No window found with title containing '{WINDOW_TITLE_SUBSTRING}'. "
                "Is Creality Scan running?"
            )

        win = gw.getWindowsWithTitle(match)[0]
        if win.isMinimized:
            try:
                win.restore()
                time.sleep(0.3)
            except Exception as e:
                log.debug("Restore reported: %s", e)

        if self._foreground_state(win) is not True:
            for attempt in range(1, 4):
                try:
                    win.activate()
                except Exception as e:
                    log.debug("activate() attempt %d reported: %s", attempt, e)
                time.sleep(0.3)
                state = self._foreground_state(win)
                if state is not False:
                    # True (confirmed foreground) or None (can't tell -- proceed
                    # rather than block on an unreliable probe).
                    break
            else:
                raise AutomationError(
                    f"Could not bring '{match}' to the foreground after 3 attempts. "
                    "Another window may be holding focus (a dialog, screen saver, or "
                    "UAC prompt). Clicks and keystrokes would land in the wrong app."
                )

        if not win.isMaximized:
            # Template images are captured against a full-screen window (folders are
            # named by resolution). A non-maximized window renders the UI at a
            # different scale/position, so template matching silently fails.
            try:
                win.maximize()
            except Exception as e:
                log.debug("maximize() reported: %s", e)
            time.sleep(0.5)
        time.sleep(0.5)
        log.info("Focused window: %s", win.title)

    def _locate_tolerant(self, template_path: str, base_confidence: float):
        """Try a range of confidences and colour/grayscale modes before giving up.

        A single fixed confidence is brittle across Creality Scan UI theme/version
        changes (icon anti-aliasing, colour shifts). This widens the search without
        weakening the default confidence used elsewhere (export dialogs, etc.).

        All variants are matched against ONE screen grab held in memory. The
        previous version called locateCenterOnScreen() per variant, and each of
        those takes its own full-screen screenshot -- 8 grabs per poll on a miss.
        Repeated at the poll interval for the length of a 60s timeout, that
        saturated the CPU badly enough to stall the Tk main loop for ~60s and push
        Modbus reads past their timeout while the drives' replies were already in
        the receive buffer, desyncing RTU framing for the rest of the run. Matching
        is cheap; grabbing the screen is not.
        """
        try:
            haystack = pyautogui.screenshot()
        except Exception as e:
            log.debug("Screen grab failed during match: %s", e)
            return None, None, None

        for conf_try in (base_confidence, 0.78, 0.7):
            for grayscale in (False, True):
                try:
                    box = pyautogui.locate(
                        template_path, haystack, confidence=conf_try, grayscale=grayscale
                    )
                except Exception:
                    box = None
                if box is not None:
                    return pyautogui.center(box), conf_try, grayscale
        return None, None, None

    def _save_debug_screenshot(self, tag: str) -> Optional[str]:
        """Save a full-screen screenshot to debug/ for inspecting a failed match."""
        try:
            dbg_path = os.path.join(
                self.debug_dir, f"timeout_{tag}_{int(time.time())}.png"
            )
            pyautogui.screenshot(dbg_path)
            log.error("Saved debug screenshot to %s", dbg_path)
            return dbg_path
        except Exception as e:
            log.debug("Failed to save debug screenshot: %s", e)
            return None

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

        # Fast path: the window is maximized by focus_window(), so pyautogui.size()
        # is the resolution its UI is actually rendered at. A folder is literally
        # named after the resolution it was captured for, so if one matches, trust
        # it outright instead of running it through the same exhaustive probe as an
        # unknown folder (4 templates x 4 confidences x 2 grayscale modes = up to 32
        # full-screen screenshot+match calls). That probe is also being run before
        # CrealityScan has necessarily finished rendering after launch, so on a
        # correctly-named-but-not-yet-drawn window it burns through every combo on
        # every folder for nothing and falls back to the empty base dir -- costing
        # tens of seconds before the real button-wait loop ever starts polling.
        screen_w, screen_h = pyautogui.size()
        preferred_dir = os.path.join(self.template_base_dir, f"{screen_w}x{screen_h}")
        if preferred_dir in candidate_dirs:
            self.active_resolution_folder = preferred_dir
            log.info(
                "Using template set matching screen resolution %dx%d: %s",
                screen_w, screen_h, preferred_dir,
            )
            return preferred_dir

        # Key elements to test for resolution matching
        test_templates = ["preview_button.png", "ready_text.png", "start_button.png", "stop_button.png"]

        for cdir in candidate_dirs:
            for t_name in test_templates:
                t_path = os.path.join(cdir, t_name)
                if os.path.exists(t_path):
                    loc, conf_used, grayscale = self._locate_tolerant(t_path, self.confidence)
                    if loc is not None:
                        self.active_resolution_folder = cdir
                        log.info(
                            "Detected active template set at: %s (via %s, confidence=%.2f, grayscale=%s)",
                            cdir, t_name, conf_used, grayscale,
                        )
                        return cdir

        # Default fallback to base directory
        self.active_resolution_folder = self.template_base_dir
        self._save_debug_screenshot("detect_resolution")
        log.warning(
            "No specific template set matched screen. Defaulting to base template dir: %s. "
            "Inspect the saved debug screenshot against the template PNGs -- the CrealityScan "
            "UI may have changed since the templates were captured.",
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
            location, conf_used, grayscale = self._locate_tolerant(template_path, conf)
            if location is not None:
                pyautogui.moveTo(location, duration=0.2)
                pyautogui.click()
                log.info(
                    "Clicked '%s' at %s (confidence=%.2f, grayscale=%s)",
                    label, location, conf_used, grayscale,
                )
                return location
            time.sleep(self.poll_interval)

        dbg_path = self._save_debug_screenshot(f"wait_for_and_click_{label}")

        raise AutomationError(
            f"Timed out after {timeout}s waiting for '{label}' button. "
            f"Template searched: {template_path}. Debug screenshot: {dbg_path}. "
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
            location, conf_used, grayscale = self._locate_tolerant(template_path, conf)
            if location is not None:
                log.info(
                    "Detected '%s' at %s (confidence=%.2f, grayscale=%s)",
                    label, location, conf_used, grayscale,
                )
                return location
            time.sleep(self.poll_interval)

        dbg_path = self._save_debug_screenshot(f"wait_for_element_{label}")

        # Auto-capture a fresh template from the current screen and retry once --
        # covers the case where the CrealityScan theme/version has drifted from
        # what the stored template was captured against.
        try:
            saved = self.capture_template_from_screen(template_name)
            if saved:
                time.sleep(0.5)
                loc, conf_used, grayscale = self._locate_tolerant(saved, min(0.8, conf))
                if loc is not None:
                    log.info("Auto-captured template matched at %s", loc)
                    return loc
                log.info("Auto-captured template did not match immediately; inspect %s", saved)
        except Exception as e:
            log.debug("Auto-capture attempt failed: %s", e)

        # OCR fallback: confirm the banner text is present even if image matching
        # can't localize it precisely. Returns an approximate top-center location.
        try:
            full = pyautogui.screenshot()
            if self._ocr_contains_keywords(full):
                w, h = full.size
                guess = (int(w // 2), int(h * 0.15))
                log.info("OCR fallback returning approximate location %s", guess)
                return guess
        except Exception as e:
            log.debug("OCR fallback failed: %s", e)

        raise AutomationError(
            f"Timed out after {timeout}s waiting for '{label}'. "
            f"Template searched: {template_path}. Debug screenshot: {dbg_path}."
        )

    def capture_template_from_screen(self, template_name: str) -> Optional[str]:
        """Capture a candidate template from the current screen into debug/ only.

        Returns the saved candidate path, or None.

        This deliberately does NOT write into the templates/ tree. It used to save
        over the canonical template (e.g. templates/1280x800/ready_text.png) using
        whatever happened to be on screen at the moment of a timeout -- so a single
        failed match permanently replaced a known-good template with a crop of the
        wrong UI state. That silently poisoned the template set for every later run
        and made the automation progressively less reliable. Candidates now land in
        debug/ for a human to inspect and promote by hand if they are actually good.

        The capture crops a top-centered band of the screen where the ready banner
        typically appears; sizes are clamped to the current screen size.
        """
        try:
            img = pyautogui.screenshot()
            w, h = img.size

            crop_w = min(900, max(200, int(w * 0.7)))
            crop_h = min(200, max(40, int(h * 0.18)))
            crop_x = max(0, (w - crop_w) // 2)
            crop_y = max(0, int(h * 0.08))

            crop = img.crop((crop_x, crop_y, crop_x + crop_w, crop_y + crop_h))

            dbg_name = f"auto_capture_{template_name.replace('.', '_')}_{int(time.time())}.png"
            dbg_path = os.path.join(self.debug_dir, dbg_name)
            crop.save(dbg_path)

            log.info(
                "Auto-captured candidate for '%s' saved to %s. The stored template was "
                "NOT modified -- inspect this crop and copy it over the template "
                "manually if it is correct.",
                template_name, dbg_path,
            )
            return dbg_path
        except Exception as e:
            log.debug("Auto-capture failed: %s", e)
            return None

    def _ocr_contains_keywords(self, image, keywords=("start", "please", "ready")) -> bool:
        """Optional OCR-based check for words in the screenshot. Returns True if any keyword found."""
        try:
            import pytesseract
        except Exception:
            log.debug("pytesseract not available; skipping OCR fallback")
            return False

        try:
            text = pytesseract.image_to_string(image).lower()
            for kw in keywords:
                if kw in text:
                    log.info("OCR found keyword '%s' in screen text", kw)
                    return True
            log.debug("OCR text: %s", text.strip()[:200])
            return False
        except Exception as e:
            log.debug("OCR check failed: %s", e)
            return False

    def start_scan(self) -> None:
        """Execute the scan start sequence: Preview -> Banner Ready -> Start."""
        self.focus_window()
        if not self.active_resolution_folder:
            self.detect_resolution()

        # Step 1: Click Preview / Setup. Generous timeout: this is the first click
        # after launch/focus, and detect_resolution() no longer spends ~30-60s
        # probing template folders on the way here -- that probe used to double as
        # an incidental buffer while CrealityScan finished its own startup
        # rendering. Give that real startup lag an explicit allowance instead of
        # relying on the removed probe to cover it.
        self.wait_for_and_click("preview_button.png", "Preview", timeout=60.0)

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