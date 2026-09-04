from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from html import unescape
import re
from typing import Any, Sequence
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from app.core.clock import beijing_now


_WEATHER_INTENT = re.compile(r"天气|气温|温度|下雨|降雨|天气预报")
_CALENDAR_INTENT = re.compile(r"今天几号|明天几号|后天几号|星期几|周几|日期|日历")
_SEARCH_INTENT = re.compile(
    r"联网(?:搜索|查找|查一下)|网上(?:搜索|查找|查一下)|帮我搜|搜索一下|搜一下|"
    r"(?:查查|查一查|查一下|查询|查证).{0,80}|最新(?:消息|新闻|资料|进展)|最近.+新闻"
)
_SEARCH_CAPABILITY_INTENT = re.compile(
    r"^(?:你|橙蓝).{0,10}(?:现在|目前)?(?:可以|能|会).{0,6}(?:联网)?(?:搜索|查询).{0,4}(?:吗|么|不|？|\?)$"
)
_LOCATION = re.compile(
    r"(?P<location>[\u4e00-\u9fffA-Za-z·]{2,24}?)(?:今天|明天|后天|未来|这几天|本周)?(?:的)?(?:天气|气温|温度|降雨|天气预报|会不会下雨)"
)
_LOCATION_ONLY = re.compile(r"^[\u4e00-\u9fffA-Za-z·\s]{2,24}$")
_RESULT_LINK = re.compile(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
_RESULT_SNIPPET = re.compile(r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_AUTHORITATIVE_SEARCHES = (
    (re.compile(r"(?:世界杯|world\s*cup|fifa)", re.I), "site:fifa.com"),
)
_AUTHORITATIVE_DOMAINS = ("fifa.com", "edu.cn", "gov.cn")


@dataclass(frozen=True, slots=True)
class ToolContext:
    text: str = ""
    tools: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


def calendar_snapshot(value: date | None = None) -> dict[str, Any]:
    local_now = beijing_now()
    target = value or local_now.date()
    labels = "一二三四五六日"
    return {
        "date": target.isoformat(),
        "weekday": f"星期{labels[target.weekday()]}",
        "beijing_time": local_now.isoformat(),
        "timezone": "Asia/Shanghai",
        "is_today": target == local_now.date(),
    }


async def weather_lookup(location: str, *, timeout_seconds: float = 10) -> dict[str, Any]:
    normalized = _clean_query(location, 60)
    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
        places: list[dict[str, Any]] = []
        for candidate in _geocoding_candidates(normalized):
            geocoding = await client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": candidate, "count": 1, "language": "zh", "format": "json"},
            )
            geocoding.raise_for_status()
            places = geocoding.json().get("results") or []
            if places:
                break
        if not places:
            raise ValueError("没有找到这个城市，请换成更完整的城市名称")
        place = places[0]
        forecast = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "forecast_days": 3,
                "timezone": "Asia/Shanghai",
            },
        )
        forecast.raise_for_status()
    payload = forecast.json()
    current = payload.get("current") or {}
    daily = payload.get("daily") or {}
    days = []
    for index, day in enumerate(daily.get("time") or []):
        days.append(
            {
                "date": day,
                "weather": _weather_label(_at(daily.get("weather_code"), index)),
                "temperature_max": _at(daily.get("temperature_2m_max"), index),
                "temperature_min": _at(daily.get("temperature_2m_min"), index),
                "precipitation_probability": _at(daily.get("precipitation_probability_max"), index),
            }
        )
    display_name = " · ".join(str(value) for value in (place.get("name"), place.get("admin1"), place.get("country")) if value)
    return {
        "location": display_name,
        "latitude": place.get("latitude"),
        "longitude": place.get("longitude"),
        "timezone": "Asia/Shanghai",
        "observed_at": current.get("time"),
        "current": {
            "weather": _weather_label(current.get("weather_code")),
            "temperature": current.get("temperature_2m"),
            "apparent_temperature": current.get("apparent_temperature"),
            "humidity": current.get("relative_humidity_2m"),
            "precipitation": current.get("precipitation"),
            "wind_speed": current.get("wind_speed_10m"),
        },
        "forecast": days,
        "source": "Open-Meteo",
    }


