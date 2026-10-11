import asyncio
from pathlib import Path

import pytest

from youtube_agent.config import Settings
from youtube_agent.downloader import DownloadRejected, Downloader


def settings(tmp_path: Path) -> Settings:
    return Settings("x" * 24, tmp_path, 100, 1000, 1, 10, 10, 100, 2, 1000, 1)


class InspectFallbackDownloader(Downloader):
    def __init__(self, config):
        super().__init__(config)
        self.commands = []

    async def _run(self, command, timeout, watch_dir=None):
        self.commands.append(command)
        if len(self.commands) == 1:
            raise DownloadRejected("Sign in to confirm you're not a bot")
        return '{"title":"Allowed embed","duration":10,"filesize":5}', ""


def test_youtube_bot_check_retries_with_embedded_client(tmp_path):
    downloader = InspectFallbackDownloader(settings(tmp_path))
    info = asyncio.run(downloader.inspect("https://www.youtube.com/watch?v=BaW_jenozKc"))
    assert info.title == "Allowed embed"
    assert len(downloader.commands) == 2
    assert "youtube:player_client=web_embedded" in downloader.commands[1]


def test_instagram_does_not_use_youtube_fallback(tmp_path):
    downloader = InspectFallbackDownloader(settings(tmp_path))
    with pytest.raises(DownloadRejected):
        asyncio.run(downloader.inspect("https://www.instagram.com/reel/Chunk8-jurw/"))
    assert len(downloader.commands) == 1


def test_youtube_bot_check_is_shown_as_safe_russian_message(tmp_path):
    downloader = Downloader(settings(tmp_path))
    message = downloader._friendly_error(
        "ERROR: Sign in to confirm you're not a bot. Use --cookies-from-browser"
    )
    assert "адреса сервера" in message
    assert "cookies вашего Google-аккаунта агент не запрашивает" in message
    assert "--cookies" not in message
    assert downloader._is_youtube_bot_check(DownloadRejected(message))
