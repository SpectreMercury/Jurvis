"""Long tasks and long conversations against a live engine with the real model.

scenarios.py checks that each kind of request is handled; this suite checks stamina: writing that is
far longer than one model call, documents that are revised over several turns, goals with many parts,
a drama plan that has to add up, and a 22-turn conversation that must still know its beginning.
Every case ends in measured checks (files, lengths, duplicates, recall), not in reading the reply.
A full transcript is written next to the results. Start an isolated engine first (scripts/longtasks.sh).

    .venv/bin/python scripts/longtasks.py <engine data dir> <scratch dir> [case name ...]
"""
import difflib
import json
import re
import sys
import time
import traceback
import uuid
from pathlib import Path

import httpx

DATA, SCRATCH = Path(sys.argv[1]), Path(sys.argv[2])
ONLY = set(sys.argv[3:])
conn = json.loads((DATA / "security/local.json").read_text())
api = httpx.Client(base_url=conn["url"], headers={"Authorization": "Bearer " + conn["token"]}, verify=False, timeout=60)
out = (SCRATCH / "longtasks.md").open("w")
results = []


def log(*parts):
    print(*parts, file=out, flush=True)


def get(path):
    response = api.get(path)
    assert response.status_code == 200, f"GET {path}: HTTP {response.status_code} {response.text[:200]}"
    return response.json()


def files():
    return {f["name"]: get(f"/api/files/{f['name']}")["content"] for f in get("/api/files")["files"]}


def alike(a, b):
    a, b = a[:5000], b[:5000]
    if min(len(a), len(b)) < 200:
        return False
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= .85


def sections(text, pattern=r"^[ \t]*(?:#{1,4}[ \t]*)?(?:\*\*)?[《【]?第[ \t]*[0-9一二三四五六七八九十]+[ \t]*集"):
    """A script split at its episode headings: [(heading line, body)]."""
    marks = list(re.finditer(pattern, text, re.M))
    return [(text[m.start():text.find("\n", m.start())], text[m.start():(marks[i + 1].start() if i + 1 < len(marks) else len(text))]) for i, m in enumerate(marks)]


class Turn:
    def __init__(self, message, job, seconds):
        self.message, self.job, self.seconds = message, job, seconds
        self.text, self.meta = message["content"], message["metadata"]
        self.calls = [c for s in self.meta.get("steps", []) for c in s["calls"]]
        self.names = [c["name"] for c in self.calls]


class Conversation:
    def __init__(self):
        response = api.post("/api/sessions", json={})
        self.session = response.json()["id"]

    def send(self, text, mode=None, timeout=3600):
        start = time.time()
        job = api.post(f"/api/sessions/{self.session}/messages", json={"content": text, "asset_ids": [], "request_id": uuid.uuid4().hex, "mode": mode}).json()
        while True:
            done = get(f"/api/jobs/{job['id']}")
            if done["status"] not in ("queued", "running"):
                break
            if time.time() - start > timeout:
                api.post(f"/api/jobs/{job['id']}/cancel")
                raise AssertionError(f"no reply within {timeout} s")
            live = get(f"/api/sessions/{self.session}/messages")[-1]
            for c in (c for s in live["metadata"].get("steps", []) for c in s["calls"]):
                if c.get("status") == "awaiting":
                    api.post(f"/api/messages/{live['id']}/commands/{c['id']}", json={"decision": "run"})
            time.sleep(1)
        turn = Turn(get(f"/api/sessions/{self.session}/messages")[-1], done, time.time() - start)
        log(f"\n### 用户（{mode or 'chat'}）：{text}\n")
        log(f"`{done['status']}` · {turn.seconds:.0f} 秒 · {len(turn.text)} 字" + (f" · 目标 {turn.meta['goal']['status']}" if turn.meta.get("goal") else "")
            + (f" · 错误：{done.get('error')}" if done.get("error") else ""))
        for step in turn.meta.get("steps", []):
            if step["text"]:
                log("\n" + step["text"])
            for c in step["calls"]:
                arguments = json.dumps(c.get("arguments", {}), ensure_ascii=False)
                log(f"\n- `{c['name']}` {arguments[:200]}{'…' if len(arguments) > 200 else ''} → {c.get('status')}：{c.get('summary', '')}")
        assert done["status"] == "succeeded", f"chat job {done['status']}: {done.get('error')}"
        assert turn.text.strip(), "empty reply"
        assert "<tool_call>" not in turn.text and "<jurvis_context>" not in turn.text, "harness markup leaked into the reply"
        return turn

    def wait(self, job_id, timeout=1800):
        start = time.time()
        while time.time() - start < timeout:
            job = get(f"/api/jobs/{job_id}")
            if job["status"] not in ("queued", "running"):
                return job
            time.sleep(2)
        raise AssertionError(f"job {job_id} still running after {timeout} s")


