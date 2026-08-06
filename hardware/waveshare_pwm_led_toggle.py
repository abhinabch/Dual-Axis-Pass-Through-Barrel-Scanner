#!/usr/bin/env python3
"""
Waveshare Modbus RTU PWM Output 4CH (SKU 33921) - Relay ON/OFF Toggle Test
---------------------------------------------------------------------------
This is a modified version of waveshare_pwm_led_demo.py for testing with a
1-Channel 5V Opto-Isolated Relay Module instead of a MOSFET driver.

WHY THIS VERSION EXISTS:
   A mechanical relay cannot switch fast enough to do real PWM dimming
   (the original demo pulses the output at 1000 Hz). Relays are mechanical
   switches and will buzz, fail to respond, or wear out quickly if driven
   at PWM speeds. So this version does NOT do 3-level brightness fading.
   Instead it simply toggles the channel fully ON (100% duty) and fully
   OFF (0% duty), holding each state for a few seconds - which is exactly
   what a relay is good at: a clean on/off switch.

   At 100% duty cycle, the PWM output is constantly HIGH (never pulses),
   and at 0% duty cycle it's constantly LOW. So this acts as a simple
   digital output toggle, safe for a relay's IN pin.

WIRING (1-Channel Opto-Isolated Relay Module):
   Control side (low voltage, talks to the PWM module):
     IN   <- PWM module's D-channel output (e.g. D1)
     GND  <- PWM module's GND (common ground with the module)
     VCC  <- 5V supply (check the relay board's current draw; the PWM
             module's output may not supply enough - use a separate 5V
             source if needed)

   Switching side (isolated, controls the actual 12V LED circuit):
     COM  <- 12V supply positive (+)
     NO   -> LED's + wire
     LED's - wire -> 12V supply ground (-)

   NOTE: Many relay boards are ACTIVE-LOW (relay closes when IN is LOW,
   not HIGH). If the LED behavior seems inverted (ON when you expect OFF,
   or vice versa), that's expected for those boards - not a wiring fault.
   This script tells you clearly which state it's requesting at each step
   so you can observe and confirm which polarity your board uses.

DEPENDENCIES:
  pip install pymodbus

USAGE:
  1. Verification / Pre-wiring check (reads address 0x4000 & firmware 0x8000):
     python waveshare_pwm_relay_toggle_demo.py --verify --port COM3

  2. Relay ON/OFF toggle test (default): toggles channel 1 ON -> OFF -> ON -> OFF
     python waveshare_pwm_relay_toggle_demo.py --port COM3

  3. Custom hold time / number of toggles / channel:
     python waveshare_pwm_relay_toggle_demo.py --port COM3 --channel 1 --hold 3 --cycles 4
"""

import argparse
import datetime
import sys
import time
from typing import List, Tuple

from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusException

# ---------------------------------------------------------------------------
# Default Configuration Constants
# ---------------------------------------------------------------------------
DEFAULT_PORT = "COM3"  # Windows: "COMx", Linux/Pi: "/dev/ttyUSB0"
DEFAULT_BAUDRATE = 9600
DEFAULT_SLAVE_ID = 1
DEFAULT_TIMEOUT = 2.0  # seconds

DEFAULT_CHANNEL = 1  # 1-indexed (1 to 4)
DEFAULT_FREQUENCY_HZ = 1000.0  # frequency is irrelevant at 0%/100% duty, kept for register writes

DEFAULT_HOLD_SEC = 2.0     # how long to hold each ON/OFF state
DEFAULT_CYCLES = 3         # number of full ON->OFF cycles to run

# Modbus Register Offsets
REG_SLAVE_ADDRESS = 0x4000
REG_VERSION_ADDRESS = 0x8000

# Modbus Exception Code Descriptions
MODBUS_EXCEPTIONS = {
    0x01: "Illegal Function (Function code not supported by slave)",
    0x02: "Illegal Data Address (Register address out of range)",
    0x03: "Illegal Data Value (Data value invalid or out of range)",
    0x04: "Slave Device Failure (Unrecoverable error occurred in slave)",
    0x05: "Acknowledge (Slave accepted request, processing async)",
    0x06: "Slave Device Busy (Slave is busy processing another command)",
}


# ---------------------------------------------------------------------------
# Helper Functions for Data Conversion
# ---------------------------------------------------------------------------
def log_msg(msg: str) -> None:
    """Print timestamped log message."""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    print(f"[{ts}] {msg}")


def get_channel_registers(channel: int) -> Tuple[int, int]:
    """Returns (freq_start_reg, duty_reg) for 1-based channel number (1 to 4)."""
    if channel < 1 or channel > 4:
        raise ValueError(f"Channel must be 1, 2, 3, or 4 (got {channel})")
    base_freq_reg = (channel - 1) * 3
    base_duty_reg = base_freq_reg + 2
    return base_freq_reg, base_duty_reg


