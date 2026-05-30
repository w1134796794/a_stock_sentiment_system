"""
KBStore —— SQLite 块存储。

每个知识块（chunk）= 一段可检索文本 + 元数据（date/kind/stock_code/sector）+ 可选向量。
向量以 float32 字节存入 BLOB；没有向量时检索层走词法回退。

单机个人工具，块量级在万级以内，向量检索直接用 numpy 全量余弦即可，无需向量数据库。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np


@dataclass
class Chunk:
    id: str
    date: str
    kind: str                       # market / plan / signal / review
    text: str
    stock_code: str = ""
    sector: str = ""
    embedding: Optional[np.ndarray] = field(default=None, repr=False)


class KBStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS kb_chunks(
                    id TEXT PRIMARY KEY,
                    date TEXT,
                    kind TEXT,
                    stock_code TEXT,
                    sector TEXT,
                    text TEXT,
                    embedding BLOB,
                    dim INTEGER,
                    updated_at TEXT DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_kb_date ON kb_chunks(date);
                CREATE INDEX IF NOT EXISTS idx_kb_kind ON kb_chunks(kind);
                CREATE INDEX IF NOT EXISTS idx_kb_code ON kb_chunks(stock_code);
                """
            )

    # ------------------------------------------------------------------
    def delete_by_date(self, date: str, kinds: Optional[List[str]] = None) -> None:
        with self._connect() as conn:
            if kinds:
                ph = ",".join("?" * len(kinds))
                conn.execute(f"DELETE FROM kb_chunks WHERE date=? AND kind IN ({ph})",
                             [date, *kinds])
            else:
                conn.execute("DELETE FROM kb_chunks WHERE date=?", (date,))

    def delete_by_kind(self, kind: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM kb_chunks WHERE kind=?", (kind,))

    def upsert_many(self, chunks: List[Chunk]) -> int:
        rows = []
        for c in chunks:
            emb_bytes, dim = None, None
            if c.embedding is not None:
                arr = np.asarray(c.embedding, dtype=np.float32).ravel()
                emb_bytes, dim = arr.tobytes(), int(arr.shape[0])
            rows.append((c.id, c.date, c.kind, c.stock_code, c.sector, c.text, emb_bytes, dim))
        with self._connect() as conn:
            conn.executemany(
                """INSERT INTO kb_chunks(id, date, kind, stock_code, sector, text, embedding, dim)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     date=excluded.date, kind=excluded.kind, stock_code=excluded.stock_code,
                     sector=excluded.sector, text=excluded.text, embedding=excluded.embedding,
                     dim=excluded.dim, updated_at=datetime('now')""",
                rows,
            )
        return len(rows)

    # ------------------------------------------------------------------
    def fetch(self,
              date_from: Optional[str] = None,
              date_to: Optional[str] = None,
              kinds: Optional[List[str]] = None,
              stock_code: Optional[str] = None,
              require_embedding: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT id,date,kind,stock_code,sector,text,embedding,dim FROM kb_chunks WHERE 1=1"
        params: List[Any] = []
        if date_from:
            sql += " AND date>=?"; params.append(date_from)
        if date_to:
            sql += " AND date<=?"; params.append(date_to)
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"; params.extend(kinds)
        if stock_code:
            sql += " AND stock_code=?"; params.append(stock_code)
        if require_embedding:
            sql += " AND embedding IS NOT NULL"
        with self._connect() as conn:
            cur = conn.execute(sql, params)
            return [self._row_to_dict(r) for r in cur.fetchall()]

    @staticmethod
    def _row_to_dict(r: sqlite3.Row) -> Dict[str, Any]:
        emb = None
        if r["embedding"] is not None and r["dim"]:
            emb = np.frombuffer(r["embedding"], dtype=np.float32, count=r["dim"])
        return {
            "id": r["id"], "date": r["date"], "kind": r["kind"],
            "stock_code": r["stock_code"], "sector": r["sector"],
            "text": r["text"], "embedding": emb,
        }

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT count(*) FROM kb_chunks").fetchone()[0]

    def dates(self) -> List[str]:
        with self._connect() as conn:
            return [r[0] for r in conn.execute(
                "SELECT DISTINCT date FROM kb_chunks ORDER BY date").fetchall()]

    def has_any_embeddings(self) -> bool:
        with self._connect() as conn:
            return conn.execute(
                "SELECT 1 FROM kb_chunks WHERE embedding IS NOT NULL LIMIT 1").fetchone() is not None
