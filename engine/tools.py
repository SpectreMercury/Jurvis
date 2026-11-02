"""Declarative tool registry for the assistant harness.

Each tool owns a pydantic argument model, so the model sees an exact JSON
schema through native tool calling and every call is validated before it runs.
"""
import ast
import json
import operator
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, Field, ValidationError

from .coding import Coding, CodeEdit, CodeTask
from .schemas import ImageRequest, ProjectCreate, VideoRequest
from .store import dumps, new_id


def calculate(expression):
    if len(expression) > 300:
        raise ValueError("算式过长")
    ops = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
           ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
    tree = ast.parse(expression, mode="eval")
    if len(list(ast.walk(tree))) > 60:
        raise ValueError("算式过于复杂")

    def visit(n):
        if isinstance(n, ast.Constant) and type(n.value) in (int, float):
            result = n.value
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.UAdd, ast.USub)):
            result = visit(n.operand) * (1 if isinstance(n.op, ast.UAdd) else -1)
        elif isinstance(n, ast.BinOp) and type(n.op) in ops:
            a, b = visit(n.left), visit(n.right)
            if isinstance(n.op, ast.Pow) and abs(b) > 100:
                raise ValueError("指数过大")
            result = ops[type(n.op)](a, b)
        else:
            raise ValueError("只支持数字和基本算术")
        if not isinstance(result, (int, float)) or abs(result) > 1e100:
            raise ValueError("结果超出范围")
        return result
    return visit(tree.body)


def tidy(value):
    """Hides float noise such as 399.50000000000006 without losing real precision."""
    if isinstance(value, float):
        value = float(f"{value:.12g}")
        return int(value) if value.is_integer() and abs(value) < 1e15 else value
    return value


# ---- argument models (what the model is allowed to send) ----

class NoArgs(BaseModel):
    pass


class CalculateArgs(BaseModel):
    expression: str = Field(description="Arithmetic expression using numbers and + - * / // % **, e.g. (12+8)*3")


class FileName(BaseModel):
    name: str = Field(description="A single file name in the assistant workspace, e.g. notes.md")


class SaveFileArgs(BaseModel):
    name: str = Field(description="File name to create, e.g. plan.md. An existing file is never overwritten.")
    content: str = Field(description="Full UTF-8 text content")


class RecallArgs(BaseModel):
    query: str = Field(default="", description="Keyword to look for in saved memories and asset names")


class RememberArgs(BaseModel):
    title: str = Field(max_length=100)
    content: str = Field(max_length=4000)


class ImageArgs(BaseModel):
    prompt: str = Field(description="Detailed visual prompt: subject, composition, lighting, style")
    aspect: Literal["square", "portrait", "landscape"] = "square"
    reference_assets: list[str] = Field(default_factory=list, max_length=4,
                                        description="Asset IDs of attached images to edit or keep consistent with")
    seed: int = Field(default=42, ge=0, le=2**32 - 1)


class VideoArgs(BaseModel):
    prompt: str = Field(description="Shot description with action, camera, sound and exact dialogue")
    duration: float = Field(default=5, ge=1, le=15, description="Seconds")
    aspect: Literal["square", "portrait", "landscape"] = "portrait"
    first_frame: str | None = Field(default=None, description="Optional image asset ID for the first frame")
    last_frame: str | None = Field(default=None, description="Optional image asset ID for the last frame")


class DramaArgs(BaseModel):
    title: str = Field(max_length=100)
    brief: str = Field(max_length=6000, description="Story, total duration, characters, style, key dialogue")


class JobArgs(BaseModel):
    job_id: str


class ProjectArgs(BaseModel):
    project_id: str


class CodeReadArgs(BaseModel):
    project_id: str
    path: str = Field(description="Relative path from code_files")


class CodeSearchArgs(BaseModel):
    project_id: str
    query: str = Field(description="Literal text to find, case-insensitive")


class EditCodeArgs(BaseModel):
    project_id: str
    summary: str = Field(max_length=2000, description="One or two sentences on what changes and why, in the user's language")
    edits: list[CodeEdit] = Field(min_length=1, max_length=20, description="All replacements for this change; several files allowed")


