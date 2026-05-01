"""Loads config.toml once at startup; exposes typed accessors."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class UserConfig:
    native_language: str
    cefr_level: str


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    model: str
    temperature: float
    max_tokens: int
    draft_enabled: bool


@dataclass(frozen=True)
class ASRConfig:
    model_size: str
    device: str
    compute_type: str
    beam_size: int


@dataclass(frozen=True)
class TTSConfig:
    voice: str


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    data_dir: Path


@dataclass(frozen=True)
class Config:
    user: UserConfig
    llm: LLMConfig
    asr: ASRConfig
    tts: TTSConfig
    server: ServerConfig
    project_root: Path


def load_config(path: Path | None = None) -> Config:
    project_root = Path(__file__).resolve().parent.parent
    config_path = path or project_root / "config.toml"
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    server_raw = raw["server"]
    data_dir = (project_root / server_raw["data_dir"]).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "audio").mkdir(parents=True, exist_ok=True)

    return Config(
        user=UserConfig(**raw["user"]),
        llm=LLMConfig(**raw["llm"]),
        asr=ASRConfig(**raw["asr"]),
        tts=TTSConfig(**raw["tts"]),
        server=ServerConfig(host=server_raw["host"], port=server_raw["port"], data_dir=data_dir),
        project_root=project_root,
    )
