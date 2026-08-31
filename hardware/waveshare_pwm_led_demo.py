#!/usr/bin/env python3
"""
Waveshare Modbus RTU PWM Output 4CH (SKU 33921) LED Brightness Controller
-------------------------------------------------------------------------
Controls PWM frequency and duty cycle over RS485 Modbus RTU for LED brightness switching.

⚠️ WIRING CAUTION - READ BEFORE CONNECTING THE LED:
   The PWM module's D1–D4 outputs are opto-isolated signal-level PWM outputs,
   jumper-selectable to 3.3V or 5V logic, rated for <30 mA max load.
   They CANNOT power a 12V DC LED directly.
   You MUST place a driver stage (e.g., logic-level N-channel MOSFET or SSR)
   between the module output and the 12V LED:
     - PWM signal (D1-D4) -> MOSFET Gate / SSR Input
     - MOSFET switches the LED's 12V DC power supply line.

MODBUS PROTOCOL REFERENCE (Waveshare PWM Output 4CH):
  Default serial parameters: 9600 baud, 8 data bits, No parity, 1 stop bit (9600 8N1)
  Function codes: 0x03 (read holding), 0x06 (write single), 0x10 (write multiple)

REGISTER MAP:
  - 0x0000 / 0x0001 : Channel 1 Frequency (u32 big-endian, unit 0.01 Hz)
  - 0x0002          : Channel 1 Duty Cycle (u16, unit 0.01%, range 0-10000)
  - 0x0003 / 0x0004 : Channel 2 Frequency
  - 0x0005          : Channel 2 Duty Cycle
  - 0x0006 / 0x0007 : Channel 3 Frequency
  - 0x0008          : Channel 3 Duty Cycle
  - 0x0009 / 0x000A : Channel 4 Frequency
  - 0x000B          : Channel 4 Duty Cycle
  - 0x2000          : Serial Port Parameters
  - 0x4000          : Device Slave Address (1-255)
  - 0x8000          : Firmware Version (read-only)

DEPENDENCIES:
  pip install pymodbus

USAGE:
  1. Verification / Pre-wiring check (reads address 0x4000 & firmware 0x8000):
     python hardware/waveshare_pwm_led_demo.py --verify --port COM3

  2. Standard 3-level 5-second LED brightness demo:
     python hardware/waveshare_pwm_led_demo.py --demo --port COM3

  3. Custom frequency and duty cycle control:
     python hardware/waveshare_pwm_led_demo.py --port COM3 --channel 1 --freq 1000 --duty 50
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
DEFAULT_FREQUENCY_HZ = 1000.0  # 1000 Hz flicker-free PWM

# 3 Brightness Levels for the 5-second demo
DEMO_LEVELS = [
    {"name": "Level 1 (Low)", "duty_pct": 20.0, "hold_sec": 1.67},
    {"name": "Level 2 (Medium)", "duty_pct": 55.0, "hold_sec": 1.67},
    {"name": "Level 3 (High)", "duty_pct": 100.0, "hold_sec": 1.66},
]

# Modbus Register Offsets
REG_BAUD_ADDRESS = 0x2000
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
    """
    Returns (freq_start_reg, duty_reg) for 1-based channel number (1 to 4).
    """
    if channel < 1 or channel > 4:
        raise ValueError(f"Channel must be 1, 2, 3, or 4 (got {channel})")
    base_freq_reg = (channel - 1) * 3
    base_duty_reg = base_freq_reg + 2
    return base_freq_reg, base_duty_reg


def freq_hz_to_u32_words(freq_hz: float) -> Tuple[int, int]:
    """
    Convert frequency in Hz to uint32 (0.01 Hz units) and split into
    two 16-bit big-endian words (high_word, low_word).
    """
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


def scan_bus(port: str, slave_ids, bauds=None, timeout: float = 0.3) -> List[dict]:
    """Probe every (baud rate, slave id) pair on `port` and report what answers.

    Built for a shared RS485 bus, where "which baud and which address is this
    board on?" is the question you actually need answered -- after a slave-address
    or baud-rate write, or when adding a device alongside the drives. Doing this
    by hand means re-running --verify in a loop, ~16s per miss, with alarming
    error spew for every combination that isn't there.

    Two kinds of hit are reported, and the difference matters:
      - "PWM board": register 0x4000 read back cleanly and echoed the address, so
        this is a Waveshare board.
      - "device present": something answered but rejected 0x4000, i.e. another
        device (a stepper drive) lives at that address.

    Short timeout, single attempt, so a full 8-baud x 8-address sweep is seconds.
    """
    if bauds is None:
        bauds = sorted(WavesharePwmController.BAUD_INDEX)
    found = []
    for baud in bauds:
        client = ModbusSerialClient(
            port=port, baudrate=baud, parity="N", stopbits=1, bytesize=8,
            timeout=timeout, retries=1,
        )
        if not client.connect():
            log_msg(f"  {baud:>6} baud: could not open {port}")
            continue
        try:
            for sid in slave_ids:
                try:
                    rr = client.read_holding_registers(
                        address=REG_SLAVE_ADDRESS, count=1, device_id=sid
                    )
                except Exception:
                    continue
                if rr is None:
                    continue
                if rr.isError():
                    # Answered, but does not implement 0x4000 -- not a PWM board.
                    found.append({"baud": baud, "slave_id": sid, "kind": "device present"})
                    log_msg(f"  {baud:>6} baud, ID {sid:>3}: device present (not a PWM board)")
                    continue
                echoed = rr.registers[0]
                found.append({
                    "baud": baud, "slave_id": sid, "kind": "PWM board", "reports": echoed,
                })
                log_msg(
                    f"  {baud:>6} baud, ID {sid:>3}: PWM board "
                    f"(0x4000 reports {echoed})"
                )
        finally:
            client.close()
    return found


# ---------------------------------------------------------------------------
# Modbus PWM Controller Class
# ---------------------------------------------------------------------------
class WavesharePwmController:
    def __init__(self, port: str, baudrate: int = 9600, slave_id: int = 1, timeout: float = 2.0,
                 client=None, lock=None):
        """PWM board controller.

        `client` / `lock`: when the board sits on the SAME RS485 bus as the stepper
        drives (the usual wiring -- one USB-RS485 adapter, one pair of wires, several
        slave IDs), it must share their pymodbus client and their lock rather than
        opening a second handle. Windows will not let two handles hold one COM port,
        and two threads transacting on one half-duplex bus corrupt each other's
        frames. Standalone use (the CLI demo) passes neither and gets its own
        connection as before.

        Note that a shared bus means a shared baud rate: the board's 9600 default
        has to be reprogrammed to match the drives, or nothing on the bus works.
        """
        self.port = port
        self.baudrate = baudrate
        self.slave_id = slave_id
        self.timeout = timeout
        self._owns_client = client is None
        if client is not None:
            self.client = client
        else:
            self.client = ModbusSerialClient(
                port=self.port,
                baudrate=self.baudrate,
                parity="N",
                stopbits=1,
                bytesize=8,
                timeout=self.timeout,
            )
        if lock is None:
            import threading
            lock = threading.Lock()
        self.lock = lock

    def connect(self) -> None:
        """Establish serial Modbus RTU connection."""
        if not self._owns_client:
            # Borrowed connection -- the owner opened it and keeps it open.
            log_msg(
                f"Using shared {self.port} connection at {self.baudrate} 8N1 "
                f"(Slave ID: {self.slave_id})."
            )
            return
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
        if not self._owns_client:
            # Never close a borrowed client -- the motors are still using it.
            return
        if self.is_connected():
            self.client.close()
            log_msg(f"Closed connection to {self.port}.")

    def _execute_transaction(self, func_name: str, action_func, *args, **kwargs):
        """Wrapper for Modbus calls providing logging and detailed error handling."""
        try:
            # pymodbus 3.x compatibility: pass device_id parameter
            kwargs["device_id"] = self.slave_id
            with self.lock:
                response = action_func(*args, **kwargs)

            if response is None:
                raise ModbusException("No response received from slave (Timeout).")

            if response.isError():
                err_msg = str(response)
                # Check for standard Modbus Exception codes
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
        """
        Set Channel Frequency and Duty Cycle combined in a single Function 0x10 transaction.
        Recommended to avoid transient invalid 32-bit frequency values on the slave module.
        """
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

    # Baud rate index written to the low byte of register 0x2000. The order is the
    # one Waveshare's own product page lists the supported rates in. The high byte
    # is the parity selector; 0 = none, matching the 8N1 this module ships with.
    #
    # NOTE: this mapping is NOT confirmed against Waveshare's protocol document
    # (their wiki blocks automated fetches and the PDF is image-only). If a write
    # lands wrong the board is not bricked -- it is just answering at some other
    # setting. Recover by sweeping: for each baud in BAUD_INDEX, run
    #   python hardware/waveshare_pwm_led_demo.py --verify --port COM3 --baud <b> --slave-id <id>
    # until one responds, then rewrite from there.
    BAUD_INDEX = {
        4800: 0, 9600: 1, 19200: 2, 38400: 3,
        57600: 4, 115200: 5, 128000: 6, 256000: 7,
    }
    PARITY_NONE = 0

    def set_baud_rate(self, new_baud: int) -> None:
        """Reprogram the board's serial baud rate (register 0x2000).

        Needed when the board shares an RS485 bus with other devices: one bus runs
        at one baud rate, so the board's 9600 default cannot coexist with drives
        running at 115200.

        The write is addressed at the CURRENT baud; the board answers at the new
        one immediately afterwards, so the confirming read must reconnect.
        """
        if int(new_baud) not in self.BAUD_INDEX:
            raise ValueError(
                f"Unsupported baud {new_baud}. Supported: {sorted(self.BAUD_INDEX)}"
            )
        idx = self.BAUD_INDEX[int(new_baud)]
        value = (self.PARITY_NONE << 8) | idx
        log_msg(
            f"Setting serial parameters 0x2000: baud {self.baudrate} -> {new_baud} "
            f"(index {idx}, parity none) = 0x{value:04X}"
        )
        log_msg(
            "After this write the board stops answering at the old baud rate. "
            "Reconnect with --baud %d to confirm." % int(new_baud)
        )
        self.write_single_register(REG_BAUD_ADDRESS, value)
        log_msg("Baud rate register written.")

    def set_slave_address(self, new_address: int) -> None:
        """Reprogram the board's Modbus slave address (register 0x4000).

        Needed when the board shares an RS485 bus with other devices: its factory
        default of 1 collides with the iDM57 rotation drive, and two devices
        answering one address makes the bus unusable.

        The write is addressed to the board's CURRENT slave id; afterwards it
        responds only on the new one.
        """
        if not 1 <= int(new_address) <= 255:
            raise ValueError(f"Slave address must be 1-255, got {new_address}")
        log_msg(
            f"Setting device slave address 0x4000: {self.slave_id} -> {new_address} ..."
        )
        self.write_single_register(REG_SLAVE_ADDRESS, int(new_address))
        log_msg(
            f"Slave address written. The board now answers as ID {new_address}; "
            f"re-run --verify with --slave-id {new_address} to confirm."
        )
        self.slave_id = int(new_address)

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
    """Test Procedure (Section 7 from spec) before wiring LED."""
    log_msg("Starting Section 7 Pre-wiring Test Procedure...")
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
        log_msg("You may now proceed to verify the MOSFET/SSR driver stage with a multimeter.")
    else:
        log_msg(f"WARNING: Readback mismatch! Expected ({test_freq}Hz, {test_duty}%), Got ({read_freq}Hz, {read_duty}%)")

    # Safe rest state
    ctrl.set_channel_duty(channel, 0.0)


def run_led_brightness_demo(ctrl: WavesharePwmController, channel: int, freq_hz: float) -> None:
    """
    Run 3-level LED brightness sequence over 5 seconds:
      - Set Channel Frequency to 1000 Hz once via Function 0x10.
      - Level 1: 20% duty for ~1.67 s
      - Level 2: 55% duty for ~1.67 s
      - Level 3: 100% duty for ~1.66 s
      - Reset duty to 0% in finally block.
    """
    log_msg("=== Starting 5-Second 3-Level LED Brightness Demo ===")

    # Step 1: Write frequency and initial 0% duty via Function 0x10
    log_msg(f"Step 1: Setting Channel {channel} Frequency to {freq_hz:.1f} Hz (FC 0x10)...")
    ctrl.set_channel_config(channel, freq_hz, duty_pct=0.0)
    time.sleep(0.2)

    # Step 2: Step duty cycle through the 3 brightness levels
    start_time = time.time()
    for lvl in DEMO_LEVELS:
        name = lvl["name"]
        pct = lvl["duty_pct"]
        hold = lvl["hold_sec"]

        log_msg(f"--> Switch to {name} ({pct:.0f}% duty cycle) for {hold:.2f}s")
        ctrl.set_channel_duty(channel, pct)

        # Confirm readback
        ctrl.read_channel(channel)

        time.sleep(hold)

    total_elapsed = time.time() - start_time
    log_msg(f"=== Demo cycle finished in {total_elapsed:.2f} seconds ===")


# ---------------------------------------------------------------------------
# Main CLI Entry Point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Waveshare Modbus RTU PWM Output 4CH LED Brightness Controller"
    )
    parser.add_argument("--port", default=DEFAULT_PORT, help=f"Serial port name (default: {DEFAULT_PORT})")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUDRATE, help=f"Baud rate (default: {DEFAULT_BAUDRATE})")
    parser.add_argument("--slave-id", type=int, default=DEFAULT_SLAVE_ID, help=f"Slave device ID (default: {DEFAULT_SLAVE_ID})")
    parser.add_argument("--channel", type=int, default=DEFAULT_CHANNEL, choices=[1, 2, 3, 4], help="PWM Channel (1-4)")
    parser.add_argument("--freq", type=float, default=DEFAULT_FREQUENCY_HZ, help=f"PWM Frequency in Hz (default: {DEFAULT_FREQUENCY_HZ})")
    parser.add_argument("--duty", type=float, default=None, help="Set static duty cycle percentage (0-100%%) and exit")
    parser.add_argument("--verify", action="store_true", help="Run pre-wiring test & verification procedure")
    parser.add_argument("--demo", action="store_true", help="Run 3-level 5-second brightness demo (default behavior if no mode specified)")
    parser.add_argument("--list-ports", action="store_true", help="List available system serial COM ports and exit")
    parser.add_argument(
        "--set-slave-id", type=int, default=None, metavar="NEW_ID",
        help="Reprogram the board's Modbus slave address (1-255) and exit. Use when "
             "the board shares an RS485 bus with the drives -- its default of 1 "
             "collides with the rotation motor. Addressed to the CURRENT --slave-id.",
    )
    parser.add_argument(
        "--scan-bus", action="store_true",
        help="Probe every baud rate x slave id on --port and report what answers, "
             "then exit. Use to find the board after a slave-address or baud-rate "
             "change, or to check for address collisions on a shared RS485 bus.",
    )
    parser.add_argument(
        "--scan-ids", default="1-8", metavar="RANGE",
        help="Slave ids for --scan-bus, e.g. '1-8' or '1,3,5' (default: 1-8).",
    )
    parser.add_argument(
        "--set-baud", type=int, default=None, metavar="NEW_BAUD",
        choices=sorted(WavesharePwmController.BAUD_INDEX),
        help="Reprogram the board's baud rate (register 0x2000) and exit. Needed when "
             "the board shares a bus with the drives -- one bus runs at one baud rate. "
             "Written at the CURRENT --baud; reconnect at the new one to confirm.",
    )

    args = parser.parse_args()

    if args.scan_bus:
        spec = args.scan_ids.strip()
        if "-" in spec and "," not in spec:
            lo, hi = spec.split("-", 1)
            ids = list(range(int(lo), int(hi) + 1))
        else:
            ids = [int(x) for x in spec.split(",") if x.strip()]
        log_msg(f"Scanning {args.port}: {len(ids)} address(es) x 8 baud rates...")
        hits = scan_bus(args.port, ids)
        if hits:
            log_msg("--- Devices found ---")
            for h in hits:
                log_msg(f"  {h['baud']} baud, slave id {h['slave_id']}: {h['kind']}")
        else:
            log_msg(
                "Nothing answered on any baud rate or address. Check the A+/B- wiring, "
                "the adapter, and that the device is powered."
            )
        return

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

        if args.set_slave_id is not None:
            controller.set_slave_address(args.set_slave_id)
        elif args.set_baud is not None:
            controller.set_baud_rate(args.set_baud)
        elif args.verify:
            run_verification_procedure(controller, args.channel)
        elif args.duty is not None:
            log_msg(f"Static Control: Setting Channel {args.channel} Freq={args.freq}Hz, Duty={args.duty}%")
            controller.set_channel_config(args.channel, args.freq, args.duty)
        else:
            # Default to --demo mode if neither --verify nor static --duty was passed
            run_led_brightness_demo(controller, args.channel, args.freq)

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
        # Safe resting state: Turn off PWM duty cycle if connected before exiting.
        # Skipped after a baud change -- the board has already moved to the new rate,
        # so this connection can no longer reach it and the cleanup would just log a
        # confusing timeout.
        if args.set_baud is not None:
            log_msg(
                "Skipping duty-cycle cleanup: the board has switched to %d baud. "
                "Confirm with: --verify --port %s --baud %d --slave-id %d"
                % (args.set_baud, args.port, args.set_baud, controller.slave_id)
            )
        elif controller.is_connected():
            try:
                log_msg(f"Safety Cleanup: Setting Channel {args.channel} Duty Cycle to 0.0% (LED Off)...")
                controller.set_channel_duty(args.channel, 0.0)
            except Exception as err:
                log_msg(f"Warning: Failed to reset duty cycle during cleanup: {err}")
        controller.close()


if __name__ == "__main__":
    main()