class CodeProposeArgs(BaseModel):
    project_id: str
    instruction: str = Field(description="What to change and why, in detail")
    files: list[str] = Field(default_factory=list, max_length=12, description="Relevant existing files to read")


IMAGE_SIZES = {"square": (1024, 1024), "portrait": (768, 1024), "landscape": (1024, 768)}
VIDEO_SIZES = {"square": (448, 448), "portrait": (352, 608), "landscape": (608, 352)}


@dataclass
class Tool:
    name: str
    label: str            # short Chinese label shown in the UI trace
    description: str
    args: type[BaseModel]
    run: Callable
    effect: Literal["read", "write", "job"] = "read"

    def schema(self):
        params = self.args.model_json_schema()
        defs = params.pop("$defs", {})

        def inline(node):  # local models handle flat schemas better than $ref
            if isinstance(node, dict):
                if "$ref" in node:
                    return inline(dict(defs[node["$ref"].rsplit("/", 1)[-1]]))
                return {k: inline(v) for k, v in node.items() if k != "title"}
            if isinstance(node, list):
                return [inline(v) for v in node]
            return node
        params = inline(params)
        params.setdefault("properties", {})
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": params}}


@dataclass
class Outcome:
    ok: bool
    result: dict
    summary: str          # one line for the user, in Chinese

    def for_model(self, limit=8000):
        text = dumps(self.result if self.ok else {"error": self.result.get("error", "失败")})
        return text if len(text) <= limit else text[:limit] + "…(truncated)"


