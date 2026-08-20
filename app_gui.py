import tkinter as tk
import customtkinter as ctk
import threading
import queue
import time
import logging
import math
import os
import csv
from datetime import datetime
from enum import Enum, auto
from typing import Optional, Callable, List

def deg_to_encoder(deg: float) -> int:
    """Linearly map degrees [-158°, +90°] to encoder pulses [-8200, +7700]."""
    deg = max(-158.0, min(90.0, float(deg)))
    if deg >= 0:
        enc = deg * (7700.0 / 90.0)
    else:
        enc = deg * (8200.0 / 158.0)
    return int(round(enc))

def encoder_to_deg(enc: int) -> float:
    """Linearly map encoder pulses [-8200, +7700] to degrees [-158°, +90°]."""
    enc_val = float(enc)
    if enc_val >= 0:
        deg = enc_val * (90.0 / 7700.0)
    else:
        deg = enc_val * (158.0 / 8200.0)
    return round(max(-158.0, min(90.0, deg)), 1)


class TiltProtractorWidget(ctk.CTkFrame):
    """
    Interactive circular dial protractor selector for setting tilt target angles per pass.
    Maps degrees [-158°, +90°] to encoder values [-8200, +7700].
    """
    def __init__(self, parent, on_change_callback: Optional[Callable] = None, **kwargs):
        super().__init__(parent, **kwargs)
        self.on_change_callback = on_change_callback

        # Initial pass degrees default (5 passes, 0° included)
        # Default encoder values [-8000, -4000, 0, 2000, 5000] -> [-154.1, -77.1, 0.0, 23.4, 58.4]
        self.passes: List[float] = [-154.1, -77.1, 0.0, 23.4, 58.4]
        self.active_pass_idx: int = 0
        self._updating_from_entry = False

        self._build_ui()
        self.draw_dial()

    def _build_ui(self):
        # Top Controls: Pass count
        top_ctrl = ctk.CTkFrame(self, fg_color="transparent")
        top_ctrl.pack(fill="x", padx=10, pady=(5, 10))

        ctk.CTkLabel(top_ctrl, text="Number of Passes:", font=("Roboto", 13, "bold")).pack(side="left", padx=(0, 10))
        
        self.pass_count_var = tk.StringVar(value=str(len(self.passes)))
        self.pass_count_entry = ctk.CTkEntry(top_ctrl, textvariable=self.pass_count_var, width=60)
        self.pass_count_entry.pack(side="left", padx=5)
        
        ctk.CTkButton(top_ctrl, text="Set Passes", width=80, command=self._on_pass_count_change).pack(side="left", padx=5)

        ctk.CTkLabel(top_ctrl, text="(Default = 5, 0° pre-set)", font=("Roboto", 11), text_color="gray").pack(side="left", padx=10)

        # Main Layout: Left Dial Canvas, Right Pass List
        main_box = ctk.CTkFrame(self, fg_color="transparent")
        main_box.pack(fill="both", expand=True, padx=5, pady=5)
        main_box.grid_columnconfigure(0, weight=0)
        main_box.grid_columnconfigure(1, weight=1)

        # Dial Canvas Container
        canvas_frame = ctk.CTkFrame(main_box, fg_color="#181818", corner_radius=10)
        canvas_frame.grid(row=0, column=0, padx=(0, 15), pady=5, sticky="n")

        self.canvas_size = 320
        self.cx = self.canvas_size // 2
        self.cy = self.canvas_size // 2
        self.r = 115

        self.canvas = tk.Canvas(
            canvas_frame,
            width=self.canvas_size,
            height=self.canvas_size,
            bg="#141414",
            highlightthickness=0
        )
        self.canvas.pack(padx=10, pady=10)

        self.canvas.bind("<Button-1>", self._on_canvas_click)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)

        # Right Panel: Pass Entries List
        self.pass_list_frame = ctk.CTkScrollableFrame(main_box, height=320, label_text="Pass Angles & Computed Encoder Targets")
        self.pass_list_frame.grid(row=0, column=1, sticky="nsew", pady=5)

        self._rebuild_pass_entries()

    def _rebuild_pass_entries(self):
        """Recreate entry rows for each pass."""
        for child in self.pass_list_frame.winfo_children():
            child.destroy()

        self.entry_vars = []
        self.encoder_labels = []

        for i, p_deg in enumerate(self.passes):
            row_frame = ctk.CTkFrame(
                self.pass_list_frame, 
                fg_color="#2b2b2b" if i == self.active_pass_idx else "transparent",
                border_width=1 if i == self.active_pass_idx else 0,
                border_color="#3a7ebf"
            )
            row_frame.pack(fill="x", padx=5, pady=3)

            # Pass selector button
            btn_color = "#1f538d" if i == self.active_pass_idx else "gray"
            btn = ctk.CTkButton(
                row_frame, 
                text=f"Pass {i+1}", 
                width=65, 
                fg_color=btn_color,
                command=lambda idx=i: self.select_pass(idx)
            )
            btn.pack(side="left", padx=5, pady=5)

            # Degree numeric input
            var = tk.StringVar(value=f"{p_deg:.1f}")
            self.entry_vars.append(var)

            deg_entry = ctk.CTkEntry(row_frame, textvariable=var, width=80)
            deg_entry.pack(side="left", padx=5, pady=5)
            deg_entry.bind("<FocusOut>", lambda e, idx=i: self._on_entry_changed(idx))
            deg_entry.bind("<Return>", lambda e, idx=i: self._on_entry_changed(idx))

            ctk.CTkLabel(row_frame, text="°", font=("Roboto", 14, "bold")).pack(side="left")

            # Computed raw encoder value label
            enc_val = deg_to_encoder(p_deg)
            lbl = ctk.CTkLabel(row_frame, text=f"Raw Encoder: {enc_val:+d} pulses", text_color="#aaaaaa", font=("Consolas", 12))
            lbl.pack(side="left", padx=15, pady=5)
            self.encoder_labels.append(lbl)

    def select_pass(self, idx: int):
        if 0 <= idx < len(self.passes):
            self.active_pass_idx = idx
            self._rebuild_pass_entries()
            self.draw_dial()

    def _on_pass_count_change(self):
        try:
            count = int(self.pass_count_var.get().strip())
            count = max(1, min(20, count))
        except ValueError:
            count = len(self.passes)

        self.set_pass_count(count)

    def set_pass_count(self, count: int):
        if count == len(self.passes):
            return

        if count < len(self.passes):
            self.passes = self.passes[:count]
        else:
            # Generate new pass angles evenly between min/max or at 0
            needed = count - len(self.passes)
            start_deg = self.passes[-1] if self.passes else 0.0
            for i in range(needed):
                new_deg = min(90.0, start_deg + (i + 1) * 15.0)
                self.passes.append(round(new_deg, 1))

        if self.active_pass_idx >= len(self.passes):
            self.active_pass_idx = len(self.passes) - 1

        self.pass_count_var.set(str(len(self.passes)))
        self._rebuild_pass_entries()
        self.draw_dial()
        self._notify_change()

    def _on_entry_changed(self, idx: int):
        if self._updating_from_entry:
            return
        self._updating_from_entry = True
        try:
            val_str = self.entry_vars[idx].get().strip()
            val = float(val_str)
            clamped = max(-158.0, min(90.0, val))
            self.passes[idx] = round(clamped, 1)
            self.entry_vars[idx].set(f"{self.passes[idx]:.1f}")
        except ValueError:
            self.entry_vars[idx].set(f"{self.passes[idx]:.1f}")
        finally:
            self._updating_from_entry = False

        # Update encoder label
        enc_val = deg_to_encoder(self.passes[idx])
        self.encoder_labels[idx].configure(text=f"Raw Encoder: {enc_val:+d} pulses")
        self.draw_dial()
        self._notify_change()

    def draw_dial(self):
        self.canvas.delete("all")
        cx, cy, r = self.cx, self.cy, self.r

        # Draw Dead Zone Arc (top ~112° between +90° and -158°)
        # In canvas polar angle (0° = 3 o'clock, counter-clockwise):
        # 0° = +90° pass angle (3 o'clock). Dead zone spans 0° to 112° polar.
        self.canvas.create_arc(
            cx - r, cy - r, cx + r, cy + r,
            start=0, extent=112,
            fill="#262626", outline="#444444", width=1.5, style="pieslice"
        )
        # Dead Zone text
        self.canvas.create_text(
            cx, cy - r + 35,
            text="DEAD ZONE\n(UNREACHABLE)",
            fill="#666666", font=("Roboto", 9, "bold"), justify="center"
        )

        # Draw Valid Arc (-158° to +90°, span 248° counter-clockwise from 112°)
        self.canvas.create_arc(
            cx - r, cy - r, cx + r, cy + r,
            start=112, extent=248,
            fill="#181e26", outline="#3a7ebf", width=2, style="pieslice"
        )

        # Gridlines & Degree Labels
        grid_degs = [-158, -150, -120, -90, -60, -30, 0, 30, 60, 90]
        for g_deg in grid_degs:
            polar_rad = math.radians(270.0 + g_deg)
            cos_a = math.cos(polar_rad)
            sin_a = math.sin(polar_rad)

            # Grid tick
            x_outer = cx + r * cos_a
            y_outer = cy - r * sin_a
            x_inner = cx + (r - 10) * cos_a
            y_inner = cy - (r - 10) * sin_a
            
            line_color = "#555555" if g_deg not in [-158, 0, 90] else "#3a7ebf"
            self.canvas.create_line(x_inner, y_inner, x_outer, y_outer, fill=line_color, width=1.5)

            # Degree labels for main positions
            if g_deg in [-158, -90, 0, 90]:
                x_lbl = cx + (r - 26) * cos_a
                y_lbl = cy - (r - 26) * sin_a
                lbl_str = f"{g_deg}°"
                self.canvas.create_text(x_lbl, y_lbl, text=lbl_str, fill="#ffffff", font=("Roboto", 9, "bold"))

        # Center indicator / Scanner reference
        self.canvas.create_oval(cx - 6, cy - 6, cx + 6, cy + 6, fill="#3a7ebf", outline="")
        self.canvas.create_line(cx, cy, cx, cy + r - 15, fill="#3a7ebf", dash=(2, 2), width=1)

        # Draw Pass Markers
        for idx, p_deg in enumerate(self.passes):
            polar_rad = math.radians(270.0 + p_deg)
            cos_a = math.cos(polar_rad)
            sin_a = math.sin(polar_rad)

            mx = cx + (r - 5) * cos_a
            my = cy - (r - 5) * sin_a

            is_active = (idx == self.active_pass_idx)
            mr = 12 if is_active else 8

            # Radial line to active marker
            if is_active:
                self.canvas.create_line(cx, cy, mx, my, fill="#00d2ff", width=2)

            fill_color = "#00d2ff" if is_active else "#444444"
            outline_color = "#ffffff" if is_active else "#888888"
            text_color = "#000000" if is_active else "#ffffff"

            self.canvas.create_oval(
                mx - mr, my - mr, mx + mr, my + mr,
                fill=fill_color, outline=outline_color, width=2 if is_active else 1
            )
            self.canvas.create_text(
                mx, my,
                text=str(idx + 1),
                fill=text_color,
                font=("Roboto", 10 if is_active else 8, "bold")
            )

    def _event_to_deg(self, event) -> Optional[float]:
        dx = event.x - self.cx
        dy = self.cy - event.y  # Inverted y
        if math.hypot(dx, dy) < 10:  # Ignore near center
            return None

        polar_rad = math.atan2(dy, dx)
        polar_deg = math.degrees(polar_rad) % 360.0

        deg = (polar_deg - 270.0) % 360.0
        if deg > 180.0:
            deg -= 360.0

        # Clamp to valid [-158°, +90°]
        if deg > 90.0:
            if deg <= 136.0:
                deg = 90.0
            else:
                deg = -158.0
        elif deg < -158.0:
            deg = -158.0

        return deg

    def _on_canvas_click(self, event):
        # First check if user clicked an existing marker to select it
        for idx, p_deg in enumerate(self.passes):
            polar_rad = math.radians(270.0 + p_deg)
            mx = self.cx + (self.r - 5) * math.cos(polar_rad)
            my = self.cy - (self.r - 5) * math.sin(polar_rad)
            if math.hypot(event.x - mx, event.y - my) <= 16:
                self.select_pass(idx)
                return

        # Otherwise move active marker to clicked angle
        deg = self._event_to_deg(event)
        if deg is not None:
            self.passes[self.active_pass_idx] = round(deg, 1)
            self._sync_active_entry()
            self.draw_dial()
            self._notify_change()

    def _on_canvas_drag(self, event):
        deg = self._event_to_deg(event)
        if deg is not None:
            self.passes[self.active_pass_idx] = round(deg, 1)
            self._sync_active_entry()
            self.draw_dial()
            self._notify_change()

    def _sync_active_entry(self):
        idx = self.active_pass_idx
        if idx < len(self.entry_vars):
            p_deg = self.passes[idx]
            self.entry_vars[idx].set(f"{p_deg:.1f}")
            enc_val = deg_to_encoder(p_deg)
            self.encoder_labels[idx].configure(text=f"Raw Encoder: {enc_val:+d} pulses")

    def _notify_change(self):
        if self.on_change_callback:
            self.on_change_callback(self.get_pulses())

    def get_pulses(self) -> List[int]:
        return [deg_to_encoder(d) for d in self.passes]

    def set_pulses(self, pulses: List[int]):
        if not pulses:
            return
        self.passes = [encoder_to_deg(p) for p in pulses]
        self.active_pass_idx = min(self.active_pass_idx, len(self.passes) - 1)
        self.pass_count_var.set(str(len(self.passes)))
        self._rebuild_pass_entries()
        self.draw_dial()