def freq_hz_to_u32_words(freq_hz: float) -> Tuple[int, int]:
    """Convert frequency in Hz to uint32 (0.01 Hz units) split into two 16-bit words."""
    if freq_hz < 1.0 or freq_hz > 200000.0:
        raise ValueError(f"Frequency must be between 1 Hz and 200,000 Hz (got {freq_hz})")
    val_u32 = int(round(freq_hz * 100))
    high_word = (val_u32 >> 16) & 0xFFFF
    low_word = val_u32 & 0xFFFF
    return high_word, low_word


def u32_words_to_freq_hz(high_word: int, low_word: int) -> float:
    """Reconstruct frequency in Hz from two 16-bit big-endian words."""
    val_u32 = (high_word << 16) | (low_word & 0xFFFF)
    return val_u32 / 100.0


def duty_pct_to_u16(duty_pct: float) -> int:
    """Convert duty cycle percentage (0-100%) to uint16 (0.01% units, range 0-10000)."""
    if duty_pct < 0.0 or duty_pct > 100.0:
        raise ValueError(f"Duty cycle must be between 0.0% and 100.0% (got {duty_pct})")
    return int(round(duty_pct * 100))


def u16_to_duty_pct(duty_u16: int) -> float:
    """Convert uint16 duty cycle value (0-10000) to percentage."""
    return duty_u16 / 100.0


def list_available_ports() -> List[str]:
    """Scan and return list of available system serial ports."""
    try:
        import serial.tools.list_ports
        ports = serial.tools.list_ports.comports()
        return [f"{p.device} ({p.description})" for p in ports]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Modbus PWM Controller Class
