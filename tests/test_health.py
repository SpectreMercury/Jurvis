"""Health data (Apple Health, WHOOP, Google Health, the cycle) and the companion conversation the assistant opens for what the user is going through."""
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from engine.app import create_app
from engine.config import Settings
from engine.health import Health, span
from engine.health_sync import HealthSync, kind_of
from engine.runtime import Completion
from engine.store import Store
from tests.test_agent import ScriptedModel, call, run

TODAY = date(2026, 11, 20)


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data=tmp_path / "data"), run_worker=False)


@pytest.fixture
def health(tmp_path):
    return Health(Store(tmp_path / "h.db"))


def fill(health, days=35, today=TODAY, source="apple", **change):
    """A month of ordinary days, and the last three days changed as given."""
    for i in range(days):
        d = today - timedelta(i)
        m = {"steps": 8000 + i * 10, "sleep_min": 450, "deep_min": 80, "rem_min": 100, "resting_hr": 58, "hrv_ms": 60, "exercise_min": 30}
        if i < 3:
            m.update(change)
        health.put(source, d, m)


def test_periods_and_their_days():
    assert span("last_7_days", TODAY)[:2] == (date(2026, 11, 14), TODAY)
    assert span("this_week", TODAY)[0] == date(2026, 11, 16) and span("yesterday", TODAY)[0] == date(2026, 11, 19)
    with pytest.raises(ValueError):
        span("forever", TODAY)


def test_summary_adds_up_and_prefers_the_device_made_for_it(health):
    fill(health)
    health.put("whoop", TODAY, {"sleep_min": 300, "recovery": 40, "strain": 12.5})
    health.put("google", TODAY, {"steps": 1})
    health.workout("apple", "w1", datetime(2026, 11, 18, 7).timestamp(), datetime(2026, 11, 18, 7, 45).timestamp(), "running", kcal=420, distance_km=7.2)
    s = health.summary("last_7_days", today=TODAY)
    assert s["days_with_data"] == 7 and s["sleep"]["last_night"] == "5 小时 0 分"  # WHOOP wins for sleep
    assert s["activity"]["steps_average"] == round(sum(8000 + i * 10 for i in range(7)) / 7)  # Apple wins for steps over Google's 1
    assert s["activity"]["by_kind"] == [{"kind": "跑步", "sessions": 1, "minutes": 45}] and s["recovery"]["average"] == 40
    card = s["card"]
    assert card["kind"] == "health" and len(card["series"]) == 7 and card["series"][-1] == 5.0 and {"label": "日均步数", "value": f"{s['activity']['steps_average']:,}"} in card["rows"]
    assert "transactions" not in json.dumps(s) and "cycle" not in s


def test_the_stress_signal_compares_with_the_users_own_baseline(health):
    fill(health, hrv_ms=42, resting_hr=64, sleep_min=320)
    stress = health.summary("last_7_days", "stress", today=TODAY)["stress"]
    assert stress["level"] == "偏高" and len(stress["signs"]) == 3 and "低 30%" in stress["signs"][0] and "不是医学测量" in stress["note"]
    calm = Health(health.store)
    calm.store.execute("DELETE FROM health_daily")
    fill(calm)
    assert calm.summary("last_7_days", "stress", today=TODAY)["stress"]["level"] == "平稳"


def test_no_data_says_where_it_comes_from(health):
    s = health.summary("today", today=TODAY)
    assert s["days_with_data"] == 0 and "Apple Health" in s["note"] and "card" not in s


def test_the_cycle_is_predicted_from_the_users_own_periods(health, monkeypatch):
    for start in ("2026-08-01", "2026-08-30", "2026-09-28", "2026-10-27"):
        first = date.fromisoformat(start)
        for i in range(5):
            health.flow(first + timedelta(i), "medium", ["痛经"] if i == 0 else [])
    c = health.cycle(today=date(2026, 11, 10))
    assert c["periods_recorded"] == 4 and c["average_cycle_days"] == 29 and c["average_period_days"] == 5
    assert c["next_period_estimate"] == "2026-11-25" and c["days_until_next"] == 15 and c["cycle_day_today"] == 15 and c["phase"] == "排卵期附近"
    assert c["common_symptoms_90_days"] == ["痛经"] and c["card"]["kind"] == "cycle" and "避孕" in c["card"]["note"]
    assert health.cycle(today=date(2026, 11, 29))["phase"] == "比平时晚" and health.cycle(today=date(2026, 11, 29))["late_by_days"] == 4
    # a sync does not overwrite what the user said themselves
    assert health.flow(date(2026, 10, 27), "light", source="apple") is False


