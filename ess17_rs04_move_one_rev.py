"""
ESS17-RS04 - Rotate exactly one revolution, safely
----------------------------------------------------
Adapted from the iDM57-RS23 script. The ESS17-RS04 is StepperOnline's
NEMA17 integrated closed-loop stepper (RS485/Modbus, "Bus Series").
It uses the same PR-path Modbus register map as the iDM-RS series
(confirmed against the iDM-RS manual: Pr9.00-Pr9.07 map to the same
addresses 0x6200-0x6207 in both product families), so this script is
structurally identical - only motor-specific values differ.

This will NOT move the motor if a fault is active or the drive is disabled.

Install dependency first:
    pip install pymodbus

Run:
    python ess17_rs04_move_one_rev.py

IMPORTANT - please confirm before running:
  - PULSES_PER_REV below matches your drive's actual Pr0.00 setting.
  - BAUDRATE/UNIT_ID match your drive's configured communication
    parameters (check DIP switches / parameter table on your unit).
  - TEST_VELOCITY_RPM is conservative for a first test on this motor
    (0.48 N.m holding torque - smaller than the RS23 used previously).
"""

import time
from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusException

# ---------------------------------------------------------------------------
# Configuration - adjust these to match your setup
# ---------------------------------------------------------------------------
SERIAL_PORT = "COM3"
BAUDRATE = 115200
UNIT_ID = 2
TIMEOUT = 2

# ---------------------------------------------------------------------------
# Registers - same PR-path Modbus map as the iDM-RS series
# (StepperOnline's "Bus Series" Modbus manual is shared across the
# integrated stepper/servo product line, including ESS-RS models)
# ---------------------------------------------------------------------------
STATUS_REGISTER = 4099        # 0x1003 - Motion state bitmask

# PR Path 0 registers ("PR Path Configuration" section)
REG_CONTROL_WORD = 0x6200     # Pr9.00 - PR path 0 control/mode word
REG_POSITION_H = 0x6201       # Pr9.01 - Position, high 16 bits
REG_POSITION_L = 0x6202       # Pr9.02 - Position, low 16 bits
REG_VELOCITY = 0x6203         # Pr9.03 - velocity, unit: rpm
REG_ACC = 0x6204              # Pr9.04 - acceleration, unit: ms/1000rpm
REG_DEC = 0x6205              # Pr9.05 - deceleration, unit: ms/1000rpm
REG_PAUSE = 0x6206            # Pr9.06 - pause time after command stops
REG_TRIGGER = 0x6207          # Pr9.07 - special parameter, mirrors Pr8.02.
                              # Writing 0x0010 here fires the move
                              # ("Immediate Trigger" method).

# ---------------------------------------------------------------------------
# Move parameters - conservative values for a first-ever test on this motor
# ---------------------------------------------------------------------------
PULSES_PER_REV = 10000          # Pr0.00 default - CONFIRM this matches your
                                # drive's actual setting before running.
                                # (ESS17-RS04 has a 1000-line/1000PPR encoder,
                                # but the electronic gear ratio - not the raw
                                # encoder count - determines pulses/rev here.)
TEST_VELOCITY_RPM = 20           # slower than the RS23 test, since this is a
                                  # smaller NEMA17 motor (0.48 N.m holding torque)
TEST_ACCEL_MS_PER_1000RPM = 300  # gentle ramp up
TEST_DECEL_MS_PER_1000RPM = 300  # gentle ramp down

# Control word: bit0=1 (position mode) + bit6=1 (relative move)
# Relative move = moves from wherever the shaft currently is, rather than
# to an absolute coordinate - safer when you haven't verified home position.
CONTROL_WORD_RELATIVE_POSITION_MOVE = (1 << 0) | (1 << 6)  # = 0x0041

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
        "faulty": bool(word & (1 << 0)),
        "enabled": bool(word & (1 << 1)),
        "running": bool(word & (1 << 2)),
        "command_completed": bool(word & (1 << 4)),
        "path_completed": bool(word & (1 << 5)),
        "homing_completed": bool(word & (1 << 6)),
    }


def check_status():
    word = read_registers(STATUS_REGISTER, 1)[0]
    status = decode_status(word)
    print(f"Motion state: {status}")

    if status["faulty"]:
        print("FAULT ACTIVE - aborting. Clear the fault before retrying.")
        return False, status
    if not status["enabled"]:
        print("Drive is DISABLED - aborting. Check DI1/enable source.")
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
          f"accel/decel {TEST_ACCEL_MS_PER_1000RPM}/{TEST_DECEL_MS_PER_1000RPM} ms per 1000rpm")

    # Control word written first, then position/velocity/acc/dec/pause,
    # and the trigger register written LAST and separately - so nothing
    # moves until every other parameter is already in place.
    write_registers(REG_CONTROL_WORD, [
        CONTROL_WORD_RELATIVE_POSITION_MOVE,   # 0x6200
    ])
    write_registers(REG_POSITION_H, [
        position_h,                    # 0x6201
        position_l,                    # 0x6202
        TEST_VELOCITY_RPM,             # 0x6203
        TEST_ACCEL_MS_PER_1000RPM,     # 0x6204
        TEST_DECEL_MS_PER_1000RPM,     # 0x6205
        0,                             # 0x6206 pause time
    ])

    print("Parameters written. Triggering move now...")
    write_registers(REG_TRIGGER, [0x0010])  # 0x6207 - fires the move

    # Poll status until the path completes or we hit a timeout
    timeout_s = 15
    start = time.time()
    while time.time() - start < timeout_s:
        word = read_registers(STATUS_REGISTER, 1)[0]
        status = decode_status(word)
        if status["faulty"]:
            print("FAULT occurred during motion!", status)
            return
        if status["path_completed"] and not status["running"]:
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