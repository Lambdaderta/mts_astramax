"""Convenient public imports for backend and local solution adapters."""
from .core import LocalConsultationCore, build_core
from .llm_client import LLMConfig

__all__ = ["LocalConsultationCore", "LLMConfig", "build_core"]
