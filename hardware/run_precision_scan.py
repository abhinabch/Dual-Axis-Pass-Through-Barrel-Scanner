"""
run_precision_scan.py

Integrates the Creality Scan UI automation with the iDM57-RS23 pan axis stepper motor.
Utilizes the actual encoder position feedback registers (0x602C/0x602D) for precision
motion verification.

Motion Sequence:
  1. Capture initial encoder value as Reference 0.
  2. Start Creality scanning.
  3. Move motor to +180 degrees.
  4. Move motor back to 0.
  5. Move motor to -180 degrees.
  6. Move motor back to 0.
  7. Once encoder registers verify motor is back to 0 the final time, stop Creality scanning.

Safety Features:
  - Emergency stop command on motor fault.
  - Watchdog checks screen for "Insufficient Data" warning popup, clearing it automatically.
  - Fail-safe: Moving the mouse cursor to any corner of the screen immediately aborts.
  - Dry-run mode: Run motor sweeps without executing UI scanner clicks (--dry-run).
"""

import sys
import time
import argparse
import logging
import pyautogui
import pygetwindow as gw
from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusException
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("precision_scan")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Motor Comms Configuration
SERIAL_PORT = "COM3"
BAUDRATE = 115200
UNIT_ID_PAN = 1
UNIT_ID_TILT = 2
TIMEOUT = 2

# Motor Calibration & Tuning
PULSES_PER_REV_PAN = 10000              # Number of steps per full 360-degree rotation
PULSES_PER_REV_TILT = 1000              # Pulses per rev for ESS17
DEADBAND_STEPS = 10                 # Allowable step error to consider target reached
TEST_VELOCITY_RPM = 30              # Speed of pan rotation
TEST_ACCEL_MS_PER_1000RPM = 300     # Acceleration profile
TEST_DECEL_MS_PER_1000RPM = 300     # Deceleration profile
TILT_VELOCITY_RPM = 20              # Speed of tilt rotation
TILT_ACCEL_MS = 200                 # Acceleration for tilt
TILT_DECEL_MS = 200                 # Deceleration for tilt

# Modbus Registers
STATUS_REGISTER = 4099              # 0x1003 - Motion status bits
ALARM_REGISTER = 4097               # 0x1001 - Alarm status register
REG_CONTROL_WORD = 0x6200           # Pr9.00 - Path 0 mode word
REG_POSITION_H = 0x6201             # Pr9.01 - Target Position (High)
REG_POSITION_L = 0x6202             # Pr9.02 - Target Position (Low)
REG_VELOCITY = 0x6203               # Pr9.03 - Velocity (RPM)
REG_ACC = 0x6204                    # Pr9.04 - Acceleration
REG_DEC = 0x6205                    # Pr9.05 - Deceleration
REG_PAUSE = 0x6206                  # Pr9.06 - Pause time
REG_TRIGGER = 0x6207                # Pr9.07 - Trigger path execution
REG_ENCODER_H = 0x602C              # Pr8.44 - Actual Encoder Feedback (High)
REG_ENCODER_L = 0x602D              # Pr8.45 - Actual Encoder Feedback (Low)

# Tilt-axis specific registers (ESS-RS Series)
TILT_STATUS_REG = 0x0007
TILT_ERROR_REG = 0x0006
TILT_REG_VELOCITY = 0x0023
TILT_REG_POS_H = 0x0024
TILT_REG_POS_L = 0x0025
TILT_REG_CONTROL = 0x0027

# Control mode settings
CONTROL_WORD_ABSOLUTE_POSITION_MOVE = 0x0001  # Bit0=1 (Position), Bit6=0 (Absolute)
TILT_START_RELATIVE_MOVE = 0x0001

# GUI Automation Settings
WINDOW_TITLE_SUBSTRING = "CrealityScan"
MATCH_CONFIDENCE = 0.85
CLICK_TIMEOUT_SECONDS = 20
POLL_INTERVAL_SECONDS = 0.5

