import os
import sys
import json

# Ensure project root is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config_manager import (
    load_config,
    save_config,
    reset_to_defaults,
    parse_tilt_targets_str,
    tilt_targets_to_str,
    DEFAULT_CONFIG
)

def test_config_manager():
    print("Testing config_manager functions...")
    
    # 1. Test load_config
    cfg = load_config()
    assert "motor_settings" in cfg, "Missing motor_settings in config"
    assert "sweep_settings" in cfg, "Missing sweep_settings in config"
    print("  load_config() passed.")
    
    # 2. Test tilt targets string parsing
    raw_str = "-8000, -4000, 0, 2000, 5000"
    parsed = parse_tilt_targets_str(raw_str)
    assert parsed == [-8000, -4000, 0, 2000, 5000], f"Failed tilt targets parse: {parsed}"
    
    formatted = tilt_targets_to_str(parsed)
    assert formatted == "-8000, -4000, 0, 2000, 5000", f"Failed tilt targets format: {formatted}"
    print("  tilt targets conversion passed.")
    
    # 3. Test saving modified config
    test_cfg_path = os.path.join(os.path.dirname(__file__), "temp_test_config.json")
    try:
        cfg["sweep_settings"]["tilt_speed_rpm"] = 75
        cfg["sweep_settings"]["tilt_targets_pulses"] = [-10000, -5000, 0, 5000, 10000]
        assert save_config(cfg, test_cfg_path), "save_config returned False"
        
        loaded_temp = load_config(test_cfg_path)
        assert loaded_temp["sweep_settings"]["tilt_speed_rpm"] == 75, "Failed to load modified tilt speed"
        assert loaded_temp["sweep_settings"]["tilt_targets_pulses"] == [-10000, -5000, 0, 5000, 10000], "Failed to load modified tilt targets"
        print("  save_config() and load_config() with custom path passed.")
    finally:
        if os.path.exists(test_cfg_path):
            os.remove(test_cfg_path)
            
    print("ALL TESTS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    test_config_manager()
