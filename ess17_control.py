import time
import logging
import argparse
from pymodbus.client import ModbusSerialClient as ModbusClient

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Modbus Register Map (ESS17-RS04 / ESS-RS Series)
# ---------------------------------------------------------------------------
STATUS_REGISTER = 0x0007        # Motion status bitmask (read-only)
ERROR_CODE_REGISTER = 0x0006    # Error code (read-only)

REG_ACCEL = 0x0021              # Accel time (ms)
REG_DECEL = 0x0022              # Decel time (ms)
REG_VELOCITY = 0x0023           # Velocity (RPM)
REG_POSITION_H = 0x0024         # Position target high 16 bits
REG_POSITION_L = 0x0025         # Position target low 16 bits
REG_MOVEMENT_CONTROL = 0x0027   # Movement control (write-only)
REG_AUX_CONTROL = 0x002D        # Aux control / drive enable (write-only)

# Control Commands (Reg 0x0027)
START_RELATIVE_POSITION_MOVE = 0x0001   # bit0=1, bit2=0 (Relative Move)
STOP_MOVE_CMD = 0x0000                  # Stop move

PULSES_PER_REV = 1000            # 1000 pulses per revolution

# Captured Calibrated Bounds
INITIAL_HOME_POS = 0             # Initial Home (0 pulses / 0.0 revs)
BOUND_1_POS = 7800               # Bound 1 (+7700 pulses / +7.70 revs)
BOUND_2_POS = -14000              # Bound 2 (-8200 pulses / -8.20 revs)

class ModbusLink:
    def __init__(self, port='COM3', baudrate=115200, slave_id=2):
        self.port = port
        self.baudrate = baudrate
        self.slave_id = slave_id
        self.client = ModbusClient(
            port=self.port,
            baudrate=self.baudrate,
            parity='N',
            stopbits=1,
            bytesize=8,
            timeout=1
        )

    def connect(self):
        if not self.client.connect():
            logger.error(f"Failed to connect to Modbus device on {self.port}")
            return False
        logger.info(f"Connected to Modbus device on {self.port} (Slave ID: {self.slave_id})")
        return True

    def disconnect(self):
        self.client.close()
        logger.info("Disconnected from Modbus device")

    def read_reg(self, address):
        try:
            result = self.client.read_holding_registers(address, count=1, device_id=self.slave_id)
            if result.isError():
                logger.error(f"Modbus Error reading register {hex(address)}: {result}")
                return None
            return result.registers[0]
        except Exception as e:
            logger.error(f"Failed to read register {hex(address)}: {e}")
            return None

    def read_regs(self, address, count):
        try:
            result = self.client.read_holding_registers(address, count=count, device_id=self.slave_id)
            if result.isError():
                logger.error(f"Modbus Error reading registers from {hex(address)}: {result}")
                return None
            return result.registers
        except Exception as e:
            logger.error(f"Failed to read registers from {hex(address)}: {e}")
            return None

    def write_reg(self, address, value):
        try:
            result = self.client.write_register(address, value, device_id=self.slave_id)
            if result.isError():
                logger.error(f"Modbus Error writing register {hex(address)}: {result}")
                return False
            return True
        except Exception as e:
            logger.error(f"Failed to write register {hex(address)}: {e}")
            return False

    def write_regs(self, address, values):
        try:
            result = self.client.write_registers(address, values, device_id=self.slave_id)
            if result.isError():
                logger.error(f"Modbus Error writing registers from {hex(address)}: {result}")
                return False
            return True
        except Exception as e:
            logger.error(f"Failed to write registers from {hex(address)}: {e}")
            return False

def read_motion_status(link):
    val = link.read_reg(STATUS_REGISTER)
    if val is None:
        return None
    return {
        'raw': val,
        'in_position': bool(val & (1 << 0)),
        'homing_completed': bool(val & (1 << 1)),
        'running': bool(val & (1 << 2)),
        'alarm': bool(val & (1 << 3)),
        'motor_released': bool(val & (1 << 4)),
    }

