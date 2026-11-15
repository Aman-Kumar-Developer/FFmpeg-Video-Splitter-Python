"""Split videos into independently playable MP4 clips using FFmpeg."""

from __future__ import annotations

import json
import math
import configparser
import concurrent.futures
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


DEFAULT_DURATION_SECONDS = 60
DEFAULT_MAX_SIZE_MB = 25
DEFAULT_SAFETY_MARGIN_MB = 1
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".mpeg", ".mpg", ".ts"}
MAX_SIZE_RETRIES = 8


class SplitError(Exception):
    """A user-facing processing error."""


def require_tools() -> tuple[str, str]:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        missing = " and ".join(name for name, path in (("FFmpeg", ffmpeg), ("FFprobe", ffprobe)) if not path)
        raise SplitError(f"{missing} not found on PATH. Install it and reopen the terminal.")
    return ffmpeg, ffprobe


def run(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, check=True, text=True, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                              stderr=subprocess.PIPE if capture else subprocess.DEVNULL)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip().splitlines()
        raise SplitError(detail[-1] if detail else "FFmpeg/FFprobe failed") from exc
    except OSError as exc:
        raise SplitError(str(exc)) from exc


def probe(ffprobe: str, path: Path) -> dict:
    result = run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], capture=True)
    try:
        data = json.loads(result.stdout or "{}")
        video = next(s for s in data["streams"] if s.get("codec_type") == "video")
        duration = float(video.get("duration") or data["format"].get("duration"))
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("invalid duration")
        return {"duration": duration, "video": video, "audio": any(s.get("codec_type") == "audio" for s in data["streams"])}
    except (KeyError, StopIteration, TypeError, ValueError) as exc:
        raise SplitError(f"Cannot read a valid video stream/duration from {path.name}") from exc


def load_config(path: Path) -> dict:
    parser = configparser.ConfigParser()
    try:
        if path.exists():
            parser.read(path, encoding="utf-8")
        mode = parser.get("SETTINGS", "split_mode", fallback="size").strip().lower()
        duration = parser.getfloat("SETTINGS", "chunk_duration_seconds", fallback=DEFAULT_DURATION_SECONDS)
        size = parser.getfloat("SETTINGS", "max_chunk_size_mb", fallback=DEFAULT_MAX_SIZE_MB)
        margin = parser.getfloat("SETTINGS", "size_safety_margin_mb", fallback=DEFAULT_SAFETY_MARGIN_MB)
        parallel = parser.getboolean("PROCESSING", "parallel_processing", fallback=True)
        workers = parser.getint("PROCESSING", "max_concurrent_videos", fallback=min(2, os.cpu_count() or 1))
    except (ValueError, configparser.Error) as exc:
        raise SplitError(f"Invalid config.txt setting: {exc}") from exc
    if mode not in {"duration", "size"}:
        raise SplitError("config.txt: split_mode must be 'duration' or 'size'")
    if not math.isfinite(duration) or duration <= 0:
        raise SplitError("config.txt: chunk_duration_seconds must be greater than zero")
    if not math.isfinite(size) or size <= 0:
        raise SplitError("config.txt: max_chunk_size_mb must be greater than zero")
    if not math.isfinite(margin) or margin < 0 or margin >= size:
        raise SplitError("config.txt: size_safety_margin_mb must be >= 0 and less than max_chunk_size_mb")
    if workers < 1 or workers > 8:
        raise SplitError("config.txt: max_concurrent_videos must be between 1 and 8")
    return {"mode": mode, "duration": duration, "max_bytes": int(size * 1024 * 1024),
            "target_bytes": int((size - margin) * 1024 * 1024), "parallel": parallel, "workers": workers}


