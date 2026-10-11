from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from youtube_agent.config import Settings
from youtube_agent.downloader import Downloader
from youtube_agent.security import canonicalize_youtube_url


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="yta-smoke-") as directory:
        root = Path(directory)
        settings = Settings(
            api_key="smoke-test-key-not-for-production",
            data_dir=root,
            max_duration_seconds=180,
            max_file_bytes=100 * 1024 * 1024,
            min_free_bytes=100 * 1024 * 1024,
            download_timeout_seconds=120,
            metadata_timeout_seconds=45,
            file_ttl_seconds=60,
            max_jobs_per_hour=1,
            max_bytes_per_day=100 * 1024 * 1024,
            max_global_jobs=1,
        )
        # The uploader marks this video CC BY in YouTube metadata.
        url = canonicalize_youtube_url("https://youtu.be/ptd1NN40vMw")
        downloader = Downloader(settings)
        info = await downloader.inspect(url)
        output = await downloader.download(url, root, "smoke")
        print({"title": info.title, "duration": info.duration, "bytes": output.stat().st_size, "suffix": output.suffix})


if __name__ == "__main__":
    if os.getenv("YTA_ALLOW_PUBLIC_SMOKE_TEST") != "yes":
        raise SystemExit("Set YTA_ALLOW_PUBLIC_SMOKE_TEST=yes to confirm this network test")
    asyncio.run(main())
