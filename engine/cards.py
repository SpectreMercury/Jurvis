"""Small live lookups whose answer is shown to the user as a card: weather, a crypto price, an exchange rate, the time somewhere, days until a date.

Each function returns the facts for the model plus a "card" the UI draws; the harness takes the card out before the model sees the result.
A card is {kind, title, subtitle, value, icon, rows: [{label, value}], days: [{label, icon, high, low}], series: [numbers], trend, note}.
The services are free and need no key: Open-Meteo (weather, places and their time zones), CoinGecko (crypto), Frankfurter (ECB exchange rates).
"""
from datetime import date, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
# WMO weather codes -> (icon the UI knows, Chinese description)
WMO = {0: ("sun", "晴"), 1: ("partly", "晴间多云"), 2: ("partly", "多云"), 3: ("cloud", "阴"), 45: ("fog", "雾"), 48: ("fog", "雾凇"),
       51: ("drizzle", "小毛毛雨"), 53: ("drizzle", "毛毛雨"), 55: ("drizzle", "大毛毛雨"), 56: ("drizzle", "冻毛毛雨"), 57: ("drizzle", "冻毛毛雨"),
       61: ("rain", "小雨"), 63: ("rain", "中雨"), 65: ("rain", "大雨"), 66: ("rain", "冻雨"), 67: ("rain", "冻雨"),
       71: ("snow", "小雪"), 73: ("snow", "中雪"), 75: ("snow", "大雪"), 77: ("snow", "雪粒"),
       80: ("rain", "阵雨"), 81: ("rain", "阵雨"), 82: ("rain", "强阵雨"), 85: ("snow", "阵雪"), 86: ("snow", "强阵雪"),
       95: ("storm", "雷阵雨"), 96: ("storm", "雷阵雨伴冰雹"), 99: ("storm", "强雷阵雨伴冰雹")}


