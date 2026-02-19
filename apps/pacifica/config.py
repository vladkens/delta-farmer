# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Don't blame me, blame the API docs
from pydantic import BaseModel, Field, SecretStr, field_validator

from core.crypto import decrypt_value, is_encrypted
from core.models import DurationSec, SizeRange, TimeRange, load_config


class AccountConfig(BaseModel):
    name: str
    privkey: SecretStr = Field(repr=False)
    proxy: str | None = None
    enabled: bool = True

    @field_validator("privkey", mode="before")
    @classmethod
    def decrypt_privkey(cls, v: str) -> str:
        return decrypt_value(v) if isinstance(v, str) and is_encrypted(v) else v


class Config(BaseModel):
    accounts: list[AccountConfig]
    markets: list[str] = Field(..., min_length=1)
    leverage: int = Field(10, gt=0, lt=50)
    trade_size_usd: SizeRange
    trade_duration: TimeRange
    trade_cooldown: TimeRange
    trade_heartbeat: DurationSec = DurationSec("15s")
    pnl_limit: float = Field(0.25, gt=0, lt=1)
    use_limit: bool = Field(False)
    limit_wait: DurationSec = DurationSec("60s")
    limit_market_fallback: bool = Field(True)
    first_as_main: bool = Field(False)

    @classmethod
    def load(cls, filepath: str):
        return load_config(cls, filepath)