class ToolRegistry:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.coding = Coding(settings, store)
        self.tools = {t.name: t for t in self.build()}

    def build(self):
        T = Tool
        return [
            T("calculate", "计算", "Evaluate an arithmetic expression exactly. Use only when the user's request needs a numeric result.", CalculateArgs,
              lambda a: {"value": tidy(calculate(a.expression))}),
            T("list_files", "查看工作区文件", "List text documents in the user's assistant workspace.", NoArgs,
              lambda a: self.list_files()),
            T("read_file", "读取文件", "Read a text document from the assistant workspace.", FileName,
              lambda a: self.read_file(a.name)),
            T("save_file", "保存文件", "Save writing the user asked to keep as a new workspace document. Existing files are never modified: if the name is taken, a new name is used and returned.", SaveFileArgs,
              lambda a: self.save_file(a.name, a.content), "write"),
            T("recall", "查找记忆", "Search saved memories and uploaded or generated media assets by keywords (any keyword may match). An empty query lists the most recent ones.", RecallArgs,
              lambda a: self.recall(a.query)),
            T("remember", "保存记忆", "Save a memory. ONLY when the user explicitly asks you to remember something.", RememberArgs,
              lambda a: self.remember(a.title, a.content), "write"),
            T("create_image", "生成图片", "Queue a text-to-image or reference-image edit task. Returns a job ID; the image appears when the job finishes.",
              ImageArgs, self.create_image, "job"),
            T("create_video", "生成视频", "Queue a short video clip with sound (several minutes). Returns a job ID.",
              VideoArgs, self.create_video, "job"),
            T("create_drama", "创建短剧", "Create a short-drama project and queue writing of characters and shots for review.",
              DramaArgs, self.create_drama, "job"),
            T("job_status", "查看任务", "Check the status and outputs of a queued job.", JobArgs,
              lambda a: self.job_status(a.job_id)),
            T("code_projects", "查看代码项目", "List code projects the user connected on the Mac.", NoArgs,
              lambda a: {"projects": [{"id": p["id"], "name": p["name"]} for p in self.coding.projects()]}),
            T("code_files", "浏览源码", "List readable source files of a connected project.", ProjectArgs,
              lambda a: self.coding.tree(a.project_id)),
            T("code_read", "阅读源码", "Read one source file of a connected project.", CodeReadArgs,
              lambda a: {"path": a.path, "content": self.coding.read(a.project_id, a.path)}),
            T("code_search", "搜索源码", "Search literal text across a connected project.", CodeSearchArgs,
              lambda a: self.coding.search(a.project_id, a.query)),
            T("edit_code", "修改代码", "Change code in a connected project with exact text replacements. Read the file with code_read first and copy old_text exactly. "
              "Put every edit for one change in a single call. The result is shown to the user as a diff to apply; it is not applied or tested yet.",
              EditCodeArgs, self.edit_code, "write"),
        ]

    def schemas(self, names=None):
        return [t.schema() for t in self.tools.values() if names is None or t.name in names]

    # ---- harness entry point: validates, never raises ----
    def invoke(self, name, arguments):
        tool = self.tools.get(name)
        if not tool:
            return Outcome(False, {"error": f"没有名为 {name} 的工具"}, f"未知工具 {name}")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                return Outcome(False, {"error": "参数不是有效的 JSON"}, "参数格式错误")
        try:
            args = tool.args.model_validate(arguments or {})
        except ValidationError as exc:
            detail = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}" for e in exc.errors()[:5])
            return Outcome(False, {"error": "参数无效：" + detail}, "参数无效")
        try:
            result = tool.run(args)
        except (ValueError, KeyError, OSError, SyntaxError, ZeroDivisionError, ValidationError) as exc:
            return Outcome(False, {"error": str(exc)[:1000]}, str(exc)[:120])
        return Outcome(True, result, self.summarize(name, result))

    # ---- direct use by HTTP endpoints: raises ValueError like before ----
    def execute(self, name, arguments):
        tool = self.tools.get(name)
        if not tool:
            raise ValueError(f"没有名为 {name} 的工具")
        return tool.run(tool.args.model_validate(arguments))

    @staticmethod
    def summarize(name, r):
        if name == "calculate": return f"= {r['value']}"
        if name == "list_files": return f"{len(r['files'])} 个文件"
        if name == "read_file": return f"{r['name']} · {len(r['content'])} 字"
        if name == "save_file": return f"已保存为 {r['saved']}"
        if name == "recall": return f"{len(r['memories'])} 条记忆，{len(r['assets'])} 个素材"
        if name == "remember": return f"已记住「{r['title']}」"
        if name in ("create_image", "create_video", "code_propose"): return "已加入任务队列"
        if name == "edit_code": return "待应用 · " + "，".join(f"{f['path']} +{f['added']} −{f['removed']}" for f in r["files"])
        if name == "create_drama": return "项目已创建，正在编写分镜"
        if name == "job_status": return r.get("status_label", r["status"])
        if name == "code_projects": return f"{len(r['projects'])} 个项目"
        if name == "code_files": return f"{len(r['files'])} 个文件"
        if name == "code_read": return f"{r['path']} · {r['content'].count(chr(10)) + 1} 行"
        if name == "code_search": return f"{len(r['matches'])} 处匹配"
        return "完成"

    # ---- implementations ----
    def workspace_path(self, name):
        if not name or Path(name).name != name or name.startswith(".") or len(name) > 120:
            raise ValueError("请使用工作区内的单个文件名")
        folder = (self.settings.data / "workspace").resolve()
        path = (folder / name).resolve()
        if path.parent != folder:
            raise ValueError("文件不在工作区")
        return path

    def list_files(self):
        folder = self.settings.data / "workspace"
        return {"files": [{"name": p.name, "bytes": p.stat().st_size} for p in sorted(folder.iterdir()) if p.is_file() and not p.is_symlink()][:200]}

    def read_file(self, name):
        path = self.workspace_path(name)
        if not path.is_file():
            raise ValueError("文件不存在")
        if path.stat().st_size > 100000:
            raise ValueError("文件超过 100 KB，请先拆分")
        return {"name": path.name, "content": path.read_text()}

    def save_file(self, name, content):
        path = self.workspace_path(name)
        if len(content.encode()) > 100000:
            raise ValueError("文件超过 100 KB")
        # Never silently replace an existing document.
        renamed = path.exists()
        if renamed:
            path = path.with_name(f"{path.stem}-{new_id()[:8]}{path.suffix}")
        with path.open("x") as f:
            f.write(content)
        result = {"saved": path.name, "bytes": path.stat().st_size}
        if renamed:
            result["note"] = f"{name} already exists and was NOT changed. The new text was saved as {path.name}; tell the user this exact name."
        return result

    def recall(self, query):
        # Any keyword may match, so "红色圆形 图片" still finds 红色圆形.png.
        terms = [t for t in query[:200].replace("，", " ").replace(",", " ").split() if t][:8]
        likes = ["%" + t + "%" for t in terms]
        memory_where = " OR ".join("title LIKE ? OR content LIKE ?" for _ in likes) or "1"
        asset_where = " OR ".join("name LIKE ? OR kind LIKE ?" for _ in likes) or "1"
        pairs = [x for like in likes for x in (like, like)]
        return {"memories": self.store.all(f"SELECT id,title,content FROM memories WHERE enabled=1 AND ({memory_where}) ORDER BY updated_at DESC LIMIT 20", pairs),
                "assets": self.store.all(f"SELECT id,name,kind,mime,created_at FROM assets WHERE {asset_where} ORDER BY created_at DESC LIMIT 20", pairs)}

    def remember(self, title, content):
        ident, now = new_id(), time.time()
        self.store.execute("INSERT INTO memories(id,title,content,created_at,updated_at) VALUES(?,?,?,?,?)", (ident, title, content, now, now))
        self.store.event("memories", ident)
        return {"saved": ident, "title": title}

    def require_images(self, ids):
        for ident in ids:
            row = self.store.one("SELECT mime FROM assets WHERE id=?", (ident,))
            if not row:
                raise ValueError(f"素材 {ident} 不存在")
            if not row["mime"].startswith("image/"):
                raise ValueError(f"素材 {ident} 不是图片")

    def create_image(self, a):
        self.require_images(a.reference_assets)
        width, height = IMAGE_SIZES[a.aspect]
        request = ImageRequest(prompt=a.prompt, width=width, height=height, steps=25, seed=a.seed, reference_assets=a.reference_assets)
        job = self.store.create_job("image", request.model_dump())
        return {"job_id": job["id"], "status": "queued"}

    def create_video(self, a):
        self.require_images([x for x in (a.first_frame, a.last_frame) if x])
        width, height = VIDEO_SIZES[a.aspect]
        request = VideoRequest(prompt=a.prompt, duration=a.duration, width=width, height=height, first_frame=a.first_frame, last_frame=a.last_frame)
        job = self.store.create_job("video", request.model_dump())
        return {"job_id": job["id"], "status": "queued", "note": "视频生成通常需要数分钟"}

    def create_drama(self, a):
        request = ProjectCreate(title=a.title, brief=a.brief)
        ident, now = new_id(), time.time()
        self.store.execute("INSERT INTO projects(id,title,brief,created_at,updated_at) VALUES(?,?,?,?,?)", (ident, request.title, request.brief, now, now))
        job = self.store.create_job("plan", {"project_id": ident})
        self.store.event("projects", ident)
        return {"project_id": ident, "job_id": job["id"], "status": "queued"}

    def job_status(self, ident):
        job = self.store.get_job(ident)
        if not job:
            raise ValueError("任务不存在")
        labels = {"queued": "排队中", "running": "进行中", "succeeded": "已完成", "failed": "失败", "cancelled": "已取消", "interrupted": "已中断"}
        return {"job_id": ident, "kind": job["kind"], "status": job["status"], "status_label": labels.get(job["status"], job["status"]),
                "phase": job["phase"], "error": job["error"], "asset_ids": job["result"].get("asset_ids", [])}

    def edit_code(self, a):
        result = self.coding.edit(a.project_id, a.summary, a.edits)
        job = self.store.record_job("code", {"code_project_id": a.project_id, "instruction": a.summary, "source": "chat"}, result)
        def count(diff, sign):
            return sum(1 for line in diff.splitlines() if line.startswith(sign) and not line.startswith(sign * 3 + " "))
        files = [{"path": c["path"], "added": count(c["diff"], "+"), "removed": count(c["diff"], "-")} for c in result["changes"]]
        return {"job_id": job["id"], "files": files, "status": "awaiting_review",
                "note": "Shown to the user as a diff with an Apply button. Not applied or tested yet."}

    def code_propose(self, a):
        self.coding.project(a.project_id)
        for name in a.files:
            self.coding.read(a.project_id, name)
        task = CodeTask(instruction=a.instruction, files=a.files)
        job = self.store.create_job("code", {"code_project_id": a.project_id, **task.model_dump()})
        return {"job_id": job["id"], "status": "queued", "note": "完成后需用户在 Coding 工作台审阅并应用"}
