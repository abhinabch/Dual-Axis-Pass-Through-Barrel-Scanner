"""
ESS17-RS04 (NEMA 17) - Rotate 15 Revolutions Forward & Backward (with E-Stop & Bus Auto-Scan)
---------------------------------------------------------------------------------------------
Demo script to command the ESS17-RS04 (NEMA 17) integrated stepper motor drive
to execute 15 full revolutions forward followed by 15 full revolutions backward.

Features:
  1. Ctrl+C (KeyboardInterrupt) Intercept: Pressing Ctrl+C during motion immediately
     issues a Modbus emergency stop / disable command to halt the motor.
  2. Standalone E-Stop mode: Run `python hardware/ess17_rs04_rotate_10_revs.py --estop`
  3. Modbus Auto-Scan mode: Run `python hardware/ess17_rs04_rotate_10_revs.py --scan`
     to automatically probe slave Unit IDs (1-10) and detect connected drives.
  4. Robust error handling with clear diagnostic feedback on bus timeout / communication failures.

Dependencies:
    pip install pymodbus

Usage:
    - Normal Demo:
        python hardware/ess17_rs04_rotate_10_revs.py [--port COM3] [--unit-id 2] [--revs 10] [--speed 60]
    - Bus Auto-Scan (troubleshoot communication / find motor Unit ID):
        python hardware/ess17_rs04_rotate_10_revs.py --scan [--port COM3]
    - Emergency Stop:
        python hardware/ess17_rs04_rotate_10_revs.py --estop [--port COM3]
"""

import argparse
import sys
import time
from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusException, ModbusIOException

# ---------------------------------------------------------------------------
# Default Configuration - matches ESS17-RS04 setup
# ---------------------------------------------------------------------------
DEFAULT_SERIAL_PORT = "COM3"
DEFAULT_BAUDRATE = 115200
DEFAULT_UNIT_ID = 2           # Confirmed slave ID for ESS17-RS04 drive
DEFAULT_TIMEOUT = 2

# ---------------------------------------------------------------------------
# Modbus Registers (ESS-RS Series Table)
# ---------------------------------------------------------------------------
STATUS_REGISTER = 0x0007        # Motion status bitmask (read-only)
ERROR_CODE_REGISTER = 0x0006    # 0 = normal, 1-5 = error (read-only)

REG_STARTING_SPEED = 0x0020     # Starting speed of positioning move, r/min
REG_ACCEL = 0x0021              # Acceleration time, ms
REG_DECEL = 0x0022              # Deceleration time, ms
REG_VELOCITY = 0x0023           # Positioning movement speed, r/min
REG_POSITION_H = 0x0024         # Total pulse count, high 16 bits
REG_POSITION_L = 0x0025         # Total pulse count, low 16 bits
REG_MOVEMENT_CONTROL = 0x0027   # Movement control command (write-only)
REG_AUX_CONTROL = 0x002D        # Auxiliary control command (write-only)

# Move parameters
NUM_REVS = 20                   # <--- EDIT THIS: Number of revolutions to test
PULSES_PER_REV = 1000            # 1000 pulses per revolution (1 pulse per encoder line)
DEFAULT_VELOCITY_RPM = 60        # 60 RPM (1 rev/sec) - smooth and efficient for 15 revs
DEFAULT_ACCEL_MS = 200           # Acceleration ramp time (ms)
DEFAULT_DECEL_MS = 200           # Deceleration ramp time (ms)

# Movement control word (register 0x0027)
START_RELATIVE_POSITION_MOVE = 0x0001   # bit0=1, bit2=0 (relative move)
STOP_MOVE_CMD = 0x0000                  # bit0=0 (stop movement)
AUX_RELEASE_MOTOR_CMD = 0x0001          # Auxiliary release/disable motor command


def create_client(port=DEFAULT_SERIAL_PORT, baudrate=DEFAULT_BAUDRATE, timeout=DEFAULT_TIMEOUT):
    """Initialize Modbus RTU serial client."""
    return ModbusSerialClient(
        port=port,
        baudrate=baudrate,
        parity="N",
        stopbits=1,
        bytesize=8,
        timeout=timeout,
    )


def read_registers(client, address, count=1, unit_id=DEFAULT_UNIT_ID):
    """
    Read holding registers from the specified unit ID.
    Supports both `slave` and `device_id` keyword compatibility across pymodbus versions.
    """
    try:
        result = client.read_holding_registers(address=address, count=count, device_id=unit_id)
    except TypeError:
        result = client.read_holding_registers(address=address, count=count, slave=unit_id)

    if result is None or result.isError():
        raise ModbusIOException(f"No response or error reading address 0x{address:04X} (Unit ID {unit_id}): {result}")
    return result.registers


