from pathlib import Path

from fastapi.testclient import TestClient

from youtube_agent.app import create_app
from youtube_agent.config import Settings
from youtube_agent.downloader import MediaInfo, TranscriptInfo


class FakeDownloader:
    async def inspect(self, url):
        return MediaInfo("Test video", 10, 5)

    async def download(self, url, target_dir, stem):
        path = target_dir / f"{stem}.mp4"
        path.write_bytes(b"video")
        return path

    async def transcribe(self, media, target_dir, stem):
        path = target_dir / f"{stem}-transcript.txt"
        path.write_text("Текст из видео\n\n[00:00:00–00:00:02] Пример речи\n", encoding="utf-8")
        return TranscriptInfo(path, "ru", 0.99)


def settings(tmp_path: Path) -> Settings:
    return Settings("x" * 24, tmp_path, 100, 1000, 1, 10, 10, 100, 2, 1000, 1)


def headers(user="alice"):
    return {"Authorization": f"Bearer {'x' * 24}", "X-User-ID": user}


def test_create_poll_and_download(tmp_path):
    with TestClient(create_app(settings(tmp_path), FakeDownloader())) as client:
        created = client.post("/api/v1/jobs", json={"url": "https://youtu.be/BaW_jenozKc"}, headers=headers())
        assert created.status_code == 202
        job_id = created.json()["id"]
        status = client.get(f"/api/v1/jobs/{job_id}", headers=headers())
        assert status.json()["status"] == "ready"
        response = client.get(f"/api/v1/jobs/{job_id}/file", headers=headers())
        assert response.status_code == 200
        assert response.content == b"video"


def test_requires_auth_and_valid_youtube_url(tmp_path):
    with TestClient(create_app(settings(tmp_path), FakeDownloader())) as client:
        assert client.post("/api/v1/jobs", json={"url": "https://youtu.be/BaW_jenozKc"}).status_code == 401
        response = client.post("/api/v1/jobs", json={"url": "https://example.com/video"}, headers=headers())
        assert response.status_code == 400


def test_accepts_instagram_reel(tmp_path):
    with TestClient(create_app(settings(tmp_path), FakeDownloader())) as client:
        response = client.post(
            "/api/v1/jobs", json={"url": "https://www.instagram.com/reel/Chunk8-jurw/"}, headers=headers("bob")
        )
        assert response.status_code == 202
        assert client.get(f"/api/v1/jobs/{response.json()['id']}", headers=headers("bob")).json()["status"] == "ready"


def test_extracts_and_downloads_transcript(tmp_path):
    with TestClient(create_app(settings(tmp_path), FakeDownloader())) as client:
        created = client.post(
            "/api/v1/jobs",
            json={"url": "https://youtu.be/BaW_jenozKc", "extract_text": True},
            headers=headers("carol"),
        )
        assert created.status_code == 202
        job_id = created.json()["id"]
        status = client.get(f"/api/v1/jobs/{job_id}", headers=headers("carol")).json()
        assert status["status"] == "ready"
        assert status["transcript_language"] == "ru"
        assert status["transcript_url"].endswith("/transcript")
        response = client.get(f"/api/v1/jobs/{job_id}/transcript", headers=headers("carol"))
        assert response.status_code == 200
        assert "Пример речи" in response.content.decode("utf-8")


def test_ready_job_survives_service_restart(tmp_path):
    config = settings(tmp_path)
    with TestClient(create_app(config, FakeDownloader())) as client:
        created = client.post(
            "/api/v1/jobs",
            json={"url": "https://youtu.be/BaW_jenozKc", "extract_text": True},
            headers=headers("restart-user"),
        )
        job_id = created.json()["id"]
        assert client.get(f"/api/v1/jobs/{job_id}", headers=headers("restart-user")).json()["status"] == "ready"

    with TestClient(create_app(config, FakeDownloader())) as restarted:
        status = restarted.get(f"/api/v1/jobs/{job_id}", headers=headers("restart-user"))
        assert status.status_code == 200
        assert status.json()["transcript_url"].endswith("/transcript")
        assert restarted.get(f"/api/v1/jobs/{job_id}/file", headers=headers("restart-user")).content == b"video"
