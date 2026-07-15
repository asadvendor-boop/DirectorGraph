from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

from app.schemas import ProjectBrief, ShotContract


class MediaError(RuntimeError):
    pass


def run(command: list[str]) -> None:
    process = subprocess.run(command, capture_output=True, text=True)
    if process.returncode != 0:
        pretty = " ".join(shlex.quote(part) for part in command)
        raise MediaError(f"Command failed: {pretty}\n{process.stderr[-4000:]}")


def has_audio(path: Path) -> bool:
    process = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "json", str(path)],
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        return False
    return bool(json.loads(process.stdout).get("streams"))


def create_silence(path: Path, duration: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", str(duration), "-c:a", "pcm_s16le", str(path)])
    return path


def extract_frame(source: Path, destination: Path, timestamp: float = 1.0) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    run([
        "ffmpeg",
        "-y",
        "-ss",
        str(timestamp),
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(destination),
    ])
    return destination


def normalize_clip(
    source: Path,
    destination: Path,
    brief: ProjectBrief,
    duration: float,
    voice: Path | None = None,
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    width, height = (720, 1280) if brief.aspect_ratio == "9:16" else (1280, 720)
    if brief.aspect_ratio == "1:1":
        width, height = 960, 960
    vf = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps=24,format=yuv420p"
    command = ["ffmpeg", "-y", "-i", str(source)]
    if voice and voice.exists():
        command += ["-i", str(voice), "-map", "0:v:0", "-map", "1:a:0", "-vf", vf, "-af", "apad"]
    elif has_audio(source):
        command += ["-map", "0:v:0", "-map", "0:a:0", "-vf", vf, "-af", "aresample=48000,apad"]
    else:
        command += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-map", "0:v:0", "-map", "1:a:0", "-vf", vf]
    command += [
        "-t", str(duration), "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", str(destination),
    ]
    run(command)
    return destination


def _timestamp(seconds: float) -> str:
    millis = int(round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_srt(contracts: list[ShotContract], destination: Path) -> Path:
    cursor = 0.0
    entries: list[str] = []
    counter = 1
    for contract in contracts:
        end = cursor + contract.duration_seconds
        text = contract.dialogue or contract.narration
        if text:
            entries.append(f"{counter}\n{_timestamp(cursor + 0.15)} --> {_timestamp(end - 0.15)}\n{text}\n")
            counter += 1
        cursor = end
    destination.write_text("\n".join(entries), encoding="utf-8")
    return destination


def compose_timeline(
    clip_paths: list[Path],
    voice_paths: list[Path | None],
    contracts: list[ShotContract],
    destination: Path,
    brief: ProjectBrief,
) -> tuple[Path, Path]:
    if not (len(clip_paths) == len(voice_paths) == len(contracts)):
        raise ValueError("Every shot needs a clip, voice slot, and contract")
    work = destination.parent / "edit-work"
    work.mkdir(parents=True, exist_ok=True)
    normalized: list[Path] = []
    for index, (source, voice, contract) in enumerate(zip(clip_paths, voice_paths, contracts, strict=True), 1):
        target = work / f"{index:02d}.mp4"
        normalize_clip(source, target, brief, contract.duration_seconds, voice)
        normalized.append(target)
    concat_file = work / "concat.txt"
    concat_file.write_text("\n".join(f"file '{path.as_posix()}'" for path in normalized), encoding="utf-8")
    joined = work / "joined.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(joined)])
    captions = write_srt(contracts, destination.with_suffix(".srt"))
    if captions.read_text(encoding="utf-8").strip():
        run([
            "ffmpeg", "-y", "-i", str(joined), "-i", str(captions),
            "-map", "0:v", "-map", "0:a", "-map", "1:0",
            "-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text",
            "-metadata:s:s:0", "language=eng", "-movflags", "+faststart", str(destination),
        ])
    else:
        destination.write_bytes(joined.read_bytes())
    return destination, captions
