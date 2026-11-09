import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import numpy as np
import soundcard as sc
import soundfile as sf
from mss import MSS, ScreenShotError
from PIL import Image, ImageTk
from screeninfo import get_monitors
from screeninfo.common import ScreenInfoError

SAMPLERATE = 48000
BLOCKSIZE = 1024


def settings_file_path(app_data: str | None = None) -> Path:
    app_data = app_data or os.environ.get("APPDATA")
    if app_data:
        return Path(app_data) / "recordSnd" / "settings.json"
    return Path.home() / "AppData" / "Roaming" / "recordSnd" / "settings.json"


def default_recorder_settings(working_directory: Path | None = None) -> dict[str, str]:
    working_directory = working_directory or Path.cwd()
    return {
        "output_directory": str(working_directory / "recordings"),
        "ffmpeg_path": "",
    }


def validate_output_directory(path: Path) -> Path:
    path = Path(path).expanduser()
    if path.exists() and not path.is_dir():
        raise NotADirectoryError(f"Selected output folder is not a directory: {path}")
    return path.resolve()


def load_recorder_settings(
    settings_path: Path | None = None,
) -> tuple[dict[str, str], str | None]:
    settings_path = settings_path or settings_file_path()
    defaults = default_recorder_settings()
    try:
        loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise TypeError("settings must be a JSON object")
        output_directory = loaded.get("output_directory", defaults["output_directory"])
        ffmpeg_path = loaded.get("ffmpeg_path", "")
        if not isinstance(output_directory, str) or not output_directory.strip():
            raise ValueError("output_directory must be a non-empty string")
        if not isinstance(ffmpeg_path, str):
            raise TypeError("ffmpeg_path must be a string")
    except FileNotFoundError:
        return defaults, None
    except (OSError, ValueError, TypeError) as error:
        return defaults, f"Saved settings could not be loaded ({error}); defaults are in use."

    return {
        "output_directory": output_directory,
        "ffmpeg_path": ffmpeg_path.strip(),
    }, None


