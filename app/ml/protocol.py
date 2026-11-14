"""Transport-independent, locally defined v1 consultation contract.

The machine-readable contract mentioned in the supplied PDF was not attached.
This module implements the fields documented in that specification.
"""
from __future__ import annotations

from typing import Any, Literal, Protocol
from pydantic import BaseModel, ConfigDict, Field


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContextMessage(ContractModel):
    id: str
    role: Literal["user", "assistant"]
    text: str


class UserFact(ContractModel):
    topic_key: str = "general"
    key: str
    value: Any
    source_message_id: str


class Evidence(ContractModel):
    document_id: str
    chunk_id: str
    quote: str = Field(min_length=1)
    relevance_score: float | None = None


class ConsultationRequest(ContractModel):
    request_id: str
    session_id: str
    user_message_id: str
    latest_user_text: str = Field(min_length=1)
    messages: list[ContextMessage] = Field(default_factory=list)
    current_phase: str = "new"
    active_topic: str | None = None
    facts: list[UserFact] = Field(default_factory=list)
    corpus_version: str = "unavailable"
    language: Literal["ru"] = "ru"
    timeout_budget_ms: int = Field(default=10000, gt=0, le=120000)


class ConsultationResult(ContractModel):
    kind: Literal["answer", "clarify", "insufficient_knowledge", "out_of_scope"]
    answer_text: str = Field(min_length=1, max_length=8000)
    reason_code: str
    retrieval_query: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    recommended_products: list[str] = Field(default_factory=list)
    fact_updates: list[UserFact] = Field(default_factory=list)
    timings_ms: dict[str, float] = Field(default_factory=dict)
    model_metadata: dict[str, str] = Field(default_factory=dict)


class ConsultationUnavailable(RuntimeError):
    """An external model or required consultation dependency is unavailable."""


class CorpusNotReady(ConsultationUnavailable):
    """The local, explicitly supplied corpus is missing or invalid."""


class InvalidEvidence(ValueError):
    """A result failed the corpus/evidence/claim validation gate."""


class InvalidModelOutput(InvalidEvidence):
    """A sanitized, machine-readable model completion or citation failure."""

    def __init__(self, code: str = "invalid_model_output"):
        messages = {
            "model_output_truncated": "The model did not finish its answer within the output budget",
            "invalid_model_output": "The model returned an invalid structured answer",
            "invalid_model_evidence": "The model returned invalid source references",
        }
        self.code = code if code in messages else "invalid_model_output"
        super().__init__(messages[self.code])


class ConsultationCore(Protocol):
    ready: bool
    corpus_version: str

    async def consult(self, request: ConsultationRequest) -> ConsultationResult: ...

    def validate_result(self, result: ConsultationResult) -> None: ...
