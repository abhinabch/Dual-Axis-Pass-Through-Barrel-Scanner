#!/usr/bin/env python3
"""
Hold the scanner LEDs on until Ctrl+C.
-------------------------------------------------------------------------------
Switches the Waveshare Modbus RTU PWM Output 4CH board's channel to a fixed
brightness and then simply stays alive, holding the port, until the operator
interrupts it. On interrupt (or any other exit path) the duty cycle is driven
back to 0% so the lamps never outlive the script.

Why a script that does nothing but wait: the PWM board latches its duty-cycle
register on its own, so a one-shot

    python hardware/waveshare_pwm_led_demo.py --port COM3 --duty 100

leaves the LEDs on and then, in its cleanup, immediately turns them back off.
This one keeps them lit for as long as you need to work under them -- aligning
the barrel, checking focus, cleaning optics -- with a single Ctrl+C to end it.

⚠️ SHARED BUS / PORT CONFLICT:
   On this rig the PWM board sits on the SAME RS485 bus as the stepper drives
   (COM3, 115200, its own slave id). Windows will not hand out a second handle
   to a COM port that is already open, so this script CANNOT run while the
   operator dashboard (app_gui.py) is running -- close the dashboard first, or
   use its Technician panel's "Manual LED Override" button instead, which
   borrows the dashboard's own connection.

DEFAULTS:
   Connection settings are read from scan_config.json's "led_settings" block, so
   with the rig configured this needs no arguments at all. Every value can be
   overridden on the command line.

USAGE:
   # Hold at the configured manual brightness
   python hardware/led_hold_on.py

   # Hold at 60%, explicit port/slave
   python hardware/led_hold_on.py --port COM3 --baud 115200 --slave-id 3 --brightness 60

   # Re-send the duty cycle every 30s, so a board that browns out relights itself
   python hardware/led_hold_on.py --refresh 30
"""

import argparse
import os
import sys
import time

# Run as `python hardware/led_hold_on.py` and sys.path[0] is hardware/, not the
# repo root -- so config_manager and the `hardware.` package path are both
# invisible. Put the repo root on the path before importing either.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from hardware.waveshare_pwm_led_demo import (  # noqa: E402
    WavesharePwmController,
    list_available_ports,
    log_msg,
)

# Fallbacks used only when scan_config.json is missing or unreadable.
FALLBACK_LED_SETTINGS = {
    "port": "COM3",
    "baudrate": 115200,
    "slave_id": 3,
    "channel": 1,
    "freq_hz": 1000.0,
    "manual_brightness_pct": 100.0,
}

# How often the "still holding" heartbeat is printed, in seconds. Frequent enough
# to show the script is alive, rare enough not to bury the startup log.
HEARTBEAT_SEC = 60.0


def load_led_defaults() -> dict:
    """LED connection settings from scan_config.json, or hardcoded fallbacks."""
    settings = dict(FALLBACK_LED_SETTINGS)
    try:
        from config_manager import load_config

        led_cfg = load_config().get("led_settings", {})
        for key in settings:
            if key in led_cfg:
                settings[key] = led_cfg[key]
        log_msg("Loaded LED defaults from scan_config.json.")
    except Exception as e:
        log_msg(f"Could not read scan_config.json ({e}); using built-in defaults.")
    return settings


