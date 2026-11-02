"""YouTube's most-popular chart per country, as the outside trends source for content ideas (WA-10, SB-03).

For a region, the chart's current top videos are read (optionally within a YouTube category matching the topic
area), and the topics they share, from their tags and the hashtags in their titles, are scored by how many top
videos carry them and how high those videos rank. Uses the YouTube Data API key already set for artist lookups;
each chart costs 1 unit of the 10,000 free daily units, and each country's answer is kept for six hours.

YouTube is blocked in China and has no chart in some countries; those return nothing, and ideas fall back to
Wamiwa's own engagement and cultural moments.
"""
import logging
import re
import time

import httpx

from app.core.country_codes import code
from app.infra.providers.base import TrendGateway, TrendSignal

log = logging.getLogger("wamiwa.trends")

URL = "https://www.googleapis.com/youtube/v3/videos"
# Wamiwa niches -> YouTube video categories (https://developers.google.com/youtube/v3/docs/videoCategories).
CATEGORIES = {
    "music": "10", "sports": "17", "sport": "17", "gaming": "20", "comedy": "23", "entertainment": "24",
    "news": "25", "politics": "25", "fashion": "26", "beauty": "26", "lifestyle": "26", "food": "26",
    "education": "27", "tech": "28", "technology": "28", "science": "28", "film": "1", "movies": "1",
    "travel": "19", "cars": "2", "autos": "2", "pets": "15", "animals": "15",
}
STOPWORDS = {"official", "video", "music", "audio", "lyrics", "lyric", "visualizer", "live", "new", "full", "the",
             "and", "feat", "ft", "official video", "music video", "trailer", "episode", "shorts", "short", "hd"}
KEEP_SECONDS = 6 * 3600
TOP_TOPICS = 10


class YouTubeTrendGateway(TrendGateway):
    def __init__(self, api_key: str, *, timeout: float = 15.0, transport: httpx.AsyncBaseTransport | None = None):
        self._key = api_key
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)
        self._kept: dict[tuple[str, str | None], tuple[float, list[TrendSignal]]] = {}

    async def rising(self, region: str, category: str | None) -> list[TrendSignal]:
        country = code(region)
        if country is None or country == "CN":
            return []
        youtube_category = CATEGORIES.get((category or "").strip().lower())
        key = (country, youtube_category)
        kept = self._kept.get(key)
        if kept and time.monotonic() - kept[0] < KEEP_SECONDS:
            return kept[1]
        signals = await self._chart(country, youtube_category)
        if signals is None and youtube_category:  # no chart for that category there: try the whole chart
            signals = await self._chart(country, None)
        signals = signals or []
        self._kept[key] = (time.monotonic(), signals)
        return signals

    async def _chart(self, country: str, category: str | None) -> list[TrendSignal] | None:
        params = {"part": "snippet", "chart": "mostPopular", "regionCode": country, "maxResults": 50, "key": self._key}
        if category:
            params["videoCategoryId"] = category
        try:
            response = await self._client.get(URL, params=params)
        except httpx.HTTPError as exc:
            log.warning("YouTube chart for %s unavailable: %s", country, exc)
            return None
        if response.status_code != 200:
            # 400/404: no chart for that country or category; 403: quota or key problem (logged for the admins).
            log.log(logging.ERROR if response.status_code == 403 else logging.INFO,
                    "YouTube chart for %s/%s: HTTP %s", country, category, response.status_code)
            return None
        videos = response.json().get("items") or []
        if not videos:
            return None
        scores: dict[str, float] = {}
        for rank, video in enumerate(videos):
            snippet = video.get("snippet") or {}
            weight = 1.0 - rank / (len(videos) + 1)  # higher in the chart counts more
            topics = {t.strip().lower() for t in snippet.get("tags") or []}
            topics |= {h.lower() for h in re.findall(r"#(\w{3,30})", snippet.get("title") or "")}
            for topic in topics:
                if 3 <= len(topic) <= 40 and topic not in STOPWORDS and not topic.isdigit():
                    scores[topic] = scores.get(topic, 0.0) + weight
        # A topic only one video carries is that video, not a trend.
        shared = {t: s for t, s in scores.items() if s >= 1.0}
        top = max(shared.values(), default=0.0)
        ranked = sorted(shared.items(), key=lambda item: item[1], reverse=True)[:TOP_TOPICS]
        return [TrendSignal(topic, round(score / top, 2), "YouTube's most-popular chart") for topic, score in ranked]
