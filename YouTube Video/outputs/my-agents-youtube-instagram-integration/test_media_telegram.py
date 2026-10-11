import pytest

from media_telegram import MAX_TELEGRAM_FILE_BYTES, TelegramUploadError, multipart_document, safe_filename


def test_safe_filename_removes_header_characters():
    result = safe_filename('../../Тест\r\n".mp4')
    assert result.endswith(".mp4")
    assert "/" not in result and "\\" not in result and "\r" not in result and "\n" not in result


def test_multipart_contains_file_and_caption():
    boundary, body = multipart_document(12, "clip.mp4", b"video", "video/mp4", "Готово")
    assert boundary.encode() in body
    assert b'filename="clip.mp4"' in body
    assert "Готово".encode() in body
    assert b"video" in body


def test_rejects_file_over_telegram_limit():
    oversized = b"x" * (MAX_TELEGRAM_FILE_BYTES + 1)
    with pytest.raises(TelegramUploadError, match="лимит"):
        multipart_document(12, "clip.mp4", oversized, "video/mp4", "Готово")