def hold(ctrl: WavesharePwmController, channel: int, freq_hz: float,
         duty_pct: float, refresh_sec: float) -> None:
    """Light the channel and block until KeyboardInterrupt.

    `refresh_sec` > 0 re-sends the duty cycle on that interval. The board holds
    its register perfectly well on its own, so this is purely insurance against
    it losing power or being reset by something else on the bus mid-session --
    leave it off unless you have seen that happen.
    """
    ctrl.set_channel_config(channel=channel, freq_hz=freq_hz, duty_pct=duty_pct)
    log_msg(
        f"LEDs ON: channel {channel} at {duty_pct:.1f}% duty, {freq_hz:.1f} Hz. "
        f"Press Ctrl+C to turn them off and exit."
    )

    started = time.monotonic()
    last_heartbeat = started
    last_refresh = started

    while True:
        # Short sleeps, not one long one: on Windows a Ctrl+C is only delivered
        # when the main thread wakes, so a multi-second sleep would make the
        # script feel unresponsive to the interrupt that is its only exit.
        time.sleep(0.25)
        now = time.monotonic()

        if refresh_sec > 0 and (now - last_refresh) >= refresh_sec:
            last_refresh = now
            try:
                ctrl.set_channel_duty(channel, duty_pct)
            except Exception as e:
                # A failed refresh is not a reason to drop the session -- the
                # lamps are probably still lit from the last good write.
                log_msg(f"WARNING: duty-cycle refresh failed, still holding: {e}")

        if (now - last_heartbeat) >= HEARTBEAT_SEC:
            last_heartbeat = now
            mins = (now - started) / 60.0
            log_msg(f"Still holding LEDs on at {duty_pct:.1f}% ({mins:.0f} min elapsed).")


def main() -> int:
    defaults = load_led_defaults()

    parser = argparse.ArgumentParser(
        description="Hold the Waveshare PWM LED channel on until Ctrl+C.",
    )
    parser.add_argument("--port", default=str(defaults["port"]),
                        help=f"Serial port (default from config: {defaults['port']})")
    parser.add_argument("--baud", type=int, default=int(defaults["baudrate"]),
                        help=f"Baud rate (default from config: {defaults['baudrate']})")
    parser.add_argument("--slave-id", type=int, default=int(defaults["slave_id"]),
                        help=f"Modbus slave id (default from config: {defaults['slave_id']})")
    parser.add_argument("--channel", type=int, default=int(defaults["channel"]),
                        choices=[1, 2, 3, 4],
                        help=f"PWM channel 1-4 (default from config: {defaults['channel']})")
    parser.add_argument("--freq", type=float, default=float(defaults["freq_hz"]),
                        help=f"PWM frequency in Hz (default from config: {defaults['freq_hz']})")
    parser.add_argument("--brightness", type=float,
                        default=float(defaults["manual_brightness_pct"]),
                        help="Duty cycle percentage 0-100 (default from config: "
                             f"{defaults['manual_brightness_pct']})")
    parser.add_argument("--refresh", type=float, default=0.0, metavar="SECONDS",
                        help="Re-send the duty cycle every SECONDS as insurance "
                             "against a board reset (default: 0, disabled).")

    args = parser.parse_args()

    if not 0.0 <= args.brightness <= 100.0:
        parser.error(f"--brightness must be between 0 and 100 (got {args.brightness})")

    controller = WavesharePwmController(
        port=args.port, baudrate=args.baud, slave_id=args.slave_id,
    )

    connected = False
    try:
        controller.connect()
        connected = True
        hold(controller, args.channel, args.freq, args.brightness, args.refresh)
    except KeyboardInterrupt:
        log_msg("Interrupted by user (Ctrl+C).")
    except Exception as e:
        log_msg(f"Fatal error: {e}")
        if not connected:
            # Far and away the most common cause on this rig, and the error
            # pymodbus reports for it is not obvious. Say it outright.
            log_msg(
                f"Could not open {args.port}. If the operator dashboard "
                f"(app_gui.py) is running it already holds this port -- close it "
                f"and retry, or use its Technician panel's Manual LED Override."
            )
            ports = list_available_ports()
            if ports:
                log_msg("Serial ports visible on this system:")
                for prt in ports:
                    log_msg(f"  - {prt}")
            else:
                log_msg("No serial ports detected. Check the USB-RS485 adapter.")
        return 1
    finally:
        # Lamps off on every exit path, including the Ctrl+C that is the normal
        # one. Best-effort: a failure here must not mask the original error.
        if connected:
            try:
                log_msg(f"Turning LEDs off (channel {args.channel} duty -> 0%)...")
                controller.set_channel_duty(args.channel, 0.0)
            except Exception as err:
                log_msg(f"WARNING: failed to switch the LEDs off: {err}")
            controller.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
