#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hdrezka_core.py — API/парсинг HDrezka для GUI.

Только requests + стандартная библиотека: никакого браузера/Playwright.

Цепочка работы
--------------
1. GET / — сайт может отдать страницу Anubis (proof-of-work). Скрипт
   решает SHA-256-задачу и получает cookie сессии.
2. GET /engine/ajax/search.php?q=... или раздел каталога — список тайтлов.
3. GET страницы тайтла — post_id, переводы, сезоны, серии, постер.
4. POST /ajax/get_cdn_series/ — JSON с полем url.
5. url = "[360p]https://...mp4:hls:manifest.m3u8 or https://...mp4,..."

HLS-плейлист у HDrezka — обычный VOD media-playlist (без master и без
#EXT-X-KEY). Для каждого качества API отдаёт две альтернативы: HLS и MP4.
"""

from __future__ import annotations

import base64
import hashlib
import html as html_module
import itertools
import json
import re
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import requests

requests.packages.urllib3.disable_warnings()

DEFAULT_BASE = "https://hdrezka.me"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HTTP_RETRIES = 2

KIND_LABELS = {
    "films": "Фильм",
    "series": "Сериал",
    "cartoons": "Мультфильм",
    "anime": "Аниме",
    "animation": "Аниме",
    "show": "ТВ-шоу",
    "doc": "Документальный",
}
KIND_ORDER = [
    "Сериал", "Фильм", "Мультфильм", "Аниме", "ТВ-шоу",
    "Документальный", "Другое",
]

# Ключ раздела в GUI -> путь на сайте. У аниме каталог и жанры
# живут под /animation/, хотя /anime/ тоже открывается.
SECTION_PATHS = {
    "films": "films",
    "series": "series",
    "cartoons": "cartoons",
    "anime": "animation",
    "show": "show",
    "home": "",
}

# Эти пункты меню — не жанры, а язык/происхождение.
GENRE_EXCLUDE = {"russian", "ukrainian", "foreign"}

CATALOG_SECTIONS = [
    ("home", "Новинки"),
    ("films", "Фильмы"),
    ("series", "Сериалы"),
    ("cartoons", "Мультфильмы"),
    ("anime", "Аниме"),
    ("show", "ТВ-шоу"),
]


class HDrezkaError(RuntimeError):
    """Ошибка API/сети/разбора, которую можно показать пользователю."""


# --------------------------------------------------------------------------
# Модели
# --------------------------------------------------------------------------

@dataclass
class SearchItem:
    title: str
    url: str
    kind: str = ""
    year: str = ""
    meta: str = ""
    rating: str = ""
    poster: str = ""
    info: str = ""
    genres: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SearchItem":
        keys = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in (data or {}).items() if k in keys})


@dataclass
class Translator:
    id: str
    name: str
    premium: bool = False
    original: bool = False


@dataclass
class Episode:
    season_id: str
    episode_id: str
    name: str
    premium: bool = False


@dataclass
class TitleInfo:
    post_id: str
    url: str
    title: str
    original_title: str = ""
    description: str = ""
    poster: str = ""
    kind: str = ""
    year: str = ""
    country: str = ""
    genres: str = ""
    rating: str = ""
    duration: str = ""
    quality: str = ""
    info: Dict[str, str] = field(default_factory=dict)
    translators: List[Translator] = field(default_factory=list)
    seasons: Dict[str, str] = field(default_factory=dict)
    episodes: Dict[str, List[Episode]] = field(default_factory=dict)
    is_series: bool = False
    default_translator: str = ""
    default_season: str = "1"
    default_episode: str = "1"
    premium: bool = False


@dataclass
class Quality:
    label: str
    hls: str = ""
    mp4: str = ""
    premium: bool = False

    @property
    def url(self) -> str:
        return self.hls or self.mp4

    @property
    def rank(self) -> int:
        low = self.label.lower().replace(" ", "")
        if "4k" in low or "2160" in low:
            return 2160
        m = re.search(r"(\d{3,4})", low)
        return int(m.group(1)) if m else 0


@dataclass
class StreamInfo:
    url: str
    quality: str = ""
    qualities: List[Quality] = field(default_factory=list)
    premium: bool = False
    subtitles: List[Tuple[str, str]] = field(default_factory=list)
    expires: Optional[int] = None


# --------------------------------------------------------------------------
# Текст/URL
# --------------------------------------------------------------------------

def clean_text(text: str) -> str:
    """Убирает теги, html-сущности и лишние пробелы."""
    text = re.sub(r"<br\s*/?>", " ", text or "", flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_module.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def kind_from_url(url: str) -> str:
    path = urllib.parse.urlparse(url or "").path.strip("/")
    first = path.split("/", 1)[0] if path else ""
    return KIND_LABELS.get(first, "Другое")


def _kinds_key(kind: str) -> int:
    try:
        return KIND_ORDER.index(kind)
    except ValueError:
        return len(KIND_ORDER)


def absolute(base: str, url: str) -> str:
    return urllib.parse.urljoin(base.rstrip("/") + "/", url or "")


def _find_year(text: str) -> str:
    m = re.search(r"\b(19\d{2}|20\d{2})\b", text or "")
    return m.group(1) if m else ""


def genres_from_misc(misc: str) -> str:
    """Из '2026, США, Драмы, Триллеры' достаёт 'Драмы, Триллеры'."""
    parts = [p.strip() for p in (misc or "").split(",") if p.strip()]
    if len(parts) <= 2:
        return ""
    return ", ".join(parts[2:])


def exp_from_url(url: str) -> Optional[int]:
    """Срок жизни подписанной ссылки CDN.

    В пути встречается метка вида .../<hex>:2026110310:<sig>/...
    (YYYYMMDDHH, московское время). Также поддерживаем ?e=/exp=.
    """
    m = re.search(r":(\d{10}):", url or "")
    if m:
        try:
            dt = datetime.strptime(m.group(1), "%Y%m%d%H")
            return int(dt.timestamp())
        except ValueError:
            pass
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url or "").query)
    for key in ("e", "exp", "expires"):
        if key in qs:
            try:
                return int(qs[key][0])
            except (TypeError, ValueError):
                pass
    return None


# --------------------------------------------------------------------------
# Разбор HTML
# --------------------------------------------------------------------------

def parse_search_html(text: str, base: str) -> List[SearchItem]:
    items: List[SearchItem] = []
    seen = set()
    for m in re.finditer(
        r'<li[^>]*>\s*<a\s+href="([^"]+)"[^>]*>(.*?)</a>',
        text or "", re.S,
    ):
        href, inner = m.group(1), m.group(2)
        url = absolute(base, href)
        if url in seen:
            continue
        seen.add(url)

        title_m = re.search(r'<span class="enty">(.*?)</span>', inner, re.S)
        title = clean_text(title_m.group(1)) if title_m else ""

        rating = ""
        rating_m = re.search(
            r'<span class="rating">.*?<i[^>]*>(.*?)</i>', inner, re.S
        )
        if rating_m:
            rating = clean_text(rating_m.group(1))

        rest = re.sub(
            r'<span class="enty">.*?</span>', "", inner, flags=re.S
        )
        rest = re.sub(
            r'<span class="rating">.*?</span>', "", rest, flags=re.S
        )
        meta = clean_text(rest).strip(" ()")
        if not title:
            title = clean_text(inner) or url
        items.append(
            SearchItem(
                title=title,
                url=url,
                kind=kind_from_url(url),
                year=_find_year(meta),
                meta=meta,
                rating=rating,
            )
        )
    return items


def parse_catalog_html(text: str, base: str) -> List[SearchItem]:
    items: List[SearchItem] = []
    seen = set()
    chunks = (text or "").split('<div class="b-content__inline_item"')[1:]
    for chunk in chunks:
        url_m = re.search(r'data-url="([^"]+)"', chunk)
        if not url_m:
            continue
        url = absolute(base, url_m.group(1))
        if url in seen:
            continue

        link_block = ""
        lb_m = re.search(
            r'<div class="b-content__inline_item-link">(.*?)</div>',
            chunk, re.S,
        )
        if lb_m:
            link_block = lb_m.group(1)

        title_m = (
            re.search(r'<a[^>]*>(.*?)</a>', link_block, re.S)
            if link_block else None
        )
        title = clean_text(title_m.group(1)) if title_m else ""
        if not title:
            alt_m = re.search(r'<img[^>]+alt="([^"]*)"', chunk)
            title = clean_text(alt_m.group(1)) if alt_m else url

        poster = ""
        img_m = re.search(r'<img[^>]+src="([^"]+)"', chunk)
        if img_m:
            poster = absolute(base, img_m.group(1))

        # На главной мета-строка имеет class="misc", в разделах — просто
        # <div>1979, США, Триллеры</div>. Поддерживаем оба варианта.
        misc = ""
        tail_m = re.search(
            r'<div class="b-content__inline_item-link">.*?</a>\s*'
            r'<div[^>]*>(.*?)</div>',
            chunk, re.S,
        )
        if tail_m:
            misc = clean_text(tail_m.group(1))
        if not misc:
            misc_m = re.search(
                r'<div class="misc">(.*?)</div>', chunk, re.S
            )
            if misc_m:
                misc = clean_text(misc_m.group(1))

        info = ""
        info_m = re.search(r'<span class="info">(.*?)</span>', chunk, re.S)
        if info_m:
            info = clean_text(info_m.group(1))

        kind = kind_from_url(url)
        if not kind or kind == "Другое":
            cat_m = re.search(r'<span class="cat\s+(\w+)"', chunk)
            if cat_m:
                kind = KIND_LABELS.get(cat_m.group(1), "Другое")

        seen.add(url)
        items.append(
            SearchItem(
                title=title,
                url=url,
                kind=kind,
                year=_find_year(misc),
                meta=misc,
                poster=poster,
                info=info,
                genres=genres_from_misc(misc),
            )
        )
    return items


def parse_genres(text: str, section: str) -> List[Tuple[str, str]]:
    """
    Жанры из верхнего меню сайта для раздела.

    Возвращает [(slug, "Название"), ...], например
    [("drama", "Драмы"), ("comedy", "Комедии")].
    """
    path_section = SECTION_PATHS.get(section, section)
    if not path_section:
        return []

    in_block = False
    block = ""
    for item in re.finditer(
        r'<li class="b-topnav__item', text or ""
    ):
        start = item.start()
        nxt = re.search(
            r'<li class="b-topnav__item', text[start + 1:]
        )
        end = (
            start + 1 + nxt.start()
            if nxt is not None else len(text)
        )
        chunk = text[start:end]
        link = re.search(
            r'<a[^>]+href="(/[^"]*?/)"[^>]*>', chunk
        )
        if not link:
            continue
        href = link.group(1)
        if href.strip("/") == path_section:
            in_block = True
            block = chunk
            break

    if not in_block:
        return []

    menu = re.search(r'<ul class="left">(.*?)</ul>', block, re.S)
    if not menu:
        return []

    result: List[Tuple[str, str]] = []
    seen = set()
    for m in re.finditer(
        r'<li><a[^>]+href="([^"]+)"[^>]*>(.*?)</a></li>',
        menu.group(1),
        re.S,
    ):
        href, name = m.group(1), clean_text(m.group(2))
        path = urllib.parse.urlparse(href).path.strip("/")
        parts = [p for p in path.split("/") if p]
        if len(parts) < 2:
            continue
        slug = parts[-1]
        if slug in GENRE_EXCLUDE or slug in seen:
            continue
        seen.add(slug)
        result.append((slug, name or slug))
    return result


def _parse_info_table(text: str) -> Dict[str, str]:
    info: Dict[str, str] = {}
    m = re.search(r'<table class="b-post__info"[^>]*>(.*?)</table>', text, re.S)
    if not m:
        return info
    for row in re.findall(r"<tr>(.*?)</tr>", m.group(1), re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) < 2:
            continue
        key = clean_text(cells[0]).rstrip(":").strip()
        value = clean_text(cells[1]).strip()
        if key:
            info[key] = value
    return info


def _parse_translators(
    text: str, default_tid: str, info: Dict[str, str]
) -> List[Translator]:
    result: List[Translator] = []
    seen = set()
    for m in re.finditer(
        r'<a([^>]*data-translator_id="(\d+)"[^>]*)>(.*?)</a>',
        text or "", re.S,
    ):
        attrs, tid, inner = m.group(1), m.group(2), m.group(3)
        if tid in seen:
            continue
        name_m = re.search(r'title="([^"]*)"', attrs)
        name = (
            html_module.unescape(name_m.group(1))
            if name_m else clean_text(inner)
        )
        name = clean_text(name) or f"Перевод {tid}"
        premium = "b-prem_translator" in attrs
        result.append(
            Translator(
                id=tid,
                name=name,
                premium=premium,
                original="оригинал" in name.lower(),
            )
        )
        seen.add(tid)

    if not result and default_tid:
        name = info.get("В переводе") or "По умолчанию"
        result.append(Translator(id=default_tid, name=name))
    return result


def _parse_seasons_html(text: str) -> Dict[str, str]:
    seasons: Dict[str, str] = {}
    for m in re.finditer(
        r'<a[^>]*data-tab_id="(\d+)"[^>]*>(.*?)</a>', text or "", re.S
    ):
        sid, inner = m.group(1), m.group(2)
        seasons.setdefault(sid, clean_text(inner) or f"Сезон {sid}")
    return seasons


def _parse_episodes_html(text: str) -> Dict[str, List[Episode]]:
    result: Dict[str, List[Episode]] = {}
    seen = set()
    for m in re.finditer(
        r'<a([^>]*data-episode_id="(\d+)"[^>]*)>(.*?)</a>', text or "", re.S
    ):
        attrs, eid, inner = m.group(1), m.group(2), m.group(3)
        sid_m = re.search(r'data-season_id="(\d+)"', attrs)
        sid = sid_m.group(1) if sid_m else "1"
        key = (sid, eid)
        if key in seen:
            continue
        seen.add(key)
        result.setdefault(sid, []).append(
            Episode(
                season_id=sid,
                episode_id=eid,
                name=clean_text(inner) or f"Серия {eid}",
            )
        )
    return result


def _parse_episodes_ajax(
    seasons_html: str, episodes_html: str
) -> Tuple[Dict[str, str], Dict[str, List[Episode]]]:
    seasons: Dict[str, str] = {}
    for m in re.finditer(r"<li([^>]*)>(.*?)</li>", seasons_html or "", re.S):
        attrs, inner = m.group(1), m.group(2)
        sid_m = re.search(r'data-tab_id="(\d+)"', attrs)
        if not sid_m:
            continue
        sid = sid_m.group(1)
        seasons.setdefault(sid, clean_text(inner) or f"Сезон {sid}")

    episodes: Dict[str, List[Episode]] = {}
    seen = set()
    for m in re.finditer(r"<li([^>]*)>(.*?)</li>", episodes_html or "", re.S):
        attrs, inner = m.group(1), m.group(2)
        sid_m = re.search(r'data-season_id="(\d+)"', attrs)
        eid_m = re.search(r'data-episode_id="(\d+)"', attrs)
        if not sid_m or not eid_m:
            continue
        sid, eid = sid_m.group(1), eid_m.group(1)
        key = (sid, eid)
        if key in seen:
            continue
        seen.add(key)
        episodes.setdefault(sid, []).append(
            Episode(
                season_id=sid,
                episode_id=eid,
                name=clean_text(inner) or f"Серия {eid}",
            )
        )
    return seasons, episodes


def _parse_init_events(text: str) -> Optional[dict]:
    m = re.search(
        r"initCDN(Movies|Series)Events\(\s*(\d+)\s*,\s*(\d+)\s*,\s*"
        r"(\d+)\s*,\s*(\d+)",
        text or "",
    )
    if not m:
        return None
    return {
        "is_series": m.group(1) == "Series",
        "post_id": m.group(2),
        "translator_id": m.group(3),
        "season": m.group(4),
        "episode": m.group(5),
    }


def parse_title_html(text: str, url: str, base: str) -> TitleInfo:
    pid_m = (
        re.search(r'id="post_id"[^>]*value="(\d+)"', text or "")
        or re.search(r'data-id="(\d+)"', text or "")
    )
    if not pid_m:
        raise HDrezkaError("на странице не найден id тайтла")
    post_id = pid_m.group(1)

    title_m = (
        re.search(r'<h1[^>]*itemprop="name"[^>]*>(.*?)</h1>', text, re.S)
        or re.search(r"<h1[^>]*>(.*?)</h1>", text, re.S)
    )
    title = clean_text(title_m.group(1)) if title_m else ""

    orig_m = re.search(
        r'<div class="b-post__origtitle"[^>]*>(.*?)</div>', text, re.S
    )
    original = clean_text(orig_m.group(1)) if orig_m else ""

    desc_m = re.search(
        r'<div class="b-post__description_text"[^>]*>(.*?)</div>',
        text, re.S,
    )
    description = clean_text(desc_m.group(1)) if desc_m else ""

    poster = ""
    poster_m = re.search(
        r'<meta property="og:image" content="([^"]+)"', text
    )
    if poster_m:
        poster = absolute(base, poster_m.group(1))

    info = _parse_info_table(text)
    init = _parse_init_events(text) or {}
    default_tid = str(init.get("translator_id") or "")
    translators = _parse_translators(text, default_tid, info)
    seasons = _parse_seasons_html(text)
    episodes = _parse_episodes_html(text)
    is_series = bool(init.get("is_series")) or bool(seasons or episodes)

    year = ""
    for key in ("Дата выхода", "Год"):
        if key in info:
            year = _find_year(info[key])
            if year:
                break
    if not year:
        year = _find_year(url)

    rating = ""
    rates = info.get("Рейтинги", "")
    imdb_m = re.search(r"IMDb:\s*([\d.]+)", rates)
    kp_m = re.search(r"Кинопоиск:\s*([\d.]+)", rates)
    parts = []
    if imdb_m:
        parts.append("IMDb " + imdb_m.group(1))
    if kp_m:
        parts.append("КП " + kp_m.group(1))
    rating = " · ".join(parts)

    if not is_series:
        default_season, default_episode = "1", "1"
    else:
        default_season = str(init.get("season") or "1")
        if default_season not in seasons and seasons:
            default_season = next(iter(seasons))
        # По умолчанию — первая серия по порядку, а не последняя
        # (initCDNSeriesEvents обычно указывает на активную/последнюю).
        season_eps = episodes.get(default_season) or []
        ordered = sorted(
            season_eps,
            key=lambda e: (
                int(e.episode_id) if e.episode_id.isdigit() else 10 ** 9
            ),
        )
        default_episode = ordered[0].episode_id if ordered else "1"
    if default_tid and default_tid not in {t.id for t in translators}:
        translators.insert(0, Translator(id=default_tid, name="По умолчанию"))

    return TitleInfo(
        post_id=post_id,
        url=url,
        title=title or original or url,
        original_title=original,
        description=description,
        poster=poster,
        kind=kind_from_url(url),
        year=year,
        country=info.get("Страна", ""),
        genres=info.get("Жанр", ""),
        rating=rating,
        duration=info.get("Время", ""),
        quality=info.get("В качестве", ""),
        info=info,
        translators=translators,
        seasons=seasons,
        episodes=episodes,
        is_series=is_series,
        default_translator=default_tid,
        default_season=default_season,
        default_episode=default_episode,
    )


# --------------------------------------------------------------------------
# Ссылки CDN/качества
# --------------------------------------------------------------------------

def _b64_decode(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    pad = "=" * (-len(text) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            return decoder((text + pad).encode("ascii")).decode("utf-8")
        except Exception:
            continue
    return ""


# Сервер иногда отдаёт url в «замусоренном» base64 (#h...).
# Актуальный набор мусорных строк — как в рабочих userscript-клиентах.
_TRASH_STRINGS = [
    "$$!!@$$@^!@#$$@",
    "@@@@@!##!^^^",
    "####^!!##!@@",
    "^^^!@##!!##",
    "$$#!!@#!@##",
]
_TRASH_TOKENS = [
    "//_//" + base64.b64encode(s.encode("ascii")).decode("ascii")
    for s in _TRASH_STRINGS
]

# Старый формат: мусор из комбинаций 2-3 символов.
_OLD_TRASH_TOKENS = {
    base64.b64encode("".join(chars).encode("ascii")).decode("ascii")
    for n in (2, 3)
    for chars in itertools.product("@#!^$", repeat=n)
}


def decode_cdn_url(raw: str) -> str:
    """Декодирует url из ответа get_cdn_series (обычный или #h-base64)."""
    raw = (raw or "").strip()
    if not raw:
        return raw
    if raw.lstrip().startswith("[") and "http" in raw:
        return raw

    # Новый формат: "#h" + base64, внутри вставлены "//_//"+base64(мусор).
    if raw.startswith("#"):
        payload = raw[2:]
        for token in _TRASH_TOKENS:
            payload = payload.replace(token, "")
        payload = payload.replace("//_//", "")
        decoded = _b64_decode(payload)
        if decoded.lstrip().startswith("[") and "http" in decoded:
            return decoded

    # Старый формат: тот же принцип, но мусор — короткие комбинации.
    payload = raw.replace("#h", "").replace("//_//", "")
    for token in _OLD_TRASH_TOKENS:
        payload = payload.replace(token, "")
    decoded = _b64_decode(payload)
    if decoded.lstrip().startswith("[") and "http" in decoded:
        return decoded

    # Крайний случай: весь url — обычный base64.
    decoded = _b64_decode(raw)
    if decoded.lstrip().startswith("[") and "http" in decoded:
        return decoded
    return raw


def parse_quality_list(raw: str) -> List[Quality]:
    result: List[Quality] = []
    seen = set()
    for part in re.split(r",(?=\[)", decode_cdn_url(raw)):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"\[([^\]]+)\](.+)", part, re.S)
        if not m:
            continue
        label_html, target = m.group(1), m.group(2).strip()
        premium = (
            "pjs-prem-quality" in label_html
            or "premium" in label_html.lower()
        )
        label = clean_text(label_html) or "?"

        alts = [
            a.strip() for a in re.split(r"\s+or\s+", target) if a.strip()
        ]
        hls = next((a for a in alts if "m3u8" in a.lower()), "")
        mp4 = next((a for a in alts if "m3u8" not in a.lower()), "")

        key = (label, hls, mp4)
        if key in seen:
            continue
        seen.add(key)
        result.append(Quality(label=label, hls=hls, mp4=mp4, premium=premium))
    return result


def choose_quality(
    qualities: List[Quality], preference: str = "auto"
) -> Optional[Quality]:
    """Выбирает качество. PRO-варианты берём только если других нет."""
    if not qualities:
        return None

    usable = [q for q in qualities if q.url and not q.premium]
    if not usable:
        usable = [q for q in qualities if q.url]
    if not usable:
        return None

    preference = (preference or "auto").lower()
    if preference in ("auto", "best", "max"):
        return max(usable, key=lambda q: q.rank)

    try:
        target = int(re.sub(r"\D", "", preference) or 0)
    except ValueError:
        target = 0
    if not target:
        return max(usable, key=lambda q: q.rank)

    below = [q for q in usable if 0 < q.rank <= target]
    if below:
        return max(below, key=lambda q: q.rank)
    return min(usable, key=lambda q: q.rank)


def parse_subtitles(raw) -> List[Tuple[str, str]]:
    if not raw or not isinstance(raw, str):
        return []
    result: List[Tuple[str, str]] = []
    for part in re.split(r",(?=\[)", raw):
        m = re.match(r"\[([^\]]+)\](.+)", part.strip(), re.S)
        if not m:
            continue
        label = clean_text(m.group(1))
        url = m.group(2).strip().split(" or ")[0]
        if url.startswith("http"):
            result.append((label, url))
    return result


# --------------------------------------------------------------------------
# Клиент
# --------------------------------------------------------------------------

class HDrezkaClient:
    """HTTP-клиент HDrezka с решением Anubis и парсингом ответов."""

    def __init__(
        self,
        base: str = DEFAULT_BASE,
        proxy: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self.base = (base or DEFAULT_BASE).rstrip("/")
        if "://" not in self.base:
            self.base = "https://" + self.base
        self.proxy = proxy or None
        self.timeout = float(timeout)
        self._lock = threading.RLock()
        self._access_ok = False
        self.challenge_count = 0
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            }
        )

    # ---------- низкий уровень ----------
    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return self.base + (path if path.startswith("/") else "/" + path)

    def _proxies(self) -> dict:
        if not self.proxy:
            return {}
        return {"http": self.proxy, "https": self.proxy}

    def _raw(self, method: str, url: str, **kwargs) -> requests.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Referer", self.base + "/")
        last: Optional[Exception] = None
        for attempt in range(1, HTTP_RETRIES + 1):
            try:
                resp = self.session.request(
                    method,
                    url,
                    headers=headers,
                    proxies=self._proxies(),
                    verify=False,
                    timeout=(min(15.0, self.timeout), self.timeout),
                    allow_redirects=True,
                    **kwargs,
                )
                if (
                    resp.status_code in (429, 500, 502, 503, 504)
                    and attempt < HTTP_RETRIES
                ):
                    time.sleep(float(attempt))
                    continue
                return resp
            except requests.RequestException as e:
                last = e
                if attempt < HTTP_RETRIES:
                    time.sleep(float(attempt))
        raise HDrezkaError(f"сетевая ошибка: {last}") from last

    @staticmethod
    def _is_challenge(resp: requests.Response) -> bool:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "text" not in ctype and "html" not in ctype:
            return False
        try:
            return 'id="anubis_challenge"' in resp.text
        except Exception:
            return False

    @staticmethod
    def _solve_pow(random_data: str, difficulty: int) -> Tuple[str, int]:
        """Anubis fast: sha256(randomData + nonce) с N ведущими hex-нулями."""
        difficulty = max(0, int(difficulty))
        nonce = 0
        target = "0" * difficulty
        deadline = time.time() + 120.0
        while True:
            digest = hashlib.sha256(
                (random_data + str(nonce)).encode("utf-8")
            ).hexdigest()
            if digest.startswith(target):
                return digest, nonce
            nonce += 1
            if (nonce & 0xFFFF) == 0 and time.time() > deadline:
                raise HDrezkaError(
                    "Anubis: задача оказалась слишком сложной (timeout)"
                )

    def _solve_challenge(self, text: str, path: str) -> bool:
        m = re.search(
            r'<script id="anubis_challenge" type="application/json">'
            r"(.*?)</script>",
            text or "",
            re.S,
        )
        if not m:
            return False
        try:
            payload = json.loads(m.group(1))
        except ValueError:
            return False

        rules = payload.get("rules") or {}
        challenge = payload.get("challenge") or {}
        random_data = str(challenge.get("randomData") or "")
        if not random_data:
            return False
        difficulty = int(rules.get("difficulty") or 0)

        started = time.time()
        digest, nonce = self._solve_pow(random_data, difficulty)

        prefix = ""
        pm = re.search(
            r'<script id="anubis_base_prefix" type="application/json">'
            r"(.*?)</script>",
            text,
            re.S,
        )
        if pm:
            try:
                prefix = json.loads(pm.group(1)) or ""
            except ValueError:
                prefix = ""

        redir = self._url(path or "/")
        params = {
            "id": challenge.get("id") or "",
            "response": digest,
            "nonce": nonce,
            "redir": redir,
            "elapsedTime": int((time.time() - started) * 1000),
        }
        url = (
            self.base
            + prefix
            + "/.within.website/x/cmd/anubis/api/pass-challenge"
        )
        try:
            resp = self._raw(
                "GET",
                url,
                params=params,
                headers={
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;"
                        "q=0.9,*/*;q=0.8"
                    ),
                },
            )
        except HDrezkaError:
            return False
        self.challenge_count += 1
        return resp.status_code < 400

    def _ensure_access(self):
        if self._access_ok:
            return
        resp = self._raw("GET", self.base + "/")
        if self._is_challenge(resp):
            if not self._solve_challenge(resp.text, "/"):
                raise HDrezkaError("не удалось решить проверку Anubis")
            resp = self._raw("GET", self.base + "/")
        if self._is_challenge(resp):
            raise HDrezkaError(
                "сайт всё ещё показывает проверку Anubis; "
                "попробуйте позже или смените зеркало"
            )
        self._access_ok = True

    # ---------- публичный запрос ----------
    def request(self, method: str, path: str, **kwargs) -> requests.Response:
        with self._lock:
            url = self._url(path)
            self._ensure_access()
            resp = self._raw(method, url, **kwargs)
            if self._is_challenge(resp):
                page_path = urllib.parse.urlparse(url).path or "/"
                if not self._solve_challenge(resp.text, page_path):
                    raise HDrezkaError("сессия Anubis истекла")
                resp = self._raw(method, url, **kwargs)
            return resp

    def reset(self):
        with self._lock:
            self.session.cookies.clear()
            self._access_ok = False

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass

    # ---------- API ----------
    def fetch_home(self) -> List[SearchItem]:
        resp = self.request("GET", "/")
        if resp.status_code >= 400:
            raise HDrezkaError(f"HTTP {resp.status_code} при загрузке главной")
        return parse_catalog_html(resp.text, self.base)

    def fetch_catalog(
        self, section: str, page: int = 1, genre: str = ""
    ) -> Tuple[List[SearchItem], bool, List[Tuple[str, str]]]:
        section = (section or "films").strip("/")
        path_section = SECTION_PATHS.get(section, section)
        if not path_section:
            raise HDrezkaError(
                "для главной страницы используйте fetch_home()"
            )
        genre = (genre or "").strip("/")
        base = f"/{path_section}/"
        if genre:
            base = f"/{path_section}/{genre}/"
        path = base if page <= 1 else f"{base}page/{page}/"
        resp = self.request("GET", path)
        if resp.status_code >= 400:
            raise HDrezkaError(f"HTTP {resp.status_code} для {path}")
        items = parse_catalog_html(resp.text, self.base)
        has_more = bool(
            re.search(rf'href="[^"]*page/{page + 1}/"', resp.text)
        )
        genres = (
            parse_genres(resp.text, section) if page <= 1 else []
        )
        return items, has_more, genres

    def search(self, query: str) -> List[SearchItem]:
        query = (query or "").strip()
        if not query:
            return []
        resp = self.request(
            "GET",
            "/engine/ajax/search.php",
            params={"q": query},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": self.base + "/",
            },
        )
        if resp.status_code >= 400:
            raise HDrezkaError(f"HTTP {resp.status_code} при поиске")
        return parse_search_html(resp.text, self.base)

    def fetch_title(self, url: str) -> TitleInfo:
        resp = self.request("GET", url)
        if resp.status_code >= 400:
            raise HDrezkaError(f"HTTP {resp.status_code} при загрузке тайтла")
        info = parse_title_html(resp.text, resp.url or self._url(url), self.base)
        return info

    def _ajax(self, data: dict, referer: str) -> dict:
        resp = self.request(
            "POST",
            "/ajax/get_cdn_series/?t=%d" % int(time.time() * 1000),
            data=data,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": referer or (self.base + "/"),
                "Origin": self.base,
            },
        )
        if resp.status_code >= 400:
            raise HDrezkaError(f"HTTP {resp.status_code} от CDN API")
        try:
            payload = resp.json()
        except ValueError as e:
            raise HDrezkaError(
                "CDN API вернул не JSON (возможно, проверка Anubis)"
            ) from e
        if not isinstance(payload, dict):
            raise HDrezkaError("неожиданный ответ CDN API")
        return payload

    def fetch_episodes(
        self, post_id: str, translator_id: str, referer: str
    ) -> Tuple[Dict[str, str], Dict[str, List[Episode]], bool]:
        payload = self._ajax(
            {
                "id": str(post_id),
                "translator_id": str(translator_id),
                "action": "get_episodes",
            },
            referer,
        )
        if not payload.get("success"):
            raise HDrezkaError(
                payload.get("message") or "не удалось получить список серий"
            )
        seasons, episodes = _parse_episodes_ajax(
            payload.get("seasons") or "", payload.get("episodes") or ""
        )
        return seasons, episodes, bool(payload.get("premium_content"))

    def _request_stream(self, data: dict, referer: str) -> dict:
        """
        POST get_cdn_series с повторами.

        CDN иногда отвечает success=true с пустым url (транзиентно или
        из-за устаревшей сессии). Пробуем несколько раз, при сообщении
        об истёкшей сессии — сбрасываем cookie и проходим Anubis заново.
        """
        last: dict = {}
        for delay in (0.0, 0.5, 1.2):
            if delay:
                time.sleep(delay)
            payload = self._ajax(data, referer)
            last = payload
            if payload.get("success") and (payload.get("url") or "").strip():
                return payload
            if not payload.get("success"):
                message = str(payload.get("message") or "").lower()
                if "сесси" in message or "session" in message:
                    self.reset()
                    continue
                return payload

        # Последний шанс: свежая сессия Anubis.
        if last.get("success") and not (last.get("url") or "").strip():
            self.reset()
            time.sleep(0.4)
            return self._ajax(data, referer)
        return last

    def fetch_stream(
        self,
        post_id: str,
        translator_id: str,
        season: str,
        episode: str,
        is_series: bool,
        referer: str,
    ) -> StreamInfo:
        action = "get_stream" if is_series else "get_movie"
        data = {
            "id": str(post_id),
            "translator_id": str(translator_id),
            "action": action,
            "season": str(season or "1"),
            "episode": str(episode or "1"),
        }
        payload = self._request_stream(data, referer)
        if not payload.get("success"):
            raise HDrezkaError(
                payload.get("message")
                or "CDN API не вернул ссылку (возможно, PRO-контент)"
            )

        raw = payload.get("url") or ""
        if not raw.strip():
            raise HDrezkaError(
                payload.get("message")
                or "CDN API вернул пустой url — для этого перевода и серии "
                   "ссылка недоступна (PRO или серия ещё не вышла)"
            )
        qualities = parse_quality_list(raw)
        if not qualities:
            raise HDrezkaError(
                "CDN API вернул ссылку в неизвестном формате "
                f"(длина {len(raw)}, начало {raw[:2]!r})"
            )

        first = next((q for q in qualities if q.url), qualities[0])
        return StreamInfo(
            url=first.url,
            quality=first.label,
            qualities=qualities,
            premium=bool(payload.get("premium_content")),
            subtitles=parse_subtitles(payload.get("subtitle")),
            expires=exp_from_url(first.url),
        )


def fetch_bytes(
    url: str,
    proxy: Optional[str] = None,
    referer: Optional[str] = None,
    timeout: float = 20.0,
) -> bytes:
    """Загружает произвольный URL (постер) с тем же прокси и без проверки TLS."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    proxies = {"http": proxy, "https": proxy} if proxy else {}
    session = requests.Session()
    try:
        resp = session.get(
            url,
            headers=headers,
            proxies=proxies,
            verify=False,
            timeout=(min(10.0, timeout), timeout),
            allow_redirects=True,
        )
        resp.raise_for_status()
        return resp.content
    finally:
        session.close()