async def web_search(query: str, *, limit: int = 5, timeout_seconds: float = 10) -> dict[str, Any]:
    normalized = _clean_query(query, 160)
    count = max(1, min(limit, 8))
    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
        queries = [f"{prefix} {normalized}" for pattern, prefix in _AUTHORITATIVE_SEARCHES if pattern.search(normalized)]
        queries.append(normalized)
        batches = [await _duckduckgo_results(client, item, count) for item in queries]
    results: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for batch in batches:
        for item in batch:
            if item["url"] in seen_urls:
                continue
            seen_urls.add(item["url"])
            item["source_tier"] = _source_tier(item["url"])
            results.append(item)
            if len(results) >= count:
                break
        if len(results) >= count:
            break
    if not results:
        raise RuntimeError("搜索服务没有返回可用结果")
    return {"query": normalized, "results": results, "source": "DuckDuckGo"}


async def _duckduckgo_results(client: httpx.AsyncClient, query: str, count: int) -> list[dict[str, str]]:
    response = await client.get(
        "https://html.duckduckgo.com/html/",
        params={"q": query, "kl": "cn-zh"},
        headers={"User-Agent": "Mozilla/5.0 (compatible; NekoAI/1.0; local-personal-assistant)"},
    )
    response.raise_for_status()
    links = _RESULT_LINK.findall(response.text)
    snippets = [_plain_text(item) for item in _RESULT_SNIPPET.findall(response.text)]
    results: list[dict[str, str]] = []
    for index, (raw_url, raw_title) in enumerate(links[:count]):
        url = _result_url(unescape(raw_url))
        if url.startswith(("https://", "http://")):
            results.append({"title": _plain_text(raw_title), "url": url, "snippet": snippets[index] if index < len(snippets) else ""})
    return results


async def online_context_for_message(content: str, *, recent_messages: Sequence[str] = ()) -> ToolContext:
    normalized = " ".join((content or "").split())
    if not normalized:
        return ToolContext()
    sections: list[str] = []
    tools: list[str] = []
    errors: list[str] = []
    if _CALENDAR_INTENT.search(normalized):
        item = calendar_snapshot()
        sections.append(f"北京时间日历：{item['date']}，{item['weekday']}。")
        tools.append("CALENDAR")
    weather_requested = bool(_WEATHER_INTENT.search(normalized))
    weather_followup = not weather_requested and _looks_like_weather_followup(normalized, recent_messages)
    if weather_requested or weather_followup:
        location = _extract_location(normalized if weather_requested else f"{normalized}天气")
        if not location:
            sections.append("天气工具：用户没有提供城市，回答时应先询问所在城市，不得猜测位置。")
            errors.append("WEATHER_LOCATION_REQUIRED")
        else:
            try:
                weather = await weather_lookup(location)
                current = weather["current"]
                forecast = weather["forecast"]
                future = "；".join(
                    f"{item['date']} {item['weather']} {item['temperature_min']}–{item['temperature_max']}℃，降雨概率 {item['precipitation_probability']}%"
                    for item in forecast
                )
                sections.append(
                    f"联网天气（Open-Meteo，{weather['location']}，北京时间 {weather['observed_at']}）："
                    f"当前 {current['weather']}，{current['temperature']}℃，体感 {current['apparent_temperature']}℃，"
                    f"湿度 {current['humidity']}%，风速 {current['wind_speed']} km/h。未来三天：{future}"
                )
                tools.append("WEATHER")
            except Exception as exc:
                sections.append(f"天气工具暂时不可用：{_safe_error(exc)}。不要编造实时天气。")
                errors.append("WEATHER_UNAVAILABLE")
    search_capability_question = bool(_SEARCH_CAPABILITY_INTENT.search(normalized))
    if search_capability_question:
        try:
            await web_search("OpenAI 官方网站", limit=1)
            sections.append("联网搜索通道状态：本轮连通性探测成功。用户是在询问搜索能力，不是在要求搜索这句话本身。")
            tools.append("WEB_SEARCH_STATUS")
        except Exception as exc:
            sections.append(f"联网搜索通道状态：本轮探测失败（{_safe_error(exc)}）。请如实说明当前暂不可用。")
            errors.append("WEB_SEARCH_UNAVAILABLE")
    elif _SEARCH_INTENT.search(normalized):
        query = _search_query(normalized)
        try:
            result = await web_search(query)
            lines = [
                f"- [{_source_tier(item['url'])}] {item['title']}：{item['snippet']}（{item['url']}）"
                for item in result["results"]
            ]
            sections.append(
                "联网搜索结果（DuckDuckGo）：\n"
                "标记为“权威来源”的结果可作为实时事实依据；“普通网页”只能作为线索。"
                "若缺少权威来源或不同来源互相矛盾，必须明确说尚无法可靠确认，不得把推测写成事实。\n"
                + "\n".join(lines)
            )
            tools.append("WEB_SEARCH")
        except Exception as exc:
            sections.append(f"联网搜索暂时不可用：{_safe_error(exc)}。请明确说明未取得实时搜索结果。")
            errors.append("WEB_SEARCH_UNAVAILABLE")
    if not sections:
        return ToolContext()
    return ToolContext("\n\n".join(sections), tuple(tools), tuple(errors))


