"""Declarative tool registry for the assistant harness.

Each tool owns a pydantic argument model, so the model sees an exact JSON
schema through native tool calling and every call is validated before it runs.
"""
import ast
import difflib
import inspect
from glob import escape as glob_escape
import json
import operator
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, Field, ValidationError

from . import cards, web
from .agenda import Agenda
from .ledger import CATEGORIES, TOTAL, Ledger
from .coding import Coding, CodeEdit, CodeTask
from .home import ACTIONS as HOME_ACTIONS, TYPES as HOME_TYPES, ConnectorError
from .mcp import McpError
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


def nearest(text, wanted):
    """The run of whole lines in text closest to wanted, and how close (0 to 1). Only an unambiguous best counts."""
    lines, size = text.splitlines(keepends=True), max(1, len(wanted.strip("\n").splitlines()))
    if not wanted.strip() or len(lines) > 20000:
        return "", 0
    matcher, scored = difflib.SequenceMatcher(autojunk=False), []
    matcher.set_seq2(wanted.strip())
    for start in range(max(1, len(lines) - size + 1)):
        window = "".join(lines[start:start + size]).strip("\n")
        matcher.set_seq1(window.strip())
        if matcher.real_quick_ratio() >= .6 and matcher.quick_ratio() >= .6:
            scored.append((matcher.ratio(), window))
    scored.sort(key=lambda pair: -pair[0])
    if not scored or len(scored) > 1 and scored[1][0] >= scored[0][0] - .03 and scored[1][1] != scored[0][1]:
        return (scored[0][1] if scored else ""), 0  # two passages fit about equally: guessing would edit the wrong one
    return scored[0][1], scored[0][0]


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


class ReadFileArgs(FileName):
    offset: int = Field(default=0, ge=0, description="For a long file: the first line to return (1 is the first line). 0 returns the file from the start")
    limit: int = Field(default=0, ge=0, le=2000, description="For a long file: how many lines to return. 0 returns everything")


class FindFilesArgs(BaseModel):
    pattern: str = Field(max_length=200, description="Glob pattern on the file path: *.md, **/*.py, src/**/test_*.ts, docs/{intro,usage}.md")
    project_id: str = Field(default="", description="A connected code project (id from code_projects). Empty: the assistant workspace")


class SearchFilesArgs(BaseModel):
    pattern: str = Field(max_length=300, description="Regular expression to find in file contents, e.g. def \\w+_handler|TODO")
    project_id: str = Field(default="", description="A connected code project (id from code_projects). Empty: the assistant workspace")
    glob: str = Field(default="", max_length=200, description="Only search files whose path matches this glob, e.g. **/*.py")
    ignore_case: bool = True
    context: int = Field(default=0, ge=0, le=5, description="Lines to show before and after each match")


class CommandIdArgs(BaseModel):
    command_id: str = Field(description="The command_id returned by run_command with background set")


class AskUserArgs(BaseModel):
    question: str = Field(min_length=1, max_length=300, description="The one question, in the user's language")
    options: str | list[str] = Field(description="2 to 4 short answers the user can tap, one per line, each a few words (under about 12 Chinese characters or 6 English words) in the user's own voice, e.g. 先搭个通用框架")


class StartTaskArgs(BaseModel):
    title: str = Field(min_length=1, max_length=60, description="A short name for the work, in the user's language, e.g. 集中流动性 IL 对照表")
    brief: str = Field(min_length=20, max_length=6000, description="Everything needed to do the work without asking again: what to produce and in what form "
                       "(a file name if it is a document), the facts and figures the user gave, the assumptions to use, and what counts as done")


class TrackGoalArgs(BaseModel):
    title: str = Field(min_length=1, max_length=80, description="A short name for what the user wants to follow or keep doing, in their language, e.g. DeFi / 质押 / 再质押长期跟踪")
    detail: str = Field(default="", max_length=1000, description="What they care about in it and how often they look at it, in one or two sentences")


class ScheduleArgs(BaseModel):
    title: str = Field(min_length=1, max_length=80, description="A short name for the scheduled work, in the user's language, e.g. L2 每月深度复盘")
    brief: str = Field(min_length=20, max_length=6000, description="What to do on each run, complete enough to do without this conversation: what to look up or produce, "
                       "what the user cares about, the form of the result. For a reminder: what to remind them of")
    every: Literal["once", "day", "week", "month"] = Field(description="How often it runs. once: a single run on date")
    time: str = Field(default="09:00", description="Time of day in 24-hour HH:MM, in the user's local time")
    weekday: Literal["", "mon", "tue", "wed", "thu", "fri", "sat", "sun"] = Field(default="", description="For every=week: the day of the week")
    day: int = Field(default=0, ge=0, le=31, description="For every=month: the day of the month, 1 to 31")
    date: str = Field(default="", description="For every=once: the date as YYYY-MM-DD")


class ScheduleIdArgs(BaseModel):
    schedule_id: str = Field(description="The id of the schedule, from agenda")


class WeatherArgs(BaseModel):
    place: str = Field(min_length=1, max_length=80, description="The city, e.g. 北京, 深圳, Tokyo. For a city outside China its English name is the safest")
    days: int = Field(default=3, ge=1, le=7, description="How many days of forecast, today included")


class CryptoArgs(BaseModel):
    asset: str = Field(min_length=1, max_length=40, description="The ticker or name of one crypto asset, e.g. BTC, ETH, Solana")


class CurrencyArgs(BaseModel):
    amount: float = Field(default=1, gt=0, le=1e12, description="How much of the source currency")
    source: str = Field(min_length=3, max_length=3, description="ISO code of the currency to convert from, e.g. USD")
    target: str = Field(min_length=3, max_length=3, description="ISO code of the currency to convert to, e.g. CNY")


class WorldTimeArgs(BaseModel):
    place: str = Field(min_length=1, max_length=80, description="A city (东京, New York) or an IANA time zone (Europe/London)")


