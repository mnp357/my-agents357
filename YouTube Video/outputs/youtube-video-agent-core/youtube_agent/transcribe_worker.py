from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

def _timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def main() -> int:
    if len(sys.argv) != 8:
        raise SystemExit("usage: transcribe_worker SOURCE OUTPUT METADATA MODEL MODEL_DIR CPU_THREADS PROGRESS")
    source, output_raw, metadata_raw, model_name, model_dir, cpu_threads, progress_raw = sys.argv[1:]
    output = Path(output_raw)
    metadata = Path(metadata_raw)
    progress_path = Path(progress_raw)
    os.environ.setdefault("HF_HOME", str(Path(model_dir) / "huggingface"))
    os.environ.setdefault("XDG_CACHE_HOME", str(Path(model_dir) / "cache"))
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    from faster_whisper import WhisperModel

    model = WhisperModel(
        model_name,
        device="cpu",
        compute_type="int8",
        cpu_threads=int(cpu_threads),
        num_workers=1,
        download_root=model_dir,
    )
    segments, info = model.transcribe(
        source,
        beam_size=5,
        vad_filter=True,
        word_timestamps=False,
        condition_on_previous_text=True,
    )
    lines = []
    last_progress_write = 0.0
    duration = float(getattr(info, "duration", 0) or 0)
    for segment in segments:
        speech = segment.text.strip()
        if speech:
            lines.append(f"[{_timestamp(segment.start)}–{_timestamp(segment.end)}] {speech}")
        now = time.monotonic()
        if now - last_progress_write >= 2:
            progress = min(1.0, max(0.0, float(segment.end) / duration)) if duration else 0.0
            temporary_progress = progress_path.with_suffix(progress_path.suffix + ".tmp")
            try:
                temporary_progress.write_text(json.dumps({"progress": progress}), encoding="utf-8")
                os.replace(temporary_progress, progress_path)
            except OSError:
                temporary_progress.unlink(missing_ok=True)
            last_progress_write = now
    if not lines:
        raise RuntimeError("В ролике не удалось распознать речь")

    language = getattr(info, "language", None)
    probability = getattr(info, "language_probability", None)
    heading = ["Текст из видео"]
    if language:
        heading.append(f"Язык: {language}")
    heading.append("")
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text("\n".join(heading + lines) + "\n", encoding="utf-8")
    os.replace(temporary, output)
    metadata.write_text(
        json.dumps({"language": language, "language_probability": probability}),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
