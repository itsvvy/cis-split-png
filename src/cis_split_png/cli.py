from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


LEFT_DIR = "images_rectified_left"
RIGHT_DIR = "images_rectified_right"
FRAME_START = 0
PNG_HEADER = b"\x89PNG\r\n\x1a\n"


class CisSplitError(RuntimeError):
    """Raised when a CIS video cannot be split or validated."""


@dataclass(frozen=True)
class VideoInfo:
    path: Path
    width: int
    height: int


def resolve_input(value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_file():
        return path.resolve()
    if path.is_dir():
        for candidate in (
            path / "cis" / "stereo.mkv",
            path / "stereo.mkv",
        ):
            if candidate.is_file():
                return candidate.resolve()
        raise CisSplitError(
            f"session directory does not contain cis/stereo.mkv: {path}"
        )
    raise CisSplitError(f"input file does not exist: {path}")


def _tool(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise CisSplitError(
            f"required executable is not available on PATH: {name}"
        )
    return executable


def probe_video(path: Path) -> VideoInfo:
    command = [
        _tool("ffprobe"),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "ffprobe failed"
        raise CisSplitError(f"cannot read input video: {detail}")
    try:
        streams = json.loads(result.stdout).get("streams", [])
        width = int(streams[0]["width"])
        height = int(streams[0]["height"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise CisSplitError(f"input has no readable video stream: {path}") from None
    if width <= 0 or height <= 0:
        raise CisSplitError(f"invalid video dimensions: {width}x{height}")
    return VideoInfo(path=path, width=width, height=height)


def _split_geometry(info: VideoInfo, split_x: int | None) -> tuple[int, int]:
    actual_split_x = info.width // 2 if split_x is None else split_x
    if not 0 < actual_split_x < info.width:
        raise CisSplitError(
            f"--split-x must be between 1 and {info.width - 1}, "
            f"got {actual_split_x}"
        )
    return actual_split_x, info.width - actual_split_x


def _ensure_output_is_available(output_root: Path, overwrite: bool) -> None:
    if output_root.exists() and not output_root.is_dir():
        raise CisSplitError(f"output is not a directory: {output_root}")
    existing = [
        output_root / name
        for name in (LEFT_DIR, RIGHT_DIR)
        if (output_root / name).exists()
    ]
    if existing and not overwrite:
        joined = ", ".join(str(path) for path in existing)
        raise CisSplitError(
            f"output already exists: {joined}; use --overwrite to replace it"
        )


def _frame_names(directory: Path) -> list[Path]:
    return sorted(path for path in directory.glob("*.png") if path.is_file())


def _validate_frames(directory: Path, expected_width: int, expected_height: int) -> int:
    frames = _frame_names(directory)
    if not frames:
        raise CisSplitError(f"no PNG frames were written to {directory}")
    for index, frame in enumerate(frames):
        expected_name = f"{FRAME_START + index:06d}.png"
        if frame.name != expected_name:
            raise CisSplitError(
                f"unexpected frame sequence in {directory}: "
                f"expected {expected_name}, found {frame.name}"
            )
    with frames[0].open("rb") as handle:
        header = handle.read(24)
    if len(header) != 24 or header[:8] != PNG_HEADER:
        raise CisSplitError(f"invalid PNG header: {frames[0]}")
    width, height = struct.unpack(">II", header[16:24])
    if (width, height) != (expected_width, expected_height):
        raise CisSplitError(
            f"unexpected PNG dimensions in {frames[0]}: "
            f"{width}x{height}, expected {expected_width}x{expected_height}"
        )
    return len(frames)


def _commit_staged_outputs(
    staging: Path,
    output_root: Path,
    overwrite: bool,
) -> None:
    staged = {
        LEFT_DIR: staging / LEFT_DIR,
        RIGHT_DIR: staging / RIGHT_DIR,
    }
    final = {
        LEFT_DIR: output_root / LEFT_DIR,
        RIGHT_DIR: output_root / RIGHT_DIR,
    }
    backup_root = staging / ".previous"
    backups: list[tuple[Path, Path]] = []
    installed: list[Path] = []
    try:
        for name in (LEFT_DIR, RIGHT_DIR):
            target = final[name]
            if target.exists():
                if not overwrite:
                    raise CisSplitError(
                        f"output already exists: {target}; "
                        "use --overwrite to replace it"
                    )
                backup_root.mkdir(exist_ok=True)
                backup = backup_root / name
                os.replace(target, backup)
                backups.append((target, backup))
        for name in (LEFT_DIR, RIGHT_DIR):
            os.replace(staged[name], final[name])
            installed.append(final[name])
    except Exception:
        for target in reversed(installed):
            if target.exists():
                shutil.rmtree(target)
        for target, backup in reversed(backups):
            if backup.exists():
                os.replace(backup, target)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cis_split_png",
        description=(
            "Split a side-by-side CIS video into PNG image directories. "
            "The output contains images_rectified_left and "
            "images_rectified_right only."
        )
    )
    parser.add_argument(
        "-i",
        "--input",
        required=True,
        metavar="FILE_OR_SESSION",
        help=(
            "Input CIS video, or a sensor_studio session directory containing "
            "cis/stereo.mkv"
        ),
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        required=True,
        metavar="DIR",
        help="Output directory for the two PNG image directories",
    )
    parser.add_argument(
        "--split-x",
        type=int,
        default=None,
        metavar="PIXEL",
        help="Horizontal split position; default: half of the video width",
    )
    parser.add_argument(
        "--swap",
        action="store_true",
        help="Write the first half as right and the second half as left",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing images_rectified_left/right directories",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate input and print the operation without writing files",
    )
    return parser


def _ffmpeg_command(
    info: VideoInfo,
    split_x: int,
    left_width: int,
    right_width: int,
    left_output: Path,
    right_output: Path,
    swap: bool,
) -> list[str]:
    graph = (
        "[0:v]split=2[first_src][second_src];"
        f"[first_src]crop={left_width}:{info.height}:0:0[first];"
        f"[second_src]crop={right_width}:{info.height}:{split_x}:0[second]"
    )
    first_map = "[second]" if swap else "[first]"
    second_map = "[first]" if swap else "[second]"
    return [
        _tool("ffmpeg"),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-stats",
        "-i",
        str(info.path),
        "-filter_complex",
        graph,
        "-map",
        first_map,
        "-fps_mode",
        "passthrough",
        "-pix_fmt",
        "rgb24",
        "-c:v",
        "png",
        "-start_number",
        str(FRAME_START),
        str(left_output / "%06d.png"),
        "-map",
        second_map,
        "-fps_mode",
        "passthrough",
        "-pix_fmt",
        "rgb24",
        "-c:v",
        "png",
        "-start_number",
        str(FRAME_START),
        str(right_output / "%06d.png"),
    ]


def run(args: argparse.Namespace) -> None:
    source = resolve_input(args.input)
    output_root = Path(args.output_dir).expanduser().resolve()
    info = probe_video(source)
    split_x, right_width = _split_geometry(info, args.split_x)
    left_width = split_x
    _ensure_output_is_available(output_root, args.overwrite)

    first_side = "right" if args.swap else "left"
    second_side = "left" if args.swap else "right"
    print(f"Input:  {info.path}")
    print(f"Video:  {info.width}x{info.height}")
    print(
        f"First half -> {first_side}: x=0..{split_x - 1} "
        f"({left_width}x{info.height})"
    )
    print(
        f"Second half -> {second_side}: x={split_x}..{info.width - 1} "
        f"({right_width}x{info.height})"
    )
    print(f"Output: {output_root}")
    print(f"Layout: {LEFT_DIR}/ and {RIGHT_DIR}/, starting at 000000.png")
    if args.dry_run:
        print("Dry run: no files written")
        return

    output_root.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".cis_split_png_", dir=output_root)
    )
    try:
        staged_left = staging / LEFT_DIR
        staged_right = staging / RIGHT_DIR
        staged_left.mkdir()
        staged_right.mkdir()
        command = _ffmpeg_command(
            info=info,
            split_x=split_x,
            left_width=left_width,
            right_width=right_width,
            left_output=staged_left,
            right_output=staged_right,
            swap=args.swap,
        )
        result = subprocess.run(command, check=False)
        if result.returncode != 0:
            raise CisSplitError(
                f"ffmpeg failed with exit code {result.returncode}"
            )
        expected_left_width = right_width if args.swap else left_width
        expected_right_width = left_width if args.swap else right_width
        left_count = _validate_frames(
            staged_left, expected_left_width, info.height
        )
        right_count = _validate_frames(
            staged_right, expected_right_width, info.height
        )
        if left_count != right_count:
            raise CisSplitError(
                f"left/right frame count mismatch: {left_count} vs {right_count}"
            )
        _commit_staged_outputs(staging, output_root, args.overwrite)
        print(
            f"Done: {left_count} left/right PNG pairs written "
            f"({left_count * 2} images)"
        )
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        run(parser.parse_args(argv))
    except (CisSplitError, OSError) as exc:
        print(f"cis_split_png: error: {exc}", file=sys.stderr)
        return 1
    return 0


def main_entry() -> None:
    raise SystemExit(main())
