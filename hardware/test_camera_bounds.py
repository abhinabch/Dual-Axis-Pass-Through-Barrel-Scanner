"""
Camera Scanner Bounds Tester
----------------------------
This program tests the movement bounds of the tilt motor (ESS17-RS04).
1. Reads the CURRENT encoder position when the script starts and uses
   that as the initial/home position (camera facing down).
2. Moves in one direction until a keyboard button is pressed -> Bound 1.
3. Returns to the initial position.
4. Moves in the opposite direction until a keyboard button is pressed -> Bound 2.
5. Returns to the initial position.
6. Logs encoder values and revolutions at both bounds.

Dependencies:
    pip install pymodbus keyboard
"""

import time
import keyboard
from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusException

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SERIAL_PORT = "COM3"
BAUDRATE = 115200
UNIT_ID = 2
TIMEOUT = 2

# Registers (confirmed from ESS-RS Series manual)
STATUS_REGISTER = 0x0007
ERROR_CODE_REGISTER = 0x0006
REG_ACCEL = 0x0021
REG_DECEL = 0x0022
REG_VELOCITY = 0x0023
REG_POSITION_H = 0x0024
REG_POSITION_L = 0x0025
REG_MOVEMENT_CONTROL = 0x0027

# Move parameters
PULSES_PER_REV = 1000  # Based on verified 1000 PPR for this drive
TEST_VELOCITY_RPM = 15  # Slow speed for safety during bounds testing
TEST_ACCEL_MS = 200
TEST_DECEL_MS = 200

# Movement control word (register 0x0027)
# bit0 = start position-mode move
# bit2 = 0 -> relative, 1 -> absolute
START_RELATIVE_POSITION_MOVE = 0x0001  # bit0=1, bit2=0
START_ABSOLUTE_POSITION_MOVE = 0x0005  # bit0=1, bit2=1

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
        "motor_released": bool(word & (1 << 4)),
        "positive_soft_limit": bool(word & (1 << 5)),
        "negative_soft_limit": bool(word & (1 << 6)),
    }


def get_current_position():
    regs = read_registers(REG_POSITION_H, 2)
    pos_h = regs[0]
    pos_l = regs[1]
    # Handle signed 32-bit integer
    raw_pos = (pos_h << 16) | pos_l
    if raw_pos & 0x80000000:
        raw_pos -= 0x100000000
    return raw_pos


def wait_until_in_position(poll_interval=0.2):
    """Blocks until the drive reports in_position and not running."""
    while True:
        status = decode_status(read_registers(STATUS_REGISTER, 1)[0])
        if status["in_position"] and not status["running"]:
            break
        time.sleep(poll_interval)


def move_to_absolute(target_pos, wait=True):
    """Commands an absolute move to target_pos (in pulses)."""
    pos_h = (target_pos >> 16) & 0xFFFF
    pos_l = target_pos & 0xFFFF

    # Before starting a new move, ensure the motor is actually stopped
    # and we aren't trying to move to the current position.
    current_pos = get_current_position()
    if current_pos == target_pos:
        return

    write_registers(REG_ACCEL, [TEST_ACCEL_MS, TEST_DECEL_MS, TEST_VELOCITY_RPM, pos_h, pos_l])
    write_registers(REG_MOVEMENT_CONTROL, [START_ABSOLUTE_POSITION_MOVE])

    if wait:
        wait_until_in_position()


def stop_motor():
    """Stop the motor immediately by commanding an absolute move to
    wherever the motor currently is (freezes it in place)."""
    current_pos = get_current_position()
    move_to_absolute(current_pos, wait=False)


def move_continuously(direction_pulses):
    """
    Moves the motor in small increments in the specified direction
    until 'space' is pressed, then stops and returns the position
    the motor stopped at.

    IMPORTANT: a new relative-move command is only issued once the
    previous one has actually finished (checked via the status
    register). Firing a new move every poll cycle regardless of
    whether the last one completed causes commands to pile up on the
    drive, so it keeps executing queued moves well after you've
    pressed SPACE (overshoot past the bound).
    """
    step_pulses = direction_pulses * 10  # size of each incremental move
    move_in_progress = False

    print("Moving... Press 'SPACE' to stop.")
    while True:
        if keyboard.is_pressed('space'):
            print("Stop signal received!")
            stop_motor()
            break

        status = decode_status(read_registers(STATUS_REGISTER, 1)[0])

        if not move_in_progress:
            # Previous move (if any) has finished -> safe to issue the next one
            pos_h = (step_pulses >> 16) & 0xFFFF
            pos_l = step_pulses & 0xFFFF
            write_registers(REG_ACCEL, [TEST_ACCEL_MS, TEST_DECEL_MS, TEST_VELOCITY_RPM, pos_h, pos_l])
            write_registers(REG_MOVEMENT_CONTROL, [START_RELATIVE_POSITION_MOVE])
            move_in_progress = True
        elif status["in_position"] and not status["running"]:
            # The move we just issued has completed -> allow the next one
            move_in_progress = False

        # Short sleep to avoid flooding the bus and allow keyboard check
        time.sleep(0.05)

    return get_current_position()


def main():
    connect()
    try:
        # 1. Record wherever the encoder currently is as the initial/home position
        initial_pos = get_current_position()
        print(f"Initial Position: {initial_pos} pulses ({initial_pos / PULSES_PER_REV:.2f} revs)")
        print("This is being used as 'camera facing down'.")
        print("Press 'ENTER' to begin bounds test...")
        input()

        # 2. Move in one direction until SPACE -> Bound 1
        print("\n--- Testing Bound 1 (Positive Direction) ---")
        bound1_pos = move_continuously(100)  # small positive steps
        print(f"Bound 1 captured at: {bound1_pos} pulses ({bound1_pos / PULSES_PER_REV:.2f} revs)")

        # 3. Return to initial position
        print("\n--- Returning to Initial Position ---")
        move_to_absolute(initial_pos)
        print("Back at initial position.")

        # 4. Move in the other direction until SPACE -> Bound 2
        print("\n--- Testing Bound 2 (Negative Direction) ---")
        print("Press 'ENTER' to start moving towards Bound 2...")
        input()
        bound2_pos = move_continuously(-100)  # small negative steps
        print(f"Bound 2 captured at: {bound2_pos} pulses ({bound2_pos / PULSES_PER_REV:.2f} revs)")

        # 5. Return to initial position again
        print("\n--- Returning to Initial Position ---")
        move_to_absolute(initial_pos)
        print("Back at initial position.")

        # Final Report
        print("\n" + "=" * 30)
        print("BOUNDS TEST REPORT")
        print("=" * 30)
        print(f"Initial Pos: {initial_pos} ({initial_pos / PULSES_PER_REV:.2f} revs)")
        print(f"Bound 1 Pos: {bound1_pos} ({bound1_pos / PULSES_PER_REV:.2f} revs)")
        print(f"Bound 2 Pos: {bound2_pos} ({bound2_pos / PULSES_PER_REV:.2f} revs)")
        print(f"Range: {(bound1_pos - bound2_pos)} pulses ({(bound1_pos - bound2_pos) / PULSES_PER_REV:.2f} revs)")
        print("=" * 30)

    except Exception as e:
        print(f"Error: {e}")
    finally:
        client.close()
        print("Connection closed.")


if __name__ == "__main__":
    main()