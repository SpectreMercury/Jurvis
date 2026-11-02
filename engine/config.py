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

    def prepare(self):
        for name in ("assets", "workspace", "logs", "security"):
            (self.data / name).mkdir(parents=True, exist_ok=True)
        self.data.chmod(0o700)

    @property
    def llm_model(self):
        return self.root / "models/qwen/aeon-mxfp4-mtplx"

    @property
    def python(self):
        return self.root / ".venv/bin/python"

    @property
    def comfy(self):
        return self.root / ".runtime/ComfyUI"

