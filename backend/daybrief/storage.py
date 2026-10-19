import sqlite3

from .models import Brief


class Storage:
    def __init__(self, url):
        if not url.startswith("sqlite:///"):
            raise ValueError("Only sqlite:/// URLs are supported")
        self.path = url.removeprefix("sqlite:///")
        with sqlite3.connect(self.path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS briefs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )

    def save(self, brief):
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO briefs VALUES (?,?)", (brief.id, brief.model_dump_json())
            )

    def get(self, id):
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT payload FROM briefs WHERE id=?", (id,)).fetchone()
        return Brief.model_validate_json(row[0]) if row else None
