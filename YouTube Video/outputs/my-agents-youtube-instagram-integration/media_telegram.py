"""Small, bounded Telegram file uploader for the media-saving page."""

import json
import re
import secrets
import urllib.error
import urllib.request


MAX_TELEGRAM_FILE_BYTES = 49_000_000


class TelegramUploadError(RuntimeError):
    pass


def safe_filename(value, fallback="file.bin"):
    raw = value or ""
    extension_match = re.search(r"(\.[A-Za-z0-9]{1,8})$", raw)
    extension = extension_match.group(1).lower() if extension_match else ""
    stem = raw[:-len(extension)] if extension else raw
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_-")[:100]
    if not stem:
        fallback_match = re.fullmatch(r"([A-Za-z0-9_-]+)(\.[A-Za-z0-9]{1,8})?", fallback)
        if not fallback_match:
            return "file" + (extension or ".bin")
        return fallback_match.group(1) + (extension or fallback_match.group(2) or "")
    return stem + extension


def multipart_document(chat_id, filename, content, mime_type, caption):
    if len(content) > MAX_TELEGRAM_FILE_BYTES:
        raise TelegramUploadError("Файл превышает лимит отправки Telegram")
    boundary = "----media-agent-" + secrets.token_hex(16)
    body = []
    for key, value in (("chat_id", str(chat_id)), ("caption", caption[:1024])):
        body.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
            + value.encode("utf-8") + b"\r\n"
        )
    body.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
        f'filename="{safe_filename(filename)}"\r\nContent-Type: {mime_type}\r\n\r\n'.encode()
    )
    body.extend((content, f"\r\n--{boundary}--\r\n".encode()))
    return boundary, b"".join(body)


def send_document(token, chat_id, filename, content, mime_type, caption):
    boundary, body = multipart_document(chat_id, filename, content, mime_type, caption)
    request = urllib.request.Request(
        "https://api.telegram.org/bot" + token + "/sendDocument",
        data=body,
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TelegramUploadError(
            "Не удалось подтвердить отправку в Telegram. Проверьте чат перед повтором."
        ) from exc
    if not result.get("ok"):
        description = str(result.get("description") or "Telegram отклонил файл")
        raise TelegramUploadError(description[:300])
    return result["result"]
