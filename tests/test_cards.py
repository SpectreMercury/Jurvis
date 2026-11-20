"""Cards: live lookups (weather, crypto price, exchange rate, time elsewhere, countdown) and the cards left by things the assistant made."""
import json
from datetime import date, datetime, timezone

import httpx
import pytest

from engine import cards
from engine.app import create_app
from engine.config import Settings
from engine.runtime import Completion
from tests.test_agent import ScriptedModel, call, run

PLACES = {"北京": [{"name": "北京", "latitude": 39.9, "longitude": 116.4, "feature_code": "PPLC", "population": 18960744, "country": "中国", "admin1": "北京市", "timezone": "Asia/Shanghai"}],
          "Tokyo": [{"name": "Tokyo", "latitude": -8.4, "longitude": 147.1, "feature_code": "PPL", "country": "巴布亚新几内亚", "timezone": "Pacific/Port_Moresby"},
                    {"name": "東京", "latitude": 35.7, "longitude": 139.7, "feature_code": "PPLC", "population": 9733276, "country": "日本", "admin1": "东京都", "timezone": "Asia/Tokyo"}]}
FORECAST = {"current": {"time": "2026-11-20T14:00", "temperature_2m": 8.4, "apparent_temperature": 2.2, "relative_humidity_2m": 25, "weather_code": 61, "wind_speed_10m": 17.1, "is_day": 1},
            "daily": {"time": ["2026-11-20", "2026-11-21", "2026-11-22"], "weather_code": [61, 0, 71], "temperature_2m_max": [8.0, 5.0, 5.4],
                      "temperature_2m_min": [-1.1, -1.4, -2.1], "precipitation_probability_max": [80, 0, 40]}}
MARKET = [{"id": "ethereum", "symbol": "eth", "name": "Ethereum", "current_price": 1738.54, "high_24h": 1813.98, "low_24h": 1717.73, "market_cap": 212600000000, "market_cap_rank": 2,
           "price_change_percentage_24h": -3.44, "price_change_percentage_24h_in_currency": -3.44, "price_change_percentage_7d_in_currency": -9.66,
           "last_updated": "2026-11-20T06:01:00.000Z", "sparkline_in_7d": {"price": [1900 - n for n in range(168)]}}]


@pytest.fixture
def services(monkeypatch):
    """The data services, answered from fixtures; `asked` collects the URLs requested."""
    asked, down = [], set()

    def handler(request):
        url = str(request.url)
        asked.append(url)
        if any(host in url for host in down):
            return httpx.Response(429 if "coingecko" in url else 500)
        if "geocoding-api" in url:
            return httpx.Response(200, json={"results": PLACES.get(request.url.params["name"], [])} if request.url.params["name"] in PLACES else {})
        if "api.open-meteo.com" in url:
            return httpx.Response(200, json=FORECAST)
        if "/search" in url:
            return httpx.Response(200, json={"coins": [{"id": "ethereum-classic", "symbol": "ETC", "name": "Ethereum Classic", "market_cap_rank": 40},
                                                       {"id": "ethereum", "symbol": "ETH", "name": "Ethereum", "market_cap_rank": 2}] if "ETH" in url else []})
        if "/coins/markets" in url:
            return httpx.Response(200, json=MARKET)
        if "frankfurter" in url:
            if request.url.params["symbols"] == "XXX":
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json={"amount": 1.0, "base": "USD", "date": "2026-11-19", "rates": {"CNY": 6.6667}})
        return httpx.Response(404)
    real = httpx.AsyncClient
    monkeypatch.setattr(cards.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return type("Services", (), {"asked": asked, "down": down})


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data=tmp_path / "data"), run_worker=False)


@pytest.mark.asyncio
async def test_weather_card_has_now_and_the_days_ahead(services):
    result = await cards.weather("北京")
    card = result["card"]
    assert (card["kind"], card["title"], card["subtitle"], card["value"], card["icon"]) == ("weather", "北京", "北京市 · 中国", "8°", "rain")
    assert card["rows"][0] == {"label": "天气", "value": "小雨"} and {"label": "体感", "value": "2°"} in card["rows"]
    assert card["days"] == [{"label": "今天", "icon": "rain", "high": 8, "low": -1}, {"label": "明天", "icon": "sun", "high": 5, "low": -1}, {"label": "周日", "icon": "snow", "high": 5, "low": -2}]
    assert result["now"]["temperature"] == 8 and result["days"][0]["rain_chance"] == 80


@pytest.mark.asyncio
async def test_a_foreign_city_named_in_chinese_finds_the_city_not_a_namesake(services):
    spot = await cards.locate("东京")
    assert "name=Tokyo" in services.asked[0] and (spot["name"], spot["timezone"]) == ("东京", "Asia/Tokyo")  # the capital, not the first hit
    with pytest.raises(ValueError, match="No place"):
        await cards.locate("火星基地")