def write_registers(client, address, values, unit_id=DEFAULT_UNIT_ID):
    """
    Write holding registers to the specified unit ID.
    Supports both `slave` and `device_id` keyword compatibility across pymodbus versions.
    """
    try:
        result = client.write_registers(address=address, values=values, device_id=unit_id)
    except TypeError:
        result = client.write_registers(address=address, values=values, slave=unit_id)

    if result is None or result.isError():
        raise ModbusIOException(f"No response or error writing address 0x{address:04X} (Unit ID {unit_id}): {result}")
    return result


def decode_status(word):
    """Decode status register bitmask into human-readable flags."""
    return {
        "in_position": bool(word & (1 << 0)),
        "homing_completed": bool(word & (1 << 1)),
        "running": bool(word & (1 << 2)),
        "alarm": bool(word & (1 << 3)),
        "motor_released": bool(word & (1 << 4)),  # True = disabled/released
        "positive_soft_limit": bool(word & (1 << 5)),
        "negative_soft_limit": bool(word & (1 << 6)),
    }


def check_status(client, unit_id=DEFAULT_UNIT_ID):
    """
    Check current motion state and alarm status.
    Returns (ok: bool, status_dict: dict).
    """
    word = read_registers(client, STATUS_REGISTER, 1, unit_id=unit_id)[0]
    status = decode_status(word)
    print(f"Motion state: {status}")

    if status["alarm"]:
        error_code = read_registers(client, ERROR_CODE_REGISTER, 1, unit_id=unit_id)[0]
        print(f"ALARM ACTIVE (error code {error_code}) - aborting. Clear fault before retrying.")
        return False, status
    if status["motor_released"]:
        print("Motor is RELEASED (disabled) - aborting. Enable motor before retrying.")
        return False, status

    return True, status


def scan_bus(port=DEFAULT_SERIAL_PORT, baudrate=DEFAULT_BAUDRATE):
    """
    Scans slave Unit IDs 1 through 10 on the given port to find responding Modbus devices.
    """
    print(f"\n=======================================================")
    print(f"  Modbus Bus Auto-Scan on {port} (Baud: {baudrate})")
    print(f"=======================================================")
    found_devices = []

    client = ModbusSerialClient(port=port, baudrate=baudrate, parity="N", stopbits=1, bytesize=8, timeout=0.3)
    if not client.connect():
        print(f"ERROR: Could not open serial port {port}.")
        return found_devices

    try:
        for unit_id in range(1, 11):
            sys.stdout.write(f"Probing Unit ID {unit_id:2d}... ")
            sys.stdout.flush()
            try:
                # Try reading ESS17 status register (0x0007)
                res = client.read_holding_registers(address=STATUS_REGISTER, count=1, device_id=unit_id)
                if res and not res.isError():
                    val = res.registers[0]
                    print(f"FOUND! (ESS17-RS04 status reg 0x0007 = 0x{val:04X})")
                    found_devices.append((unit_id, "ESS17-RS04", val))
                    continue

                # Try reading iDM57 test register (0x01BC / 444)
                res2 = client.read_holding_registers(address=444, count=1, device_id=unit_id)
                if res2 and not res2.isError():
                    val2 = res2.registers[0]
                    print(f"FOUND! (iDM57 test reg 0x01BC = 0x{val2:04X})")
                    found_devices.append((unit_id, "iDM57-RS23", val2))
                    continue

                print("No response")
            except Exception:
                print("No response")
    finally:
        client.close()

    print(f"=======================================================")
    if found_devices:
        print("Summary of responding devices:")
        for dev in found_devices:
            print(f"  -> Unit ID {dev[0]}: Likely {dev[1]} (Response: 0x{dev[2]:04X})")
    else:
        print("No devices responded on Unit IDs 1-10.")
        print("\nTroubleshooting Tips:")
        print("  1. Verify motor 24V/48V DC power supply is switched ON.")
        print("  2. Check RS485 wiring (A+ to A+, B- to B-). Try swapping A/B if unconfirmed.")
        print("  3. Verify correct serial port (e.g. COM3 vs COM4/COM5 in Device Manager).")
        print("  4. Ensure no other software (STEPPERONLINE MotionStudio, Node-RED, etc.) is holding the COM port.")

    return found_devices


