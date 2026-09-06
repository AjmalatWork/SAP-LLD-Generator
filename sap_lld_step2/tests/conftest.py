import psycopg
import pytest

from lld_step2.config import load_config


@pytest.fixture(scope="session")
def db_config():
    return load_config()


@pytest.fixture()
def db_conn(db_config):
    try:
        conn = psycopg.connect(db_config.database_url, connect_timeout=3)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres not reachable at {db_config.database_url}: {exc}")
        return

    # Start each test from a clean slate so counts are deterministic.
    with conn.cursor() as cur:
        cur.execute(
            "TRUNCATE object_calls, object_uses_table, code_chunks, ddic_objects, objects CASCADE"
        )
    conn.commit()

    yield conn
    conn.close()
