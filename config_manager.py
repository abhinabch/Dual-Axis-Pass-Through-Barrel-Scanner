import os
import json
import copy
import logging
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "scan_config.json")

# Valid cleanup modes for the reconstruction pipeline. "rules" is the hand-tuned,
# production-default thresholding pipeline; "learned" is the PointNet + GridUNet
# model-based cleanup path; "hybrid" uses the learned model only deep in the stave
# wall and falls back to rules everywhere else (heads/poles/crozehead bevel) as a
# bounded safety net. See docs/PROMOTION_RULE.md and
# notebooks/05_rules_vs_learned_volume_accuracy.ipynb for accuracy comparisons --
# as of that validation run, "learned" has NOT met the promotion bar and is
# meaningfully less accurate than "rules" on synthetic ground truth; "hybrid" gets
# close to rules' accuracy but is not shown to beat it outright.
VALID_CLEANUP_MODES = ("rules", "learned", "hybrid")

DEFAULT_CONFIG: Dict[str, Any] = {
    "motor_settings": {
        "port": "COM3",
        "baudrate": 115200,
        "tilt_slave_id": 2,
        "rot_slave_id": 1,
        "tilt_pulses_per_rev": 1000,
        "rot_pulses_per_rev": 10000,
        "tilt_accel_ms": 200,
        "tilt_decel_ms": 200,
        "rot_accel_ms": 300,
        "rot_decel_ms": 300
    },
    "sweep_settings": {
        "tilt_speed_rpm": 60,
        "rot_speed_rpm": 60,
        "tilt_targets_pulses": [-8000, -4000, 0, 2000, 5000],
        "rot_deg": 1440.0,
        "rot_revs": 4.0,
        "pause_seconds": 1.0
    },
    "reconstruction_settings": {
        "cleanup_mode": "rules"
    },
    "led_settings": {
        "enabled": False,
        "port": "COM4",
        "baudrate": 9600,
        "slave_id": 1,
        "channel": 1,
        "freq_hz": 1000.0,
        "overall_brightness_pct": 40.0,
        "tilt_pass_brightness_pct": 70.0,
        "rotation_brightness_pct": 100.0
    }
}


def load_config(config_path: str = DEFAULT_CONFIG_PATH) -> Dict[str, Any]:
    """
    Loads scan configuration from JSON file. If file is missing or corrupted,
    returns default configuration with missing keys populated.
    """
    config = copy.deepcopy(DEFAULT_CONFIG)
    if not os.path.exists(config_path):
        logger.warning(f"Config file not found at {config_path}. Creating default config.")
        save_config(config, config_path)
        return config

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Merge motor_settings
        if "motor_settings" in data and isinstance(data["motor_settings"], dict):
            for k, v in data["motor_settings"].items():
                config["motor_settings"][k] = v

        # Merge sweep_settings
        if "sweep_settings" in data and isinstance(data["sweep_settings"], dict):
            for k, v in data["sweep_settings"].items():
                config["sweep_settings"][k] = v

        # Merge reconstruction_settings
        if "reconstruction_settings" in data and isinstance(data["reconstruction_settings"], dict):
            for k, v in data["reconstruction_settings"].items():
                config["reconstruction_settings"][k] = v

        # Merge led_settings
        if "led_settings" in data and isinstance(data["led_settings"], dict):
            for k, v in data["led_settings"].items():
                config["led_settings"][k] = v

        # Validate cleanup_mode; fall back to the safe default if the config file
        # has an unrecognized value (e.g. hand-edited or from an older version).
        if config["reconstruction_settings"].get("cleanup_mode") not in VALID_CLEANUP_MODES:
            logger.warning(
                "Invalid reconstruction cleanup_mode %r in config; defaulting to 'rules'.",
                config["reconstruction_settings"].get("cleanup_mode"),
            )
            config["reconstruction_settings"]["cleanup_mode"] = "rules"

        # Clamp LED brightness percentages to the valid 0-100% duty cycle range.
        for pct_key in ("overall_brightness_pct", "tilt_pass_brightness_pct", "rotation_brightness_pct"):
            try:
                pct_val = float(config["led_settings"].get(pct_key, 0.0))
            except (TypeError, ValueError):
                pct_val = 0.0
            config["led_settings"][pct_key] = max(0.0, min(100.0, pct_val))

        # Keep rot_deg and rot_revs synchronized
        rot_pulses_per_rev = config["motor_settings"].get("rot_pulses_per_rev", 10000)
        if "rot_revs" in config["sweep_settings"]:
            config["sweep_settings"]["rot_deg"] = float(config["sweep_settings"]["rot_revs"]) * 360.0
        elif "rot_deg" in config["sweep_settings"]:
            config["sweep_settings"]["rot_revs"] = float(config["sweep_settings"]["rot_deg"]) / 360.0

    except Exception as e:
        logger.error(f"Error reading config file {config_path}: {e}. Returning default config.")
        config = copy.deepcopy(DEFAULT_CONFIG)

    return config


def save_config(config_data: Dict[str, Any], config_path: str = DEFAULT_CONFIG_PATH) -> bool:
    """
    Saves configuration dict to JSON file. Returns True if successful.
    """
    try:
        # Ensure rotation degree and revolutions consistency
        if "sweep_settings" in config_data:
            if "rot_revs" in config_data["sweep_settings"]:
                config_data["sweep_settings"]["rot_deg"] = float(config_data["sweep_settings"]["rot_revs"]) * 360.0
            elif "rot_deg" in config_data["sweep_settings"]:
                config_data["sweep_settings"]["rot_revs"] = float(config_data["sweep_settings"]["rot_deg"]) / 360.0

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=2)
        logger.info(f"Scan configuration saved successfully to {config_path}.")
        return True
    except Exception as e:
        logger.error(f"Failed to save scan configuration to {config_path}: {e}")
        return False


def reset_to_defaults(config_path: str = DEFAULT_CONFIG_PATH) -> Dict[str, Any]:
    """
    Resets the configuration file to default settings.
    """
    defaults = copy.deepcopy(DEFAULT_CONFIG)
    save_config(defaults, config_path)
    return defaults


def parse_tilt_targets_str(targets_str: str) -> List[int]:
    """
    Parses comma/space separated string of integers into a list of ints.
    Example: "-8000, -4000, 0, 2000, 5000" -> [-8000, -4000, 0, 2000, 5000]
    """
    cleaned = targets_str.replace("[", "").replace("]", "")
    parts = [p.strip() for p in cleaned.replace(";", ",").split(",") if p.strip()]
    targets = []
    for part in parts:
        try:
            targets.append(int(part))
        except ValueError:
            pass
    return targets


def tilt_targets_to_str(targets: List[int]) -> str:
    """
    Converts a list of int tilt encoder values to comma-separated string.
    """
    return ", ".join(str(t) for t in targets)
