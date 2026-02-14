# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Sleep is overrated anyway
import random
from typing import Generic, TypeVar

from pydantic import BaseModel, Field, GetCoreSchemaHandler, model_validator
from pydantic_core import core_schema

from core.utils import parse_duration

T = TypeVar("T", int, float)  # constrain if you want numeric types


class DurationSec(int):
    def __new__(cls, value):
        if isinstance(value, str):
            # parse duration string like "15s", "5m", "1h" into seconds
            value = max(int(parse_duration(value)), 1)

        if isinstance(value, float):
            raise TypeError("DurationSec does not accept float values")

        return super().__new__(cls, int(value))

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type, handler: GetCoreSchemaHandler):
        t = [core_schema.int_schema(), core_schema.str_schema()]
        return core_schema.no_info_after_validator_function(cls, core_schema.union_schema(t))


class Range(BaseModel, Generic[T]):
    min: T = Field(..., gt=0)
    max: T = Field(..., gt=0)

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, v):
        if isinstance(v, (list, tuple)):
            if len(v) != 2:
                raise ValueError(f"{cls.__name__} expected 2 values, got {len(v)}")
            return {"min": v[0], "max": v[1]}

        if isinstance(v, dict):
            return v

        raise TypeError(f"Cannot coerce {cls.__name__} from {type(v).__name__}")

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
