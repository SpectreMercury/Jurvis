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
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image, ImageOps, UnidentifiedImageError
from . import __version__
from .config import Settings
from .runtime import Runtime
from .schemas import ChatCreate,SessionCreate,ImageRequest,VideoRequest,ProjectCreate,ProjectUpdate,MemoryCreate,PairRequest
from .security import Security,lan_addresses
from .store import Store,dumps,new_id
from .worker import Worker
from .coding import CodeConnect,CodeTask,CodeCommand


def create_app(settings=None, *, run_worker=True):
    settings=settings or Settings()
    settings.prepare()
    store=Store(settings.data/"jurvis.sqlite3")
    security=Security(settings,store)
    runtime=Runtime(settings)
    worker=Worker(settings,store,runtime)

    @asynccontextmanager
    async def lifespan(app):
        if run_worker: await worker.start()
        yield
        if run_worker: await worker.close()

    app=FastAPI(title="Jurvis Engine",version=__version__,lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.store,app.state.security,app.state.worker=store,security,worker

    @app.middleware("http")
    async def authenticate(request,call_next):
        if request.url.path not in ("/health","/pair"):
            header=request.headers.get("authorization","")
            identity=security.authorize(header[7:] if header.startswith("Bearer ") else "")
            if not identity:return JSONResponse({"detail":"请先与 Mac 配对"},status_code=401)
            request.state.identity=identity
        response=await call_next(request)
        response.headers["X-Content-Type-Options"]="nosniff"
        response.headers["Cache-Control"]="no-store"
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
        jobs=store.all("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 200")
        for job in jobs:
            job['result'].pop('workflow',None)
            changes=job['result'].pop('changes',None)
            if changes is not None:job['result']['change_count']=len(changes)
        return {"version":__version__,"sessions":store.all("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT 200"),
                "jobs":jobs,
                "assets":store.all("SELECT * FROM assets ORDER BY created_at DESC LIMIT 500"),
                "projects":store.all("SELECT * FROM projects ORDER BY updated_at DESC LIMIT 100"),
                "memories":store.all("SELECT * FROM memories ORDER BY updated_at DESC LIMIT 200"),
                "runtime":{"active":runtime.active,"memory_used_gib":round(psutil.virtual_memory().used/2**30,1),"memory_total_gib":round(psutil.virtual_memory().total/2**30,1)},
                "last_event":(store.one("SELECT MAX(id) AS id FROM events")["id"] or 0)}

    @app.post("/api/sessions")
    async def new_session(body:SessionCreate):
        ident,now=new_id(),time.time()
        store.execute("INSERT INTO sessions VALUES(?,?,?,?)",(ident,body.title,now,now))
        return store.one("SELECT * FROM sessions WHERE id=?",(ident,))

    @app.get("/api/sessions/{ident}/messages")
    async def messages(ident:str):
        if not store.one("SELECT id FROM sessions WHERE id=?",(ident,)):raise HTTPException(404,"对话不存在")
        return store.all("SELECT * FROM messages WHERE session_id=? ORDER BY created_at",(ident,))

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
        if store.one("SELECT id FROM jobs WHERE kind='chat' AND status IN ('queued','running') AND json_extract(payload,'$.session_id')=?",(ident,)):
            raise HTTPException(409,"请等待当前回复结束，或先停止它")
        for asset in body.asset_ids:
            worker.media.asset(asset)
        now=time.time();user_id,message_id,job_id=new_id(),new_id(),new_id()
        payload={"session_id":ident,"message_id":message_id,"input":body.model_dump()}
        with store.connection() as db:
            db.execute("INSERT INTO messages(id,session_id,role,content,status,created_at,metadata) VALUES(?,?,'user',?,'complete',?,?)",(user_id,ident,body.content,now,dumps({"asset_ids":body.asset_ids})))
            db.execute("INSERT INTO messages(id,session_id,role,content,status,created_at) VALUES(?,?,'assistant','','queued',?)",(message_id,ident,now+.0001))
            db.execute("INSERT INTO jobs(id,kind,status,payload,created_at,updated_at,idempotency_key) VALUES(?,'chat','queued',?,?,?,?)",(job_id,dumps(payload),now,now,key))
            db.execute("UPDATE sessions SET title=CASE WHEN title='新对话' THEN ? ELSE title END,updated_at=? WHERE id=?",(body.content[:40],now,ident))
        store.event("messages",ident)
        return store.get_job(job_id)

    @app.post("/api/images")
    async def image(body:ImageRequest,request:Request):
        for ident in body.reference_assets:worker.media.asset(ident)
        return store.create_job("image",body.model_dump(),request.headers.get("x-idempotency-key"))

    @app.post("/api/videos")
    async def video(body:VideoRequest,request:Request):
        validate_video(body)
        worker.catalog.validate_video(body.model_dump())
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
            if store.one("SELECT id FROM jobs WHERE kind='chat' AND status IN ('queued','running') AND json_extract(payload,'$.session_id')=?",(old["payload"]["session_id"],)):
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
        changed=store.execute("UPDATE projects SET document=?,updated_at=? WHERE id=? AND updated_at=?",(body.document.model_dump_json(),time.time(),ident,body.expected_updated_at))
        if not changed:raise HTTPException(409,"项目已在另一台设备更新，请重新载入后编辑")
        return store.one("SELECT * FROM projects WHERE id=?",(ident,))

    @app.post("/api/projects/{ident}/plan")
    async def replan(ident:str):
        if not store.one("SELECT id FROM projects WHERE id=?",(ident,)):raise HTTPException(404,"项目不存在")
        return store.create_job("plan",{"project_id":ident})

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
        content=await file.read(100001)
        if len(content)>100000:raise HTTPException(413,"文本文件不能超过100KB")
        try:text=content.decode("utf-8")
        except UnicodeDecodeError:raise HTTPException(400,"目前支持UTF-8文本文件：TXT、Markdown、CSV、JSON等")
        return worker.tools.execute("save_file",{"name":Path(file.filename or "document.txt").name,"content":text})

    @app.get("/api/files/{name}")
    async def read_file(name:str):return worker.tools.execute("read_file",{"name":name})

    @app.post("/api/memories")
    async def memory(body:MemoryCreate):
        result=worker.tools.execute("remember",body.model_dump())
        if not body.enabled:store.execute("UPDATE memories SET enabled=0 WHERE id=?",(result["saved"],))
        return store.one("SELECT * FROM memories WHERE id=?",(result["saved"],))

    @app.put("/api/memories/{ident}")
    async def update_memory(ident:str,body:MemoryCreate):
        store.execute("UPDATE memories SET title=?,content=?,enabled=?,updated_at=? WHERE id=?",(body.title,body.content,int(body.enabled),time.time(),ident))
        return {"ok":True}

    @app.delete("/api/memories/{ident}")
    async def delete_memory(ident:str):
        store.execute("DELETE FROM memories WHERE id=?",(ident,));return {"ok":True}

    @app.get("/api/models")
    async def models():
        result=[]
        for ident,title,relative,kind in [("qwen","Qwen 3.8 · 27B","qwen/aeon-mxfp4-mtplx","chat"),("image","Qwen Image 2.1","qwen-image/qwen-image-2.1-UC-Q4_K_M.gguf","image"),("video","MiniMax H3","minimax-h3/MiniMax-H3-FL2VA-Pruned-Q5_K_M.gguf","video")]:
            path=settings.root/"models"/relative
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
        return store.create_job('code_test',{'code_project_id':ident,'command_id':body.command_id},request.headers.get('x-idempotency-key'))

    @app.post("/api/pairing")
    async def invitation(request:Request):
        local_only(request)
        addresses=lan_addresses()
        if not addresses:raise HTTPException(409,"未找到局域网地址，请连接Wi-Fi或以太网")
        return security.pairing(addresses[0])

    @app.get("/api/devices")
    async def devices():return store.all("SELECT id,name,created_at,last_seen,revoked FROM devices ORDER BY created_at DESC")

    @app.delete("/api/devices/{ident}")
    async def revoke(ident:str,request:Request):
        local_only(request)
        store.execute("UPDATE devices SET revoked=1 WHERE id=?",(ident,));return {"ok":True}

    return app
