# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Don't blame me, blame the API docs
from datetime import UTC, datetime, timedelta
from typing import Awaitable, Callable, Generic, TypeVar

from core.logger import logger
from core.utils import pickle_dump, pickle_load

T = TypeVar("T", bound=dict)
FetchFn = Callable[[datetime | None], Awaitable[list[T]]]


class DataStore(Generic[T]):
    def __init__(self, filepath: str, id_key: str = "id"):
        self.filepath = filepath
        self.last_dt: datetime | None = None
        self.records: dict[str, T] = {}
        self.id_key = id_key
        self._load()

    def _load(self):
        if data := pickle_load(self.filepath, delete_on_error=True):
            self.last_dt = data.get("last_sync")
            self.records = data.get("records", {})

    def save(self):
        data = {"last_sync": self.last_dt, "records": self.records}
        pickle_dump(self.filepath, data)

    def upsert(self, records: list[T]):
        for record in records:
            record_id = record[self.id_key]
            self.records[record_id] = record

    def count(self) -> int:
        return len(self.records)

    def get_all(self) -> list[T]:
        return list(self.records.values())

    def needs_sync(self, ttl_sec: int) -> bool:
        if self.last_dt is None:
            return True
        age = (datetime.now(tz=UTC) - self.last_dt).total_seconds()
        return age >= ttl_sec

    def update_sync_time(self, dt: datetime | None = None):
        self.last_dt = dt or datetime.now(tz=UTC)

    def get_last_sync(self) -> datetime | None:
        return self.last_dt

    async def sync(self, fetch_fn: FetchFn, ttl_sec=3600, lookback_sec=60) -> "DataStore[T]":
        if not self.needs_sync(ttl_sec) and self.last_dt is not None:
            df = self.last_dt.strftime("%Y-%m-%d %H:%M")
            logger.trace(f"No sync needed for {self.filepath.split('/')[-1]} (last: {df})")
            return self

        since = self.last_dt
        since = since - timedelta(seconds=lookback_sec) if since else None

        df = since.strftime("%Y-%m-%d %H:%M") if since else "beginning"
        logger.trace(f"Syncing data for {self.filepath.split('/')[-1]} (last: {df})...")

        records = await fetch_fn(since)
        self.upsert(records)
        self.update_sync_time()
        self.save()
        return self
