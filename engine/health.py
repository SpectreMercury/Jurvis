"""The user's health data: sleep, activity, heart, recovery, mood and their menstrual cycle, from Apple Health, WHOOP,
Google Health and what they tell the assistant.

Privacy is a property of this module's shape, not of a prompt:
- Everything is stored on this Mac. Sources write daily figures (one value per day, metric and source), workouts and cycle days.
- What any model, cloud ones included, may see is summary(): averages and totals for a period, never a single reading.
- The cycle (period days, symptoms, predictions) is for the local model only: cycle() is never offered to a cloud model.
- Every figure is computed here. The model quotes results; it never does the arithmetic.
"""
import json
import time
from datetime import date, datetime, timedelta
from statistics import mean

from .store import new_id

# Daily metrics and their units. A source writes any subset of them for a day.
METRICS = {
    "steps": "步", "active_kcal": "千卡", "exercise_min": "分钟", "distance_km": "公里",
    "sleep_min": "分钟", "deep_min": "分钟", "rem_min": "分钟", "awake_min": "分钟", "in_bed_min": "分钟", "sleep_score": "%",
    "resting_hr": "次/分", "hrv_ms": "毫秒", "spo2": "%", "resp_rate": "次/分", "skin_temp_delta": "°C",
    "recovery": "%", "strain": "", "weight_kg": "公斤", "mindful_min": "分钟", "mood": "",
}
# Which source wins when several report the same metric for the same day: the device made for it first.
PREFER = {"recovery": ["whoop"], "strain": ["whoop"], "sleep_min": ["whoop", "apple", "google"], "hrv_ms": ["whoop", "apple", "google"],
          "resting_hr": ["whoop", "apple", "google"], "steps": ["apple", "google", "whoop"], "mood": ["manual", "apple", "google"]}
ORDER = ["manual", "whoop", "apple", "google"]
SOURCES = {"apple": "Apple 健康", "whoop": "WHOOP", "google": "Google Health", "manual": "手动记录"}
FLOWS = {"none": "无", "spotting": "点滴出血", "light": "少量", "medium": "中等", "heavy": "大量", "unspecified": "有"}
PERIODS = {"today": "今天", "yesterday": "昨天", "last_7_days": "最近 7 天", "last_14_days": "最近 14 天", "last_30_days": "最近 30 天", "this_week": "本周", "this_month": "本月"}
WORKOUTS = {"running": "跑步", "walking": "步行", "cycling": "骑行", "swimming": "游泳", "strength": "力量训练", "yoga": "瑜伽", "hiit": "HIIT",
            "hiking": "徒步", "rowing": "划船", "elliptical": "椭圆机", "dance": "舞蹈", "pilates": "普拉提", "tennis": "网球", "basketball": "篮球",
            "football": "足球", "badminton": "羽毛球", "climbing": "攀岩", "other": "其他运动"}


def day_of(value):
    """A date, a datetime, an ISO string or a timestamp -> "YYYY-MM-DD"."""
    if isinstance(value, (int, float)):
        return date.fromtimestamp(value).isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(str(value)[:10]).isoformat()


def span(period, today=None):
    """(first day, last day, label) of a named period, both inclusive."""
    today = today or date.today()
    if period == "today":
        return today, today, PERIODS[period]
    if period == "yesterday":
        return today - timedelta(1), today - timedelta(1), PERIODS[period]
    if period == "this_week":
        return today - timedelta(today.weekday()), today, PERIODS[period]
    if period == "this_month":
        return today.replace(day=1), today, PERIODS[period]
    days = {"last_7_days": 7, "last_14_days": 14, "last_30_days": 30}.get(period)
    if not days:
        raise ValueError("period must be one of " + ", ".join(PERIODS))
    return today - timedelta(days - 1), today, PERIODS[period]


def hours(minutes):
    return f"{int(minutes // 60)} 小时 {int(round(minutes % 60))} 分" if minutes >= 60 else f"{int(round(minutes))} 分钟"


def avg(values, digits=1):
    values = [v for v in values if v is not None]
    return round(mean(values), digits) if values else None