def enable_drive(link):
    logger.info("Enabling drive...")
    return link.write_reg(REG_AUX_CONTROL, 0)

def emergency_stop(link):
    logger.info("Sending emergency stop command...")
    return link.write_reg(REG_MOVEMENT_CONTROL, STOP_MOVE_CMD)

def wait_for_motion_complete(link, timeout=30):
    """Blocks until drive reports in_position and not running."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        status = read_motion_status(link)
        if status is None:
            time.sleep(0.05)
            continue
            
        if status['alarm']:
            logger.error("Drive ALARM active during motion!")
            return False
            
        if status['in_position'] and not status['running']:
            time.sleep(0.05)
            final_status = read_motion_status(link)
            if final_status and final_status['in_position'] and not final_status['running']:
                return True
            
        time.sleep(0.05)
        
    logger.warning(f"Motion timed out after {timeout} seconds.")
    return False

class ESS17Controller:
    """Tracks position and controls motion for ESS17-RS04 drive."""
    def __init__(self, link, initial_position=0):
        self.link = link
        self.position = initial_position

    def get_position(self):
        return self.position

    def move_relative(self, pulses, velocity_rpm=60, accel_ms=200, decel_ms=200):
        if pulses == 0:
            return True

        direction_str = "forward" if pulses >= 0 else "backward"
        pos_u32 = int(pulses) & 0xFFFFFFFF
        high = (pos_u32 >> 16) & 0xFFFF
        low = pos_u32 & 0xFFFF
        
        logger.info(f"Moving relative ({direction_str}): {pulses} pulses ({pulses / PULSES_PER_REV:+.2f} revs) at {velocity_rpm} RPM...")
        
        # Calculate dynamic timeout based on pulses and speed (minimum 30 seconds)
        expected_time_s = abs(pulses) / (velocity_rpm * PULSES_PER_REV / 60.0)
        dynamic_timeout = max(30.0, expected_time_s + 15.0)

        # Write motion parameters to block 0x0021 - 0x0025
        if not self.link.write_regs(REG_ACCEL, [accel_ms, decel_ms, velocity_rpm, high, low]):
            logger.error("Failed to write motion parameters.")
            return False
            
        # Trigger relative move
        if not self.link.write_reg(REG_MOVEMENT_CONTROL, START_RELATIVE_POSITION_MOVE):
            logger.error("Failed to trigger relative move.")
            return False
            
        time.sleep(0.15)
        if wait_for_motion_complete(self.link, timeout=dynamic_timeout):
            self.position += int(pulses)
            logger.info(f"Move complete. Current position: {self.position} pulses ({self.position / PULSES_PER_REV:+.2f} revs)")
            return True
        return False

    def move_absolute(self, target_position, velocity_rpm=60, accel_ms=200, decel_ms=200):
        relative_distance = target_position - self.position
        if relative_distance == 0:
            logger.info(f"Already at target position {target_position}.")
            return True

        direction_str = "forward" if relative_distance >= 0 else "backward"
        logger.info(f"Target: {target_position} pulses ({target_position / PULSES_PER_REV:+.2f} revs). Moving {direction_str} by {relative_distance} pulses...")

        return self.move_relative(relative_distance, velocity_rpm=velocity_rpm, accel_ms=accel_ms, decel_ms=decel_ms)

def run_bounds_routine(link, bound1=BOUND_1_POS, bound2=BOUND_2_POS, initial_home=INITIAL_HOME_POS, velocity_rpm=60, pause_s=2.0):
    """
    Executes automated bounds sweep routine:
    1. Move to Bound 1 (+7700 pulses)
    2. Return to Initial Home (0 pulses)
    3. Pause 2 seconds
    4. Move to Bound 2 (-8200 pulses)
    5. Return to Initial Home (0 pulses)
    """
    print("\n" + "=" * 65)
    print("  ESS17-RS04 AUTOMATED BOUNDS SWEEP ROUTINE")
    print("=" * 65)
    print(f"  Initial Home Target: {initial_home:6d} pulses ({initial_home / PULSES_PER_REV:6.2f} revs)")
    print(f"  Bound 1 Target:       {bound1:6d} pulses ({bound1 / PULSES_PER_REV:6.2f} revs)")
    print(f"  Bound 2 Target:       {bound2:6d} pulses ({bound2 / PULSES_PER_REV:6.2f} revs)")
    print(f"  Speed: {velocity_rpm} RPM | Pause Duration: {pause_s}s")
    print("=" * 65 + "\n")

    controller = ESS17Controller(link, initial_position=initial_home)

    # 1. Move to Bound 1
    print("\n--- STEP 1: Moving to Bound 1 ---")
    if not controller.move_absolute(bound1, velocity_rpm=velocity_rpm):
        logger.error("Step 1 failed: Could not reach Bound 1.")
        return False

    # 2. Return to Initial Home
    print("\n--- STEP 2: Returning to Initial Home ---")
    if not controller.move_absolute(initial_home, velocity_rpm=velocity_rpm):
        logger.error("Step 2 failed: Could not return to Initial Home.")
        return False

    # 3. Pause
    print(f"\n--- STEP 3: Pausing for {pause_s} seconds ---")
    time.sleep(pause_s)
    print("Pause complete.")

    # 4. Move to Bound 2
    print("\n--- STEP 4: Moving to Bound 2 ---")
    if not controller.move_absolute(bound2, velocity_rpm=velocity_rpm):
        logger.error("Step 4 failed: Could not reach Bound 2.")
        return False

    # 5. Return to Initial Home
    print("\n--- STEP 5: Returning to Initial Home ---")
    if not controller.move_absolute(initial_home, velocity_rpm=velocity_rpm):
        logger.error("Step 5 failed: Could not return to Initial Home.")
        return False

    print("\n" + "=" * 65)
    print("  AUTOMATED BOUNDS SWEEP ROUTINE COMPLETED SUCCESSFULLY!")
    print(f"  Final Position: {controller.get_position()} pulses ({controller.get_position() / PULSES_PER_REV:.2f} revs)")
    print("=" * 65 + "\n")
    return True

def main():
    parser = argparse.ArgumentParser(description="ESS17-RS04 Automated Bounds Routine")
    parser.add_argument("--port", default="COM3", help="Serial port (default: COM3)")
    parser.add_argument("--baud", type=int, default=115200, help="Baud rate (default: 115200)")
    parser.add_argument("--unit-id", type=int, default=2, help="Modbus Slave ID (default: 2)")
    parser.add_argument("--speed", type=int, default=60, help="Velocity in RPM (default: 60 RPM)")
    parser.add_argument("--pause", type=float, default=2.0, help="Pause time between sweeps in seconds (default: 2.0s)")
    parser.add_argument("--bound1", type=int, default=BOUND_1_POS, help=f"Bound 1 pulse count (default: {BOUND_1_POS})")
    parser.add_argument("--bound2", type=int, default=BOUND_2_POS, help=f"Bound 2 pulse count (default: {BOUND_2_POS})")
    args = parser.parse_args()

    link = ModbusLink(port=args.port, baudrate=args.baud, slave_id=args.unit_id)
    
    if not link.connect():
        return

    try:
        if not enable_drive(link):
            logger.error("Failed to enable drive.")
            return

        run_bounds_routine(
            link=link,
            bound1=args.bound1,
            bound2=args.bound2,
            initial_home=INITIAL_HOME_POS,
            velocity_rpm=args.speed,
            pause_s=args.pause
        )
    except KeyboardInterrupt:
        print("\n[Ctrl+C] Motion aborted by user. Issuing emergency stop...")
        emergency_stop(link)
    finally:
        link.disconnect()

if __name__ == "__main__":
    main()
