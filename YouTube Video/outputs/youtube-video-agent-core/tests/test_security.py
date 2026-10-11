import pytest

from youtube_agent.security import UnsafeUrl, canonicalize_media_url, canonicalize_youtube_url, user_key


@pytest.mark.parametrize("url", [
    "https://youtu.be/BaW_jenozKc",
    "https://www.youtube.com/watch?v=BaW_jenozKc&list=ignored",
    "https://youtube.com/shorts/BaW_jenozKc",
])
def test_accepts_video_urls(url):
    assert canonicalize_youtube_url(url) == "https://www.youtube.com/watch?v=BaW_jenozKc"


@pytest.mark.parametrize(("url", "expected"), [
    ("https://instagram.com/reel/Chunk8-jurw/?igsh=ignored", "https://www.instagram.com/reel/Chunk8-jurw/"),
    ("https://www.instagram.com/reels/Chunk8-jurw/", "https://www.instagram.com/reel/Chunk8-jurw/"),
    ("https://m.instagram.com/p/BQ0eAlwhDrw/", "https://www.instagram.com/p/BQ0eAlwhDrw/"),
    ("https://www.instagram.com/tv/BkfuX9UB-eK/", "https://www.instagram.com/tv/BkfuX9UB-eK/"),
])
def test_accepts_direct_instagram_video_urls(url, expected):
    assert canonicalize_media_url(url) == expected


@pytest.mark.parametrize("url", [
    "http://youtube.com/watch?v=BaW_jenozKc",
    "https://youtube.com.evil.test/watch?v=BaW_jenozKc",
    "https://user:pass@youtube.com/watch?v=BaW_jenozKc",
    "https://youtube.com:443/watch?v=BaW_jenozKc",
    "https://youtube.com/playlist?list=abc",
    "https://127.0.0.1/watch?v=BaW_jenozKc",
    "https://instagram.com/accounts/login/",
    "https://instagram.com/some-user/reels/",
    "https://instagram.evil.test/reel/Chunk8-jurw/",
])
def test_rejects_unsafe_or_non_video_urls(url):
    with pytest.raises(UnsafeUrl):
        canonicalize_media_url(url)


def test_user_keys_are_stable_and_not_raw():
    assert user_key("user-42") == user_key("user-42")
    assert user_key("user-42") != "user-42"
