from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import Settings


class DownloadRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaInfo:
    title: str
    duration: int | None
    estimated_bytes: int | None


@dataclass(frozen=True)
class TranscriptInfo:
    path: Path
    language: str | None
    language_probability: float | None


def _estimated_size(info: dict[str, Any]) -> int | None:
    requested = info.get("requested_formats") or []
    values = [item.get("filesize") or item.get("filesize_approx") for item in requested]
    values = [int(value) for value in values if value]
    if values:
        return sum(values)
    value = info.get("filesize") or info.get("filesize_approx")
    return int(value) if value else None


class Downloader:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _base(self) -> list[str]:
        return [
            sys.executable,
            "-m",
            "yt_dlp",
            "--ignore-config",
            "--no-playlist",
            "--socket-timeout", "15",
            "--retries", "3",
            "--fragment-retries", "3",
        ]

    @staticmethod
    def _is_youtube(url: str) -> bool:
        return (urlsplit(url).hostname or "").lower() in {
            "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"
        }

    @staticmethod
    def _youtube_embedded_args() -> list[str]:
        # The logged-out embedded client does not need account cookies or a PO
        # token, but YouTube only exposes videos whose owner permits embedding.
        return ["--extractor-args", "youtube:player_client=web_embedded"]

    @staticmethod
    def _is_youtube_bot_check(error: BaseException) -> bool:
        message = str(error).lower()
        return (
            ("sign in to confirm you" in message and "not a bot" in message)
            or "адреса сервера как автоматический" in message
        )

    @staticmethod
    def _friendly_error(stderr: str) -> str:
        lowered = stderr.lower()
        if "sign in to confirm you" in lowered and "not a bot" in lowered:
            return (
                "YouTube временно отклонил запрос с адреса сервера как автоматический. "
                "Пароль или cookies вашего Google-аккаунта агент не запрашивает. "
                "Попробуйте позже или загрузите собственный файл напрямую."
            )
        return stderr

    @staticmethod
    def _format_selector() -> str:
        return (
            "bv*[vcodec^=avc1][ext=mp4]+ba[ext=m4a]/"
            "b[vcodec^=avc1][ext=mp4]/"
            "bv*[vcodec^=avc1]+ba/b[vcodec^=avc1]/"
            "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"
        )

    async def _run(self, command: list[str], timeout: int, watch_dir: Path | None = None) -> tuple[str, str]:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        communicate = asyncio.create_task(process.communicate())
        started = time.monotonic()
        try:
            while not communicate.done():
                if time.monotonic() - started > timeout:
                    raise DownloadRejected("Истекло время обработки ролика")
                if watch_dir:
                    used = sum(p.stat().st_size for p in watch_dir.glob("*") if p.is_file())
                    if used > int(self.settings.max_file_bytes * 2.2):
                        raise DownloadRejected("Временные файлы превысили допустимый размер")
                    if shutil.disk_usage(watch_dir).free < self.settings.min_free_bytes:
                        raise DownloadRejected("Недостаточно свободного места на сервере")
                try:
                    await asyncio.wait_for(asyncio.shield(communicate), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
            stdout, stderr = await communicate
        except BaseException:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
            if not communicate.done():
                communicate.cancel()
            await asyncio.gather(communicate, return_exceptions=True)
            raise
        out = stdout.decode("utf-8", "replace")
        err = stderr.decode("utf-8", "replace")
        if process.returncode:
            safe_last_line = next((line for line in reversed(err.splitlines()) if line.strip()), "Ошибка загрузки")
            raise DownloadRejected(self._friendly_error(safe_last_line)[:500])
        return out, err

    async def inspect(self, url: str) -> MediaInfo:
        arguments = [
            "--skip-download",
            "--dump-single-json",
            "--no-warnings",
            "--format", self._format_selector(),
            url,
        ]
        try:
            stdout, _ = await self._run(self._base() + arguments, self.settings.metadata_timeout_seconds)
        except DownloadRejected as exc:
            if not (self._is_youtube(url) and self._is_youtube_bot_check(exc)):
                raise
            stdout, _ = await self._run(
                self._base() + self._youtube_embedded_args() + arguments,
                self.settings.metadata_timeout_seconds,
            )
        try:
            info = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise DownloadRejected("Не удалось прочитать сведения о ролике") from exc
        if info.get("_type") in {"playlist", "multi_video"}:
            raise DownloadRejected("Плейлисты и составные публикации не поддерживаются")
        if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
            raise DownloadRejected("Прямые эфиры и ожидающие публикации не поддерживаются")
        raw_duration = info.get("duration")
        duration = int(raw_duration) if raw_duration else None
        if duration is None and "instagram.com" not in url:
            raise DownloadRejected("Не удалось определить длительность ролика")
        if duration and duration > self.settings.max_duration_seconds:
            raise DownloadRejected("Ролик превышает допустимую длительность")
        size = _estimated_size(info)
        if size and size > self.settings.max_file_bytes:
            raise DownloadRejected("Ролик превышает допустимый размер")
        return MediaInfo(str(info.get("title") or "video")[:200], duration, size)

    async def _verify_final_duration(self, path: Path) -> int:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            raise DownloadRejected("На сервере отсутствует ffprobe для проверки длительности")
        stdout, _ = await self._run([
            ffprobe, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ], 30)
        try:
            duration = max(1, int(float(stdout.strip())))
        except (TypeError, ValueError) as exc:
            raise DownloadRejected("Не удалось проверить длительность готового файла") from exc
        if duration > self.settings.max_duration_seconds:
            raise DownloadRejected("Ролик превышает допустимую длительность")
        return duration

    async def _video_codec(self, path: Path) -> str:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            raise DownloadRejected("На сервере отсутствует ffprobe для проверки видео")
        stdout, _ = await self._run([
            ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name", "-of", "json", str(path),
        ], 30)
        try:
            streams = json.loads(stdout).get("streams") or []
            return str(streams[0]["codec_name"]).lower()
        except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise DownloadRejected("В готовом файле не найдена видеодорожка") from exc

    async def _ensure_compatible_video(self, path: Path, target_dir: Path) -> Path:
        codec = await self._video_codec(path)
        if codec == "h264" and path.suffix.lower() == ".mp4":
            return path
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise DownloadRejected("На сервере отсутствует ffmpeg для совместимого видео")
        temporary = target_dir / f"{path.stem}-compatible.tmp.mp4"
        final = target_dir / f"{path.stem}.mp4"
        command = [
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-profile:a", "aac_low",
            "-b:a", "160k", "-movflags", "+faststart", str(temporary),
        ]
        try:
            await self._run(command, self.settings.download_timeout_seconds, target_dir)
            if not temporary.is_file() or temporary.stat().st_size > self.settings.max_file_bytes:
                raise DownloadRejected("Совместимый видеофайл превышает допустимый размер")
            if await self._video_codec(temporary) != "h264":
                raise DownloadRejected("Не удалось подготовить совместимую видеодорожку")
            if final != path:
                path.unlink(missing_ok=True)
            os.replace(temporary, final)
            return final
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    async def download(self, url: str, target_dir: Path, stem: str) -> Path:
        if shutil.disk_usage(target_dir).free < self.settings.min_free_bytes:
            raise DownloadRejected("Недостаточно свободного места на сервере")
        output = str(target_dir / f"{stem}.%(ext)s")
        arguments = [
            "--no-progress",
            "--max-filesize", str(self.settings.max_file_bytes),
            "--format", self._format_selector(),
            "--merge-output-format", "mp4",
            "--output", output,
            "--print", "after_move:filepath",
            url,
        ]
        try:
            stdout, _ = await self._run(
                self._base() + arguments, self.settings.download_timeout_seconds, target_dir)
        except DownloadRejected as exc:
            if not (self._is_youtube(url) and self._is_youtube_bot_check(exc)):
                raise
            stdout, _ = await self._run(
                self._base() + self._youtube_embedded_args() + arguments,
                self.settings.download_timeout_seconds,
                target_dir,
            )
        candidates = [Path(line.strip()) for line in stdout.splitlines() if line.strip()]
        result = next((path for path in reversed(candidates) if path.exists()), None)
        if not result or result.parent != target_dir:
            raise DownloadRejected("Готовый файл не найден")
        if result.stat().st_size > self.settings.max_file_bytes:
            result.unlink(missing_ok=True)
            raise DownloadRejected("Готовый файл превышает допустимый размер")
        try:
            result = await self._ensure_compatible_video(result, target_dir)
            await self._verify_final_duration(result)
        except DownloadRejected:
            result.unlink(missing_ok=True)
            raise
        return result

    async def transcribe(self, media: Path, target_dir: Path, stem: str) -> TranscriptInfo:
        duration = await self._verify_final_duration(media)
        if duration > self.settings.max_transcript_seconds:
            raise DownloadRejected("Ролик слишком длинный для извлечения текста")

        self.settings.model_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        output = target_dir / f"{stem}-transcript.txt"
        metadata = target_dir / f"{stem}-transcript.json"
        command = [
            sys.executable,
            "-m",
            "youtube_agent.transcribe_worker",
            str(media),
            str(output),
            str(metadata),
            self.settings.transcript_model,
            str(self.settings.model_dir),
            str(self.settings.transcript_cpu_threads),
        ]
        await self._run(command, self.settings.transcript_timeout_seconds, target_dir)
        if not output.is_file() or output.parent != target_dir or not metadata.is_file():
            raise DownloadRejected("Не удалось создать файл с текстом")
        try:
            details = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            output.unlink(missing_ok=True)
            raise DownloadRejected("Не удалось проверить файл с текстом") from exc
        finally:
            metadata.unlink(missing_ok=True)
        return TranscriptInfo(
            path=output,
            language=details.get("language"),
            language_probability=details.get("language_probability"),
        )
