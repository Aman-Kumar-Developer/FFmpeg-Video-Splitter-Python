# FFmpeg Video Splitter

Automatically splits every supported video in `input/` into independently playable MP4 clips. FFmpeg handles stream copying or quality-conscious encoding; no Python media packages are required.

## Features

- Processes multiple videos concurrently with a configurable worker limit.
- Duration mode favors stream copying and re-encodes only when keyframe gaps exceed the configured duration.
- Size mode uses H.264/AAC encoding, retries oversized clips, and verifies each result against the configured cap.
- Creates a separate output subfolder for each source video. Original inputs remain untouched.

## Requirements and setup

- Python 3.10+
- FFmpeg and FFprobe available on `PATH` (`Get-Command ffmpeg, ffprobe` in PowerShell)

Place `video_splitter.py`, `config.txt`, and this README in the project root. The script creates `input/` and `output/` automatically. Place videos directly inside `input/`.

```text
VideoSplitter/
├── video_splitter.py
├── config.txt
├── README.md
├── input/    # Add source videos here
└── output/   # Generated clips, cleaned at each run
```

## Configure

Edit `[SETTINGS]` in `config.txt`; changes take effect the next time the script runs:

```ini
[SETTINGS]
split_mode = size
chunk_duration_seconds = 60
max_chunk_size_mb = 25
size_safety_margin_mb = 1

[PROCESSING]
parallel_processing = true
max_concurrent_videos = 2
```

Set `split_mode` to `duration` or `size`. Duration mode uses `chunk_duration_seconds`. Size mode treats `max_chunk_size_mb` as a strict cap and reserves `size_safety_margin_mb` below it. Both size values are configurable, including values such as 1024 MB.

Examples:

- Instagram-oriented: `split_mode = size`, cap `25`, margin `1` (aims below 24 MiB).
- Telegram-oriented: `split_mode = size`, cap `1024`, margin `10`.

The script uses binary MiB (1 MB setting = 1,048,576 bytes). Choose a safety margin below the cap.
Set `parallel_processing = false` or `max_concurrent_videos = 1` for sequential processing. Worker count is limited to 1–8; each FFmpeg encode uses at most two codec threads to avoid overwhelming CPU and memory.

## Run

From the project directory:

```powershell
python .\video_splitter.py
```

Unsupported files directly in `input/` are reported and skipped. Each source gets a folder named after its filename (without extension), with sequential clips such as `Travel_part_001.mp4`.

Every chunk is probed and fully decoded with FFmpeg before a video is marked successful. Validation checks non-empty files, duration/size limits, video resolution, and source audio presence. Passing these checks verifies media structure and decodability; it does not certify playback on every phone or messaging app.

## Important: output is cleared

**Every execution completely deletes and recreates the project `output/` directory before processing.** Copy any clips you need elsewhere before running again. The script restricts cleanup to the `output/` directory beside itself; it does not delete input files or paths elsewhere.
