from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import shutil
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .config import Settings
from .downloader import DownloadRejected, Downloader
from .quota import QuotaExceeded, QuotaStore
from .security import UnsafeUrl, canonicalize_media_url, user_key


class DownloadRequest(BaseModel):
    url: str = Field(min_length=10, max_length=2048)
    extract_text: bool = False


@dataclass
class Job:
    id: str
    owner: str
    status: str
    created_at: float
    title: str | None = None
    duration: int | None = None
    size: int | None = None
    error: str | None = None
    file: Path | None = None
    transcript: Path | None = None
    transcript_language: str | None = None
    transcript_error: str | None = None

    def public(self) -> dict:
        data = asdict(self)
        data.pop("owner")
        data.pop("file")
        data.pop("transcript")
        data["download_url"] = f"/api/v1/jobs/{self.id}/file" if self.status == "ready" else None
        data["transcript_url"] = (
            f"/api/v1/jobs/{self.id}/transcript" if self.status == "ready" and self.transcript else None
        )
        return data


def create_app(settings: Settings | None = None, downloader: Downloader | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    root = settings.data_dir
    jobs_dir = root / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    quota = QuotaStore(root / "quota.sqlite3", settings.max_jobs_per_hour, settings.max_bytes_per_day)
    worker = downloader or Downloader(settings)
    jobs: dict[str, Job] = {}
    active_users: set[str] = set()
    lock = asyncio.Lock()
    global_slots = asyncio.Semaphore(settings.max_global_jobs)
    app = FastAPI(title="Сохранение видео YouTube и Instagram", version="1.2")

    def save_job(job: Job) -> None:
        directory = jobs_dir / job.id
        if not directory.is_dir():
            return
        data = asdict(job)
        data["file"] = job.file.name if job.file else None
        data["transcript"] = job.transcript.name if job.transcript else None
        temporary = directory / ".job.json.tmp"
        manifest = directory / ".job.json"
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, manifest)

    def restore_ready_jobs() -> None:
        cutoff = time.time() - settings.file_ttl_seconds
        for directory in jobs_dir.iterdir():
            manifest = directory / ".job.json"
            if not directory.is_dir() or not manifest.is_file():
                continue
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                job_id = str(data["id"])
                if directory.name != job_id or not re.fullmatch(r"[0-9a-f]{32}", job_id):
                    continue
                if data.get("status") != "ready" or float(data["created_at"]) < cutoff:
                    continue
                file_name = data.get("file")
                transcript_name = data.get("transcript")
                if not file_name or Path(file_name).name != file_name:
                    continue
                file_path = directory / file_name
                if not file_path.is_file():
                    continue
                transcript_path = None
                if transcript_name and Path(transcript_name).name == transcript_name:
                    candidate = directory / transcript_name
                    transcript_path = candidate if candidate.is_file() else None
                jobs[job_id] = Job(
                    id=job_id,
                    owner=str(data["owner"]),
                    status="ready",
                    created_at=float(data["created_at"]),
                    title=data.get("title"),
                    duration=data.get("duration"),
                    size=file_path.stat().st_size,
                    error=data.get("error"),
                    file=file_path,
                    transcript=transcript_path,
                    transcript_language=data.get("transcript_language"),
                    transcript_error=data.get("transcript_error"),
                )
            except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
                continue

    restore_ready_jobs()

    def authenticate(
        authorization: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
    ) -> str:
        expected = f"Bearer {settings.api_key}"
        if not authorization or not hmac.compare_digest(authorization, expected):
            raise HTTPException(401, "Неверный ключ внутреннего API")
        try:
            return user_key(x_user_id or "")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    def cleanup_expired() -> None:
        cutoff = time.time() - settings.file_ttl_seconds
        for directory in jobs_dir.iterdir():
            if directory.is_dir() and directory.stat().st_mtime < cutoff:
                shutil.rmtree(directory, ignore_errors=True)
        for job_id, job in list(jobs.items()):
            if job.created_at < cutoff and job.status != "running":
                jobs.pop(job_id, None)

    async def perform(job: Job, url: str, reservation: int, extract_text: bool) -> None:
        job_dir = jobs_dir / job.id
        try:
            async with global_slots:
                job.status = "running"
                save_job(job)
                info = await worker.inspect(url)
                job.title, job.duration = info.title, info.duration
                if info.estimated_bytes:
                    quota.ensure_capacity(reservation, info.estimated_bytes)
                result = await worker.download(url, job_dir, uuid.uuid4().hex)
                job.file = result
                job.size = result.stat().st_size
                quota.complete(reservation, job.size)
                if extract_text:
                    job.status = "transcribing"
                    save_job(job)
                    try:
                        transcript = await worker.transcribe(result, job_dir, result.stem)
                        job.transcript = transcript.path
                        job.transcript_language = transcript.language
                    except DownloadRejected as exc:
                        job.transcript_error = str(exc)
                    except Exception:
                        job.transcript_error = "Не удалось извлечь текст из речи"
                job.status = "ready"
                save_job(job)
        except (DownloadRejected, QuotaExceeded) as exc:
            job.status = "failed"
            job.error = str(exc)
            shutil.rmtree(job_dir, ignore_errors=True)
        except Exception:
            job.status = "failed"
            job.error = "Внутренняя ошибка обработки"
            shutil.rmtree(job_dir, ignore_errors=True)
        finally:
            async with lock:
                active_users.discard(job.owner)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "free_bytes": shutil.disk_usage(root).free}

    @app.post("/api/v1/jobs", status_code=202)
    async def create_job(payload: DownloadRequest, background: BackgroundTasks, owner: str = Depends(authenticate)) -> dict:
        cleanup_expired()
        try:
            url = canonicalize_media_url(payload.url)
        except UnsafeUrl as exc:
            raise HTTPException(400, str(exc)) from exc
        async with lock:
            if owner in active_users:
                raise HTTPException(429, "У пользователя уже есть активное задание")
            try:
                reservation = quota.reserve(owner)
            except QuotaExceeded as exc:
                raise HTTPException(429, str(exc)) from exc
            active_users.add(owner)
        job_id = uuid.uuid4().hex
        job_dir = jobs_dir / job_id
        job_dir.mkdir(mode=0o700)
        job = Job(job_id, owner, "queued", time.time())
        jobs[job_id] = job
        save_job(job)
        background.add_task(perform, job, url, reservation, payload.extract_text)
        return job.public()

    def owned_job(job_id: str, owner: str) -> Job:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise HTTPException(404, "Задание не найдено")
        job = jobs.get(job_id)
        if not job or job.owner != owner:
            raise HTTPException(404, "Задание не найдено")
        return job

    @app.get("/api/v1/jobs/{job_id}")
    async def get_job(job_id: str, owner: str = Depends(authenticate)) -> dict:
        cleanup_expired()
        return owned_job(job_id, owner).public()

    @app.get("/api/v1/jobs/{job_id}/file")
    async def get_file(job_id: str, request: Request, owner: str = Depends(authenticate)) -> FileResponse:
        cleanup_expired()
        job = owned_job(job_id, owner)
        if job.status != "ready" or not job.file or not job.file.is_file():
            raise HTTPException(409, "Файл ещё не готов")
        suffix = job.file.suffix.lower()
        title = re.sub(r"[^\w .()-]+", "_", job.title or "video", flags=re.UNICODE).strip()[:100] or "video"
        return FileResponse(job.file, filename=f"{title}{suffix}", media_type="application/octet-stream")

    @app.get("/api/v1/jobs/{job_id}/transcript")
    async def get_transcript(job_id: str, owner: str = Depends(authenticate)) -> FileResponse:
        cleanup_expired()
        job = owned_job(job_id, owner)
        if job.status != "ready" or not job.transcript or not job.transcript.is_file():
            raise HTTPException(409, "Текст ещё не готов")
        title = re.sub(r"[^\w .()-]+", "_", job.title or "video", flags=re.UNICODE).strip()[:100] or "video"
        return FileResponse(
            job.transcript,
            filename=f"{title}-текст.txt",
            media_type="text/plain; charset=utf-8",
        )

    return app


app = create_app()
