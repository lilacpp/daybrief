"""Replay a search snapshot without making any search-provider requests.

Run from backend: python -m daybrief.replay --seed .daybrief-cache/search/FILE.json
Model requests may still run when the current analysis is not cached.
"""

import argparse
import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from .adapters import Nemotron, ProviderError, Tavily
from .config import Settings
from .models import Article
from .pipeline import run_pipeline
from .search_queries import GENERAL_QUERIES, MARKET_QUERIES


class SnapshotSearch(Tavily):
    def __init__(self, settings, seed_path):
        super().__init__(settings)
        self.seed = json.loads(seed_path.read_text())
        self.records = [
            json.loads(p.read_text()) for p in sorted(seed_path.parent.glob("*.json"))
        ]
        self.misses = []

    async def search_news(self, topic, hours, now):
        queries = {"markets": MARKET_QUERIES, "general": GENERAL_QUERIES}.get(topic, ())
        if self.seed["request"]["query"] in queries:
            return await super().search_news(topic, hours, now)
        return self.parse(self.seed)

    @staticmethod
    def parse(record):
        return [
            Article.model_validate_json(json.dumps(a))
            for a in record["value"]["articles"]
        ]

    async def _search(self, query, topic, hours, now, max_results):
        matches = [
            record
            for record in self.records
            if record["request"]["query"] == query
            and record["request"]["topic"] == topic
            and record["request"]["hours"] == hours
            and record["request"]["start_date"] == self.seed["request"]["start_date"]
        ]
        if not matches:
            self.misses.append(query)
            logging.getLogger("uvicorn.error").warning(
                "Replay search snapshot missing: %s", query
            )
            return []
        record = max(matches, key=lambda r: r["saved_at"])
        logging.getLogger("uvicorn.error").info("Replay local search: %s", query)
        return self.parse(record)


class EventCollector(logging.Handler):
    def __init__(self):
        super().__init__()
        self.events = []

    def emit(self, record):
        message = record.getMessage()
        if message.startswith("Pipeline details "):
            value = json.loads(message.removeprefix("Pipeline details "))
            if value["stage"] == "event_final":
                self.events.append(value)


async def replay(seed_path, output):
    output.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("uvicorn.error")
    logger.setLevel(logging.INFO)
    file_handler = logging.FileHandler(output / "run.log", mode="w")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    console = logging.StreamHandler()
    console.addFilter(
        lambda record: not record.getMessage().startswith("Pipeline details ")
    )
    collector = EventCollector()
    handlers = [file_handler, console, collector]
    for handler in handlers:
        logger.addHandler(handler)
    settings = Settings()
    search = SnapshotSearch(settings, seed_path)
    # Freeze publication filtering to the original retrieval, not today's clock.
    originals = search.parse(search.seed)
    now = originals[0].retrieved_at
    request = search.seed["request"]
    summary = {
        "seed": str(seed_path.resolve()),
        "now": now.isoformat(),
        "search_network_calls": 0,
        "status": "failed",
    }
    try:
        settings.validate_real()
        brief = await run_pipeline(
            request["topic"],
            request["hours"],
            now,
            search,
            Nemotron(settings),
            "real",
            enrichment_max_candidates=settings.enrichment_max_candidates,
            enrichment_queries_per_candidate=settings.enrichment_queries_per_candidate,
            fallback_max_candidates=settings.fallback_max_candidates,
            fallback_max_articles=settings.fallback_max_articles,
        )
        (output / "brief.json").write_text(brief.model_dump_json(indent=2))
        summary["status"] = "success"
        summary["selected"] = len(brief.selected_events)
    except (ProviderError, ValueError) as exc:
        summary["error"] = str(exc)
        logger.error("Replay failed: %s", exc)
    finally:
        summary["events"] = collector.events
        summary["corroborated"] = sum(e["corroborated"] for e in collector.events)
        summary["search_snapshot_misses"] = search.misses
        (output / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2)
        )
        for handler in handlers:
            logger.removeHandler(handler)
            handler.close()
    return summary["status"] == "success"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or Path(".daybrief-cache/replays") / datetime.now(
        UTC
    ).strftime("%Y%m%dT%H%M%S")
    raise SystemExit(0 if asyncio.run(replay(args.seed, output)) else 1)


if __name__ == "__main__":
    main()