def case(name):
    def wrap(fn):
        if ONLY and name not in ONLY:
            return fn
        print(f"\n== {name}", flush=True)
        log(f"\n\n## {name}")
        start = time.time()
        try:
            fn(Conversation())
            results.append((name, True, "", time.time() - start))
            print(f"PASS {name} ({time.time() - start:.0f}s)", flush=True)
        except Exception as exc:
            results.append((name, False, str(exc), time.time() - start))
            log(f"\n**失败：{exc}**")
            print(f"FAIL {name} ({time.time() - start:.0f}s): {exc}", flush=True)
            if not isinstance(exc, AssertionError):
                traceback.print_exc(limit=3)
        return fn
    return wrap


def no_twins(parts, what):
    for i, (name, body) in enumerate(parts):
        for other, text in parts[:i]:
            assert not alike(body, text), f"{what}: {name.strip()} is a near copy of {other.strip()}"


# ---- cases ----

@case("script revised over turns")
def _(c):
    """A five-episode script saved to a file, one episode rewritten in place, then a question answered from the file."""
    before = set(files())
    c.send("帮我写一个都市悬疑短剧的完整剧本，一共 5 集，每集 600 字左右，要有场景说明和完整对白。写完保存成文件。")
    new = {n: t for n, t in files().items() if n not in before}
    assert len(new) == 1, f"expected one new file, got {sorted(new)}"
    name, first = next(iter(new.items()))
    parts = sections(first)
    assert len(parts) == 5, f"expected 5 episodes in {name}, found {len(parts)}"
    assert all(len(body) >= 350 for _, body in parts), f"an episode is too short: {[len(b) for _, b in parts]}"
    no_twins(parts, name)
    t = c.send("把第 2 集的结尾改成一个反转，其他集不要动，改完更新文件。")
    now = files()
    assert set(now) - before == {name}, f"the update made another file: {sorted(set(now) - before)}"
    second = sections(now[name])
    assert len(second) == 5 and second[1][1] != parts[1][1], "episode 2 was not changed"
    for index in (0, 2, 3, 4):
        assert second[index][1].strip() == parts[index][1].strip(), f"episode {index + 1} changed although only episode 2 was to change"
    t = c.send("第 5 集的标题是什么？最后一句台词是谁说的？")
    title = re.sub(r"^[#*\s《【]*第\s*\S+?\s*集[》】]?\s*[：:·\-—]*\s*", "", second[4][0]).strip(" *》】")
    assert title and title in t.text, f"reply does not name episode 5's title {title!r}"


@case("ten episodes in one file")
def _(c):
    """Far more than one model call can hold: must be written part by part, every episode different, then renamed throughout."""
    c.send("写一个 10 集的都市复仇短剧完整剧本，每集 1000 字以上，要有场景、动作和完整对白，保存成一个文件 revenge.md。")
    text = files().get("revenge.md")
    assert text, "revenge.md was not written"
    parts = sections(text)
    assert len(parts) == 10, f"expected 10 episodes, found {len(parts)}: {[h for h, _ in parts]}"
    short = [h.strip() for h, body in parts if len(body) < 800]
    assert not short, f"episodes under 800 characters: {short}"
    no_twins(parts, "revenge.md")
    names = re.findall(r"^[ \t]*(?:\*\*)?([一-鿿]{2,4})(?:\*\*)?[ \t]*(?:[（(][^）)\n]*[）)])?[：:]", text, re.M)  # whoever speaks most
    names = [n for n in names if n not in ("场景", "时间", "地点", "人物", "动作", "旁白", "字幕", "画外音", "场景说明", "动作描写")]
    lead = max(set(names), key=names.count) if names else None
    assert lead, "could not find a character name to rename"
    renamed = "沈知意" if "沈知意" not in text else "顾清辞"
    t = c.send(f"把「{lead}」的名字全部改成「{renamed}」，更新 revenge.md。")
    after = files()
    assert [n for n in after if n.startswith("revenge")] == ["revenge.md"], "the rename made a copy instead of updating the file"
    assert lead not in after["revenge.md"] and renamed in after["revenge.md"], f"{lead} is still in the file"
    assert len(sections(after["revenge.md"])) == 10 and "edit_file" in t.names


