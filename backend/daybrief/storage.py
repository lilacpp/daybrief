import logging
import sqlite3

from .brief_cache import brief_cache_key, cache_expiry, usable_cached_brief
from .models import Brief, VisualPlan

logger = logging.getLogger("uvicorn.error")


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


    @staticmethod
    def _ensure_brief_cache(db):
        # Only invoked by cache operations; disabled caching never touches this table.
        db.execute("CREATE TABLE IF NOT EXISTS brief_cache (cache_key TEXT NOT NULL, brief_id TEXT NOT NULL, generated_at REAL NOT NULL, PRIMARY KEY (cache_key, brief_id))")
        db.execute("CREATE INDEX IF NOT EXISTS brief_cache_recent ON brief_cache(cache_key, generated_at DESC)")

    def save_cached_brief(self, key, brief):
        if not usable_cached_brief(brief):
            return False
        if key != brief_cache_key(brief.topic, brief.lookback_hours, brief.mode):
            return False
        with sqlite3.connect(self.path) as db:
            self._ensure_brief_cache(db)
            db.execute("INSERT OR REPLACE INTO brief_cache VALUES (?, ?, ?)",
                       (key, brief.id, brief.generated_at.timestamp()))
        logger.info("brief_cache_saved brief_id=%s story_count=%d", brief.id, len(brief.selected_events))
        return True

    def get_cached_brief(self, key, now, ttl_seconds):
        with sqlite3.connect(self.path) as db:
            self._ensure_brief_cache(db)
            rows = db.execute(
                "SELECT briefs.payload FROM brief_cache JOIN briefs ON briefs.id=brief_cache.brief_id "
                "WHERE cache_key=? ORDER BY brief_cache.generated_at DESC", (key,),
            ).fetchall()
        for (payload,) in rows:
            try:
                brief = Brief.model_validate_json(payload)
                if key != brief_cache_key(brief.topic, brief.lookback_hours, brief.mode):
                    continue
                if now >= cache_expiry(brief, ttl_seconds):
                    logger.info("brief_cache_expired brief_id=%s", brief.id)
                    continue
                if brief.generated_at > now or not usable_cached_brief(brief):
                    continue
                return brief
            except (ValueError, TypeError):
                logger.warning("brief_cache_invalid")
        return None
