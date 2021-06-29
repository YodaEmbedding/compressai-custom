import abc
import argparse

from pathlib import Path
from typing import Any, List

from .rawvideo import get_raw_video_file_info


class Codec(abc.ABC):
    name = ""
    help = ""

    def add_parser_args(self, parser: argparse.ArgumentParser) -> None:
        # """Overridable. Add options for this sub-command to the
        # given `parser` instance.

        # Args:
        #     parser (argparse.ArgumentParser): Instance to add arguments to.
        # """
        pass

    @abc.abstractmethod
    def get_encode_cmd(self, filepath: Path, args) -> List[Any]:
        raise NotImplementedError


class H264(Codec):
    name = "h264"

    def add_parser_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("-p", "--preset", default="medium", help="preset")
        parser.add_argument("-q", "--qp", default=32, help="quality")

    def get_outputpath(self, filepath: Path, args) -> Path:
        return Path(args.output) / (
            f"{filepath.stem}_{self.name}_{args.preset}_qp{args.qp}.mp4"
        )

    def get_encode_cmd(self, filepath: Path, args) -> List[Any]:
        info = get_raw_video_file_info(filepath.stem)
        outputpath = self.get_outputpath(filepath, args)
        cmd = [
            "ffmpeg",
            "-s:v",
            f"{info['width']}x{info['height']}",
            "-i",
            filepath,
            "-c:v",
            "h264",
            "-crf",
            args.qp,
            "-preset",
            args.preset,
            "-bf",
            0,
            "-tune",
            "ssim",
            "-pix_fmt",
            "yuv420p",
            "-threads",
            "4",
            outputpath,
        ]
        return cmd


class H265(H264):
    name = "h265"

    def get_encode_cmd(self, filepath: Path, args) -> List[Any]:
        info = get_raw_video_file_info(filepath.stem)
        outputpath = self.get_outputpath(filepath, args)
        cmd = [
            "ffmpeg",
            "-s:v",
            f"{info['width']}x{info['height']}",
            "-i",
            filepath,
            "-c:v",
            "hevc",
            "-crf",
            args.qp,
            "-preset",
            args.preset,
            "-x265-params",
            "bframes=0",
            "-tune",
            "ssim",
            "-pix_fmt",
            "yuv420p",
            "-threads",
            "4",
            outputpath,
        ]
        return cmd