def emergency_stop(client, unit_id=DEFAULT_UNIT_ID):
    """
    Send an emergency stop / halt signal over Modbus.
    1. Writes 0x0000 to Movement Control Register (0x0027) to cancel active motion.
    2. Writes 0x0001 to Auxiliary Control Register (0x002D) to release/disable the drive.
    """
    print("\n!!! EMERGENCY STOP TRIGGERED !!!")
    errors = []

    try:
        write_registers(client, REG_MOVEMENT_CONTROL, [STOP_MOVE_CMD], unit_id=unit_id)
        print("-> Sent STOP command (0x0000 -> Reg 0x0027).")
    except Exception as e:
        errors.append(f"Failed to write stop cmd to 0x0027: {e}")

    try:
        write_registers(client, REG_AUX_CONTROL, [AUX_RELEASE_MOTOR_CMD], unit_id=unit_id)
        print("-> Sent MOTOR RELEASE command (0x0001 -> Reg 0x002D).")
    except Exception as e:
        errors.append(f"Failed to write release cmd to 0x002D: {e}")

    if errors:
        for err in errors:
            print(f"E-Stop Warning: {err}")
    else:
        print("Emergency stop commands sent successfully. Motor motion halted.")


def rotate_relative(client, num_revolutions, velocity_rpm=DEFAULT_VELOCITY_RPM,
                    unit_id=DEFAULT_UNIT_ID, pulses_per_rev=PULSES_PER_REV,
                    accel_ms=DEFAULT_ACCEL_MS, decel_ms=DEFAULT_DECEL_MS):
    """
    Command a relative move for a given number of revolutions.
    Positive num_revolutions = forward rotation.
    Negative num_revolutions = backward rotation.
    Catches Ctrl+C (KeyboardInterrupt) to execute immediate Emergency Stop.
    """
    direction_str = "forward" if num_revolutions >= 0 else "backward"
    abs_revs = abs(num_revolutions)
    print(f"\n--- Initiating relative move: {abs_revs} rev(s) {direction_str} ({num_revolutions:+} revs total) ---")
    print(f"Testing configuration: {NUM_REVS} revolutions target.")
    print(">>> Press [Ctrl+C] at any time for EMERGENCY STOP <<<")

    try:
        ok, _ = check_status(client, unit_id=unit_id)
        if not ok:
            print("Pre-move status check failed. Move NOT sent.")
            return False

        status_word = read_registers(client, STATUS_REGISTER, 1, unit_id=unit_id)[0]
        if status_word & (1 << 2):
            print("Motor is already running - aborting to avoid overlapping commands.")
            return False

        # Calculate target pulse count (32-bit signed two's complement)
        target_pulses = int(num_revolutions * pulses_per_rev)
        pulses_u32 = target_pulses & 0xFFFFFFFF
        position_h = (pulses_u32 >> 16) & 0xFFFF
        position_l = pulses_u32 & 0xFFFF

        print(f"Writing parameters: target_pulses={target_pulses} (0x{position_h:04X}{position_l:04X}), "
              f"speed={velocity_rpm} RPM, accel={accel_ms} ms, decel={decel_ms} ms")

        # Write accel, decel, speed, position_h, position_l (0x0021 - 0x0025)
        write_registers(client, REG_ACCEL, [
            accel_ms,
            decel_ms,
            velocity_rpm,
            position_h,
            position_l,
        ], unit_id=unit_id)

        print("Triggering move now...")
        write_registers(client, REG_MOVEMENT_CONTROL, [START_RELATIVE_POSITION_MOVE], unit_id=unit_id)

        # Dynamic timeout calculation (minimum 25s)
        expected_move_time_s = (abs_revs / (velocity_rpm / 60.0)) + ((accel_ms + decel_ms) / 1000.0)
        timeout_s = max(25.0, expected_move_time_s + 10.0)

        start_time = time.time()
        while time.time() - start_time < timeout_s:
            word = read_registers(client, STATUS_REGISTER, 1, unit_id=unit_id)[0]
            status = decode_status(word)
            if status["alarm"]:
                print("ALARM occurred during motion!", status)
                return False
            if status["in_position"] and not status["running"]:
                elapsed = time.time() - start_time
                print(f"Move completed successfully in {elapsed:.2f}s.", status)
                return True
            time.sleep(0.15)

        print(f"Timed out after {timeout_s:.1f}s waiting for move to complete.")
        return False

    except KeyboardInterrupt:
        print("\n[Ctrl+C] detected during motion polling!")
        emergency_stop(client, unit_id=unit_id)
        raise