class CountdownArgs(BaseModel):
    date: str = Field(description="The date as YYYY-MM-DD. Work it out from the current date you are given; for a lunar or movable holiday be sure of the date first")
    label: str = Field(default="", max_length=40, description="What the date is, in the user's language, e.g. 春节, 项目上线")


Category = Literal["餐饮", "交通", "购物", "日用", "住房", "娱乐", "医疗", "教育", "通讯", "旅行", "订阅", "转账", "其他"]
Period = Field(default="this_month", description="today, this_week, this_month, last_month, this_year, a month as YYYY-MM, or a year as YYYY")


class ExpenseArgs(BaseModel):
    amount: float = Field(gt=0, le=1e9, description="The amount in yuan, e.g. 35 or 128.5")
    category: Category = Field(description="The closest category")
    merchant: str = Field(default="", max_length=80, description="Where or to whom it was paid, if the user said")
    note: str = Field(default="", max_length=200, description="What it was for, in a few words, e.g. 午饭")
    date: str = Field(default="", description="YYYY-MM-DD when it was not today")
    income: bool = Field(default=False, description="True when this is money received, not spent")


class SpendingArgs(BaseModel):
    period: str = Period
    category: Literal["", "餐饮", "交通", "购物", "日用", "住房", "娱乐", "医疗", "教育", "通讯", "旅行", "订阅", "转账", "其他"] = Field(default="", description="Only this category; empty for all spending")


class BudgetArgs(BaseModel):
    category: Literal["总计", "餐饮", "交通", "购物", "日用", "住房", "娱乐", "医疗", "教育", "通讯", "旅行", "订阅", "转账", "其他"] = Field(description="总计 for all spending in a month, or one category")
    amount: float = Field(ge=0, le=1e9, description="The monthly limit in yuan; 0 removes the budget")


class TransactionsArgs(BaseModel):
    period: str = Period
    category: Literal["", "餐饮", "交通", "购物", "日用", "住房", "娱乐", "医疗", "教育", "通讯", "旅行", "订阅", "转账", "其他"] = ""
    merchant: str = Field(default="", max_length=80, description="Only payments whose payee contains this text")


class FindToolsArgs(BaseModel):
    query: str = Field(max_length=200, description="What you need a tool for, in a few keywords, e.g. github issue create")


class BrowserOpenArgs(BaseModel):
    url: str = Field(max_length=2000, description="Full http(s) address")


class BrowserTargetArgs(BaseModel):
    target: str = Field(max_length=300, description="The element: a ref from browser_elements (e.g. e12) or a CSS selector")


class BrowserTypeArgs(BrowserTargetArgs):
    text: str = Field(max_length=4000, description="Text to put in the field; it replaces what is there")
    submit: bool = Field(default=False, description="Press Enter afterwards, e.g. to run a search")


class BrowserEvalArgs(BaseModel):
    script: str = Field(max_length=4000, description="JavaScript evaluated in the page; the value of the last expression (or a resolved promise) is returned")


class SaveFileArgs(BaseModel):
    name: str = Field(description="File name, e.g. plan.md. If the file exists it is replaced (the old version is kept in history).")
    content: str = Field(description="Full UTF-8 text content. For a long document put only the first part here and add the rest with append_file.")


class AppendFileArgs(BaseModel):
    name: str = Field(description="File name in the workspace; created if it does not exist")
    content: str = Field(description="Text to add at the end of the file")


class EditFileArgs(BaseModel):
    # Flat on purpose: local models write plain text arguments reliably, but not long text nested in JSON lists.
    name: str = Field(description="File name in the workspace")
    old_text: str = Field(min_length=1, description="Text to find, copied exactly from the file")
    new_text: str = Field(description="Text to put in its place (empty deletes it)")
    replace_all: bool = Field(default=False, description="Replace every occurrence (e.g. renaming a character). Otherwise old_text must occur exactly once")


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


class RunCommandArgs(BaseModel):
    command: str = Field(max_length=4000, description="zsh command line, non-interactive (use -y / --yes style flags; there is no stdin)")
    reason: str = Field(default="", max_length=300, description="One short sentence in the user's language: why this command, shown to the user when they approve it")
    cwd: str = Field(default="", description="Working directory (absolute, or ~/...). Empty: the default directory")
    timeout: int = Field(default=300, ge=5, le=1800, description="Seconds before the command is stopped; installs may need 900")
    background: bool = Field(default=False, description="For a command that keeps running (a dev server, a watcher): start it and return at once with a command_id. "
                                                        "Read what it prints with command_output and end it with stop_command")


class FinishGoalArgs(BaseModel):
    status: Literal["done", "blocked"] = Field(description="done: the goal is achieved and you verified it. blocked: you cannot continue without the user")
    summary: str = Field(max_length=3000, description="For the user, in their language: what was done (done) or what you need from them (blocked)")
    evidence: str = Field(default="", max_length=2000, description="For done: the tool results that prove the goal is met, e.g. the test run that passed")


class WriteDocumentArgs(BaseModel):
    # Flat text on purpose (see EditFileArgs).
    name: str = Field(default="", description="File name when the whole document goes into one file, e.g. script.md. Leave empty when files is given")
    brief: str = Field(max_length=6000, description="Everything a writer needs: what the piece is, the characters with their names and traits, setting, tone and style, "
                                                    "what must happen overall, formatting rules (e.g. scene headings and full dialogue)")
    outline: str = Field(max_length=6000, description="One line per section, in order: its title and what happens in it, e.g. 第 3 集：对峙 — 沈知意在董事会上拿出证据")
    length: int = Field(default=800, ge=100, le=4000, description="Characters per section the user asked for (e.g. 1000 for 每集 1000 字)")
    files: str = Field(default="", max_length=2000, description="Only when the user wants every section in its own file (then leave name empty): one file name per line, same order as the outline, e.g. ep1.md")


class PlanArgs(BaseModel):
    # Flat text on purpose (see EditFileArgs).
    steps: str = Field(default="", max_length=4000, description="The parts of the work, one per line, in order, e.g. 写第 3 集并保存为 ep3.md. Give it on the first call; later only to change the plan")
    done: str = Field(default="", max_length=200, description="Numbers of the steps finished so far, e.g. 1,2,3")