# Template Images
TEMPLATE_PREVIEW_BUTTON = "templates/preview_button.png"
TEMPLATE_START_BUTTON = "templates/start_button.png"
TEMPLATE_READY_TEXT = "templates/ready_text.png"
TEMPLATE_STOP_BUTTON = "templates/stop_button.png"
TEMPLATE_WARNING_BUTTON = "templates/warning_button.png"
TEMPLATE_WARNING_OK_BUTTON = "templates/warning_ok_button.png"

# Safety: PyAutoGUI fail-safe (move cursor to top-left / corners to abort)
pyautogui.FAILSAFE = True

# Disable PyAutoGUI raising ImageNotFoundException if image not found (return None instead)
try:
    pyautogui.useImageNotFoundException(False)
except AttributeError:
    pass

class AutomationError(RuntimeError):
    """Raised when a GUI automation check fails."""

class PipelineError(Exception):
    """Raised for general pipeline failures that should trigger the Error screen."""

# ---------------------------------------------------------------------------
# Modbus / Motor Controls
# ---------------------------------------------------------------------------

def read_encoder_position(client: ModbusSerialClient, unit_id: int) -> int:
    """Reads 32-bit signed feedback position from registers 0x602C and 0x602D."""
    result = client.read_holding_registers(address=REG_ENCODER_H, count=2, device_id=unit_id)
    if result.isError():
        raise ModbusException(f"Failed to read encoder registers: {result}")
    
    high_word = result.registers[0]
    low_word = result.registers[1]
    
    val = (high_word << 16) | low_word
    # Convert to 32-bit signed integer (two's complement)
    if val >= 0x80000000:
        val -= 0x100000000
    return val

def decode_status(word: int) -> dict:
    """Decodes motor status bits."""
    return {
        "running": bool(word & (1 << 2)),
        "path_completed": bool(word & (1 << 5)),
        "faulty": bool(word & (1 << 0)),
        "enabled": bool(word & (1 << 1)),
    }

def read_motor_status(client: ModbusSerialClient, unit_id: int) -> dict:
    """Queries current motion state bitmask."""
    result = client.read_holding_registers(address=STATUS_REGISTER, count=1, device_id=unit_id)
    if result.isError():
        raise ModbusException(f"Failed to read status register: {result}")
    return decode_status(result.registers[0])

def check_motor_alarm(client: ModbusSerialClient, unit_id: int) -> int:
    """Queries current alarm code. Returns 0 if healthy."""
    result = client.read_holding_registers(address=ALARM_REGISTER, count=1, device_id=unit_id)
    if result.isError():
        raise ModbusException(f"Failed to read alarm register: {result}")
    return result.registers[0]

