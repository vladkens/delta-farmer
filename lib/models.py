# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Powered by caffeine and stackoverflow
import random
from decimal import Decimal
from typing import Annotated

from eth_utils import to_checksum_address
from pydantic import (
    BaseModel,
    BeforeValidator,
    Field,
    GetCoreSchemaHandler,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_core import core_schema

from .crypto import decrypt_value, is_encrypted
from .utils import parse_duration


def _empty_to_none(value: object) -> object:
    return None if value == "" else value


OptionalDec = Annotated[Decimal | None, BeforeValidator(_empty_to_none)]


class DurationSec(int):
    def __new__(cls, value):
        if isinstance(value, str):
            value = max(int(parse_duration(value)), 1)
        if isinstance(value, float):
            raise TypeError("DurationSec does not accept float values")
        return super().__new__(cls, int(value))

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type, handler: GetCoreSchemaHandler):
        choices: list[core_schema.CoreSchema | tuple[core_schema.CoreSchema, str]] = [
            core_schema.int_schema(),
            core_schema.str_schema(),
        ]
        return core_schema.no_info_after_validator_function(cls, core_schema.union_schema(choices))


class Range[T: (int, float)](BaseModel):
    min: T = Field(..., gt=0)
    max: T = Field(..., gt=0)

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, v):
        if isinstance(v, list | tuple):
            if len(v) != 2:
                raise ValueError(f"expected 2 values, got {len(v)}")
            return {"min": v[0], "max": v[1]}

        if isinstance(v, dict):
            return v

        raise ValueError(f"expected list/tuple [min, max] or dict, got {type(v).__name__}")

    @model_validator(mode="after")
    def _verify(self):
        if self.min > self.max:
            raise ValueError(f"{self.__class__.__name__}: min must be <= max")
        return self

    def sample(self):
        if isinstance(self.min, int) and isinstance(self.max, int):
            return random.randint(self.min, self.max)

        return random.uniform(self.min, self.max)


SizeRange = Range[float]
TimeRange = Range[DurationSec]


class TgConfig(BaseModel):
    """Telegram notification config. Set under [telegram] in your config file.

    notify channels: "start" | "stop" | "errors" | "reports"
    Remove a channel from the list to silence it.
    """

    token: SecretStr = Field(default=SecretStr(""), repr=False)
    chat_id: str = ""
    notify: list[str] = ["start", "stop", "errors", "reports"]
    report_interval: DurationSec = Field(default=DurationSec("1h"))

    @field_validator("token", mode="before")
    @classmethod
    def decrypt_token(cls, v: str) -> str:
        return decrypt_value(v) if is_encrypted(v) else v


class AccountConfig(BaseModel):
    name: str
    privkey: SecretStr = Field(repr=False)
    proxy: str | None = None
    cex_addr: str | None = None
    enabled: bool = True

    @field_validator("cex_addr", mode="before")
    @classmethod
    def validate_cex_addr(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None

        return to_checksum_address(value)

    @field_validator("privkey", mode="before")
    @classmethod
    def decrypt_privkey(cls, v: str) -> str:
        return decrypt_value(v) if is_encrypted(v) else v
