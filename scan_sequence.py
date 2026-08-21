import time
import threading
import logging
import argparse
from pymodbus.client import ModbusSerialClient as ModbusClient

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Modbus Register Maps
# ---------------------------------------------------------------------------
# ESS17-RS04 Registers (Tilt Axis - Slave 2 default)
ESS17_STATUS_REGISTER = 0x0007      # Motion status bitmask (read-only)
ESS17_ERROR_CODE_REGISTER = 0x0006  # Error code (read-only)
ESS17_REG_ACCEL = 0x0021            # Accel time (ms)
ESS17_REG_DECEL = 0x0022            # Decel time (ms)
ESS17_REG_VELOCITY = 0x0023         # Velocity (RPM)
ESS17_REG_POSITION_H = 0x0024       # Position target high 16 bits
ESS17_REG_POSITION_L = 0x0025       # Position target low 16 bits
ESS17_REG_MOVEMENT_CONTROL = 0x0027 # Movement control (write-only)
ESS17_REG_AUX_CONTROL = 0x002D      # Aux control / drive enable (write-only)

ESS17_START_RELATIVE_MOVE = 0x0001
ESS17_STOP_MOVE_CMD = 0x0000

# iDM57-RS23 Registers (Rotation / Pan Axis - Slave 1 default)
IDM57_STATUS_REGISTER = 4099        # 0x1003 Motion status bitmask
IDM57_ALARM_REGISTER = 4097         # 0x1001 Alarm register
IDM57_REG_CONTROL_WORD = 0x6200     # Pr9.00 Path 0 control word
IDM57_REG_POSITION_H = 0x6201       # Pr9.01 Position High 16 bits
IDM57_REG_POSITION_L = 0x6202       # Pr9.02 Position Low 16 bits
IDM57_REG_VELOCITY = 0x6203         # Pr9.03 Velocity (RPM)
IDM57_REG_ACC = 0x6204              # Pr9.04 Accel (ms/1000rpm)
IDM57_REG_DEC = 0x6205              # Pr9.05 Decel (ms/1000rpm)
IDM57_REG_PAUSE = 0x6206            # Pr9.06 Pause time
IDM57_REG_TRIGGER = 0x6207          # Pr9.07 Write 0x0010 to trigger path 0 move
IDM57_REG_EMERGENCY_STOP = 0x6002   # Write 0x0040 to E-Stop

IDM57_RELATIVE_MOVE_WORD = (1 << 0) | (1 << 6) # 0x0041 (Relative Position Move)


