import asyncio
import io
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
import psutil
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps, UnidentifiedImageError
from . import __version__,drama
from .config import Settings
from .runtime import Runtime
from .schemas import ChatCreate,CommandDecision,SessionCreate,SessionUpdate,ImageRequest,VideoRequest,ProjectCreate,ProjectProduce,ProjectUpdate,MemoryCreate,PairRequest,SettingsUpdate,SpeakRequest,ProviderUpdate,SkillSave,McpServerSave,ConnectorSave,HomeControl
from .llm import LOCAL,split
from .speech import Speech,SpeechError,MAX_AUDIO_BYTES,IDLE_SECONDS
from .security import Security,lan_addresses,reach_addresses
from .store import Store,dumps,new_id
from .worker import Worker
from .coding import CodeConnect,CodeTask,CodeCommand


MEMORY_COLUMNS="SELECT id,title,content,enabled,created_at,updated_at,importance,durable,tags,source,session_id,embed_model FROM memories"


def create_app(settings=None, *, run_worker=True):
    settings=settings or Settings()
    settings.prepare()
    store=Store(settings.data/"jurvis.sqlite3")
    security=Security(settings,store)
    runtime=Runtime(settings)
    worker=Worker(settings,store,runtime)
    speech=Speech(settings)
    tidy={}
    def tidy_later():
        # give the speech models' memory back once they have sat unused for a while
        if tidy.get("timer"):tidy["timer"].cancel()
        tidy["timer"]=asyncio.get_running_loop().call_later(IDLE_SECONDS+5,lambda:asyncio.ensure_future(speech.tidy()))

    @asynccontextmanager
    async def lifespan(app):
        if run_worker: await worker.start()
        yield
        if run_worker: await worker.close()
        speech.close()

    app=FastAPI(title="Jurvis Engine",version=__version__,lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.store,app.state.security,app.state.worker=store,security,worker

    @app.middleware("http")
    async def authenticate(request,call_next):
        path=request.url.path
        if path=="/":return RedirectResponse("/app/")
        if path not in ("/health","/pair") and not path.startswith("/app"):
            header=request.headers.get("authorization","")
            identity=security.authorize(header[7:] if header.startswith("Bearer ") else "")
            if not identity:return JSONResponse({"detail":"请先与 Mac 配对"},status_code=401)
            request.state.identity=identity
        response=await call_next(request)
        response.headers["X-Content-Type-Options"]="nosniff"
        response.headers["Cache-Control"]="no-cache" if path.startswith("/app") else "no-store"
        return response

    @app.exception_handler(ValueError)
    async def invalid(request,exc):
        return JSONResponse({"detail":str(exc)},status_code=400)

    @app.get("/health")
    async def health():return {"service":"jurvis","version":__version__}

    @app.post("/pair")
    async def pair(body:PairRequest,request:Request):
        return security.pair(body.code,body.name,request.client.host if request.client else "unknown")

    @app.get("/api/state")
    async def state():
        jobs=store.all("SELECT * FROM jobs WHERE kind!='memory' ORDER BY created_at DESC LIMIT 200")  # memory upkeep is housekeeping, not a task the user started
        for job in jobs:
            job['result'].pop('workflow',None)
            changes=job['result'].pop('changes',None)
            if changes is not None:job['result']['change_count']=len(changes)
        return {"version":__version__,"sessions":store.all("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT 200"),
                "jobs":jobs,
                "assets":store.all("SELECT * FROM assets ORDER BY created_at DESC LIMIT 500"),
                "projects":store.all("SELECT * FROM projects ORDER BY updated_at DESC LIMIT 100"),
                "memories":store.all(MEMORY_COLUMNS+" ORDER BY updated_at DESC LIMIT 500"),
                "chat_model":worker.llm.chat_model(),
                "runtime":{"active":runtime.active,"memory_used_gib":round(psutil.virtual_memory().used/2**30,1),"memory_total_gib":round(psutil.virtual_memory().total/2**30,1)},
                "last_event":(store.one("SELECT MAX(id) AS id FROM events")["id"] or 0)}

    @app.post("/api/sessions")
    async def new_session(body:SessionCreate):
        if body.code_project_id:worker.coding.project(body.code_project_id)
        ident,now=new_id(),time.time()
        store.execute("INSERT INTO sessions(id,title,created_at,updated_at,code_project_id,model,incognito) VALUES(?,?,?,?,?,?,?)",
                      (ident,body.title,now,now,body.code_project_id,check_model(body.model),int(body.incognito)))
        return store.one("SELECT * FROM sessions WHERE id=?",(ident,))

    @app.get("/api/sessions/{ident}/messages")
    async def messages(ident:str):
        if not store.one("SELECT id FROM sessions WHERE id=?",(ident,)):raise HTTPException(404,"对话不存在")
        return store.all("SELECT * FROM messages WHERE session_id=? ORDER BY created_at",(ident,))

    @app.patch("/api/sessions/{ident}")
    async def update_session(ident:str,body:SessionUpdate):
        if not store.one("SELECT id FROM sessions WHERE id=?",(ident,)):raise HTTPException(404,"对话不存在")
        changes={}
        if body.allow_commands is not None:changes["allow_commands"]=int(body.allow_commands)
        if body.incognito is not None:changes["incognito"]=int(body.incognito)
        if body.title is not None:changes["title"]=body.title
        if body.model is not None:changes["model"]=check_model(body.model)
        if changes:store.execute("UPDATE sessions SET "+",".join(f"{k}=?" for k in changes)+" WHERE id=?",(*changes.values(),ident))
        store.event("sessions",ident)
        return store.one("SELECT * FROM sessions WHERE id=?",(ident,))

    @app.post("/api/messages/{message_id}/commands/{call_id}")
    async def decide_command(message_id:str,call_id:str,body:CommandDecision):
        worker.shell.decide(message_id,call_id,body.decision)
        return {"ok":True}

    @app.delete("/api/sessions/{ident}")
    async def delete_session(ident:str):
        pending=store.one("SELECT id FROM jobs WHERE kind='chat' AND status IN ('queued','running') AND json_extract(payload,'$.session_id')=?",(ident,))
        if pending:raise HTTPException(409,"请先停止这段对话的任务")
        store.execute("DELETE FROM sessions WHERE id=?",(ident,))
        return {"ok":True}

    @app.post("/api/sessions/{ident}/messages")
    async def send(ident:str,body:ChatCreate):
        if not store.one("SELECT id FROM sessions WHERE id=?",(ident,)):raise HTTPException(404,"对话不存在")
        key="chat:"+body.request_id
        prior=store.one("SELECT * FROM jobs WHERE idempotency_key=?",(key,))
        if prior:
            if prior["payload"].get("input")!=body.model_dump() or prior["payload"].get("session_id")!=ident:
                raise HTTPException(409,"请求标识已用于不同消息")
            return prior
        # Background work does not hold the conversation: only a reply the user is waiting for does.
        if store.one("SELECT id FROM jobs WHERE kind='chat' AND status IN ('queued','running') AND json_extract(payload,'$.session_id')=? AND json_extract(payload,'$.background') IS NULL",(ident,)):
            raise HTTPException(409,"请等待当前回复结束，或先停止它")
        for asset in body.asset_ids:
            worker.media.asset(asset)
        now=time.time();user_id,message_id,job_id=new_id(),new_id(),new_id()
        payload={"session_id":ident,"message_id":message_id,"input":body.model_dump()}
        with store.connection() as db:
            db.execute("INSERT INTO messages(id,session_id,role,content,status,created_at,metadata) VALUES(?,?,'user',?,'complete',?,?)",(user_id,ident,body.content,now,dumps({"asset_ids":body.asset_ids,"mode":body.mode,**({"voice":True} if body.voice else {})})))
            db.execute("INSERT INTO messages(id,session_id,role,content,status,created_at) VALUES(?,?,'assistant','','queued',?)",(message_id,ident,now+.0001))
            db.execute("INSERT INTO jobs(id,kind,status,payload,created_at,updated_at,idempotency_key) VALUES(?,'chat','queued',?,?,?,?)",(job_id,dumps(payload),now,now,key))
            db.execute("UPDATE sessions SET title=CASE WHEN title='新对话' THEN ? ELSE title END,updated_at=? WHERE id=?",(body.content[:40],now,ident))
        store.event("messages",ident)
        return store.get_job(job_id)

    @app.post("/api/images")
    async def image(body:ImageRequest,request:Request):
        for ident in body.reference_assets:worker.media.asset(ident)
        worker.directed(body.model_dump())
        return store.create_job("image",body.model_dump(),request.headers.get("x-idempotency-key"))

    @app.post("/api/videos")
    async def video(body:VideoRequest,request:Request):
        validate_video(body)
        worker.catalog.validate_video(body.model_dump())
        worker.directed(body.model_dump())
        return store.create_job("video",body.model_dump(),request.headers.get("x-idempotency-key"))

    def validate_video(body):
        for ident in (body.first_frame,body.last_frame):
            if ident:worker.media.asset(ident)

    @app.post("/api/rewrite")
    async def rewrite(body:VideoRequest):
        validate_video(body)
        return store.create_job("rewrite",body.model_dump())

    @app.get("/api/jobs/{ident}")
    async def job(ident:str):
        found=store.get_job(ident)
        if not found:raise HTTPException(404,"任务不存在")
        return found

    @app.post("/api/jobs/{ident}/cancel")
    async def cancel(ident:str):
        worker.cancel(ident);return store.get_job(ident)

    @app.post("/api/jobs/{ident}/retry")
    async def retry(ident:str):
        old=store.get_job(ident)
        if not old:raise HTTPException(404,"任务不存在")
        if old["status"] not in ("failed","cancelled","interrupted"):raise HTTPException(409,"只有未完成的任务可以重试")
        if old["kind"]=="chat":
            if not store.one("SELECT id FROM sessions WHERE id=?",(old["payload"]["session_id"],)):raise HTTPException(404,"原对话已删除")
            latest=store.one("SELECT id FROM jobs WHERE kind='chat' AND json_extract(payload,'$.session_id')=? ORDER BY created_at DESC LIMIT 1",(old["payload"]["session_id"],))
            if latest["id"]!=ident:
                raise HTTPException(409,"这段对话已有后续消息，请重新发送需要回答的问题")
            if store.one("SELECT id FROM jobs WHERE kind='chat' AND status IN ('queued','running') AND json_extract(payload,'$.session_id')=? AND json_extract(payload,'$.background') IS NULL",(old["payload"]["session_id"],)):
                raise HTTPException(409,"当前对话已有进行中的回复")
            payload=dict(old["payload"])
            payload["message_id"]=store.add_message(payload["session_id"],"assistant","","queued")
        else:payload=old["payload"]
        return store.create_job(old["kind"],payload)

    @app.post("/api/projects")
    async def project(body:ProjectCreate):
        result=worker.tools.execute("create_drama",body.model_dump())
        return store.one("SELECT * FROM projects WHERE id=?",(result["project_id"],))

    @app.put("/api/projects/{ident}")
    async def update_project(ident:str,body:ProjectUpdate):
        for shot in body.document.shots:
            for asset in [shot.first_frame,shot.last_frame,shot.video_asset,*shot.versions]:
                if asset:worker.media.asset(asset)
        for char in body.document.characters:
            if char.reference_asset:worker.media.asset(char.reference_asset)
        project=store.one("SELECT document FROM projects WHERE id=?",(ident,))
        stored={x["id"]:x for x in project["document"].get("shots",[])} if project else {}
        for shot in body.document.shots:
            # A frame the user picked by hand counts as approved for the current cast.
            if shot.first_frame and shot.first_frame!=stored.get(shot.id,{}).get("first_frame"):
                shot.frame_references=drama.references(body.document.model_dump(),shot.model_dump())
        changed=store.execute("UPDATE projects SET document=?,updated_at=? WHERE id=? AND updated_at=?",(body.document.model_dump_json(),time.time(),ident,body.expected_updated_at))
        if not changed:raise HTTPException(409,"项目已在另一台设备更新，请重新载入后编辑")
        return store.one("SELECT * FROM projects WHERE id=?",(ident,))

    @app.post("/api/projects/{ident}/plan")
    async def replan(ident:str):
        if not store.one("SELECT id FROM projects WHERE id=?",(ident,)):raise HTTPException(404,"项目不存在")
        return store.create_job("plan",{"project_id":ident})

    @app.post("/api/projects/{ident}/produce")
    async def produce(ident:str,body:ProjectProduce):
        project=store.one("SELECT * FROM projects WHERE id=?",(ident,))
        if not project:raise HTTPException(404,"项目不存在")
        if not project["document"].get("shots"):raise HTTPException(409,"请先生成分镜")
        if body.stage=="video":worker.catalog.validate_video(body.model_dump())
        return store.create_job("produce",{**body.model_dump(),"project_id":ident})

    @app.post("/api/projects/{ident}/export")
    async def export(ident:str):
        project=store.one("SELECT * FROM projects WHERE id=?",(ident,))
        if not project:raise HTTPException(404,"项目不存在")
        if not project["document"].get("shots") or any(not x.get("video_asset") for x in project["document"]["shots"]):
            raise HTTPException(409,"请先完成所有镜头")
        return store.create_job("export",{"project_id":ident})

    @app.post("/api/assets")
    async def upload(file:UploadFile=File(...)):
        content=bytearray()
        while chunk:=await file.read(65536):
            content.extend(chunk)
            if len(content)>settings.max_upload_bytes:raise HTTPException(413,"图片不能超过32MB")
        try:
            with Image.open(io.BytesIO(content)) as source:
                if source.width*source.height>32_000_000:raise HTTPException(413,"图片像素过大")
                image=ImageOps.exif_transpose(source).convert("RGB")
                image.thumbnail((3072,3072))
                path=settings.data/"assets"/(new_id()+".png")
                image.save(path,format="PNG")
        except (UnidentifiedImageError,OSError,Image.DecompressionBombError):raise HTTPException(400,"图片格式不可读取")
        ident=worker.media.record(path,Path(file.filename or "参考图").name[:120],"image/png","reference",metadata={"width":image.width,"height":image.height})
        return store.one("SELECT * FROM assets WHERE id=?",(ident,))

    @app.get("/api/assets/{ident}/file")
    async def asset_file(ident:str):
        try:asset,path=worker.media.asset(ident)
        except ValueError:raise HTTPException(404,"素材不存在")
        return FileResponse(path,media_type=asset["mime"],filename=path.name,content_disposition_type="inline")

    @app.post("/api/assets/{ident}/frames")
    async def extract_frames(ident:str):
        asset,_=worker.media.asset(ident)
        if not asset['mime'].startswith('video/'):raise HTTPException(400,'请选择视频素材')
        return store.create_job('frames',{'asset_id':ident})

    @app.get("/api/files")
    async def files():return worker.tools.execute("list_files",{})

    @app.post("/api/files")
    async def import_file(file:UploadFile=File(...)):
        content=await file.read(400001)
        if len(content)>400000:raise HTTPException(413,"文本文件不能超过400KB")
        try:text=content.decode("utf-8")
        except UnicodeDecodeError:raise HTTPException(400,"目前支持UTF-8文本文件：TXT、Markdown、CSV、JSON等")
        return worker.tools.save_file(Path(file.filename or "document.txt").name,text,replace=False)

    @app.get("/api/files/{name}")
    async def read_file(name:str):return worker.tools.execute("read_file",{"name":name})

    @app.get("/api/agenda")
    async def agenda():
        """The long-term goals and the scheduled work, for the Goals view."""
        return {"goals":worker.tools.agenda.goals(),"schedules":worker.tools.agenda.schedules()}

    @app.delete("/api/goals/{ident}")
    async def delete_goal(ident:str):
        worker.tools.agenda.remove_goal(ident);return {"ok":True}

    @app.delete("/api/schedules/{ident}")
    async def delete_schedule(ident:str):
        try:worker.tools.agenda.cancel(ident)
        except ValueError:raise HTTPException(404,"定时任务不存在")
        return {"ok":True}

    @app.get("/api/ledger")
    async def ledger(period:str="this_month",category:str=""):
        """The ledger page: totals and every payment of a period. Read by the user's own devices, never by a model."""
        try:
            summary=worker.tools.ledger.summary(period,category)
            return {"summary":summary,"transactions":worker.tools.ledger.transactions(period,category)["transactions"],"budgets":worker.tools.ledger.budgets()}
        except ValueError as exc:raise HTTPException(400,str(exc))

    @app.post("/api/ledger/import")
    async def import_bill(file:UploadFile=File(...)):
        content=await file.read(8*1024*1024+1)
        if len(content)>8*1024*1024:raise HTTPException(413,"账单文件不能超过 8MB")
        try:result=worker.tools.ledger.import_bill(content,file.filename or "")
        except ValueError as exc:raise HTTPException(400,str(exc))
        # Payments the keyword rules could not place are sorted by the local model in the background.
        if result["uncategorized"] and not store.one("SELECT id FROM jobs WHERE kind='ledger' AND status IN ('queued','running')"):
            result["job_id"]=store.create_job("ledger",{})["id"]
        return result

    @app.patch("/api/ledger/transactions/{ident}")
    async def recategorize(ident:str,body:dict):
        try:worker.tools.ledger.recategorize(ident,str(body.get("category","")))
        except ValueError as exc:raise HTTPException(400,str(exc))
        return {"ok":True}

    @app.delete("/api/ledger/transactions/{ident}")
    async def delete_transaction(ident:str):
        worker.tools.ledger.remove(ident);return {"ok":True}

    @app.post("/api/memories")
    async def memory(body:MemoryCreate):
        result=worker.memory.save(body.title,body.content,importance=body.importance,durable=body.durable,tags=body.tags)
        if not body.enabled:store.execute("UPDATE memories SET enabled=0 WHERE id=?",(result["saved"],))
        await worker.memory.index([result["saved"]])
        return store.one(MEMORY_COLUMNS+" WHERE id=?",(result["saved"],))

    @app.put("/api/memories/{ident}")
    async def update_memory(ident:str,body:MemoryCreate):
        store.execute("UPDATE memories SET title=?,content=?,enabled=?,importance=?,durable=?,tags=?,updated_at=?,embedding=NULL,embed_model=NULL WHERE id=?",
                      (body.title,body.content,int(body.enabled),body.importance,int(body.durable),dumps(body.tags),time.time(),ident))
        await worker.memory.index([ident])
        store.event("memories",ident)
        return {"ok":True}

    @app.delete("/api/memories")
    async def clear_memories():
        store.execute("DELETE FROM memories");store.event("memories","all");return {"ok":True}

    @app.get("/api/memories/search")
    async def search_memories(q:str=""):
        return {"memories":await worker.memory.retrieve(q,limit=20) if q.strip() else []}

    @app.post("/api/memories/reindex")
    async def reindex_memories():
        store.execute("UPDATE memories SET embedding=NULL,embed_model=NULL")
        return {"indexed":await worker.memory.index(),"embed_model":worker.prefs.get()["embed_model"]}

    @app.delete("/api/memories/{ident}")
    async def delete_memory(ident:str):
        store.execute("DELETE FROM memories WHERE id=?",(ident,));return {"ok":True}

    # ---- speech ----
    @app.exception_handler(SpeechError)
    async def speech_failed(request,exc):
        return JSONResponse({"detail":str(exc)},status_code=409)

    @app.get("/api/speech")
    async def speech_status():
        return {**speech.status(),"speaker":worker.prefs.get()["voice"].get("speaker") or ""}

    @app.post("/api/speech/transcribe")
    async def transcribe(file:UploadFile=File(...),language:str="auto"):
        content=bytearray()
        while chunk:=await file.read(65536):
            content.extend(chunk)
            if len(content)>MAX_AUDIO_BYTES:raise HTTPException(413,"录音太长了")
        result=await speech.transcribe(content,language)
        tidy_later()
        return result

    @app.post("/api/speech/warm")
    async def speech_warm():
        # load both models now, so the first sentence of a voice conversation does not wait for them
        asyncio.ensure_future(speech.warm());tidy_later()
        return {"ok":True}

    @app.post("/api/speech/speak")
    async def speak(body:SpeakRequest):
        speaker=body.speaker if body.speaker is not None else worker.prefs.get()["voice"].get("speaker") or ""
        audio=await speech.speak(body.text,speaker)
        tidy_later()
        return Response(audio,media_type="audio/wav")

    # ---- models and settings ----
    def check_model(model):
        if not model or model==LOCAL:return None if not model else LOCAL
        provider,slug=split(model)
        if not provider or not slug or provider not in worker.prefs.providers():raise ValueError("模型需写成 local 或 服务名:模型名，且服务已添加")
        return model

    def settings_view():
        return {"settings":worker.prefs.get(),"providers":worker.prefs.public_providers(),
                "local":{"id":LOCAL,"title":settings.llm_title,"installed":settings.llm_model.is_dir()}}

    @app.get("/api/settings")
    async def get_settings():return settings_view()

    @app.patch("/api/settings")
    async def update_settings(body:SettingsUpdate):
        changes=body.model_dump(exclude_none=True)
        for key in ("chat_model","tool_model","embed_model"):
            if changes.get(key):check_model(changes[key])
        if changes.get("embed_model")==LOCAL:raise ValueError("本机模型不提供向量；留空则使用内置的关键词匹配")
        worker.prefs.update(changes)
        store.event("settings","settings")
        return settings_view()

    @app.put("/api/providers/{name}")
    async def save_provider(name:str,body:ProviderUpdate):
        worker.prefs.set_provider(name,**body.model_dump())
        store.event("settings","providers")
        return settings_view()

    @app.delete("/api/providers/{name}")
    async def delete_provider(name:str):
        worker.prefs.remove_provider(name)
        current=worker.prefs.get()
        reset={k:(LOCAL if k=="chat_model" else "") for k in ("chat_model","tool_model","embed_model") if split(current[k])[0]==name and name not in worker.prefs.providers()}
        if reset:worker.prefs.update(reset)
        return settings_view()

    @app.get("/api/providers/{name}/models")
    async def provider_models(name:str):return {"models":await worker.llm.list_models(name)}

    # ---- skills ----
    @app.get("/api/skills")
    async def skills():return {"skills":worker.skills.list(),"folder":str(worker.skills.folder)}

    @app.get("/api/skills/{name}")
    async def skill(name:str):
        try:return {"name":name,"content":worker.skills.find(name)[1].read_text()}
        except ValueError:return {"name":name,"content":""}

    @app.put("/api/skills/{name}")
    async def save_skill(name:str,body:SkillSave):return worker.skills.save(name,body.content)

    @app.post("/api/skills/{name}/enabled")
    async def switch_skill(name:str,body:dict):
        try:return worker.skills.switch(name,bool(body.get("enabled")))
        except ValueError as exc:raise HTTPException(400,str(exc))

    @app.delete("/api/skills/{name}")
    async def delete_skill(name:str):
        worker.skills.delete(name);return {"ok":True}

    # ---- MCP servers ----
    @app.get("/api/mcp")
    async def mcp_servers():return {"servers":worker.mcp.status()}

    @app.put("/api/mcp/{name}")
    async def save_mcp(name:str,body:McpServerSave,request:Request):
        if body.command:local_only(request)  # a command runs on the Mac, so only the Mac may add one
        worker.mcp.save(name,{k:v for k,v in body.model_dump().items() if v not in (None,[],{})} | {"enabled":body.enabled,"trusted":body.trusted})
        await worker.mcp.connect()
        return {"servers":worker.mcp.status()}

    @app.delete("/api/mcp/{name}")
    async def delete_mcp(name:str):
        worker.mcp.remove(name);await worker.mcp.connect()
        return {"servers":worker.mcp.status()}

    @app.post("/api/mcp/reconnect")
    async def reconnect_mcp():
        await worker.mcp.connect();return {"servers":worker.mcp.status()}

    # ---- smart home ----
    @app.get("/api/home")
    async def home():return {"catalog":worker.home.catalog(),"connectors":worker.home.public()}

    @app.put("/api/home/connectors/{ident}")
    async def save_connector(ident:str,body:ConnectorSave):
        worker.home.save(ident,body.kind,body.settings,body.title,body.enabled)
        devices=await worker.home.devices(refresh=True)
        return {"connectors":worker.home.public(),"devices":len(devices)}

    @app.delete("/api/home/connectors/{ident}")
    async def delete_connector(ident:str):
        worker.home.remove(ident);await worker.home.devices(refresh=True)
        return {"connectors":worker.home.public()}

    @app.get("/api/home/devices")
    async def home_devices(refresh:bool=False):
        return {"devices":await worker.home.devices(refresh),"errors":worker.home.errors}

    @app.post("/api/home/devices/control")
    async def home_control(body:HomeControl):
        # The user pressed the control themself, so there is nothing further to confirm.
        return await worker.home.control(body.device_id,body.action,body.value)

    # ---- change feed ----
    @app.get("/api/events")
    async def events(request:Request,after:int=0):
        """Server-sent events: one line per change (entity, id), so clients need not poll."""
        async def feed():
            last,idle=after,0
            yield "retry: 2000\n\n"
            while not await request.is_disconnected():
                rows=store.all("SELECT id,entity,entity_id FROM events WHERE id>? ORDER BY id LIMIT 200",(last,))
                for row in rows:
                    last=row["id"]
                    yield f"id: {row['id']}\ndata: {dumps({'entity':row['entity'],'id':row['entity_id']})}\n\n"
                idle=0 if rows else idle+1
                if idle%50==49:yield ": keep-alive\n\n"
                await asyncio.sleep(.1 if rows else .3)
        return StreamingResponse(feed(),media_type="text/event-stream",headers={"X-Accel-Buffering":"no"})

    @app.get("/api/models")
    async def models():
        result=[]
        for ident,title,relative,kind in [("qwen","Qwen 3.8 · 27B","qwen/aeon-mxfp4-mtplx","chat"),("image","Qwen Image 2.1","qwen-image/qwen-image-2.1-UC-Q4_K_M.gguf","image"),("video","MiniMax H3","minimax-h3/MiniMax-H3-FL2VA-Pruned-Q5_K_M.gguf","video")]:
            path=settings.root/"models"/relative
            if ident=="qwen":title,path=settings.llm_title.removesuffix("（本机）"),settings.llm_model
            verified=store.one("SELECT id FROM jobs WHERE kind=? AND status='succeeded' LIMIT 1",(kind,)) is not None
            result.append({"id":ident,"title":title,"installed":path.exists(),"verified":verified,"active":runtime.active==("llm" if ident=="qwen" else "comfy"),"kind":kind})
        return {"models":result,"media_runtime_installed":(settings.comfy/"venv/bin/python").exists(),"addresses":lan_addresses()}

    def local_only(request):
        if not request.state.identity.get("local"):raise HTTPException(403,"请在 Mac 上管理设备")

    @app.get("/api/components")
    async def components():
        return worker.catalog.public()

    @app.post("/api/components/{ident}/install")
    async def install_component(ident:str,request:Request):
        local_only(request)
        worker.catalog.entry(ident)
        existing=store.one("SELECT * FROM jobs WHERE kind='install' AND status IN ('queued','running') AND json_extract(payload,'$.component_id')=?",(ident,))
        return existing or store.create_job("install",{"component_id":ident})

    @app.get("/api/code/projects")
    async def code_projects():return worker.coding.projects()

    @app.post("/api/code/projects")
    async def connect_code(body:CodeConnect,request:Request):
        local_only(request)
        return worker.coding.connect(body.root)

    @app.get("/api/code/projects/{ident}/files")
    async def code_files(ident:str):return worker.coding.tree(ident)

    @app.get("/api/code/projects/{ident}/file")
    async def code_file(ident:str,path:str):return {"path":path,"content":worker.coding.read(ident,path)}

    @app.get("/api/code/projects/{ident}/search")
    async def code_search(ident:str,q:str):return worker.coding.search(ident,q)

    @app.get("/api/code/projects/{ident}/commands")
    async def code_commands(ident:str):
        return [{k:v for k,v in c.items() if k!='argv'} for c in worker.coding.commands(ident)]

    @app.post("/api/code/projects/{ident}/tasks")
    async def code_task(ident:str,body:CodeTask,request:Request):
        worker.coding.project(ident)
        for name in body.files:worker.coding.read(ident,name)
        return store.create_job('code',{'code_project_id':ident,**body.model_dump()},request.headers.get('x-idempotency-key'))

    @app.post("/api/code/jobs/{ident}/apply")
    async def code_apply(ident:str):return worker.coding.apply(ident)

    @app.post("/api/code/jobs/{ident}/revert")
    async def code_revert(ident:str):return worker.coding.apply(ident,revert=True)

    @app.post("/api/code/projects/{ident}/run")
    async def code_run(ident:str,body:CodeCommand,request:Request):
        if body.command_id not in [c['id'] for c in worker.coding.commands(ident)]:raise HTTPException(400,'该命令不属于项目的构建和测试预设')
        return store.create_job('code_test',{'code_project_id':ident,'command_id':body.command_id,'change_id':body.change_id},request.headers.get('x-idempotency-key'))

    @app.post("/api/pairing")
    async def invitation(request:Request):
        local_only(request)
        addresses=reach_addresses()
        if not addresses:raise HTTPException(409,"未找到可用的网络地址，请连接 Wi-Fi、以太网或 Tailscale")
        return security.pairing(addresses[0],addresses[1:])

    @app.get("/api/reach")
    async def reach():
        # Paired devices ask for this so they learn a new address (Tailscale switched on later) without pairing again.
        return {"urls":[f"https://{address}:{settings.port}" for address in reach_addresses()]}

    @app.get("/api/devices")
    async def devices():return store.all("SELECT id,name,created_at,last_seen,revoked FROM devices ORDER BY created_at DESC")

    @app.delete("/api/devices/{ident}")
    async def revoke(ident:str,request:Request):
        local_only(request)
        store.execute("UPDATE devices SET revoked=1 WHERE id=?",(ident,));return {"ok":True}

    web=settings.root/"web"
    class Fresh(StaticFiles):
        """The web client's files: always revalidated, so a browser never runs a stale script against a new page."""
        async def get_response(self,path,scope):
            response=await super().get_response(path,scope)
            response.headers["Cache-Control"]="no-cache"
            return response
    if web.is_dir():app.mount("/app",Fresh(directory=web,html=True),name="app")
    return app
