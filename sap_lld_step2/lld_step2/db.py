"""Thin connection helper. No ORM — plain SQL via psycopg."""
from __future__ import annotations

import psycopg

from .config import Config


def connect(config: Config) -> psycopg.Connection:
    conn = psycopg.connect(config.database_url, autocommit=False)
    return conn