class CodeProposeArgs(BaseModel):
    project_id: str
    instruction: str = Field(description="What to change and why, in detail")
    files: list[str] = Field(default_factory=list, max_length=12, description="Relevant existing files to read")


class WebFetchArgs(BaseModel):
    url: str = Field(max_length=2000, description="Full http(s) address of a public web page")


class WebSearchArgs(BaseModel):
    query: str = Field(max_length=500, description="What to look up, as a specific search question")


class SkillArgs(BaseModel):
    name: str = Field(description="Skill name exactly as listed")
    file: str = Field(default="", max_length=200, description="One of the skill's other files to read (from the files list load_skill returned). Empty: the skill's instructions")


class HomeDevicesArgs(BaseModel):
    query: str = Field(default="", description="Words from the device name, e.g. 客厅 灯. Empty lists everything")
    room: str = Field(default="", description="Only this room")
    type: Literal[HOME_TYPES] | None = Field(default=None, description="Only this kind of device")


class HomeControlArgs(BaseModel):
    device_id: str = Field(description="Device id exactly as returned by home_devices")
    action: Literal[HOME_ACTIONS]
    value: str | float | None = Field(default=None, description="For set_brightness / set_position: 0-100. set_color: #rrggbb. set_color_temperature: kelvin. set_temperature: °C. set_mode: the mode name")


class DelegateArgs(BaseModel):
    task: str = Field(max_length=6000, description="A complete, self-contained brief: the goal, what you already know, and exactly what to report back. The helper sees nothing else of this conversation")
    title: str = Field(max_length=60, description="A few words naming the subtask, in the user's language")


MAX_FILE_BYTES = 400000
SEARCH_BYTES = 2_000_000    # text read by one search_files call
SEARCH_MATCHES = 100
DEFER_TOOLS = 24            # with more MCP tools than this, they are found with find_tools instead of all being listed in every prompt
BROWSER_TOOLS = {"browser_open", "browser_read", "browser_elements", "browser_screenshot", "browser_click", "browser_type", "browser_eval"}


