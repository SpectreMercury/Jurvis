"""End-to-end acceptance run against a live engine with real models.

Exercises every user-facing feature through the same HTTP API the apps use and
prints PASS/FAIL per check. Media sizes are kept small so the full run takes
minutes, not hours. Start an isolated engine first (see scripts/e2e.sh).

    .venv/bin/python scripts/e2e.py <engine data dir> <scratch dir>
"""
import json
import shutil
import sys
import time
import traceback
import uuid
from pathlib import Path

import httpx

DATA, SCRATCH = Path(sys.argv[1]), Path(sys.argv[2])
conn = json.loads((DATA / "security/local.json").read_text())
api = httpx.Client(base_url=conn["url"], headers={"Authorization": "Bearer " + conn["token"]}, verify=False, timeout=60)
results = []


def check(name):
    def wrap(fn):
        start = time.time()
        try:
            detail = fn() or ""
            results.append((name, True)); print(f"PASS {name} ({time.time()-start:.0f}s) {detail}", flush=True)
        except Exception as exc:
            results.append((name, False)); print(f"FAIL {name} ({time.time()-start:.0f}s): {exc}", flush=True)
            traceback.print_exc(limit=2)
        return fn
    return wrap


def ok(response, status=200):
    assert response.status_code == status, f"HTTP {response.status_code}: {response.text[:300]}"
    return response.json()


def wait(job_id, timeout=1800):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = ok(api.get(f"/api/jobs/{job_id}"))
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(2)
    raise AssertionError(f"job {job_id} timed out")


def succeeded(job_id, timeout=1800):
    job = wait(job_id, timeout)
    assert job["status"] == "succeeded", f"{job['kind']} {job['status']}: {job['error']}"
    return job


def chat(session, text, assets=()):
    job = ok(api.post(f"/api/sessions/{session}/messages", json={"content": text, "asset_ids": list(assets), "request_id": uuid.uuid4().hex}))
    succeeded(job["id"], 900)
    message = ok(api.get(f"/api/sessions/{session}/messages"))[-1]
    calls = [c for s in message["metadata"].get("steps", []) for c in s["calls"]]
    return message, calls


state = {}


@check("health and state")
def _():
    ok(api.get("/health")); s = ok(api.get("/api/state"))
    return f"version {s['version']}"


@check("upload reference image")
def _():
    sample = SCRATCH / "e2e-reference.png"
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (512, 512), (240, 236, 228)); draw = ImageDraw.Draw(image)
    draw.ellipse((156, 156, 356, 356), fill=(220, 40, 40)); image.save(sample)
    asset = ok(api.post("/api/assets", files={"file": ("红色圆形.png", sample.read_bytes(), "image/png")}))
    state["ref"] = asset["id"]
    assert api.post("/api/assets", files={"file": ("x.png", b"not an image", "image/png")}).status_code == 400
    return asset["id"]


@check("memories: create, disable, edit, delete")
def _():
    m = ok(api.post("/api/memories", json={"title": "称呼", "content": "用户希望被称为 Lee"}))
    ok(api.put(f"/api/memories/{m['id']}", json={"title": "称呼", "content": "用户希望被称为 Lee 老师", "enabled": False}))
    row = next(x for x in ok(api.get("/api/state"))["memories"] if x["id"] == m["id"])
    assert row["enabled"] == 0 and "老师" in row["content"]
    ok(api.put(f"/api/memories/{m['id']}", json={"title": "称呼", "content": "用户希望被称为 Lee", "enabled": True}))
    temp = ok(api.post("/api/memories", json={"title": "临时", "content": "删除测试"}))
    ok(api.delete(f"/api/memories/{temp['id']}"))
    assert all(x["id"] != temp["id"] for x in ok(api.get("/api/state"))["memories"])


@check("workspace files: import, list, read, no overwrite")
def _():
    ok(api.post("/api/files", files={"file": ("notes.md", "# 会议\n- 周五交付".encode(), "text/plain")}))
    second = ok(api.post("/api/files", files={"file": ("notes.md", "第二份".encode(), "text/plain")}))
    assert second["saved"] != "notes.md"
    names = [f["name"] for f in ok(api.get("/api/files"))["files"]]
    assert "notes.md" in names
    assert "周五交付" in ok(api.get("/api/files/notes.md"))["content"]


@check("chat: plain reply with memory")
def _():
    state["session"] = ok(api.post("/api/sessions", json={}))["id"]
    message, calls = chat(state["session"], "你好，你应该怎么称呼我？只回答称呼。")
    assert message["status"] == "complete" and "Lee" in message["content"], message["content"]
    return message["content"][:60]


