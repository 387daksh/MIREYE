from __future__ import annotations

import json
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.infrastructure.events import DomainEvent


def _url(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


class PostgresOutbox:
    def __init__(self, database_url: str):
        self.database_url = _url(database_url)

    def append(self, conn: Any, event: DomainEvent) -> None:
        conn.execute(
            """INSERT INTO outbox_events (event_id, event_type, aggregate_type, aggregate_id, workspace_id, project_id, occurred_at, correlation_id, causation_id, payload, schema_version)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (event_id) DO NOTHING""",
            (
                event.event_id,
                event.event_type.value,
                event.aggregate_type,
                event.aggregate_id,
                event.workspace_id,
                event.project_id,
                event.occurred_at,
                event.correlation_id,
                event.causation_id,
                json.dumps(event.payload, sort_keys=True),
                event.schema_version,
            ),
        )

    def claim(self, limit: int = 100) -> list[DomainEvent]:
        with psycopg.connect(self.database_url, row_factory=dict_row) as conn:
            rows = conn.execute(
                """SELECT * FROM outbox_events WHERE published_at IS NULL ORDER BY occurred_at
                FOR UPDATE SKIP LOCKED LIMIT %s""",
                (limit,),
            ).fetchall()
            return [
                DomainEvent.model_validate({**row, "payload": json.loads(row["payload"]), "event_type": row["event_type"]}) for row in rows
            ]

    def mark_published(self, event_id: str) -> None:
        with psycopg.connect(self.database_url) as conn:
            conn.execute("UPDATE outbox_events SET published_at = now(), attempts = attempts + 1 WHERE event_id = %s", (event_id,))


class PostgresProcessedEvents:
    def __init__(self, database_url: str):
        self.database_url = _url(database_url)

    def claim(self, consumer: str, event_id: str) -> str:
        with psycopg.connect(self.database_url, row_factory=dict_row) as conn:
            inserted = conn.execute(
                """INSERT INTO processed_events (consumer_name, event_id, status)
                VALUES (%s, %s, 'processing')
                ON CONFLICT (consumer_name, event_id) DO UPDATE
                SET status = 'processing', updated_at = now()
                WHERE processed_events.status = 'processing'
                  AND processed_events.updated_at < now() - interval '5 minutes'
                RETURNING event_id""",
                (consumer, event_id),
            ).fetchone()
            if inserted:
                return "claimed"
            row = conn.execute(
                "SELECT status FROM processed_events WHERE consumer_name = %s AND event_id = %s", (consumer, event_id)
            ).fetchone()
            return "completed" if row and row["status"] == "completed" else "busy"

    def complete(self, consumer: str, event_id: str) -> None:
        with psycopg.connect(self.database_url) as conn:
            conn.execute(
                "UPDATE processed_events SET status = 'completed', completed_at = now(), updated_at = now() WHERE consumer_name = %s AND event_id = %s",
                (consumer, event_id),
            )

    def release(self, consumer: str, event_id: str) -> None:
        with psycopg.connect(self.database_url) as conn:
            conn.execute(
                "DELETE FROM processed_events WHERE consumer_name = %s AND event_id = %s AND status = 'processing'", (consumer, event_id)
            )
