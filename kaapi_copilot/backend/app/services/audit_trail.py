"""
Append-only, hash-chained audit trail backed by SQLite. Every entry embeds a
SHA-256 hash of (prev_hash + event content) — tampering with any past row
breaks the chain, detectable via verify_chain().
"""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from app.models.domain import AuditEvent, new_event_id
from app.core.config import settings

GENESIS_HASH = "0" * 64


class AuditTrail:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or settings.db_path
        self._init_db()

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("""CREATE TABLE IF NOT EXISTS audit_log (
                event_id TEXT PRIMARY KEY, ts TEXT, event_type TEXT,
                session_id TEXT, payload TEXT, prev_hash TEXT, hash TEXT
            )""")

    def _last_hash(self, conn) -> str:
        row = conn.execute("SELECT hash FROM audit_log ORDER BY rowid DESC LIMIT 1").fetchone()
        return row[0] if row else GENESIS_HASH

    def log(self, event_type: str, session_id: str, payload: dict) -> AuditEvent:
        with self._conn() as c:
            prev_hash = self._last_hash(c)
            ts = datetime.now(timezone.utc).isoformat()
            event_id = new_event_id()
            payload_json = json.dumps(payload, sort_keys=True, default=str)
            digest_input = f"{event_id}|{ts}|{event_type}|{session_id}|{payload_json}|{prev_hash}"
            entry_hash = hashlib.sha256(digest_input.encode()).hexdigest()
            c.execute(
                "INSERT INTO audit_log VALUES (?,?,?,?,?,?,?)",
                (event_id, ts, event_type, session_id, payload_json, prev_hash, entry_hash),
            )
            return AuditEvent(event_id, ts, event_type, session_id, payload, prev_hash, entry_hash)

    def list_events(self, session_id: str = None, limit: int = 200) -> list:
        with self._conn() as c:
            if session_id:
                rows = c.execute(
                    "SELECT * FROM audit_log WHERE session_id=? ORDER BY rowid DESC LIMIT ?",
                    (session_id, limit)).fetchall()
            else:
                rows = c.execute("SELECT * FROM audit_log ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        events = []
        for r in rows:
            events.append({
                "event_id": r[0], "ts": r[1], "event_type": r[2], "session_id": r[3],
                "payload": json.loads(r[4]), "prev_hash": r[5], "hash": r[6],
            })
        return events

    def verify_chain(self) -> dict:
        """Recomputes every hash in insertion order; reports first break, if any."""
        with self._conn() as c:
            rows = c.execute("SELECT * FROM audit_log ORDER BY rowid ASC").fetchall()
        prev_hash = GENESIS_HASH
        for i, r in enumerate(rows):
            event_id, ts, event_type, session_id, payload_json, stored_prev, stored_hash = r
            if stored_prev != prev_hash:
                return {"valid": False, "broken_at_row": i, "event_id": event_id, "reason": "prev_hash mismatch"}
            digest_input = f"{event_id}|{ts}|{event_type}|{session_id}|{payload_json}|{stored_prev}"
            recomputed = hashlib.sha256(digest_input.encode()).hexdigest()
            if recomputed != stored_hash:
                return {"valid": False, "broken_at_row": i, "event_id": event_id, "reason": "hash mismatch (tampered)"}
            prev_hash = stored_hash
        return {"valid": True, "entries": len(rows)}


audit_trail = AuditTrail()
