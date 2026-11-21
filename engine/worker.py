import asyncio
import json
import logging
import re
import time
from pathlib import Path
from . import drama
from .media import Media
from pydantic import ValidationError
from .schemas import DramaDocument
from .store import dumps
from .health_sync import HealthSync
from .checkin import Checkin
from .tools import ToolRegistry
from .agent import Agent
from .workflows import video_frames
from .catalog import MediaCatalog
from .coding import Coding
from .shell import Shell
from .browser import Browser
from .llm import LLM,Prefs
from .memory import Memory
from .skills import Skills
from .home import Home
from .mcp import Mcp

log=logging.getLogger("jurvis.worker")


def parse_json(text):
    text=text.strip()
    if text.startswith("```"):
        text=text.split("\n",1)[1].rsplit("```",1)[0].strip()
    return json.loads(text)


def plain_errors(exc):
    """Validation problems as short lines the model (and the user) can act on."""
    if isinstance(exc,ValidationError):
        return "\n".join(f"- {'.'.join(map(str,e['loc']))}: {e['msg']}（收到 {str(e.get('input'))[:60]}）" for e in exc.errors()[:12])
    return "- "+str(exc)[:500]


CLOUD_SLOTS = 4  # conversations answered by cloud models at the same time


class Worker:
    """Runs queued jobs. Work that needs this Mac's GPU (the local model, image and video generation) goes one at a
    time in the "local" lane; conversations answered by a cloud model run beside it in the "cloud" lane."""
    def __init__(self,settings,store,runtime):
        self.settings,self.store,self.runtime=settings,store,runtime
        self.prefs=Prefs(settings)
        self.llm=LLM(settings,runtime,self.prefs)
        self.memory=Memory(settings,store,self.llm)
        self.skills=Skills(settings)
        self.home=Home(settings,self.prefs)
        self.mcp=Mcp(settings)
        self.media=Media(settings,store,runtime)
        self.tools=ToolRegistry(settings,store)
        self.shell=Shell(settings,store)
        self.browser=Browser(settings,headless=settings.browser_headless)
        self.tools.attach(self.llm,self.memory,self.skills,self.home,self.mcp,self.shell,self.browser,self.media)
        self.catalog=MediaCatalog(settings)
        self.coding=Coding(settings,store)
        self.agent=Agent(settings,store,runtime,self.tools,self.media,self.shell,self.llm,self.memory,self.skills)
        self.health_sync=HealthSync(settings,self.tools.health,store)
        self.checkin=Checkin(settings,store,self.tools.health)
        self.task=None
        self.running={}      # job id -> (task, lane)
        self.active=None     # the local-lane task and its job id
        self.active_id=None
        self.connecting=None
        self.syncing=None

    async def start(self):
        self.store.recover()
        self.connecting=asyncio.create_task(self.connect_tools())
        self.task=asyncio.create_task(self.loop())

    async def connect_tools(self):
        try:
            await self.mcp.connect()
        except Exception:
            log.exception("MCP servers could not be connected")

    async def close(self):
        for task in [self.task,self.connecting,*[t for t,_ in self.running.values()]]:
            if task:
                task.cancel()
                try: await task
                except (asyncio.CancelledError,Exception): pass
        await self.mcp.close()
        await self.shell.close()
        await self.browser.close()
        await self.runtime.shutdown()

    def cancel(self,ident):
        job=self.store.get_job(ident)
        if not job:
            raise ValueError("任务不存在")
        if job["status"]=="queued":
            self.store.update_job(ident,status="cancelled",phase="已取消")
            if job["kind"]=="chat":
                self.store.update_message(job["payload"]["message_id"],"", "cancelled")
        elif job["status"]=="running":
            self.store.update_job(ident,cancel_requested=1,phase="正在停止")
            if ident in self.running:
                self.running[ident][0].cancel()

    def lane(self,job):
        """ "cloud" when the job only talks to a cloud model, otherwise "local"."""
        kind,p=job["kind"],job["payload"]
        if kind=="chat":
            session=self.store.one("SELECT model FROM sessions WHERE id=?",(p["session_id"],)) or {}
            return "local" if self.llm.is_local(self.llm.chat_model(session.get("model"))) else "cloud"
        if kind=="memory":
            session=self.store.one("SELECT model FROM sessions WHERE id=?",(p["session_id"],)) or {}
            return "local" if self.llm.is_local(self.llm.tool_model(session.get("model"))) else "cloud"
        if kind in ("plan","rewrite","code"):
            return "local" if self.llm.is_local() else "cloud"
        return "local"

    def startable(self):
        """Queued jobs that may start now, oldest first. Memory upkeep waits until nothing else is queued or running."""
        queued=self.store.queued()
        lanes=[lane for _,lane in self.running.values()]
        local_busy,cloud_free="local" in lanes,CLOUD_SLOTS-lanes.count("cloud")
        idle=not self.running and all(j["kind"]=="memory" for j in queued)
        picked=[]
        for job in queued:
            if job["kind"]=="memory" and not idle:
                continue
            lane=self.lane(job)
            if lane=="local" and not local_busy:
                local_busy=True;picked.append((job,lane))
            elif lane=="cloud" and cloud_free>0:
                cloud_free-=1;picked.append((job,lane))
        return picked

    async def loop(self):
        checked=0.0
        while True:
            for queued,lane in self.startable():
                job=self.store.claim(queued["id"])
                if not job:
                    continue
                self.store.event("jobs",job["id"])
                task=asyncio.create_task(self.execute(job,lane))
                self.running[job["id"]]=(task,lane)
                if lane=="local":
                    self.active,self.active_id=task,job["id"]
            if time.monotonic()-checked>15:  # schedules are to the minute: looking a few times a minute is enough
                checked=time.monotonic()
                try:
                    self.tools.agenda.fire()
                except Exception:
                    log.exception("Scheduled tasks could not be started")
                if not self.syncing or self.syncing.done():
                    self.syncing=asyncio.create_task(self.health_sync.tick())  # connected health accounts, every 15 minutes
                try:
                    self.checkin.tick()  # new health data or a sign that just appeared: check in on the user now
                except Exception:
                    log.exception("Check-in could not be started")
            await asyncio.sleep(.2 if self.running else .4)

    async def execute(self,job,lane):
        awake=None
        if lane=="local" and Path('/usr/bin/caffeinate').exists():
            awake=await asyncio.create_subprocess_exec('/usr/bin/caffeinate','-i')
        try:
            result=await self.handle(job)
            failed=job['kind']=='code_test' and not result.get('passed')
            self.store.update_job(job["id"],status="failed" if failed else "succeeded",progress=1,phase="测试未通过" if failed else "已完成",result=result,error=f"命令退出码 {result['exit_code']}，请查看输出" if failed else None)
            if job["kind"]=="chat":
                self.after_chat(job["payload"]["session_id"])
            elif job["kind"]=="memory":
                self.store.execute("DELETE FROM jobs WHERE id=?",(job["id"],))  # finished housekeeping leaves no record
        except asyncio.CancelledError:
            if lane=="local":
                await self.runtime.shutdown()
            user_cancel=bool(self.store.get_job(job["id"])["cancel_requested"])
            status="cancelled" if user_cancel else "interrupted"
            self.store.update_job(job["id"],status=status,phase="已取消" if user_cancel else "引擎已停止")
            if job["kind"]=="chat":
                self.store.set_message_status(job["payload"]["message_id"],status)
        except Exception as exc:
            log.exception("Job %s failed",job["id"])
            if job["kind"]=="memory":  # upkeep the user never asked for: fail quietly
                self.store.update_job(job["id"],status="failed",phase="记忆整理未完成",error=str(exc)[:2000])
            else:
                self.store.update_job(job["id"],status="failed",phase="需要处理",error=str(exc)[:2000])
            if job["kind"]=="chat":
                self.store.set_message_status(job["payload"]["message_id"],"failed",error=str(exc)[:1000])
        finally:
            if awake and awake.returncode is None:awake.terminate();await awake.wait()
            self.running.pop(job["id"],None)
            if self.active_id==job["id"]:
                self.active=None;self.active_id=None

    def after_chat(self,session_id):
        """Queues memory extraction and summarizing for a conversation that just got a reply."""
        if not self.memory.pending(session_id):
            return
        if self.store.one("SELECT id FROM jobs WHERE kind='memory' AND status='queued' AND json_extract(payload,'$.session_id')=?",(session_id,)):
            return
        self.store.create_job("memory",{"session_id":session_id})

    async def handle(self,job):
        kind,p,ident=job["kind"],job["payload"],job["id"]
        if kind=="install":
            async def progress(value,phase):
                self.store.update_job(ident,progress=value,phase=phase)
            return await self.catalog.install(p["component_id"],progress)
        if kind=="code":
            self.store.update_job(ident,phase="正在阅读代码并准备修改建议",progress=.1)
            return await self.coding.propose(p,self.llm)
        if kind=="code_test":
            await self.runtime.shutdown()
            async def progress(output,command):
                self.store.update_job(ident,phase="正在运行 "+command,result={"output":output,"command":command})
            return await self.coding.run(p,progress)
        if kind=="chat":
            return await self.chat(job)
        if kind=="ledger":
            # Sorting imported payments reads payee names: always the local model, whatever model the conversations use.
            self.store.update_job(ident,phase="正在整理账单分类",progress=.2)
            return await self.tools.ledger.classify(self.runtime)
        if kind=="memory":
            self.store.update_job(ident,phase="正在整理记忆",progress=.2)
            return await self.memory.process(p["session_id"])
        if kind=="plan":
            project=self.store.one("SELECT * FROM projects WHERE id=?",(p["project_id"],))
            if not project:
                raise ValueError("项目不存在")
            revision=project["updated_at"]
            self.store.update_job(ident,phase="正在编写角色和分镜",progress=.1)
            schema=DramaDocument.model_json_schema()
            prompt="你是短剧编剧和导演。根据用户创意生成可拍摄的短剧计划，所有说明用中文，image_prompt/video_prompt可用英文，中文对白保持原文。每个角色写appearance：用英文写死可见外貌，含年龄、脸型、发型发色、瞳色、体型、服装及配色，这是全片唯一的外貌来源。角色image_prompt留空。镜头的image_prompt和video_prompt只写景别、场景、动作、表情，用角色名称呼人物，不要重复外貌。character_ids必须列全该镜头出场的角色，每镜不超过4人。每个镜头1到15秒，合理分配动作和对白，没有指定时长时默认4到6个镜头。镜头需包含明确的景别、动作、运镜、环境音、对白。每个镜头写出首帧生图提示词image_prompt和视频提示词video_prompt。只输出符合下面JSON Schema的JSON，不要Markdown。ID用c1/c2、s1/s2等。所有reference_asset、first_frame、last_frame、video_asset为null，versions为空。\n"+dumps(schema)
            prompt+="\n时长规则：duration是数字，单位秒，每个镜头不超过15。全片时长等于各镜头duration之和；用户要求的时长更长时就增加镜头数（最多24个），不要把单个镜头写长。例如3分钟的片子需要12个以上镜头。"
            messages=[{"role":"system","content":prompt},{"role":"user","content":project["brief"]}]
            document=None
            asked=re.search(r"(\d+(?:\.\d+)?)\s*(分钟|分半|秒|minutes?|seconds?)",project["brief"])
            wanted=min(360,round(float(asked[1])*(1 if asked[2] in("秒","second","seconds") else 60)+(30 if asked[2]=="分半" else 0))) if asked else 0
            for attempt in range(3):
                # A plan that fails validation goes back to the model with the exact problems instead of failing the job.
                result=await self.llm.complete(messages,max_tokens=12000,temperature=.5 if attempt==0 else .3)
                try:
                    document=DramaDocument.model_validate(parse_json(result)).model_dump()
                    if not document["shots"]:
                        raise ValueError("shots is empty")
                    total=sum(shot["duration"] for shot in document["shots"])
                    if wanted and total<wanted*.7 and attempt<2:  # a third short plan is kept: a shorter film beats a failed job
                        raise ValueError(f"全片时长是各镜头duration之和，现在只有{total:g}秒，而用户要求约{wanted}秒。增加镜头数（最多24个）并把镜头写到8到15秒，让总和接近{wanted}秒")
                    break
                except (ValueError,ValidationError) as exc:
                    if attempt==2:
                        raise ValueError("分镜计划三次都没有通过检查，请简化创意或缩短时长后重试："+plain_errors(exc)[:300])
                    self.store.update_job(ident,phase=f"分镜计划有问题，正在修正（第 {attempt+2} 次）",progress=.3+.2*attempt)
                    messages+=[{"role":"assistant","content":result},{"role":"user","content":"这份JSON没有通过检查：\n"+plain_errors(exc)+"\n请修正这些问题，重新输出完整的JSON，只输出JSON。"}]
            if not document["shots"]:
                raise ValueError("分镜计划为空，请增加创意细节")
            changed=self.store.execute("UPDATE projects SET document=?,updated_at=? WHERE id=? AND updated_at=?",(dumps(document),time.time(),project["id"],revision))
            if not changed:
                raise ValueError("生成期间项目已被编辑，未覆盖你的修改，请重新生成")
            self.store.event("projects",project["id"])
            return {"project_id":project["id"],"shots":len(document["shots"])}
        if kind in ("video","rewrite"):
            p=self.directed(p)
            if kind=="rewrite":
                return {"prompt":await self.rewrite(p,ident)}
            return await self.render_video(p,ident)
        if kind=="image":
            result=await self.render_image(self.directed(p),ident)
            if p.get("outfit_art"):
                # An illustration drawn for weather cards belongs to the cards, not to the user's library.
                for asset in result.get("asset_ids",[]):
                    self.store.execute("UPDATE assets SET kind='card',name=? WHERE id=?",("穿衣插画 · "+p["outfit_art"],asset))
            return result
        if kind=="produce":
            return await self.produce(p,ident)
        if kind=="export":
            project=self.store.one("SELECT * FROM projects WHERE id=?",(p["project_id"],))
            if not project:
                raise ValueError("项目不存在")
            self.store.update_job(ident,phase="正在拼接和编码成片",progress=.1)
            return await self.media.export(project,ident)
        if kind=="frames":
            self.store.update_job(ident,phase='正在提取首尾帧')
            return await self.media.frames(p['asset_id'],ident)
        raise ValueError("不支持的任务类型")

    def directed(self,p):
        """Project media always follows the production order, whichever client asked for it."""
        project=self.store.one("SELECT * FROM projects WHERE id=?",(p["project_id"],)) if p.get("project_id") else None
        if not project:
            return p
        doc=project["document"]
        char=drama.find(doc.get("characters",[]),p.get("character_id"))
        if char:
            return {**p,**drama.character_request(doc,char)}
        shot=drama.find(doc.get("shots",[]),p.get("shot_id"))
        if not shot:
            return p
        if "duration" not in p:
            return {**p,**drama.frame_request(doc,shot)} if drama.cast(doc,shot) else p
        drama.require_cast(doc,shot)
        if drama.cast(doc,shot) and not p.get("first_frame"):
            raise ValueError(f"「{shot['title']}」有角色出场，请先生成首帧")
        return {**p,"prompt":drama.locked_prompt(doc,shot,p["prompt"])}

    async def render_image(self,p,ident,phase=None):
        result=await self.media.run("image",p,ident,phase=phase)
        self.attach_result(p,result,"image")
        return result

    async def render_video(self,p,ident,phase=None):
        self.catalog.validate_video(p)
        prompt=await self.rewrite(p,ident) if p.get("enhance",True) else p["prompt"]
        result=await self.media.run("video",p,ident,prompt,phase=phase)
        self.attach_result(p,result,"video")
        return result

    async def produce(self,p,ident):
        pid=p["project_id"]
        def document():
            project=self.store.one("SELECT * FROM projects WHERE id=?",(pid,))
            if not project:
                raise ValueError("项目不存在")
            return project["document"]
        counts={"characters":0,"frames":0,"videos":0}
        for char in document().get("characters",[]):
            if not char.get("reference_asset"):
                await self.render_image({**drama.character_request(document(),char),"project_id":pid},ident,f"正在定妆：{char['name']}")
                counts["characters"]+=1
        for item in document().get("shots",[]):
            doc=document();shot=drama.find(doc["shots"],item["id"])
            if shot and drama.frame_stale(doc,shot):
                await self.render_image({**drama.frame_request(doc,shot),"project_id":pid},ident,f"正在生成首帧：{shot['title']}")
                counts["frames"]+=1
        if p.get("stage")=="video":
            settings={k:v for k,v in p.items() if k not in ("stage","project_id")}
            for item in document().get("shots",[]):
                doc=document();shot=drama.find(doc["shots"],item["id"])
                if shot and not shot.get("video_asset"):
                    request=self.directed({**drama.video_request(doc,shot,settings),"project_id":pid})
                    await self.render_video(request,ident,f"正在生成镜头：{shot['title']}")
                    counts["videos"]+=1
        return {"project_id":pid,**counts}

    async def chat(self,job):
        return await self.agent.run(job)

    async def rewrite(self,p,ident):
        self.store.update_job(ident,phase="正在编排镜头与声音",progress=.05)
        guide=(self.settings.root/"engine/prompts/h3_base.txt").read_text()
        mode="FL2VA" if p.get("first_frame") and p.get("last_frame") else "I2VA" if p.get("first_frame") else "L2VA" if p.get("last_frame") else "T2VA"
        duration=video_frames(p["duration"])/24
        content=[{"type":"text","text":f"Task: {mode}. Effective duration: {duration:.3f} seconds. Preserve user intent and exact dialogue.\nOriginal prompt:\n{p['prompt']}"}]
        for key,label in (("first_frame","First frame"),("last_frame","Last frame")):
            if p.get(key):
                asset,path=self.media.asset(p[key]);content.extend([{"type":"text","text":label},self.runtime.image_block(path,asset["mime"])])
        text=await self.llm.complete([{"role":"system","content":"Rewrite the user's video prompt according to this guide. Return only the final prompt, no explanation.\n"+guide},{"role":"user","content":content}],max_tokens=3500,temperature=.4)
        required=("integrated_multimodal_description:","overall_soundscape:","non_diegetic_music:")
        if any(x not in text for x in required):
            raise ValueError("提示词改写缺少 H3 必要字段，已停止生成以便重试")
        for minutes,seconds in re.findall(r"At (\d+):(\d+(?:\.\d+)?)",text):
            if int(minutes)*60+float(seconds)>=duration:
                raise ValueError("改写后的镜头时间超过目标时长")
        return text

    def attach_result(self,p,result,kind):
        if not p.get("project_id") or not result.get("asset_ids"):
            return
        # Atomic read-modify-write preserves edits made while rendering.
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("SELECT document FROM projects WHERE id=?",(p["project_id"],)).fetchone()
            if not row:return
            doc=json.loads(row["document"])
            if kind=="video":
                for shot in doc.get("shots",[]):
                    if shot["id"]==p.get("shot_id"):
                        shot.setdefault("versions",[]).append(result["asset_ids"][0]);shot["video_asset"]=result["asset_ids"][0]
            elif p.get("character_id"):
                for char in doc.get("characters",[]):
                    if char["id"]==p["character_id"]:char["reference_asset"]=result["asset_ids"][0]
            elif p.get("shot_id"):
                for shot in doc.get("shots",[]):
                    if shot["id"]==p["shot_id"]:
                        # A new keyframe invalidates the shot's take; earlier takes stay in versions.
                        shot.update(first_frame=result["asset_ids"][0],frame_references=p.get("reference_assets",[]),video_asset=None)
            db.execute("UPDATE projects SET document=?,updated_at=? WHERE id=?",(dumps(doc),time.time(),p["project_id"]))
        self.store.event("projects",p["project_id"])