@pytest.mark.asyncio
async def test_price_card_takes_the_asset_the_ticker_names_and_draws_a_week(services):
    result = await cards.crypto_price("ETH")
    card = result["card"]
    assert "ids=ethereum&" in services.asked[1]  # ETH is Ethereum, not Ethereum Classic that came first in the search
    assert (card["title"], card["subtitle"], card["value"], card["trend"]) == ("Ethereum", "ETH · USD", "$1,738.54", "down")
    assert {"label": "24 小时", "value": "-3.44%"} in card["rows"] and {"label": "市值", "value": "$212.6B"} in card["rows"]
    assert 40 <= len(card["series"]) <= 60 and card["series"][0] > card["series"][-1]
    services.down.add("coingecko")
    with pytest.raises(ValueError, match="rate-limiting.*do not guess"):
        await cards.crypto_price("ETH")


@pytest.mark.asyncio
async def test_currency_card_gives_the_amount_and_both_rates(services):
    result = await cards.convert_currency(100, "usd", "cny")
    assert result["converted"] == 666.67 and result["card"]["value"] == "666.67 CNY" and result["card"]["title"] == "100 USD"
    assert result["card"]["rows"] == [{"label": "汇率", "value": "1 USD = 6.6667 CNY"}, {"label": "反向", "value": "1 CNY = 0.1500 USD"}]
    with pytest.raises(ValueError, match="not a currency code"):
        await cards.convert_currency(1, "USD", "XXX")
    with pytest.raises(ValueError, match="same"):
        await cards.convert_currency(1, "usd", "USD")


@pytest.mark.asyncio
async def test_clock_card_tells_the_time_there_and_the_gap(services):
    noon_utc = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    tokyo = await cards.world_time("东京", now=noon_utc)
    assert tokyo["time"] == "2026-11-20 21:00" and tokyo["card"]["value"] == "21:00" and tokyo["card"]["icon"] == "moon"
    here = noon_utc.astimezone()
    gap = (9 * 3600 - here.utcoffset().total_seconds()) / 3600
    assert tokyo["hours_ahead_of_user"] == gap
    zone = await cards.world_time("America/New_York", now=noon_utc)  # a zone name needs no place lookup
    assert zone["time"] == "2026-11-20 07:00" and zone["card"]["title"] == "New York" and len(services.asked) == 1


def test_countdown_card_counts_both_ways():
    ahead = cards.countdown("2027-02-06", "春节", today=date(2026, 11, 20))
    assert ahead["days"] == 78 and ahead["card"]["value"] == "78" and ahead["card"]["rows"] == [{"label": "", "value": "还有 78 天"}, {"label": "约", "value": "11 周 1 天"}]
    assert cards.countdown("2026-11-20", today=date(2026, 11, 20))["card"]["value"] == "今天"
    assert cards.countdown("2026-11-01", today=date(2026, 11, 20))["card"]["rows"][0]["value"] == "已过去 19 天"
    with pytest.raises(ValueError):
        cards.countdown("明年春节")


@pytest.mark.asyncio
async def test_a_lookup_shows_the_user_a_card_and_the_model_only_the_facts(app, services):
    model = ScriptedModel([
        Completion("", [call("weather", place="北京"), call("crypto_price", asset="ETH"), call("convert_currency", amount=100, source="USD", target="CNY"),
                        call("countdown", date="2030-01-01", label="2030 年")], "tool_calls"),
        Completion("北京今天有小雨，出门带伞。", [], "stop"),
    ])
    _, message = await run(app, model, "北京天气、ETH 价格、100 美元换人民币、离 2030 年还有几天")
    offered = {t["function"]["name"] for t in model.calls[0]["tools"]}
    assert {"weather", "crypto_price", "convert_currency", "world_time", "countdown"} <= offered
    calls = message["metadata"]["steps"][0]["calls"]
    assert [c["card"]["kind"] for c in calls] == ["weather", "price", "currency", "countdown"] and all(c["status"] == "ok" for c in calls)
    assert all("card" not in json.loads(c["output"]) for c in calls)  # the card is for the screen, not for the model
    assert json.loads(calls[0]["output"])["now"]["sky"] == "小雨" and calls[0]["summary"] == "北京 · 8° 小雨"
    assert "show them to the user as a card" in model.calls[0]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_a_failed_lookup_has_no_card_and_tells_the_model_not_to_guess(app, services):
    services.down.add("api.open-meteo.com")
    model = ScriptedModel([Completion("", [call("weather", place="北京")], "tool_calls"), Completion("天气服务暂时连不上。", [], "stop")])
    _, message = await run(app, model, "北京天气")
    failed = message["metadata"]["steps"][0]["calls"][0]
    assert failed["status"] == "error" and "card" not in failed and "do not guess" in failed["output"]


