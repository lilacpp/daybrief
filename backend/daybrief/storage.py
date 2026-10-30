import sqlite3

from .models import Brief, VisualPlan


class Storage:
    def __init__(self, url):
        if not url.startswith("sqlite:///"):
            raise ValueError("Only sqlite:/// URLs are supported")
        self.path = url.removeprefix("sqlite:///")
        with sqlite3.connect(self.path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS briefs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )

            db.execute(
                "CREATE TABLE IF NOT EXISTS visual_plans (brief_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )

    def save_visual_plan(self, plan):
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT OR REPLACE INTO visual_plans VALUES (?, ?)",
                (plan.brief_id, plan.model_dump_json()),
            )

    def get_visual_plan(self, brief_id):
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT payload FROM visual_plans WHERE brief_id=?", (brief_id,)).fetchone()
        return VisualPlan.model_validate_json(row[0]) if row else None

    def save(self, brief):
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO briefs VALUES (?,?)", (brief.id, brief.model_dump_json())
            )

    def get(self, id):
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT payload FROM briefs WHERE id=?", (id,)).fetchone()
        return Brief.model_validate_json(row[0]) if row else None