def save_recorder_settings(
    settings: dict[str, str],
    settings_path: Path | None = None,
) -> None:
    settings_path = settings_path or settings_file_path()
    output_directory = settings.get("output_directory", "").strip()
    ffmpeg_path = settings.get("ffmpeg_path", "").strip()
    if not output_directory:
        raise ValueError("Output folder cannot be empty.")

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = settings_path.with_name(settings_path.name + ".tmp")
    try:
        temporary_path.write_text(
            json.dumps(
                {"output_directory": output_directory, "ffmpeg_path": ffmpeg_path},
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary_path.replace(settings_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def bundled_ffmpeg_path(runtime_directory: Path | None = None) -> Path | None:
    if runtime_directory is None:
        if not getattr(sys, "frozen", False):
            return None
        runtime_directory = getattr(sys, "_MEIPASS", None)
    if runtime_directory is None:
        return None

    executable = Path(runtime_directory) / "ffmpeg" / "ffmpeg.exe"
    return executable if executable.is_file() else None


def _conda_ffmpeg_executable() -> Path | None:
    conda_prefix = os.environ.get("CONDA_PREFIX")
    if conda_prefix:
        prefix_executable = Path(conda_prefix) / "Library" / "bin" / "ffmpeg.exe"
        if prefix_executable.is_file():
            return prefix_executable

    conda_executable = os.environ.get("CONDA_EXE") or shutil.which("conda")
    if not conda_executable:
        return None

    conda_path = Path(conda_executable).resolve()
    base_executable = conda_path.parent.parent / "Library" / "bin" / "ffmpeg.exe"
    return base_executable if base_executable.is_file() else None


def resolve_ffmpeg_executable(configured_path: str | None = None) -> str:
    configured_path = (configured_path or "").strip()
    if configured_path:
        executable = Path(configured_path).expanduser()
        if not executable.is_file():
            raise FileNotFoundError(
                f"Configured FFmpeg executable was not found: {executable}"
            )
        return str(executable.resolve())

    bundled_executable = bundled_ffmpeg_path()
    if bundled_executable is not None:
        return str(bundled_executable.resolve())

    executable = shutil.which("ffmpeg")
    if executable:
        return executable

    conda_executable = _conda_ffmpeg_executable()
    if conda_executable is not None:
        return str(conda_executable)

    raise FileNotFoundError(
        "FFmpeg was not found. Create and activate the project Conda environment "
        "with `conda env create -f environment.yml` and `conda activate recordsnd`, "
        "or select ffmpeg.exe in Settings."
    )


def get_speaker_loopback(speaker):
    return sc.get_microphone(speaker.id, include_loopback=True)


def get_monitors_by_number(monitors):
    monitors_by_number = {}
    for monitor in monitors:
        match = re.fullmatch(r"\\\\\.\\DISPLAY([1-9]\d*)", monitor.name or "", re.IGNORECASE)
        if match is None:
            raise RuntimeError(
                f"Cannot determine Windows display number for monitor {monitor.name!r}."
            )
        number = int(match.group(1))
        if number in monitors_by_number:
            raise RuntimeError(f"Multiple monitors have Windows display number {number}.")
        monitors_by_number[number] = monitor
    return monitors_by_number


def choose_monitor():
    monitors = get_monitors()

    if not monitors:
        raise RuntimeError("No monitors detected.")

    monitors_by_number = get_monitors_by_number(monitors)

    print("\nAvailable monitors (Windows display numbers):")
    for number, m in sorted(monitors_by_number.items()):
        primary = " | Primary" if m.is_primary else ""
        print(f"  [{number}] {m.name} | {m.width}x{m.height} | x={m.x}, y={m.y}{primary}")

    while True:
        choice = input("\nEnter Windows display number to record: ").strip()
        try:
            number = int(choice)
            if number in monitors_by_number:
                return monitors_by_number[number], number
            available = ", ".join(str(n) for n in sorted(monitors_by_number))
            print(f"Invalid display number. Choose one of: {available}.")
        except ValueError:
            print("Please enter a valid integer.")


def monitor_capture_region(monitor):
    return {
        "left": int(monitor.x),
        "top": int(monitor.y),
        "width": int(monitor.width),
        "height": int(monitor.height),
    }


def build_device_choices(devices):
    name_counts = {}
    for device in devices:
        name_counts[device.name] = name_counts.get(device.name, 0) + 1

    name_occurrences = {}
    used_labels = set()
    choices = []
    for index, device in enumerate(devices, start=1):
        name_occurrences[device.name] = name_occurrences.get(device.name, 0) + 1
        if name_counts[device.name] == 1:
            label = device.name
        else:
            label = f"{device.name} ({name_occurrences[device.name]})"
        if label in used_labels:
            label = f"{device.name} [{index}]"
        used_labels.add(label)
        choices.append((label, device))
    return choices


def recording_control_states(recording_state, has_valid_selection, selectors_available):
    selector_state = "readonly" if selectors_available else "disabled"
    if recording_state == "idle":
        start_state = "normal" if has_valid_selection else "disabled"
        return start_state, "disabled", selector_state
    if recording_state == "recording":
        return "disabled", "normal", "disabled"
    return "disabled", "disabled", "disabled"


def settings_button_state(recording_state: str) -> str:
    return "normal" if recording_state == "idle" else "disabled"


def audio_level_percent(samples: np.ndarray) -> int:
    samples = np.asarray(samples, dtype=np.float64)
    if samples.size == 0:
        return 0

    samples = np.nan_to_num(samples, nan=0.0, posinf=1.0, neginf=-1.0)
    rms = float(np.sqrt(np.mean(np.square(samples))))
    decibels = 20.0 * np.log10(max(rms, 1e-3))
    level = ((decibels + 60.0) / 60.0) * 100.0
    return int(np.clip(round(level), 0, 100))


def record_audio(
    device,
    output_path: Path,
    channels: int,
    stop_event: threading.Event,
    on_level: Callable[[int], None],
) -> None:
    with device.recorder(samplerate=SAMPLERATE, channels=channels) as recorder, sf.SoundFile(
        output_path,
        mode="w",
        samplerate=SAMPLERATE,
        channels=channels,
    ) as output:
        while not stop_event.is_set():
            data = recorder.record(numframes=BLOCKSIZE)
            output.write(data)
            on_level(audio_level_percent(data))


def start_screen_recording(output_file, monitor, fps=30, ffmpeg_executable=None):
    executable = ffmpeg_executable or resolve_ffmpeg_executable()
    cmd = [
        executable,
        "-y",
        "-loglevel", "error",
        "-f", "gdigrab",
        "-framerate", str(fps),
        "-offset_x", str(monitor.x),
        "-offset_y", str(monitor.y),
        "-video_size", f"{monitor.width}x{monitor.height}",
        "-i", "desktop",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        str(output_file)
    ]

    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True
    )


def build_recording_paths(output_directory: Path, monitor_number: int, timestamp: str):
    output_directory = Path(output_directory)
    return {
        "video": output_directory / f"screen_monitor{monitor_number}_{timestamp}.mp4",
        "microphone": output_directory / f"microphone_{timestamp}.wav",
        "system": output_directory / f"system_audio_{timestamp}.wav",
        "mixed": output_directory / f"mixed_audio_{timestamp}.wav",
        "final": output_directory / f"desktop_monitor{monitor_number}_{timestamp}.mp4",
    }


def screen_capture_exit_message(screen_process) -> str | None:
    if screen_process.poll() is None:
        return None
    return f"FFmpeg exited unexpectedly with status {screen_process.returncode}."


def audio_capture_worker(
    source_name,
    device,
    output_path,
    channels,
    stop_event,
    worker_errors,
    ui_events,
):
    try:
        record_audio(
            device,
            output_path,
            channels,
            stop_event,
            lambda level: ui_events.put(("level", source_name, level)),
        )
    except (OSError, RuntimeError, ValueError, TypeError, IndexError) as error:
        message = str(error)
        worker_errors.put((source_name, message))
        ui_events.put(("capture_error", source_name, message))
        stop_event.set()


def finalize_recording(
    video_file,
    mic_file,
    sys_file,
    mixed_audio_file,
    final_file,
    ffmpeg_executable=None,
):
    executable = ffmpeg_executable or resolve_ffmpeg_executable()
    subprocess.run(
        [
            executable,
            "-y",
            "-loglevel", "error",
            "-i", str(mic_file),
            "-i", str(sys_file),
            "-filter_complex",
            "[0:a]volume=1.5[m];[1:a]volume=1.0[s];[m][s]amix=inputs=2:duration=longest:dropout_transition=2[aout]",
            "-map", "[aout]",
            str(mixed_audio_file),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            executable,
            "-y",
            "-loglevel", "error",
            "-i", str(video_file),
            "-i", str(mixed_audio_file),
            "-c:v", "copy",
            "-c:a", "aac",
            str(final_file),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    cleanup_warnings = []
    for path in (video_file, mic_file, sys_file, mixed_audio_file):
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as error:
            cleanup_warnings.append(f"Could not remove temporary file {path}: {error}")
    return cleanup_warnings


class SettingsDialog:
    def __init__(self, parent, settings, on_save):
        self.window = tk.Toplevel(parent)
        self.window.title("Recorder Settings")
        self.window.transient(parent)
        self.window.resizable(False, False)
        self.window.columnconfigure(0, weight=1)
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)
        self.on_save = on_save
        self.output_directory_var = tk.StringVar(
            value=settings["output_directory"]
        )
        self.ffmpeg_path_var = tk.StringVar(value=settings.get("ffmpeg_path", ""))

        content = ttk.Frame(self.window, padding=16)
        content.grid(row=0, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        ttk.Label(content, text="Recordings folder").grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        ttk.Entry(content, textvariable=self.output_directory_var).grid(
            row=1, column=0, sticky="ew", pady=(4, 12)
        )
        ttk.Button(content, text="Browse...", command=self._browse_output_directory).grid(
            row=1, column=1, sticky="ew", padx=(8, 0), pady=(4, 12)
        )
        ttk.Label(content, text="FFmpeg executable (leave blank for automatic detection)").grid(
            row=2, column=0, columnspan=2, sticky="w"
        )
        ttk.Entry(content, textvariable=self.ffmpeg_path_var).grid(
            row=3, column=0, sticky="ew", pady=(4, 12)
        )
        ttk.Button(content, text="Browse...", command=self._browse_ffmpeg).grid(
            row=3, column=1, sticky="ew", padx=(8, 0), pady=(4, 12)
        )

        buttons = ttk.Frame(content)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="Cancel", command=self.cancel).grid(row=0, column=0)
        ttk.Button(buttons, text="Save", command=self.save).grid(
            row=0, column=1, padx=(8, 0)
        )
        self.window.grab_set()

    def _browse_output_directory(self):
        current_path = Path(self.output_directory_var.get()).expanduser()
        initial_directory = current_path if current_path.is_dir() else Path.home()
        selected_directory = filedialog.askdirectory(
            parent=self.window,
            initialdir=str(initial_directory),
            title="Choose recordings folder",
        )
        if selected_directory:
            self.output_directory_var.set(selected_directory)

    def _browse_ffmpeg(self):
        current_path = Path(self.ffmpeg_path_var.get()).expanduser()
        initial_directory = current_path.parent if current_path.is_file() else Path.home()
        selected_file = filedialog.askopenfilename(
            parent=self.window,
            initialdir=str(initial_directory),
            title="Choose FFmpeg executable",
            filetypes=(
                ("FFmpeg executable", "ffmpeg.exe"),
                ("Executable files", "*.exe"),
                ("All files", "*.*"),
            ),
        )
        if selected_file:
            self.ffmpeg_path_var.set(selected_file)

    def save(self):
        output_text = self.output_directory_var.get().strip()
        ffmpeg_path = self.ffmpeg_path_var.get().strip()
        try:
            if not output_text:
                raise ValueError("Choose a recordings folder.")
            output_directory = validate_output_directory(Path(output_text))
            if ffmpeg_path:
                resolve_ffmpeg_executable(ffmpeg_path)
            settings = {
                "output_directory": str(output_directory),
                "ffmpeg_path": ffmpeg_path,
            }
            save_recorder_settings(settings)
        except (OSError, ValueError) as error:
            messagebox.showerror("Invalid recorder settings", str(error), parent=self.window)
            return

        self.on_save(settings)
        self.window.destroy()

    def cancel(self):
        self.window.destroy()


class RecorderApp:
    PREVIEW_WIDTH = 640
    PREVIEW_HEIGHT = 360

    def __init__(self, root):
        self.root = root
        self.settings, self.settings_warning = load_recorder_settings()
        self.output_directory = Path(self.settings["output_directory"]).expanduser()
        self.output_directory_error = None
        self.ffmpeg_executable = None
        self.ffmpeg_error = None
        self._refresh_recording_configuration()
        self.root.title("Screen and Audio Recorder")
        self.root.geometry("900x700")
        self.root.minsize(760, 620)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        self.monitor_choices = {}
        self.microphone_choices = {}
        self.speaker_choices = {}
        self.monitor_var = tk.StringVar()
        self.microphone_var = tk.StringVar()
        self.speaker_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Choose a monitor and audio devices.")
        self.preview_after_id = None
        self.preview_photo = None
        self.preview_capture: MSS = MSS()
        self.recording_state = "idle"
        self.stop_event: threading.Event | None = None
        self.screen_process: subprocess.Popen[str] | None = None
        self.recording_ffmpeg_executable: str | None = None
        self.audio_threads: list[threading.Thread] = []
        self.recording_files: dict[str, Path] | None = None
        self.ui_events = queue.Queue()
        self.worker_errors = queue.Queue()
        self.event_poll_after_id = None
        self.close_after_recording = False
        self.closed = False
        self.device_errors = []

        self._build_ui()
        self._load_choices()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.event_poll_after_id = self.root.after(100, self._poll_ui_events)

    def _refresh_recording_configuration(self):
        self.output_directory_error = None
        try:
            self.output_directory = validate_output_directory(self.output_directory)
        except (OSError, ValueError) as error:
            self.output_directory_error = f"Output folder is unavailable: {error}"

        self.ffmpeg_executable = None
        self.ffmpeg_error = None
        try:
            self.ffmpeg_executable = resolve_ffmpeg_executable(
                self.settings.get("ffmpeg_path", "")
            )
        except FileNotFoundError as error:
            self.ffmpeg_error = str(error)

    def _refresh_status(self):
        messages = list(self.device_errors)
        if self.settings_warning:
            messages.append(self.settings_warning)
        if self.output_directory_error:
            messages.append(self.output_directory_error)
        if self.ffmpeg_error:
            messages.append(self.ffmpeg_error)
        if messages:
            self.status_var.set(" ".join(messages))
        elif self.has_valid_selection:
            self.status_var.set("Ready. Confirm the preview and selected audio devices.")
        else:
            self.status_var.set("Choose a monitor and audio devices.")

    @property
    def can_start_recording(self):
        return (
            self.has_valid_selection
            and self.output_directory_error is None
            and self.ffmpeg_executable is not None
        )

    def open_settings(self):
        if self.recording_state != "idle":
            return
        SettingsDialog(self.root, self.settings, self._apply_settings)

    def _apply_settings(self, settings):
        self.settings = settings.copy()
        self.settings_warning = None
        self.output_directory = Path(settings["output_directory"]).expanduser()
        self._refresh_recording_configuration()
        self._update_control_states()
        self._refresh_status()

    def _build_ui(self):
        content = ttk.Frame(self.root, padding=16)
        content.grid(row=0, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)

        ttk.Label(
            content,
            text="Screen and Audio Recorder",
            font=("Segoe UI", 17, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 12))

        monitor_frame = ttk.LabelFrame(content, text="Monitor", padding=12)
        monitor_frame.grid(row=1, column=0, sticky="ew")
        monitor_frame.columnconfigure(0, weight=1)
        self.monitor_selector = ttk.Combobox(
            monitor_frame,
            textvariable=self.monitor_var,
            state="disabled",
        )
        self.monitor_selector.grid(row=0, column=0, sticky="ew")
        self.monitor_selector.bind("<<ComboboxSelected>>", self._on_monitor_selected)

        audio_frame = ttk.LabelFrame(content, text="Audio devices", padding=12)
        audio_frame.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        audio_frame.columnconfigure(0, weight=1)
        audio_frame.columnconfigure(1, weight=1)
        ttk.Label(audio_frame, text="Microphone input").grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        ttk.Label(audio_frame, text="System audio output").grid(
            row=0, column=1, sticky="w", padx=(8, 0)
        )
        self.microphone_selector = ttk.Combobox(
            audio_frame,
            textvariable=self.microphone_var,
            state="disabled",
        )
        self.microphone_selector.grid(row=1, column=0, sticky="ew", padx=(0, 8))
        self.microphone_selector.bind(
            "<<ComboboxSelected>>", self._on_audio_selected
        )
        self.speaker_selector = ttk.Combobox(
            audio_frame,
            textvariable=self.speaker_var,
            state="disabled",
        )
        self.speaker_selector.grid(row=1, column=1, sticky="ew", padx=(8, 0))
        self.speaker_selector.bind("<<ComboboxSelected>>", self._on_audio_selected)
        ttk.Label(audio_frame, text="Microphone level").grid(
            row=2, column=0, sticky="w", padx=(0, 8), pady=(8, 0)
        )
        ttk.Label(audio_frame, text="System audio level").grid(
            row=2, column=1, sticky="w", padx=(8, 0), pady=(8, 0)
        )
        self.microphone_level_var = tk.DoubleVar(value=0)
        self.system_level_var = tk.DoubleVar(value=0)
        ttk.Progressbar(
            audio_frame,
            maximum=100,
            variable=self.microphone_level_var,
        ).grid(row=3, column=0, sticky="ew", padx=(0, 8))
        ttk.Progressbar(
            audio_frame,
            maximum=100,
            variable=self.system_level_var,
        ).grid(row=3, column=1, sticky="ew", padx=(8, 0))

        preview_frame = ttk.LabelFrame(content, text="Selected monitor preview", padding=12)
        preview_frame.grid(row=3, column=0, sticky="nsew", pady=(12, 0))
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)
        content.rowconfigure(3, weight=1)
        self.preview_canvas = tk.Canvas(
            preview_frame,
            width=self.PREVIEW_WIDTH,
            height=self.PREVIEW_HEIGHT,
            background="#171717",
            highlightthickness=0,
        )
        self.preview_canvas.grid(row=0, column=0, sticky="")
        self._show_preview_message("Select a monitor to preview it")

        controls = ttk.Frame(content)
        controls.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        self.start_button = ttk.Button(
            controls,
            text="Start recording",
            command=self.start_recording,
        )
        self.start_button.grid(row=0, column=0, sticky="w")
        self.stop_button = ttk.Button(
            controls,
            text="Stop recording",
            command=self.stop_recording,
            state="disabled",
        )
        self.stop_button.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.settings_button = ttk.Button(
            controls,
            text="Settings",
            command=self.open_settings,
        )
        self.settings_button.grid(row=0, column=2, sticky="w", padx=(8, 0))

        ttk.Label(
            content,
            textvariable=self.status_var,
            wraplength=850,
        ).grid(row=5, column=0, sticky="w", pady=(12, 0))

    def _load_choices(self):
        errors = self._load_monitors()
        errors.extend(self._load_microphones())
        errors.extend(self._load_speakers())

        self._set_selector(self.monitor_selector, self.monitor_var, self.monitor_choices)
        self._set_selector(
            self.microphone_selector, self.microphone_var, self.microphone_choices
        )
        self._set_selector(self.speaker_selector, self.speaker_var, self.speaker_choices)
        primary_label = next(
            (
                label
                for label, monitor in self.monitor_choices.items()
                if monitor.is_primary
            ),
            None,
        )
        if primary_label is not None:
            self.monitor_var.set(primary_label)

        self.device_errors = errors
        self._refresh_preview()
        self._refresh_status()
        self._update_control_states()

    def _load_monitors(self):
        try:
            monitors = get_monitors()
            if not monitors:
                raise RuntimeError("No monitors detected.")
            self.monitor_choices = {
                f"[{number}] {monitor.name} | {monitor.width}x{monitor.height} | "
                f"x={monitor.x}, y={monitor.y}"
                f"{' | Primary' if monitor.is_primary else ''}": monitor
                for number, monitor in sorted(get_monitors_by_number(monitors).items())
            }
            return []
        except (ScreenInfoError, RuntimeError, OSError) as error:
            self.monitor_choices = {}
            return [f"Monitor selection unavailable: {error}"]

    def _load_microphones(self):
        try:
            self.microphone_choices = dict(build_device_choices(sc.all_microphones()))
            if not self.microphone_choices:
                return ["No microphones found."]
            return []
        except (RuntimeError, OSError) as error:
            self.microphone_choices = {}
            return [f"Microphone enumeration failed: {error}"]

    def _load_speakers(self):
        try:
            self.speaker_choices = dict(build_device_choices(sc.all_speakers()))
            if not self.speaker_choices:
                return ["No speakers found."]
            return []
        except (RuntimeError, OSError) as error:
            self.speaker_choices = {}
            return [f"Speaker enumeration failed: {error}"]

    @staticmethod
    def _set_selector(selector, variable, choices):
        labels = list(choices)
        selector.configure(values=labels, state="readonly" if labels else "disabled")
        variable.set(labels[0] if labels else "")

    @property
    def has_valid_selection(self):
        return (
            self.monitor_var.get() in self.monitor_choices
            and self.microphone_var.get() in self.microphone_choices
            and self.speaker_var.get() in self.speaker_choices
        )

    def _on_monitor_selected(self, event=None):
        self._update_control_states()
        self._refresh_preview()

    def _on_audio_selected(self, event=None):
        self._update_control_states()

    def _update_control_states(self):
        start_state, stop_state, selector_state = recording_control_states(
            self.recording_state,
            self.can_start_recording,
            self.recording_state == "idle",
        )
        self.start_button.configure(state=start_state)
        self.stop_button.configure(state=stop_state)
        self.settings_button.configure(state=settings_button_state(self.recording_state))
        for selector, choices in (
            (self.monitor_selector, self.monitor_choices),
            (self.microphone_selector, self.microphone_choices),
            (self.speaker_selector, self.speaker_choices),
        ):
            selector.configure(state=selector_state if choices else "disabled")

    def _set_recording_state(self, state):
        self.recording_state = state
        self._update_control_states()
        if state == "idle":
            self._refresh_preview()
            return
        if self.preview_after_id is not None:
            try:
                self.root.after_cancel(self.preview_after_id)
            except tk.TclError:
                pass
            self.preview_after_id = None

    def start_recording(self):
        if self.recording_state != "idle" or not self.can_start_recording:
            return

        monitor = self.monitor_choices[self.monitor_var.get()]
        match = re.fullmatch(r"\\\\\.\\DISPLAY([1-9]\d*)", monitor.name or "", re.IGNORECASE)
        if match is None:
            self.status_var.set(f"Cannot determine Windows display number for {monitor.name!r}.")
            return

        monitor_number = int(match.group(1))
        try:
            speaker = self.speaker_choices[self.speaker_var.get()]
            loopback = get_speaker_loopback(speaker)
            self._prepare_screen_capture(monitor, monitor_number)
        except FileNotFoundError as error:
            self.ffmpeg_error = str(error)
            self.ffmpeg_executable = None
            self._update_control_states()
            self.status_var.set(f"FFmpeg is unavailable: {error}")
            self.screen_process = None
            self.recording_files = None
            return
        except (OSError, RuntimeError, ValueError) as error:
            if isinstance(error, NotADirectoryError):
                self.output_directory_error = str(error)
            self.screen_process = None
            self.recording_files = None
            self._update_control_states()
            self.status_var.set(f"Could not start recording: {error}")
            return

        self._start_audio_workers(monitor_number, loopback)

    def _prepare_screen_capture(self, monitor, monitor_number):
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")
        output_directory = validate_output_directory(self.output_directory)
        output_directory.mkdir(parents=True, exist_ok=True)
        output_directory = validate_output_directory(output_directory)
        self.output_directory = output_directory
        self.recording_files = build_recording_paths(
            output_directory, monitor_number, timestamp
        )
        executable = self.ffmpeg_executable
        if executable is None:
            raise FileNotFoundError(
                self.ffmpeg_error
                or "FFmpeg was not found. Configure its path in Settings."
            )
        self.recording_ffmpeg_executable = executable
        self.screen_process = start_screen_recording(
            self.recording_files["video"],
            monitor,
            fps=30,
            ffmpeg_executable=executable,
        )
        if self.screen_process.poll() is not None:
            raise RuntimeError(
                f"FFmpeg exited immediately with status {self.screen_process.returncode}."
            )

    def _start_audio_workers(self, monitor_number, loopback):
        recording_files = self.recording_files
        if recording_files is None:
            raise RuntimeError("Recording paths were not initialized.")
        microphone = self.microphone_choices[self.microphone_var.get()]
        self.stop_event = threading.Event()
        self.worker_errors = queue.Queue()
        self.audio_threads = [
            threading.Thread(
                target=audio_capture_worker,
                args=(
                    "Microphone",
                    microphone,
                    recording_files["microphone"],
                    1,
                    self.stop_event,
                    self.worker_errors,
                    self.ui_events,
                ),
                name="microphone-capture",
                daemon=True,
            ),
            threading.Thread(
                target=audio_capture_worker,
                args=(
                    "System audio",
                    loopback,
                    recording_files["system"],
                    2,
                    self.stop_event,
                    self.worker_errors,
                    self.ui_events,
                ),
                name="system-audio-capture",
                daemon=True,
            ),
        ]
        self.microphone_level_var.set(0)
        self.system_level_var.set(0)
        self._set_recording_state("recording")
        self.status_var.set(f"Recording monitor [{monitor_number}]...")
        for worker in self.audio_threads:
            try:
                worker.start()
            except RuntimeError as error:
                self.worker_errors.put(("Audio capture", str(error)))
                self.stop_recording()
                break

    def stop_recording(self):
        if self.recording_state != "recording":
            return

        stop_event = self.stop_event
        screen_process = self.screen_process
        if stop_event is None or screen_process is None:
            self.status_var.set("Unable to stop recording because capture is not active.")
            return

        stop_event.set()
        self._set_recording_state("finalizing")
        self.status_var.set("Stopping capture and finalizing the MP4...")
        if screen_process.poll() is None and screen_process.stdin:
            try:
                screen_process.stdin.write("q\n")
                screen_process.stdin.flush()
            except OSError as error:
                self.worker_errors.put(("Screen capture", str(error)))
                screen_process.terminate()

        finalizer = threading.Thread(
            target=self._finish_recording,
            name="recording-finalizer",
            daemon=True,
        )
        finalizer.start()

    def _finish_recording(self):
        try:
            screen_process = self.screen_process
            files = self.recording_files
            if screen_process is None or files is None:
                raise RuntimeError("Recording state was lost before finalization.")

            return_code, process_errors = self._wait_for_screen_process(screen_process)
            self._join_audio_workers()
            if return_code != 0:
                process_errors.append(f"Screen capture exited with status {return_code}.")
            capture_errors = self._take_capture_errors()
            if process_errors or capture_errors:
                errors = process_errors + capture_errors
                raise RuntimeError("Recording failed: " + "; ".join(errors))

            cleanup_warnings = finalize_recording(
                files["video"],
                files["microphone"],
                files["system"],
                files["mixed"],
                files["final"],
                ffmpeg_executable=self.recording_ffmpeg_executable,
            )
            self.ui_events.put(("finished", str(files["final"]), cleanup_warnings))
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            self.ui_events.put(("failed", str(error)))

    @staticmethod
    def _wait_for_screen_process(screen_process):
        try:
            return screen_process.wait(timeout=10), []
        except subprocess.TimeoutExpired:
            screen_process.terminate()
            try:
                return screen_process.wait(timeout=5), [
                    "FFmpeg did not stop promptly and was terminated."
                ]
            except subprocess.TimeoutExpired:
                screen_process.kill()
                return screen_process.wait(), [
                    "FFmpeg had to be force-stopped after timing out."
                ]

    def _join_audio_workers(self):
        for worker in self.audio_threads:
            if worker.ident is not None:
                worker.join()

    def _take_capture_errors(self):
        errors = []
        while True:
            try:
                source_name, message = self.worker_errors.get_nowait()
            except queue.Empty:
                return errors
            errors.append(f"{source_name}: {message}")

    def _poll_ui_events(self):
        self.event_poll_after_id = None
        while True:
            try:
                event = self.ui_events.get_nowait()
            except queue.Empty:
                break
            self._handle_ui_event(event)
            if self.closed:
                return

        screen_process = self.screen_process
        exit_message = (
            screen_capture_exit_message(screen_process)
            if self.recording_state == "recording" and screen_process is not None
            else None
        )
        if exit_message is not None:
            self.worker_errors.put(("Screen capture", exit_message))
            self.ui_events.put(
                ("capture_error", "Screen capture", exit_message)
            )

        if not self.closed:
            self.event_poll_after_id = self.root.after(100, self._poll_ui_events)

    def _handle_ui_event(self, event):
        event_type = event[0]
        if event_type == "level":
            self._update_level(event[1], event[2])
        elif event_type == "capture_error":
            self._handle_capture_error(event[1], event[2])
        elif event_type == "finished":
            self._handle_recording_finished(event[1], event[2])
        elif event_type == "failed":
            self._handle_recording_failed(event[1])

    def _update_level(self, source_name, level):
        level_var = (
            self.microphone_level_var
            if source_name == "Microphone"
            else self.system_level_var
        )
        level_var.set(level)

    def _handle_capture_error(self, source_name, message):
        self.status_var.set(f"{source_name} capture failed: {message}")
        if self.recording_state == "recording":
            self.stop_recording()

    def _handle_recording_finished(self, final_path, cleanup_warnings):
        self._set_recording_state("idle")
        self.microphone_level_var.set(0)
        self.system_level_var.set(0)
        warning_text = " " + " ".join(cleanup_warnings) if cleanup_warnings else ""
        self.status_var.set(f"Recording saved: {final_path}.{warning_text}")
        if self.close_after_recording:
            self._destroy()

    def _handle_recording_failed(self, message):
        self._set_recording_state("idle")
        files = self.recording_files
        temporary_directory = files["video"].parent if files else Path.cwd()
        self.status_var.set(
            f"Recording failed: {message} Temporary files were kept in {temporary_directory}."
        )
        if self.close_after_recording:
            self._destroy()

    def _refresh_preview(self):
        if self.preview_after_id is not None:
            try:
                self.root.after_cancel(self.preview_after_id)
            except tk.TclError:
                pass
            self.preview_after_id = None

        monitor = self.monitor_choices.get(self.monitor_var.get())
        if monitor is None:
            self._show_preview_message("Select a monitor to preview it")
            return
        try:
            screenshot = self.preview_capture.grab(monitor_capture_region(monitor))
            image = Image.frombytes("RGB", screenshot.size, screenshot.rgb)
            image.thumbnail((self.PREVIEW_WIDTH, self.PREVIEW_HEIGHT), Image.Resampling.LANCZOS)
            preview_photo = ImageTk.PhotoImage(image=image)
            self.preview_photo = preview_photo
            self.preview_canvas.delete("all")
            self.preview_canvas.create_image(
                self.PREVIEW_WIDTH // 2,
                self.PREVIEW_HEIGHT // 2,
                image=preview_photo,
                anchor="center",
            )
        except (ScreenShotError, OSError, tk.TclError, ValueError) as error:
            self._show_preview_message("Monitor preview unavailable")
            self.status_var.set(f"Monitor preview failed: {error}")
            return

        if self.recording_state == "idle":
            self.preview_after_id = self.root.after(750, self._refresh_preview)

    def _show_preview_message(self, message):
        self.preview_photo = None
        self.preview_canvas.delete("all")
        self.preview_canvas.create_text(
            self.PREVIEW_WIDTH // 2,
            self.PREVIEW_HEIGHT // 2,
            text=message,
            fill="#f0f0f0",
            width=self.PREVIEW_WIDTH - 40,
        )

    def close(self):
        if self.recording_state == "recording":
            self.close_after_recording = True
            self.stop_recording()
            return
        if self.recording_state == "finalizing":
            self.close_after_recording = True
            self.status_var.set("Closing after recording finalization...")
            return
        self._destroy()

    def _destroy(self):
        self.closed = True
        if self.preview_after_id is not None:
            try:
                self.root.after_cancel(self.preview_after_id)
            except tk.TclError:
                pass
            self.preview_after_id = None
        if self.event_poll_after_id is not None:
            try:
                self.root.after_cancel(self.event_poll_after_id)
            except tk.TclError:
                pass
            self.event_poll_after_id = None
        self.preview_capture.close()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    RecorderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
