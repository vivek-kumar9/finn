"""Supabase access with the service key. Server side only."""
import os
from functools import lru_cache

from supabase import Client, create_client

CHUNK = 500


@lru_cache(maxsize=1)
def client() -> Client:
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])


def upsert(table: str, rows: list[dict], on_conflict: str):
    for i in range(0, len(rows), CHUNK):
        client().table(table).upsert(rows[i:i + CHUNK], on_conflict=on_conflict).execute()


def insert(table: str, rows: list[dict]):
    for i in range(0, len(rows), CHUNK):
        client().table(table).insert(rows[i:i + CHUNK]).execute()


def select(table: str, columns: str = "*", **filters) -> list[dict]:
    """Equality filters, plus gte_<col> and in_<col>. Pages through results past 1000 rows."""
    def query():
        q = client().table(table).select(columns)
        for key, value in filters.items():
            if key.startswith("gte_"):
                q = q.gte(key[4:], value)
            elif key.startswith("in_"):
                q = q.in_(key[3:], value)
            else:
                q = q.eq(key, value)
        return q

    rows, start = [], 0
    while True:  # PostgREST caps responses at 1000 rows
        page = query().range(start, start + 999).execute().data
        rows.extend(page)
        if len(page) < 1000:
            return rows
        start += 1000