# Backend imports
from hardware.run_precision_scan import (
    ModbusSerialClient,
    UNIT_ID_PAN,
    UNIT_ID_TILT,
    SERIAL_PORT,
    BAUDRATE,
    TIMEOUT,
    PULSES_PER_REV_PAN,
    PULSES_PER_REV_TILT,
    read_motor_status,
    check_motor_alarm,
    emergency_stop,
    send_absolute_move,
    read_encoder_position,
    SafetyWatchdog,
    # perform_raster_sweep, # Removed in favor of scan_sequence
    PipelineError
)
from scan_sequence import run_scan_sequence, ModbusLink
from reconstruction.barrel_reconstruct import run_reconstruction_pipeline # Assumed entry point
from reconstruction.barrel_batch import save_to_log # Assumed entry point
from hardware.creality_autostart import CrealityAutomator, AutomationError
from config_manager import (
    load_config,
    save_config,
    reset_to_defaults,
    parse_tilt_targets_str,
    tilt_targets_to_str,
    VALID_CLEANUP_MODES
)

# Setup logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("OperatorDashboard")

class AppState(Enum):
    READY = auto()
    SCANNING = auto()
    PROCESSING = auto()
    RESULTS = auto()
    ERROR = auto()
    TECHNICIAN = auto()

class WorkerMessage:
    """Message object for communication from background thread to UI."""
    def __init__(self, type: str, data=None, error=None):
        self.type = type # 'progress', 'status', 'result', 'error'
        self.data = data
        self.error = error