def test_logging_a_period_and_its_end(health):
    out = health.log_period(str(date.today() - timedelta(2)), "", "heavy", ["腰酸"])
    assert out["recorded"]["flow"] == "大量" and out["periods_recorded"] == 1
    health.log_period(str(date.today() - timedelta(2)), str(date.today()), "medium")
    health.end_period(str(date.today() - timedelta(1)))
    assert health.periods()[-1] == (date.today() - timedelta(2), date.today() - timedelta(1))
    with pytest.raises(ValueError):
        health.log_period(str(date.today() + timedelta(1)))


def test_the_page_and_the_iphone_import(app):
    client, token = TestClient(app), {"Authorization": "Bearer " + app.state.security.local_token}
    today = date.today().isoformat()
    now = int(datetime.now().timestamp()) - 7200
    body = {"source": "apple", "days": [{"day": today, "metrics": {"steps": 9000, "sleep_min": 420, "made_up": 1}}],
            "workouts": [{"id": "HK1", "start": now, "end": now + 1800, "kind": "yoga"}],
            "cycle": [{"day": today, "flow": "light"}], "moods": [{"id": "SoM1", "at": 1_763_600_000, "score": 4}]}
    assert client.post("/api/health/import", json=body, headers=token).json() == {"days": 1, "workouts": 1, "cycle": 1, "moods": 1}
    assert client.post("/api/health/import", json={"source": "whoop", "days": []}, headers=token).status_code == 400
    page = client.get("/api/health", headers=token).json()
    assert page["days"][-1]["steps"] == 9000 and "made_up" not in page["days"][-1] and page["workouts"][0]["kind_label"] == "瑜伽"
    assert page["cycle"]["periods_recorded"] == 1 and {a["provider"] for a in page["accounts"]} == {"whoop", "google"}
    assert client.post("/api/health/period", json={"start": today, "flow": "heavy"}, headers=token).json()["recorded"]["flow"] == "大量"
    assert client.get("/api/health").status_code == 401


def test_connecting_an_account_needs_the_users_own_oauth_app(tmp_path):
    settings = Settings(data=tmp_path / "d", whoop_client_id="cid", whoop_client_secret="sec", oauth_base="https://mac.example.ts.net")
    sync = HealthSync(settings, Health(Store(tmp_path / "h.db")), None)
    with pytest.raises(ValueError, match="JURVIS_GOOGLE_CLIENT_ID"):
        sync.connect("google")
    url = sync.connect("whoop")["url"]
    assert url.startswith("https://api.prod.whoop.com/oauth/oauth2/auth?") and "redirect_uri=https%3A%2F%2Fmac.example.ts.net%2Foauth%2Fwhoop" in url and "offline" in url
    assert [s["configured"] for s in sync.status()] == [True, False]


def test_the_oauth_return_needs_no_token_but_a_fresh_state(app):
    client = TestClient(app)
    page = client.get("/oauth/whoop?code=abc&state=forged")
    assert page.status_code == 400 and "失效" in page.text and "<script" not in page.text