@case("long story in the reply")
def _(c):
    """A piece longer than the output limit, written straight into the conversation and then extended."""
    t = c.send("直接在对话里写一篇 5000 字左右的科幻短篇小说，主题是最后一个邮差。不要保存文件。")
    assert len(t.text) >= 3800, f"asked for about 5000 characters, got {len(t.text)}"
    assert not t.meta.get("truncated"), "the story was cut off"
    assert not {"save_file", "append_file"} & set(t.names), "saved a file although told not to"
    halves = t.text[:len(t.text) // 2], t.text[len(t.text) // 2:]
    assert not alike(*halves), "the second half repeats the first"
    t2 = c.send("继续，把结尾再扩写 1000 字左右，只发新增的部分。")
    assert len(t2.text) >= 600 and not alike(t2.text, t.text[-len(t2.text):]), "the extension is missing or repeats the ending"


@case("goal with many files")
def _(c):
    """Goal mode: six episodes in six files plus an index, each its own content, finished and reviewed."""
    t = c.send("写一个 6 集的古装宫斗短剧剧本，每集 500 字以上，每集保存成单独的文件 ep1.md 到 ep6.md，最后写一个 index.md 列出每集标题和一句话梗概。", mode="goal")
    written = files()
    missing = [n for n in [f"ep{i}.md" for i in range(1, 7)] + ["index.md"] if n not in written]
    assert not missing, f"missing files: {missing}"
    episodes = [(f"ep{i}.md", written[f"ep{i}.md"]) for i in range(1, 7)]
    short = [n for n, body in episodes if len(body) < 500]
    assert not short, f"episodes under 500 characters: {short}"
    no_twins(episodes, "episodes")
    assert t.meta["goal"]["status"] == "done", f"goal ended as {t.meta['goal']['status']}: {t.meta['goal'].get('missing', '')}"
    assert sum(1 for i in range(1, 7) if re.search(rf"第\s*{i}\s*集|ep{i}", written["index.md"])) == 6, "index.md does not list all six episodes"


@case("drama plan that adds up")
def _(c):
    """Short-drama mode: the plan job must succeed with shots that sum to the requested length, and the follow-up must list the real shots."""
    t = c.send("一个外卖员意外发现自己能听见别人心声，靠这个能力帮一位老人找回走失的孙女，3 分钟左右，温暖治愈。", mode="drama")
    created = [call for call in t.calls if call["name"] == "create_drama" and call.get("job_id")]
    assert len(created) == 1, f"expected one create_drama, got {t.names}"
    assert "通知" not in t.text, "promised to notify the user, which it cannot do"
    job = c.wait(created[0]["job_id"])
    assert job["status"] == "succeeded", f"plan job {job['status']}: {job.get('error')}"
    project = next(p for p in get("/api/state")["projects"] if p["id"] == created[0]["project_id"])
    shots = project["document"]["shots"]
    total = sum(s["duration"] for s in shots)
    log(f"\n分镜：{len(shots)} 个镜头，共 {total:.0f} 秒")
    assert 120 <= total <= 260, f"asked for about 3 minutes, the shots add up to {total:.0f} s"
    assert all(s["duration"] <= 15 for s in shots) and project["document"]["characters"], "a shot is too long or there are no characters"
    t = c.send("分镜写好了吗？把每个镜头的标题列给我。")
    named = sum(1 for s in shots if s["title"] in t.text)
    assert named >= len(shots) * .8, f"the reply names {named} of {len(shots)} real shot titles"
    assert t.names.count("create_drama") == 0, "started the drama again"


@case("22-turn conversation")
def _(c):
    """Topics change, a file is written and edited, and at the end the conversation is asked about its own beginning."""
    turns = {}
    for number, text in enumerate([
        "我叫周野，在成都开了一家叫「山月」的咖啡店，下个月 12 号是三周年店庆，后面聊的都跟这个有关。",
        "帮我想 5 个店庆活动的点子，编号列出来，每个点子起一个四个字的名字。",
        "第 2 个展开一下，给出预算明细。",
        "预算控制在 3000 元以内，重新算一版。",
        "写一条店庆的朋友圈文案，第一句要单独成行。",
        "再来一个小红书版本，带标题和标签。",
        "换个话题：我最近想学 Python，给我一个 4 周的学习计划。",
        "第 1 周具体每天学什么？",
        "写一个统计咖啡店每日营业额平均值的小脚本。",
        "给脚本加上找出营业额最高那一天的功能。",
        "回到店庆：帮我写一条给老顾客的邀请短信，60 字以内。",
        "店庆那天大概需要多少人手？排个班。",
        "把活动方案整理成一份文档，保存为 店庆方案.md。",
        "在文档里加一节「风险预案」。",
        "另一个话题：推荐三本咖啡店经营方面的书。",
        "第二本主要讲什么？",
        "帮我算一下：店庆当天客流 180 人，客单价 38 元，毛利率 62%，毛利是多少？",
        "减掉 3000 元活动预算之后呢？",
    ], 1):
        turns[number] = c.send(text)
    plan = files().get("店庆方案.md")
    assert plan and "风险预案" in plan, "店庆方案.md is missing or has no 风险预案 section"
    assert [n for n in files() if n.startswith("店庆方案")] == ["店庆方案.md"], "adding a section made a copy of the document"
    assert re.search(r"4[,，]?240", turns[17].text), f"180 × 38 × 62% should be 4240.8: {turns[17].text[:120]!r}"
    assert re.search(r"1[,，]?240", turns[18].text), f"4240.8 − 3000 should be 1240.8: {turns[18].text[:120]!r}"
    session = next(s for s in get("/api/state")["sessions"] if s["id"] == c.session)
    log(f"\n会话摘要（{len(session.get('summary') or '')} 字）：\n{session.get('summary') or '（无）'}")
    assert session.get("summary"), "nothing was folded into a summary after 18 turns"

    t = c.send("我的店叫什么名字？店庆是哪一天？")
    assert "山月" in t.text and "12" in t.text, f"forgot the shop or the date: {t.text[:120]!r}"
    idea = re.search(r"^\s*(?:#+\s*)?(?:\*\*)?2[.、)）]\s*(?:\*\*)?\s*[「《【]?([一-鿿]{2,8})", turns[2].text, re.M)
    assert idea, "could not find idea 2 in the second turn to check against"
    t = c.send("我们最开始定的 5 个活动点子里，第 2 个叫什么？")
    assert idea.group(1) in t.text, f"idea 2 was {idea.group(1)!r}; the reply says {t.text[:120]!r}"
    opening = next((line.strip(" *>#「」“”\"") for line in turns[5].text.splitlines() if len(line.strip(" *>#")) >= 6 and not line.strip().endswith(("：", ":"))), "")
    t = c.send("之前那条朋友圈文案的第一句是什么？原样告诉我。")
    assert opening and opening[:8] in t.text, f"the copy opened with {opening[:20]!r}; the reply says {t.text[:120]!r}"
    t = c.send("在 店庆方案.md 的最后加一行「总预算：2800 元」，然后告诉我文档现在有哪几节。")
    after = files()
    assert "2800" in after["店庆方案.md"] and "风险预案" in after["店庆方案.md"], "the document was not updated in place"
    assert [n for n in after if n.startswith("店庆方案")] == ["店庆方案.md"] and "风险预案" in t.text


# ---- report ----
passed = sum(1 for _, ok, _, _ in results if ok)
print(f"\n{passed}/{len(results)} long tasks passed. Transcript: {SCRATCH / 'longtasks.md'}")
log(f"\n\n## 结果：{passed}/{len(results)}\n")
for name, ok, why, seconds in results:
    log(f"- {'通过' if ok else '失败'} · {name} · {seconds:.0f} 秒" + (f" · {why}" if why else ""))
(SCRATCH / "longtasks.json").write_text(json.dumps([{"name": n, "passed": ok, "reason": why, "seconds": round(s)} for n, ok, why, s in results], ensure_ascii=False, indent=1))
sys.exit(0 if passed == len(results) else 1)