class AppGUI(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Barrel Scanner Operator Dashboard")
        self.geometry("1100x700")
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # --- Backend State ---
        self._app_state = AppState.READY
        self.client: Optional[ModbusSerialClient] = None
        self.link: Optional[ModbusLink] = None
        self.watchdog: Optional[SafetyWatchdog] = None
        self.automator = CrealityAutomator()
        self.worker_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.last_results: Optional[dict] = None

        # Config state
        self.scan_config = load_config()

        # Operator Data
        self.barrel_id = tk.StringVar()
        self.operator_name = tk.StringVar()

        # --- UI Layout ---
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # 1. Top Status Bar
        self.status_bar = ctk.CTkFrame(self, height=40, corner_radius=0)
        self.status_bar.grid(row=0, column=0, sticky="ew")
        
        self.conn_indicator = ctk.CTkLabel(self.status_bar, text="● Motors: Disconnected", text_color="red")
        self.conn_indicator.pack(side="left", padx=20)
        
        self.scanner_indicator = ctk.CTkLabel(self.status_bar, text="● Scanner: Disconnected", text_color="red")
        self.scanner_indicator.pack(side="left", padx=20)
        
        self.ready_indicator = ctk.CTkLabel(self.status_bar, text="● Ready to Scan: NO", text_color="red")
        self.ready_indicator.pack(side="left", padx=20)

        # 2. Main Content Area (Container for frames)
        self.main_container = ctk.CTkFrame(self)
        self.main_container.grid(row=1, column=0, sticky="nsew", padx=20, pady=20)
        self.main_container.grid_columnconfigure(0, weight=1)
        self.main_container.grid_rowconfigure(0, weight=1)

        # 3. Persistent Footer / E-Stop
        self.footer = ctk.CTkFrame(self, height=100)
        self.footer.grid(row=2, column=0, sticky="ew")
        
        self.estop_button = ctk.CTkButton(
            self.footer, 
            text="E-STOP", 
            fg_color="red", 
            hover_color="darkred", 
            text_color="white",
            font=("Roboto", 24, "bold"),
            command=self.handle_emergency_stop
        )
        self.estop_button.pack(side="right", padx=30, pady=20)
        
        self.support_btn = ctk.CTkButton(self.footer, text="Call Support", fg_color="transparent", border_width=1, command=lambda: log.info("Support requested"))
        self.support_btn.pack(side="left", padx=30, pady=20)

        # Initialize Screens
        self.frames = {}
        self.init_frames()
        
        # Start Connectivity Polling
        self.poll_connections()
        # Start Queue Listener
        self.after(100, self.process_queue)

    def init_frames(self):
        """Initialize all screen frames but don't show them yet."""
        # For now, we create placeholders. I will build them in detail in next steps.
        self.frames[AppState.READY] = self.create_ready_frame()
        self.frames[AppState.SCANNING] = self.create_scanning_frame()
        self.frames[AppState.PROCESSING] = self.create_processing_frame()
        self.frames[AppState.RESULTS] = self.create_results_frame()
        self.frames[AppState.ERROR] = self.create_error_frame()
        self.frames[AppState.TECHNICIAN] = self.create_technician_frame()
        
        self.show_frame(AppState.READY)

    def show_frame(self, state: AppState):
        self._app_state = state
        for frame in self.frames.values():
            frame.grid_forget()
        
        frame = self.frames[state]
        frame.grid(row=0, column=0, sticky="nsew")
        self.main_container.grid_columnconfigure(0, weight=1)
        self.main_container.grid_rowconfigure(0, weight=1)

    # --- Placeholder Frame Creators (To be expanded) ---
    def create_ready_frame(self):
        frame = ctk.CTkFrame(self.main_container)
        frame.grid_columnconfigure(0, weight=1)
        
        title_lbl = ctk.CTkLabel(frame, text="System Ready", font=("Roboto", 32, "bold"))
        title_lbl.pack(pady=(40, 20))

        # Operator Input Section
        input_frame = ctk.CTkFrame(frame, fg_color="transparent")
        input_frame.pack(pady=20, padx=50, fill="x")
        
        # Barrel ID
        id_container = ctk.CTkFrame(input_frame, fg_color="transparent")
        id_container.pack(side="left", expand=True, padx=10)
        ctk.CTkLabel(id_container, text="Barrel ID:").pack()
        self.barrel_id_entry = ctk.CTkEntry(id_container, textvariable=self.barrel_id, width=200)
        self.barrel_id_entry.pack(pady=5)
        
        # Operator Name
        op_container = ctk.CTkFrame(input_frame, fg_color="transparent")
        op_container.pack(side="left", expand=True, padx=10)
        ctk.CTkLabel(op_container, text="Operator:").pack()
        self.operator_entry = ctk.CTkEntry(op_container, textvariable=self.operator_name, width=200)
        self.operator_entry.pack(pady=5)

        # Start Button
        self.start_btn = ctk.CTkButton(
            frame, 
            text="START SCAN", 
            font=("Roboto", 20, "bold"),
            height=60,
            width=300,
            state="disabled", 
            command=self.start_pipeline
        )
        self.start_btn.pack(pady=60)
        
        # Helper Links
        links_frame = ctk.CTkFrame(frame, fg_color="transparent")
        links_frame.pack(pady=20)
        
        ctk.CTkButton(links_frame, text="System Check", fg_color="transparent", 
                     command=self.poll_connections).pack(side="left", padx=10)
        ctk.CTkButton(links_frame, text="Scan History", fg_color="transparent", 
                     command=lambda: self.show_frame(AppState.RESULTS)).pack(side="left", padx=10)
        ctk.CTkButton(links_frame, text="Advanced / Technician", fg_color="transparent", 
                     command=lambda: self.show_frame(AppState.TECHNICIAN)).pack(side="left", padx=10)
        
        return frame

    def create_scanning_frame(self):
        frame = ctk.CTkFrame(self.main_container)
        frame.grid_columnconfigure(0, weight=1)
        
        # Back Button
        back_btn = ctk.CTkButton(frame, text="← Back", width=60, fg_color="transparent", border_width=1, 
                                 command=lambda: self.show_frame(AppState.READY))
        back_btn.pack(anchor="nw", padx=20, pady=20)
        
        title_lbl = ctk.CTkLabel(frame, text="Scanning in Progress", font=("Roboto", 32, "bold"))
        title_lbl.pack(pady=(0, 20))

        # Progress Area
        prog_container = ctk.CTkFrame(frame, fg_color="transparent")
        prog_container.pack(pady=20, padx=50, fill="x")
        
        self.scan_progress_lbl = ctk.CTkLabel(prog_container, text="Initializing...", font=("Roboto", 18))
        self.scan_progress_lbl.pack(pady=10)
        
        self.scan_progress_bar = ctk.CTkProgressBar(prog_container)
        self.scan_progress_bar.pack(pady=10, padx=20, fill="x")
        self.scan_progress_bar.set(0)
        
        # Timer Section
        self.timer_lbl = ctk.CTkLabel(frame, text="Elapsed Time: 00:00", font=("Roboto", 14))
        self.timer_lbl.pack(pady=10)

        # Control Section
        self.stop_scan_btn = ctk.CTkButton(
            frame, 
            text="STOP SCAN", 
            fg_color="gray", 
            hover_color="darkgray",
            command=self.handle_controlled_stop
        )
        self.stop_scan_btn.pack(pady=40)
        
        return frame

    def create_processing_frame(self):
        frame = ctk.CTkFrame(self.main_container)
        frame.grid_columnconfigure(0, weight=1)
        
        # Back Button
        back_btn = ctk.CTkButton(frame, text="← Back", width=60, fg_color="transparent", border_width=1, 
                                 command=lambda: self.show_frame(AppState.SCANNING))
        back_btn.pack(anchor="nw", padx=20, pady=20)
        
        title_lbl = ctk.CTkLabel(frame, text="Processing Scan Data", font=("Roboto", 32, "bold"))
        title_lbl.pack(pady=(0, 20))

        # Phase Text
        self.proc_phase_lbl = ctk.CTkLabel(frame, text="Loading scan data...", font=("Roboto", 18))
        self.proc_phase_lbl.pack(pady=20)
        
        # Indeterminate Progress Bar
        self.proc_progress_bar = ctk.CTkProgressBar(frame, mode="indeterminate")
        self.proc_progress_bar.pack(pady=10, padx=50, fill="x")
        self.proc_progress_bar.start()
        
        # Status box
        self.proc_status_box = ctk.CTkTextbox(frame, height=150, width=500, font=("Consolas", 12))
        self.proc_status_box.pack(pady=20)
        self.proc_status_box.insert("0.0", "Initializing reconstruction pipeline...\n")
        self.proc_status_box.configure(state="disabled")
        
        return frame

    def create_results_frame(self):
        frame = ctk.CTkFrame(self.main_container)
        frame.grid_columnconfigure(0, weight=1)
        
        # Back Button
        back_btn = ctk.CTkButton(frame, text="← Back", width=60, fg_color="transparent", border_width=1, 
                                 command=lambda: self.show_frame(AppState.PROCESSING))
        back_btn.pack(anchor="nw", padx=20, pady=20)
        
        title_lbl = ctk.CTkLabel(frame, text="Measurement Results", font=("Roboto", 32, "bold"))
        title_lbl.pack(pady=(0, 20))

        # Main Results Grid
        res_grid = ctk.CTkFrame(frame, fg_color="transparent")
        res_grid.pack(pady=20, padx=50, fill="x")
        
        # Volume
        vol_container = ctk.CTkFrame(res_grid, fg_color="#2b2b2b")
        vol_container.pack(side="left", expand=True, padx=10, pady=10)
        ctk.CTkLabel(vol_container, text="Internal Volume", font=("Roboto", 14)).pack(pady=5)
        self.res_volume = ctk.CTkLabel(vol_container, text="-- L", font=("Roboto", 28, "bold"))
        self.res_volume.pack(pady=10)
        self.res_vol_gal = ctk.CTkLabel(vol_container, text="-- gal", font=("Roboto", 12), text_color="gray")
        self.res_vol_gal.pack(pady=5)
        
        # Surface Area
        surf_container = ctk.CTkFrame(res_grid, fg_color="#2b2b2b")
        surf_container.pack(side="left", expand=True, padx=10, pady=10)
        ctk.CTkLabel(surf_container, text="Internal Surface Area", font=("Roboto", 14)).pack(pady=5)
        self.res_surface = ctk.CTkLabel(surf_container, text="-- m²", font=("Roboto", 28, "bold"))
        self.res_surface.pack(pady=10)
        
        # Quality Flag
        qual_container = ctk.CTkFrame(res_grid, fg_color="#2b2b2b")
        qual_container.pack(side="left", expand=True, padx=10, pady=10)
        ctk.CTkLabel(qual_container, text="Scan Quality", font=("Roboto", 14)).pack(pady=5)
        self.res_quality = ctk.CTkLabel(qual_container, text="--", font=("Roboto", 28, "bold"))
        self.res_quality.pack(pady=10)

        # Secondary Metrics (dynamic list)
        self.secondary_metrics_frame = ctk.CTkFrame(frame, fg_color="transparent")
        self.secondary_metrics_frame.pack(pady=20, padx=50, fill="x")
        
        # Operator Info
        info_lbl = ctk.CTkLabel(frame, text=f"Barrel ID: {self.barrel_id.get()} | Operator: {self.operator_name.get()}", font=("Roboto", 12), text_color="gray")
        info_lbl.pack(pady=10)

        # Actions
        btn_frame = ctk.CTkFrame(frame, fg_color="transparent")
        btn_frame.pack(pady=30)
        
        self.save_log_btn = ctk.CTkButton(btn_frame, text="Save & Add to Log", command=self.save_results_to_log)
        self.save_log_btn.pack(side="left", padx=10)
        
        btn_home = ctk.CTkButton(btn_frame, text="New Scan", fg_color="gray", command=lambda: self.show_frame(AppState.READY))
        btn_home.pack(side="left", padx=10)
        
        return frame

    def create_error_frame(self):
        frame = ctk.CTkFrame(self.main_container)
        frame.grid_columnconfigure(0, weight=1)
        
        self.error_title = ctk.CTkLabel(frame, text="System Error", text_color="red", font=("Roboto", 32, "bold"))
        self.error_title.pack(pady=(40, 10))
        
        self.error_cause = ctk.CTkLabel(frame, text="Likely cause: Unknown", font=("Roboto", 16), text_color="gray")
        self.error_cause.pack(pady=5)
        
        self.error_msg = ctk.CTkLabel(frame, text="An unexpected error occurred.", wraplength=600, font=("Roboto", 18))
        self.error_msg.pack(pady=20)
        
        # Recovery Actions Container
        self.recovery_frame = ctk.CTkFrame(frame, fg_color="transparent")
        self.recovery_frame.pack(pady=30)
        
        self.error_recovery_btn = ctk.CTkButton(
            self.recovery_frame, 
            text="Return Home", 
            command=lambda: self.show_frame(AppState.READY)
        )
        self.error_recovery_btn.pack(pady=10)
        
        # Advanced Details (Collapsed)
        self.details_collapsed = True
        self.details_btn = ctk.CTkButton(
            frame, 
            text="Show Technical Details", 
            fg_color="transparent", 
            border_width=1,
            command=self.toggle_details
        )
        self.details_btn.pack(pady=20)
        
        self.details_box = ctk.CTkTextbox(frame, height=100, width=600, font=("Consolas", 12))
        # Hidden by default
        
        return frame

    def toggle_details(self):
        if self.details_collapsed:
            self.details_box.pack(pady=10)
            self.details_btn.configure(text="Hide Technical Details")
            self.details_collapsed = False
        else:
            self.details_box.pack_forget()
            self.details_btn.configure(text="Show Technical Details")
            self.details_collapsed = True

    def handle_pipeline_error(self, error):
        log.error(f"Pipeline Error: {error}")
        
        # Map error types to friendly messages
        error_templates = {
            "ModbusException": {
                "title": "Can't reach the turret motors.",
                "cause": "Likely cause: Modbus link down or cable disconnected.",
                "action": "Retry Connection",
                "cmd": self.poll_connections
            },
            "AutomationError": {
                "title": "Scanner software not found.",
                "cause": "Likely cause: Creality Scan is not running or window is minimized.",
                "action": "Reset Scanner Link",
                "cmd": self.start_pipeline
            },
            "TimeoutError": {
                "title": "Turret stopped unexpectedly.",
                "cause": "Likely cause: Position error or mechanical stall.",
                "action": "Home Turret & Retry",
                "cmd": self.start_pipeline
            },
            "EmergencyStop": {
                "title": "Emergency stop activated.",
                "cause": "Likely cause: E-Stop button pressed.",
                "action": "Clear Fault & Return Home",
                "cmd": lambda: self.show_frame(AppState.READY)
            }
        }
        
        # Find best match or use generic
        err_type = type(error).__name__ if not isinstance(error, str) else "Generic"
        template = error_templates.get(err_type, {
            "title": "System Error",
            "cause": "Likely cause: Internal software exception.",
            "action": "Return Home",
            "cmd": lambda: self.show_frame(AppState.READY)
        })
        
        self.show_frame(AppState.ERROR)
        self.error_title.configure(text=template["title"])
        self.error_cause.configure(text=template["cause"])
        self.error_msg.configure(text=str(error))
        self.error_recovery_btn.configure(text=template["action"], command=template["cmd"])
        
        self.details_box.configure(state="normal")
        self.details_box.delete("0.0", "end")
        self.details_box.insert("0.0", f"Exception Type: {err_type}\nDetails: {error}")
        self.details_box.configure(state="disabled")
        self.details_box.pack_forget() # ensure it's hidden initially

    def handle_emergency_stop(self):
        """Immediate E-Stop path."""
        log.critical("E-STOP TRIGGERED FROM GUI")
        if self.client:
            emergency_stop(self.client)
        self.stop_event.set()
        self.show_frame(AppState.ERROR)
        
        # Use the new template system for E-Stop
        self.error_title.configure(text="Emergency stop activated.")
        self.error_cause.configure(text="Likely cause: E-Stop button pressed.")
        self.error_msg.configure(text="Motors are powered down. Please ensure area is clear before resetting.")
        self.error_recovery_btn.configure(text="Clear Fault & Return Home", command=lambda: self.show_frame(AppState.READY))
        
        self.details_box.configure(state="normal")
        self.details_box.delete("0.0", "end")
        self.details_box.insert("0.0", "Trigger: Hardware E-Stop Button\nStatus: All motion halted, drives disabled.")
        self.details_box.configure(state="disabled")

    def handle_controlled_stop(self):
        """Controlled stop: halts motion and prompts for save/discard."""
        log.info("Controlled stop requested.")
        # Signal the worker thread to stop
        self.stop_event.set()
        
        # Stop Creality scanner process
        try:
            threading.Thread(target=self.automator.stop_scan, daemon=True).start()
        except Exception as e:
            log.warning("Failed to stop Creality Scan software: %s", e)

        self.show_frame(AppState.ERROR)
        self.error_title.configure(text="Scan Halted")
        self.error_msg.configure(text="The scan was stopped by the operator. Please decide whether to save the partial data in the technician panel.")

    def _watchdog_fault_callback(self, error):
        """
        SafetyWatchdog polls hardware registers on a background thread. Tk widgets may
        only be touched from the main thread, so route the fault through the same
        worker_queue / process_queue path the pipeline worker uses instead of calling
        handle_pipeline_error() directly from that thread.
        """
        self.worker_queue.put(WorkerMessage('error', error=error))

    def start_pipeline(self):
        """Launch the background worker for the scan pipeline."""
        if self.client is None or self.link is None:
            self.handle_pipeline_error(PipelineError(
                "Motor link is not connected. Run System Check before starting a scan."
            ))
            return

        self.stop_event.clear()
        self.show_frame(AppState.SCANNING)
        self.scan_progress_bar.set(0)
        self.scan_progress_lbl.configure(text="Initializing...")

        # Start the elapsed-time timer here (on the main/Tk thread) rather than from the
        # background worker -- Tk widgets must only be touched from the main thread.
        self.start_time = time.time()
        self.update_timer_loop()

        # Start Safety Watchdog
        self.watchdog = SafetyWatchdog(self.client, self._watchdog_fault_callback)
        self.watchdog.start()

        # Launch Worker
        threading.Thread(target=self.pipeline_worker, daemon=True).start()

    def pipeline_worker(self):
        """Background thread for scan -> reconstruct -> log."""
        try:
            # 1. Scanning Phase
            def progress_cb(msg):
                self.worker_queue.put(WorkerMessage('progress', data=msg))

            if self.link is None:
                raise PipelineError("Motor link is not connected. Cannot run scan sequence.")

            # Trigger Creality Scan autostart sequence. If this fails, the motors must
            # NOT proceed to sweep with no active capture running -- surface it instead
            # of silently continuing as if the scan had started.
            log.info("Triggering Creality Scan autostart sequence...")
            try:
                self.automator.start_scan()
            except Exception as scan_err:
                raise PipelineError(
                    f"Failed to start Creality Scan capture: {scan_err}"
                ) from scan_err

            # Execute the raster sweep using scan_sequence.py. Note: this needs the
            # ModbusLink wrapper (self.link), NOT the raw pymodbus client (self.client) --
            # ESS17Controller/IDM57Controller call link.read_reg()/write_reg()/write_regs(),
            # which the raw ModbusSerialClient does not implement.
            run_scan_sequence(self.link, config=self.scan_config, progress_callback=progress_cb)

            # Stop scan in scanner UI
            try:
                self.automator.stop_scan()
            except Exception as stop_err:
                log.warning("Creality Scan stop note: %s", stop_err)

            # 2. Processing Phase
            self.worker_queue.put(WorkerMessage('status', data='processing'))
            self.after(0, lambda: self.show_frame(AppState.PROCESSING))

            # Export scan file via CrealityAutomator
            barrel_id_str = self.barrel_id.get().strip() if self.barrel_id.get() else "default"
            try:
                scan_file_path = self.automator.export_scan(barrel_id=barrel_id_str)
            except Exception as exp_err:
                log.warning("Creality export fallback: %s", exp_err)
                scan_file_path = "saved_creality_files/fallback.obscan"

            log.info("Scan output recorded at: %s", scan_file_path)

            # 3. Reconstruction Phase -- run the real volume/surface pipeline on the
            # exported .obscan file and translate its result schema into what the
            # Results screen expects.
            progress_cb("Running 3D reconstruction pipeline (this can take a minute)...")
            cleanup_mode = self.scan_config.get("reconstruction_settings", {}).get("cleanup_mode", "rules")
            try:
                recon_result = run_reconstruction_pipeline(scan_file_path, cleanup_mode=cleanup_mode)
            except Exception as recon_err:
                raise PipelineError(f"Reconstruction failed for '{scan_file_path}': {recon_err}") from recon_err

            results_payload = self._translate_reconstruction_result(recon_result)

            # 4. Results Phase
            self.worker_queue.put(WorkerMessage('result', data=results_payload))

        except Exception as e:
            self.worker_queue.put(WorkerMessage('error', error=e))
        finally:
            if self.watchdog:
                self.watchdog.stop()

    def _translate_reconstruction_result(self, recon_result: dict) -> dict:
        """
        Convert the raw summary dict returned by reconstruction.barrel_reconstruct's
        run_reconstruction_pipeline() (shaped like {"clean": {"vol_L", "area",
        "watertight", ...}, "axisym": {...}, "fidelity_rms", "asym_rms", "bung", ...})
        into the {'volume', 'surface', 'quality', 'metrics'} schema handle_results() /
        the Results screen expect.
        """
        clean = recon_result.get("clean", {}) if recon_result else {}
        volume_l = clean.get("vol_L")
        area_m2 = clean.get("area")
        watertight = clean.get("watertight", False)
        fidelity_rms = recon_result.get("fidelity_rms") if recon_result else None

        quality = "Good"
        if not watertight:
            quality = "Review"
        elif fidelity_rms is not None and fidelity_rms > 2.0:
            quality = "Review"

        metrics = {}
        if recon_result:
            if recon_result.get("length") is not None:
                metrics["Axial Length"] = f"{recon_result['length'] * 1000:.0f}mm"
            if fidelity_rms is not None:
                metrics["Fidelity RMS"] = f"{fidelity_rms:.2f}mm"
            if recon_result.get("asym_rms") is not None:
                metrics["Asymmetry RMS"] = f"{recon_result['asym_rms']:.2f}mm"
            if recon_result.get("bung"):
                metrics["Bung Cells"] = str(recon_result["bung"])
            if clean.get("v") is not None and clean.get("f") is not None:
                metrics["Mesh"] = f"{clean['v']}v / {clean['f']}f"
            metrics["Watertight"] = "Yes" if watertight else "No"

        return {
            'volume': round(volume_l, 2) if isinstance(volume_l, (int, float)) else '--',
            'surface': round(area_m2, 3) if isinstance(area_m2, (int, float)) else '--',
            'quality': quality,
            'metrics': metrics,
            'raw': recon_result,
        }

    def update_timer_loop(self):
        """Update the elapsed time label on the Scanning screen."""
        if self._app_state == AppState.SCANNING:
            elapsed = int(time.time() - self.start_time)
            mins, secs = divmod(elapsed, 60)
            self.timer_lbl.configure(text=f"Elapsed Time: {mins:02d}:{secs:02d}")
            self.after(1000, self.update_timer_loop)


    def handle_results(self, data):
        # data is expected to be a dict: {'volume': float, 'surface': float, 'quality': str, 'metrics': dict}
        self.last_results = data
        self.res_volume.configure(text=f"{data.get('volume', '--')} L")
        self.res_vol_gal.configure(text=f"{ (data.get('volume', 0) * 0.264172 if isinstance(data.get('volume'), (int, float)) else '--') } gal")
        self.res_surface.configure(text=f"{data.get('surface', '--')} m²")
        self.res_quality.configure(
            text=data.get('quality', '--'),
            text_color="green" if data.get('quality') == "Good" else "orange"
        )
        
        # Clear and populate secondary metrics
        for child in self.secondary_metrics_frame.winfo_children():
            child.destroy()
            
        metrics = data.get('metrics', {})
        if metrics:
            m_grid = ctk.CTkFrame(self.secondary_metrics_frame, fg_color="transparent")
            m_grid.pack(pady=10, fill="x")
            m_grid.grid_columnconfigure((0, 1, 2), weight=1)
            
            for i, (k, v) in enumerate(metrics.items()):
                ctk.CTkLabel(m_grid, text=f"{k}: {v}", font=("Roboto", 12)).grid(row=i//3, column=i%3, padx=20, pady=5)

        self.show_frame(AppState.RESULTS)

    def save_results_to_log(self):
        """Persist the current scan results to a CSV log on disk (and the text log)."""
        if not self.last_results:
            log.warning("No results available to save yet.")
            return

        log.info("Saving results to log...")
        data = self.last_results
        try:
            log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan_results_log.csv")
            file_exists = os.path.exists(log_path)
            with open(log_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(["timestamp", "barrel_id", "operator", "volume_L", "surface_m2", "quality"])
                writer.writerow([
                    datetime.now().isoformat(timespec="seconds"),
                    self.barrel_id.get(),
                    self.operator_name.get(),
                    data.get("volume"),
                    data.get("surface"),
                    data.get("quality"),
                ])

            save_to_log(
                f"Saved scan for barrel '{self.barrel_id.get()}' by "
                f"'{self.operator_name.get()}': {data.get('volume')} L, "
                f"{data.get('surface')} m^2, quality={data.get('quality')}"
            )

            self.save_log_btn.configure(text="Saved!", state="disabled", fg_color="green")
            self.after(2000, lambda: self.save_log_btn.configure(text="Save & Add to Log", state="normal", fg_color=["#3a7ebf", "#1f538d"]))
        except Exception as e:
            log.error("Failed to save results to log: %s", e)
            self.save_log_btn.configure(text="Save Failed", fg_color="red")
            self.after(2500, lambda: self.save_log_btn.configure(text="Save & Add to Log", state="normal", fg_color=["#3a7ebf", "#1f538d"]))

    def jog_axis(self, unit_id: int, revs: int):
        """Manual jog for technician panel."""
        log.info("Jogging axis %d by %d revs", unit_id, revs)
        if not self.client:
            log.warning("Jog ignored: motor client is not connected.")
            return
        try:
            pulses_per_rev = PULSES_PER_REV_PAN if unit_id == UNIT_ID_PAN else PULSES_PER_REV_TILT
            curr = read_encoder_position(self.client, unit_id)
            target = curr + (revs * pulses_per_rev)
            send_absolute_move(self.client, unit_id, target)
        except Exception as e:
            log.error("Jog failed: %s", e)

    def _init_modbus_client(self, port: str, baud: int) -> bool:
        """
        (Re)initialize the shared Modbus client used for status polling, jogging, and
        the safety watchdog, plus the ModbusLink wrapper the scan sequence needs.
        The link reuses the same underlying connection -- opening a second handle to
        the same serial port would fail or contend with the first.
        """
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass

        self.client = ModbusSerialClient(port=port, baudrate=baud, timeout=TIMEOUT)
        connected = self.client.connect()
        self.link = ModbusLink(port=port, baudrate=baud, client=self.client)
        return connected

    def apply_settings(self):
        """Update Modbus client settings from the Technician panel fields."""
        new_port = self.port_entry.get().strip()
        new_baud = int(self.baud_entry.get().strip())
        log.info("Applying settings: Port=%s, Baud=%d", new_port, new_baud)
        self._init_modbus_client(new_port, new_baud)

    def manual_reconstruct(self):
        """Run reconstruction on a specific file, entered by a technician."""
        path = self.file_path_entry.get().strip()
        if not path:
            log.warning("No path provided for manual reconstruction.")
            return
        log.info("Starting manual reconstruction for: %s", path)
        # Trigger worker thread in PROCESSING state
        self.show_frame(AppState.PROCESSING)
        threading.Thread(target=self.manual_reconstruct_worker, args=(path,), daemon=True).start()

    def manual_reconstruct_worker(self, path: str):
        try:
            cleanup_mode = self.scan_config.get("reconstruction_settings", {}).get("cleanup_mode", "rules")
            recon_result = run_reconstruction_pipeline(path, cleanup_mode=cleanup_mode)
            results_payload = self._translate_reconstruction_result(recon_result)
            self.worker_queue.put(WorkerMessage('result', data=results_payload))
        except Exception as e:
            self.worker_queue.put(WorkerMessage('error', error=e))

    def poll_connections(self):
        """Periodically check connection status of turret and scanner."""
        log.info("Polling connections...")
        
        # 1. Check Turret Connectivity (Modbus)
        turret_ok = False
        try:
            if self.client is None:
                # Try to initialize client if it doesn't exist, using the configured
                # port/baud from scan_config.json (falls back to hardware defaults).
                m_cfg = self.scan_config.get("motor_settings", {})
                self._init_modbus_client(
                    m_cfg.get("port", SERIAL_PORT),
                    m_cfg.get("baudrate", BAUDRATE),
                )

            if self.client.connected:
                # Try to read a status register to verify actual communication
                status = read_motor_status(self.client, UNIT_ID_PAN)
                if status is not None:
                    turret_ok = True
        except Exception as e:
            log.debug("Turret poll failed: %s", e)

        # 2. Check Scanner Connectivity
        scanner_ok = False 
        try:
            if self.automator.is_window_available(): 
                scanner_ok = True 
        except Exception as e:
            log.debug("Scanner poll failed: %s", e)

        # Update UI
        self.conn_indicator.configure(
            text="● Turret: Connected" if turret_ok else "● Turret: Disconnected", 
            text_color="green" if turret_ok else "red"
        )
        self.scanner_indicator.configure(
            text="● Scanner: Connected" if scanner_ok else "● Scanner: Disconnected", 
            text_color="green" if scanner_ok else "red"
        )
        
        # 3. Check Overall Readiness
        ready_ok = turret_ok and scanner_ok
        self.ready_indicator.configure(
            text="● Ready to Scan: YES" if ready_ok else "● Ready to Scan: NO", 
            text_color="green" if ready_ok else "red"
        )
        
        # Update Start Button state
        if hasattr(self, 'start_btn'):
            self.start_btn.configure(state="normal" if ready_ok else "disabled")
        
        # Schedule next poll in 5 seconds
        self.after(5000, self.poll_connections)

    def process_queue(self):
        """Process messages from the background worker thread."""
        try:
            while True:
                msg = self.worker_queue.get_nowait()
                if msg.type == 'progress':
                    self.scan_progress_lbl.configure(text=msg.data)
                    # Estimate progress bar from message text if possible
                    # For now just increment slightly
                    curr = self.scan_progress_bar.get()
                    self.scan_progress_bar.set(min(1.0, curr + 0.01))
                elif msg.type == 'status':
                    log.info(f"Pipeline status: {msg.data}")
                elif msg.type == 'result':
                    self.after(0, lambda d=msg.data: self.handle_results(d))
                elif msg.type == 'error':
                    self.after(0, lambda e=msg.error: self.handle_pipeline_error(e))
        except queue.Empty:
            pass
        # Schedule next check
        self.after(100, self.process_queue)

    def create_technician_frame(self):
        frame = ctk.CTkFrame(self.main_container)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)

        # Back Button / Title Header
        header_frame = ctk.CTkFrame(frame, fg_color="transparent")
        header_frame.pack(fill="x", padx=20, pady=(15, 5))

        back_btn = ctk.CTkButton(header_frame, text="← Back", width=60, fg_color="transparent", border_width=1,
                                 command=lambda: self.show_frame(AppState.READY))
        back_btn.pack(side="left", padx=10)

        title_lbl = ctk.CTkLabel(header_frame, text="Technician & Configuration Panel", font=("Roboto", 28, "bold"))
        title_lbl.pack(side="left", padx=20)

        # Scrollable Content Area
        scroll_container = ctk.CTkScrollableFrame(frame)
        scroll_container.pack(fill="both", expand=True, padx=20, pady=10)
        scroll_container.grid_columnconfigure(0, weight=1)

        # -------------------------------------------------------------------
        # Section 1: Motor Hardware & Software Sweep Configuration
        # -------------------------------------------------------------------
        config_box = ctk.CTkFrame(scroll_container)
        config_box.pack(fill="x", padx=20, pady=10)

        ctk.CTkLabel(config_box, text="Motor & Software Sweep Configuration", font=("Roboto", 18, "bold")).pack(pady=(15, 5))
        ctk.CTkLabel(config_box, text="Configure motor speeds, Modbus settings, and pass-through sweep angles", font=("Roboto", 12), text_color="gray").pack(pady=(0, 15))

        grid_frame = ctk.CTkFrame(config_box, fg_color="transparent")
        grid_frame.pack(fill="x", padx=20, pady=10)
        grid_frame.grid_columnconfigure((0, 1, 2, 3), weight=1)

        # Row 0: Comms Parameters
        ctk.CTkLabel(grid_frame, text="Serial Port:").grid(row=0, column=0, padx=10, pady=5, sticky="e")
        self.port_entry = ctk.CTkEntry(grid_frame, width=140)
        self.port_entry.grid(row=0, column=1, padx=10, pady=5, sticky="w")

        ctk.CTkLabel(grid_frame, text="Baud Rate:").grid(row=0, column=2, padx=10, pady=5, sticky="e")
        self.baud_entry = ctk.CTkEntry(grid_frame, width=140)
        self.baud_entry.grid(row=0, column=3, padx=10, pady=5, sticky="w")

        # Row 1: Slave IDs
        ctk.CTkLabel(grid_frame, text="Tilt Slave ID (ESS17):").grid(row=1, column=0, padx=10, pady=5, sticky="e")
        self.tilt_slave_entry = ctk.CTkEntry(grid_frame, width=140)
        self.tilt_slave_entry.grid(row=1, column=1, padx=10, pady=5, sticky="w")

        ctk.CTkLabel(grid_frame, text="Rot Slave ID (iDM57):").grid(row=1, column=2, padx=10, pady=5, sticky="e")
        self.rot_slave_entry = ctk.CTkEntry(grid_frame, width=140)
        self.rot_slave_entry.grid(row=1, column=3, padx=10, pady=5, sticky="w")

        # Row 2: Motor Speeds
        ctk.CTkLabel(grid_frame, text="Tilt Speed (RPM):").grid(row=2, column=0, padx=10, pady=5, sticky="e")
        self.tilt_speed_entry = ctk.CTkEntry(grid_frame, width=140)
        self.tilt_speed_entry.grid(row=2, column=1, padx=10, pady=5, sticky="w")

        ctk.CTkLabel(grid_frame, text="Rotation Speed (RPM):").grid(row=2, column=2, padx=10, pady=5, sticky="e")
        self.rot_speed_entry = ctk.CTkEntry(grid_frame, width=140)
        self.rot_speed_entry.grid(row=2, column=3, padx=10, pady=5, sticky="w")

        # Row 3: Sweep Extent & Pause (Swapped: Pause Duration is now first, Rotation per Pass second)
        ctk.CTkLabel(grid_frame, text="Pause Duration (s):").grid(row=3, column=0, padx=10, pady=5, sticky="e")
        self.pause_entry = ctk.CTkEntry(grid_frame, width=140)
        self.pause_entry.grid(row=3, column=1, padx=10, pady=5, sticky="w")

        ctk.CTkLabel(grid_frame, text="Rotation per Pass (Revs):").grid(row=3, column=2, padx=10, pady=5, sticky="e")
        self.rot_revs_entry = ctk.CTkEntry(grid_frame, width=140)
        self.rot_revs_entry.grid(row=3, column=3, padx=10, pady=5, sticky="w")

        # Row 4: Tilt Target Selector (Interactive Circular Protractor Dial + Secondary Raw Input)
        targets_container = ctk.CTkFrame(config_box, fg_color="transparent")
        targets_container.pack(fill="x", padx=20, pady=10)

        ctk.CTkLabel(targets_container, text="Tilt Pass Target Angles (Interactive Protractor Dial):", font=("Roboto", 14, "bold")).pack(anchor="w", padx=10, pady=(5, 5))

        self.tilt_protractor = TiltProtractorWidget(
            targets_container,
            on_change_callback=self._on_protractor_change
        )
        self.tilt_protractor.pack(fill="x", padx=10, pady=5)

        # Secondary Raw Encoder Target Field (Read-only / secondary reference for technicians)
        raw_frame = ctk.CTkFrame(targets_container, fg_color="transparent")
        raw_frame.pack(fill="x", padx=10, pady=(5, 10))

        ctk.CTkLabel(raw_frame, text="Secondary Raw Encoder Target Pulses (comma-separated):", font=("Roboto", 11), text_color="gray").pack(anchor="w", pady=(2, 2))
        self.tilt_targets_entry = ctk.CTkEntry(raw_frame, width=650, placeholder_text="-8000, -4000, 0, 2000, 5000")
        self.tilt_targets_entry.pack(anchor="w", fill="x")

        # Row 5: Reconstruction Cleanup Mode (rules-based vs model-based/learned)
        cleanup_container = ctk.CTkFrame(config_box, fg_color="transparent")
        cleanup_container.pack(fill="x", padx=20, pady=(5, 10))

        ctk.CTkLabel(cleanup_container, text="Reconstruction Cleanup Mode:", font=("Roboto", 14, "bold")).pack(anchor="w", padx=10, pady=(5, 2))

        cleanup_row = ctk.CTkFrame(cleanup_container, fg_color="transparent")
        cleanup_row.pack(anchor="w", padx=10, pady=(0, 2))

        self.cleanup_mode_var = tk.StringVar(value="rules")
        self.cleanup_mode_menu = ctk.CTkOptionMenu(
            cleanup_row,
            values=list(VALID_CLEANUP_MODES),
            variable=self.cleanup_mode_var,
            width=160,
            command=self._on_cleanup_mode_change,
        )
        self.cleanup_mode_menu.pack(side="left")

        self.cleanup_mode_note_lbl = ctk.CTkLabel(cleanup_container, text="", font=("Roboto", 11), text_color="gray", justify="left")
        self.cleanup_mode_note_lbl.pack(anchor="w", padx=10, pady=(2, 5))
        self._update_cleanup_mode_note()

        # Config Buttons & Status
        cfg_btn_row = ctk.CTkFrame(config_box, fg_color="transparent")
        cfg_btn_row.pack(pady=10)

        ctk.CTkButton(cfg_btn_row, text="💾 Save Configuration", fg_color="#1f538d", hover_color="#3a7ebf", command=self.save_gui_config).pack(side="left", padx=10)
        ctk.CTkButton(cfg_btn_row, text="↺ Reset Defaults", fg_color="gray", command=self.reset_gui_config).pack(side="left", padx=10)
        ctk.CTkButton(cfg_btn_row, text="🔌 Apply Comms Settings", fg_color="#2b5b84", command=self.apply_settings).pack(side="left", padx=10)

        self.cfg_status_lbl = ctk.CTkLabel(config_box, text="", font=("Roboto", 13, "bold"))
        self.cfg_status_lbl.pack(pady=(0, 10))

        # Populate current fields from self.scan_config
        self.populate_gui_config_fields()

        # -------------------------------------------------------------------
        # Section 2: Manual Axis Control (Jogging)
        # -------------------------------------------------------------------
        controls_frame = ctk.CTkFrame(scroll_container)
        controls_frame.pack(fill="x", padx=20, pady=10)

        ctk.CTkLabel(controls_frame, text="Manual Axis Control (Jogging)", font=("Roboto", 18, "bold")).pack(pady=10)

        jog_row = ctk.CTkFrame(controls_frame, fg_color="transparent")
        jog_row.pack(pady=10)

        ctk.CTkButton(jog_row, text="PAN +1 Rev", command=lambda: self.jog_axis(UNIT_ID_PAN, 1)).pack(side="left", padx=10)
        ctk.CTkButton(jog_row, text="PAN -1 Rev", command=lambda: self.jog_axis(UNIT_ID_PAN, -1)).pack(side="left", padx=10)
        ctk.CTkButton(jog_row, text="TILT +1 Rev", command=lambda: self.jog_axis(UNIT_ID_TILT, 1)).pack(side="left", padx=10)
        ctk.CTkButton(jog_row, text="TILT -1 Rev", command=lambda: self.jog_axis(UNIT_ID_TILT, -1)).pack(side="left", padx=10)

        # -------------------------------------------------------------------
        # Section 3: Manual Reconstruction
        # -------------------------------------------------------------------
        recon_frame = ctk.CTkFrame(scroll_container)
        recon_frame.pack(fill="x", padx=20, pady=10)

        ctk.CTkLabel(recon_frame, text="Manual File Reconstruction", font=("Roboto", 18, "bold")).pack(pady=10)

        recon_row = ctk.CTkFrame(recon_frame, fg_color="transparent")
        recon_row.pack(pady=10)

        self.file_path_entry = ctk.CTkEntry(recon_row, placeholder_text="Path to .obscan file...", width=400)
        self.file_path_entry.pack(side="left", padx=10)

        ctk.CTkButton(recon_row, text="Run Reconstruction", command=self.manual_reconstruct).pack(side="left", padx=10)

        return frame

    def _on_protractor_change(self, pulses: List[int]):
        """Sync protractor changes with the secondary raw encoder text entry."""
        if hasattr(self, "tilt_targets_entry"):
            self.tilt_targets_entry.delete(0, "end")
            self.tilt_targets_entry.insert(0, tilt_targets_to_str(pulses))

    def _on_cleanup_mode_change(self, _value: str = None):
        """Called when the technician changes the reconstruction cleanup mode dropdown."""
        self._update_cleanup_mode_note()

    def _update_cleanup_mode_note(self):
        """Refresh the advisory note under the cleanup mode dropdown based on current selection."""
        if not hasattr(self, "cleanup_mode_note_lbl"):
            return
        mode = self.cleanup_mode_var.get() if hasattr(self, "cleanup_mode_var") else "rules"
        if mode == "learned":
            self.cleanup_mode_note_lbl.configure(
                text=("⚠ Model-based (learned) cleanup is experimental. Validation on synthetic "
                      "barrels (notebooks/05_rules_vs_learned_volume_accuracy.ipynb) showed ~31% mean "
                      "volume error vs ~4% for Rules mode -- do not use for production volume reporting "
                      "until this is resolved."),
                text_color="#d9822b",
            )
        elif mode == "hybrid":
            self.cleanup_mode_note_lbl.configure(
                text=("Uses the learned model only deep in the stave wall and falls back to Rules "
                      "everywhere else (heads/poles/bevel) as a safety net. Gets close to Rules' "
                      "accuracy but is not shown to beat it -- see notebooks/05_rules_vs_learned_volume_accuracy.ipynb."),
                text_color="#2f6fed",
            )
        else:
            self.cleanup_mode_note_lbl.configure(
                text="Rules mode is the validated production default (see notebooks/05_rules_vs_learned_volume_accuracy.ipynb).",
                text_color="gray",
            )

    def populate_gui_config_fields(self):
        """Populate GUI entries from current self.scan_config."""
        m_cfg = self.scan_config.get("motor_settings", {})
        s_cfg = self.scan_config.get("sweep_settings", {})

        if hasattr(self, "port_entry"):
            self.port_entry.delete(0, "end")
            self.port_entry.insert(0, str(m_cfg.get("port", "COM3")))

        if hasattr(self, "baud_entry"):
            self.baud_entry.delete(0, "end")
            self.baud_entry.insert(0, str(m_cfg.get("baudrate", 115200)))

        if hasattr(self, "tilt_slave_entry"):
            self.tilt_slave_entry.delete(0, "end")
            self.tilt_slave_entry.insert(0, str(m_cfg.get("tilt_slave_id", 2)))

        if hasattr(self, "rot_slave_entry"):
            self.rot_slave_entry.delete(0, "end")
            self.rot_slave_entry.insert(0, str(m_cfg.get("rot_slave_id", 1)))

        if hasattr(self, "tilt_speed_entry"):
            self.tilt_speed_entry.delete(0, "end")
            self.tilt_speed_entry.insert(0, str(s_cfg.get("tilt_speed_rpm", 60)))

        if hasattr(self, "rot_speed_entry"):
            self.rot_speed_entry.delete(0, "end")
            self.rot_speed_entry.insert(0, str(s_cfg.get("rot_speed_rpm", 60)))

        if hasattr(self, "rot_revs_entry"):
            self.rot_revs_entry.delete(0, "end")
            self.rot_revs_entry.insert(0, str(s_cfg.get("rot_revs", 4.0)))

        if hasattr(self, "pause_entry"):
            self.pause_entry.delete(0, "end")
            self.pause_entry.insert(0, str(s_cfg.get("pause_seconds", 1.0)))

        targets = s_cfg.get("tilt_targets_pulses", [-8000, -4000, 0, 2000, 5000])

        if hasattr(self, "tilt_targets_entry"):
            self.tilt_targets_entry.delete(0, "end")
            self.tilt_targets_entry.insert(0, tilt_targets_to_str(targets))

        if hasattr(self, "tilt_protractor"):
            self.tilt_protractor.set_pulses(targets)

        r_cfg = self.scan_config.get("reconstruction_settings", {})
        cleanup_mode = r_cfg.get("cleanup_mode", "rules")
        if cleanup_mode not in VALID_CLEANUP_MODES:
            cleanup_mode = "rules"

        if hasattr(self, "cleanup_mode_var"):
            self.cleanup_mode_var.set(cleanup_mode)
            self._update_cleanup_mode_note()

    def save_gui_config(self):
        """Save input values from GUI into self.scan_config and write to scan_config.json."""
        try:
            m_cfg = self.scan_config.get("motor_settings", {})
            s_cfg = self.scan_config.get("sweep_settings", {})

            m_cfg["port"] = self.port_entry.get().strip()
            m_cfg["baudrate"] = int(self.baud_entry.get().strip())
            m_cfg["tilt_slave_id"] = int(self.tilt_slave_entry.get().strip())
            m_cfg["rot_slave_id"] = int(self.rot_slave_entry.get().strip())

            s_cfg["tilt_speed_rpm"] = int(self.tilt_speed_entry.get().strip())
            s_cfg["rot_speed_rpm"] = int(self.rot_speed_entry.get().strip())
            s_cfg["rot_revs"] = float(self.rot_revs_entry.get().strip())
            s_cfg["rot_deg"] = s_cfg["rot_revs"] * 360.0
            s_cfg["pause_seconds"] = float(self.pause_entry.get().strip())

            if hasattr(self, "tilt_protractor"):
                parsed_targets = self.tilt_protractor.get_pulses()
            else:
                parsed_targets = parse_tilt_targets_str(self.tilt_targets_entry.get())

            if parsed_targets:
                s_cfg["tilt_targets_pulses"] = parsed_targets

            r_cfg = self.scan_config.get("reconstruction_settings", {})
            if hasattr(self, "cleanup_mode_var"):
                selected_mode = self.cleanup_mode_var.get().strip()
                r_cfg["cleanup_mode"] = selected_mode if selected_mode in VALID_CLEANUP_MODES else "rules"

            self.scan_config["motor_settings"] = m_cfg
            self.scan_config["sweep_settings"] = s_cfg
            self.scan_config["reconstruction_settings"] = r_cfg

            if save_config(self.scan_config):
                self.cfg_status_lbl.configure(text="✔ Configuration saved to scan_config.json", text_color="green")
                self.after(3000, lambda: self.cfg_status_lbl.configure(text=""))
            else:
                self.cfg_status_lbl.configure(text="✖ Failed to save scan_config.json", text_color="red")
        except Exception as e:
            log.error(f"Error saving config from GUI: {e}")
            self.cfg_status_lbl.configure(text=f"✖ Error: {e}", text_color="red")

    def reset_gui_config(self):
        """Reset config to factory defaults."""
        self.scan_config = reset_to_defaults()
        self.populate_gui_config_fields()
        self.cfg_status_lbl.configure(text="✔ Reset to default configuration", text_color="yellow")
        self.after(3000, lambda: self.cfg_status_lbl.configure(text=""))

if __name__ == "__main__":
    app = AppGUI()
    app.mainloop()