def send_absolute_move(client: ModbusSerialClient, unit_id: int, target_pos: int):
    """Programs and triggers an absolute move command to target_pos."""
    if unit_id == UNIT_ID_TILT:
        # ESS-RS specific move logic
        pos_val = int(target_pos) & 0xFFFFFFFF
        pos_h = (pos_val >> 16) & 0xFFFF
        pos_l = pos_val & 0xFFFF
        
        # Set velocity and accel for tilt
        client.write_registers(address=TILT_REG_VELOCITY, values=[TILT_VELOCITY_RPM], device_id=unit_id)
        client.write_registers(address=TILT_REG_ACCEL if 'TILT_REG_ACCEL' in globals() else 0x0021, values=[TILT_ACCEL_MS], device_id=unit_id)
        client.write_registers(address=TILT_REG_DECEL if 'TILT_REG_DECEL' in globals() else 0x0022, values=[TILT_DECEL_MS], device_id=unit_id)
        
        # Set position
        client.write_registers(address=TILT_REG_POS_H, values=[pos_h, pos_l], device_id=unit_id)
        # Trigger move
        client.write_registers(address=TILT_REG_CONTROL, values=[TILT_START_RELATIVE_MOVE], device_id=unit_id)
    else:
        # iDM57-RS specific move logic
        pos_val = int(target_pos) & 0xFFFFFFFF
        pos_h = (pos_val >> 16) & 0xFFFF
        pos_l = pos_val & 0xFFFF
        
        # 1. Set mode to absolute positioning
        result = client.write_registers(address=REG_CONTROL_WORD, values=[CONTROL_WORD_ABSOLUTE_POSITION_MOVE], device_id=unit_id)
        if result.isError():
            raise ModbusException(f"Failed to set absolute move control word: {result}")
            
        # 2. Set motion profile parameters
        result = client.write_registers(address=REG_POSITION_H, values=[
            pos_h,
            pos_l,
            TEST_VELOCITY_RPM,
            TEST_ACCEL_MS_PER_1000RPM,
            TEST_DECEL_MS_PER_1000RPM,
            0  # Pause time
        ], device_id=unit_id)
        if result.isError():
            raise ModbusException(f"Failed to write motion parameters: {result}")
            
        # 3. Trigger immediate execution of Path 0
        result = client.write_registers(address=REG_TRIGGER, values=[0x0010], device_id=unit_id)
        if result.isError():
            raise ModbusException(f"Trigger command failed: {result}")

def emergency_stop(client: ModbusSerialClient):
    """Commands E-stop (write 0x0040 to trigger register) to instantly halt motor."""
    log.warning("EMERGENCY STOP COMMAND SENT TO MOTOR")
    try:
        # Pan axis E-stop
        client.write_registers(address=0x6002, values=[0x0040], device_id=UNIT_ID_PAN)
        # Tilt axis (ESS-RS typically uses a different stop command, 
        # but often writing 0 to movement control or a specific stop reg works)
        client.write_registers(address=TILT_REG_CONTROL, values=[0x0000], device_id=UNIT_ID_TILT)
    except Exception as e:
        log.error("Failed to execute hardware emergency stop: %s", e)

class SafetyWatchdog:
    """
    Background thread that polls motor positions and alarms to detect stalls or faults.
    If a fault is detected, it calls the provided callback to trigger the Error screen.
    """
    def __init__(self, client: ModbusSerialClient, callback: Callable[[], None], poll_interval: float = 0.5):
        self.client = client
        self.callback = callback
        self.poll_interval = poll_interval
        self._running = False
        self._stop_event = None

    def start(self):
        import threading
        self._running = True
        self._stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        log.info("SafetyWatchdog started.")

    def stop(self):
        self._running = False
        if self._stop_event:
            self._stop_event().set()
        log.info("SafetyWatchdog stopped.")

    def _run(self):
        while self._running:
            try:
                # Check Pan Axis
                pan_status = read_motor_status(self.client, UNIT_ID_PAN)
                if pan_status["faulty"]:
                    log.error("SafetyWatchdog: Pan axis fault detected!")
                    self.callback()
                    break
                
                # Check Tilt Axis
                tilt_status = read_motor_status(self.client, UNIT_ID_TILT)
                if tilt_status["faulty"]:
                    log.error("SafetyWatchdog: Tilt axis fault detected!")
                    self.callback()
                    break
                
            except Exception as e:
                log.warning("SafetyWatchdog poll error: %s", e)
            
            time.sleep(self.poll_interval)

# ---------------------------------------------------------------------------
# Creality GUI Automation
# ---------------------------------------------------------------------------

def focus_creality_window():
    """Finds and activates the CrealityScan window in the foreground."""
    matches = [w for w in gw.getAllTitles() if WINDOW_TITLE_SUBSTRING.lower() in w.lower()]
    if not matches:
        raise AutomationError(
            f"No window found containing '{WINDOW_TITLE_SUBSTRING}'. "
            "Is the Creality Scan software running?"
        )
    win = gw.getWindowsWithTitle(matches[0])[0]
    if win.isMinimized:
        win.restore()
    win.activate()
    time.sleep(0.5)

