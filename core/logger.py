# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Sleep is overrated anyway
import sys

from loguru import logger

__all__ = ["logger"]

logger.level("DEBUG", icon="·")
logger.level("INFO", icon="i")
logger.level("WARNING", icon="!")
logger.level("ERROR", icon="x")
logger.level("SUCCESS", icon="+")
logger.level("CRITICAL", icon="#")


def formatter(record):
    time = "<green>{time:YYYY-MM-DD HH:mm:ss}</green>"
    # level = "<level>{level.name:<8}</level>"
    level = "<level>{level.icon}</level>"
    message = "<level>{message}</level>"

    account = record["extra"].get("account")
    message = message = f"<cyan>[{account}]</cyan> {message}" if account else message

    extra = sorted(record["extra"].items(), key=lambda x: x[0])
    extra = [(k, v) for k, v in extra if k != "account"]
    extra = [f"<cyan>{k}</cyan>=<yellow>{v}</yellow>" for k, v in extra]
    extra = " ".join(extra)
    extra = f" {extra}" if extra else ""

    return f"{time} | {level} | {message}{extra}\n"


# https://github.com/Delgan/loguru/blob/0.7.3/loguru/_defaults.py#L32-L38
logger.remove()
logger.add(sys.stderr, format=formatter)