@pytest.mark.asyncio
async def test_things_the_assistant_makes_leave_a_card(app):
    brief = "把今天聊到的三家供应商报价整理成一张对比表，保存为 quotes.md。"
    model = ScriptedModel([
        Completion("", [call("remember", title="过敏", content="用户对花生过敏"),
                        {"id": "call_save", "name": "save_file", "arguments": json.dumps({"name": "note.md", "content": "# 周会\n\n- 下周三上线\n- 预算待批"})},
                        call("start_task", title="供应商报价对比", brief=brief)], "tool_calls"),
        Completion("记下了，也存好了。", [], "stop"),
    ])
    _, message = await run(app, model, "记住我对花生过敏，把周会纪要存成 note.md，再整理一下供应商报价")
    memory, saved, task = (c["card"] for c in message["metadata"]["steps"][0]["calls"])
    assert memory == {"kind": "memory", "title": "过敏", "subtitle": "已记住", "note": "用户对花生过敏"}
    assert (saved["kind"], saved["title"], saved["note"]) == ("file", "note.md", "# 周会 - 下周三上线 - 预算待批") and saved["subtitle"].startswith("文档")
    assert task == {"kind": "task", "title": "供应商报价对比", "subtitle": "后台任务 · 做完发在这里"}


def test_what_to_wear_follows_how_it_feels_and_the_sky():
    snow = cards.outfit(-3, "snow", 10, 0, 1, -6)
    assert snow["art"] == "freezing-snow" and snow["wear"][0] == "羽绒服" and "防滑靴" in snow["wear"] and "雪" in snow["tip"]
    rain = cards.outfit(12, "cloud", 8, 80, 15, 9)  # a cloudy sky with rain very likely is dressed for rain
    assert rain["art"] == "cool-rain" and "雨伞" in rain["wear"] and "umbrella" in rain["art_prompt"]
    assert "防风" in cards.outfit(5, "sun", 35)["tip"] and "温差 12°" in cards.outfit(19, "partly", 5, 0, 24, 12)["tip"]
    assert cards.outfit(33, "sun")["art"] == "hot-clear" and cards.outfit(22, "cloud")["art"] == "mild-cloud" and cards.outfit(23, "moon")["art"] == "warm-clear"
    assert all("Japanese minimalist manga" in cards.outfit(t, "sun")["art_prompt"] for t in (-10, 5, 12, 20, 25, 35))


@pytest.mark.asyncio
async def test_each_kind_of_weather_is_drawn_once_and_the_picture_is_kept_out_of_the_library(app, services, monkeypatch):
    tools, store = app.state.worker.tools, app.state.store
    monkeypatch.setattr(tools, "can_draw", lambda: True)
    model = ScriptedModel([Completion("", [call("weather", place="北京")], "tool_calls"), Completion("有小雨，带伞。", [], "stop")])
    _, message = await run(app, model, "北京天气")
    entry = message["metadata"]["steps"][0]["calls"][0]
    card = entry["card"]
    assert card["outfit"] == {"wear": ["厚外套", "毛衣", "长裤", "雨伞"], "tip": "有雨，带伞，鞋子选不怕湿的"} and "art_prompt" not in card and "art" not in card
    job = store.get_job(card["image_job"])
    assert entry["job_id"] == job["id"] and job["kind"] == "image" and job["payload"]["outfit_art"] == "cold-rain" and "umbrella" in job["payload"]["prompt"]
    assert json.loads(entry["output"])["what_to_wear"]["clothes"] == card["outfit"]["wear"]  # the model is told the same advice the card shows
    # asked again while it is being drawn: the same drawing, not a second one
    again = (await tools.call("weather", {"place": "北京"})).result["card"]
    assert again["image_job"] == job["id"] and len(store.all("SELECT id FROM jobs WHERE kind='image'")) == 1
    # once drawn, the picture is on every later card for this kind of weather
    path = app.state.worker.settings.data / "assets" / "art.png"
    path.write_bytes(b"png")
    asset = app.state.worker.media.record(path, "x", "image/png", "image", job["id"])
    store.update_job(job["id"], status="succeeded", result={"asset_ids": [asset]})
    drawn = (await tools.call("weather", {"place": "北京"})).result["card"]
    assert drawn["image"] == asset and "image_job" not in drawn
    # without the image engine the card simply has no picture
    monkeypatch.setattr(tools, "can_draw", lambda: False)
    plain = (await tools.call("weather", {"place": "北京"})).result["card"]
    assert "image" not in plain and "image_job" not in plain and plain["outfit"]["wear"]
