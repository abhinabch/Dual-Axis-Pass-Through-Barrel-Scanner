"""
simultaneous_dual_axis_move.py
------------------------------
Simultaneous movement control script for dual-axis scanner setup:
  - ESS17-RS04 (NEMA 17 Stepper Drive, Unit ID 2): Rotates 10 revolutions
  - iDM57-RS23 (Integrated Servo/Stepper Drive, Unit ID 1): Moves 5 revolutions

Both motion commands are dispatched sequentially in rapid succession over Modbus RTU 
so that both axes execute their respective movements at the same time. A concurrent 
polling loop monitors both drives until motion completes on both axes.

Features:
  1. Safe Speed Tuning CLI Section: Custom speed controls with built-in safety bounds checking
     (ESS17-RS04 up to 300 RPM safe limit; iDM57-RS23 up to 600 RPM safe limit).
  2. Ctrl+C Intercept: Pressing Ctrl+C during motion immediately triggers Emergency Stop 
     on both motor drives.
  3. Flexible CLI arguments for custom revolutions, speeds, serial port, and slave IDs.
  4. Pre-move status & safety verification before triggering motion.
  5. Cross-version support for pymodbus (`slave` / `device_id` compatibility).

Dependencies:
    pip install pymodbus

Usage:
    python hardware/simultaneous_dual_axis_move.py [--port COM3] [--ess-speed 120] [--idm-speed 30]
"""

import argparse
import sys
import time
from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusException, ModbusIOException

# ---------------------------------------------------------------------------
# Default Configuration
# ---------------------------------------------------------------------------
DEFAULT_SERIAL_PORT = "COM3"
DEFAULT_BAUDRATE = 115200
DEFAULT_TIMEOUT = 2.0

UNIT_ID_ESS17 = 2        # ESS17-RS04 drive Unit ID
UNIT_ID_IDM57 = 1        # iDM57-RS23 drive Unit ID

PULSES_PER_REV_ESS17 = 1000   # ESS17: 1000 pulses/rev (1000 pulses = 1 rev)
PULSES_PER_REV_IDM57 = 10000  # iDM57: 10000 pulses/rev (10000 pulses = 1 rev)

# Target revolutions requested by default
DEFAULT_ESS17_REVS = 10.0
DEFAULT_IDM57_REVS = 5.0

# Velocity configuration (RPM)
# Increased ESS17 default speed to 120 RPM (2 revs/sec) for faster motion (10 revs in ~5.0s)
DEFAULT_ESS17_RPM = 120
DEFAULT_IDM57_RPM = 30

# Maximum safe speeds (hardware protection thresholds)
MAX_SAFE_ESS_RPM = 300   # ESS17-RS04 maximum recommended speed under load
MAX_SAFE_IDM_RPM = 600   # iDM57-RS23 maximum recommended speed under load
MIN_SAFE_RPM = 1

DEFAULT_ESS17_ACCEL_MS = 200
DEFAULT_ESS17_DECEL_MS = 200

DEFAULT_IDM57_ACCEL_MS = 300
DEFAULT_IDM57_DECEL_MS = 300

# ---------------------------------------------------------------------------
# Register Definitions
# ---------------------------------------------------------------------------
# ESS17-RS04 Registers
ESS17_STATUS_REG = 0x0007
ESS17_ERROR_REG = 0x0006
ESS17_REG_ACCEL = 0x0021
ESS17_REG_DECEL = 0x0022
ESS17_REG_VELOCITY = 0x0023
ESS17_REG_POSITION_H = 0x0024
ESS17_REG_POSITION_L = 0x0025
ESS17_REG_CONTROL = 0x0027       # Movement control (write 0x0001 to start relative move)
ESS17_REG_AUX_CONTROL = 0x002D   # Auxiliary control (write 0x0001 to release/disable)

