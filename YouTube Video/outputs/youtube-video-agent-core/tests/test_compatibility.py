import asyncio
from pathlib import Path

from youtube_agent.config import Settings
from youtube_agent.downloader import Downloader


def settings(tmp_path: Path) -> Settings:
    return Settings("x" * 24, tmp_path, 100, 1000, 1, 10, 10, 100, 2, 1000, 1)


class FakeCodecDownloader(Downloader):
    def __init__(self, config, original_codec):
        super().__init__(config)
        self.original_codec = original_codec
        self.commands = []

    async def _video_codec(self, path):
        return "h264" if "compatible" in path.name else self.original_codec

    async def _run(self, command, timeout, watch_dir=None):
        self.commands.append(command)
        Path(command[-1]).write_bytes(b"h264-video")
        return "", ""


def test_format_prefers_h264_before_other_codecs(tmp_path):
    selector = Downloader(settings(tmp_path))._format_selector()
    assert selector.startswith("bv*[vcodec^=avc1][ext=mp4]")
    assert selector.index("vcodec^=avc1") < selector.index("bv*[ext=mp4]")


def test_h264_mp4_is_not_transcoded(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"original")
    downloader = FakeCodecDownloader(settings(tmp_path), "h264")
    result = asyncio.run(downloader._ensure_compatible_video(source, tmp_path))
    assert result == source
    assert source.read_bytes() == b"original"
    assert downloader.commands == []


def test_vp9_mp4_is_replaced_with_h264(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"vp9-video")
    downloader = FakeCodecDownloader(settings(tmp_path), "vp9")
    result = asyncio.run(downloader._ensure_compatible_video(source, tmp_path))
    assert result == source
    assert source.read_bytes() == b"h264-video"
    assert downloader.commands and "libx264" in downloader.commands[0]
