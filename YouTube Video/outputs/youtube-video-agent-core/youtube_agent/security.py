from __future__ import annotations

import hashlib
import ipaddress
import re
from urllib.parse import parse_qs, urlsplit, urlunsplit


_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}
_INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}
_INSTAGRAM_CODE = re.compile(r"^[A-Za-z0-9_-]{5,64}$")


class UnsafeUrl(ValueError):
    pass


def canonicalize_media_url(raw: str) -> str:
    """Return a minimal supported-media URL, rejecting SSRF-shaped input."""
    if len(raw) > 2_048 or any(ord(char) < 32 for char in raw):
        raise UnsafeUrl("Некорректная ссылка")
    parsed = urlsplit(raw.strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise UnsafeUrl("Разрешены только HTTPS-ссылки YouTube")
    if parsed.username or parsed.password or parsed.port is not None:
        raise UnsafeUrl("Ссылка содержит запрещённые сетевые параметры")
    host = parsed.hostname.lower().rstrip(".")
    if host not in _YOUTUBE_HOSTS | _INSTAGRAM_HOSTS:
        raise UnsafeUrl("Разрешены только ссылки YouTube и Instagram")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise UnsafeUrl("IP-адрес вместо YouTube запрещён")

    if host in _INSTAGRAM_HOSTS:
        parts = parsed.path.strip("/").split("/")
        if len(parts) != 2 or parts[0] not in {"p", "reel", "reels", "tv"}:
            raise UnsafeUrl("Нужна ссылка на один Reel или видеопубликацию Instagram")
        code = parts[1]
        if not _INSTAGRAM_CODE.fullmatch(code):
            raise UnsafeUrl("Некорректная ссылка Instagram")
        kind = "reel" if parts[0] == "reels" else parts[0]
        return urlunsplit(("https", "www.instagram.com", f"/{kind}/{code}/", "", ""))

    video_id = ""
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/", 1)[0]
    elif parsed.path == "/watch":
        video_id = parse_qs(parsed.query).get("v", [""])[0]
    elif parsed.path.startswith(("/shorts/", "/live/", "/embed/")):
        parts = parsed.path.strip("/").split("/")
        video_id = parts[1] if len(parts) == 2 else ""
    if not _VIDEO_ID.fullmatch(video_id):
        raise UnsafeUrl("Нужна ссылка на один ролик YouTube, не канал и не плейлист")
    return urlunsplit(("https", "www.youtube.com", "/watch", f"v={video_id}", ""))


def canonicalize_youtube_url(raw: str) -> str:
    """Backward-compatible helper retained for integrations using the first release."""
    result = canonicalize_media_url(raw)
    if urlsplit(result).hostname != "www.youtube.com":
        raise UnsafeUrl("Нужна ссылка YouTube")
    return result


def user_key(value: str) -> str:
    if not value or len(value) > 200 or not re.fullmatch(r"[A-Za-z0-9_.:@+-]+", value):
        raise ValueError("Некорректный идентификатор пользователя")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
