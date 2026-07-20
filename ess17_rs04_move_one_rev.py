"""
ESS17-RS04 - Rotate exactly one revolution, safely
----------------------------------------------------
Register map confirmed against StepperOnline's official
"Modbus Series Bus Driver Function Manual" (Appendix 2: ESS-RS Series).

This is a DIFFERENT register map than the iDM57-RS23 script this was
originally adapted from - the iDM-RS "PR Path 0" registers (0x6200-0x6207)
do NOT apply to this drive. The ESS-RS family uses its own, simpler set
of registers in the 0x0000-0x0030 range, confirmed directly from the
manual you provided.

This will NOT move the motor if a fault is active or the drive is released
(disabled).

Install dependency first:
    pip install pymodbus

Run:
    python ess17_rs04_move_one_rev.py

IMPORTANT - please confirm before running:
  - PULSES_PER_REV below (4000) matches Modbus register 0x0101
    ("Encoder resolution") on your actual drive. The manual states this
    defaults to 4x the encoder line count (1000-line encoder -> 4000),
    which matches the ESS17-RS04 datasheet's 1000 PPR encoder - but
    please verify by reading 0x0101 before relying on it.
  - TEST_VELOCITY_RPM is conservative for a first test.
"""

import time
from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusException

# ---------------------------------------------------------------------------
# Configuration - adjust these to match your setup
# ---------------------------------------------------------------------------
SERIAL_PORT = "COM3"
BAUDRATE = 115200
UNIT_ID = 2          # confirmed slave ID for this drive
TIMEOUT = 2

# ---------------------------------------------------------------------------
# Registers - confirmed from "Modbus Series Bus Driver Function Manual",
# Appendix 2: Modbus Register Parameter Table - ESS-RS Series
# ---------------------------------------------------------------------------
STATUS_REGISTER = 0x0007        # Motion status bit (read-only)
ERROR_CODE_REGISTER = 0x0006    # 0 = normal, 1-5 = error (read-only)

REG_STARTING_SPEED = 0x0020     # Starting speed of positioning move, r/min
REG_ACCEL = 0x0021              # Acceleration time, ms
REG_DECEL = 0x0022              # Deceleration time, ms
REG_VELOCITY = 0x0023           # Positioning movement speed, r/min
REG_POSITION_H = 0x0024         # Total pulse count, high 16 bits
REG_POSITION_L = 0x0025         # Total pulse count, low 16 bits
REG_MOVEMENT_CONTROL = 0x0027   # Movement control command (write-only)
REG_AUX_CONTROL = 0x002D        # Auxiliary control command (write-only)

# ---------------------------------------------------------------------------
# Move parameters - conservative values for a first-ever test on this motor
# ---------------------------------------------------------------------------
PULSES_PER_REV = 4000            # CONFIRM against register 0x0101 (Encoder
                                  # resolution) on your actual drive before
                                  # relying on this for a full revolution.
STARTING_SPEED_RPM = 10          # starting speed for the trapezoidal ramp
TEST_VELOCITY_RPM = 20           # slow test speed, r/min
TEST_ACCEL_MS = 200              # acceleration time, ms (plain ms on this
TEST_DECEL_MS = 200              # drive - not "ms per 1000rpm")

# Movement control word (register 0x0027) bit values:
#   bit0 = start position-mode move
#   bit2 = 0 -> relative positioning, 1 -> absolute positioning
START_RELATIVE_POSITION_MOVE = 0x0001   # bit0=1, bit2=0 (relative)

client = ModbusSerialClient(
    port=SERIAL_PORT,
    baudrate=BAUDRATE,
    parity="N",
    stopbits=1,
    bytesize=8,
    timeout=TIMEOUT,
)


def connect():
    if not client.connect():
        raise ConnectionError(f"Could not open {SERIAL_PORT}.")
    print(f"Connected to {SERIAL_PORT} at {BAUDRATE} baud.")


def read_registers(address, count=1):
    result = client.read_holding_registers(address=address, count=count, device_id=UNIT_ID)
    if result.isError():
        raise ModbusException(f"Read failed at address {address}: {result}")
    return result.registers


def write_registers(address, values):
    result = client.write_registers(address=address, values=values, device_id=UNIT_ID)
    if result.isError():
        raise ModbusException(f"Write failed at address {address}: {result}")
    return result


def decode_status(word):
    return {
        "in_position": bool(word & (1 << 0)),
        "homing_completed": bool(word & (1 << 1)),
        "running": bool(word & (1 << 2)),
        "alarm": bool(word & (1 << 3)),
        "motor_released": bool(word & (1 << 4)),  # True = disabled/released
        "positive_soft_limit": bool(word & (1 << 5)),
        "negative_soft_limit": bool(word & (1 << 6)),
    }


def check_status():
    word = read_registers(STATUS_REGISTER, 1)[0]
    status = decode_status(word)
    print(f"Motion state: {status}")

    if status["alarm"]:
        error_code = read_registers(ERROR_CODE_REGISTER, 1)[0]
        print(f"ALARM ACTIVE (error code {error_code}) - aborting. Clear the fault before retrying.")
        return False, status
    if status["motor_released"]:
        print("Motor is RELEASED (disabled) - aborting. Enable the motor before retrying.")
        return False, status

    return True, status


def rotate_one_revolution():
    ok, _ = check_status()
    if not ok:
        print("Pre-move check failed. Move NOT sent.")
        return

    if read_registers(STATUS_REGISTER, 1)[0] & (1 << 2):
        print("Motor is already running - aborting to avoid overlapping commands.")
        return

    position_value = PULSES_PER_REV & 0xFFFFFFFF
    position_h = (position_value >> 16) & 0xFFFF
    position_l = position_value & 0xFFFF

    print(f"\nWriting move parameters: "
          f"{PULSES_PER_REV} pulses (1 rev), "
          f"{TEST_VELOCITY_RPM} rpm, "
          f"accel/decel {TEST_ACCEL_MS}/{TEST_DECEL_MS} ms")

    # Write acceleration, deceleration, speed, and total pulse count
    # (registers 0x0021-0x0025) in one block, matching the manual's own
    # example sequence. The movement control register (0x0027) is written
    # separately and last, so nothing moves until every parameter is set.
    write_registers(REG_ACCEL, [
        TEST_ACCEL_MS,      # 0x0021
        TEST_DECEL_MS,      # 0x0022
        TEST_VELOCITY_RPM,  # 0x0023
        position_h,         # 0x0024
        position_l,         # 0x0025
    ])

    print("Parameters written. Triggering move now...")
    write_registers(REG_MOVEMENT_CONTROL, [START_RELATIVE_POSITION_MOVE])  # 0x0027

    # Poll status until the move completes or we hit a timeout
    timeout_s = 15
    start = time.time()
    while time.time() - start < timeout_s:
        word = read_registers(STATUS_REGISTER, 1)[0]
        status = decode_status(word)
        if status["alarm"]:
            print("ALARM occurred during motion!", status)
            return
        if status["in_position"] and not status["running"]:
            print("Move completed successfully.", status)
            return
        time.sleep(0.2)

    print("Timed out waiting for move to complete - check the motor/status manually.")


def main():
    connect()
    try:
        rotate_one_revolution()
    finally:
        client.close()
        print("Connection closed.")


if __name__ == "__main__":
    main()