# iDM57-RS23 Registers
IDM57_STATUS_REG = 4099          # 0x1003 Motion status bitmask
IDM57_ALARM_REG = 4097           # 0x1001 Alarm register
IDM57_REG_CONTROL_WORD = 0x6200  # Pr9.00 Path 0 control word
IDM57_REG_POSITION_H = 0x6201    # Pr9.01 High 16 bits
IDM57_REG_POSITION_L = 0x6202    # Pr9.02 Low 16 bits
IDM57_REG_VELOCITY = 0x6203      # Pr9.03 Velocity (RPM)
IDM57_REG_ACC = 0x6204           # Pr9.04 Accel (ms/1000rpm)
IDM57_REG_DEC = 0x6205           # Pr9.05 Decel (ms/1000rpm)
IDM57_REG_PAUSE = 0x6206         # Pr9.06 Pause time
IDM57_REG_TRIGGER = 0x6207       # Pr9.07 Write 0x0010 to trigger move
IDM57_REG_EMERGENCY_STOP = 0x6002 # Hardware stop control word (write 0x0040)

# Control mode constants
IDM57_RELATIVE_MOVE_WORD = (1 << 0) | (1 << 6)  # 0x0041 (Relative positioning)
ESS17_START_RELATIVE_MOVE = 0x0001


def create_client(port: str = DEFAULT_SERIAL_PORT, baudrate: int = DEFAULT_BAUDRATE, timeout: float = DEFAULT_TIMEOUT) -> ModbusSerialClient:
    """Initialize Modbus RTU serial client."""
    return ModbusSerialClient(
        port=port,
        baudrate=baudrate,
        parity="N",
        stopbits=1,
        bytesize=8,
        timeout=timeout,
    )


def read_registers(client: ModbusSerialClient, address: int, count: int = 1, unit_id: int = 1):
    """
    Read holding registers with version compatibility for pymodbus (slave vs device_id).
    """
    try:
        result = client.read_holding_registers(address=address, count=count, device_id=unit_id)
    except TypeError:
        result = client.read_holding_registers(address=address, count=count, slave=unit_id)

    if result is None or result.isError():
        raise ModbusIOException(f"Read failure at address 0x{address:04X} on Unit ID {unit_id}: {result}")
    return result.registers


def write_registers(client: ModbusSerialClient, address: int, values: list, unit_id: int = 1):
    """
    Write holding registers with version compatibility for pymodbus (slave vs device_id).
    """
    try:
        result = client.write_registers(address=address, values=values, device_id=unit_id)
    except TypeError:
        result = client.write_registers(address=address, values=values, slave=unit_id)

    if result is None or result.isError():
        raise ModbusIOException(f"Write failure at address 0x{address:04X} on Unit ID {unit_id}: {result}")
    return result


def decode_ess17_status(word: int) -> dict:
    """Decode ESS17-RS04 status word."""
    return {
        "in_position": bool(word & (1 << 0)),
        "running": bool(word & (1 << 2)),
        "alarm": bool(word & (1 << 3)),
        "motor_released": bool(word & (1 << 4)),
    }


def decode_idm57_status(word: int) -> dict:
    """Decode iDM57-RS23 status word."""
    return {
        "faulty": bool(word & (1 << 0)),
        "enabled": bool(word & (1 << 1)),
        "running": bool(word & (1 << 2)),
        "path_completed": bool(word & (1 << 5)),
    }


def emergency_stop_all(client: ModbusSerialClient, unit_ess: int = UNIT_ID_ESS17, unit_idm: int = UNIT_ID_IDM57):
    """
    Triggers an immediate emergency stop on both motors.
    """
    print("\n" + "!" * 60)
    print(" !!! EMERGENCY STOP TRIGGERED FOR BOTH AXES !!!")
    print("!" * 60)

    # Halt ESS17-RS04
    try:
        write_registers(client, ESS17_REG_CONTROL, [0x0000], unit_id=unit_ess)
        write_registers(client, ESS17_REG_AUX_CONTROL, [0x0001], unit_id=unit_ess)
        print(" -> ESS17-RS04 (Unit ID %d): Stop & Release sent." % unit_ess)
    except Exception as e:
        print(" -> ESS17-RS04 E-Stop warning: %s" % e)

    # Halt iDM57-RS23
    try:
        write_registers(client, IDM57_REG_EMERGENCY_STOP, [0x0040], unit_id=unit_idm)
        print(" -> iDM57-RS23 (Unit ID %d): Stop command sent." % unit_idm)
    except Exception as e:
        print(" -> iDM57-RS23 E-Stop warning: %s" % e)