def print_communication_troubleshooting(port, unit_id):
    """Prints clear, friendly troubleshooting instructions when communication fails."""
    print("\n" + "!" * 70)
    print(f" COMMUNICATION ERROR: No response from motor on {port} (Unit ID: {unit_id})")
    print("!" * 70)
    print("\nPossible Causes & Solutions:")
    print(f" 1. WRONG UNIT ID: The motor may be configured as Unit ID 1 (or another ID).")
    print(f"    -> Run:  python hardware/ess17_rs04_rotate_10_revs.py --scan")
    print(f"    -> Or try:  python hardware/ess17_rs04_rotate_10_revs.py --unit-id 1")
    print(f" 2. MOTOR POWER OFF: Ensure the 24V/48V DC power supply is ON.")
    print(f" 3. WRONG COM PORT / BUSY PORT: Verify {port} in Windows Device Manager.")
    print(f"    -> Close any other serial monitors or apps holding {port}.")
    print(f" 4. RS485 WIRING: Check RS485 A+ and B- connections (swap A/B if unsure).")
    print("!" * 70 + "\n")


def main():
    parser = argparse.ArgumentParser(description="ESS17-RS04 NEMA 17 Stepper Demo & E-Stop / Auto-Scan Utility")
    parser.add_argument("--port", default=DEFAULT_SERIAL_PORT, help=f"Serial port (default: {DEFAULT_SERIAL_PORT})")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUDRATE, help=f"Baud rate (default: {DEFAULT_BAUDRATE})")
    parser.add_argument("--unit-id", type=int, default=DEFAULT_UNIT_ID, help=f"Modbus Unit ID (default: {DEFAULT_UNIT_ID})")
    parser.add_argument("--revs", type=float, default=NUM_REVS, help=f"Number of revolutions per leg (default: {NUM_REVS})")
    parser.add_argument("--speed", type=int, default=DEFAULT_VELOCITY_RPM, help=f"Target speed in RPM (default: {DEFAULT_VELOCITY_RPM})")
    parser.add_argument("--pause", type=float, default=2.0, help="Pause time between forward and backward moves in seconds (default: 2.0)")
    parser.add_argument("--scan", action="store_true", help="Scan Modbus bus for responding slave Unit IDs (1-10)")
    parser.add_argument("--estop", action="store_true", help="Send immediate emergency stop command to motor and exit")

    args = parser.parse_args()

    if args.scan:
        scan_bus(port=args.port, baudrate=args.baud)
        return

    client = create_client(port=args.port, baudrate=args.baud)
    print(f"Connecting to ESS17-RS04 on {args.port} (Unit ID: {args.unit_id}, Baud: {args.baud})...")

    if not client.connect():
        print(f"ERROR: Could not open serial port {args.port}. Check port connection.")
        sys.exit(1)

    print("Connected successfully.")

    try:
        if args.estop:
            emergency_stop(client, unit_id=args.unit_id)
            return

        # Step 1: Rotate Forward (e.g. +10 revolutions)
        print(f"\n==========================================")
        print(f"  STEP 1: Forward Rotation (+{args.revs} Revs)")
        print(f"==========================================")
        success_fwd = rotate_relative(
            client=client,
            num_revolutions=args.revs,
            velocity_rpm=args.speed,
            unit_id=args.unit_id,
        )

        if not success_fwd:
            print("\nForward move failed or aborted. Skipping backward move.")
            return

        # Step 2: Pause between moves
        print(f"\nPausing for {args.pause} second(s) before reversing (Ctrl+C to abort)...")
        time.sleep(args.pause)

        # Step 3: Rotate Backward (e.g. -10 revolutions)
        print(f"\n==========================================")
        print(f"  STEP 2: Backward Rotation (-{args.revs} Revs)")
        print(f"==========================================")
        success_bwd = rotate_relative(
            client=client,
            num_revolutions=-args.revs,
            velocity_rpm=args.speed,
            unit_id=args.unit_id,
        )

        if success_bwd:
            print("\n==========================================")
            print("  DEMO COMPLETED SUCCESSFULLY!")
            print("==========================================")
        else:
            print("\nBackward move failed or timed out.")

    except (ModbusIOException, ModbusException) as e:
        print_communication_troubleshooting(args.port, args.unit_id)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nExecution interrupted by user. Script aborted safely.")
        sys.exit(1)
    finally:
        client.close()
        print("Connection closed.")


if __name__ == "__main__":
    main()
