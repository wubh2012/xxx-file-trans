[简体中文](README.md) · English

<div align="center">

# xxx-file-trans

**Turn files into visual frames and transfer them one way through screens, videos, or PNG sequences.**

[![Windows](https://img.shields.io/badge/Windows-11-0078D4)](#getting-started)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB)](#run-from-source)
[![License: MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)

[Download](https://github.com/wubh2012/xxx-file-trans/releases/latest) · [Features](#features) · [Get started](#getting-started) · [Workflow](#workflow) · [FAQ](#faq) · [Documentation](#documentation-and-contributing)

</div>

The sender compresses a file, splits it into chunks, and encodes them as black-and-white block frames. The receiver reads those frames and reconstructs the original file. Neither a direct network connection nor per-frame acknowledgments are required. Play the frames in a browser, or create an MP4 or PNG sequence for manual handoff.

## What it is for

Use it when ordinary file-transfer channels are unavailable but you can still display or exchange visual content: logs, configuration files, reports, and small document bundles.

| Your task | Recommended path |
| --- | --- |
| The receiver can see the sender's playback on its desktop | Browser window playback → desktop capture → file reconstruction |
| The two sides need an asynchronous handoff | Create an MP4 → manually hand off the video → offline reconstruction |
| Verify encoding and decoding, or exchange static frames | Export PNG frames → read the directory → file reconstruction |

These instructions target **Windows 11**. The `camera` source is currently an interface stub; full camera capture is not implemented. Remote-desktop scaling and video-platform recompression need validation in your actual environment.

## Demos

- [Bilibili test video 1 (BV1Euan6zEL8)](https://www.bilibili.com/video/BV1Euan6zEL8/)
- [Bilibili test video 2 (BV1ata16YEWg)](https://www.bilibili.com/video/BV1ata16YEWg/)

Use these to test recognition and reconstruction from platform playback. Select the highest quality and keep the complete data frame visible. Scaling, recompression, and capture conditions can affect the result.

## Features

- **Browser sender** — single-file `sender.html`, no dependencies or build step; window playback, fullscreen playback, and PNG export.
- **Multiple receiver inputs** — GUI and CLI support desktop capture, video files, and PNG sequences.
- **MP4 creation** — a separate GUI and CLI encode files directly into video without recording the screen first. Requires FFmpeg.
- **Missing-frame recovery and resume** — CRC32 checks, forward error correction, repeated playback, and persisted progress handle corruption, missing frames, and interruptions.
- **Experimental fountain mode (since v1.1.0)** — interspersed XOR repair frames in the first pass, followed by continuously generated repair frames. The browser offers 32:1, 32:2, 16:1, and 8:1 ratios; the receiver detects the mode automatically. Fixed FEC remains the default.
- **Integrity checks** — gzip integrity checks during reconstruction, followed by a summary with filename, size, elapsed time, and SHA-256 digest.

Fountain mode reduces waiting for the last missing chunks by supplying new repair information without receiver feedback. It uses random linear combinations over GF(2), with up to 32 chunks per group; it is not LT/RaptorQ. Local comparisons showed a 36.1% reduction in median time with MSS capture and a 5.9% increase with stable DXGI capture. See the [fountain experiment guide](docs/fountain-experiment.md) for compatibility and full results.

## Getting started

### Download for Windows

Get these files from the [latest release](https://github.com/wubh2012/xxx-file-trans/releases/latest). Python setup is not required:

| File | Purpose |
| --- | --- |
| [sender.html](https://github.com/wubh2012/xxx-file-trans/releases/latest/download/sender.html) | Browser playback and PNG export |
| [receiver_gui.exe](https://github.com/wubh2012/xxx-file-trans/releases/latest/download/receiver_gui.exe) | Desktop, video, and PNG reception |
| [tapemaker_gui.exe](https://github.com/wubh2012/xxx-file-trans/releases/latest/download/tapemaker_gui.exe) | MP4 creation |
| [SHA256SUMS.txt](https://github.com/wubh2012/xxx-file-trans/releases/latest/download/SHA256SUMS.txt) | Download checksums |

Place the EXEs in a writable directory and double-click to run. **MP4 creation still requires FFmpeg on PATH**; reopen the video maker after installing it.

### Complete your first transfer (GUI)

Start with a small file on one machine to verify the complete loop. The steps include the Chinese control labels for reference.

1. Open `sender.html` in a modern browser. Click the desktop preset («预设：desktop 抓屏») for window playback, BIT 6, and 20 FPS.
2. Select or drop a small file, then click Start playback («开始播放»). Keep the entire frame visible.
3. Open `receiver_gui.exe`. Select Desktop capture («抓屏（desktop）») → Select region («框选…»). In the frozen screenshot, select the complete data area, all four corner markers, and the surrounding quiet border.
4. Click Start receiving («开始接收»). Wait for Reconstruction complete («还原完成»). The file is saved in `output/` next to the receiver EXE by default.
5. Confirm success, then press Esc in the sender page or click Stop playback («停止播放»). The sender cannot see receiver progress and must be stopped manually.

**Start playback before selecting the region.** Avoid obscuring or cropping the window, moving playback to a background tab, or letting the screen turn off. Esc during region selection cancels reception.

### Run from source

Requires Python ≥ 3.11 and a modern browser. Video creation also requires FFmpeg on PATH. Use Windows PowerShell; after cloning, run commands from the repository root:

```powershell
git clone https://github.com/wubh2012/xxx-file-trans.git
cd xxx-file-trans
py -3.11 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

For another Python version ≥ 3.11, replace `py -3.11` with the corresponding launcher command. Open [sender.html](sender.html), choose the desktop preset, load a small file, and start playback. Then run:

```powershell
# Receive after interactive region selection
.venv/Scripts/python.exe -m receiver receive --source desktop --region pick

# Or start the GUI
.venv/Scripts/pythonw.exe receiver_gui.pyw
```

On Windows, default `--capture auto` prefers DXGI and falls back to MSS when unavailable. The CLI reports the actual backend. Verify stability with the preset before trying 30 FPS.

On completion, the receiver displays filename, size, elapsed time, frame counts, and SHA-256 digest. Source runs save files in the repository's `output/` by default. If both files are accessible, compare their full hashes:

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath sample.txt
Get-FileHash -Algorithm SHA256 -LiteralPath output/sample.txt
```

The hashes should match. Stop the sender manually after completion.

## Workflow

```text
File → compress, split, encode → black-and-white frames
                                ├─ browser replay → desktop capture ─┐
                                ├─ MP4 creation → manual handoff ────┤
                                └─ PNG export → directory handoff ───┤
                                                                     ↓
                                  locate, decode, check, recover → original file
```

For example, load a log in the browser and select its playback region in the receiver. Once all chunks arrive, the receiver writes the log and displays a completion summary.

### Create an MP4 and reconstruct offline

This example assumes you have `sample.zip` and `ffmpeg` on PATH:

```powershell
# Create a video
.venv/Scripts/python.exe -m tapemaker make "sample.zip" -o out.mp4

# Reconstruct and compare locally before handoff
.venv/Scripts/python.exe -m receiver receive --source video --video out.mp4 --out out_pre

# Recipient: reconstruct after obtaining the video
.venv/Scripts/python.exe -m receiver receive --source video --video out.mp4

# Or start the video-maker GUI
.venv/Scripts/pythonw.exe tapemaker_gui.pyw
```

Generated MP4s and ordinary screen recordings are parsed frame by frame by default. `--tape` is unnecessary and remains for compatibility with older commands. Uploads and downloads are manual. Validate platform changes to resolution, bitrate, or visual content using representative samples. See the [video-maker guide](docs/tapemaker.md) for repeated rounds and recompression calibration.

### Receive PNG sequences or screen recordings

Click Export PNG frame sequence («导出 PNG 帧序列») and hand off the exported directory, or supply an ordinary recording of playback:

```powershell
.venv/Scripts/python.exe -m receiver receive --source images --dir frames_png
.venv/Scripts/python.exe -m receiver receive --source video --video recording.mp4
```

Append `--out <directory>` to choose another output location.

### Resume reception

Replay the same file with the original geometry, chunk size, and coding mode, then restart reception. Saved chunks in `progress/` are reused. Before switching coding modes, back up progress you need and clear the corresponding unfinished task. Unsolved fountain repair equations are held only in memory and must be accumulated again after a restart.

Default fixed FEC adds 2 parity frames per group of up to 32 data frames. Up to 2 missing data frames per group can be recovered when sufficient valid frames are available; replay fills remaining gaps. See the [protocol](docs/protocol.md).

## File sizes and measured performance

Begin with a **representative 1–4 MB file**. Local tests suggest 1–5 MB is practical for routine transfers. Allow about 3 minutes for 10 MB of poorly compressible content; compressible text can be faster.

| Sample | Original size | End-to-end time |
| --- | ---: | ---: |
| Poorly compressible binary | 1 MB | 15.3 s |
| Poorly compressible binary | 5 MB | 80.5 s |
| Poorly compressible binary | 10 MB | 160.8 s |
| Synthetic application log | 1 MB | 3.0 s |
| Synthetic application log | 10 MB | 28.4 s |
| Synthetic business CSV | 5 MB | 31.3 s |

Tests ran on 2026-10-01 with DXGI capture, 30 FPS, and a clear, unobscured browser window. Each data frame carried 2,431 bytes; each sample had one formal run. Timing included loading, compression, playback, reception, reconstruction, and disk writes. All six passed SHA-256 and byte-for-byte comparisons without replay to fill gaps. 1 MB = 1,000,000 bytes.

Binary samples were pseudorandom; logs and CSV data were synthetic. These are neither speed guarantees nor file-size limits. Larger files, cameras, remote desktops, and video recompression were not covered. See the [file-size report](benchmark/results/report-size-matrix-20261001.md) and [three runs with a 3.57 MB JPG](benchmark/results/report-panda-optimized-20261001.md).

## Local data and limitations

| Item | Behavior or limitation |
| --- | --- |
| Storage root | Repository root for source runs; executable directory for packaged apps |
| `output/` | Reconstructed files; override with `--out` |
| `progress/` | Persisted progress; deleting a task removes its resumable data |
| `debug/` | Debug-image directory |
| Platform | Instructions target Windows 11; source runs need Python ≥ 3.11, EXEs do not |
| Frame quality | Keep data, markers, and quiet border clear and complete; scaling, compression, and occlusion may cause recognition failures |
| Camera | `camera` is an interface stub and cannot provide full live reception |
| Video platforms | Manual upload/download; test reconstruction after recompression |
| Security | No built-in encryption, authentication, or digital signatures; CRC and error correction handle corruption and missing frames |

## FAQ

### Why is there no progress, or why does reception never finish?

Check the region includes the complete canvas, four markers, and quiet border. Keep playback visible and uncropped. Lower FPS first; higher rates can increase frame loss and replay waits. To collect recognition statistics, run this command and press Ctrl+C after sampling:

```powershell
.venv/Scripts/python.exe -m receiver calibrate --source desktop --region pick
```

### Why does video reconstruction fail?

Confirm the video exists and is readable. Test the original and handed-off versions separately to identify recompression damage. Neither generated MP4s nor recordings need `--tape`. Simulate recompression with:

```powershell
.venv/Scripts/python.exe -m tapemaker calibrate "sample.zip" -o calibration
```

Simulation does not replace actual platform validation. See the [video-maker guide](docs/tapemaker.md).

### Why does resuming report mismatched parameters?

Restore the original sender settings. To restart with different parameters, back up progress you need, clear the corresponding task directory under `progress/`, and receive from scratch.

### Why can the packaged video maker not find FFmpeg?

Install FFmpeg, add the directory containing `ffmpeg.exe` to PATH, and reopen the video maker. The EXE removes Python setup; MP4 encoding still needs FFmpeg.

## Tech stack and project layout

| Component | Technology |
| --- | --- |
| Sender | Single-file HTML, vanilla JavaScript, Canvas |
| Reception | Python ≥ 3.11, OpenCV, NumPy |
| Capture | DXGI (dxcam), MSS |
| GUI | Tkinter; Windows EXEs packaged with PyInstaller |
| Video creation | Python, FFmpeg |
| Testing | pytest, Playwright end-to-end tests |

```text
.
├── sender.html          # Browser sender
├── receiver/            # Reception, decoding, correction, reconstruction
├── receiver_gui.pyw     # Receiver GUI
├── tapemaker/           # MP4 creation and recompression calibration
├── tapemaker_gui.pyw    # Video-maker GUI
├── tests/               # Unit and end-to-end tests
├── benchmark/           # Tools, reports, and results
├── docs/                # Protocol, guides, and architecture decisions
└── build_exe.ps1        # Windows packaging
```

## Documentation and contributing

Detailed documents are currently primarily in Chinese:

- [Video-maker guide](docs/tapemaker.md): MP4 creation, repeated rounds, reconstruction, and calibration.
- [Fountain experiment](docs/fountain-experiment.md): principles, compatibility, usage, and results.
- [Protocol](docs/protocol.md): frame format, checks, correction, and resume rules.
- [File-size report](benchmark/results/report-size-matrix-20261001.md): samples, conditions, and results.
- [Benchmark tools](benchmark/README.md): performance testing and diagnostics.
- [Requirements](需求文档.md): requirements and scenarios.
- [Domain glossary](CONTEXT.md) and [architecture decisions](docs/adr/): concepts, naming, and rationale.

Report problems through [Issues](https://github.com/wubh2012/xxx-file-trans/issues). Include your OS, Python version for source runs, sender settings, input source, actual capture backend, errors, and reproduction steps. For video issues, state whether the video was recompressed.

Read the protocol and glossary before making changes. Protocol changes require updates to requirements, the protocol document, and both implementations. Keep both READMEs in sync for user-facing changes.

### Testing and packaging

Use the repository's `.venv` interpreter from the repository root:

```powershell
# Install Chromium before the first end-to-end test run
.venv/Scripts/python.exe -m playwright install chromium
.venv/Scripts/python.exe -m pytest

# Package the Windows GUIs
.\build_exe.ps1
```

Tests clear `.tmp/`; do not store important files there, and clean temporary artifacts after testing. Packaging creates `dist/receiver_gui.exe` and `dist/tapemaker_gui.exe`. Both run without Python; MP4 creation still requires FFmpeg on PATH. Receiver data defaults to directories next to its EXE.

## License

This project is licensed under the [MIT License](LICENSE).