class Health:
    def __init__(self, store):
        self.store = store
        with store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS health_daily(
                    day TEXT NOT NULL,source TEXT NOT NULL,metric TEXT NOT NULL,value REAL NOT NULL,updated_at REAL NOT NULL,
                    PRIMARY KEY(day,source,metric));
                CREATE TABLE IF NOT EXISTS health_workouts(
                    id TEXT PRIMARY KEY,source TEXT NOT NULL,start REAL NOT NULL,end REAL NOT NULL,kind TEXT NOT NULL,
                    minutes REAL NOT NULL,kcal REAL,distance_km REAL,avg_hr REAL,strain REAL,note TEXT NOT NULL DEFAULT '');
                CREATE INDEX IF NOT EXISTS health_workouts_start ON health_workouts(start);
                CREATE TABLE IF NOT EXISTS cycle_days(
                    day TEXT PRIMARY KEY,flow TEXT NOT NULL,symptoms TEXT NOT NULL DEFAULT '[]',note TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'manual',updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS moods(
                    id TEXT PRIMARY KEY,at REAL NOT NULL,score INTEGER NOT NULL,note TEXT NOT NULL DEFAULT '',source TEXT NOT NULL DEFAULT 'manual');
            """)

    # ---- writing ----
    def put(self, source, day, metrics):
        """Daily figures from one source; a later sync of the same day replaces them."""
        now, day = time.time(), day_of(day)
        rows = [(day, source, k, float(v), now) for k, v in (metrics or {}).items() if k in METRICS and v is not None]
        with self.store.connection() as db:
            db.executemany("INSERT INTO health_daily(day,source,metric,value,updated_at) VALUES(?,?,?,?,?) "
                           "ON CONFLICT(day,source,metric) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", rows)
        return len(rows)

    def workout(self, source, ref, start, end, kind, kcal=None, distance_km=None, avg_hr=None, strain=None, note=""):
        kind = kind if kind in WORKOUTS else "other"
        minutes = max(0.0, (end - start) / 60)
        with self.store.connection() as db:
            db.execute("INSERT INTO health_workouts(id,source,start,end,kind,minutes,kcal,distance_km,avg_hr,strain,note) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                       "ON CONFLICT(id) DO UPDATE SET start=excluded.start,end=excluded.end,kind=excluded.kind,minutes=excluded.minutes,kcal=excluded.kcal,"
                       "distance_km=excluded.distance_km,avg_hr=excluded.avg_hr,strain=excluded.strain",
                       (f"{source}:{ref}", source, start, end, kind, round(minutes, 1), kcal, distance_km, avg_hr, strain, note))

    def flow(self, day, flow, symptoms=(), note="", source="manual"):
        if flow not in FLOWS:
            raise ValueError("flow must be one of " + ", ".join(FLOWS))
        day = day_of(day)
        with self.store.connection() as db:
            old = db.execute("SELECT source,symptoms,note FROM cycle_days WHERE day=?", (day,)).fetchone()
            if old and old["source"] == "manual" and source != "manual":
                return False  # what the user told us themselves is not overwritten by a sync
            keep = json.loads(old["symptoms"]) if old and not symptoms else list(symptoms)
            db.execute("INSERT INTO cycle_days(day,flow,symptoms,note,source,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET "
                       "flow=excluded.flow,symptoms=excluded.symptoms,note=excluded.note,source=excluded.source,updated_at=excluded.updated_at",
                       (day, flow, json.dumps(keep, ensure_ascii=False), note or (old["note"] if old else ""), source, time.time()))
        return True

    def ingest(self, source, body):
        """One sync from a device or service: {"days": [{"day", "metrics"}], "workouts": [...], "cycle": [...], "moods": [...]}."""
        if source not in SOURCES:
            raise ValueError("unknown source")
        counts = {"days": 0, "workouts": 0, "cycle": 0, "moods": 0}
        for item in body.get("days", [])[:400]:
            counts["days"] += bool(self.put(source, item["day"], item.get("metrics", {})))
        for w in body.get("workouts", [])[:500]:
            self.workout(source, str(w["id"]), float(w["start"]), float(w["end"]), str(w.get("kind", "other")), w.get("kcal"), w.get("distance_km"), w.get("avg_hr"), w.get("strain"))
            counts["workouts"] += 1
        for c in body.get("cycle", [])[:400]:
            counts["cycle"] += bool(self.flow(c["day"], c.get("flow", "unspecified"), c.get("symptoms", []), "", source))
        for m in body.get("moods", [])[:400]:
            ref = f"{source}:{m['id']}"
            with self.store.connection() as db:
                db.execute("INSERT OR REPLACE INTO moods(id,at,score,note,source) VALUES(?,?,?,?,?)", (ref, float(m["at"]), int(m["score"]), "", source))
            counts["moods"] += 1
        self.store.event("health", source)
        return counts

    def mood(self, score, note=""):
        score = max(1, min(5, int(score)))
        ident = new_id()
        with self.store.connection() as db:
            db.execute("INSERT INTO moods(id,at,score,note,source) VALUES(?,?,?,?,?)", (ident, time.time(), score, note[:200], "manual"))
        self.store.event("health", "manual")
        return {"recorded": {"score": score, "note": note[:200]}, "week": self.mood_week()}

    def mood_week(self):
        since = time.time() - 7 * 86400
        rows = self.store.all("SELECT score FROM moods WHERE at>=?", (since,))
        return {"entries": len(rows), "average": avg([r["score"] for r in rows])}

    # ---- reading ----
    def daily(self, first, last):
        """{day: {metric: value}} with each metric taken from the preferred source for that day."""
        rows = self.store.all("SELECT day,source,metric,value FROM health_daily WHERE day>=? AND day<=?", (first.isoformat(), last.isoformat()))
        found = {}
        for r in rows:
            found.setdefault(r["day"], {}).setdefault(r["metric"], {})[r["source"]] = r["value"]
        out = {}
        for day, metrics in found.items():
            for metric, by_source in metrics.items():
                order = PREFER.get(metric, []) + ORDER
                source = next(s for s in order + list(by_source) if s in by_source)
                out.setdefault(day, {})[metric] = by_source[source]
        return out

    def sources(self):
        rows = self.store.all("SELECT source,MAX(day) AS last,MAX(updated_at) AS synced FROM health_daily GROUP BY source")
        return [{"source": r["source"], "name": SOURCES.get(r["source"], r["source"]), "last_day": r["last"], "synced_at": r["synced"]} for r in rows]

    def stress(self, days, today):
        """A body-stress signal from HRV and resting heart rate against the user's own 30-day baseline, plus sleep. An estimate, not a measurement."""
        base = self.daily(today - timedelta(30), today - timedelta(4))
        recent = {d: m for d, m in days.items() if d >= (today - timedelta(2)).isoformat()}
        hrv_base, rhr_base = avg([m.get("hrv_ms") for m in base.values()]), avg([m.get("resting_hr") for m in base.values()])
        hrv_now, rhr_now = avg([m.get("hrv_ms") for m in recent.values()]), avg([m.get("resting_hr") for m in recent.values()])
        sleep_now = avg([m.get("sleep_min") for m in recent.values()], 0)
        signs = []
        if hrv_base and hrv_now and len(base) >= 7:
            change = (hrv_now - hrv_base) / hrv_base * 100
            if change <= -15:
                signs.append(f"心率变异性比你平时低 {abs(change):.0f}%")
        if rhr_base and rhr_now and len(base) >= 7 and rhr_now - rhr_base >= 3:
            signs.append(f"静息心率比平时高 {rhr_now - rhr_base:.0f} 次/分")
        if sleep_now and sleep_now < 360:
            signs.append(f"最近几晚平均只睡了 {hours(sleep_now)}")
        if not (hrv_now or rhr_now or sleep_now):
            return None
        level = "偏高" if len(signs) >= 2 else "略高" if signs else "平稳"
        return {"level": level, "signs": signs, "hrv_ms": hrv_now, "hrv_baseline_ms": hrv_base, "resting_hr": rhr_now, "resting_hr_baseline": rhr_base,
                "note": "估算：根据心率变异性、静息心率和睡眠与你自己 30 天的平均相比，不是医学测量"}

    def summary(self, period="last_7_days", focus="all", today=None):
        """Averages and totals for a period. What any model may see: no single reading, no cycle."""
        today = today or date.today()
        first, last, label = span(period, today)
        days = self.daily(first, last)
        count = (last - first).days + 1
        values = lambda key: [m[key] for m in days.values() if key in m]
        sleep = values("sleep_min")
        out = {"period": label, "from": first.isoformat(), "to": last.isoformat(), "days_with_data": len(days),
               "sources": [s["name"] for s in self.sources()]}
        if not days:
            out["note"] = "No health data for this period yet. The user connects Apple Health in the iPhone app, or WHOOP / Google Health on the 健康 page."
            return out
        if focus in ("all", "sleep") and sleep:
            out["sleep"] = {"average": hours(mean(sleep)), "average_minutes": round(mean(sleep)), "nights": len(sleep),
                            "deep_average_minutes": avg(values("deep_min"), 0), "rem_average_minutes": avg(values("rem_min"), 0),
                            "short_nights_under_6h": sum(1 for s in sleep if s < 360), "score_average": avg(values("sleep_score"), 0)}
            last_night = days.get(last.isoformat(), {}).get("sleep_min")
            if last_night:
                out["sleep"]["last_night"] = hours(last_night)
        if focus in ("all", "activity"):
            steps = values("steps")
            workouts = self.store.all("SELECT kind,minutes,kcal FROM health_workouts WHERE start>=? AND start<?",
                                      (datetime.combine(first, datetime.min.time()).timestamp(), datetime.combine(last + timedelta(1), datetime.min.time()).timestamp()))
            kinds = {}
            for w in workouts:
                k = kinds.setdefault(WORKOUTS.get(w["kind"], "其他运动"), {"sessions": 0, "minutes": 0})
                k["sessions"] += 1; k["minutes"] += round(w["minutes"])
            out["activity"] = {"steps_average": round(mean(steps)) if steps else None, "steps_total": round(sum(steps)) if steps else None,
                               "exercise_minutes_total": round(sum(values("exercise_min"))) or None, "active_kcal_average": avg(values("active_kcal"), 0),
                               "workouts": len(workouts), "workout_minutes": round(sum(w["minutes"] for w in workouts)),
                               "by_kind": [{"kind": k, **v} for k, v in sorted(kinds.items(), key=lambda x: -x[1]["minutes"])],
                               "strain_average": avg(values("strain"))}
        if focus in ("all", "heart", "stress", "recovery"):
            out["heart"] = {"resting_hr_average": avg(values("resting_hr"), 0), "hrv_average_ms": avg(values("hrv_ms"), 0),
                            "spo2_average": avg(values("spo2")), "respiratory_rate_average": avg(values("resp_rate"))}
            if values("recovery"):
                out["recovery"] = {"average": avg(values("recovery"), 0), "low_days_under_34": sum(1 for v in values("recovery") if v < 34),
                                   "latest": days.get(max(d for d, m in days.items() if "recovery" in m), {}).get("recovery")}
            signal = self.stress(days, last)
            if signal:
                out["stress"] = signal
        if focus in ("all", "mood"):
            moods = self.store.all("SELECT score FROM moods WHERE at>=? AND at<?", (datetime.combine(first, datetime.min.time()).timestamp(),
                                                                                   datetime.combine(last + timedelta(1), datetime.min.time()).timestamp()))
            if moods:
                out["mood"] = {"entries": len(moods), "average_1_to_5": avg([m["score"] for m in moods])}
            mindful = values("mindful_min")
            if mindful:
                out["mindful_minutes_total"] = round(sum(mindful))
        if values("weight_kg"):
            latest = max(d for d, m in days.items() if "weight_kg" in m)
            out["weight_kg_latest"] = days[latest]["weight_kg"]
        # The period before, so a "better or worse than usual" needs no second call.
        before = self.daily(first - timedelta(count), first - timedelta(1))
        earlier = lambda key: [m[key] for m in before.values() if key in m]
        compare = {}
        for key, label in (("sleep_min", "sleep_average_minutes"), ("steps", "steps_average"), ("resting_hr", "resting_hr_average"), ("hrv_ms", "hrv_average_ms")):
            if values(key) and earlier(key):
                compare[label] = {"this_period": round(mean(values(key))), "period_before": round(mean(earlier(key)))}
        if compare:
            out["compared_with_period_before"] = compare
        out["note"] = "Every figure is computed for this period and the one before it. Answer from them now; another call for a different period is not needed unless the user asked for one."
        out["card"] = self.card(out, count)
        return out

    def card(self, s, count):
        rows, value, subtitle, series = [], "", f"{s['from'][5:].replace('-', '/')} – {s['to'][5:].replace('-', '/')}", []
        if "sleep" in s:
            value = s["sleep"]["average"]
            rows.append({"label": "平均睡眠", "value": s["sleep"]["average"]})
            if s["sleep"].get("short_nights_under_6h"):
                rows.append({"label": "不足 6 小时", "value": f"{s['sleep']['short_nights_under_6h']} 晚"})
        act = s.get("activity", {})
        if act.get("steps_average"):
            rows.append({"label": "日均步数", "value": f"{act['steps_average']:,}"})
            value = value or f"{act['steps_average']:,} 步"
        if act.get("workouts"):
            rows.append({"label": "运动", "value": f"{act['workouts']} 次 · {act['workout_minutes']} 分钟"})
        heart = s.get("heart", {})
        if heart.get("resting_hr_average"):
            rows.append({"label": "静息心率", "value": f"{heart['resting_hr_average']:g} 次/分"})
        if heart.get("hrv_average_ms"):
            rows.append({"label": "心率变异性", "value": f"{heart['hrv_average_ms']:g} 毫秒"})
        if s.get("recovery"):
            rows.append({"label": "平均恢复", "value": f"{s['recovery']['average']:g}%"})
        if s.get("stress"):
            rows.append({"label": "身体压力", "value": s["stress"]["level"]})
        first = date.fromisoformat(s["from"])
        daily = self.daily(first, date.fromisoformat(s["to"]))
        key = "sleep_min" if "sleep" in s else "steps"
        series = [round(daily.get((first + timedelta(i)).isoformat(), {}).get(key, 0) / (60 if key == "sleep_min" else 1), 1) for i in range(count)] if count > 1 else []
        return {"kind": "health", "title": f"{s['period']} · 健康", "subtitle": subtitle, "value": value, "icon": "heart", "rows": rows[:6],
                "series": series, "series_label": "每晚睡眠（小时）" if key == "sleep_min" else "每日步数",
                "note": s["stress"]["note"] if s.get("stress") and s["stress"]["signs"] else "数据来自 " + "、".join(s.get("sources", [])) if s.get("sources") else ""}

    # ---- the cycle: local only ----
    def periods(self):
        """Periods as (first day, last day): flow days no more than two days apart belong to one period."""
        rows = self.store.all("SELECT day FROM cycle_days WHERE flow NOT IN ('none') ORDER BY day")
        out = []
        for r in rows:
            d = date.fromisoformat(r["day"])
            if out and (d - out[-1][1]).days <= 2:
                out[-1][1] = d
            else:
                out.append([d, d])
        return [(a, b) for a, b in out]

    def cycle(self, today=None):
        """Where the user is in their cycle and when the next period is likely. An estimate from their own history; never for contraception."""
        today = today or date.today()
        periods = self.periods()
        if not periods:
            return {"periods_recorded": 0, "note": "No period recorded yet. The user can tell you when it started, or it syncs from Apple Health / Google Health."}
        starts = [a for a, _ in periods]
        gaps = [(b - a).days for a, b in zip(starts, starts[1:]) if 15 <= (b - a).days <= 60][-6:]
        length = round(mean(gaps)) if gaps else 28
        duration = round(mean([(b - a).days + 1 for a, b in periods[-6:]]))
        last_start, last_end = periods[-1]
        nxt = last_start + timedelta(length)
        while nxt + timedelta(3) < today:
            nxt += timedelta(length)  # a missed record: count forward
        day = (today - last_start).days + 1
        ovulation = nxt - timedelta(14)
        if last_start <= today <= last_end:
            phase = "经期"
        elif day <= length and abs((today - ovulation).days) <= 2:
            phase = "排卵期附近"
        elif day <= length and today < ovulation:
            phase = "卵泡期"
        elif day <= length:
            phase = "黄体期"
        else:
            phase = "比平时晚"
        late = (today - (last_start + timedelta(length))).days
        symptoms = self.store.all("SELECT symptoms FROM cycle_days WHERE day>=?", ((today - timedelta(90)).isoformat(),))
        common = {}
        for r in symptoms:
            for s in json.loads(r["symptoms"]):
                common[s] = common.get(s, 0) + 1
        out = {"periods_recorded": len(periods), "last_period": {"start": last_start.isoformat(), "end": last_end.isoformat(), "days": (last_end - last_start).days + 1},
               "average_cycle_days": length, "cycle_lengths": gaps, "average_period_days": duration, "cycle_day_today": day, "phase": phase,
               "next_period_estimate": nxt.isoformat(), "days_until_next": (nxt - today).days, "irregular": bool(gaps) and max(gaps) - min(gaps) > 7,
               "common_symptoms_90_days": [k for k, _ in sorted(common.items(), key=lambda x: -x[1])][:5],
               "note": "Estimated from the user's own records" + ("" if len(gaps) >= 2 else " (fewer than 3 periods recorded, so 28 days is assumed)") + ". Not a method of contraception."}
        if late > 2 and phase == "比平时晚":
            out["late_by_days"] = late
        out["card"] = {"kind": "cycle", "title": "经期", "subtitle": f"周期第 {day} 天 · {phase}", "icon": "drop",
                       "value": f"{(nxt - today).days} 天后" if nxt >= today else "随时可能来",
                       "rows": [{"label": "下次预计", "value": nxt.strftime("%m/%d")}, {"label": "平均周期", "value": f"{length} 天"},
                                {"label": "上次经期", "value": f"{last_start.strftime('%m/%d')} · {(last_end - last_start).days + 1} 天"}],
                       "note": "根据你自己的记录估算，不能用于避孕"}
        return out

    def log_period(self, start, end="", flow="medium", symptoms=(), note=""):
        """The user says their period started (or ran from start to end): each day is recorded with the flow they gave."""
        first = date.fromisoformat(day_of(start))
        last = date.fromisoformat(day_of(end)) if end else first
        if last < first or (last - first).days > 14:
            raise ValueError("end must be on or after start, and a period is at most 15 days")
        if first > date.today():
            raise ValueError("the start date is in the future")
        d = first
        while d <= last:
            self.flow(d, flow, symptoms if d == first else (), note if d == first else "")
            d += timedelta(1)
        self.store.event("health", "manual")
        return {"recorded": {"start": first.isoformat(), "end": last.isoformat(), "flow": FLOWS[flow]}, **self.cycle()}

    def end_period(self, day=""):
        """The period ended: days after the given day that were assumed to be flow are removed."""
        last = date.fromisoformat(day_of(day)) if day else date.today()
        with self.store.connection() as db:
            db.execute("DELETE FROM cycle_days WHERE day>? AND source='manual' AND day<=?", (last.isoformat(), (last + timedelta(10)).isoformat()))
        return self.cycle()

    def page(self, days=30):
        """Everything the 健康 page shows. Read by the user's own devices, never by a model."""
        today = date.today()
        first = today - timedelta(days - 1)
        daily = self.daily(first, today)
        workouts = self.store.all("SELECT * FROM health_workouts WHERE start>=? ORDER BY start DESC LIMIT 60",
                                  (datetime.combine(first, datetime.min.time()).timestamp(),))
        cycle = self.cycle()
        cycle.pop("card", None)
        flow = self.store.all("SELECT day,flow,symptoms FROM cycle_days WHERE day>=? ORDER BY day", ((today - timedelta(120)).isoformat(),))
        summary = self.summary("last_7_days", today=today)
        summary.pop("card", None)
        return {"summary": summary, "days": [{"day": (first + timedelta(i)).isoformat(), **daily.get((first + timedelta(i)).isoformat(), {})} for i in range(days)],
                "workouts": [{**w, "kind_label": WORKOUTS.get(w["kind"], "其他运动")} for w in workouts], "cycle": cycle,
                "flow_days": [{**f, "symptoms": json.loads(f["symptoms"])} for f in flow], "sources": self.sources()}