def verify_drives_ready(client: ModbusSerialClient, unit_ess: int, unit_idm: int) -> bool:
    """
    Verify both ESS17 and iDM57 drives are powered, connected, and fault-free before move.
    """
    print("\n--- Verifying Drive Status before Motion ---")
    
    # Check ESS17-RS04
    try:
        word_ess = read_registers(client, ESS17_STATUS_REG, 1, unit_id=unit_ess)[0]
        status_ess = decode_ess17_status(word_ess)
        print(f"ESS17-RS04 (Unit ID {unit_ess}) Status: {status_ess}")
        if status_ess["alarm"]:
            err = read_registers(client, ESS17_ERROR_REG, 1, unit_id=unit_ess)[0]
            print(f"ERROR: ESS17-RS04 has active alarm code {err}. Clear fault before moving.")
            return False
        if status_ess["motor_released"]:
            print("ERROR: ESS17-RS04 motor is released (disabled).")
            return False
    except Exception as e:
        print(f"ERROR: Failed communicating with ESS17-RS04 (Unit ID {unit_ess}): {e}")
        return False

    # Check iDM57-RS23
    try:
        word_idm = read_registers(client, IDM57_STATUS_REG, 1, unit_id=unit_idm)[0]
        status_idm = decode_idm57_status(word_idm)
        print(f"iDM57-RS23 (Unit ID {unit_idm}) Status: {status_idm}")
        if status_idm["faulty"]:
            alarm = read_registers(client, IDM57_ALARM_REG, 1, unit_id=unit_idm)[0]
            print(f"ERROR: iDM57-RS23 has active fault (alarm register: {alarm}). Clear fault before moving.")
            return False
        if not status_idm["enabled"]:
            print("ERROR: iDM57-RS23 drive is not enabled.")
            return False
    except Exception as e:
        print(f"ERROR: Failed communicating with iDM57-RS23 (Unit ID {unit_idm}): {e}")
        return False

    return True


def validate_speed(speed: int, max_safe: int, axis_name: str) -> int:
    """
    Validates requested speed against safety bounds. Caps at max_safe with warning if exceeded.
    """
    if speed < MIN_SAFE_RPM:
        print(f"WARNING: Speed {speed} RPM is below min limit. Setting {axis_name} to {MIN_SAFE_RPM} RPM.")
        return MIN_SAFE_RPM
    if speed > max_safe:
        print(f"WARNING: Requested {axis_name} speed ({speed} RPM) exceeds maximum safe limit ({max_safe} RPM).")
        print(f"  -> Capping {axis_name} speed to safe limit of {max_safe} RPM to protect hardware.")
        return max_safe
    return speed