def wait_for_and_click(template_path: str, label: str, timeout: float = CLICK_TIMEOUT_SECONDS):
    """Finds the template image on screen and clicks its center."""
    log.info("Waiting for '%s' button (timeout %.0fs)...", label, timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        location = pyautogui.locateCenterOnScreen(template_path, confidence=MATCH_CONFIDENCE)
        if location is not None:
            pyautogui.moveTo(location, duration=0.2)
            pyautogui.click()
            log.info("Clicked '%s' at %s", label, location)
            return location
        time.sleep(POLL_INTERVAL_SECONDS)
    raise AutomationError(f"Timed out waiting for '{label}' button ({template_path}).")

def wait_for_element(template_path: str, label: str, timeout: float = CLICK_TIMEOUT_SECONDS):
    """Finds the template image on screen without clicking it."""
    log.info("Waiting for '%s' (timeout %.0fs)...", label, timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        location = pyautogui.locateCenterOnScreen(template_path, confidence=MATCH_CONFIDENCE)
        if location is not None:
            log.info("'%s' detected.", label)
            return location
        time.sleep(POLL_INTERVAL_SECONDS)
    raise AutomationError(f"Timed out waiting for '{label}' to appear ({template_path}).")

def check_and_handle_warning():
    """Scans for warning popups and clicks the OK confirmation button to dismiss it."""
    try:
        warning_loc = pyautogui.locateCenterOnScreen(TEMPLATE_WARNING_BUTTON, confidence=MATCH_CONFIDENCE)
        if warning_loc is not None:
            log.warning("Scanning warning (insufficient data) detected! Attempting to dismiss...")
            ok_loc = pyautogui.locateCenterOnScreen(TEMPLATE_WARNING_OK_BUTTON, confidence=MATCH_CONFIDENCE)
            if ok_loc is not None:
                pyautogui.moveTo(ok_loc, duration=0.2)
                pyautogui.click()
                log.info("Warning dismissed by clicking OK at %s", ok_loc)
                # Keep target application focused
                focus_creality_window()
            else:
                log.warning("Warning dialog detected but OK button template was not found.")
    except Exception as e:
        log.error("Error checking/dismissing warning dialog: %s", e)

def run_scan_start_sequence(dry_run: bool):
    """Executes the start sequence: clicks Preview, waits for ready, clicks Start."""
    if dry_run:
        log.info("[DRY-RUN] Skipping UI scan start sequence.")
        return
        
    focus_creality_window()
    # Click Preview
    wait_for_and_click(TEMPLATE_PREVIEW_BUTTON, "Preview")
    # Wait for ready banner
    wait_for_element(TEMPLATE_READY_TEXT, "ready-to-scan banner")
    # Click Start
    wait_for_and_click(TEMPLATE_START_BUTTON, "Start", timeout=5)
    log.info("Scan started successfully.")

def run_scan_stop_sequence(dry_run: bool):
    """Stops scanning: clicks the stop/complete button."""
    if dry_run:
        log.info("[DRY-RUN] Skipping UI scan stop sequence.")
        return
        
    log.info("Sweeps complete. Stopping scanning session...")
    try:
        focus_creality_window()
        wait_for_and_click(TEMPLATE_STOP_BUTTON, "Stop/Complete")
        log.info("Scanner stopped successfully.")
    except Exception as e:
        log.error("Failed to click stop/complete button: %s. Please stop scan manually.", e)

def export_scan_file(src_path: str, dst_path: str):
    """
    Handles the manual file movement from Creality's default save location 
    to the pipeline's processed directory. 
    
    NOTE: Creality Scan's save path is typically user-defined or in a 
    specific AppData folder. This function assumes the file has been 
    successfully saved to `src_path`.
    """
    import shutil
    log.info("Exporting scan file from %s to %s", src_path, dst_path)
    try:
        shutil.copy2(src_path, dst_path)
        log.info("Scan file exported successfully.")
        return True
    except Exception as e:
        log.error("Failed to export scan file: %s", e)
        raise PipelineError(f"Scan export failed: {e}")

def perform_raster_sweep(client: ModbusSerialClient, dry_run: bool, progress_callback: Optional[Callable[[str], None]] = None):
    """
    Executes a full raster sweep of the barrel interior.
    Toggles between pan and tilt moves to cover the internal surface.
    """
    def report(msg):
        if progress_callback:
            progress_callback(msg)
        log.info(msg)

    # 1. Pre-flight Checks
    alarm_pan = check_motor_alarm(client, UNIT_ID_PAN)
    alarm_tilt = check_motor_alarm(client, UNIT_ID_TILT)
    if alarm_pan != 0 or alarm_tilt != 0:
        raise PipelineError(f"Motor fault detected! Pan: {alarm_pan}, Tilt: {alarm_tilt}")
        
    status_pan = read_motor_status(client, UNIT_ID_PAN)
    status_tilt = read_motor_status(client, UNIT_ID_TILT)
    if not status_pan["enabled"] or not status_tilt["enabled"]:
        raise PipelineError("One or more motor drivers are disabled.")

    # 2. Establish References
    start_pos_pan = read_encoder_position(client, UNIT_ID_PAN)
    start_pos_tilt = read_encoder_position(client, UNIT_ID_TILT)
    
    # Define a simple raster pattern (e.g., 3 tilt levels, each with a pan sweep)
    # In a real scenario, these would be calibrated based on barrel height
    tilt_levels = [0, 1000, -1000] # Relative steps for tilt
    pan_targets = [start_pos_pan + (PULSES_PER_REV_PAN // 2), start_pos_pan] # Simple 180 deg sweep

    # 3. Initiate Scanner
    run_scan_start_sequence(dry_run)
    time.sleep(1.0)

    try:
        for i, tilt_offset in enumerate(tilt_levels):
            report(f"Positioning tilt axis - Pass {i+1}/{len(tilt_levels)}...")
            
            # Move Tilt
            send_absolute_move(client, UNIT_ID_TILT, start_pos_tilt + tilt_offset)
            # Wait for tilt to settle (simplification: fixed wait or poll status)
            time.sleep(2.0) 
            
            for j, pan_target in enumerate(pan_targets):
                report(f"Scanning Pass {i+1}, Row {j+1}...")
                send_absolute_move(client, UNIT_ID_PAN, pan_target)
                
                # Monitor pan movement
                move_start = time.time()
                while True:
                    if time.time() - move_start > 30:
                        raise TimeoutError("Pan move timed out")
                    
                    pos = read_encoder_position(client, UNIT_ID_PAN)
                    state = read_motor_status(client, UNIT_ID_PAN)
                    if abs(pos - pan_target) <= DEADBAND_STEPS and not state["running"]:
                        break
                    
                    if not dry_run:
                        check_and_handle_warning()
                    time.sleep(0.1)
        
        # Return to Home
        report("Returning to home position...")
        send_absolute_move(client, UNIT_ID_TILT, start_pos_tilt)
        send_absolute_move(client, UNIT_ID_PAN, start_pos_pan)
        time.sleep(2.0)

    except Exception as e:
        emergency_stop(client)
        raise e
    finally:
        run_scan_stop_sequence(dry_run)

# ---------------------------------------------------------------------------
# Coordinated Control Loop
# ---------------------------------------------------------------------------

def execute_scan_motion(client: ModbusSerialClient, dry_run: bool):
    # 1. Pre-flight Checks
    alarm = check_motor_alarm(client)
    if alarm != 0:
        log.error("Motor reports active fault code: %d (0x%04X). Clear it first.", alarm, alarm)
        return
        
    status = read_motor_status(client)
    if not status["enabled"]:
        log.error("Motor driver is disabled. Verify power and enable input.")
        return
        
    if status["running"]:
        log.error("Motor is currently running. Clear active motions first.")
        return

    # 2. Establish Starting Position (Treat as reference 0)
    start_pos = read_encoder_position(client, UNIT_ID_PAN)
    log.info("Established starting reference encoder position: %d", start_pos)

    # 3. Calculate absolute targets
    target_180_steps = PULSES_PER_REV_PAN // 2
    targets = [
        ("Go to +180°", start_pos + target_180_steps),
        ("Go to 0°", start_pos),
        ("Go to -180°", start_pos - target_180_steps),
        ("Go to 0° (Final)", start_pos)
    ]

    # 4. Initiate Scanner
    run_scan_start_sequence(dry_run)
    time.sleep(1.0) # Settle scan startup

    # 5. Sweep through targets
    try:
        for name, target_pos in targets:
            log.info("--- Executing Command: %s (Target: %d steps) ---", name, target_pos)
            send_absolute_move(client, UNIT_ID_PAN, target_pos)
            
            # Monitoring loop for this movement segment
            last_warning_check = 0.0
            move_start_time = time.time()
            segment_timeout = 25.0
            
            while time.time() - move_start_time < segment_timeout:
                current_time = time.time()
                
                # Check for warning window and handle it
                if not dry_run and (current_time - last_warning_check >= 0.5):
                    check_and_handle_warning()
                    last_warning_check = current_time
                
                # Read hardware feedback
                encoder_pos = read_encoder_position(client, UNIT_ID_PAN)
                motor_state = read_motor_status(client, UNIT_ID_PAN)
                
                # Check for faults during motion
                if motor_state["faulty"] or check_motor_alarm(client, UNIT_ID_PAN) != 0:
                    raise RuntimeError("Motor driver faulted during movement segment!")
                    
                diff = abs(encoder_pos - target_pos)
                log.info("Pos: %d | Target: %d | Remaining: %d | Moving: %s", 
                         encoder_pos, target_pos, diff, motor_state["running"])
                
                # Settle condition: Encoder feedback is close to target and motor stopped running
                if diff <= DEADBAND_STEPS:
                    time.sleep(0.2)  # Settle time
                    final_pos = read_encoder_position(client, UNIT_ID_PAN)
                    final_state = read_motor_status(client, UNIT_ID_PAN)
                    if abs(final_pos - target_pos) <= DEADBAND_STEPS and not final_state["running"]:
                        log.info("Target reached and settled successfully at %d", final_pos)
                        break
                        
                time.sleep(0.1)
            else:
                raise TimeoutError(f"Motion segment timed out waiting for target: {target_pos}")
                
    except (Exception, KeyboardInterrupt) as e:
        log.error("Sweep exception occurred: %s. Initiating emergency halt.", e)
        emergency_stop(client)
        raise e
    
    # 6. Final stop scan
    run_scan_stop_sequence(dry_run)

# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Coordinated scanner and motor sweep script.")
    parser.add_argument("--dry-run", action="store_true", help="Perform motor sweeps without triggering Creality Scan UI actions.")
    args = parser.parse_args()

    client = ModbusSerialClient(
        port=SERIAL_PORT,
        baudrate=BAUDRATE,
        parity="N",
        stopbits=1,
        bytesize=8,
        timeout=TIMEOUT,
    )

    log.info("Connecting to motor on %s at %d baud...", SERIAL_PORT, BAUDRATE)
    if not client.connect():
        log.error("Could not open serial connection to motor on %s.", SERIAL_PORT)
        sys.exit(1)

    try:
        execute_scan_motion(client, args.dry_run)
        log.info("System sweep sequence executed successfully.")
    except Exception as e:
        log.critical("Scan aborted due to error: %s", e)
        sys.exit(1)
    finally:
        client.close()
        log.info("Modbus connection closed.")

if __name__ == "__main__":
    main()