def prepare_output(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for child in output.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def ensure_space(directory: Path, estimated_bytes: int) -> None:
    try:
        free = shutil.disk_usage(directory).free
        if free < estimated_bytes + 32 * 1024 * 1024:
            raise SplitError("Not enough free disk space for this output (including a 32 MB reserve).")
    except OSError:
        pass


def duration_split(ffmpeg: str, source: Path, output: Path, chunk_seconds: float) -> list[Path]:
    pattern = output / f"{source.stem}_part_%03d.mp4"
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(source), "-map", "0:v:0", "-map", "0:a?",
         "-c", "copy", "-f", "segment", "-segment_time", str(chunk_seconds), "-reset_timestamps", "1",
         "-segment_start_number", "1", "-segment_format", "mp4", "-segment_format_options", "movflags=+faststart", str(pattern)])
    parts = sorted(output.glob(f"{source.stem}_part_*.mp4"))
    if not parts:
        raise SplitError("FFmpeg produced no clips")
    # Stream-copy cuts land on keyframes. If that makes a segment materially longer
    # than requested, re-encode with forced keyframes so the time limit is respected.
    ffprobe = shutil.which("ffprobe")
    if ffprobe and any(probe(ffprobe, part)["duration"] > chunk_seconds + 0.25 for part in parts):
        for part in parts:
            part.unlink()
        info = probe(ffprobe, source)
        start, index = 0.0, 1
        while start < info["duration"] - 0.02:
            length = min(chunk_seconds, info["duration"] - start)
            destination = output / f"{source.stem}_part_{index:03d}.mp4"
            cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-ss", f"{start:.6f}", "-i", str(source),
                   "-t", f"{length:.6f}", "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "medium",
                   "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(destination)]
            run(cmd)
            start += length
            index += 1
        parts = sorted(output.glob(f"{source.stem}_part_*.mp4"))
    return parts


def encode_segment(ffmpeg: str, source: Path, destination: Path, start: float, duration: float,
                   video_kbps: int, audio_kbps: int, has_audio: bool) -> None:
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-threads", "2", "-ss", f"{start:.6f}", "-i", str(source),
           "-t", f"{duration:.6f}", "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "medium",
           "-b:v", f"{video_kbps}k", "-maxrate", f"{video_kbps}k", "-bufsize", f"{max(video_kbps, 128)}k",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", f"{audio_kbps}k" if has_audio else "64k",
           "-movflags", "+faststart", "-sn", "-dn", str(destination)]
    run(cmd)


def size_split(ffmpeg: str, source: Path, output: Path, info: dict, max_bytes: int, target_bytes: int) -> list[Path]:
    duration, has_audio = info["duration"], info["audio"]
    # Start with chunks expected to use about 92% of the cap; each is measured and retried.
    segment_seconds = duration * target_bytes / max(source.stat().st_size, 1)
    segment_seconds = max(1.0, min(duration, segment_seconds))
    parts: list[Path] = []
    start = 0.0
    index = 1
    while start < duration - 0.02:
        length = min(segment_seconds, duration - start)
        path = output / f"{source.stem}_part_{index:03d}.mp4"
        ensure_space(output, max_bytes)
        attempt_length = length
        retry_target_bytes = target_bytes
        for attempt in range(MAX_SIZE_RETRIES):
            audio_kbps = 128 if has_audio else 64
            total_kbps = max(180, int(retry_target_bytes * 8 / attempt_length / 1000))
            video_kbps = max(100, total_kbps - audio_kbps - 48)
            try:
                encode_segment(ffmpeg, source, path, start, attempt_length, video_kbps, audio_kbps, has_audio)
            except SplitError:
                path.unlink(missing_ok=True)
                raise
            actual = path.stat().st_size
            if actual <= max_bytes:
                break
            path.unlink(missing_ok=True)
            ratio = retry_target_bytes / actual
            if attempt < 3 and ratio > 0.65:
                retry_target_bytes = int(retry_target_bytes * max(0.5, ratio * 0.90))
            else:
                attempt_length /= 2
            if attempt_length < 0.25:
                raise SplitError(f"Could not encode a clip below the {max_bytes:,}-byte limit")
        else:
            raise SplitError("Exceeded size retry limit")
        parts.append(path)
        start += attempt_length
        segment_seconds = min(segment_seconds, length)
        index += 1
    return parts


def validate_parts(ffmpeg: str, ffprobe: str, source_info: dict, parts: list[Path], config: dict) -> list[str]:
    warnings = []
    for part in parts:
        if not part.is_file() or part.stat().st_size == 0:
            raise SplitError(f"Missing or empty chunk: {part.name}")
        info = probe(ffprobe, part)
        if source_info["audio"] and not info["audio"]:
            raise SplitError(f"Audio missing from chunk: {part.name}")
        src_v, out_v = source_info["video"], info["video"]
        if (src_v.get("width"), src_v.get("height")) != (out_v.get("width"), out_v.get("height")):
            raise SplitError(f"Resolution changed in chunk: {part.name}")
        if config["mode"] == "size" and part.stat().st_size > config["max_bytes"]:
            raise SplitError(f"Chunk exceeds configured size cap: {part.name}")
        if config["mode"] == "duration" and info["duration"] > config["duration"] + 0.5:
            raise SplitError(f"Chunk exceeds configured duration: {part.name}")
        check = run([ffmpeg, "-hide_banner", "-v", "error", "-xerror", "-i", str(part), "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"], capture=True)
        if check.stderr.strip():
            warnings.append(f"Decoder warning in {part.name}: {check.stderr.strip().splitlines()[-1]}")
    return warnings


