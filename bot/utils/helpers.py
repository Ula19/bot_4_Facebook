"""Утилиты для работы с Facebook URL"""
import re
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

# Паттерны URL Facebook-видео, Reels и Stories
FACEBOOK_VIDEO_PATTERNS = [
    # Обычное видео: facebook.com/watch?v=ID
    r"(?:https?://)?(?:www\.)?facebook\.com/watch\?(?:.*&)?v=\d+",
    # Видео в посте: facebook.com/username/videos/ID
    r"(?:https?://)?(?:www\.)?facebook\.com/[^/]+/videos/\d+",
    # Reels: facebook.com/reel/ID или /reels/ID
    r"(?:https?://)?(?:www\.)?facebook\.com/(?:[^/]+/)?reels?/\d+",
    # Короткие ссылки fb.watch
    r"(?:https?://)?fb\.watch/[a-zA-Z0-9_-]+",
    # Шеринг: facebook.com/share/r/ID (reels), /v/ID (video), /ID (общий)
    r"(?:https?://)?(?:www\.)?facebook\.com/share/(?:r/|v/)?[a-zA-Z0-9_-]+",
    # Stories: facebook.com/stories/USER_ID/...
    r"(?:https?://)?(?:www\.)?facebook\.com/stories/[^/]+(?:/[^/\s]+)?",
    # story.php: facebook.com/story.php?story_fbid=ID&id=ID
    r"(?:https?://)?(?:www\.)?facebook\.com/story\.php\?(?:.*&)?(?:story_fbid|id)=\d+",
    # Мобильный домен: m.facebook.com
    r"(?:https?://)?m\.facebook\.com/(?:watch\?(?:.*&)?v=\d+|[^/]+/videos/\d+|reels?/\d+|share/[^\s]+|stories/[^\s]+|story\.php\?[^\s]+)",
    # Ссылки через story_fbid/fbid
    r"(?:https?://)?(?:www\.)?facebook\.com/(?:photo|video|permalink)(?:\.php)?\?(?:.*&)?(?:story_fbid|fbid|v)=\d+",
]

_FB_PATTERN = re.compile(
    "|".join(f"(?:{p})" for p in FACEBOOK_VIDEO_PATTERNS),
    re.IGNORECASE,
)


def is_facebook_url(text: str) -> bool:
    """Проверяет, является ли текст ссылкой на Facebook-видео или Reel"""
    return bool(_FB_PATTERN.search(text.strip()))


def clean_facebook_url(url: str) -> str:
    """Очищает Facebook URL от лишних параметров трекинга.
    Оставляет только необходимые параметры (v= для watch, fbid= и т.п.)
    """
    url = url.strip()

    # fb.watch — не трогаем, они короткие
    if "fb.watch" in url:
        return url

    try:
        parsed = urlparse(url)

        # параметры которые важны для идентификации контента
        important_params = {"v", "fbid", "story_fbid", "id"}

        query_params = parse_qs(parsed.query, keep_blank_values=False)
        filtered = {k: v for k, v in query_params.items() if k in important_params}

        clean_query = urlencode({k: v[0] for k, v in filtered.items()}, doseq=False)
        clean_parsed = parsed._replace(query=clean_query, fragment="")
        return urlunparse(clean_parsed)
    except Exception:
        # при ошибке возвращаем исходный URL
        return url


def extract_facebook_video_id(url: str) -> str | None:
    """Извлекает ID видео из Facebook URL (если возможно)"""
    url = url.strip()

    # watch?v=ID
    m = re.search(r"[?&]v=(\d+)", url)
    if m:
        return m.group(1)

    # /videos/ID
    m = re.search(r"/videos/(\d+)", url)
    if m:
        return m.group(1)

    # /reel/ID
    m = re.search(r"/reel/(\d+)", url)
    if m:
        return m.group(1)

    # fbid= или story_fbid=
    m = re.search(r"(?:fbid|story_fbid)=(\d+)", url)
    if m:
        return m.group(1)

    return None
