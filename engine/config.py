from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JURVIS_", env_file=ROOT / ".env", extra="ignore")
    root: Path = ROOT
    data: Path = ROOT / "data"
    host: str = "0.0.0.0"
    port: int = 18777
    llm_port: int = 18778
    comfy_port: int = 18779
    max_upload_bytes: int = 32 * 1024 * 1024
    llm_timeout: float = 900
    media_timeout: float = 7200
    history_messages: int = 24
    browser_headless: bool = False            # drive the browser without a window (automated runs)
    local_model: str = ""                     # folder of an MLX model to use instead of the default; relative paths start at models/
    speech_stt: str = "speech/sensevoice-small"   # speech recognition model folder; relative paths start at models/
    speech_tts: str = "speech/qwen3-tts-0.6b"     # speech synthesis model folder
    shared_skills: str = "~/.claude/skills"   # skills written for Claude Code are offered here too; empty turns that off

    def prepare(self):
        for name in ("assets", "workspace", "logs", "security"):
            (self.data / name).mkdir(parents=True, exist_ok=True)
        self.data.chmod(0o700)

    @property
    def llm_model(self):
        if self.local_model:
            path = Path(self.local_model).expanduser()
            return path if path.is_absolute() else self.root / "models" / path
        return self.root / "models/qwen/aeon-mxfp4-mtplx"

    @property
    def llm_title(self):
        return f"{self.llm_model.name}（本机）" if self.local_model else "Qwen 3.8 · 27B（本机）"

    @property
    def python(self):
        return self.root / ".venv/bin/python"

    @property
    def comfy(self):
        return self.root / ".runtime/ComfyUI"

