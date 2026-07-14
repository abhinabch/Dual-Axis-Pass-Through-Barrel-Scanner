"""
iDM57-RS23 Modbus RTU pre-motion check
---------------------------------------
This script connects to the motor over RS485, verifies communication,
and checks for an active fault BEFORE any move command is attempted.

Install dependency first:
    pip install pymodbus

Run:
    python idm57_rs23_modbus_check.py
"""

from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusException

# ---------------------------------------------------------------------------
# Configuration - adjust these to match your setup
# ---------------------------------------------------------------------------
SERIAL_PORT = "COM3"        # Windows: "COM3", "COM4", etc.
                             # Linux/Raspberry Pi: "/dev/ttyUSB0"
BAUDRATE = 115200            # Must match the motor's DIP switch setting
                             # (SW6-SW7). Factory default is 115200.
UNIT_ID = 1                  # The motor's slave address (Unit-ID)
TIMEOUT = 2                  # Seconds to wait for a response

# Registers from the iDM-RS series manual
TEST_REGISTER = 444          # 0x01BC - manufacturer's own worked example,
                             # good for a basic "is the link alive" check
ALARM_REGISTER = 4097        # 0x1001 - current fault/alarm status
                             # 0 = no active fault, nonzero = fault code

# ---------------------------------------------------------------------------
# Client setup
# ---------------------------------------------------------------------------
client = ModbusSerialClient(
    port=SERIAL_PORT,
    baudrate=BAUDRATE,
    parity="N",
    stopbits=1,
    bytesize=8,
    timeout=TIMEOUT,
)


def connect():
    """Open the serial connection to the motor."""
    if not client.connect():
        raise ConnectionError(
            f"Could not open {SERIAL_PORT}. Check the port name, that "
            f"nothing else has it open, and that the RS485 adapter is "
            f"plugged in."
        )
    print(f"Connected to {SERIAL_PORT} at {BAUDRATE} baud.")


def read_registers(address, count=1):
    """Read `count` holding registers starting at `address`. Returns a list of ints."""
    result = client.read_holding_registers(address=address, count=count, slave=UNIT_ID)
    if result.isError():
        raise ModbusException(f"Read failed at address {address}: {result}")
    return result.registers


def check_connection():
    """Quick sanity read using the manual's known-good test address."""
    values = read_registers(TEST_REGISTER, 6)
    print(f"Test read OK, values: {values}")
    return True


def check_alarm():
    """
    Reads the fault/alarm register.
    Returns True if the motor is fault-free and safe to proceed,
    False if a fault is active.
    """
    values = read_registers(ALARM_REGISTER, 1)
    code = values[0]
    if code == 0:
        print("No active fault - motor is clear to proceed.")
        return True
    else:
        print(f"FAULT ACTIVE - error code: {code} (0x{code:04X})")
        print("Look this code up in the Error Codes table (iDM-RS manual, "
              "page 25) before doing anything else. Clear it via "
              "STEPPERONLINE MotionStudio or the external clear-fault "
              "input before attempting any move.")
        return False


def main():
    connect()
    try:
        check_connection()

        if not check_alarm():
            print("Stopping here - do not send a move command while a "
                  "fault is active.")
            return

        print("\nMotor is connected and fault-free.")
        print("Move command logic is intentionally NOT included here yet.")
        print("Sending motion requires the drive's Profile Position/Velocity "
              "path registers (target position, velocity, acceleration) "
              "and the trigger register (Pr8.02 / 0x6002), which should be "
              "verified against the manual or MotionStudio before being "
              "written from code.")

    finally:
        client.close()
        print("Connection closed.")


if __name__ == "__main__":
    main()