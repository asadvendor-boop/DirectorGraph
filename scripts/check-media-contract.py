#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


TIMECODE = re.compile(
    r"^(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2}),(?P<ms>\d{3})"
    r"\s+-->\s+"
    r"(?P<eh>\d{2}):(?P<em>\d{2}):(?P<es>\d{2}),(?P<ems>\d{3})$"
)


@dataclass(frozen=True)
class Cue:
    index: int
    start: float
    end: float
    text: str


def seconds(hours: str, minutes: str, secs: str, millis: str) -> float:
    return int(hours) * 3600 + int(minutes) * 60 + int(secs) + int(millis) / 1000


def parse_srt(path: Path) -> list[Cue]:
    blocks = [block.strip() for block in path.read_text(encoding="utf-8-sig").split("\n\n") if block.strip()]
    cues: list[Cue] = []
    previous_end = 0.0
    for expected_index, block in enumerate(blocks, start=1):
        lines = [line.strip() for line in block.splitlines()]
        if len(lines) < 3:
            raise ValueError(f"SRT cue {expected_index} must include index, time range, and text")
        try:
            index = int(lines[0])
        except ValueError as exc:
            raise ValueError(f"SRT cue {expected_index} has non-numeric index: {lines[0]!r}") from exc
        if index != expected_index:
            raise ValueError(f"SRT cue index mismatch: expected {expected_index}, got {index}")
        match = TIMECODE.match(lines[1])
        if not match:
            raise ValueError(f"SRT cue {index} has invalid time range: {lines[1]!r}")
        start = seconds(match["h"], match["m"], match["s"], match["ms"])
        end = seconds(match["eh"], match["em"], match["es"], match["ems"])
        text = "\n".join(lines[2:]).strip()
        if not text:
            raise ValueError(f"SRT cue {index} has empty text")
        if start < previous_end:
            raise ValueError(f"SRT cue {index} starts before previous cue ends")
        if end <= start:
            raise ValueError(f"SRT cue {index} must end after it starts")
        cues.append(Cue(index=index, start=start, end=end, text=text))
        previous_end = end
    if not cues:
        raise ValueError("SRT must contain at least one cue")
    return cues


def probe_media(path: Path) -> dict:
    probe = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_name,width,height",
            "-of",
            "json",
            str(path),
        ],
        text=True,
    )
    data = json.loads(probe)
    streams = data.get("streams", [])
    codecs = {stream.get("codec_name") for stream in streams}
    video = next((stream for stream in streams if stream.get("codec_name") == "h264"), {})
    duration = float(data.get("format", {}).get("duration") or 0)
    checks = [
        ("h264" in codecs, "H.264 video stream"),
        ("aac" in codecs, "AAC audio stream"),
        ("mov_text" in codecs, "embedded mov_text caption stream"),
        (video.get("width") == 720 and video.get("height") == 1280, "720x1280 video"),
        (20 <= duration <= 55, "demo duration in expected preview range"),
    ]
    failed = [label for ok, label in checks if not ok]
    if failed:
        raise ValueError("Media check failed: " + ", ".join(failed))
    return {
        "codecs": sorted(str(codec) for codec in codecs if codec),
        "duration": duration,
        "height": video.get("height"),
        "width": video.get("width"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate DirectorGraph bundled media contract.")
    parser.add_argument("--video", default="examples/demo-output/directorgraph-preview.mp4")
    parser.add_argument("--captions", default="examples/demo-output/directorgraph-preview.srt")
    args = parser.parse_args()

    media = probe_media(Path(args.video))
    cues = parse_srt(Path(args.captions))
    if cues[-1].end > media["duration"] + 0.75:
        raise ValueError(
            f"SRT cue end {cues[-1].end:.3f}s exceeds media duration {media['duration']:.3f}s"
        )
    payload = {
        "captions": {
            "cue_count": len(cues),
            "first_start": cues[0].start,
            "last_end": cues[-1].end,
        },
        "schema": "directorgraph.media-contract.v1",
        "video": media,
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
