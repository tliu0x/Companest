"""
Persistent event history for the operator console.

Stores lifecycle and operator events in SQLite via aiosqlite so the console can
query recent history and recover after reconnects or restarts.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class StoredEvent:
    sequence_id: int
    event_type: str
    timestamp: str
    company_id: Optional[str]
    payload: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sequence_id": self.sequence_id,
            "type": self.event_type,
            "timestamp": self.timestamp,
            "company_id": self.company_id,
            "payload": self.payload,
        }


class EventStore:
    """SQLite-backed append-only event store."""

    def __init__(self, data_dir: Path):
        self._data_dir = data_dir
        self._db = None

    async def start(self) -> bool:
        try:
            import aiosqlite
        except ImportError:
            logger.info("Event history disabled; missing optional dependency: aiosqlite")
            return False

        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._data_dir / "events.db"))
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                sequence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                company_id TEXT,
                payload_json TEXT NOT NULL
            )
            """
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp DESC)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_company ON events(company_id)"
        )
        await self._db.commit()
        return True

    async def close(self) -> None:
        if self._db is None:
            return
        await self._db.close()
        self._db = None

    async def get_latest_sequence(self) -> int:
        if self._db is None:
            return 0
        async with self._db.execute(
            "SELECT COALESCE(MAX(sequence_id), 0) FROM events"
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0] if row else 0)

    async def append_event(
        self,
        event_type: str,
        payload: Dict[str, Any],
        timestamp: str,
        company_id: Optional[str] = None,
    ) -> StoredEvent:
        if self._db is None:
            raise RuntimeError("EventStore is not started")

        cursor = await self._db.execute(
            """
            INSERT INTO events (event_type, timestamp, company_id, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (event_type, timestamp, company_id, json.dumps(payload)),
        )
        await self._db.commit()
        sequence_id = int(cursor.lastrowid)
        return StoredEvent(
            sequence_id=sequence_id,
            event_type=event_type,
            timestamp=timestamp,
            company_id=company_id,
            payload=payload,
        )

    async def list_events(
        self,
        *,
        event_type: Optional[str] = None,
        company_id: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[StoredEvent], int]:
        if self._db is None:
            return [], 0

        where_parts: List[str] = []
        params: List[Any] = []
        if event_type:
            where_parts.append("event_type = ?")
            params.append(event_type)
        if company_id:
            where_parts.append("company_id = ?")
            params.append(company_id)
        if start:
            where_parts.append("timestamp >= ?")
            params.append(start)
        if end:
            where_parts.append("timestamp <= ?")
            params.append(end)

        where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

        async with self._db.execute(
            f"SELECT COUNT(*) FROM events {where_sql}",
            params,
        ) as count_cursor:
            count_row = await count_cursor.fetchone()
        total = int(count_row[0] if count_row else 0)

        query_params = [*params, limit, offset]
        async with self._db.execute(
            f"""
            SELECT sequence_id, event_type, timestamp, company_id, payload_json
            FROM events
            {where_sql}
            ORDER BY sequence_id DESC
            LIMIT ? OFFSET ?
            """,
            query_params,
        ) as cursor:
            rows = await cursor.fetchall()

        events = [
            StoredEvent(
                sequence_id=int(row[0]),
                event_type=row[1],
                timestamp=row[2],
                company_id=row[3],
                payload=json.loads(row[4]) if row[4] else {},
            )
            for row in rows
        ]
        return events, total