class ModbusLink:
    def __init__(self, port='COM3', baudrate=115200, client=None, lock=None):
        """
        Thin register-access wrapper used by the scan sequence.

        If `client` is provided (an already-constructed pymodbus client, e.g. the
        one the GUI keeps open for status polling / jogging / the safety watchdog),
        it is reused instead of opening a second connection on the same serial
        port -- opening two independent handles to the same COM port will fail or
        fight each other.

        If `lock` is provided (shared with other callers of that same client, e.g.
        the GUI's SafetyWatchdog and jog/poll code), every register transaction is
        serialized against it. A Modbus RTU link is a single half-duplex channel --
        concurrent transactions from different threads corrupt each other's
        request/response frames on the wire. Falls back to a private lock if none
        is given, which is still correct as long as this ModbusLink is the only
        thing touching `client`.
        """
        self.port = port
        self.baudrate = baudrate
        self.lock = lock if lock is not None else threading.Lock()
        if client is not None:
            self.client = client
        else:
            self.client = ModbusClient(
                port=self.port,
                baudrate=self.baudrate,
                parity='N',
                stopbits=1,
                bytesize=8,
                timeout=2.0
            )

    def connect(self):
        if not self.client.connect():
            logger.error(f"Failed to connect to Modbus device on {self.port}")
            return False
        logger.info(f"Connected to Modbus device on {self.port}")
        return True

    def disconnect(self):
        self.client.close()
        logger.info("Disconnected from Modbus device")

    def ensure_connected(self):
        """Re-establishes connection if pymodbus closed port after transient errors."""
        if hasattr(self.client, "connected") and not self.client.connected:
            logger.warning("Modbus connection was closed by driver. Re-connecting...")
            self.client.connect()

    # Default retry count/backoff for register transactions. Under GUI load (Tk
    # mainloop + SafetyWatchdog polling thread + scan thread all fighting over the
    # GIL and this same serial link), a single request/response can transiently
    # miss its timeout window even though the drive answered fine -- retrying a
    # couple of times with a short backoff recovers from that without having to
    # guess at static settle delays.
    RETRY_ATTEMPTS = 2
    RETRY_DELAY_S = 0.2

    def read_reg(self, slave_id, address):
        with self.lock:
            for attempt in range(1, self.RETRY_ATTEMPTS + 2):
                self.ensure_connected()
                time.sleep(0.01)
                try:
                    result = self.client.read_holding_registers(address, count=1, device_id=slave_id)
                    if result is None or result.isError():
                        logger.warning(f"Modbus read attempt {attempt}/{self.RETRY_ATTEMPTS + 1} failed for register {hex(address)} on Slave {slave_id}: {result}")
                    else:
                        return result.registers[0]
                except Exception as e:
                    logger.warning(f"Modbus read attempt {attempt}/{self.RETRY_ATTEMPTS + 1} raised for register {hex(address)} on Slave {slave_id}: {e}")
                if attempt <= self.RETRY_ATTEMPTS:
                    time.sleep(self.RETRY_DELAY_S)
            logger.error(f"Modbus read failed after {self.RETRY_ATTEMPTS + 1} attempts: register {hex(address)} on Slave {slave_id}")
            return None

    def write_reg(self, slave_id, address, value):
        with self.lock:
            for attempt in range(1, self.RETRY_ATTEMPTS + 2):
                self.ensure_connected()
                time.sleep(0.01)
                try:
                    result = self.client.write_register(address, value, device_id=slave_id)
                    if result is None or result.isError():
                        logger.warning(f"Modbus write attempt {attempt}/{self.RETRY_ATTEMPTS + 1} failed for register {hex(address)} on Slave {slave_id}: {result}")
                    else:
                        return True
                except Exception as e:
                    logger.warning(f"Modbus write attempt {attempt}/{self.RETRY_ATTEMPTS + 1} raised for register {hex(address)} on Slave {slave_id}: {e}")
                if attempt <= self.RETRY_ATTEMPTS:
                    time.sleep(self.RETRY_DELAY_S)
            logger.error(f"Modbus write failed after {self.RETRY_ATTEMPTS + 1} attempts: register {hex(address)} on Slave {slave_id}, value {value}")
            return False

    def write_regs(self, slave_id, address, values):
        with self.lock:
            for attempt in range(1, self.RETRY_ATTEMPTS + 2):
                self.ensure_connected()
                time.sleep(0.01)
                try:
                    result = self.client.write_registers(address, values, device_id=slave_id)
                    if result is None or result.isError():
                        logger.warning(f"Modbus write attempt {attempt}/{self.RETRY_ATTEMPTS + 1} failed for registers from {hex(address)} on Slave {slave_id}: {result}")
                    else:
                        return True
                except Exception as e:
                    logger.warning(f"Modbus write attempt {attempt}/{self.RETRY_ATTEMPTS + 1} raised for registers from {hex(address)} on Slave {slave_id}: {e}")
                if attempt <= self.RETRY_ATTEMPTS:
                    time.sleep(self.RETRY_DELAY_S)
            logger.error(f"Modbus write failed after {self.RETRY_ATTEMPTS + 1} attempts: registers from {hex(address)} on Slave {slave_id}, values {values}")
            return False


