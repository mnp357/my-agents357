from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _integer(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


@dataclass(frozen=True)
class Settings:
    api_key: str
    data_dir: Path
    max_duration_seconds: int
    max_file_bytes: int
    min_free_bytes: int
    download_timeout_seconds: int
    metadata_timeout_seconds: int
    file_ttl_seconds: int
    max_jobs_per_hour: int
    max_bytes_per_day: int
    max_global_jobs: int
    max_transcript_seconds: int = 7_200
    transcript_timeout_seconds: int = 3_600
    transcript_model: str = "small"
    transcript_cpu_threads: int = 4
    model_dir: Path = Path("./var/models")

    @classmethod
    def from_env(cls) -> "Settings":
        api_key = os.getenv("YTA_API_KEY", "")
        if len(api_key) < 24:
            raise RuntimeError("YTA_API_KEY must contain at least 24 characters")
        data_dir = Path(os.getenv("YTA_DATA_DIR", "./var")).resolve()
        return cls(
            api_key=api_key,
            data_dir=data_dir,
            max_duration_seconds=_integer("YTA_MAX_DURATION_SECONDS", 14_400),
            max_file_bytes=_integer("YTA_MAX_FILE_BYTES", 2 * 1024**3),
            min_free_bytes=_integer("YTA_MIN_FREE_BYTES", 3 * 1024**3),
            download_timeout_seconds=_integer("YTA_DOWNLOAD_TIMEOUT_SECONDS", 1_800),
            metadata_timeout_seconds=_integer("YTA_METADATA_TIMEOUT_SECONDS", 45),
            file_ttl_seconds=_integer("YTA_FILE_TTL_SECONDS", 3_600),
            max_jobs_per_hour=_integer("YTA_MAX_JOBS_PER_HOUR", 20),
            max_bytes_per_day=_integer("YTA_MAX_BYTES_PER_DAY", 4 * 1024**3),
            max_global_jobs=_integer("YTA_MAX_GLOBAL_JOBS", 2),
            max_transcript_seconds=_integer("YTA_MAX_TRANSCRIPT_SECONDS", 7_200),
            transcript_timeout_seconds=_integer("YTA_TRANSCRIPT_TIMEOUT_SECONDS", 3_600),
            transcript_model=os.getenv("YTA_TRANSCRIPT_MODEL", "small"),
            transcript_cpu_threads=_integer("YTA_TRANSCRIPT_CPU_THREADS", 4),
            model_dir=Path(os.getenv("YTA_MODEL_DIR", str(data_dir / "models"))).resolve(),
        )
