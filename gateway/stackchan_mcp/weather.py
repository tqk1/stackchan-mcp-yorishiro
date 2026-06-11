"""Weather check for the Phase E notify-heartbeat (yorishiro fork).

Fetches the JMA (Japan Meteorological Agency) bosai feeds — no API key
required — and decides whether today's weather is worth a proactive
one-liner. Silence is the default: a normal day produces ``None``, and
the heartbeat says nothing.

Endpoints (``{office}`` is a JMA office code such as ``270000`` for
Osaka prefecture):

- ``https://www.jma.go.jp/bosai/warning/data/warning/{office}.json`` —
  active warnings/advisories per municipality (class20 code, e.g.
  ``2720900`` for Moriguchi City).
- ``https://www.jma.go.jp/bosai/forecast/data/forecast/{office}.json`` —
  short-term forecast including precipitation probabilities (pops).

Judgement is split into pure functions over the parsed JSON so the
speak/stay-silent matrix is unit-testable without the network.
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

JMA_WARNING_URL = "https://www.jma.go.jp/bosai/warning/data/warning/{office}.json"
JMA_FORECAST_URL = "https://www.jma.go.jp/bosai/forecast/data/forecast/{office}.json"

FETCH_TIMEOUT_S = 15

#: JMA warning/advisory codes → spoken names. Unknown codes fall back
#: to a generic phrase rather than being dropped, so a new code the
#: agency introduces still gets announced.
WARNING_NAMES = {
    # 特別警報
    "32": "暴風雪特別警報",
    "33": "大雨特別警報",
    "35": "暴風特別警報",
    "36": "大雪特別警報",
    "37": "波浪特別警報",
    "38": "高潮特別警報",
    # 警報
    "02": "暴風雪警報",
    "03": "大雨警報",
    "04": "洪水警報",
    "05": "暴風警報",
    "06": "大雪警報",
    "07": "波浪警報",
    "08": "高潮警報",
    # 注意報
    "10": "大雨注意報",
    "12": "大雪注意報",
    "13": "風雪注意報",
    "14": "雷注意報",
    "15": "強風注意報",
    "16": "波浪注意報",
    "17": "融雪注意報",
    "18": "洪水注意報",
    "19": "高潮注意報",
    "20": "濃霧注意報",
    "21": "乾燥注意報",
    "22": "なだれ注意報",
    "23": "低温注意報",
    "24": "霜注意報",
    "25": "着氷注意報",
    "26": "着雪注意報",
}

#: Statuses meaning the warning is in effect right now. "解除"
#: (cancelled) entries linger in the feed and must not trigger speech.
ACTIVE_STATUSES = frozenset({"発表", "継続"})


def active_warnings(warning_json: dict[str, Any], city_code: str) -> list[str]:
    """Names of warnings currently in effect for one municipality."""
    names: list[str] = []
    for area_type in warning_json.get("areaTypes", []):
        for area in area_type.get("areas", []):
            if area.get("code") != city_code:
                continue
            for warning in area.get("warnings", []):
                if warning.get("status") not in ACTIVE_STATUSES:
                    continue
                code = str(warning.get("code", ""))
                names.append(WARNING_NAMES.get(code, "気象の注意報"))
    return names


def today_max_pop(
    forecast_json: list[Any], today: _dt.date
) -> int | None:
    """Max precipitation probability (%) among today's forecast slots.

    Returns None when the feed has no usable pops for today (past
    slots are published as empty strings).
    """
    best: int | None = None
    try:
        time_series = forecast_json[0]["timeSeries"]
    except (IndexError, KeyError, TypeError):
        return None
    for series in time_series:
        areas = series.get("areas") or []
        if not areas or "pops" not in areas[0]:
            continue
        pops = areas[0]["pops"]
        for when_s, pop_s in zip(series.get("timeDefines", []), pops):
            try:
                when = _dt.datetime.fromisoformat(when_s)
                pop = int(pop_s)
            except (ValueError, TypeError):
                continue
            if when.date() != today:
                continue
            if best is None or pop > best:
                best = pop
    return best


def judge_weather(
    warning_json: dict[str, Any],
    forecast_json: list[Any],
    *,
    city_code: str,
    pop_threshold: int,
    today: _dt.date,
) -> str | None:
    """One spoken line when the weather warrants it, else None.

    Priority: active warnings/advisories first, then a rain heads-up
    when today's precipitation probability reaches the threshold. A
    normal day returns None — the heartbeat stays silent.

    Phrasing is fixed templates for now; swapping in an LLM for the
    wording (detection stays deterministic) is a known extension point.
    """
    warnings = active_warnings(warning_json, city_code)
    if warnings:
        listed = "と".join(warnings[:2])
        return f"{listed}が出てるよ、気をつけてね"

    pop = today_max_pop(forecast_json, today)
    if pop is not None and pop >= pop_threshold:
        return f"今日は雨が降りそうだよ、降水確率{pop}%。傘を忘れずにね"
    return None


async def _fetch_json(session: aiohttp.ClientSession, url: str) -> Any:
    async with session.get(url) as resp:
        resp.raise_for_status()
        # JMA serves JSON with a text/plain-ish content type at times;
        # don't let aiohttp's strict content-type check break us.
        return await resp.json(content_type=None)


async def check_weather(
    office_code: str,
    city_code: str,
    pop_threshold: int,
    *,
    today: _dt.date | None = None,
) -> str | None:
    """Fetch both feeds and judge. Raises on network/HTTP failure.

    Raising (rather than swallowing) lets the heartbeat distinguish
    "checked, nothing to say" (mark today as done) from "could not
    check" (leave the daily flag unset and retry on the next tick
    inside the window).
    """
    timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        warning_json = await _fetch_json(
            session, JMA_WARNING_URL.format(office=office_code)
        )
        forecast_json = await _fetch_json(
            session, JMA_FORECAST_URL.format(office=office_code)
        )
    return judge_weather(
        warning_json,
        forecast_json,
        city_code=city_code,
        pop_threshold=pop_threshold,
        today=today or _dt.date.today(),
    )