@check("chat: multi-step tools (calculate + save_file)")
def _():
    message, calls = chat(state["session"], "计算 (128+64)*3，然后把结果保存到 e2e-result.md")
    names = [c["name"] for c in calls]
    assert "calculate" in names and "save_file" in names, names
    assert all(c["status"] == "ok" for c in calls)
    assert "576" in ok(api.get("/api/files/e2e-result.md"))["content"]
    return " → ".join(names)


@check("chat: follow-up uses earlier tool results")
def _():
    message, calls = chat(state["session"], "刚才保存的文件名是什么？只回答文件名。")
    assert "e2e-result" in message["content"], message["content"]


@check("chat: image understanding")
def _():
    message, _ = chat(state["session"], "这张图片里主要是什么形状和颜色？简短回答。", [state["ref"]])
    assert "红" in message["content"] and ("圆" in message["content"] or "circle" in message["content"].lower()), message["content"]
    return message["content"][:60]


@check("chat: stop then retry")
def _():
    session = ok(api.post("/api/sessions", json={}))["id"]
    job = ok(api.post(f"/api/sessions/{session}/messages", json={"content": "写一首八行的诗", "request_id": uuid.uuid4().hex}))
    time.sleep(4)
    ok(api.post(f"/api/jobs/{job['id']}/cancel"))
    assert wait(job["id"])["status"] == "cancelled"
    retry = ok(api.post(f"/api/jobs/{job['id']}/retry"))
    succeeded(retry["id"], 900)
    last = ok(api.get(f"/api/sessions/{session}/messages"))[-1]
    assert last["status"] == "complete" and last["content"]
    ok(api.delete(f"/api/sessions/{session}"))


@check("chat → create_image tool with attached reference, job completes")
def _():
    message, calls = chat(state["session"], "用我刚才发的那张图作参考，生成一张同样红色圆形放在木桌上的方形图片。", [state["ref"]])
    image = next((c for c in calls if c["name"] == "create_image"), None)
    assert image and image["status"] == "ok", calls
    assert state["ref"] in json.dumps(image["arguments"]), image["arguments"]
    job = succeeded(image["job_id"])
    assert job["result"]["asset_ids"]
    return image["job_id"]


@check("studio: direct image generation")
def _():
    job = ok(api.post("/api/images", json={"prompt": "a teal ceramic cup on a wooden table, soft window light", "width": 512, "height": 512, "steps": 8, "seed": 7}))
    job = succeeded(job["id"])
    asset = next(a for a in ok(api.get("/api/state"))["assets"] if a["id"] == job["result"]["asset_ids"][0])
    data = api.get(f"/api/assets/{asset['id']}/file")
    assert data.status_code == 200 and data.content[:4] == b"\x89PNG"
    state["image"] = asset["id"]


@check("studio: video with prompt rewrite and first frame")
def _():
    job = ok(api.post("/api/videos", json={"prompt": "镜头缓慢推进，杯口升起热气，安静的室内环境声。", "duration": 1, "width": 256, "height": 256,
                                           "first_frame": state["image"], "enhance": True}))
    job = succeeded(job["id"])
    asset_id = job["result"]["asset_ids"][0]
    data = api.get(f"/api/assets/{asset_id}/file")
    assert data.status_code == 200 and b"ftyp" in data.content[:32], "not an mp4"
    state["video"] = asset_id
    return f"{len(data.content)//1024} KB"


@check("extract first and last frames")
def _():
    job = succeeded(ok(api.post(f"/api/assets/{state['video']}/frames"))["id"])
    assert len(job["result"]["asset_ids"]) == 2


@check("drama: plan, edit with lock, frame, shots, export")
def _():
    project = ok(api.post("/api/projects", json={"title": "E2E 短剧", "brief": "两个镜头，共 2 秒：清晨窗边的一杯茶冒着热气；一只手拿起杯子。写实风格，无对白。"}))
    plan = next(j for j in ok(api.get("/api/state"))["jobs"] if j["kind"] == "plan" and j["payload"]["project_id"] == project["id"])
    succeeded(plan["id"], 900)
    project = next(p for p in ok(api.get("/api/state"))["projects"] if p["id"] == project["id"])
    doc = project["document"]
    assert doc["shots"], "plan produced no shots"
    doc["shots"] = doc["shots"][:2]
    for shot in doc["shots"]:
        shot["duration"] = 1
    stale = api.put(f"/api/projects/{project['id']}", json={"document": doc, "expected_updated_at": project["updated_at"] - 1})
    assert stale.status_code == 409
    project = ok(api.put(f"/api/projects/{project['id']}", json={"document": doc, "expected_updated_at": project["updated_at"]}))
    first = project["document"]["shots"][0]
    succeeded(ok(api.post("/api/images", json={"prompt": first["image_prompt"] or first["description"], "width": 256, "height": 256, "steps": 8,
                                                 "project_id": project["id"], "shot_id": first["id"]}))["id"])
    for shot in project["document"]["shots"]:
        current = next(p for p in ok(api.get("/api/state"))["projects"] if p["id"] == project["id"])
        shot = next(s for s in current["document"]["shots"] if s["id"] == shot["id"])
        body = {"prompt": shot["video_prompt"] or shot["description"], "duration": 1, "width": 256, "height": 256, "enhance": False,
                "project_id": project["id"], "shot_id": shot["id"]}
        if shot.get("first_frame"):
            body["first_frame"] = shot["first_frame"]
        succeeded(ok(api.post("/api/videos", json=body))["id"])
    export = succeeded(ok(api.post(f"/api/projects/{project['id']}/export"))["id"])
    film = export["result"]["asset_ids"][0]
    assert api.get(f"/api/assets/{film}/file").status_code == 200
    return f"{len(doc['shots'])} shots exported"