def move_simultaneous(
    client: ModbusSerialClient,
    ess_revs: float = DEFAULT_ESS17_REVS,
    idm_revs: float = DEFAULT_IDM57_REVS,
    ess_rpm: int = DEFAULT_ESS17_RPM,
    idm_rpm: int = DEFAULT_IDM57_RPM,
    unit_ess: int = UNIT_ID_ESS17,
    unit_idm: int = UNIT_ID_IDM57,
):
    """
    Commands simultaneous relative moves for both ESS17-RS04 and iDM57-RS23 drives.
    """
    # Enforce safe speed limits
    ess_rpm = validate_speed(ess_rpm, MAX_SAFE_ESS_RPM, "ESS17-RS04")
    idm_rpm = validate_speed(idm_rpm, MAX_SAFE_IDM_RPM, "iDM57-RS23")

    if not verify_drives_ready(client, unit_ess=unit_ess, unit_idm=unit_idm):
        print("Pre-move safety checks failed. Movement sequence aborted.")
        return False

    ess_target_pulses = int(ess_revs * PULSES_PER_REV_ESS17)
    idm_target_pulses = int(idm_revs * PULSES_PER_REV_IDM57)

    print("\n" + "=" * 65)
    print(f"  INITIATING SIMULTANEOUS DUAL-AXIS MOTION")
    print("=" * 65)
    print(f"  Axis 1 (ESS17-RS04, Unit ID {unit_ess}): {ess_revs} revs ({ess_target_pulses} pulses) @ {ess_rpm} RPM")
    print(f"  Axis 2 (iDM57-RS23, Unit ID {unit_idm}): {idm_revs} revs ({idm_target_pulses} pulses) @ {idm_rpm} RPM")
    print(">>> Press [Ctrl+C] at any time to trigger EMERGENCY STOP on both axes <<<")
    print("-" * 65)

    try:
        # -------------------------------------------------------------------
        # Step 1: Program move parameters for ESS17-RS04
        # -------------------------------------------------------------------
        ess_u32 = ess_target_pulses & 0xFFFFFFFF
        ess_pos_h = (ess_u32 >> 16) & 0xFFFF
        ess_pos_l = ess_u32 & 0xFFFF

        print("Writing ESS17-RS04 motion parameters...")
        write_registers(client, ESS17_REG_ACCEL, [
            DEFAULT_ESS17_ACCEL_MS,
            DEFAULT_ESS17_DECEL_MS,
            ess_rpm,
            ess_pos_h,
            ess_pos_l,
        ], unit_id=unit_ess)

        # -------------------------------------------------------------------
        # Step 2: Program move parameters for iDM57-RS23
        # -------------------------------------------------------------------
        idm_u32 = idm_target_pulses & 0xFFFFFFFF
        idm_pos_h = (idm_u32 >> 16) & 0xFFFF
        idm_pos_l = idm_u32 & 0xFFFF

        print("Writing iDM57-RS23 motion parameters...")
        write_registers(client, IDM57_REG_CONTROL_WORD, [IDM57_RELATIVE_MOVE_WORD], unit_id=unit_idm)
        write_registers(client, IDM57_REG_POSITION_H, [
            idm_pos_h,
            idm_pos_l,
            idm_rpm,
            DEFAULT_IDM57_ACCEL_MS,
            DEFAULT_IDM57_DECEL_MS,
            0,  # pause time
        ], unit_id=unit_idm)

        # -------------------------------------------------------------------
        # Step 3: Rapid Trigger Sequence (Start both movements immediately)
        # -------------------------------------------------------------------
        print("Dispatching triggers to both drives...")
        start_time = time.time()
        
        # Trigger ESS17-RS04
        write_registers(client, ESS17_REG_CONTROL, [ESS17_START_RELATIVE_MOVE], unit_id=unit_ess)
        
        # Trigger iDM57-RS23
        write_registers(client, IDM57_REG_TRIGGER, [0x0010], unit_id=unit_idm)
        
        print("Both movement triggers sent successfully! Monitoring execution...")

        # -------------------------------------------------------------------
        # Step 4: Concurrent Polling Loop
        # -------------------------------------------------------------------
        # Estimate expected time based on slowest move duration
        ess_est_time = (abs(ess_revs) / (ess_rpm / 60.0)) + 0.5
        idm_est_time = (abs(idm_revs) / (idm_rpm / 60.0)) + 0.6
        max_expected = max(ess_est_time, idm_est_time)
        timeout_s = max(30.0, max_expected + 15.0)

        ess_done = False
        idm_done = False

        while time.time() - start_time < timeout_s:
            elapsed = time.time() - start_time

            # Poll ESS17-RS04 status if not yet completed
            if not ess_done:
                word_ess = read_registers(client, ESS17_STATUS_REG, 1, unit_id=unit_ess)[0]
                st_ess = decode_ess17_status(word_ess)
                if st_ess["alarm"]:
                    print(f"\n[ALARM] ESS17-RS04 reported alarm during motion! Stopping both.")
                    emergency_stop_all(client, unit_ess=unit_ess, unit_idm=unit_idm)
                    return False
                if st_ess["in_position"] and not st_ess["running"]:
                    ess_done = True
                    print(f" [{elapsed:5.2f}s] Axis 1 (ESS17-RS04): {ess_revs} Revs Complete!")

            # Poll iDM57-RS23 status if not yet completed
            if not idm_done:
                word_idm = read_registers(client, IDM57_STATUS_REG, 1, unit_id=unit_idm)[0]
                st_idm = decode_idm57_status(word_idm)
                if st_idm["faulty"]:
                    print(f"\n[FAULT] iDM57-RS23 reported fault during motion! Stopping both.")
                    emergency_stop_all(client, unit_ess=unit_ess, unit_idm=unit_idm)
                    return False
                if st_idm["path_completed"] and not st_idm["running"]:
                    idm_done = True
                    print(f" [{elapsed:5.2f}s] Axis 2 (iDM57-RS23): {idm_revs} Revs Complete!")

            if ess_done and idm_done:
                total_time = time.time() - start_time
                print("=" * 65)
                print(f"  SUCCESS: Dual-axis move finished in {total_time:.2f} seconds!")
                print("=" * 65)
                return True

            time.sleep(0.15)

        print(f"\n[TIMEOUT] Motion loop timed out after {timeout_s:.1f}s.")
        emergency_stop_all(client, unit_ess=unit_ess, unit_idm=unit_idm)
        return False

    except KeyboardInterrupt:
        print("\n[Ctrl+C Intercepted] User interrupted motion execution!")
        emergency_stop_all(client, unit_ess=unit_ess, unit_idm=unit_idm)
        raise


