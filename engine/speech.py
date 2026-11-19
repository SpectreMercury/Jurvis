"""Speech on the Mac: recognition and synthesis with local MLX models, shared by every client.

Clients record audio and send it here to be transcribed, and send text here to get audio back,
so the phone, the web app and the Mac all sound the same and nothing leaves the machine.
Models live in models/speech/ and are loaded on first use.
"""
import asyncio
import io
import json
import re
import subprocess
import tempfile
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MAX_AUDIO_BYTES = 24 * 1024 * 1024
MAX_AUDIO_SECONDS = 300
MAX_SPEAK_CHARS = 600
IDLE_SECONDS = 600           # models are dropped after this long without use, to give the memory back
DEFAULT_SPEAKER = "vivian"   # used when no voice has been chosen, if the model has it


class SpeechError(RuntimeError):
    pass


def plain(text):
    """What should be said aloud for a reply written in Markdown: no code, no markup, no links."""
    text = re.sub(r"```.*?(```|$)", " ", text, flags=re.S)
    text = re.sub(r"<[^>\n]{1,80}>", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"^\s{0,3}(#{1,6}|[-*+]|\d+[.)]|>)\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*\|?[\s:|-]{3,}\|?\s*$", " ", text, flags=re.M)
    text = re.sub(r"[*_`~|]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def sentences(text, limit=MAX_SPEAK_CHARS):
    """Splits text into pieces short enough to synthesise one at a time, at sentence ends where possible."""
    out, current = [], ""
    for piece in re.findall(r"[^。！？!?；;\n]+[。！？!?；;\n]*|[。！？!?；;\n]+", text):
        while len(piece) > limit:
            cut = max(piece.rfind(mark, 0, limit) for mark in "，,、 ") + 1 or limit
            out.append((current + piece[:cut]).strip()) if len(current) + cut <= limit else out.extend([current.strip(), piece[:cut].strip()])
            current, piece = "", piece[cut:]
        if len(current) + len(piece) > limit or (len(current) >= 60 and current.rstrip()[-1:] in "。！？!?\n"):
            out.append(current.strip())
            current = ""
        current += piece
    out.append(current.strip())
    return [piece for piece in out if piece]


def language_of(text):
    """The language to speak this text in, for the synthesiser."""
    if re.search(r"[぀-ヿ]", text):
        return "japanese"
    if re.search(r"[가-힯]", text):
        return "korean"
    if re.search(r"[一-鿿]", text):
        return "chinese"
    return "english" if re.search(r"[A-Za-z]", text) else "auto"


def to_wav(samples, rate):
    """Mono float samples in -1..1 as 16-bit WAV bytes."""
    import numpy as np
    pcm = (np.clip(np.asarray(samples, dtype=np.float32).reshape(-1), -1, 1) * 32767).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(int(rate))
        out.writeframes(pcm.tobytes())
    return buffer.getvalue()


class Speech:
    def __init__(self, settings):
        self.settings = settings
        self.stt_path = self.resolve(settings.speech_stt)
        self.tts_path = self.resolve(settings.speech_tts)
        # MLX work stays on one thread: models are loaded and run there, one request at a time.
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="speech")
        self.models, self.used = {}, {}
        self.guard = threading.Lock()

    def resolve(self, value):
        path = Path(value).expanduser()
        return path if path.is_absolute() else self.settings.root / "models" / path

    # ---- what is installed ----
    def speakers(self):
        """Voices the installed synthesiser offers, read from its config so listing them does not load it."""
        try:
            config = json.loads((self.tts_path / "config.json").read_text())
        except (OSError, json.JSONDecodeError):
            return []
        table = (config.get("talker_config") or {}).get("spk_id") or config.get("spk_id") or {}
        return sorted(table, key=str.lower) if isinstance(table, dict) else []

    def status(self):
        installed = lambda path: (path / "config.json").is_file() and any(path.glob("*.safetensors"))
        return {"stt": installed(self.stt_path), "tts": installed(self.tts_path),
                "stt_model": self.stt_path.name, "tts_model": self.tts_path.name, "speakers": self.speakers()}

    # ---- models ----
    def model(self, kind):
        """Runs on the speech thread."""
        with self.guard:
            self.used[kind] = time.monotonic()
            if kind not in self.models:
                path = self.stt_path if kind == "stt" else self.tts_path
                if not (path / "config.json").is_file():
                    raise SpeechError(("语音识别" if kind == "stt" else "语音合成") + f"模型还没有安装，请放到 {path}")
                if kind == "stt":
                    from mlx_audio.stt import load
                else:
                    from mlx_audio.tts.utils import load
                self.models[kind] = load(str(path))
            return self.models[kind]

    def release(self, idle=IDLE_SECONDS):
        """Drops models nobody has used for a while. Runs on the speech thread."""
        with self.guard:
            now = time.monotonic()
            for kind in [k for k, at in self.used.items() if now - at >= idle and k in self.models]:
                del self.models[kind]
            if not self.models:
                try:
                    import mlx.core as mx
                    mx.clear_cache()
                except Exception:
                    pass

    async def run(self, work, *args):
        return await asyncio.get_running_loop().run_in_executor(self.pool, work, *args)

    def preload(self):
        for kind in ("stt", "tts"):
            try:
                self.model(kind)
            except Exception:
                pass

    async def warm(self):
        await self.run(self.preload)

    async def tidy(self):
        await self.run(self.release)

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)

    # ---- recognition ----
    def decode(self, data, folder):
        """Whatever the client recorded (WebM, MP4, WAV...) as 16 kHz mono WAV, the form the recogniser wants."""
        source, target = Path(folder) / "in", Path(folder) / "in.wav"
        source.write_bytes(data)
        try:
            done = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source), "-t", str(MAX_AUDIO_SECONDS),
                                   "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(target)], capture_output=True, timeout=60)
        except FileNotFoundError:
            raise SpeechError("需要 ffmpeg 才能读取录音")
        except subprocess.TimeoutExpired:
            raise SpeechError("录音读取超时")
        if done.returncode or not target.is_file() or target.stat().st_size <= 44:
            raise SpeechError("这段录音读不出来")
        with wave.open(str(target)) as audio:
            seconds = audio.getnframes() / audio.getframerate()
        return target, seconds

    def recognise(self, data, language):
        with tempfile.TemporaryDirectory() as folder:
            path, seconds = self.decode(data, folder)
            if seconds < 0.25:
                return {"text": "", "language": "", "seconds": round(seconds, 2)}
            result = self.model("stt").generate(str(path), language=language if language in ("zh", "en", "ja", "ko", "yue") else "auto", use_itn=True)
        text = re.sub(r"<\|[^|>]*\|>", "", getattr(result, "text", "") or "").strip()
        spoken = getattr(result, "language", "") or ""
        return {"text": text, "language": spoken if isinstance(spoken, str) else "", "seconds": round(seconds, 2)}

    async def transcribe(self, data, language="auto"):
        if not data:
            raise SpeechError("没有收到录音")
        if len(data) > MAX_AUDIO_BYTES:
            raise SpeechError("录音太长了")
        return await self.run(self.recognise, bytes(data), language)

    # ---- synthesis ----
    def synthesise(self, text, speaker):
        import numpy as np
        model = self.model("tts")
        voices = self.speakers()
        if speaker and voices and speaker.lower() not in [v.lower() for v in voices]:
            speaker = ""
        speaker = speaker or next((v for v in voices if v.lower() == DEFAULT_SPEAKER), voices[0] if voices else "")
        language = language_of(text)
        if hasattr(model, "generate_custom_voice") and speaker:
            results = model.generate_custom_voice(text=text, speaker=speaker, language=language)
        else:
            results = model.generate(text=text, **({"voice": speaker} if speaker else {}))
        chunks = [np.asarray(result.audio, dtype=np.float32).reshape(-1) for result in results]
        if not chunks:
            raise SpeechError("没有合成出声音")
        return to_wav(np.concatenate(chunks), getattr(model, "sample_rate", 24000))

    async def speak(self, text, speaker=""):
        text = plain(text)
        if not text:
            raise SpeechError("没有可以朗读的内容")
        if len(text) > MAX_SPEAK_CHARS:
            raise SpeechError(f"一次最多朗读 {MAX_SPEAK_CHARS} 个字，请分段")
        return await self.run(self.synthesise, text, speaker)
