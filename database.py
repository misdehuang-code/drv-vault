"""SQLite 数据存储模块。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "scraper.db"


class Status(str, Enum):
    PENDING = "pending"
    CLEANED = "cleaned"
    ERROR = "error"
    APPROVED = "approved"
    DELETED = "deleted"


class Database:
    """封装抓取结果的 SQLite 读写。"""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path) if db_path != ":memory:" else Path(":memory:")
        self._is_memory = str(db_path) == ":memory:"

        if not self._is_memory:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # 使用长连接，避免 :memory: 多连接各自独立库的问题
        self._conn = sqlite3.connect(
            ":memory:" if self._is_memory else str(self.db_path)
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scraped_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                device_model TEXT,
                source_url TEXT NOT NULL,
                raw_content TEXT,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN (
                        'pending', 'cleaned', 'error', 'approved', 'deleted'
                    )),
                created_at TEXT NOT NULL,
                os_version TEXT,
                seo_title TEXT,
                value_score REAL
            )
            """
        )
        # 兼容旧库：补齐清洗字段，并放宽 status 枚举
        self._ensure_column("os_version", "TEXT")
        self._ensure_column("seo_title", "TEXT")
        self._ensure_column("value_score", "REAL")
        self._migrate_status_constraint()

        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_scraped_items_status
            ON scraped_items (status)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_scraped_items_source_url
            ON scraped_items (source_url)
            """
        )
        self._init_forum_posts_schema()
        self._conn.commit()

    def _init_forum_posts_schema(self) -> None:
        """论坛/问答线索表：保存关键词命中帖与提取出的型号、报错码。"""
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS forum_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                keyword TEXT NOT NULL,
                title TEXT,
                source_url TEXT NOT NULL UNIQUE,
                device_model TEXT,
                error_codes TEXT,
                raw_content TEXT,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN (
                        'pending', 'cleaned', 'error', 'approved', 'deleted'
                    )),
                created_at TEXT NOT NULL,
                device_category TEXT,
                pain_point TEXT,
                value_score REAL
            )
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_forum_posts_keyword
            ON forum_posts (keyword)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_forum_posts_status
            ON forum_posts (status)
            """
        )
        # AI 清洗字段（兼容已有库）
        self._ensure_forum_column("device_category", "TEXT")
        self._ensure_forum_column("pain_point", "TEXT")
        self._ensure_forum_column("value_score", "REAL")

    def _ensure_column(self, name: str, col_type: str) -> None:
        cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(scraped_items)").fetchall()
        }
        if name not in cols:
            self._conn.execute(
                f"ALTER TABLE scraped_items ADD COLUMN {name} {col_type}"
            )

    def _ensure_forum_column(self, name: str, col_type: str) -> None:
        cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(forum_posts)").fetchall()
        }
        if name not in cols:
            self._conn.execute(
                f"ALTER TABLE forum_posts ADD COLUMN {name} {col_type}"
            )

    def _migrate_status_constraint(self) -> None:
        """旧库 CHECK 不含 approved/deleted 时重建表。"""
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='scraped_items'"
        ).fetchone()
        ddl = (row[0] if row else "") or ""
        if "approved" in ddl and "deleted" in ddl:
            return

        self._conn.execute("BEGIN")
        try:
            self._conn.execute(
                """
                CREATE TABLE scraped_items_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT,
                    device_model TEXT,
                    source_url TEXT NOT NULL,
                    raw_content TEXT,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN (
                            'pending', 'cleaned', 'error', 'approved', 'deleted'
                        )),
                    created_at TEXT NOT NULL,
                    os_version TEXT,
                    seo_title TEXT
                )
                """
            )
            self._conn.execute(
                """
                INSERT INTO scraped_items_new
                    (id, title, device_model, source_url, raw_content,
                     status, created_at, os_version, seo_title)
                SELECT id, title, device_model, source_url, raw_content,
                       status, created_at, os_version, seo_title
                FROM scraped_items
                """
            )
            self._conn.execute("DROP TABLE scraped_items")
            self._conn.execute(
                "ALTER TABLE scraped_items_new RENAME TO scraped_items"
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def insert_item(
        self,
        *,
        source_url: str,
        title: Optional[str] = None,
        device_model: Optional[str] = None,
        raw_content: Optional[str] = None,
        status: Status | str = Status.PENDING,
        os_version: Optional[str] = None,
        seo_title: Optional[str] = None,
    ) -> int:
        status_value = status.value if isinstance(status, Status) else status
        cursor = self._conn.execute(
            """
            INSERT INTO scraped_items
                (title, device_model, source_url, raw_content, status,
                 created_at, os_version, seo_title)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                device_model,
                source_url,
                raw_content,
                status_value,
                self._now_iso(),
                os_version,
                seo_title,
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def update_status(
        self,
        item_id: int,
        status: Status | str,
        *,
        raw_content: Optional[str] = None,
        title: Optional[str] = None,
        device_model: Optional[str] = None,
        os_version: Optional[str] = None,
        seo_title: Optional[str] = None,
        value_score: Optional[float] = None,
    ) -> None:
        status_value = status.value if isinstance(status, Status) else status
        fields: list[str] = ["status = ?"]
        values: list[Any] = [status_value]

        if raw_content is not None:
            fields.append("raw_content = ?")
            values.append(raw_content)
        if title is not None:
            fields.append("title = ?")
            values.append(title)
        if device_model is not None:
            fields.append("device_model = ?")
            values.append(device_model)
        if os_version is not None:
            fields.append("os_version = ?")
            values.append(os_version)
        if seo_title is not None:
            fields.append("seo_title = ?")
            values.append(seo_title)
        if value_score is not None:
            fields.append("value_score = ?")
            values.append(float(value_score))

        values.append(item_id)
        self._conn.execute(
            f"UPDATE scraped_items SET {', '.join(fields)} WHERE id = ?",
            values,
        )
        self._conn.commit()

    def mark_cleaned(
        self,
        item_id: int,
        *,
        device_model: str,
        os_version: str,
        seo_title: str,
        title: Optional[str] = None,
        value_score: Optional[float] = None,
    ) -> None:
        """将清洗结果写回并标记为 cleaned。"""
        self.update_status(
            item_id,
            Status.CLEANED,
            device_model=device_model,
            os_version=os_version,
            seo_title=seo_title,
            title=title,
            value_score=value_score,
        )

    def discard_item(
        self,
        item_id: int,
        *,
        reason: str,
        title: Optional[str] = None,
        device_model: Optional[str] = None,
    ) -> None:
        """黑名单等场景：标记删除，value_score=0。"""
        self.update_status(
            item_id,
            Status.DELETED,
            title=title,
            device_model=device_model,
            value_score=0.0,
            seo_title=f"[已丢弃] {reason}" if reason else None,
        )

    def get_item(self, item_id: int) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM scraped_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_by_status(
        self,
        status: Status | str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        status_value = status.value if isinstance(status, Status) else status
        rows = self._conn.execute(
            """
            SELECT * FROM scraped_items
            WHERE status = ?
            ORDER BY id ASC
            LIMIT ? OFFSET ?
            """,
            (status_value, limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_random_by_status(
        self,
        status: Status | str,
        *,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """随机抽取指定状态的记录（用于人工抽检）。"""
        status_value = status.value if isinstance(status, Status) else status
        rows = self._conn.execute(
            """
            SELECT * FROM scraped_items
            WHERE status = ?
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (status_value, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def count_by_status(self, status: Status | str) -> int:
        status_value = status.value if isinstance(status, Status) else status
        row = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM scraped_items WHERE status = ?",
            (status_value,),
        ).fetchone()
        return int(row["cnt"]) if row else 0

    def approve_item(self, item_id: int) -> None:
        self.update_status(item_id, Status.APPROVED)

    def soft_delete_item(self, item_id: int) -> None:
        """标记删除（软删除，保留原始数据便于追溯）。"""
        self.update_status(item_id, Status.DELETED)

    def exists_url(self, source_url: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM scraped_items WHERE source_url = ? LIMIT 1",
            (source_url,),
        ).fetchone()
        return row is not None

    def bulk_insert(self, items: Iterable[dict[str, Any]]) -> int:
        """批量插入，返回成功写入条数。"""
        created_at = self._now_iso()
        rows = []
        for item in items:
            status = item.get("status", Status.PENDING.value)
            if isinstance(status, Status):
                status = status.value
            rows.append(
                (
                    item.get("title"),
                    item.get("device_model"),
                    item["source_url"],
                    item.get("raw_content"),
                    status,
                    created_at,
                    item.get("os_version"),
                    item.get("seo_title"),
                )
            )

        if not rows:
            return 0

        self._conn.executemany(
            """
            INSERT INTO scraped_items
                (title, device_model, source_url, raw_content, status,
                 created_at, os_version, seo_title)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self._conn.commit()
        return len(rows)

    # ---- forum_posts ----

    def forum_exists_url(self, source_url: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM forum_posts WHERE source_url = ? LIMIT 1",
            (source_url,),
        ).fetchone()
        return row is not None

    def insert_forum_post(
        self,
        *,
        source: str,
        keyword: str,
        source_url: str,
        title: Optional[str] = None,
        device_model: Optional[str] = None,
        error_codes: Optional[str] = None,
        raw_content: Optional[str] = None,
        status: Status | str = Status.PENDING,
    ) -> Optional[int]:
        """按 source_url 去重插入论坛帖；已存在则返回 None。"""
        if self.forum_exists_url(source_url):
            return None
        status_value = status.value if isinstance(status, Status) else status
        cursor = self._conn.execute(
            """
            INSERT INTO forum_posts
                (source, keyword, title, source_url, device_model,
                 error_codes, raw_content, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source,
                keyword,
                title,
                source_url,
                device_model,
                error_codes,
                raw_content,
                status_value,
                self._now_iso(),
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def list_forum_posts(
        self,
        *,
        limit: int = 50,
        keyword: Optional[str] = None,
        status: Optional[Status | str] = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if keyword:
            clauses.append("keyword = ?")
            values.append(keyword)
        if status is not None:
            status_value = status.value if isinstance(status, Status) else status
            clauses.append("status = ?")
            values.append(status_value)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(limit)
        rows = self._conn.execute(
            f"""
            SELECT * FROM forum_posts
            {where}
            ORDER BY id ASC
            LIMIT ?
            """,
            values,
        ).fetchall()
        return [dict(row) for row in rows]

    def update_forum_analysis(
        self,
        item_id: int,
        *,
        device_category: str,
        device_model: str,
        pain_point: str,
        status: Status | str = Status.CLEANED,
        error_codes: Optional[str] = None,
        value_score: Optional[float] = None,
    ) -> None:
        status_value = status.value if isinstance(status, Status) else status
        fields = [
            "device_category = ?",
            "device_model = ?",
            "pain_point = ?",
            "status = ?",
        ]
        values: list[Any] = [
            device_category,
            device_model,
            pain_point,
            status_value,
        ]
        if error_codes is not None:
            fields.append("error_codes = ?")
            values.append(error_codes)
        if value_score is not None:
            fields.append("value_score = ?")
            values.append(float(value_score))
        values.append(item_id)
        self._conn.execute(
            f"UPDATE forum_posts SET {', '.join(fields)} WHERE id = ?",
            values,
        )
        self._conn.commit()

    def list_forum_cleaned(self, *, limit: int = 5000) -> list[dict[str, Any]]:
        return self.list_forum_posts(limit=limit, status=Status.CLEANED)

    def count_forum_posts(self, status: Optional[Status | str] = None) -> int:
        if status is None:
            row = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM forum_posts"
            ).fetchone()
        else:
            status_value = status.value if isinstance(status, Status) else status
            row = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM forum_posts WHERE status = ?",
                (status_value,),
            ).fetchone()
        return int(row["cnt"]) if row else 0
