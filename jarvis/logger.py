from __future__ import annotations
import logging
from pathlib import Path
from .security import redact

class RedactingFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact(str(record.msg))
        if record.args:
            record.args = tuple(redact(str(x)) for x in record.args)
        return True

def setup_logger(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("jarvis")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.FileHandler(root / "activity.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)
    return logger