# ---------------------------------------------------------------------------
# ESS17 Controller (Tilt Axis - Slave 2)
# ---------------------------------------------------------------------------
class ESS17Controller:
    """Tracks position and controls motion for ESS17-RS04 drive (1000 pulses/rev)."""
    PULSES_PER_REV = 1000

    def __init__(self, link, slave_id=2, initial_position=0):
        self.link = link
        self.slave_id = slave_id
        self.position = initial_position

    def enable(self):
        logger.info(f"Enabling ESS17 drive (Slave {self.slave_id})...")
        return self.link.write_reg(self.slave_id, ESS17_REG_AUX_CONTROL, 0)

    def emergency_stop(self):
        logger.info(f"Emergency stop sent to ESS17 drive (Slave {self.slave_id})...")
        return self.link.write_reg(self.slave_id, ESS17_REG_MOVEMENT_CONTROL, ESS17_STOP_MOVE_CMD)

    def read_status(self):
        val = self.link.read_reg(self.slave_id, ESS17_STATUS_REGISTER)
        if val is None:
            return None
        return {
            'raw': val,
            'in_position': bool(val & (1 << 0)),
            'running': bool(val & (1 << 2)),
            'alarm': bool(val & (1 << 3)),
            'motor_released': bool(val & (1 << 4)),
        }

    def wait_for_complete(self, timeout=30):
        # Poll interval deliberately loose: this loop runs continuously for the
        # entire duration of a move (many seconds), sharing one half-duplex RS485
        # line with SafetyWatchdog's own polling. Polling at 50ms (~20 req/s) here
        # sustains enough combined bus traffic to eventually wedge a drive's RS485
        # receiver (observed needing a physical power cycle to recover) -- 250ms is
        # still far tighter than needed to notice a multi-second move finishing.
        POLL_INTERVAL = 0.25
        start_time = time.time()
        while time.time() - start_time < timeout:
            status = self.read_status()
            if status is None:
                time.sleep(POLL_INTERVAL)
                continue
            if status['alarm']:
                logger.error(f"ESS17 drive ALARM on Slave {self.slave_id}!")
                return False
            if status['in_position'] and not status['running']:
                time.sleep(POLL_INTERVAL)
                final_status = self.read_status()
                if final_status and final_status['in_position'] and not final_status['running']:
                    return True
            time.sleep(POLL_INTERVAL)
        logger.warning(f"ESS17 motion timed out after {timeout}s on Slave {self.slave_id}.")
        return False

    def get_position(self):
        return self.position

    def move_relative(self, pulses, velocity_rpm=60, accel_ms=200, decel_ms=200):
        if pulses == 0:
            return True
        direction_str = "forward" if pulses >= 0 else "backward"
        pos_u32 = int(pulses) & 0xFFFFFFFF
        high = (pos_u32 >> 16) & 0xFFFF
        low = pos_u32 & 0xFFFF

        logger.info(f"Moving ESS17 Slave {self.slave_id} relative ({direction_str}): {pulses} pulses ({pulses / self.PULSES_PER_REV:+.2f} revs) at {velocity_rpm} RPM...")

        expected_time_s = abs(pulses) / (velocity_rpm * self.PULSES_PER_REV / 60.0)
        dynamic_timeout = max(30.0, expected_time_s + 15.0)

        if not self.link.write_regs(self.slave_id, ESS17_REG_ACCEL, [accel_ms, decel_ms, velocity_rpm, high, low]):
            logger.error(f"Failed writing ESS17 parameters to Slave {self.slave_id}.")
            return False

        if not self.link.write_reg(self.slave_id, ESS17_REG_MOVEMENT_CONTROL, ESS17_START_RELATIVE_MOVE):
            logger.error(f"Failed to trigger move on ESS17 Slave {self.slave_id}.")
            return False

        time.sleep(0.15)
        if self.wait_for_complete(timeout=dynamic_timeout):
            self.position += int(pulses)
            logger.info(f"ESS17 Slave {self.slave_id} move complete. Position: {self.position} pulses ({self.position / self.PULSES_PER_REV:+.2f} revs)")
            return True
        return False

    def move_absolute(self, target_position, velocity_rpm=60, accel_ms=200, decel_ms=200):
        relative_distance = target_position - self.position
        if relative_distance == 0:
            logger.info(f"ESS17 Slave {self.slave_id} already at target position {target_position}.")
            return True
        direction_str = "forward" if relative_distance >= 0 else "backward"
        logger.info(f"ESS17 Slave {self.slave_id} target: {target_position} pulses ({target_position / self.PULSES_PER_REV:+.2f} revs). Moving {direction_str} by {relative_distance} pulses...")
        return self.move_relative(relative_distance, velocity_rpm=velocity_rpm, accel_ms=accel_ms, decel_ms=decel_ms)


