"""SQLite helpers: one connection per request, schema creation, seat-layout generator."""
import os
import sqlite3
import string

from flask import g

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")


def db_path():
    return os.environ.get("DB_PATH", os.path.join(BASE_DIR, "bus.db"))


def connect(path=None):
    conn = sqlite3.connect(path or db_path(), timeout=15)   # wait up to 15 s if another request is writing
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")                # readers don't block the writer
    return conn


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db(path=None):
    conn = connect(path)
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        conn.executescript(fh.read())
    conn.commit()
    conn.close()


def generate_seats(total, per_row):
    """[(seat_no, row_no, col_no)] e.g. 1A 1B 1C 1D 2A ... the last row may be partial."""
    seats = []
    for i in range(total):
        row, col = divmod(i, per_row)
        seats.append((f"{row + 1}{string.ascii_uppercase[col]}", row + 1, col + 1))
    return seats