def glob_regex(pattern):
    """A glob on a relative path as a regular expression: * stays inside one folder, ** crosses folders, {a,b} is either."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
            continue
        if pattern.startswith("**", i):
            out, i = out + ".*", i + 2
            continue
        c, end = pattern[i], pattern.find("}", i)
        if c == "*":
            out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        elif c == "{" and end > i:
            out += "(?:" + "|".join(re.escape(part) for part in pattern[i + 1:end].split(",")) + ")"
            i = end
        else:
            out += re.escape(c)
        i += 1
    # "*.py" means any Python file, wherever it is.
    return re.compile(("" if "/" in pattern else "(?:.*/)?") + out + r"\Z", re.I)
IMAGE_SIZES = {"square": (1024, 1024), "portrait": (768, 1024), "landscape": (1024, 768)}
VIDEO_SIZES = {"square": (448, 448), "portrait": (352, 608), "landscape": (608, 352)}


@dataclass
class Tool:
    name: str
    label: str            # short Chinese label shown in the UI trace
    description: str
    args: type[BaseModel]
    run: Callable         # may return a coroutine; the agent awaits it
    effect: Literal["read", "write", "job", "command", "goal", "agent", "writer", "external"] = "read"  # command, goal, agent and writer are run by the agent loop
    parameters: dict | None = None      # a ready JSON schema (MCP tools) instead of an argument model
    confirm: Callable | None = None     # async (args) -> text shown to the user for approval, or None to run without asking
    summary: Callable | None = None     # (result) -> one line for the user

    def schema(self):
        if self.parameters is not None:
            params = {k: v for k, v in self.parameters.items() if k != "$schema"}
            params.setdefault("type", "object")
            params.setdefault("properties", {})
            return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": params}}
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
        failure = {"error": self.result.get("error", "失败")}
        if "output" in self.result:  # a failed command's output is what the model needs to fix it
            failure.update({k: self.result[k] for k in ("exit_code", "timed_out", "output") if k in self.result})
        text = dumps(self.result if self.ok else failure)
        return text if len(text) <= limit else text[:limit] + "…(truncated)"


class ToolRegistry:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.agenda = Agenda(store)
        self.ledger = Ledger(store)
        self.coding = Coding(settings, store)
        self.llm = self.memory = self.skills = self.home = self.mcp = None  # attached by the worker; tools that need them are offered only then
        self.shell = self.browser = self.media = None
        self.page = {"url": "", "elements": {}}   # the page the browser last reported, so an approval card can name what will be clicked
        self.tools = {t.name: t for t in self.build()}
        self.dynamic = set()

    def attach(self, llm, memory, skills, home, mcp, shell=None, browser=None, media=None):
        self.llm, self.memory, self.skills, self.home, self.mcp = llm, memory, skills, home, mcp
        self.shell, self.browser, self.media = shell, browser, media

    def deferred(self):
        """True when there are too many MCP tools to list them all: the model then looks them up with find_tools."""
        return len(self.dynamic) > DEFER_TOOLS

    def refresh(self):
        """Re-reads the tools that MCP servers currently offer."""
        for name in self.dynamic:
            self.tools.pop(name, None)
        self.dynamic = set()
        for name, server, tool, ask in (self.mcp.tools() if self.mcp else []):
            if name in self.tools:
                continue

            async def run(arguments, server=server, tool=tool):
                return {"output": await server.call(tool["name"], arguments)}

            async def confirm(arguments, server=server, tool=tool):
                return f"{server.name} · {tool['name']} {dumps(arguments)[:600]}"
            self.tools[name] = Tool(name, f"{server.name} · {tool['name']}"[:40], str(tool.get("description") or tool["name"])[:1500], None, run, "external",
                                    parameters=tool.get("inputSchema") or {"type": "object", "properties": {}}, confirm=confirm if ask else None,
                                    summary=lambda r: "完成")
            self.dynamic.add(name)

    def available(self, model=None):
        """Names of optional tools that make sense right now, given what the user has set up and which model answers."""
        names = {"web_fetch"} | ({"find_tools"} if self.deferred() else self.dynamic)
        if self.llm and self.llm.is_local(model) and self.llm.search_model():
            names.add("web_search")
        if self.llm:
            names.add("delegate")
        if self.shell:
            names |= {"command_output", "stop_command"}
        if self.browser and self.browser.available():
            names |= BROWSER_TOOLS
        if self.skills and self.skills.enabled():
            names.add("load_skill")
        if self.home and self.home.config():
            names |= {"home_devices", "home_control"}
        return names

    def build(self):
        T = Tool
        return [
            T("calculate", "计算", "Evaluate an arithmetic expression exactly. Use only when the user's request needs a numeric result.", CalculateArgs,
              lambda a: {"value": tidy(calculate(a.expression))}),
            T("list_files", "查看工作区文件", "List text documents in the user's assistant workspace.", NoArgs,
              lambda a: self.list_files()),
            T("read_file", "读取文件", "Read a text document from the assistant workspace. For a long file, pass offset and limit to read a range of lines (returned with line numbers).", ReadFileArgs,
              lambda a: self.read_file(a.name, a.offset, a.limit)),
            T("find_files", "查找文件", "Find files by a glob pattern on their path, in the assistant workspace or in a connected code project. Returns the matching paths.", FindFilesArgs,
              lambda a: self.find_files(a.pattern, a.project_id), summary=lambda r: f"{r['total']} 个文件"),
            T("search_files", "搜索文件内容", "Search the contents of files with a regular expression, in the assistant workspace or in a connected code project. "
              "Returns each matching line with its file and line number.", SearchFilesArgs, self.search_files,
              summary=lambda r: f"{len(r['matches'])} 处匹配 · {r['files']} 个文件"),
            T("save_file", "保存文件", "Write a workspace document: creates the file, or replaces it when the user asked to update it (the previous version is kept). "
              "Write a long document in parts: the first part here, each further part with append_file.", SaveFileArgs,
              lambda a: self.save_file(a.name, a.content), "write"),
            T("append_file", "续写文件", "Add text to the end of a workspace document. Use it to write a long document part by part.", AppendFileArgs,
              lambda a: self.append_file(a.name, a.content), "write"),
            T("write_document", "撰写长文档", "Write a long document that has several sections (episodes, chapters, parts) into the workspace. "
              "You give the brief and the outline; each section is then written in full, one after another, and saved. Use it instead of save_file whenever a document has more than two sections "
              "or is longer than one call can hold.", WriteDocumentArgs, None, "writer"),
            T("edit_file", "修改文件", "Change part of a workspace document by replacing one piece of text, leaving the rest untouched. "
              "Prefer this over rewriting the whole file. Read the file first and copy old_text exactly. Call it once per change.", EditFileArgs,
              lambda a: self.edit_file(a.name, a.old_text, a.new_text, a.replace_all), "write"),
            T("recall", "查找记忆", "Search saved memories and uploaded or generated media assets by keywords (any keyword may match). An empty query lists the most recent ones.", RecallArgs,
              lambda a: self.recall(a.query)),
            T("remember", "保存记忆", "Save one lasting fact about the user: when they ask you to remember something, or when they tell you a standing interest, habit, "
              "preference or fact about themselves that will matter in later conversations. Not for one-off requests, and never passwords or keys.", RememberArgs,
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
            T("run_command", "运行命令", "Run a shell command on the user's Mac (zsh login shell): install packages or plugins, run scripts and tests, use git, inspect the system. "
              "The user sees the command and approves it before it runs. Returns exit_code and output. Run one step at a time and read the output before the next.",
              RunCommandArgs, None, "command"),
            T("command_output", "查看命令输出", "Read what a background command (run_command with background) has printed so far and whether it is still running.", CommandIdArgs,
              lambda a: self.shell.output(a.command_id), summary=lambda r: "运行中" if r["running"] else f"已结束 · 退出码 {r['exit_code']}"),
            T("stop_command", "停止命令", "Stop a background command.", CommandIdArgs,
              lambda a: self.shell.kill(a.command_id), "write", summary=lambda r: "已停止"),
            T("find_tools", "查找工具", "Find tools from the user's connected MCP servers by keywords. The tools it returns can be called from your next step on.", FindToolsArgs,
              lambda a: self.find_tools(a.query), summary=lambda r: f"{len(r['tools'])} 个工具"),
            T("browser_open", "打开网页", "Open a page in a browser window on the user's Mac and return its title and the start of its text. Use the browser instead of web_fetch "
              "for pages that need JavaScript, clicking, forms, or a sign-in the user does themselves in that window.", BrowserOpenArgs,
              lambda a: self.browsed(self.browser.open(a.url)), summary=lambda r: (r.get("title") or r["url"])[:60]),
            T("browser_read", "读取页面", "Read the text of the page now open in the browser.", NoArgs,
              lambda a: self.browsed(self.browser.read()), summary=lambda r: (r.get("title") or r["url"])[:60]),
            T("browser_elements", "列出页面元素", "List the links, buttons and fields on the open page, each with a ref to pass to browser_click or browser_type.", NoArgs,
              lambda a: self.browsed(self.browser.dom()), summary=lambda r: f"{len(r['elements'])} 个元素"),
            T("browser_screenshot", "网页截图", "Save a screenshot of the open page for the user. You do not see the picture: read the page with browser_read.", NoArgs,
              self.browser_screenshot, summary=lambda r: "已截图"),
            T("browser_click", "点击页面", "Click one element on the open page.", BrowserTargetArgs,
              lambda a: self.browsed(self.browser.click(a.target)), "external", confirm=self.confirm_browser("点击"), summary=lambda r: str(r.get("clicked") or "已点击")[:60]),
            T("browser_type", "输入文字", "Type into one field on the open page.", BrowserTypeArgs,
              lambda a: self.browsed(self.browser.type(a.target, a.text, a.submit)), "external", confirm=self.confirm_browser("输入"),
              summary=lambda r: f"已输入 {r.get('typed', 0)} 字"),
            T("browser_eval", "运行页面脚本", "Run JavaScript in the open page and return its value. Only when reading and clicking cannot do it.", BrowserEvalArgs,
              lambda a: self.browser.evaluate(a.script), "external", confirm=self.confirm_browser("运行脚本"), summary=lambda r: "完成"),
            T("web_fetch", "读取网页", "Read the text of one public web page by its address. Use it for a link the user gave or a source you need to read in full.",
              WebFetchArgs, lambda a: web.fetch(a.url), summary=lambda r: (r["title"] or r["url"])[:60]),
            T("web_search", "联网搜索", "Search the web for current information (news, prices, weather, documentation). Returns an answer with source links.",
              WebSearchArgs, lambda a: self.web_search(a.query), summary=lambda r: f"{len(r['sources'])} 个来源"),
            T("load_skill", "加载技能", "Load the full instructions of a listed skill before doing a task it covers.", SkillArgs,
              lambda a: self.skills.read(a.name, a.file), summary=lambda r: f"已加载 {r['name']}" + (f" · {r['file']}" if r.get("file") else "")),
            T("home_devices", "查看家居设备", "List the user's smart-home devices with their rooms, current state and the actions each supports. Call it before home_control to get device ids.",
              HomeDevicesArgs, lambda a: self.home.search(a.query, a.room, a.type or ""), summary=lambda r: f"{r['total']} 个设备"),
            T("home_control", "控制家居设备", "Perform one action on one smart-home device. For several devices, call it once per device. Locks, doors and cameras ask the user first.",
              HomeControlArgs, lambda a: self.home.control(a.device_id, a.action, a.value), "external", confirm=self.confirm_home,
              summary=lambda r: f"{r['device']} · {r['action']}" + (f" {r['value']}" if "value" in r else "")),
            T("delegate", "委派子任务", "Hand a self-contained research or reading subtask to a helper that works on it separately and reports back. "
              "Use it for side investigations that would otherwise fill this conversation with detail. The helper can read and search but cannot change anything.",
              DelegateArgs, None, "agent"),
            T("ask_user", "向你提问", "End your reply with a question the user answers by tapping one of a few options (they can also type their own answer). "
              "Use it when the next step is the user's choice: which direction to take, how often, which of a few things to do first. "
              "Say what you have to say first; this call comes last and ends your turn. Not for a question with an open answer, and not after an answer that leaves nothing to decide.",
              AskUserArgs, None, "ask"),
            T("start_task", "开始后台任务", "Start a longer piece of work in the background: research across many sources, a document, a table or an analysis to build, "
              "anything that needs many steps. It runs on its own while the conversation goes on, and its result is posted in this conversation when it is done. "
              "Not for a question you can answer now or an action that takes a step or two.", StartTaskArgs, None, "task"),
            T("weather", "查天气", "Current weather and the forecast for a city. The user is shown a weather card with the figures.", WeatherArgs,
              lambda a: cards.weather(a.place, a.days), summary=lambda r: f"{r['place']} · {r['now']['temperature']}° {r['now']['sky']}"),
            T("crypto_price", "查币价", "The current USD price of one crypto asset with its 24-hour and 7-day change. The user is shown a price card with a 7-day chart.", CryptoArgs,
              lambda a: cards.crypto_price(a.asset), summary=lambda r: f"{r['symbol']} · ${r['price_usd']:,} · {r['change_24h_percent']:+.2f}%"),
            T("convert_currency", "换算汇率", "Convert an amount between two currencies at the latest reference rate. The user is shown a card with the result and the rate.", CurrencyArgs,
              lambda a: cards.convert_currency(a.amount, a.source, a.target), summary=lambda r: f"{r['amount']:g} {r['from']} = {r['converted']:,} {r['to']}"),
            T("world_time", "查当地时间", "The current time in a city or time zone and how far it is from the user's own clock. The user is shown a clock card.", WorldTimeArgs,
              lambda a: cards.world_time(a.place), summary=lambda r: f"{r['place']} · {r['time'][-5:]}"),
            T("countdown", "倒计时", "How many days from today until a date (or since it). The user is shown a countdown card.", CountdownArgs,
              lambda a: cards.countdown(a.date, a.label), summary=lambda r: f"{r['days']} 天" if r["days"] > 0 else "就是今天" if not r["days"] else f"已过 {-r['days']} 天"),
            T("log_expense", "记一笔", "Record one payment (or one income) in the user's spending ledger when they tell you about it, e.g. 午饭 35. "
              "The user is shown a card with the entry and where the month stands.", ExpenseArgs, self.log_expense, "write",
              summary=lambda r: f"{r['recorded']['category']} · ¥{r['recorded']['amount']:g}"),
            T("spending_summary", "看支出", "How much the user spent in a period, by category, with the change from the period before and budget progress. "
              "All figures are added up for you: quote them, never do the sums yourself. It has no single payments and no payee names. The user is shown a card.",
              SpendingArgs, lambda a: self.ledger.summary(a.period, a.category), summary=lambda r: f"{r['period']} · ¥{r['spent']:,.2f} · {r['payments']} 笔"),
            T("set_budget", "设预算", "Set or remove a monthly spending limit, for all spending (总计) or one category.", BudgetArgs,
              self.set_budget, "write", summary=lambda r: r["summary"]),
            T("ledger_transactions", "看账单明细", "List the single payments in a period: time, amount, category, payee, note. Use it when the user asks which payments make up a figure.",
              TransactionsArgs, self.ledger_transactions, summary=lambda r: f"{len(r['transactions'])} 笔"),
            T("agenda", "查看目标和定时任务", "List what you are already tracking and running for the user: their long-term goals and the scheduled work, each with its id. "
              "Call it before saving a goal or a schedule, so nothing is saved twice.", NoArgs,
              lambda a: self.agenda.overview(), summary=lambda r: f"{len(r['goals'])} 个目标 · {len(r['schedules'])} 个定时任务"),
            T("track_goal", "保存长期目标", "Save something the user wants to follow or keep doing over time as a long-term goal they can see under Goals. "
              "A goal with nearly the same title is updated instead of duplicated.", TrackGoalArgs, None, "agenda"),
            T("schedule_task", "设置定时任务", "Set up work that runs by itself at a time or on a cadence (a weekly brief, a monthly review, a reminder) and posts its result in this conversation. "
              "The cadence must be the user's choice: ask first if they have not said how often.", ScheduleArgs, None, "agenda"),
            T("cancel_schedule", "取消定时任务", "Cancel a scheduled task by its id.", ScheduleIdArgs,
              lambda a: self.agenda.cancel(a.schedule_id), "write", summary=lambda r: f"已取消 · {r['cancelled']}"[:60]),
            T("update_plan", "更新计划", "Keep a checklist of the parts of a larger piece of work; the user sees it. Call it first with steps (one per line), "
              "then with done (the finished step numbers) whenever a part is finished. Not for a request that is one action or one answer.",
              PlanArgs, None, "goal"),
            T("finish_goal", "结束目标", "Goal mode only: end the goal. Call it with status done only after a tool result proves the goal is met; "
              "with status blocked when you need the user (missing information, a permission, or the goal is impossible).",
              FinishGoalArgs, None, "goal"),
        ]

    OPTIONAL = {"web_fetch", "web_search", "load_skill", "home_devices", "home_control", "delegate", "command_output", "stop_command", "find_tools"} | BROWSER_TOOLS

    def default_names(self, model=None):
        return {n for n in self.tools if n not in ("finish_goal", "update_plan", "ask_user", "start_task") and n not in self.OPTIONAL and n not in self.dynamic} | self.available(model)

    def schemas(self, names=None):
        names = names or self.default_names()
        return [t.schema() for t in self.tools.values() if t.name in names]

    # ---- harness entry points: validate, never raise ----
    def prepare(self, name, arguments):
        """(tool, validated arguments) or an Outcome describing why the call cannot run."""
        tool = self.tools.get(name)
        if not tool or tool.run is None:
            return Outcome(False, {"error": f"没有名为 {name} 的工具"}, f"未知工具 {name}")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                return Outcome(False, {"error": "参数不是有效的 JSON object：" + arguments[:200]}, "参数格式错误")
        if tool.parameters is not None:
            return tool, arguments if isinstance(arguments, dict) else {}
        try:
            return tool, tool.args.model_validate(arguments or {})
        except ValidationError as exc:
            detail = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}" for e in exc.errors()[:5])
            return Outcome(False, {"error": "参数无效：" + detail}, "参数无效")

    FAILURES = (ValueError, KeyError, OSError, SyntaxError, ZeroDivisionError, ValidationError, McpError, ConnectorError, RuntimeError)

    def finished(self, tool, result):
        return Outcome(True, result, tool.summary(result) if tool.summary else self.summarize(tool.name, result))

    def invoke(self, name, arguments):
        """Runs a synchronous tool."""
        ready = self.prepare(name, arguments)
        if isinstance(ready, Outcome):
            return ready
        tool, args = ready
        try:
            result = tool.run(args)
            if inspect.isawaitable(result):
                result.close()
                raise ValueError(f"{name} 需要通过对话调用")
        except self.FAILURES as exc:
            return Outcome(False, {"error": str(exc)[:1000]}, str(exc)[:120])
        return self.finished(tool, result)

    async def call(self, name, arguments, approve=None):
        """Runs any tool. approve(text) is awaited first for tools that need the user's go-ahead; it returns an Outcome to stop the call."""
        ready = self.prepare(name, arguments)
        if isinstance(ready, Outcome):
            return ready
        tool, args = ready
        try:
            if tool.confirm and approve:
                question = await tool.confirm(args)
                if question:
                    refused = await approve(question)
                    if refused:
                        return refused
            result = tool.run(args)
            if inspect.isawaitable(result):
                result = await result
        except self.FAILURES as exc:
            return Outcome(False, {"error": str(exc)[:1000] or type(exc).__name__}, (str(exc) or type(exc).__name__)[:120])
        return self.finished(tool, result)

    def confirm_browser(self, verb):
        async def confirm(a):
            if hasattr(a, "script"):
                what = a.script
            else:
                seen = self.page["elements"].get(a.target)
                what = (f"「{seen['text'] or seen.get('name') or seen.get('placeholder') or a.target}」（{seen['role']}）" if seen else a.target) + (f" ← {a.text}" if hasattr(a, "text") else "")
            return f"浏览器 · {verb} {what[:400]}\n{self.page['url'] or '当前页面'}"
        return confirm

    async def browsed(self, work):
        """Runs a browser call and remembers where it left the page."""
        result = await work
        self.page["url"] = result.get("url", self.page["url"])
        if "elements" in result:
            self.page["elements"] = {e["ref"]: e for e in result["elements"]}
        return result

    async def browser_screenshot(self, a):
        shot = await self.browser.screenshot()
        result = {k: shot[k] for k in ("width", "height", "url", "title") if k in shot}
        path = Path(shot["path"])
        if self.media:  # in the library, where the user can open it
            result["asset_id"] = self.media.record(path, f"{(shot.get('title') or '网页截图')[:40]}.png", "image/png", "screenshot")
        result["note"] = "Saved for the user. You cannot see the picture."
        return result

    def find_tools(self, query):
        terms = [t for t in re.split(r"[\s,，]+", query.casefold()) if t][:8]
        scored = []
        for name in self.dynamic:
            tool = self.tools[name]
            text = (name + " " + tool.label + " " + tool.description).casefold()
            score = sum((3 if term in name.casefold() else 1) for term in terms if term in text)
            if score:
                scored.append((-score, name, tool.description[:200]))
        return {"tools": [{"name": name, "description": description} for _, name, description in sorted(scored)[:8]],
                "note": "Call these by name from your next step. If none fits, try other keywords."}

    async def confirm_home(self, a):
        device = await self.home.find(a.device_id)
        if not self.home.needs_confirmation(device, a.action):
            return None
        return f"{device['name']}（{device['room'] or device['type']}）→ {a.action}" + (f" {a.value}" if a.value is not None else "")

    async def web_search(self, query):
        model = self.llm.search_model() if self.llm else None
        if not model:
            raise ValueError("还没有配置可联网搜索的云端模型")
        reply = await self.llm.stream([{"role": "system", "content": "Search the web and answer the question with the specific facts, figures and dates you found. Be concise. Reply in the language of the question."},
                                       {"role": "user", "content": query}], max_tokens=1500, temperature=0.2, model=model, search=True)
        return {"answer": reply.text, "sources": reply.citations[:10]}

    # ---- direct use by HTTP endpoints: raises ValueError like before ----
    def execute(self, name, arguments):
        tool = self.tools.get(name)
        if not tool or tool.run is None:
            raise ValueError(f"没有名为 {name} 的工具")
        return tool.run(tool.args.model_validate(arguments))

    @staticmethod
    def summarize(name, r):
        if name == "calculate": return f"= {r['value']}"
        if name == "list_files": return f"{len(r['files'])} 个文件"
        if name == "read_file": return f"{r['name']} · {len(r['content'])} 字"
        if name == "save_file": return f"已保存为 {r['saved']} · {r['characters']} 字" + ("（已替换旧版本）" if r.get("replaced") else "")
        if name == "append_file": return f"{r['saved']} · 新增 {r['added']} 字，共 {r['characters']} 字"
        if name == "edit_file": return f"{r['saved']} · 修改 {r['replacements']} 处"
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
        return {"files": [{"name": p.name, "bytes": p.stat().st_size, "modified": int(p.stat().st_mtime)} for p in sorted(folder.iterdir()) if p.is_file() and not p.is_symlink()][:200]}

    def read_file(self, name, offset=0, limit=0):
        path = self.workspace_path(name)
        if not path.is_file():
            raise ValueError("文件不存在")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f"文件超过 {MAX_FILE_BYTES // 1000} KB，请先拆分")
        text = path.read_text()
        if not offset and not limit:
            return {"name": path.name, "content": text}
        lines, first = text.splitlines(), max(offset, 1)
        chunk = lines[first - 1:first - 1 + limit] if limit else lines[first - 1:]
        return {"name": path.name, "lines": len(lines), "from": first, "to": first + len(chunk) - 1,
                "content": "\n".join(f"{n}\t{line}" for n, line in enumerate(chunk, first))}

    def scope(self, project_id):
        """The files a search covers, and how to read one: the workspace, or a connected code project."""
        if project_id:
            return self.coding.tree(project_id)["files"], lambda path: self.coding.read(project_id, path)
        return [{"path": f["name"], "bytes": f["bytes"]} for f in self.list_files()["files"]], lambda path: self.workspace_path(path).read_text()

    def find_files(self, pattern, project_id=""):
        wanted = glob_regex(pattern.strip().lstrip("./"))
        found = [f for f in self.scope(project_id)[0] if wanted.match(f["path"])]
        return {"files": found[:200], "total": len(found)}

    def search_files(self, a):
        try:
            wanted = re.compile(a.pattern, re.I if a.ignore_case else 0)
        except re.error as exc:
            raise ValueError(f"正则表达式无效：{exc}")
        files, read = self.scope(a.project_id)
        only = glob_regex(a.glob.strip().lstrip("./")) if a.glob.strip() else None
        matches, hit, budget, truncated = [], set(), SEARCH_BYTES, False
        for item in files:
            if only and not only.match(item["path"]):
                continue
            budget -= item["bytes"]
            if budget < 0:
                truncated = True
                break
            try:
                lines = read(item["path"]).splitlines()
            except (ValueError, OSError, UnicodeError):
                continue
            for n, line in enumerate(lines):
                if not wanted.search(line[:2000]):
                    continue
                if len(matches) >= SEARCH_MATCHES:
                    truncated = True
                    break
                match = {"path": item["path"], "line": n + 1, "text": line.strip()[:300]}
                if a.context:
                    match["around"] = "\n".join(f"{i + 1}\t{lines[i][:200]}" for i in range(max(0, n - a.context), min(len(lines), n + a.context + 1)))
                matches.append(match)
                hit.add(item["path"])
            if len(matches) >= SEARCH_MATCHES:
                break
        return {"matches": matches, "files": len(hit), "truncated": truncated}

    def keep_version(self, path):
        """Copies a document into .history before it changes, so an update by the assistant can be undone."""
        folder = path.parent / ".history"
        folder.mkdir(exist_ok=True)
        (folder / f"{path.stem}.{time.strftime('%Y%m%d-%H%M%S')}-{new_id()[:4]}{path.suffix}").write_text(path.read_text())
        versions = sorted(folder.glob(f"{glob_escape(path.stem)}.*{glob_escape(path.suffix)}"), key=lambda p: p.stat().st_mtime)
        for stale in versions[:-20]:
            stale.unlink()

    def save_file(self, name, content, replace=True):
        path = self.workspace_path(name)
        if len(content.encode()) > MAX_FILE_BYTES:
            raise ValueError(f"文件超过 {MAX_FILE_BYTES // 1000} KB")
        if not content.strip():
            raise ValueError("内容是空的")
        existed = path.exists()
        if existed and not replace:  # imports from the user never replace a document
            path = path.with_name(f"{path.stem}-{new_id()[:8]}{path.suffix}")
        elif existed:
            self.keep_version(path)
        path.write_text(content)
        result = {"saved": path.name, "bytes": path.stat().st_size, "characters": len(content)}
        if existed and replace:
            result.update(replaced=True, note="The previous version was replaced (a copy is kept in history).")
        return result

    def append_file(self, name, content):
        path = self.workspace_path(name)
        if not content.strip():
            raise ValueError("内容是空的")
        before = path.read_text() if path.is_file() else ""
        if len((before + content).encode()) > MAX_FILE_BYTES:
            raise ValueError(f"文件会超过 {MAX_FILE_BYTES // 1000} KB，请另存为新文件")
        joined = before + ("" if not before or before.endswith("\n") else "\n") + ("\n" if before and not content.startswith("\n") else "") + content
        path.write_text(joined)
        return {"saved": path.name, "added": len(content), "characters": len(joined)}

    def edit_file(self, name, old_text, new_text, replace_all=False):
        path = self.workspace_path(name)
        if not path.is_file():
            raise ValueError("文件不存在")
        text = path.read_text()
        if not text.count(old_text) and "\\n" in old_text:
            # Newlines sent as the two characters \n: a slip in escaping, not different text.
            old_text, new_text = (t.replace("\\n", "\n").replace("\\t", "\t") for t in (old_text, new_text))
        if old_text == new_text:
            raise ValueError("old_text 和 new_text 完全一样，文件没有改动。要在文件末尾加内容请用 append_file；要在中间插入，new_text 里写上原文加新内容")
        count = text.count(old_text)
        if not count:
            # A local model rarely copies a passage character for character: a close, unambiguous match is what it meant.
            close, score = nearest(text, old_text)
            if score >= .8:
                old_text, count = close, text.count(close)
            elif close:
                raise ValueError(f"old_text 在 {name} 里找不到。最接近的原文是：\n{close[:600]}\n请逐字用它作 old_text")
            else:
                raise ValueError(f"old_text 在 {name} 里找不到，请先 read_file 再逐字复制")
        if count > 1 and not replace_all:
            raise ValueError(f"old_text 在 {name} 里出现了 {count} 次；多带一些上下文让它唯一，或设 replace_all")
        text = text.replace(old_text, new_text)
        if len(text.encode()) > MAX_FILE_BYTES:
            raise ValueError(f"文件会超过 {MAX_FILE_BYTES // 1000} KB")
        self.keep_version(path)
        path.write_text(text)
        return {"saved": path.name, "replacements": count, "characters": len(text), "replaced": old_text[:300]}

    def recall(self, query):
        # Any keyword may match, so "红色圆形 图片" still finds 红色圆形.png.
        terms = [t for t in query[:200].replace("，", " ").replace(",", " ").split() if t][:8]
        likes = ["%" + t + "%" for t in terms]
        memory_where = " OR ".join("title LIKE ? OR content LIKE ?" for _ in likes) or "1"
        asset_where = " OR ".join("name LIKE ? OR kind LIKE ?" for _ in likes) or "1"
        pairs = [x for like in likes for x in (like, like)]
        memories = (self.memory.search(query) if self.memory else
                    self.store.all(f"SELECT id,title,content FROM memories WHERE enabled=1 AND ({memory_where}) ORDER BY updated_at DESC LIMIT 20", pairs))
        return {"memories": memories,
                "assets": self.store.all(f"SELECT id,name,kind,mime,created_at FROM assets WHERE {asset_where} ORDER BY created_at DESC LIMIT 20", pairs)}

    def log_expense(self, a):
        when = None
        if a.date.strip():
            try:
                when = datetime.strptime(a.date.strip(), "%Y-%m-%d").replace(hour=12).timestamp()
            except ValueError:
                raise ValueError("date must be YYYY-MM-DD")
        return self.ledger.recorded(self.ledger.add(a.amount, a.category, a.merchant, a.note, when, income=a.income))

    def set_budget(self, a):
        self.ledger.set_budget(a.category, a.amount)
        label = "每月总支出" if a.category == TOTAL else f"每月{a.category}"
        if not a.amount:
            return {"removed": a.category, "summary": f"已取消{label}预算"}
        month = self.ledger.summary("this_month", "" if a.category == TOTAL else a.category)
        used = round(month["spent"] / a.amount * 100, 1)
        return {"budget": {"category": a.category, "monthly_limit": a.amount}, "spent_this_month": month["spent"], "used_percent": used, "summary": f"{label}预算 ¥{a.amount:g}",
                "card": {"kind": "ledger", "title": f"{label}预算", "subtitle": "本月进度", "value": f"¥{a.amount:,.0f}", "icon": "wallet", "bars": [],
                         "rows": [{"label": "本月已花", "value": f"¥{month['spent']:,.2f}"}, {"label": "已用", "value": f"{used:g}%"}], "note": "超出时我会在看支出时提醒你"}}

    def ledger_transactions(self, a):
        found = self.ledger.transactions(a.period, a.category, a.merchant, limit=60)
        return {"period": found["period"], "currency": "CNY", "transactions": [
            {"time": datetime.fromtimestamp(t["at"]).strftime("%Y-%m-%d %H:%M"), "amount": t["amount_cents"] / 100, "direction": t["direction"], "category": t["category"],
             "payee": t["merchant"], "note": t["note"]} for t in found["transactions"]]}

    def remember(self, title, content):
        if self.memory:
            return self.memory.save(title, content, importance=0.8, source="manual")
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
        result = {"job_id": ident, "kind": job["kind"], "status": job["status"], "status_label": labels.get(job["status"], job["status"]),
                  "phase": job["phase"], "error": job["error"], "asset_ids": job["result"].get("asset_ids", [])}
        if job["status"] in ("queued", "running"):
            result["note"] = ("Not finished. Background jobs do not advance while you are replying, so do not check again in this reply: "
                              "tell the user it is still in progress and where to find it. Do not describe its result.")
        elif job["status"] == "failed":
            result["note"] = "It failed. Tell the user the reason in plain words. Start it again only if the user asks."
        elif job["kind"] == "plan" and job["result"].get("project_id"):
            project = self.store.one("SELECT title,document FROM projects WHERE id=?", (job["result"]["project_id"],))
            if project:
                doc = project["document"]
                result["drama"] = {"title": project["title"], "logline": doc.get("logline", ""),
                                   "characters": [c["name"] for c in doc.get("characters", [])],
                                   "shots": [{"title": s["title"], "duration": s["duration"], "dialogue": s.get("dialogue", "")[:200]} for s in doc.get("shots", [])]}
        return result

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
