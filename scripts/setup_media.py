#!/usr/bin/env python3
"""Install the pinned Apple Silicon media runtime without downloading model weights."""
import json
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
COMFY=ROOT/".runtime/ComfyUI"
SOURCES={
    "ComfyUI":("https://github.com/Comfy-Org/ComfyUI.git","426f5eea52a7ece1dd599fff4cb6cd1f352ddd24"),
    "ComfyUI-GGUF":("https://github.com/city96/ComfyUI-GGUF.git","6ea2651e7df66d7585f6ffee804b20e92fb38b8a"),
    "ComfyUI-AppleSilicon-FP8":("https://github.com/pawel-mazurkiewicz/ComfyUI-AppleSilicon-FP8.git","303b40798f4debf8d832af1ee26c88192b54361d"),
    "ComfyUI-PlagueKind-Nodes":("https://github.com/PlagueKind/ComfyUI-PlagueKind-Nodes.git","aaec055cd642b3292df18e69824c012d345ebfe8"),
}

def run(*args,cwd=ROOT):
    subprocess.run(list(map(str,args)),cwd=cwd,check=True)

def main():
    for name,(url,revision) in SOURCES.items():
        dest=COMFY if name=="ComfyUI" else COMFY/"custom_nodes"/name
        dest.parent.mkdir(parents=True,exist_ok=True)
        if not dest.exists():
            run("git","clone","--no-checkout","--filter=blob:none",url,dest)
        current=subprocess.check_output(["git","-C",str(dest),"rev-parse","HEAD"],text=True).strip()
        unpopulated=not any(p.name!='.git' for p in dest.iterdir())
        if current!=revision or unpopulated:
            dirty=subprocess.check_output(["git","-C",str(dest),"status","--porcelain"],text=True).strip()
            if dirty and not unpopulated:
                raise RuntimeError(f"{dest} 有本地修改，停止以避免覆盖")
            run("git","fetch","--depth","1","origin",revision,cwd=dest)
            run("git","checkout","--detach",revision,cwd=dest)
    loader=COMFY/"custom_nodes/ComfyUI-GGUF/loader.py"
    text=loader.read_text()
    if '"qwen_image21"' not in text and "'qwen_image21'" not in text:
        if '"qwen_image",' in text:
            text=text.replace('"qwen_image",','"qwen_image", "qwen_image21",',1)
        elif '"qwen_image"' in text:
            text=text.replace('"qwen_image"','"qwen_image", "qwen_image21"',1)
        else:
            raise RuntimeError("GGUF loader 结构已改变，不能应用架构白名单补丁")
        loader.write_text(text)
    python=COMFY/"venv/bin/python"
    if not python.exists():
        run("uv","venv","--python","3.12",COMFY/"venv")
    lock=ROOT/"config/media-requirements.lock"
    if lock.exists():
        run("uv","pip","sync","--python",python,lock)
    else:
        run("uv","pip","install","--python",python,"-r",COMFY/"requirements.txt","gguf==0.18.0")
        for name in list(SOURCES)[1:]:
            requirements=COMFY/"custom_nodes"/name/"requirements.txt"
            if requirements.exists():
                lines=[line for line in requirements.read_text().splitlines() if not line.strip().lower().startswith(("nvidia","triton","sageattention","flash-attn","xformers"))]
                filtered=ROOT/".runtime"/(name+"-requirements.txt")
                filtered.write_text("\n".join(lines))
                run("uv","pip","install","--python",python,"-r",filtered)
    config=ROOT/"config";config.mkdir(exist_ok=True)
    # Absolute paths are generated per installation; original weight files remain in place.
    model_paths={"unet":["qwen-image","minimax-h3"],"diffusion_models":["qwen-image","minimax-h3"],
                 "text_encoders":["qwen-image","minimax-h3"],"clip":["qwen-image","minimax-h3"],
                 "vae":["qwen-image","minimax-h3"],"loras":["minimax-h3"]}
    import yaml
    (config/"comfy-models.yaml").write_text(yaml.safe_dump({"jurvis":{"base_path":str(ROOT/"models"),**{k:"\n".join(v) for k,v in model_paths.items()}}}))
    (config/"runtime-sources.json").write_text(json.dumps({k:{"url":v[0],"revision":v[1]} for k,v in SOURCES.items()},indent=2)+"\n")
    result=subprocess.check_output(["uv","pip","freeze","--python",str(python)],text=True)
    (config/"media-requirements.lock").write_text(result)
    print("Media runtime installed. Real inference validation is still required.")

if __name__=="__main__":
    main()
