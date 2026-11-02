import asyncio
import io
import json
import mimetypes
import time
from pathlib import Path
import httpx
from PIL import Image, ImageOps
from .store import dumps,new_id
from .workflows import image_graph,video_graph,video_frames


class Media:
    def __init__(self,settings,store,runtime):
        self.settings,self.store,self.runtime=settings,store,runtime

    def asset(self,ident):
        row=self.store.one("SELECT * FROM assets WHERE id=?",(ident,))
        if not row:
            raise ValueError("参考素材不存在")
        path=(self.settings.data/row["path"]).resolve()
        if not path.is_relative_to((self.settings.data/"assets").resolve()) or not path.is_file():
            raise ValueError("素材文件缺失")
        return row,path

    def record(self,path,name,mime,kind,job_id=None,metadata=None):
        ident=new_id()
        self.store.execute("INSERT INTO assets(id,name,path,mime,size,kind,created_at,job_id,metadata) VALUES(?,?,?,?,?,?,?,?,?)",
                           (ident,name,str(path.relative_to(self.settings.data)),mime,path.stat().st_size,kind,time.time(),job_id,dumps(metadata or {})))
        self.store.event("assets",ident)
        return ident

    async def reference(self,client,ident,canvas=None):
        row,path=self.asset(ident)
        if not row["mime"].startswith("image/"):
            raise ValueError("首尾帧和角色参考必须是图片")
        with Image.open(path) as image:
            image=ImageOps.exif_transpose(image).convert("RGB")
            if canvas:
                image=ImageOps.fit(image,canvas,method=Image.Resampling.LANCZOS)
            else:
                image.thumbnail((1536,1536))
            buf=io.BytesIO();image.save(buf,format="PNG")
        response=await client.post("/upload/image",files={"image":(f"jurvis-{ident}.png",buf.getvalue(),"image/png")},data={"overwrite":"true"})
        response.raise_for_status()
        obj=response.json()
        return (obj.get("subfolder","")+"/"+obj["name"]).lstrip("/")

    async def run(self,kind,p,job_id,prompt=None):
        await self.runtime.ensure("comfy")
        base=f"http://127.0.0.1:{self.settings.comfy_port}"
        async with httpx.AsyncClient(base_url=base,timeout=60,trust_env=False) as client:
            if kind=="image":
                refs=[await self.reference(client,x,(p["width"],p["height"])) for x in p.get("reference_assets",[])]
                graph=image_graph(p,job_id,refs)
            else:
                canvas=(p["width"],p["height"])
                first=await self.reference(client,p["first_frame"],canvas) if p.get("first_frame") else None
                last=await self.reference(client,p["last_frame"],canvas) if p.get("last_frame") else None
                graph=video_graph(p,job_id,prompt or p["prompt"],first,last)
            response=await client.post("/prompt",json={"prompt":graph,"client_id":"jurvis"})
            if response.status_code!=200:
                raise RuntimeError("创作工作流校验失败："+response.text[:1200])
            prompt_id=response.json()["prompt_id"]
            self.store.update_job(job_id,phase="正在生成" if kind=="image" else "正在生成画面与声音",progress=0.2,
                                  result={"prompt_id":prompt_id,"workflow":graph,"enhanced_prompt":prompt})
            started=time.monotonic()
            while time.monotonic()-started<self.settings.media_timeout:
                response=await client.get(f"/history/{prompt_id}")
                response.raise_for_status()
                entry=response.json().get(prompt_id)
                if entry:
                    if entry.get("status",{}).get("status_str")=="error":
                        errors=[m[1] for m in entry.get("status",{}).get("messages",[]) if m[0]=="execution_error"]
                        raise RuntimeError("创作引擎执行失败："+str(errors[-1].get("exception_message","未知错误") if errors else "请查看引擎日志")[:1000])
                    outputs=[]
                    for output in entry.get("outputs",{}).values():
                        for key in ("images","videos","gifs"):
                            outputs.extend(output.get(key,[]))
                    outputs=[o for o in outputs if o.get("type")=="output"]
                    if outputs:
                        ids=[]
                        for output in outputs:
                            ext=Path(output["filename"]).suffix.lower()
                            if ext not in (".png",".jpg",".jpeg",".webp",".mp4",".webm",".mov"):
                                continue
                            result=await client.get("/view",params={k:output[k] for k in ("filename","subfolder","type") if k in output})
                            result.raise_for_status()
                            path=self.settings.data/"assets"/(new_id()+ext)
                            path.write_bytes(result.content)
                            metadata={"prompt":p["prompt"],"seed":p["seed"],"width":p["width"],"height":p["height"],"workflow_version":1}
                            if kind=="video":
                                metadata.update({key:p.get(key,default) for key,default in (("model_profile","gguf_q5"),("sampling_profile","balanced"),("turbo_steps",6),("motion_strength",0))})
                                metadata.update(requested_duration=p["duration"],frames=video_frames(p["duration"]),effective_duration=video_frames(p["duration"])/24)
                                probe=await probe_media(path)
                                if not any(x.get("codec_type")=="video" for x in probe["streams"]):
                                    raise RuntimeError("生成文件没有视频轨道")
                                metadata["probe"]=probe
                            else:
                                with Image.open(path) as img:
                                    metadata.update(width=img.width,height=img.height)
                                    img.verify()
                            ids.append(self.record(path,p["prompt"][:60],mimetypes.guess_type(path)[0] or "application/octet-stream",kind,job_id,metadata))
                        if ids:
                            return {"asset_ids":ids,"prompt_id":prompt_id,"enhanced_prompt":prompt,"workflow":graph}
                    if entry.get("status",{}).get("completed"):
                        raise RuntimeError("工作流完成但没有输出媒体文件")
                await asyncio.sleep(2)
            raise RuntimeError("创作任务超时，可降低分辨率或时长后重试")

    async def export(self,project,job_id):
        shots=project["document"].get("shots",[])
        if not shots or any(not s.get("video_asset") for s in shots):
            raise ValueError("请先生成每一个镜头的视频，再导出成片")
        folder=self.settings.data/"assets"
        paths=[self.asset(s["video_asset"])[1] for s in shots]
        output=folder/(new_id()+".mp4")
        inputs=[];filters=[];joins=[]
        for i,path in enumerate(paths):
            inputs.extend(["-i",str(path)])
            probe=await probe_media(path)
            filters.append(f"[{i}:v]scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24,format=yuv420p,setpts=PTS-STARTPTS[v{i}]")
            if any(s.get("codec_type")=="audio" for s in probe["streams"]):
                filters.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS[a{i}]")
            else:
                duration=float(probe["format"]["duration"])
                filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={duration},asetpts=PTS-STARTPTS[a{i}]")
            joins.append(f"[v{i}][a{i}]")
        filters.append("".join(joins)+f"concat=n={len(paths)}:v=1:a=1[v][a]")
        await run_process(["ffmpeg","-hide_banner","-loglevel","error",*inputs,"-filter_complex",";".join(filters),"-map","[v]","-map","[a]","-c:v","libx264","-preset","fast","-crf","20","-c:a","aac","-b:a","192k","-movflags","+faststart",str(output)],timeout=1800)
        probe=await probe_media(output)
        ident=self.record(output,project["title"]+" · 成片","video/mp4","film",job_id,{"project_id":project["id"],"shots":[s["id"] for s in shots],"probe":probe})
        return {"asset_ids":[ident],"project_id":project["id"]}

    async def frames(self,asset_id,job_id):
        source,path=self.asset(asset_id)
        if not source['mime'].startswith('video/'):raise ValueError('请选择视频素材')
        probe=await probe_media(path)
        stream=next(s for s in probe['streams'] if s.get('codec_type')=='video')
        duration=float(stream.get('duration') or probe['format']['duration'])
        ids=[]
        for label,position in [('首帧',0),('尾帧',max(0,duration-1/24))]:
            output=self.settings.data/'assets'/(new_id()+'.png')
            await run_process(['ffmpeg','-hide_banner','-loglevel','error','-ss',str(position),'-i',str(path),'-frames:v','1',str(output)])
            if not output.exists():raise RuntimeError('未能提取视频帧')
            ids.append(self.record(output,source['name'][:40]+' · '+label,'image/png','reference',job_id,{'source_asset':asset_id,'position':position,'width':stream['width'],'height':stream['height']}))
        return {'asset_ids':ids,'first_frame':ids[0],'last_frame':ids[1],'source_asset':asset_id}


async def run_process(command,timeout=120):
    process=await asyncio.create_subprocess_exec(*command,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:
        out,err=await asyncio.wait_for(process.communicate(),timeout)
    except BaseException:
        if process.returncode is None:
            process.kill();await process.wait()
        raise
    if process.returncode:
        raise RuntimeError(err.decode(errors="replace")[-1500:])
    return out


async def probe_media(path):
    data=await run_process(["ffprobe","-v","error","-show_streams","-show_format","-of","json",str(path)])
    return json.loads(data)
