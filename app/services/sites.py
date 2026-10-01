from functools import lru_cache
import subprocess
import threading

from yt_dlp.extractor import gen_extractor_classes
from app.services.yt_dlp_runtime import yt_dlp_runtime


_matches: dict[tuple[str, str], bool] = {}
_lock = threading.RLock()


@lru_cache(maxsize=1024)
def _image_has_site_extractor(url: str) -> bool:
    """Use the installed yt-dlp site's URL patterns, rather than a fixed allowlist."""
    return any(extractor.IE_NAME != "generic" and extractor.suitable(url)
               for extractor in gen_extractor_classes())


def prefetch(urls: list[str]) -> None:
    runtime = yt_dlp_runtime.active()
    if runtime.identity == "image":
        return
    with _lock:
        missing = list(dict.fromkeys(url for url in urls if (runtime.identity, url) not in _matches))
        if not missing:
            return
        try:
            values = yt_dlp_runtime.matches(runtime, missing)
        except (OSError, ValueError, subprocess.SubprocessError, RuntimeError):
            # A transient probe failure should not take down the queue UI or
            # permanently cache an incorrect answer.
            return
        if len(_matches) + len(missing) > 2048:
            _matches.clear()
        _matches.update(((runtime.identity, url), supported) for url, supported in zip(missing, values))


def has_site_extractor(url: str) -> bool:
    runtime = yt_dlp_runtime.active()
    if runtime.identity == "image":
        return _image_has_site_extractor(url)
    prefetch([url])
    with _lock:
        return _matches.get((runtime.identity, url), False)


def can_login(url: str, error: str = "") -> bool:
    if "unsupported url" in error.lower():
        return False
    # Generic supports embedded media on additional websites. Offer login when
    # it attempted extraction and didn't report an unsupported URL.
    return has_site_extractor(url) or "[generic]" in error.lower()
