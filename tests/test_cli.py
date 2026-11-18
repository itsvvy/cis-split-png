from __future__ import annotations

import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from cis_split_png.cli import (
    LEFT_DIR,
    RIGHT_DIR,
    main,
    probe_video,
)


class CisSplitTests(unittest.TestCase):
    @staticmethod
    def png_size(path: Path) -> tuple[int, int]:
        header = path.read_bytes()[:24]
        return struct.unpack(">II", header[16:24])

    def make_video(self, root: Path, *, width: int = 64, height: int = 32) -> Path:
        if shutil.which("ffmpeg") is None:
            self.skipTest("ffmpeg is not available")
        video = root / "stereo.mkv"
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size={width}x{height}:rate=2",
                "-frames:v",
                "3",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(video),
            ],
            check=True,
        )
        return video

    def test_split_creates_only_two_aligned_png_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = self.make_video(root)
            output = root / "converted"
            self.assertEqual(
                main(["-i", str(video), "-o", str(output)]),
                0,
            )
            self.assertEqual(
                sorted(path.name for path in output.iterdir()),
                [LEFT_DIR, RIGHT_DIR],
            )
            for directory in (LEFT_DIR, RIGHT_DIR):
                frames = sorted((output / directory).glob("*.png"))
                self.assertEqual(
                    [path.name for path in frames],
                    ["000000.png", "000001.png", "000002.png"],
                )
            info = probe_video(Path(video))
            self.assertEqual(info.width, 64)
            self.assertEqual(info.height, 32)

    def test_existing_output_requires_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = self.make_video(root)
            output = root / "converted"
            marker_dir = output / LEFT_DIR
            marker_dir.mkdir(parents=True)
            marker = marker_dir / "keep.txt"
            marker.write_text("keep")
            self.assertEqual(
                main(["-i", str(video), "-o", str(output)]),
                1,
            )
            self.assertTrue(marker.is_file())
            self.assertEqual(
                main(
                    [
                        "-i",
                        str(video),
                        "-o",
                        str(output),
                        "--overwrite",
                    ]
                ),
                0,
            )
            self.assertFalse(marker.exists())
            self.assertTrue((output / LEFT_DIR / "000000.png").is_file())
            self.assertTrue((output / RIGHT_DIR / "000000.png").is_file())

    def test_swap_and_custom_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = self.make_video(root)
            output = root / "converted"
            self.assertEqual(
                main(
                    [
                        "-i",
                        str(video),
                        "-o",
                        str(output),
                        "--split-x",
                        "40",
                        "--swap",
                    ]
                ),
                0,
            )
            self.assertEqual(
                self.png_size(output / LEFT_DIR / "000000.png"),
                (24, 32),
            )
            self.assertEqual(
                self.png_size(output / RIGHT_DIR / "000000.png"),
                (40, 32),
            )

    def test_session_directory_is_accepted_as_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "session"
            cis = session / "cis"
            cis.mkdir(parents=True)
            video = self.make_video(cis)
            self.assertEqual(
                main(
                    [
                        "-i",
                        str(session),
                        "-o",
                        str(Path(tmp) / "converted"),
                        "--dry-run",
                    ]
                ),
                0,
            )
            self.assertEqual(video.name, "stereo.mkv")


if __name__ == "__main__":
    unittest.main()