# ---------------------------------------------------------------------------
class WavesharePwmController:
    def __init__(self, port: str, baudrate: int = 9600, slave_id: int = 1, timeout: float = 2.0):
        self.port = port
        self.baudrate = baudrate
        self.slave_id = slave_id
        self.timeout = timeout
        self.client = ModbusSerialClient(
            port=self.port,
            baudrate=self.baudrate,
            parity="N",
            stopbits=1,
            bytesize=8,
            timeout=self.timeout,
        )

    def connect(self) -> None:
        """Establish serial Modbus RTU connection."""
        log_msg(f"Connecting to {self.port} at {self.baudrate} 8N1 (Slave ID: {self.slave_id})...")
        if not self.client.connect():
            raise ConnectionError(
                f"Failed to open serial port {self.port}. Check cable, port name, and permissions."
            )
        log_msg(f"Connected successfully to {self.port}.")

    def is_connected(self) -> bool:
        """Check if serial connection is currently open."""
        return self.client is not None and self.client.is_socket_open()

    def close(self) -> None:
        """Close serial Modbus connection safely."""
        if self.is_connected():
            self.client.close()
            log_msg(f"Closed connection to {self.port}.")

    def _execute_transaction(self, func_name: str, action_func, *args, **kwargs):
        """Wrapper for Modbus calls providing logging and detailed error handling."""
        try:
            kwargs["device_id"] = self.slave_id
            response = action_func(*args, **kwargs)

            if response is None:
                raise ModbusException("No response received from slave (Timeout).")

            if response.isError():
                err_msg = str(response)
                if hasattr(response, "exception_code"):
                    code = response.exception_code
                    desc = MODBUS_EXCEPTIONS.get(code, f"Unknown exception code 0x{code:02X}")
                    err_msg = f"Modbus Exception 0x{code:02X}: {desc}"
                raise ModbusException(f"Transaction failed: {err_msg}")

            return response
        except Exception as e:
            log_msg(f"ERROR during {func_name}: {e}")
            raise

    def read_holding_registers(self, start_reg: int, count: int) -> List[int]:
        """Read holding registers (Function Code 0x03)."""
        log_msg(f"READ [FC 0x03] Start Reg: 0x{start_reg:04X} ({start_reg}), Count: {count}")
        res = self._execute_transaction(
            "read_holding_registers",
            self.client.read_holding_registers,
            address=start_reg,
            count=count,
        )
        values = res.registers
        log_msg(f"READ OK -> Values: {[f'0x{v:04X} ({v})' for v in values]}")
        return values

    def write_single_register(self, reg: int, value: int) -> None:
        """Write single holding register (Function Code 0x06)."""
        log_msg(f"WRITE SINGLE [FC 0x06] Reg: 0x{reg:04X} ({reg}) <- Value: 0x{value:04X} ({value})")
        self._execute_transaction(
            "write_single_register",
            self.client.write_register,
            address=reg,
            value=value,
        )
        log_msg("WRITE SINGLE OK")

    def write_multiple_registers(self, start_reg: int, values: List[int]) -> None:
        """Write multiple holding registers (Function Code 0x10)."""
        val_str = ", ".join([f"0x{v:04X} ({v})" for v in values])
        log_msg(f"WRITE MULTIPLE [FC 0x10] Start Reg: 0x{start_reg:04X} ({start_reg}), Values: [{val_str}]")
        self._execute_transaction(
            "write_multiple_registers",
            self.client.write_registers,
            address=start_reg,
            values=values,
        )
        log_msg("WRITE MULTIPLE OK")

    def set_channel_config(self, channel: int, freq_hz: float, duty_pct: float) -> None:
        """Set Channel Frequency and Duty Cycle combined in a single Function 0x10 transaction."""
        freq_reg, duty_reg = get_channel_registers(channel)
        high_word, low_word = freq_hz_to_u32_words(freq_hz)
        duty_u16 = duty_pct_to_u16(duty_pct)

        log_msg(
            f"Configuring Channel {channel}: Frequency = {freq_hz:.2f} Hz, "
            f"Duty Cycle = {duty_pct:.2f}% (Combined FC 0x10 Write)"
        )
        self.write_multiple_registers(freq_reg, [high_word, low_word, duty_u16])

    def set_channel_duty(self, channel: int, duty_pct: float) -> None:
        """Update duty cycle only for a channel using Function Code 0x06."""
        _, duty_reg = get_channel_registers(channel)
        duty_u16 = duty_pct_to_u16(duty_pct)
        log_msg(f"Updating Channel {channel} Duty Cycle -> {duty_pct:.2f}% (FC 0x06 Write)")
        self.write_single_register(duty_reg, duty_u16)

    def read_channel(self, channel: int) -> Tuple[float, float]:
        """Read back frequency and duty cycle for specified channel using Function Code 0x03."""
        freq_reg, _ = get_channel_registers(channel)
        regs = self.read_holding_registers(freq_reg, count=3)
        freq_hz = u32_words_to_freq_hz(regs[0], regs[1])
        duty_pct = u16_to_duty_pct(regs[2])
        log_msg(f"Channel {channel} Readback -> Frequency: {freq_hz:.2f} Hz, Duty Cycle: {duty_pct:.2f}%")
        return freq_hz, duty_pct

    def verify_device(self) -> bool:
        """Read slave address (0x4000) and firmware version (0x8000)."""
        log_msg("--- Pre-wiring Hardware Verification ---")
        try:
            addr_vals = self.read_holding_registers(REG_SLAVE_ADDRESS, 1)
            slave_addr = addr_vals[0]
            log_msg(f"Device Slave Address (0x4000): {slave_addr} (0x{slave_addr:04X})")

            ver_vals = self.read_holding_registers(REG_VERSION_ADDRESS, 1)
            ver_raw = ver_vals[0]
            ver_major = (ver_raw >> 8) & 0xFF
            ver_minor = ver_raw & 0xFF
            log_msg(f"Firmware Version (0x8000): V{ver_major}.{ver_minor:02d} (0x{ver_raw:04X})")
            return True
        except Exception as e:
            log_msg(f"Device verification failed: {e}")
            return False


# ---------------------------------------------------------------------------
# Execution Workflows
# ---------------------------------------------------------------------------
def run_verification_procedure(ctrl: WavesharePwmController, channel: int) -> None:
    """Pre-wiring test: confirm Modbus comms work before touching the relay/LED."""
    log_msg("Starting Pre-wiring Test Procedure...")
    if not ctrl.verify_device():
        log_msg("FAILED: Device communication verification failed.")
        return

    log_msg(f"Testing round-trip write/read for Channel {channel}...")
    test_freq = 1000.0
    test_duty = 10.0
    ctrl.set_channel_config(channel, test_freq, test_duty)

    read_freq, read_duty = ctrl.read_channel(channel)
    if abs(read_freq - test_freq) < 0.1 and abs(read_duty - test_duty) < 0.1:
        log_msg("SUCCESS: Modbus readback matches expected values cleanly!")
        log_msg("You may now proceed to wire the relay module and run the toggle test.")
    else:
        log_msg(f"WARNING: Readback mismatch! Expected ({test_freq}Hz, {test_duty}%), Got ({read_freq}Hz, {read_duty}%)")

    # Safe rest state
    ctrl.set_channel_duty(channel, 0.0)


