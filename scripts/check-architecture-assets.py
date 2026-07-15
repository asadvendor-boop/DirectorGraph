#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageStat


PNG = Path("docs/assets/architecture.png")
MIN_WIDTH = 1200
MIN_HEIGHT = 300


def require(condition: bool, message: str, violations: list[str]) -> None:
    if not condition:
        violations.append(message)


def main() -> None:
    violations: list[str] = []

    if not PNG.is_file():
        raise SystemExit(f"{PNG}: architecture diagram is missing")

    with Image.open(PNG) as image:
        width, height = image.size
        require(
            width >= MIN_WIDTH,
            f"{PNG}: expected at least {MIN_WIDTH}px width, got {width}",
            violations,
        )
        require(
            height >= MIN_HEIGHT,
            f"{PNG}: expected at least {MIN_HEIGHT}px height, got {height}",
            violations,
        )
        stat = ImageStat.Stat(image.convert("L"))
        require(
            (stat.extrema[0][1] - stat.extrema[0][0]) > 20,
            f"{PNG}: image appears blank",
            violations,
        )

    if violations:
        raise SystemExit("\n".join(violations))

    print(
        json.dumps(
            {
                "schema": "directorgraph.architecture-assets.v2",
                "status": "pass",
                "png": PNG.as_posix(),
                "png_size": [width, height],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
