"""Versioned ComfyUI API graphs. These remain separate from assistant logic."""
import math


def node(kind, **inputs):
    return {"class_type":kind,"inputs":inputs}


def video_frames(seconds):
    n=max(5,round(seconds*24))
    return n+(5-n%17)%17


def image_graph(p, job_id, references=None):
    graph={
        "1":node("UnetLoaderGGUF",unet_name="qwen-image-2.1-UC-Q4_K_M.gguf"),
        "2":node("CLIPLoader",clip_name="qwen3vl_8b_int8_convrot.safetensors",type="qwen_image",device="default"),
        "3":node("VAELoader",vae_name="qwen_image_2.1_vae_bf16.safetensors"),
        "4":node("TextEncodeQwenImage21",clip=["2",0],prompt=p["prompt"],negative_prompt="",resolution=1024),
        "5":node("EmptyLatentImage",width=p["width"],height=p["height"],batch_size=1),
        "6":node("KSampler",model=["1",0],positive=["4",0],negative=["4",1],latent_image=["5",0],seed=p["seed"],steps=p["steps"],cfg=1.0,sampler_name="euler",scheduler="simple",denoise=1.0),
        "7":node("VAEDecode",samples=["6",0],vae=["3",0]),
        "8":node("SaveImage",images=["7",0],filename_prefix=f"jurvis/{job_id}")}
    if references:
        graph["4"]["inputs"]["vae"]=["3",0]
        for n,filename in enumerate(references,1):
            ident=str(20+n)
            graph[ident]=node("LoadImage",image=filename)
            graph["4"]["inputs"][f"images.image_{n}"]=[ident,0]
        graph["6"]["inputs"]["latent_image"]=["4",2]
        graph["9"]=node("ImageScale",image=["7",0],upscale_method="lanczos",width=p["width"],height=p["height"],crop="center")
        graph["8"]["inputs"]["images"]=["9",0]
        del graph["5"]
    return graph


def video_graph(p, job_id, prompt, first=None, last=None):
    graph={
        "1":node("UnetLoaderGGUF",unet_name="MiniMax-H3-FL2VA-Pruned-Q5_K_M.gguf"),
        "2":node("LoraLoaderModelOnly",model=["1",0],lora_name="H3-PK-Parasyte-Turbo.safetensors",strength_model=1.5),
        "3":node("H3AdaLNLoRAFix",model=["2",0],mode="strip"),
        "4":node("MiniMaxH3SigmaShift",model=["3",0],shift_video=12.0,shift_audio=3.0),
        "5":node("CLIPLoaderGGUF",clip_name="MiniMax-H3-TextEncoder-Qwen3VL-32B-abliterated-Q3_K_S_vis.gguf",type="minimax"),
        "6":node("VAELoader",vae_name="minimax_h3_video_vae_fp16.safetensors"),
        "7":node("VAELoader",vae_name="minimax_h3_audio_vae_fp32.safetensors"),
        "8":node("MiniMaxH3ImageToVideo",clip=["5",0],vae=["6",0],prompt=prompt,width=p["width"],height=p["height"],length=video_frames(p["duration"])),
        "9":node("RandomNoise",noise_seed=p["seed"]),
        "10":node("BasicGuider",model=["4",0],conditioning=["8",0]),
        "11":node("KSamplerSelect",sampler_name="er_sde"),
        "12":node("BetaSamplingScheduler",model=["4",0],steps=8,alpha=0.5,beta=0.7),
        "13":node("SamplerCustomAdvanced",noise=["9",0],guider=["10",0],sampler=["11",0],sigmas=["12",0],latent_image=["8",1]),
        "14":node("VAEDecode",samples=["13",0],vae=["6",0]),
        "15":node("VAEDecodeAudio",samples=["13",0],vae=["7",0]),
        "16":node("CreateVideo",images=["14",0],audio=["15",0],fps=24,bit_depth=8,color_space="sRGB",codec="none"),
        "17":node("SaveVideo",video=["16",0],filename_prefix=f"jurvis/{job_id}",format="mp4",**{"format.codec":"h264"})}
    if p.get("model_profile") == "convrot_int8":
        graph["1"] = node("UNETLoader", unet_name="minimax_h3_fl2va_pruned_int8_convrot.safetensors", weight_dtype="default")
    if p.get("sampling_profile") == "turbo_v2":
        graph["2"]["inputs"].update(lora_name="minimax_h3_hyperflow_EMA600_pruned_r128_fro0995_turbo_lora.safetensors", strength_model=1.0)
        graph["4"]["inputs"]["shift_video"] = 6.0
        graph["11"] = node("KSamplerSelect", sampler_name="euler")
        graph["12"] = node("BasicScheduler", model=["4",0], scheduler="simple", steps=p.get("turbo_steps",6), denoise=1.0)
    if p.get("sampling_profile") == "dasiwa_v33":
        # Port of the pinned v3.3 core recipe, using native nodes and no NVIDIA finishing passes.
        # The 25-step recipe runs the base model; acceleration LoRAs are deliberately absent.
        del graph["2"]
        graph["3"]["inputs"]["model"] = ["1",0]
        graph["4"]["inputs"].update(shift_video=11.0, shift_audio=4.0)
        graph["11"] = node("KSamplerSelect", sampler_name="res_multistep")
        graph["12"] = node("BasicScheduler", model=["4",0], scheduler="simple", steps=25, denoise=1.0)
    if p.get("motion_strength", 0):
        # Apply before stripping AdaLN patches: both adapters may target the pruned modulation layers.
        graph["22"] = node("LoraLoaderModelOnly", model=graph["3"]["inputs"]["model"], lora_name="H3_speed_slider_1.1.safetensors", strength_model=p["motion_strength"])
        graph["3"]["inputs"]["model"] = ["22",0]
    for ident,name,filename in (("20","first_frame",first),("21","last_frame",last)):
        if filename:
            graph[ident]=node("LoadImage",image=filename)
            graph["8"]["inputs"][name]=[ident,0]
    return graph
