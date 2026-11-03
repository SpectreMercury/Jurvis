from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator


class SessionCreate(BaseModel):
    title: str = Field(default="新对话", min_length=1, max_length=100)
    code_project_id: str | None = Field(default=None, max_length=64)


class ChatCreate(BaseModel):
    content: str = Field(min_length=1, max_length=16000)
    asset_ids: list[str] = Field(default_factory=list, max_length=4)
    request_id: str = Field(min_length=8, max_length=80)
    mode: Literal["image", "video", "drama", "goal"] | None = None

    @field_validator("content")
    @classmethod
    def clean(cls, text):
        if not text.strip():
            raise ValueError("请输入内容")
        return text.strip()


class ImageRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=6000)
    width: int = Field(default=1024, ge=256, le=1536)
    height: int = Field(default=1024, ge=256, le=1536)
    steps: int = Field(default=20, ge=4, le=50)
    seed: int = Field(default=42, ge=0, le=2**32-1)
    project_id: str | None = None
    character_id: str | None = None
    shot_id: str | None = None
    reference_assets: list[str] = Field(default_factory=list, max_length=4)
    @model_validator(mode="after")
    def dimensions(self):
        if self.width % 32 or self.height % 32 or self.width*self.height > 1536*1024:
            raise ValueError("图片边长需为 32 的倍数，总像素不超过 157 万")
        return self


class VideoRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=16000)
    duration: float = Field(default=5, ge=1, le=15)
    width: int = Field(default=352, ge=256, le=1024)
    height: int = Field(default=608, ge=256, le=1024)
    seed: int = Field(default=42, ge=0, le=2**32-1)
    first_frame: str | None = None
    last_frame: str | None = None
    enhance: bool = True
    model_profile: Literal["gguf_q5", "convrot_int8"] = "gguf_q5"
    sampling_profile: Literal["balanced", "turbo_v2", "dasiwa_v33"] = "balanced"
    turbo_steps: Literal[4, 6, 8] = 6
    motion_strength: float = Field(default=0, ge=-8, le=8, allow_inf_nan=False)
    project_id: str | None = None
    shot_id: str | None = None
    @model_validator(mode="after")
    def dimensions(self):
        if self.width % 32 or self.height % 32 or self.width*self.height > 768*768:
            raise ValueError("视频边长需为 32 的倍数，总像素不超过 59 万")
        return self


class Character(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(max_length=3000)
    image_prompt: str = Field(default="", max_length=4000)
    reference_asset: str | None = None


class Shot(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=6000)
    dialogue: str = Field(default="", max_length=2000)
    duration: float = Field(default=5, ge=1, le=15)
    image_prompt: str = Field(default="", max_length=6000)
    video_prompt: str = Field(default="", max_length=16000)
    character_ids: list[str] = Field(default_factory=list, max_length=10)
    first_frame: str | None = None
    last_frame: str | None = None
    video_asset: str | None = None
    versions: list[str] = Field(default_factory=list, max_length=100)


class DramaDocument(BaseModel):
    logline: str = Field(default="", max_length=1000)
    style: str = Field(default="写实电影", max_length=1000)
    characters: list[Character] = Field(default_factory=list, max_length=12)
    shots: list[Shot] = Field(default_factory=list, max_length=24)
    @model_validator(mode="after")
    def ids(self):
        chars = [c.id for c in self.characters]
        shots = [s.id for s in self.shots]
        if len(chars)!=len(set(chars)) or len(shots)!=len(set(shots)):
            raise ValueError("角色和镜头 ID 必须唯一")
        for shot in self.shots:
            if set(shot.character_ids)-set(chars):
                raise ValueError("分镜引用了不存在的角色")
        return self


class ProjectCreate(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    brief: str = Field(min_length=1, max_length=6000)


class ProjectUpdate(BaseModel):
    document: DramaDocument
    expected_updated_at: float


class MemoryCreate(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=4000)
    enabled: bool = True


class PairRequest(BaseModel):
    code: str = Field(min_length=16, max_length=128)
    name: str = Field(min_length=1, max_length=80)


class CommandDecision(BaseModel):
    decision: Literal["run", "deny", "always"]


class SessionUpdate(BaseModel):
    allow_commands: bool