def main():
    parser = argparse.ArgumentParser(
        description="Simultaneous dual-axis movement program for ESS17-RS04 & iDM57-RS23 drives."
    )

    # General Connection Settings
    conn_group = parser.add_argument_group("Communication Options")
    conn_group.add_argument("--port", default=DEFAULT_SERIAL_PORT, help=f"Serial port (default: {DEFAULT_SERIAL_PORT})")
    conn_group.add_argument("--baud", type=int, default=DEFAULT_BAUDRATE, help=f"Baud rate (default: {DEFAULT_BAUDRATE})")
    conn_group.add_argument("--ess-unit", type=int, default=UNIT_ID_ESS17, help=f"ESS17 slave Unit ID (default: {UNIT_ID_ESS17})")
    conn_group.add_argument("--idm-unit", type=int, default=UNIT_ID_IDM57, help=f"iDM57 slave Unit ID (default: {UNIT_ID_IDM57})")

    # Revolution Settings
    move_group = parser.add_argument_group("Revolution Options")
    move_group.add_argument("--ess-revs", type=float, default=DEFAULT_ESS17_REVS, help=f"ESS17 target revolutions (default: {DEFAULT_ESS17_REVS})")
    move_group.add_argument("--idm-revs", type=float, default=DEFAULT_IDM57_REVS, help=f"iDM57 target revolutions (default: {DEFAULT_IDM57_REVS})")

    # Speed & Motion Safety Configuration Section
    speed_group = parser.add_argument_group("Speed & Motion Safety Tuning (RPM)")
    speed_group.add_argument(
        "--ess-speed", "--speed-ess", type=int, default=DEFAULT_ESS17_RPM,
        help=f"ESS17-RS04 motor speed in RPM (default: {DEFAULT_ESS17_RPM} RPM, max safe: {MAX_SAFE_ESS_RPM} RPM)"
    )
    speed_group.add_argument(
        "--idm-speed", "--speed-idm", type=int, default=DEFAULT_IDM57_RPM,
        help=f"iDM57-RS23 motor speed in RPM (default: {DEFAULT_IDM57_RPM} RPM, max safe: {MAX_SAFE_IDM_RPM} RPM)"
    )
    speed_group.add_argument(
        "--speed", "-s", type=int, default=None,
        help="Quick override to set both ESS17 and iDM57 motor speeds to the same RPM value."
    )

    args = parser.parse_args()

    # Handle quick --speed override if provided
    ess_speed = args.speed if args.speed is not None else args.ess_speed
    idm_speed = args.speed if args.speed is not None else args.idm_speed

    client = create_client(port=args.port, baudrate=args.baud)
    print(f"Connecting to serial port {args.port} @ {args.baud} baud...")

    if not client.connect():
        print(f"ERROR: Unable to open serial port '{args.port}'. Ensure USB-to-RS485 adapter is plugged in.")
        sys.exit(1)

    try:
        move_simultaneous(
            client=client,
            ess_revs=args.ess_revs,
            idm_revs=args.idm_revs,
            ess_rpm=ess_speed,
            idm_rpm=idm_speed,
            unit_ess=args.ess_unit,
            unit_idm=args.idm_unit,
        )
    except ModbusException as me:
        print(f"\nModbus Communication Failure: {me}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nProgram stopped by user.")
        sys.exit(1)
    finally:
        client.close()
        print("Serial connection closed.")


if __name__ == "__main__":
    main()