def process_video(source_text: str, output_text: str, ffmpeg: str, ffprobe: str, config: dict) -> dict:
    source, destination = Path(source_text), Path(output_text)
    started = time.perf_counter()
    try:
        print(f"Starting: {source.name}", flush=True)
        source_info = probe(ffprobe, source)
        parts = (duration_split(ffmpeg, source, destination, config["duration"]) if config["mode"] == "duration"
                 else size_split(ffmpeg, source, destination, source_info, config["max_bytes"], config["target_bytes"]))
        warnings = validate_parts(ffmpeg, ffprobe, source_info, parts, config)
        return {"source": source.name, "ok": True, "count": len(parts), "seconds": time.perf_counter() - started,
                "warnings": warnings, "source_info": source_info,
                "chunks": [(p.name, p.stat().st_size, probe(ffprobe, p)["duration"], probe(ffprobe, p)["video"].get("codec_name")) for p in parts]}
    except Exception as exc:
        shutil.rmtree(destination, ignore_errors=True)
        return {"source": source.name, "ok": False, "error": str(exc), "seconds": time.perf_counter() - started}


def main() -> int:
    try:
        project = Path(__file__).resolve().parent
        input_dir, output_dir = project / "input", project / "output"
        input_dir.mkdir(parents=True, exist_ok=True)
        prepare_output(output_dir)
        config = load_config(project / "config.txt")
        ffmpeg, ffprobe = require_tools()
        entries = sorted(p for p in input_dir.iterdir() if p.is_file())
        videos = [p for p in entries if p.suffix.lower() in VIDEO_EXTENSIONS]
        skipped_files = [p for p in entries if p.suffix.lower() not in VIDEO_EXTENSIONS]
        for skipped in skipped_files:
            print(f"Skipping unsupported file: {skipped.name}")
        if not videos:
            print(f"No supported videos found in {input_dir}")
            return 0
        workers = min(config["workers"], len(videos)) if config["parallel"] else 1
        print(f"Mode: {config['mode']} | Workers: {workers} | Duration: {config['duration']:g}s | Size cap: {config['max_bytes'] / 1048576:g} MiB")
        jobs = []
        for source in videos:
            destination = output_dir / source.stem
            destination.mkdir(parents=True, exist_ok=True)
            jobs.append((str(source), str(destination), ffmpeg, ffprobe, config))
        started = time.perf_counter()
        if workers == 1:
            results = [process_video(*job) for job in jobs]
        else:
            results = []
            with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(process_video, *job): Path(job[0]).name for job in jobs}
                for future in concurrent.futures.as_completed(futures):
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        results.append({"source": futures[future], "ok": False, "error": str(exc), "seconds": 0})
        passed = [r for r in results if r["ok"]]
        failed = [r for r in results if not r["ok"]]
        for result in sorted(results, key=lambda r: r["source"].lower()):
            if result["ok"]:
                print(f"PASS {result['source']}: {result['count']} chunk(s), {result['seconds']:.1f}s")
                for warning in result["warnings"]:
                    print(f"  WARNING: {warning}")
                for name, size, duration, codec in result["chunks"]:
                    print(f"  {name}: {size} bytes, {duration:.2f}s, video={codec}")
            else:
                print(f"FAIL {result['source']}: {result['error']}", file=sys.stderr)
        print(f"Summary: {len(passed)} passed, {len(failed)} failed, {len(skipped_files)} skipped; total {time.perf_counter()-started:.1f}s. Output: {output_dir}")
        return 1 if failed else 0
    except (SplitError, KeyboardInterrupt) as exc:
        if isinstance(exc, KeyboardInterrupt):
            print("Interrupted; the current FFmpeg process was stopped.", file=sys.stderr)
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