@pytest.mark.asyncio
async def test_whoop_and_google_are_read_into_daily_figures(tmp_path):
    health = Health(Store(tmp_path / "h.db"))
    sync = HealthSync(Settings(data=tmp_path / "d"), health, None)
    end = "2026-11-20T06:30:00.000Z"
    whoop = {
        "recovery": [{"score_state": "SCORED", "created_at": end, "score": {"recovery_score": 71, "resting_heart_rate": 52, "hrv_rmssd_milli": 88.4, "spo2_percentage": 96.1}}],
        "activity/sleep": [{"score_state": "SCORED", "nap": False, "end": end, "score": {"respiratory_rate": 15.2, "sleep_performance_percentage": 90, "stage_summary": {
            "total_in_bed_time_milli": 30_600_000, "total_awake_time_milli": 1_800_000, "total_light_sleep_time_milli": 14_400_000,
            "total_slow_wave_sleep_time_milli": 6_000_000, "total_rem_sleep_time_milli": 7_200_000}}},
                           {"score_state": "SCORED", "nap": True, "end": end, "score": {"stage_summary": {}}}],
        "cycle": [{"score_state": "SCORED", "start": "2026-11-20T06:30:00Z", "score": {"strain": 13.4}}],
        "activity/workout": [{"id": "w-1", "start": "2026-11-19T10:00:00Z", "end": "2026-11-19T10:50:00Z", "sport_name": "running",
                              "score": {"strain": 11, "kilojoule": 2092, "distance_meter": 8000, "average_heart_rate": 150}}],
    }
    pages = []

    def answer(request):
        path = request.url.path.split("/v2/")[-1]
        pages.append((path, request.url.params.get("nextToken")))
        if path == "recovery" and not request.url.params.get("nextToken"):
            return httpx.Response(200, json={"records": whoop[path], "next_token": "p2"})
        return httpx.Response(200, json={"records": [] if request.url.params.get("nextToken") else whoop[path]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
        body = await sync.whoop(http, "t", datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert ("recovery", "p2") in pages  # every page is read
    day = local = datetime.fromisoformat(end.replace("Z", "+00:00")).astimezone().date().isoformat()
    m = next(d["metrics"] for d in body["days"] if d["day"] == local)
    assert m["recovery"] == 71 and m["hrv_ms"] == 88.4 and m["sleep_min"] == 460 and m["deep_min"] == 100 and m["in_bed_min"] == 510 and m["strain"] == 13.4
    assert body["workouts"] == [{"id": "w-1", "start": datetime(2026, 11, 19, 10, tzinfo=timezone.utc).timestamp(), "end": datetime(2026, 11, 19, 10, 50, tzinfo=timezone.utc).timestamp(), "kind": "running", "kcal": 500, "distance_km": 8.0, "avg_hr": 150, "strain": 11}]

    google = {
        "steps": [{"steps": {"interval": {"startTime": "2026-11-19T08:00:00Z"}, "count": "4000"}}, {"steps": {"interval": {"startTime": "2026-11-19T09:00:00Z"}, "count": "2500"}}],
        "sleep": [{"sleep": {"type": "CLASSIC", "interval": {"endTime": end}, "summary": {"minutesAsleep": "421", "minutesAwake": "30", "minutesInSleepPeriod": "451",
                                                                                        "stagesSummary": [{"type": "DEEP", "minutes": "70"}, {"type": "REM", "minutes": "95"}]}}}],
        "daily-resting-heart-rate": [{"dailyRestingHeartRate": {"date": {"year": 2026, "month": 11, "day": 19}, "beatsPerMinute": "57"}}],
        "daily-heart-rate-variability": [{"dailyHeartRateVariability": {"date": {"year": 2026, "month": 11, "day": 19}, "averageHeartRateVariabilityMilliseconds": 41.5}}],
        "exercise": [{"name": "users/me/dataTypes/exercise/dataPoints/e1", "exercise": {"exerciseType": "STRENGTH_TRAINING", "interval": {
            "startTime": "2026-11-19T18:00:00Z", "endTime": "2026-11-19T18:40:00Z"}, "metricsSummary": {"caloriesKcal": 210}}}],
    }

    def answer_google(request):
        kind = request.url.path.split("/dataTypes/")[1].split("/")[0]
        assert "filter" in request.url.params
        return httpx.Response(200, json={"dataPoints": google[kind]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(answer_google)) as http:
        body = await sync.google(http, "t", datetime(2026, 11, 1, tzinfo=timezone.utc))
    days = {d["day"]: d["metrics"] for d in body["days"]}
    nineteen = datetime(2026, 11, 19, 8, tzinfo=timezone.utc).astimezone().date().isoformat()
    assert days[nineteen]["steps"] == 6500 and days["2026-11-19"]["resting_hr"] == 57 and days["2026-11-19"]["hrv_ms"] == 41.5
    assert days[local]["sleep_min"] == 421 and days[local]["deep_min"] == 70
    assert body["workouts"][0]["kind"] == "strength" and body["workouts"][0]["kcal"] == 210
    health.ingest("google", body)
    assert health.sources()[0]["name"] == "Google Health"


def test_sport_names_become_kinds():
    assert kind_of("Running") == "running" and kind_of("Functional Fitness") == "strength" and kind_of("Spin") == "cycling" and kind_of("Sauna") == "other"


@pytest.mark.asyncio
async def test_asking_about_sleep_shows_a_health_card(app):
    fill(app.state.worker.tools.health, today=date.today())
    model = ScriptedModel([Completion("", [call("health_summary", period="last_7_days", focus="sleep")], "tool_calls"), Completion("这周平均睡了 7 个半小时。", [], "stop")])
    _, message = await run(app, model, "我这周睡得怎么样")
    got = message["metadata"]["steps"][0]["calls"][0]
    assert got["card"]["kind"] == "health" and json.loads(got["output"])["sleep"]["average"] == "7 小时 30 分"
    offered = {t["function"]["name"] for t in model.calls[0]["tools"]}
    assert {"health_summary", "cycle_status", "log_period", "end_period", "log_mood", "open_session"} <= offered
    assert "never a method of contraception" in model.calls[0]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_a_cloud_model_never_sees_the_cycle(app):
    worker, store = app.state.worker, app.state.store
    worker.tools.health.flow(date.today() - timedelta(3), "medium", ["偏头痛"])
    local = ScriptedModel([Completion("", [call("cycle_status")], "tool_calls"), Completion("周期第 4 天。", [], "stop")])
    _, message = await run(app, local, "我的经期到哪了")
    assert "偏头痛" in message["metadata"]["steps"][0]["calls"][0]["output"]
    sent = []

    async def stream(messages, *, model=None, tools=None, **options):
        sent.append({"messages": messages, "tools": tools})
        if len(sent) == 1:
            return Completion("", [call("cycle_status"), call("health_summary")], "tool_calls")
        return Completion("经期记录在健康页里看。", [], "stop")
    worker.agent.llm = SimpleNamespace(chat_model=lambda m=None: "miavo:x", is_local=lambda m=None: m in (None, "local"), searches=lambda m=None: False, stream=stream)
    store.execute("UPDATE sessions SET model='miavo:x' WHERE id='s1'")
    _, message = await run(app, local, "下次什么时候来")
    offered = {t["function"]["name"] for t in sent[0]["tools"]}
    assert "cycle_status" not in offered and {"health_summary", "log_period"} <= offered
    everything = json.dumps([s["messages"] for s in sent], ensure_ascii=False)
    assert "偏头痛" not in everything and "cycle record" in everything
    assert message["metadata"]["steps"][0]["calls"][0]["status"] == "error"


@pytest.mark.asyncio
async def test_sharing_something_heavy_opens_a_companion_conversation(app):
    store = app.state.store
    model = ScriptedModel([
        Completion("", [call("open_session", title="最近的压力", brief="用户说工作连着加班一个月，觉得撑不住了，和男朋友也老吵架。",
                             opening="连着一个月加班，回家还要吵架，这真的很耗人。想先从哪一件说起都可以。")], "tool_calls"),
        Completion("我给这件事开了一个安静的对话，我们去那边慢慢聊。", [], "stop"),
    ])
    _, message = await run(app, model, "帮我查下明天天气。唉其实我最近真的撑不住了，工作和感情都一团糟")
    assert "open_session" in {t["function"]["name"] for t in model.calls[0]["tools"]}
    assert "do not open a new conversation" in model.calls[0]["messages"][0]["content"]
    opened = message["metadata"]["steps"][0]["calls"][0]
    new = opened["card"]["session_id"]
    assert opened["card"]["kind"] == "session" and message["metadata"]["switch_session"] == new
    session = store.one("SELECT * FROM sessions WHERE id=?", (new,))
    assert session["persona"] == "companion" and session["parent_id"] == "s1" and "加班" in session["brief"]
    first = store.all("SELECT role,content FROM messages WHERE session_id=?", (new,))
    assert first == [{"role": "assistant", "content": "连着一个月加班，回家还要吵架，这真的很耗人。想先从哪一件说起都可以。"}]

    # in the companion conversation: its own way of talking, no option buttons, no opening yet another one
    store.add_message(new, "user", "主要是觉得自己很没用")
    mid = store.add_message(new, "assistant", "", "queued")
    companion = ScriptedModel([Completion("听起来你在怪自己。是哪件事让你最这么觉得？", [], "stop")])
    app.state.worker.agent.runtime = companion
    await app.state.worker.agent.run(store.create_job("chat", {"session_id": new, "message_id": mid}))
    system = companion.calls[0]["messages"][0]["content"]
    offered = {t["function"]["name"] for t in companion.calls[0]["tools"]}
    assert "Companion conversation" in system and "加班" in system and "12356" in system
    assert not {"open_session", "ask_user", "start_task"} & offered and "health_summary" in offered
