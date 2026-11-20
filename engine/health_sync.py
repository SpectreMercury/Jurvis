"""WHOOP and Google Health: the user connects an account once in the browser (OAuth), and the engine pulls their recent
days every hour into engine/health.py. Apple Health has no web API: the iPhone app reads HealthKit and posts to /api/health/import.

Tokens live in data/security/health.json (owner-only). The browser comes back to /oauth/{provider}, which is the one
health address that needs no device token; a single-use state ties it to the connect request.
"""
import json
import logging
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

log = logging.getLogger("jurvis.health")

PROVIDERS = {
    "whoop": {
        "name": "WHOOP",
        "auth": "https://api.prod.whoop.com/oauth/oauth2/auth",
        "token": "https://api.prod.whoop.com/oauth/oauth2/token",
        "scope": "offline read:recovery read:cycles read:sleep read:workout read:body_measurement",
        "console": "https://developer-dashboard.whoop.com",
    },
    "google": {
        "name": "Google Health",
        "auth": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "scope": " ".join("https://www.googleapis.com/auth/googlehealth." + s for s in
                          ("activity_and_fitness.readonly", "health_metrics_and_measurements.readonly", "sleep.readonly")),
        "console": "https://console.cloud.google.com/apis/credentials",
    },
}
WHOOP_API = "https://api.prod.whoop.com/developer/v2"
GOOGLE_API = "https://health.googleapis.com/v4/users/me/dataTypes"
SYNC_EVERY = 3600
FIRST_DAYS = 30
AGAIN_DAYS = 4
KINDS = [("running", ("run", "跑")), ("walking", ("walk", "步行")), ("cycling", ("cycl", "bike", "spin", "骑")), ("swimming", ("swim", "游泳")),
         ("strength", ("weight", "strength", "functional", "力量", "powerlifting", "crossfit")), ("yoga", ("yoga", "瑜伽")), ("hiit", ("hiit", "interval")),
         ("hiking", ("hik", "徒步")), ("rowing", ("row",)), ("elliptical", ("elliptical",)), ("pilates", ("pilates",)), ("dance", ("danc",)),
         ("tennis", ("tennis",)), ("basketball", ("basketball",)), ("football", ("soccer", "football")), ("badminton", ("badminton",)), ("climbing", ("climb",))]


def kind_of(name):
    low = (name or "").lower()
    return next((k for k, words in KINDS if any(w in low for w in words)), "other")


def local_day(stamp):
    """An RFC 3339 time -> the user's local date."""
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().date().isoformat()


def epoch(stamp):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