# ---------------------------------------------------------------------------
# iDM57 Controller (Rotation / Pan Axis - Slave 1)
# ---------------------------------------------------------------------------
class IDM57Controller:
    """Tracks position and controls motion for iDM57-RS23 drive (10,000 pulses/rev)."""
    PULSES_PER_REV = 10000

    def __init__(self, link, slave_id=1, initial_position=0):
        self.link = link
        self.slave_id = slave_id
        self.position = initial_position

    def enable(self):
        logger.info(f"Checking iDM57 drive status (Slave {self.slave_id})...")
        status = self.read_status()
        if status and status['faulty']:
            logger.error(f"iDM57 Slave {self.slave_id} has active FAULT! Clear fault before moving.")
            return False
        if status and not status['enabled']:
            logger.warning(f"iDM57 Slave {self.slave_id} is disabled. Check hardware enable / power.")
        return True

    def emergency_stop(self):
        logger.info(f"Emergency stop sent to iDM57 drive (Slave {self.slave_id})...")
        return self.link.write_regs(self.slave_id, IDM57_REG_EMERGENCY_STOP, [0x0040])

    def read_status(self):
        val = self.link.read_reg(self.slave_id, IDM57_STATUS_REGISTER)
        if val is None:
            return None
        return {
            'raw': val,
            'faulty': bool(val & (1 << 0)),
            'enabled': bool(val & (1 << 1)),
            'running': bool(val & (1 << 2)),
            'path_completed': bool(val & (1 << 5)),
        }

    def wait_for_complete(self, timeout=30):
        # See ESS17Controller.wait_for_complete for why this interval is loose
        # rather than 50ms -- sustained dense polling on this same bus/slave has
        # been observed to wedge the iDM57's RS485 receiver until power-cycled.
        POLL_INTERVAL = 0.25
        start_time = time.time()
        while time.time() - start_time < timeout:
            status = self.read_status()
            if status is None:
                time.sleep(POLL_INTERVAL)
                continue
            if status['faulty']:
                logger.error(f"iDM57 drive FAULT detected on Slave {self.slave_id}!")
                return False
            if status['path_completed'] and not status['running']:
                return True
            time.sleep(POLL_INTERVAL)
        logger.warning(f"iDM57 motion timed out after {timeout}s on Slave {self.slave_id}.")
        return False

    def get_position(self):
        return self.position

    def move_relative(self, pulses, velocity_rpm=30, accel_ms=300, decel_ms=300):
        if pulses == 0:
            return True
        direction_str = "forward" if pulses >= 0 else "backward"
        pos_u32 = int(pulses) & 0xFFFFFFFF
        high = (pos_u32 >> 16) & 0xFFFF
        low = pos_u32 & 0xFFFF

        logger.info(f"Moving iDM57 Slave {self.slave_id} relative ({direction_str}): {pulses} pulses ({pulses / self.PULSES_PER_REV:+.2f} revs / {pulses * 360.0 / self.PULSES_PER_REV:+.1f} deg) at {velocity_rpm} RPM...")

        expected_time_s = abs(pulses) / (velocity_rpm * self.PULSES_PER_REV / 60.0)
        dynamic_timeout = max(30.0, expected_time_s + 15.0)

        # 1. Write control word (relative positioning)
        if not self.link.write_regs(self.slave_id, IDM57_REG_CONTROL_WORD, [IDM57_RELATIVE_MOVE_WORD]):
            logger.error(f"Failed writing iDM57 control word to Slave {self.slave_id}.")
            return False

        # 2. Write position target, velocity, accel, decel, pause
        if not self.link.write_regs(self.slave_id, IDM57_REG_POSITION_H, [high, low, velocity_rpm, accel_ms, decel_ms, 0]):
            logger.error(f"Failed writing iDM57 parameters to Slave {self.slave_id}.")
            return False

        # 3. Trigger move
        if not self.link.write_regs(self.slave_id, IDM57_REG_TRIGGER, [0x0010]):
            logger.error(f"Failed triggering move on iDM57 Slave {self.slave_id}.")
            return False

        time.sleep(0.15)
        if self.wait_for_complete(timeout=dynamic_timeout):
            self.position += int(pulses)
            logger.info(f"iDM57 Slave {self.slave_id} move complete. Position: {self.position} pulses ({self.position / self.PULSES_PER_REV:+.2f} revs)")
            return True
        return False

    def move_absolute(self, target_position, velocity_rpm=30, accel_ms=300, decel_ms=300):
        relative_distance = target_position - self.position
        if relative_distance == 0:
            logger.info(f"iDM57 Slave {self.slave_id} already at target position {target_position}.")
            return True
        direction_str = "forward" if relative_distance >= 0 else "backward"
        logger.info(f"iDM57 Slave {self.slave_id} target: {target_position} pulses ({target_position / self.PULSES_PER_REV:+.2f} revs). Moving {direction_str} by {relative_distance} pulses...")
        return self.move_relative(relative_distance, velocity_rpm=velocity_rpm, accel_ms=accel_ms, decel_ms=decel_ms)


from config_manager import load_config, DEFAULT_CONFIG_PATH

