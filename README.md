# recordSnd

Select a monitor and audio devices in a Windows desktop UI, then capture:

- screen video with `ffmpeg`
- microphone audio
- system audio loopback

The script then mixes the audio tracks and muxes them into a final `.mp4` file.

## Requirements

- Windows
- Conda
- `ffmpeg` available through the Conda environment
- A working microphone and speaker device
- Python packages listed in `environment.yml`

## Setup

Create the Conda environment:

```powershell
conda env create -f environment.yml
conda activate recordsnd
```

## Install file

This project uses `environment.yml` so Conda can install both Python dependencies and the native `ffmpeg` package.

## Run

```powershell
python .\record-desktop-with-selection-of-monitor.py
```

## Portable Windows build

The one-folder build includes the Python/Tk runtime, application dependencies,
and FFmpeg. The tested FFmpeg runtime contributes about 212 MB across 190 DLLs.
The target computer does not need Python, Conda, or a separately installed
FFmpeg, but the complete folder must stay together; this is not a single-file
executable.

Build from the project environment:

```powershell
conda activate recordsnd
python -m pip install -r .\packaging\requirements-build.txt
.\packaging\build_windows.ps1
```

The output is `dist\recordSnd\recordSnd.exe` and its `_internal` runtime
folder. Share or zip the entire `dist\recordSnd\` folder. The build script
refuses to overwrite an existing output folder; move the old folder yourself
before rebuilding.

The bundle includes the GPL-enabled FFmpeg build with `libx264`, plus FFmpeg and
dependency notices under `LICENSES`. Review those notices and the licenses for
all bundled components before public or commercial redistribution. If a custom
FFmpeg binary has no discoverable Conda package metadata, add its dependency
notices manually.

## How it works

1. Opens a window listing detected monitors by Windows display number and marking the primary display
2. Shows a live preview of the selected monitor
3. Lets you choose a microphone and a speaker for system-audio loopback, with a live level meter for each
4. Starts screen and audio capture when you select **Start recording**; audio blocks are written directly to temporary WAV files to keep memory use bounded
5. Stops capture when you select **Stop recording**
6. Mixes the microphone and system-audio tracks, then combines them with the screen video into a final MP4

## Output

Recordings are saved in:

```text
.\recordings\
```

This is the default output folder. Use **Settings** in the recorder window to
choose a different folder; the choice is remembered between launches.

Generated files include:

- temporary screen recording
- temporary microphone WAV
- temporary system-audio WAV
- temporary mixed-audio WAV
- final MP4 output

Temporary files are deleted after successful finalization. If recording or FFmpeg finalization fails, they are kept in `recordings\` for troubleshooting.

The screen and final MP4 filenames include the selected Windows display number,
for example `desktop_monitor3_2026-10-12_21-00-00.mp4`.
Existing recordings are not renamed.

## Notes

- The **Settings** dialog remembers the recordings folder and optional FFmpeg
  executable for the current Windows user in `%APPDATA%\recordSnd\settings.json`.
- Leave the FFmpeg path empty to use automatic discovery (`PATH`, the active
  Conda environment, then the Conda base installation). A selected executable
  takes precedence.
- To check your display numbers, open **Settings > System > Display** and click
  **Identify**. Select the matching number in the monitor selector.
- Display numbers come from the Windows device names (`\\.\DISPLAY1`,
  `\\.\DISPLAY2`, etc.), not detection order or left-to-right position.
  Only detected numbers are accepted; gaps are preserved.
- Choose the microphone input and system-audio output independently in the window. The level bars show current input activity.

## Troubleshooting

### FFmpeg unavailable

The recorder reports this before starting capture and keeps **Start recording**
disabled. Either create and activate the documented Conda environment:

```powershell
conda env create -f environment.yml
conda activate recordsnd
```

Then verify that FFmpeg is available:

```powershell
where.exe ffmpeg
ffmpeg -version
```

Alternatively, open **Settings**, browse to `ffmpeg.exe`, and save the path.
An invalid selected path is reported directly rather than silently falling back.

### No monitors detected

Check that Windows detects the displays and that the monitor is enabled.

### Audio device not found

Connect or enable the microphone and speaker in Windows, then close and reopen the recorder so it can refresh the device list.

## Tests

Run the monitor-selection tests in the configured Python environment:

```powershell
python -m unittest test_monitor_selection -v
```

## Main files

- `record-desktop-with-selection-of-monitor.py` — recording script
- `environment.yml` — Conda environment definition
