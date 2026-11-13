from typing import Any, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .privacy import normalize_phone


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Empty(Model):
    pass


class TurnInput(Model):
    client_message_id: UUID
    text: str = Field(min_length=1, max_length=4000)

    @field_validator('text', mode='before')
    @classmethod
    def trim(cls, value):
        return value.strip() if isinstance(value, str) else value


class ContactFields(Model):
    full_name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    company: str | None = Field(default=None, max_length=300)

    @field_validator('full_name', 'company', mode='before')
    @classmethod
    def clean(cls, value):
        return (value.strip() or None) if isinstance(value, str) else value

    @field_validator('phone')
    @classmethod
    def phone_format(cls, value):
        return normalize_phone(value)


class DraftPatch(Model):
    expected_revision: int = Field(ge=0, strict=True)
    fields: ContactFields


class LeadInput(Model):
    idempotency_key: UUID
    preview_id: UUID
    confirm: Literal[True]

    @field_validator('confirm', mode='before')
    @classmethod
    def explicit_boolean(cls, value):
        if value is not True:
            raise ValueError('Explicit confirmation is required')
        return value


class Session(Model):
    id: str
    status: Literal['active', 'closed']
    phase: Literal['new','clarification','consulting','collecting_contacts','reviewing_lead','lead_saved','closed']
    revision: int
    opened_at: str
    closed_at: str | None


class Message(Model):
    id: str
    seq: int
    role: Literal['user','assistant']
    text: str
    created_at: str


class SessionCreated(Model):
    session: Session
    session_token: str
    greeting: Message


class SessionResponse(Model):
    session: Session


class MessagePage(Model):
    items: list[Message]
    next_after_seq: int | None


class Draft(ContactFields):
    revision: int
    status: Literal['draft','partial','complete']
    missing_fields: list[str]


class DraftResponse(Model):
    draft: Draft


class Decision(Model):
    kind: Literal['answer','clarify','insufficient_knowledge','out_of_scope','collecting_contacts','lead_review','lead_saved']
    reason_code: str


class TurnCompleted(Model):
    turn_id: str
    status: Literal['completed']
    session: Session
    user_message: Message
    assistant_message: Message
    decision: Decision
    lead_draft: Draft | None = None


class TurnProcessing(Model):
    turn_id: str
    status: Literal['processing']
    poll_after_ms: int = 700


class ErrorDetail(Model):
    code: str
    message: str
    request_id: str
    retryable: bool
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(Model):
    error: ErrorDetail


class TurnFailed(Model):
    turn_id: str
    status: Literal['failed']
    error: ErrorDetail


class Preview(ContactFields):
    preview_id: str
    snapshot_last_seq: int
    draft_revision: int
    status: Literal['draft','partial','complete']
    can_confirm: bool
    client_problem: str
    summary: str
    facts: list[dict[str, Any]]
    recommended_products: list[str]
    open_questions: list[str]
    missing_fields: list[str]


class Lead(ContactFields):
    id: str
    session_id: str
    status: Literal['partial','complete']
    client_problem: str
    summary: str
    facts: list[dict[str, Any]]
    recommended_products: list[str]
    open_questions: list[str]
    missing_fields: list[str]
    created_at: str
    confirmed_at: str


class LeadResponse(Model):
    lead: Lead
    message: str


class SessionPage(Model):
    items: list[Session]
    next_offset: int | None


class ReviewSession(Model):
    session: Session
    messages: list[Message]
    draft: Draft
    leads: list[Lead]


class ReviewLead(Model):
    lead: Lead


class ReviewDraftRecord(Draft):
    record_type: Literal['draft']
    session_id: str


class ReviewLeadRecord(Lead):
    record_type: Literal['confirmed']


class ReviewLeadPage(Model):
    items: list[ReviewDraftRecord | ReviewLeadRecord]
    next_offset: int | None


class Evidence(Model):
    document_id: str
    chunk_id: str
    quote: str
    relevance_score: float | None = None


class Trace(Model):
    assistant_message_id: str
    retrieval_query: str
    decision: Decision
    source_ids: list[str]
    evidence: list[Evidence]
    timings_ms: dict[str,float]
    corpus_version: str
    latency_ms: float
    model_metadata: dict[str,str]
    recommended_products: list[str]


class Readiness(Model):
    status: Literal['ready','not_ready']
    database: bool
    knowledge: bool
    corpus_version: str