# ---------------------------------------------------------------------------
# Main Scan Sequence Routine
# ---------------------------------------------------------------------------
def run_scan_sequence(
    link,
    tilt_slave=None,
    rot_slave=None,
    tilt_speed=None,
    rot_speed=None,
    pause_s=None,
    rot_revs=None,
    tilt_targets=None,
    config=None,
    config_path=DEFAULT_CONFIG_PATH,
    progress_callback=None
):
    """
    Executes scan routine across specified tilt angles and rotation sweeps:
    - Config loaded from scan_config.json by default or passed via `config` dict.
    - Tilt positions (ESS17): loaded from sweep_settings (e.g., [-8000, -4000, 0, 2000, 5000] pulses)
    - At each tilt step: pause, rotate +rot_revs revolutions (+40,000 pulses for 4 revs on iDM57),
      rotate -rot_revs revolutions back, return to initial rotation position.
    - Return tilt and rotation to initial positions at end.
    """
    def _notify(msg):
        logger.info(msg)
        if progress_callback:
            try:
                progress_callback(msg)
            except Exception:
                pass

    if config is None:
        config = load_config(config_path)

    motor_cfg = config.get("motor_settings", {})
    sweep_cfg = config.get("sweep_settings", {})

    tilt_slave = tilt_slave if tilt_slave is not None else motor_cfg.get("tilt_slave_id", 2)
    rot_slave = rot_slave if rot_slave is not None else motor_cfg.get("rot_slave_id", 1)
    tilt_speed = tilt_speed if tilt_speed is not None else sweep_cfg.get("tilt_speed_rpm", 60)
    rot_speed = rot_speed if rot_speed is not None else sweep_cfg.get("rot_speed_rpm", 60)
    pause_s = pause_s if pause_s is not None else sweep_cfg.get("pause_seconds", 1.0)
    rot_revs = rot_revs if rot_revs is not None else sweep_cfg.get("rot_revs", 4.0)

    if tilt_targets is None:
        tilt_targets = sweep_cfg.get("tilt_targets_pulses", [-8000, -4000, 0, 2000, 5000])

    tilt_axis = ESS17Controller(link, slave_id=tilt_slave, initial_position=0)
    rot_axis = IDM57Controller(link, slave_id=rot_slave, initial_position=0)

    # Record initial positions
    initial_tilt_pos = tilt_axis.get_position()
    initial_rot_pos = rot_axis.get_position()
    _notify(f"Initial positions captured - Tilt (Slave {tilt_slave}): {initial_tilt_pos} pulses, Rot (Slave {rot_slave}): {initial_rot_pos} pulses")

    rot_pulses = int(rot_revs * IDM57Controller.PULSES_PER_REV)
    rot_deg = rot_revs * 360.0

    print("\n" + "=" * 65)
    print("  DUAL-AXIS PASS-THROUGH SCAN SEQUENCE")
    print("=" * 65)
    print(f"  Tilt Motor:     ESS17-RS04 (Slave {tilt_slave}, {ESS17Controller.PULSES_PER_REV} pulses/rev)")
    print(f"  Rotation Motor: iDM57-RS23 (Slave {rot_slave}, {IDM57Controller.PULSES_PER_REV} pulses/rev)")
    print(f"  Tilt Targets (pulses): {tilt_targets}")
    print(f"  Rotation per step:     +{rot_deg:.0f} deg (+{rot_pulses} pulses) -> -{rot_deg:.0f} deg (-{rot_pulses} pulses)")
    print(f"  Tilt Speed: {tilt_speed} RPM | Rotation Speed: {rot_speed} RPM | Pause: {pause_s}s")
    print("=" * 65 + "\n")

    try:
        total_steps = len(tilt_targets)
        for idx, target in enumerate(tilt_targets, 1):
            _notify(f"Step {idx}/{total_steps}: Moving Tilt to {target} pulses ({target / ESS17Controller.PULSES_PER_REV:+.2f} revs)...")
            if not tilt_axis.move_absolute(target, velocity_rpm=tilt_speed):
                logger.error(f"Tilt move to {target} failed. Aborting sequence.")
                break

            _notify(f"Pausing for {pause_s}s at tilt target {target}...")
            time.sleep(pause_s)

            # Rotation motor positive sweep (+rot_revs revolutions)
            _notify(f"Step {idx}/{total_steps}: Rotation sweep +{rot_deg:.0f}° (+{rot_pulses} pulses)...")
            time.sleep(pause_s)
            if not rot_axis.move_relative(rot_pulses, velocity_rpm=rot_speed):
                time.sleep(pause_s)
                #this sleep is necessary for the rot motor to be able to recieve and
                # send the next command. Without this sleep, the next command wont
                # be sent and the program will hang. This is a known issue with the
                #iDM57 motor and is not a problem with the code.
                logger.error(f"Rotation +{rot_deg:.0f} deg sweep failed. Aborting sequence.")
                break

            time.sleep(0.5)

            # Rotation motor negative sweep (-rot_revs revolutions back)
            _notify(f"Step {idx}/{total_steps}: Rotation return sweep -{rot_deg:.0f}° (-{rot_pulses} pulses)...")
            if not rot_axis.move_relative(-rot_pulses, velocity_rpm=rot_speed):
                logger.error(f"Rotation -{rot_deg:.0f} deg return sweep failed. Aborting sequence.")
                break

            # Guarantee alignment back to initial rotation position for each pass
            rot_axis.move_absolute(initial_rot_pos, velocity_rpm=rot_speed)

    except Exception as e:
        logger.exception(f"Unexpected error during scan sequence execution: {e}")
    finally:
        _notify("Returning axes to initial home positions...")
        logger.info(f"Returning Tilt axis to initial position ({initial_tilt_pos} pulses)...")
        tilt_axis.move_absolute(initial_tilt_pos, velocity_rpm=tilt_speed)
        
        logger.info(f"Returning Rotation axis to initial position ({initial_rot_pos} pulses)...")
        rot_axis.move_absolute(initial_rot_pos, velocity_rpm=rot_speed)

        _notify("Scan sequence completed.")