@check("coding: connect, browse, search, propose, apply, test, revert")
def _():
    root = SCRATCH / "e2e-code"
    shutil.rmtree(root, ignore_errors=True); (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text("def total(a, b):\n    return a * b\n")
    (root / "tests/test_calc.py").write_text("from calc import total\n\n\ndef test_total():\n    assert total(2, 3) == 5\n")
    (root / "pyproject.toml").write_text("[project]\nname='e2e'\nversion='0'\n[tool.pytest.ini_options]\npythonpath=['.']\n")
    project = ok(api.post("/api/code/projects", json={"root": str(root)}))
    pid = project["id"]
    files = [f["path"] for f in ok(api.get(f"/api/code/projects/{pid}/files"))["files"]]
    assert "calc.py" in files
    assert ok(api.get(f"/api/code/projects/{pid}/search", params={"q": "a * b"}))["matches"]
    commands = ok(api.get(f"/api/code/projects/{pid}/commands"))
    test = next(c for c in commands if c["id"] == "pytest")
    failing = wait(ok(api.post(f"/api/code/projects/{pid}/run", json={"command_id": test["id"]}))["id"])
    assert failing["status"] == "failed", "test should fail before the fix"
    job = succeeded(ok(api.post(f"/api/code/projects/{pid}/tasks", json={"instruction": "total 应该返回两数之和，修复这个 bug", "files": ["calc.py", "tests/test_calc.py"]}))["id"], 900)
    assert job["result"]["changes"], job["result"]["summary"]
    ok(api.post(f"/api/code/jobs/{job['id']}/apply"))
    assert "a + b" in (root / "calc.py").read_text()
    passing = succeeded(ok(api.post(f"/api/code/projects/{pid}/run", json={"command_id": test["id"]}))["id"])
    assert passing["result"]["passed"]
    ok(api.post(f"/api/code/jobs/{job['id']}/revert"))
    assert "a * b" in (root / "calc.py").read_text()
    state["code_project"] = pid


@check("chat → code tools (list projects, read, propose)")
def _():
    session = ok(api.post("/api/sessions", json={}))["id"]
    message, calls = chat(session, "看看我连接的代码项目里 calc.py 的内容，然后提交一个修复 total 函数的修改建议。")
    names = [c["name"] for c in calls]
    assert "code_read" in names and "code_propose" in names, names
    propose = next(c for c in calls if c["name"] == "code_propose")
    succeeded(propose["job_id"], 900)
    return " → ".join(names)


@check("chat → create_drama tool")
def _():
    session = ok(api.post("/api/sessions", json={}))["id"]
    message, calls = chat(session, "帮我创建一部短剧项目：标题《雨夜》，10 秒，一个人在雨夜的车站等车。")
    drama = next((c for c in calls if c["name"] == "create_drama"), None)
    assert drama and drama["status"] == "ok", calls
    succeeded(drama["job_id"], 900)


@check("pairing: invite, pair, scoped access, revoke")
def _():
    invite = ok(api.post("/api/pairing"))
    device = httpx.Client(base_url=conn["url"], verify=False, timeout=30)
    pair = ok(device.post("/pair", json={"code": invite["code"], "name": "E2E iPhone"}))
    device.headers["Authorization"] = "Bearer " + pair["token"]
    ok(device.get("/api/state"))
    assert device.post("/api/pairing").status_code == 403
    assert device.post("/pair", json={"code": invite["code"], "name": "again"}).status_code != 200
    ok(api.delete(f"/api/devices/{pair['device_id']}"))
    assert device.get("/api/state").status_code == 401


@check("settings: models and components")
def _():
    models = ok(api.get("/api/models"))
    assert all(m["installed"] for m in models["models"]), models
    ok(api.get("/api/components"))


failed = [n for n, passed in results if not passed]
print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (": failed " + ", ".join(failed) if failed else ""))
sys.exit(1 if failed else 0)
