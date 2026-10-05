from ..config import LMSInstance
from .base import LMSAdapter, SessionExpired
from .canvas import CanvasAdapter
from .moodle import MoodleAdapter

ADAPTERS: dict[str, type[LMSAdapter]] = {"moodle": MoodleAdapter, "canvas": CanvasAdapter}


def make_adapter(inst: LMSInstance, headless: bool = True) -> LMSAdapter:
    return ADAPTERS[inst.kind](inst, headless=headless)


__all__ = ["LMSAdapter", "SessionExpired", "make_adapter"]