def main():
    parser = argparse.ArgumentParser(description="Dual-Axis Scan Sequence (ESS17 Tilt + iDM57 Rotation)")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help=f"Path to scan config file (default: {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--port", help="Serial port for RS485 adapter")
    parser.add_argument("--baud", type=int, help="Baud rate")
    parser.add_argument("--tilt-slave", type=int, help="Modbus Slave ID for Tilt motor")
    parser.add_argument("--rot-slave", type=int, help="Modbus Slave ID for Rotation motor")
    parser.add_argument("--tilt-speed", type=int, help="Tilt speed in RPM")
    parser.add_argument("--rot-speed", type=int, help="Rotation speed in RPM")
    parser.add_argument("--pause", type=float, help="Pause duration at tilt position in seconds")
    parser.add_argument("--rot-revs", type=float, help="Number of rotation revolutions per sweep pass")
    args = parser.parse_args()

    cfg = load_config(args.config)
    motor_cfg = cfg.get("motor_settings", {})
    sweep_cfg = cfg.get("sweep_settings", {})

    port = args.port or motor_cfg.get("port", "COM3")
    baud = args.baud or motor_cfg.get("baudrate", 115200)
    tilt_slave = args.tilt_slave or motor_cfg.get("tilt_slave_id", 2)
    rot_slave = args.rot_slave or motor_cfg.get("rot_slave_id", 1)
    tilt_speed = args.tilt_speed or sweep_cfg.get("tilt_speed_rpm", 60)
    rot_speed = args.rot_speed or sweep_cfg.get("rot_speed_rpm", 60)
    pause_s = args.pause if args.pause is not None else sweep_cfg.get("pause_seconds", 2.0)
    rot_revs = args.rot_revs if args.rot_revs is not None else sweep_cfg.get("rot_revs", 4.0)

    link = ModbusLink(port=port, baudrate=baud)

    if not link.connect():
        logger.error(f"Failed to connect to Modbus adapter on {port}.")
        return

    tilt_axis = ESS17Controller(link, slave_id=tilt_slave)
    rot_axis = IDM57Controller(link, slave_id=rot_slave)

    try:
        if not tilt_axis.enable():
            logger.error(f"Failed to enable Tilt drive (Slave {tilt_slave}).")
            return
            
        if not rot_axis.enable():
            logger.error(f"Failed checking Rotation drive status (Slave {rot_slave}).")
            return

        run_scan_sequence(
            link=link,
            tilt_slave=tilt_slave,
            rot_slave=rot_slave,
            tilt_speed=tilt_speed,
            rot_speed=rot_speed,
            pause_s=pause_s,
            rot_revs=rot_revs,
            config=cfg
        )
    except KeyboardInterrupt:
        print("\n[Ctrl+C] Motion aborted by user. Issuing emergency stop to both drives...")
        tilt_axis.emergency_stop()
        rot_axis.emergency_stop()
    finally:
        link.disconnect()
        logger.info("Done.")

if __name__ == "__main__":
    main()

