import asyncio
import json
import logging
import re
import time
from pathlib import Path
from .media import Media
from .schemas import DramaDocument
from .store import dumps
from .tools import ToolRegistry
from .agent import Agent
from .workflows import video_frames
from .catalog import MediaCatalog
from .coding import Coding
from .shell import Shell

log=logging.getLogger("jurvis.worker")


def parse_json(text):
    text=text.strip()
    if text.startswith("```"):
        text=text.split("\n",1)[1].rsplit("```",1)[0].strip()
    return json.loads(text)


class Worker:
    def __init__(self,settings,store,runtime):
        self.settings,self.store,self.runtime=settings,store,runtime
        self.media=Media(settings,store,runtime)
        self.tools=ToolRegistry(settings,store)
        self.catalog=MediaCatalog(settings)
        self.coding=Coding(settings,store)
        self.shell=Shell(settings,store)
        self.agent=Agent(settings,store,runtime,self.tools,self.media,self.shell)
        self.task=None
        self.active=None
        self.active_id=None

    async def start(self):
        self.store.recover()
        self.task=asyncio.create_task(self.loop())

    async def close(self):
        if self.task:
            self.task.cancel()
            try: await self.task
            except asyncio.CancelledError: pass
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
            if self.active_id==ident and self.active:
                self.active.cancel()

    async def loop(self):
        while True:
            job=self.store.claim_next()
            if not job:
                await asyncio.sleep(.4);continue
            self.active_id=job["id"]
            self.store.event("jobs",job["id"])
            self.active=asyncio.create_task(self.handle(job))
            awake=None
            if Path('/usr/bin/caffeinate').exists():
                awake=await asyncio.create_subprocess_exec('/usr/bin/caffeinate','-i')
            try:
                result=await self.active
                failed=job['kind']=='code_test' and not result.get('passed')
                self.store.update_job(job["id"],status="failed" if failed else "succeeded",progress=1,phase="测试未通过" if failed else "已完成",result=result,error=f"命令退出码 {result['exit_code']}，请查看输出" if failed else None)
            except asyncio.CancelledError:
                await self.runtime.shutdown()
                user_cancel=bool(self.store.get_job(job["id"])["cancel_requested"])
                status="cancelled" if user_cancel else "interrupted"
                self.store.update_job(job["id"],status=status,phase="已取消" if user_cancel else "引擎已停止")
                if job["kind"]=="chat":
                    self.store.set_message_status(job["payload"]["message_id"],status)
                if asyncio.current_task().cancelling():
                    raise
            except Exception as exc:
                log.exception("Job %s failed",job["id"])
                self.store.update_job(job["id"],status="failed",phase="需要处理",error=str(exc)[:2000])
                if job["kind"]=="chat":
                    self.store.set_message_status(job["payload"]["message_id"],"failed",error=str(exc)[:1000])
            finally:
                if awake and awake.returncode is None:awake.terminate();await awake.wait()
                self.active=None;self.active_id=None

    async def handle(self,job):
        kind,p,ident=job["kind"],job["payload"],job["id"]
        if kind=="install":
            async def progress(value,phase):
                self.store.update_job(ident,progress=value,phase=phase)
            return await self.catalog.install(p["component_id"],progress)
        if kind=="code":
            self.store.update_job(ident,phase="正在阅读代码并准备修改建议",progress=.1)
            return await self.coding.propose(p,self.runtime)
        if kind=="code_test":
            await self.runtime.shutdown()
            async def progress(output,command):
                self.store.update_job(ident,phase="正在运行 "+command,result={"output":output,"command":command})
            return await self.coding.run(p,progress)
        if kind=="chat":
            return await self.chat(job)
        if kind=="plan":
            project=self.store.one("SELECT * FROM projects WHERE id=?",(p["project_id"],))
            if not project:
                raise ValueError("项目不存在")
            revision=project["updated_at"]
            self.store.update_job(ident,phase="正在编写角色和分镜",progress=.1)
            schema=DramaDocument.model_json_schema()
            prompt="你是短剧编剧和导演。根据用户创意生成可拍摄的短剧计划，所有说明用中文，image_prompt/video_prompt可用英文，中文对白保持原文。角色外貌、服装、声音、道具必须在各镜头保持一致。每个镜头1到15秒，合理分配动作和对白，默认4到6个镜头，不超过12个。镜头需包含明确的景别、动作、运镜、环境音、对白。每个镜头写出首帧生图提示词image_prompt和视频提示词video_prompt。只输出符合下面JSON Schema的JSON，不要Markdown。ID用c1/c2、s1/s2等。所有reference_asset、first_frame、last_frame、video_asset为null，versions为空。\n"+dumps(schema)
            result=await self.runtime.complete([{"role":"system","content":prompt},{"role":"user","content":project["brief"]}],max_tokens=6000,temperature=.5)
            document=DramaDocument.model_validate(parse_json(result)).model_dump()
            if not document["shots"]:
                raise ValueError("分镜计划为空，请增加创意细节")
            changed=self.store.execute("UPDATE projects SET document=?,updated_at=? WHERE id=? AND updated_at=?",(dumps(document),time.time(),project["id"],revision))
            if not changed:
                raise ValueError("生成期间项目已被编辑，未覆盖你的修改，请重新生成")
            self.store.event("projects",project["id"])
            return {"project_id":project["id"],"shots":len(document["shots"])}
        if kind in ("video","rewrite"):
            if kind=="video":self.catalog.validate_video(p)
            prompt=await self.rewrite(p,ident) if p.get("enhance",True) or kind=="rewrite" else p["prompt"]
            if kind=="rewrite":
                return {"prompt":prompt}
            result=await self.media.run("video",p,ident,prompt)
            self.attach_result(p,result,"video")
            return result
        if kind=="image":
            result=await self.media.run("image",p,ident)
            self.attach_result(p,result,"image")
            return result
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
        text=await self.runtime.complete([{"role":"system","content":"Rewrite the user's video prompt according to this guide. Return only the final prompt, no explanation.\n"+guide},{"role":"user","content":content}],max_tokens=3500,temperature=.4)
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
                    if shot["id"]==p["shot_id"]:shot["first_frame"]=result["asset_ids"][0]
            db.execute("UPDATE projects SET document=?,updated_at=? WHERE id=?",(dumps(doc),time.time(),p["project_id"]))
        self.store.event("projects",p["project_id"])