def run_relay_toggle_demo(
    ctrl: WavesharePwmController,
    channel: int,
    freq_hz: float,
    hold_sec: float,
    cycles: int,
) -> None:
    """
    Toggle the channel fully ON (100% duty, constant HIGH) and fully OFF
    (0% duty, constant LOW), holding each state for `hold_sec` seconds,
    repeated `cycles` times. Safe for a mechanical/opto relay's IN pin,
    unlike true PWM dimming which switches too fast for a relay.
    """
    log_msg(f"=== Starting Relay ON/OFF Toggle Test ({cycles} cycles, {hold_sec:.1f}s each state) ===")
    log_msg(
        "NOTE: If your relay board is ACTIVE-LOW, 'ON' below (100% duty / HIGH) "
        "may actually correspond to the relay being OPEN (LED off), and vice versa. "
        "Watch the relay's onboard LED/click to confirm actual behavior."
    )

    # Establish frequency once (irrelevant at 0/100% duty, but keeps config consistent)
    log_msg(f"Step 1: Setting Channel {channel} Frequency to {freq_hz:.1f} Hz, starting OFF (FC 0x10)...")
    ctrl.set_channel_config(channel, freq_hz, duty_pct=0.0)
    time.sleep(0.3)

    for i in range(1, cycles + 1):
        log_msg(f"--- Cycle {i}/{cycles} ---")

        log_msg(f"--> Requesting ON (100% duty / constant HIGH) for {hold_sec:.1f}s")
        ctrl.set_channel_duty(channel, 100.0)
        ctrl.read_channel(channel)
        time.sleep(hold_sec)

        log_msg(f"--> Requesting OFF (0% duty / constant LOW) for {hold_sec:.1f}s")
        ctrl.set_channel_duty(channel, 0.0)
        ctrl.read_channel(channel)
        time.sleep(hold_sec)

    log_msg("=== Relay toggle test finished ===")


# ---------------------------------------------------------------------------
# Main CLI Entry Point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Waveshare Modbus RTU PWM Output 4CH - Relay ON/OFF Toggle Test"
    )
    parser.add_argument("--port", default=DEFAULT_PORT, help=f"Serial port name (default: {DEFAULT_PORT})")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUDRATE, help=f"Baud rate (default: {DEFAULT_BAUDRATE})")
    parser.add_argument("--slave-id", type=int, default=DEFAULT_SLAVE_ID, help=f"Slave device ID (default: {DEFAULT_SLAVE_ID})")
    parser.add_argument("--channel", type=int, default=DEFAULT_CHANNEL, choices=[1, 2, 3, 4], help="PWM Channel (1-4)")
    parser.add_argument("--freq", type=float, default=DEFAULT_FREQUENCY_HZ, help=f"PWM Frequency in Hz (default: {DEFAULT_FREQUENCY_HZ}, irrelevant at 0/100% duty)")
    parser.add_argument("--hold", type=float, default=DEFAULT_HOLD_SEC, help=f"Seconds to hold each ON/OFF state (default: {DEFAULT_HOLD_SEC})")
    parser.add_argument("--cycles", type=int, default=DEFAULT_CYCLES, help=f"Number of ON->OFF cycles to run (default: {DEFAULT_CYCLES})")
    parser.add_argument("--verify", action="store_true", help="Run pre-wiring test & verification procedure")
    parser.add_argument("--list-ports", action="store_true", help="List available system serial COM ports and exit")

    args = parser.parse_args()

    if args.list_ports:
        ports = list_available_ports()
        if ports:
            print("Available Serial COM Ports:")
            for p in ports:
                print(f"  - {p}")
        else:
            print("No active serial COM ports detected on the system.")
        return

    controller = WavesharePwmController(
        port=args.port,
        baudrate=args.baud,
        slave_id=args.slave_id,
        timeout=DEFAULT_TIMEOUT,
    )

    try:
        controller.connect()

        if args.verify:
            run_verification_procedure(controller, args.channel)
        else:
            # Default behavior: relay ON/OFF toggle test
            run_relay_toggle_demo(controller, args.channel, args.freq, args.hold, args.cycles)

    except KeyboardInterrupt:
        log_msg("\nInterrupted by user (Ctrl+C). Cleaning up...")
    except Exception as e:
        log_msg(f"Fatal execution error: {e}")
        ports = list_available_ports()
        if ports:
            log_msg(f"Available serial ports found on system: {ports}")
        else:
            log_msg("No active serial COM ports found on system. Check USB connection and driver.")
        sys.exit(1)
    finally:
        # Safe resting state: Turn off PWM duty cycle if connected before exiting
        if controller.is_connected():
            try:
                log_msg(f"Safety Cleanup: Setting Channel {args.channel} Duty Cycle to 0.0% (Relay OFF)...")
                controller.set_channel_duty(args.channel, 0.0)
            except Exception as err:
                log_msg(f"Warning: Failed to reset duty cycle during cleanup: {err}")
        controller.close()


if __name__ == "__main__":
    main()