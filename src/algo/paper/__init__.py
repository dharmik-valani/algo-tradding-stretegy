"""Paper trading: virtual fills, multi-strategy desk, optional live LTP."""

from algo.paper.registry import create_strategy, list_strategies
from algo.paper.session import PaperSession, get_session, reset_session

__all__ = [
    "PaperSession",
    "create_strategy",
    "get_session",
    "list_strategies",
    "reset_session",
]
