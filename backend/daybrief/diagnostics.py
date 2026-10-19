"""Full JSON snapshots of news data for pipeline diagnosis."""

import json
import logging

from pydantic import BaseModel


def _serialize(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return str(value)


def log_details(stage, **data):
    # JSON escapes embedded newlines so each snapshot remains one log record.
    logging.getLogger("uvicorn.error").info(
        "Pipeline details %s",
        json.dumps({"stage": stage, **data}, ensure_ascii=False, default=_serialize),
    )