async def fetch_json(url):
    """JSON from one of the services above; ValueError with a reason the model can relay."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=8), follow_redirects=True) as client:
            response = await client.get(url, headers={"User-Agent": "assistant-cards", "Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise ValueError(f"The data service could not be reached ({type(exc).__name__}). Say so; do not guess the figures.") from exc
    if response.status_code == 429:
        raise ValueError("The data service is rate-limiting requests right now. Say so and suggest trying again in a minute; do not guess the figures.")
    if response.status_code != 200:
        raise ValueError(f"The data service answered {response.status_code}. Say so; do not guess the figures.")
    return response.json()


def number(value, digits=2):
    """12345.678 -> "12,345.68"; small prices keep their significant digits."""
    if abs(value) < 1 and value:
        return f"{value:.6g}"
    text = f"{value:,.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def percent(value):
    return f"{value:+.2f}%"


# The place search knows Chinese cities by their Chinese names, but for cities abroad a Chinese name finds a namesake village or nothing.
ABROAD = {"东京": "Tokyo", "大阪": "Osaka", "京都": "Kyoto", "首尔": "Seoul", "新加坡": "Singapore", "曼谷": "Bangkok", "吉隆坡": "Kuala Lumpur", "雅加达": "Jakarta",
          "马尼拉": "Manila", "河内": "Hanoi", "胡志明市": "Ho Chi Minh City", "新德里": "New Delhi", "孟买": "Mumbai", "迪拜": "Dubai", "伊斯坦布尔": "Istanbul",
          "伦敦": "London", "巴黎": "Paris", "柏林": "Berlin", "法兰克福": "Frankfurt", "慕尼黑": "Munich", "阿姆斯特丹": "Amsterdam", "苏黎世": "Zurich", "日内瓦": "Geneva",
          "罗马": "Rome", "米兰": "Milan", "马德里": "Madrid", "巴塞罗那": "Barcelona", "里斯本": "Lisbon", "维也纳": "Vienna", "莫斯科": "Moscow", "斯德哥尔摩": "Stockholm",
          "纽约": "New York", "洛杉矶": "Los Angeles", "旧金山": "San Francisco", "西雅图": "Seattle", "芝加哥": "Chicago", "波士顿": "Boston", "华盛顿": "Washington",
          "迈阿密": "Miami", "拉斯维加斯": "Las Vegas", "多伦多": "Toronto", "温哥华": "Vancouver", "墨西哥城": "Mexico City", "圣保罗": "Sao Paulo",
          "悉尼": "Sydney", "墨尔本": "Melbourne", "奥克兰": "Auckland", "开罗": "Cairo", "约翰内斯堡": "Johannesburg", "内罗毕": "Nairobi"}


async def locate(place):
    asked = place.strip().removesuffix("市")
    found = (await fetch_json(f"https://geocoding-api.open-meteo.com/v1/search?name={quote(ABROAD.get(asked, place.strip()))}&count=10&language=zh")).get("results")
    if not found:
        raise ValueError(f"No place called {place!r} was found. Ask me which city I mean, or try its name in English.")
    # Many villages share a big city's name: a capital comes first, then the most populous place.
    spot = max(found, key=lambda r: (r.get("feature_code") == "PPLC", r.get("population") or 0))
    region = " · ".join(dict.fromkeys(p for p in (spot.get("admin1"), spot.get("country")) if p and p != spot["name"]))
    # Shown under the name the user knows: the search answers 東京 or 紐約市 for a city asked for in English.
    known = {english.casefold(): chinese for chinese, english in ABROAD.items()}
    return {"name": asked if asked in ABROAD else known.get(asked.casefold(), spot["name"]), "region": region, "latitude": spot["latitude"], "longitude": spot["longitude"], "timezone": spot.get("timezone") or "UTC"}


async def weather(place, days=3):
    spot = await locate(place)
    days = max(3, min(int(days or 3), 7))  # the card always shows a few days ahead, whatever was asked
    data = await fetch_json("https://api.open-meteo.com/v1/forecast?latitude={latitude}&longitude={longitude}".format(**spot) +
                            "&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day"
                            "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
                            f"&timezone=auto&forecast_days={days}")
    now, daily = data["current"], data["daily"]
    icon, sky = WMO.get(now["weather_code"], ("cloud", "多云"))
    if icon == "sun" and not now.get("is_day", 1):
        icon = "moon"
    forecast = []
    for n, day in enumerate(daily["time"]):
        day_icon, day_sky = WMO.get(daily["weather_code"][n], ("cloud", "多云"))
        when = date.fromisoformat(day)
        forecast.append({"date": day, "label": "今天" if n == 0 else "明天" if n == 1 else WEEKDAYS[when.weekday()], "icon": day_icon, "sky": day_sky,
                         "high": round(daily["temperature_2m_max"][n]), "low": round(daily["temperature_2m_min"][n]),
                         "rain_chance": daily["precipitation_probability_max"][n]})
    current = {"temperature": round(now["temperature_2m"]), "feels_like": round(now["apparent_temperature"]), "sky": sky,
               "humidity": now["relative_humidity_2m"], "wind_kmh": round(now["wind_speed_10m"])}
    card = {"kind": "weather", "title": spot["name"], "subtitle": spot["region"], "value": f"{current['temperature']}°", "icon": icon,
            "rows": [{"label": "天气", "value": sky}, {"label": "体感", "value": f"{current['feels_like']}°"},
                     {"label": "湿度", "value": f"{current['humidity']}%"}, {"label": "风", "value": f"{current['wind_kmh']} km/h"}],
            "days": [{k: d[k] for k in ("label", "icon", "high", "low")} for d in forecast], "note": "Open-Meteo · " + now["time"].replace("T", " ")}
    return {"place": spot["name"], "region": spot["region"], "local_time": now["time"], "unit": "°C", "now": current, "days": forecast, "card": card}


async def crypto_price(asset):
    query = asset.strip()
    found = (await fetch_json(f"https://api.coingecko.com/api/v3/search?query={quote(query)}")).get("coins") or []
    # The ticker the user typed wins over a look-alike that merely ranks first in the search.
    exact = [c for c in found if query.casefold() in (c["symbol"].casefold(), c["name"].casefold(), c["id"].casefold())]
    ranked = sorted(exact or found, key=lambda c: c.get("market_cap_rank") or 10 ** 9)
    if not ranked:
        raise ValueError(f"No crypto asset called {asset!r} was found. Ask me which one I mean.")
    rows = await fetch_json(f"https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids={quote(ranked[0]['id'])}&sparkline=true&price_change_percentage=24h,7d")
    if not rows:
        raise ValueError(f"No market data for {ranked[0]['name']}.")
    coin = rows[0]
    week = [p for p in (coin.get("sparkline_in_7d") or {}).get("price") or [] if p is not None]
    series = week[::max(1, len(week) // 48)]
    day, seven = coin.get("price_change_percentage_24h_in_currency") or coin.get("price_change_percentage_24h") or 0, coin.get("price_change_percentage_7d_in_currency")
    result = {"name": coin["name"], "symbol": coin["symbol"].upper(), "price_usd": coin["current_price"], "change_24h_percent": round(day, 2),
              "high_24h": coin.get("high_24h"), "low_24h": coin.get("low_24h"), "market_cap_usd": coin.get("market_cap"), "market_cap_rank": coin.get("market_cap_rank"),
              "as_of": (coin.get("last_updated") or "")[:16].replace("T", " ") + " UTC"}
    rows = [{"label": "24 小时", "value": percent(day)}]
    if seven is not None:
        result["change_7d_percent"] = round(seven, 2)
        rows.append({"label": "7 天", "value": percent(seven)})
    if coin.get("high_24h") and coin.get("low_24h"):
        rows.append({"label": "24h 高 / 低", "value": f"{number(coin['high_24h'])} / {number(coin['low_24h'])}"})
    if coin.get("market_cap"):
        rows.append({"label": "市值", "value": f"${coin['market_cap'] / 1e9:,.1f}B" if coin["market_cap"] >= 1e9 else f"${coin['market_cap'] / 1e6:,.1f}M"})
    result["card"] = {"kind": "price", "title": coin["name"], "subtitle": result["symbol"] + " · USD", "value": "$" + number(coin["current_price"]), "icon": "coin",
                      "trend": "up" if day > 0 else "down" if day < 0 else "flat", "rows": rows, "series": series, "note": "CoinGecko · " + result["as_of"]}
    return result


async def convert_currency(amount, source, target):
    source, target = source.strip().upper(), target.strip().upper()
    if source == target:
        raise ValueError("The two currencies are the same.")
    try:
        data = await fetch_json(f"https://api.frankfurter.dev/v1/latest?base={quote(source)}&symbols={quote(target)}")
    except ValueError as exc:
        if "answered 4" in str(exc):
            raise ValueError(f"{source} or {target} is not a currency code this service knows (it covers about 30 major currencies, by ISO code such as USD, CNY, EUR, JPY).") from exc
        raise
    rate = data["rates"][target]
    converted = round(amount * rate, 2)
    card = {"kind": "currency", "title": f"{number(amount)} {source}", "subtitle": f"{source} → {target}", "value": f"{number(converted)} {target}", "icon": "exchange",
            "rows": [{"label": "汇率", "value": f"1 {source} = {rate:.4f} {target}"}, {"label": "反向", "value": f"1 {target} = {1 / rate:.4f} {source}"}],
            "note": "欧洲央行参考汇率 · " + data["date"]}
    return {"amount": amount, "from": source, "to": target, "rate": rate, "converted": converted, "rate_date": data["date"],
            "note": "ECB reference rate for that date, not a bank's buying or selling rate.", "card": card}


async def world_time(place, now=None):
    """The time in a city or an IANA zone (Asia/Tokyo), and how far it is from the user's own clock."""
    name, region = place.strip(), ""
    try:
        zone = ZoneInfo(name)
        name = name.split("/")[-1].replace("_", " ")
    except (ZoneInfoNotFoundError, ValueError):
        spot = await locate(place)
        name, region, zone = spot["name"], spot["region"], ZoneInfo(spot["timezone"])
    here = (now or datetime.now()).astimezone()
    there = here.astimezone(zone)
    ahead = (there.utcoffset() - here.utcoffset()).total_seconds() / 3600
    gap = "与你同一时区" if not ahead else f"比你{'早' if ahead > 0 else '晚'} {abs(ahead):g} 小时"
    offset = there.strftime("%z")
    card = {"kind": "clock", "title": name, "subtitle": region or str(zone), "value": there.strftime("%H:%M"), "icon": "sun" if 6 <= there.hour < 18 else "moon",
            "rows": [{"label": "日期", "value": f"{there:%m月%d日} {WEEKDAYS[there.weekday()]}"}, {"label": "时差", "value": gap},
                     {"label": "时区", "value": f"UTC{offset[:3]}:{offset[3:]}"}], "note": f"你这里现在 {here:%H:%M}"}
    return {"place": name, "timezone": str(zone), "time": there.strftime("%Y-%m-%d %H:%M"), "weekday": WEEKDAYS[there.weekday()],
            "hours_ahead_of_user": ahead, "user_time": here.strftime("%Y-%m-%d %H:%M"), "card": card}


def countdown(day, label="", today=None):
    """Days from today to a date (YYYY-MM-DD), either way."""
    try:
        target = date.fromisoformat(day.strip())
    except ValueError:
        raise ValueError("date must be YYYY-MM-DD")
    today = today or date.today()
    days = (target - today).days
    label = label.strip() or f"{target:%Y年%m月%d日}"
    words = "就是今天" if not days else f"还有 {days} 天" if days > 0 else f"已过去 {-days} 天"
    weeks = abs(days) // 7
    card = {"kind": "countdown", "title": label, "subtitle": f"{target:%Y-%m-%d} {WEEKDAYS[target.weekday()]}", "value": str(abs(days)) if days else "今天", "icon": "calendar",
            "rows": [{"label": "", "value": words}] + ([{"label": "约", "value": f"{weeks} 周" + (f" {abs(days) % 7} 天" if abs(days) % 7 else "")}] if weeks else []),
            "note": f"从今天 {today:%m月%d日} 算起"}
    return {"date": target.isoformat(), "weekday": WEEKDAYS[target.weekday()], "days": days, "today": today.isoformat(), "card": card}
