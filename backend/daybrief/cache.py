"""Readable, expiring local provider snapshots; credentials are never cache keys."""

import hashlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path

logger = logging.getLogger("uvicorn.error")


class FileCache:
    def __init__(self, settings):
        self.root = Path(settings.provider_cache_dir)
        self.enabled = settings.provider_cache_enabled
        self.ttl = settings.provider_cache_ttl_seconds

    def path(self, kind, key):
        digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
        return self.root / kind / f"{digest}.json"

    def get(self, kind, key):
        if not self.enabled:
            return None
        path = self.path(kind, key)
        try:
            record = json.loads(path.read_text())
            if time.time() - record["saved_at"] >= self.ttl:
                logger.info("Cache expired kind=%s file=%s", kind, path)
                return None
            logger.info("Cache hit kind=%s file=%s", kind, path)
            return record["value"]
        except (OSError, ValueError, KeyError, TypeError):
            logger.info("Cache miss kind=%s file=%s", kind, path)
            return None

    def put(self, kind, key, value):
        if not self.enabled:
            return
        path = self.path(kind, key)
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", dir=path.parent, delete=False
            ) as file:
                temporary = file.name
                json.dump(
                    {"saved_at": time.time(), "request": key, "value": value},
                    file,
                    ensure_ascii=False,
                    indent=2,
                )
            os.replace(temporary, path)
            logger.info("Cache saved kind=%s file=%s", kind, path)
        except OSError as exc:
            logger.warning("Cache write failed kind=%s: %s", kind, exc)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
