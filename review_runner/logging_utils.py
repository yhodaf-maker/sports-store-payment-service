from __future__ import annotations

import logging

LOGGER_NAME = "review-runner"


def get_logger(level: str) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level.upper())
    return logger
