"""Manifest schema models — data objects surfaced by the Manifest API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel


class AccessRecord(BaseModel):
    """A single access-log event for an object."""

    notebook: Optional[str] = None
    cell_index: Optional[int] = None
    cell_source_hash: Optional[str] = None
    op: Literal["write", "read", "delete"]
    timestamp: datetime


class NotebookEntry(BaseModel):
    """Summary of a notebook's activity in the project."""

    name: str
    last_run: Optional[datetime] = None
    write_count: int = 0
    read_count: int = 0


class ObjectEntry(BaseModel):
    """Summary of a cache or vector object."""

    id: str
    kind: Literal["cache", "vector"]
    type: str
    created_at: datetime
    last_write: Optional[datetime] = None
    last_read: Optional[datetime] = None
    write_count: int = 0
    read_count: int = 0
    parents: list[str] = []
    is_stale: bool = False
