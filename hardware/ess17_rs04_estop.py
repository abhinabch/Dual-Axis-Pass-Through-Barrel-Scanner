"""
ESS17-RS04 (NEMA 17) - Standalone Emergency Stop (E-Stop) Utility
------------------------------------------------------------------
Immediately halts any active movement on the ESS17-RS04 stepper drive
by issuing Modbus stop and motor release commands.

Usage:
    python hardware/ess17_rs04_estop.py [--port COM3] [--unit-id 2]
"""

import argparse
import sys
from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusException

DEFAULT_SERIAL_PORT = "COM3"
DEFAULT_BAUDRATE = 115200
DEFAULT_UNIT_ID = 2

REG_MOVEMENT_CONTROL = 0x0027   # Movement control command (write-only)
REG_AUX_CONTROL = 0x002D        # Auxiliary control command (write-only)

STOP_MOVE_CMD = 0x0000          # 0x0000 = stop motion
AUX_RELEASE_MOTOR_CMD = 0x0001  # 0x0001 = release / disable motor power


def main():
    parser = argparse.ArgumentParser(description="ESS17-RS04 Standalone Emergency Stop")
    parser.add_argument("--port", default=DEFAULT_SERIAL_PORT, help=f"Serial port (default: {DEFAULT_SERIAL_PORT})")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUDRATE, help=f"Baud rate (default: {DEFAULT_BAUDRATE})")
    parser.add_argument("--unit-id", type=int, default=DEFAULT_UNIT_ID, help=f"Modbus Unit ID (default: {DEFAULT_UNIT_ID})")
    args = parser.parse_args()

    client = ModbusSerialClient(
        port=args.port,
        baudrate=args.baud,
        parity="N",
        stopbits=1,
        bytesize=8,
        timeout=2,
    )

    print(f"Connecting to ESS17-RS04 on {args.port} (Unit ID {args.unit_id})...")
    if not client.connect():
        print(f"ERROR: Could not open {args.port}. Make sure serial port is free.")
        sys.exit(1)

    print("\n!!! EXECUTING EMERGENCY STOP !!!")
    try:
        # 1. Stop active motion
        res1 = client.write_registers(address=REG_MOVEMENT_CONTROL, values=[STOP_MOVE_CMD], device_id=args.unit_id)
        if res1.isError():
            print(f"Warning: Write to 0x0027 returned error: {res1}")
        else:
            print("-> Motion Stop command sent (0x0000 -> Reg 0x0027).")

        # 2. Auxiliary release motor output stage
        res2 = client.write_registers(address=REG_AUX_CONTROL, values=[AUX_RELEASE_MOTOR_CMD], device_id=args.unit_id)
        if res2.isError():
            print(f"Warning: Write to 0x002D returned error: {res2}")
        else:
            print("-> Motor Release command sent (0x0001 -> Reg 0x002D).")

        print("Emergency stop complete. Motor is stopped and released.")

    except Exception as e:
        print(f"E-Stop Exception: {e}")
    finally:
        client.close()
        print("Connection closed.")


if __name__ == "__main__":
    main()
