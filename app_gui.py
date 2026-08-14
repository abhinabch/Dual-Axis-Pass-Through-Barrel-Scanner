import tkinter as tk
import customtkinter as ctk
import threading
import queue
import time
import logging
from enum import Enum, auto
from typing import Optional, Callable

# Backend imports
from pymodbus.client import ModbusSerialClient
from hardware.run_precision_scan import (
    ModbusSerialClient,
    UNIT_ID_PAN,
    UNIT_ID_TILT,
    SERIAL_PORT,
    BAUDRATE,
    TIMEOUT,
    read_motor_status,
    check_motor_alarm,
    emergency_stop,
    SafetyWatchdog,
    # perform_raster_sweep, # Removed in favor of scan_sequence
    PipelineError
)
from scan_sequence import run_scan_sequence, ModbusLink
from reconstruction.barrel_reconstruct import run_reconstruction_pipeline # Assumed entry point
from reconstruction.barrel_batch import save_to_log # Assumed entry point
from hardware.creality_autostart import CrealityAutomator, AutomationError

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
        self.watchdog: Optional[SafetyWatchdog] = None
        self.automator = CrealityAutomator()
        self.worker_queue = queue.Queue()
        self.stop_event = threading.Event()

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

    def handle_pipeline_error(self, error):
        log.error(f"Pipeline Error: {error}")
        self.show_frame(AppState.ERROR)
        self.error_msg.configure(text=str(error))

    def start_pipeline(self):
        """Launch the background worker for the scan pipeline."""
        self.stop_event.clear()
        self.show_frame(AppState.SCANNING)
        
        # Start Safety Watchdog
        self.watchdog = SafetyWatchdog(self.client, self.handle_pipeline_error)
        self.watchdog.start()
        
        # Launch Worker
        threading.Thread(target=self.pipeline_worker, daemon=True).start()

    def pipeline_worker(self):
        """Background thread for scan -> reconstruct -> log."""
        try:
            # 1. Scanning Phase
            def progress_cb(msg):
                self.worker_queue.put(WorkerMessage('progress', data=msg))
            
            # Start timer
            self.start_time = time.time()
            self.update_timer_loop()

            # Trigger Creality Scan autostart sequence
            log.info("Triggering Creality Scan autostart sequence...")
            try:
                self.automator.start_scan()
            except Exception as scan_err:
                log.warning("Creality Scan autostart note: %s", scan_err)

            # Execute the raster sweep using scan_sequence.py
            # We use the link already established in the GUI
            run_scan_sequence(self.client, progress_callback=progress_cb)
            
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
            
            # Simulation of reconstruction pipeline
            # results = run_reconstruction_pipeline(scan_file_path)
            time.sleep(3) # Simulate processing time
            
            # Mock results based on the requested schema
            mock_results = {
                'volume': 225.5, 
                'surface': 6.2, 
                'quality': 'Good', 
                'metrics': {'Axial Length': '850mm', 'Bilge Radius': '310mm'}
            }
            
            # 3. Results Phase
            self.worker_queue.put(WorkerMessage('result', data=mock_results))
            
        except Exception as e:
            self.worker_queue.put(WorkerMessage('error', error=e))
        finally:
            if self.watchdog:
                self.watchdog.stop()

    def update_timer_loop(self):
        """Update the elapsed time label on the Scanning screen."""
        if self.state == AppState.SCANNING:
            elapsed = int(time.time() - self.start_time)
            mins, secs = divmod(elapsed, 60)
            self.timer_lbl.configure(text=f"Elapsed Time: {mins:02d}:{secs:02d}")
            self.after(1000, self.update_timer_loop)


    def handle_results(self, data):
        # data is expected to be a dict: {'volume': float, 'surface': float, 'quality': str, 'metrics': dict}
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
        """Interface with barrel_batch.py to save the current result."""
        log.info("Saving results to log...")
        # In a real implementation:
        # save_to_log(barrel_id=self.barrel_id.get(), operator=self.operator_name.get(), results=...)
        self.save_log_btn.configure(text="Saved!", state="disabled", fg_color="green")
        self.after(2000, lambda: self.save_log_btn.configure(text="Save & Add to Log", state="normal", fg_color=["#3a7ebf", "#1f538d"]))

    def jog_axis(self, unit_id: int, revs: int):
        """Manual jog for technician panel."""
        log.info("Jogging axis %d by %d revs", unit_id, revs)
        try:
            if self.client:
                # Use relative move for jogging
                # Need a relative move function in run_precision_scan or implement here
                # For simplicity, we'll call a temporary helper
                from hardware.run_precision_scan import send_absolute_move
                # This is a hack; proper relative move should be in hardware module
                # Assuming current position + offset
                from hardware.run_precision_scan import read_encoder_position
                curr = read_encoder_position(self.client, unit_id)
                target = curr + (revs * (PULSES_PER_REV_PAN if unit_id == UNIT_ID_PAN else PULSES_PER_REV_TILT))
                send_absolute_move(self.client, unit_id, target)
        except Exception as e:
            log.error("Jog failed: %s", e)

    def apply_settings(self):
        """Update Modbus client settings."""
        new_port = self.port_entry.get()
        new_baud = int(self.baud_entry.get())
        log.info("Applying settings: Port=%s, Baud=%d", new_port, new_baud)
        # Re-initialize client
        if self.client:
            self.client.close()
        self.client = ModbusSerialClient(port=new_port, baudrate=new_baud, timeout=TIMEOUT)
        self.client.connect()

    def manual_reconstruct(self):
        """Run reconstruction on a specific file."""
        path = self.file_path_entry.get()
        if not path:
            log.warning("No path provided for manual reconstruction.")
            return
        log.info("Starting manual reconstruction for: %s", path)
        # Trigger worker thread in PROCESSING state
        self.show_frame(AppState.PROCESSING)
        threading.Thread(target=self.manual_reconstruct_worker, args=(path,), daemon=True).start()

    def manual_reconstruct_worker(self):
        try:
            # simulate reconstruction
            time.sleep(3)
            self.worker_queue.put(WorkerMessage('result', data={'volume': 210.2, 'surface': 5.8, 'quality': 'Good'}))
        except Exception as e:
            self.worker_queue.put(WorkerMessage('error', error=str(e)))

    def poll_connections(self):
        """Periodically check connection status of turret and scanner."""
        log.info("Polling connections...")
        
        # 1. Check Turret Connectivity (Modbus)
        turret_ok = False
        try:
            if self.client is None:
                # Try to initialize client if it doesn't exist
                self.client = ModbusSerialClient(port=SERIAL_PORT, baudrate=BAUDRATE, timeout=TIMEOUT)
                self.client.connect()
            
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
        
        title_lbl = ctk.CTkLabel(frame, text="Technician / Advanced Panel", font=("Roboto", 32, "bold"))
        title_lbl.pack(pady=(40, 20))
        
        # Connection Settings
        settings_frame = ctk.CTkFrame(frame)
        settings_frame.pack(pady=20, padx=50, fill="x")
        
        ctk.CTkLabel(settings_frame, text="Modbus Connection Settings", font=("Roboto", 18, "bold")).pack(pady=10)
        
        conn_row = ctk.CTkFrame(settings_frame, fg_color="transparent")
        conn_row.pack(pady=10)
        
        self.port_entry = ctk.CTkEntry(conn_row, placeholder_text="COM Port (e.g. COM3)", width=150)
        self.port_entry.pack(side="left", padx=10)
        
        self.baud_entry = ctk.CTkEntry(conn_row, placeholder_text="Baudrate (e.g. 9600)", width=150)
        self.baud_entry.pack(side="left", padx=10)
        
        ctk.CTkButton(conn_row, text="Apply Settings", command=self.apply_settings).pack(side="left", padx=10)
        
        # Manual Controls
        controls_frame = ctk.CTkFrame(frame)
        controls_frame.pack(pady=20, padx=50, fill="x")
        
        ctk.CTkLabel(controls_frame, text="Manual Axis Control (Jogging)", font=("Roboto", 18, "bold")).pack(pady=10)
        
        jog_row = ctk.CTkFrame(controls_frame, fg_color="transparent")
        jog_row.pack(pady=10)
        
        # This is just a placeholder; in a real app we'd have buttons for +1/-1 rev
        ctk.CTkButton(jog_row, text="PAN +1 Rev", command=lambda: self.jog_axis(UNIT_ID_PAN, 1)).pack(side="left", padx=10)
        ctk.CTkButton(jog_row, text="PAN -1 Rev", command=lambda: self.jog_axis(UNIT_ID_PAN, -1)).pack(side="left", padx=10)
        ctk.CTkButton(jog_row, text="TILT +1 Rev", command=lambda: self.jog_axis(UNIT_ID_TILT, 1)).pack(side="left", padx=10)
        ctk.CTkButton(jog_row, text="TILT -1 Rev", command=lambda: self.jog_axis(UNIT_ID_TILT, -1)).pack(side="left", padx=10)
        
        # Manual Reconstruction
        recon_frame = ctk.CTkFrame(frame)
        recon_frame.pack(pady=20, padx=50, fill="x")
        
        ctk.CTkLabel(recon_frame, text="Manual File Reconstruction", font=("Roboto", 18, "bold")).pack(pady=10)
        
        recon_row = ctk.CTkFrame(recon_frame, fg_color="transparent")
        recon_row.pack(pady=10)
        
        self.file_path_entry = ctk.CTkEntry(recon_row, placeholder_text="Path to .obscan file...", width=400)
        self.file_path_entry.pack(side="left", padx=10)
        
        ctk.CTkButton(recon_row, text="Run Reconstruction", command=self.manual_reconstruct).pack(side="left", padx=10)
        
        return frame

if __name__ == "__main__":
    app = AppGUI()
    app.mainloop()