class HealthSync:
    def __init__(self, settings, health, store):
        self.settings, self.health, self.store = settings, health, store
        self.path = settings.data / "security" / "health.json"
        self.pending = {}  # state -> (provider, created)

    # ---- tokens ----
    def load(self):
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def client(self, provider):
        cid = getattr(self.settings, f"{provider}_client_id", "")
        secret = getattr(self.settings, f"{provider}_client_secret", "")
        return cid, secret

    def redirect(self, provider):
        base = (self.settings.oauth_base or getattr(self.settings, "public_url", "") or f"https://localhost:{self.settings.port}").rstrip("/")
        return f"{base}/oauth/{provider}"

    def status(self):
        tokens = self.load()
        out = []
        for key, p in PROVIDERS.items():
            cid, secret = self.client(key)
            t = tokens.get(key, {})
            out.append({"provider": key, "name": p["name"], "configured": bool(cid and secret), "connected": bool(t.get("refresh_token") or t.get("access_token")),
                        "synced_at": t.get("synced_at"), "error": t.get("error"), "redirect_uri": self.redirect(key), "console": p["console"]})
        return out

    def connect(self, provider):
        if provider not in PROVIDERS:
            raise ValueError("unknown provider")
        cid, secret = self.client(provider)
        if not (cid and secret):
            raise ValueError(f"先在 .env 里填 JURVIS_{provider.upper()}_CLIENT_ID 和 JURVIS_{provider.upper()}_CLIENT_SECRET，回调地址填 {self.redirect(provider)}")
        state = secrets.token_urlsafe(24)
        now = time.time()
        self.pending = {s: v for s, v in self.pending.items() if now - v[1] < 900}
        self.pending[state] = (provider, now)
        query = {"response_type": "code", "client_id": cid, "redirect_uri": self.redirect(provider), "scope": PROVIDERS[provider]["scope"], "state": state}
        if provider == "google":
            query.update(access_type="offline", prompt="consent", include_granted_scopes="true")
        return {"url": PROVIDERS[provider]["auth"] + "?" + urlencode(query)}

    async def finish(self, provider, code, state):
        """The browser came back with a code: trade it for tokens and pull the first month."""
        held = self.pending.pop(state, None)
        if not held or held[0] != provider or time.time() - held[1] > 900:
            raise ValueError("这个连接链接已失效，请回到 Jurvis 重新点连接")
        cid, secret = self.client(provider)
        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.post(PROVIDERS[provider]["token"], data={"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect(provider),
                                                                    "client_id": cid, "client_secret": secret})
        if r.status_code >= 400:
            raise ValueError(f"{PROVIDERS[provider]['name']} 拒绝了授权：{r.text[:200]}")
        tokens = self.load()
        tokens[provider] = {**self.stamp(r.json()), "connected_at": time.time()}
        self.save(tokens)
        await self.sync(provider)

    def disconnect(self, provider):
        tokens = self.load()
        tokens.pop(provider, None)
        self.save(tokens)

    @staticmethod
    def stamp(body):
        return {"access_token": body["access_token"], "refresh_token": body.get("refresh_token"), "expires_at": time.time() + float(body.get("expires_in", 3600)) - 60}

    async def token(self, provider, http):
        tokens = self.load()
        t = tokens.get(provider) or {}
        if t.get("access_token") and t.get("expires_at", 0) > time.time():
            return t["access_token"]
        if not t.get("refresh_token"):
            raise ValueError("not connected")
        cid, secret = self.client(provider)
        data = {"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": cid, "client_secret": secret}
        if provider == "whoop":
            data["scope"] = "offline"
        r = await http.post(PROVIDERS[provider]["token"], data=data)
        if r.status_code >= 400:
            t["error"] = "授权已失效，请重新连接"
            tokens[provider] = t
            self.save(tokens)
            raise ValueError(t["error"])
        fresh = self.stamp(r.json())
        fresh["refresh_token"] = fresh["refresh_token"] or t["refresh_token"]  # Google keeps the old refresh token
        tokens[provider] = {**t, **fresh, "error": None}
        self.save(tokens)
        return fresh["access_token"]

    # ---- syncing ----
    async def tick(self):
        """Called by the worker: each connected account is pulled about once an hour."""
        tokens = self.load()
        for provider, t in tokens.items():
            if provider in PROVIDERS and t.get("refresh_token") and time.time() - (t.get("synced_at") or 0) > SYNC_EVERY:
                try:
                    await self.sync(provider)
                except Exception as exc:
                    log.warning("Health sync from %s failed: %s", provider, exc)

    async def sync(self, provider):
        tokens = self.load()
        first = not tokens.get(provider, {}).get("synced_at")
        since = datetime.now(timezone.utc) - timedelta(days=FIRST_DAYS if first else AGAIN_DAYS)
        async with httpx.AsyncClient(timeout=45) as http:
            token = await self.token(provider, http)
            body = await (self.whoop(http, token, since) if provider == "whoop" else self.google(http, token, since))
        counts = self.health.ingest(provider, body)
        tokens = self.load()
        tokens.setdefault(provider, {}).update(synced_at=time.time(), error=None)
        self.save(tokens)
        return counts

    async def pages(self, http, url, token, params, key, cursor, next_key, limit=40):
        out, params = [], dict(params)
        for _ in range(limit):
            r = await http.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
            if r.status_code >= 400:
                raise ValueError(f"{url.rsplit('/', 1)[-1]}: HTTP {r.status_code} {r.text[:200]}")
            data = r.json()
            out.extend(data.get(key) or [])
            nxt = data.get(next_key)
            if not nxt:
                break
            params[cursor] = nxt
        return out

    async def whoop(self, http, token, since):
        params = {"start": since.isoformat().replace("+00:00", "Z"), "limit": 25}
        get = lambda path: self.pages(http, f"{WHOOP_API}/{path}", token, params, "records", "nextToken", "next_token")
        days = {}
        put = lambda day, **m: days.setdefault(day, {}).update({k: v for k, v in m.items() if v is not None})
        for rec in await get("recovery"):
            score = rec.get("score") or {}
            if rec.get("score_state") == "SCORED" and rec.get("created_at"):
                put(local_day(rec["created_at"]), recovery=score.get("recovery_score"), resting_hr=score.get("resting_heart_rate"),
                    hrv_ms=score.get("hrv_rmssd_milli"), spo2=score.get("spo2_percentage"))
        for rec in await get("activity/sleep"):
            score = rec.get("score") or {}
            stages = score.get("stage_summary") or {}
            if rec.get("nap") or rec.get("score_state") != "SCORED" or not rec.get("end"):
                continue
            ms = lambda k: (stages.get(k) or 0) / 60000
            asleep = ms("total_light_sleep_time_milli") + ms("total_slow_wave_sleep_time_milli") + ms("total_rem_sleep_time_milli")
            put(local_day(rec["end"]), sleep_min=round(asleep), deep_min=round(ms("total_slow_wave_sleep_time_milli")), rem_min=round(ms("total_rem_sleep_time_milli")),
                awake_min=round(ms("total_awake_time_milli")), in_bed_min=round(ms("total_in_bed_time_milli")),
                sleep_score=score.get("sleep_performance_percentage"), resp_rate=score.get("respiratory_rate"))
        for rec in await get("cycle"):
            score = rec.get("score") or {}
            if rec.get("score_state") == "SCORED" and rec.get("start"):
                put(local_day(rec["start"]), strain=score.get("strain"))
        workouts = []
        for rec in await get("activity/workout"):
            score = rec.get("score") or {}
            if not (rec.get("start") and rec.get("end")):
                continue
            workouts.append({"id": rec["id"], "start": epoch(rec["start"]), "end": epoch(rec["end"]), "kind": kind_of(rec.get("sport_name")),
                             "kcal": round(score["kilojoule"] / 4.184) if score.get("kilojoule") else None,
                             "distance_km": round(score["distance_meter"] / 1000, 2) if score.get("distance_meter") else None,
                             "avg_hr": score.get("average_heart_rate"), "strain": score.get("strain")})
        return {"days": [{"day": d, "metrics": m} for d, m in days.items()], "workouts": workouts}

    async def google(self, http, token, since):
        start = since.astimezone().date()
        civil = lambda kind: f'{kind}.interval.civil_start_time >= "{start.isoformat()}"'
        get = lambda kind, flt, size=10000: self.pages(http, f"{GOOGLE_API}/{kind}/dataPoints", token, {"filter": flt, "pageSize": size},
                                                       "dataPoints", "pageToken", "nextPageToken")
        days = {}
        put = lambda day, **m: days.setdefault(day, {}).update({k: v for k, v in m.items() if v is not None})
        num = lambda v: float(v) if v not in (None, "") else None
        steps = {}
        for p in await get("steps", civil("steps")):
            s = p.get("steps") or {}
            when = (s.get("interval") or {}).get("startTime")
            if when:
                steps[local_day(when)] = steps.get(local_day(when), 0) + int(s.get("count") or 0)
        for day, count in steps.items():
            put(day, steps=count)
        for p in await get("sleep", f'sleep.interval.end_time >= "{since.isoformat().replace("+00:00", "Z")}"', 25):
            s = p.get("sleep") or {}
            summary, end = s.get("summary") or {}, (s.get("interval") or {}).get("endTime")
            if not end or s.get("type") == "NAP":
                continue
            stage = {x.get("type"): num(x.get("minutes")) for x in summary.get("stagesSummary") or []}
            put(local_day(end), sleep_min=num(summary.get("minutesAsleep")), awake_min=num(summary.get("minutesAwake")),
                in_bed_min=num(summary.get("minutesInSleepPeriod")), deep_min=stage.get("DEEP"), rem_min=stage.get("REM"))
        for p in await get("daily-resting-heart-rate", f'dailyRestingHeartRate.date >= "{start.isoformat()}"'):
            r = p.get("dailyRestingHeartRate") or {}
            d = r.get("date") or {}
            if d.get("year"):
                put(date(d["year"], d["month"], d["day"]).isoformat(), resting_hr=num(r.get("beatsPerMinute")))
        for p in await get("daily-heart-rate-variability", f'dailyHeartRateVariability.date >= "{start.isoformat()}"'):
            r = p.get("dailyHeartRateVariability") or {}
            d = r.get("date") or {}
            if d.get("year"):
                put(date(d["year"], d["month"], d["day"]).isoformat(), hrv_ms=num(r.get("averageHeartRateVariabilityMilliseconds")))
        workouts = []
        for p in await get("exercise", civil("exercise"), 25):
            e = p.get("exercise") or {}
            interval, metrics = e.get("interval") or {}, e.get("metricsSummary") or {}
            if not (interval.get("startTime") and interval.get("endTime")):
                continue
            workouts.append({"id": p.get("name") or interval["startTime"], "start": epoch(interval["startTime"]), "end": epoch(interval["endTime"]),
                             "kind": kind_of(e.get("exerciseType") or e.get("displayName")), "kcal": metrics.get("caloriesKcal"),
                             "distance_km": round(metrics["distanceMillimeters"] / 1e6, 2) if metrics.get("distanceMillimeters") else None,
                             "avg_hr": num(metrics.get("averageHeartRateBeatsPerMinute"))})
        return {"days": [{"day": d, "metrics": m} for d, m in days.items()], "workouts": workouts}
