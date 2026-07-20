#!/usr/bin/env python3
"""Download a Douyin media sample and prepare ASR-friendly audio.

This script expects already-discovered signed media URLs from a logged-in
Douyin browser session. It intentionally stores outputs under runtime/, which
is ignored by Git.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from urllib.request import Request, urlopen


def download(url: str, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150 Safari/537.36"
            )
        },
    )
    with urlopen(request, timeout=30) as response:
        output.write_bytes(response.read())


def run_ffmpeg(ffmpeg: Path, args: list[str]) -> None:
    command = [str(ffmpeg), "-hide_banner", *args]
    subprocess.run(command, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-id", required=True)
    parser.add_argument("--video-url", required=True)
    parser.add_argument("--audio-url")
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument(
        "--out-dir",
        default="douyin_creator_monitor/runtime/media",
        help="Output directory. Keep this under runtime/ so Git ignores it.",
    )
    args = parser.parse_args()

    ffmpeg = Path(args.ffmpeg)
    out_dir = Path(args.out_dir)
    video_path = out_dir / f"{args.work_id}.video.mp4"
    audio_path = out_dir / f"{args.work_id}.audio.m4a"
    merged_path = out_dir / f"{args.work_id}.merged.mp4"
    wav_path = out_dir / f"{args.work_id}.16k.wav"

    download(args.video_url, video_path)
    source_audio = audio_path
    if args.audio_url:
        download(args.audio_url, audio_path)
        run_ffmpeg(
            ffmpeg,
            [
                "-i",
                str(video_path),
                "-i",
                str(audio_path),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c",
                "copy",
                "-shortest",
                str(merged_path),
                "-y",
            ],
        )
    else:
        source_audio = video_path

    run_ffmpeg(
        ffmpeg,
        [
            "-i",
            str(source_audio),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(wav_path),
            "-y",
        ],
    )

    print(f"video={video_path}")
    if args.audio_url:
        print(f"audio={audio_path}")
        print(f"merged={merged_path}")
    print(f"wav={wav_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
