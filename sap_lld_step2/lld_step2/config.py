"""Central place for the few things that are meant to be tweaked.

Everything here is overridable via environment variable so the model, DB
connection, and chunking window can change without touching code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    database_url: str
    embedding_model: str
    chunk_size_lines: int


def load_config() -> Config:
    return Config(
        database_url=os.environ.get(
            "LLD_DATABASE_URL", "postgresql://lld:lld@localhost:5432/sap_lld"
        ),
        embedding_model=os.environ.get(
            "LLD_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
        ),
        chunk_size_lines=int(os.environ.get("LLD_CHUNK_SIZE_LINES", "100")),
    )
