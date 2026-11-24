"""Settings from .env and limits from policy.json."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    server_key: str = ""
    internal_token: str = ""
    db_path: Path = Path("ordr.db")
    policy_path: Path = Path("policy.json")
    store_mcp_url: str = "http://127.0.0.1:8001/mcp"
    parent_number: str = ""
    kid_number: str = ""
    safe_phrase: str = ""
    parent_lang: str = "kn"  # kn or en. The kid always gets English
    channel: str = "meta"
    webhook_secret: str = ""
    llm_api_key: str = ""
    stt_api_key: str = ""
    tts_api_key: str = ""


class Policy(BaseModel):
    """Unknown or missing keys fail loudly. A typo must not remove a limit."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    auto_approve: bool
    per_order_cap_paise: int = Field(gt=0)
    monthly_cap_paise: int = Field(gt=0)
    surge_ceiling_paise: int = Field(ge=0)
    duplicate_window_minutes: int = Field(ge=0)
    mandate_ttl_minutes: int = Field(gt=0)
    search_result_ttl_minutes: int = Field(gt=0)
    backup_approver_after_minutes: int | None
    allowed_payment: list[str]


def load_policy(path: Path = Path("policy.json")) -> Policy:
    return Policy.model_validate_json(path.read_text(encoding="utf-8"))