def _extract_location(content: str) -> str:
    match = _LOCATION.search(content)
    if not match:
        return ""
    value = match.group("location").strip()
    value = re.sub(r"^(?:请问|麻烦|帮我|查一下|查查|看看|今天|明天|后天|现在|目前|当地)+", "", value)
    value = re.sub(r"(?:当地|这里|那边)的?$", "", value).strip()
    return "" if value in {"今天", "明天", "后天", "现在", "当地"} else value


def _looks_like_weather_followup(content: str, recent_messages: Sequence[str]) -> bool:
    candidate = content.strip(" ，。！？!?；;")
    if not _LOCATION_ONLY.fullmatch(candidate):
        return False
    if re.search(r"(?:你|我|联网|可以|能否|能不能|为什么|怎么|了吗|是不是|查询|搜索|工具)", candidate):
        return False
    # Only treat a bare place name as a continuation when the immediately
    # preceding reply explicitly asked the user to clarify a weather location.
    # A successful weather answer somewhere in older history must not make an
    # unrelated sentence look like a city.
    return any(
        _WEATHER_INTENT.search(prior)
        and re.search(r"(?:城市|地点|位置|区县|街道|哪里|所在)", prior)
        for prior in (" ".join((item or "").split()) for item in tuple(recent_messages)[:2])
    )


def _geocoding_candidates(location: str) -> tuple[str, ...]:
    candidates = [location]
    province_tail = re.sub(r"^.+?(?:省|自治区|特别行政区)", "", location).strip()
    if province_tail and province_tail != location:
        candidates.append(province_tail)
    without_suffix = re.sub(r"(?:市|自治州|地区|县|区)$", "", province_tail or location).strip()
    if without_suffix:
        candidates.append(without_suffix)
    return tuple(dict.fromkeys(candidates))


def _search_query(content: str) -> str:
    value = re.sub(
        r"^(?:那|那么)?(?:请|麻烦)?(?:你)?(?:可以|能不能|能否|能)?(?:帮我)?(?:联网|网上)?"
        r"(?:搜索|查找|查一下|查一查|查查|查询|查证|搜一下)(?:一下)?",
        "",
        content,
    ).strip(" ，。？?")
    return value or content


def _clean_query(value: str, limit: int) -> str:
    normalized = " ".join((value or "").split())
    if not normalized:
        raise ValueError("查询内容不能为空")
    return normalized[:limit]


def _plain_text(value: str) -> str:
    return " ".join(unescape(_TAG.sub(" ", value)).split())


def _result_url(value: str) -> str:
    parsed = urlparse(value)
    redirected = parse_qs(parsed.query).get("uddg") if parsed.path.startswith("/l/") else None
    return unquote(redirected[0]) if redirected else value


def _source_tier(url: str) -> str:
    hostname = (urlparse(url).hostname or "").casefold().removeprefix("www.")
    if any(hostname == domain or hostname.endswith(f".{domain}") for domain in _AUTHORITATIVE_DOMAINS):
        return "权威来源"
    return "普通网页"


def _at(values: Any, index: int) -> Any:
    return values[index] if isinstance(values, list) and index < len(values) else None


def _safe_error(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:160] or exc.__class__.__name__


def _weather_label(code: Any) -> str:
    try:
        value = int(code)
    except (TypeError, ValueError):
        return "未知"
    if value == 0:
        return "晴"
    if value in {1, 2}:
        return "少云"
    if value == 3:
        return "阴"
    if value in {45, 48}:
        return "雾"
    if value in {51, 53, 55, 56, 57}:
        return "毛毛雨"
    if value in {61, 63, 65, 66, 67, 80, 81, 82}:
        return "雨"
    if value in {71, 73, 75, 77, 85, 86}:
        return "雪"
    if value in {95, 96, 99}:
        return "雷雨"
    return "多云"
