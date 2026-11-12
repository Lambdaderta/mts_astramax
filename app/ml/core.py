"""A conservative, runnable RAG baseline with no shared conversation state."""
from __future__ import annotations

import re
import time
from pathlib import Path

from .corpus import Corpus
from .grounding import SAFE_RESPONSES, UNTRUSTED_INSTRUCTIONS, render_excerpts, validate_result, validate_generated_result
from .llm_client import EvidenceSelector, LLMConfig
from .protocol import ConsultationRequest, ConsultationResult, ContextMessage, CorpusNotReady, Evidence, InvalidEvidence
from .retriever import BM25Retriever, SearchHit, tokens

CONTACT_PATTERN = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\+\d[\d ()-]{5,}\d|(?<!\d)[78](?:[ ()-]*\d){10}(?!\d)")
OUT_OF_SCOPE = re.compile(r"настро(?:ить|йк|йт)|прошив|пароль|схем\w*\s+подключ|техническ\w*\s+документац|инструкци\w*\s+(?:для|по|к)\s+(?:счетчик|счётчик|прибор)|пошагов\w*\s+подключ", re.I)
INJECTION = re.compile(r"игнориру|ignore\s+(?:all\s+)?(?:previous|system)|system\s*prompt|системн\w*\s+(?:промпт|инструкц)|(?:придум|выдум)\w*.*(?:цен|тариф|эконом)|(?:цен|тариф).*из\s+голов", re.I)
PRICE_QUERY = re.compile(r"цен|стоим|стоит|рубл|окупаем", re.I)
PRICE_SOURCE = re.compile(r"цен|стоим|рубл|₽|окупаем", re.I)
COMPATIBILITY_QUERY = re.compile(r"совмест|поддержива|работа\w*\s+с\s", re.I)
COMPATIBILITY_SOURCE = re.compile(r"совмест|поддержива|работа\w*\s+с\s", re.I)


def redact_contacts(text: str) -> str:
    """Defense in depth; backend must keep all contact fields out of ML input."""
    def replace_contact(match: re.Match) -> str:
        # Long numeric consumption/budget values are customer facts, not phones.
        suffix = text[match.end():match.end() + 30]
        if re.match(r"\s*(?:[гмк]?вт|руб|₽|доллар|евро|%|процент|счетчик|счётчик|датчик|шт|лет|год|месяц)", suffix, re.I):
            return match.group()
        return "[контакт скрыт]"
    text = CONTACT_PATTERN.sub(replace_contact, text)
    text = re.sub(r"(?i)((?:телефон|позвоните|номер\s+телефона)\s*[:—-]?\s*)(?:\+?\d[\d ()-]{5,}\d)", r"\1[контакт скрыт]", text)
    return re.sub(r"(?i)(?:меня\s+зовут|фио\s*:)\s*[^.!?\n;]+", "[имя скрыто]", text)


def build_query(request: ConsultationRequest) -> str:
    latest = redact_contacts(request.latest_user_text).strip()
    query = latest
    # Resolve short follow-ups from this request only; never use process history.
    if len(tokens(latest)) <= 2 or re.search(r"\b(?:это|этого|него|нее|они|такой|такого|также)\b", latest, re.I):
        previous = [message for message in request.messages if message.role == "user" and message.id != request.user_message_id]
        for message in reversed(previous):
            prior = redact_contacts(message.text)
            if len(tokens(prior)) >= 2:
                query = prior[:1000] + "\n" + latest
                break
    if request.active_topic:
        query += "\nТема: " + redact_contacts(request.active_topic)[:150]
    return query[:4000]


def _eligible(hit: SearchHit, query: str) -> bool:
    query_tokens = set(tokens(query))
    if hit.overlap < min(2, len(query_tokens)) or hit.coverage < 0.75:
        return False
    if UNTRUSTED_INSTRUCTIONS.search(hit.chunk.text):
        return False
    # Intact oversized paragraphs stay in the index but need manual chunk review.
    if len(hit.chunk.text.split()) > 125 or len(hit.chunk.text) > 6000:
        return False
    if PRICE_QUERY.search(query) and not PRICE_SOURCE.search(hit.chunk.text):
        return False
    if COMPATIBILITY_QUERY.search(query) and not COMPATIBILITY_SOURCE.search(hit.chunk.text):
        return False
    return True


def _conflicting_prices(hits: list[SearchHit], query: str) -> bool:
    if not PRICE_QUERY.search(query):
        return False
    price_sets = {
        tuple(re.findall(r"\d[\d\s.,]*(?:\s+[а-яА-Яa-zA-Z]+){0,2}\s*(?:₽|руб\w*)", hit.chunk.text, re.I))
        for hit in hits
    }
    price_sets.discard(())
    return len(price_sets) > 1


class LocalConsultationCore:
    def __init__(self, index_dir: str | Path, *, llm_config: LLMConfig | None = None):
        try:
            self.corpus: Corpus | None = Corpus.load(index_dir)
        except CorpusNotReady:
            self.corpus = None
        self.ready = self.corpus is not None
        self.corpus_version = self.corpus.version if self.corpus else "unavailable"
        self.retriever = BM25Retriever(self.corpus) if self.corpus else None
        self.selector = EvidenceSelector(llm_config) if llm_config else None
        self.model_name = llm_config.model if llm_config else "lexical-extractive-baseline"

    def validate_result(self, result: ConsultationResult) -> None:
        if self.selector and self.selector.config.generation_mode == "grounded":
            validate_generated_result(result, self.corpus)
        else:
            validate_result(result, self.corpus)

    async def consult(self, request: ConsultationRequest) -> ConsultationResult:
        started = time.perf_counter()
        timings = {"retrieval": 0.0, "rerank": 0.0, "generation": 0.0, "validation": 0.0}
        query = build_query(request)

        def result(kind: str, text: str, reason: str, evidence: list[Evidence] | None = None) -> ConsultationResult:
            candidate = ConsultationResult(
                kind=kind, answer_text=text, reason_code=reason, retrieval_query=query,
                evidence=evidence or [], timings_ms=timings.copy(),
                model_metadata={"model": self.model_name, "corpus_version": self.corpus_version, "mode": self.selector.config.generation_mode if self.selector else "extractive"},
            )
            validation_start = time.perf_counter()
            self.validate_result(candidate)
            candidate.timings_ms["validation"] = (time.perf_counter() - validation_start) * 1000
            candidate.timings_ms["total"] = (time.perf_counter() - started) * 1000
            return candidate

        latest = request.latest_user_text
        if INJECTION.search(latest):
            return result("insufficient_knowledge", SAFE_RESPONSES["injection"], "untrusted_instruction")
        if OUT_OF_SCOPE.search(latest):
            return result("out_of_scope", SAFE_RESPONSES["scope"], "outside_scope")
        if re.search(r"(?:вырос|увелич|больш)[^.!?]{0,30}(?:счет|счёт|расход)|(?:счет|счёт)[^.!?]{0,30}(?:вырос|увелич)", latest, re.I) and not re.search(r"квт|потреблен|тариф|ценов", latest, re.I):
            return result("clarify", SAFE_RESPONSES["bill"], "missing_material_context")
        if len(tokens(query)) < 2 or re.fullmatch(r"\s*(?:привет|здравствуйте|добрый день|помогите)[.!?\s]*", latest, re.I):
            return result("clarify", SAFE_RESPONSES["clarify"], "missing_material_context")
        if not self.ready:
            return result("insufficient_knowledge", SAFE_RESPONSES["corpus"], "corpus_unavailable")
        if request.corpus_version not in {"unavailable", self.corpus_version}:
            raise CorpusNotReady("The requested corpus version differs from the loaded index")
        retrieval_start = time.perf_counter()
        hits = self.retriever.search(query, limit=5)
        eligible = [hit for hit in hits if _eligible(hit, query)]
        timings["retrieval"] = (time.perf_counter() - retrieval_start) * 1000
        if _conflicting_prices(hits, query):
            return result("insufficient_knowledge", SAFE_RESPONSES["conflict"], "conflicting_evidence")
        if self.selector and self.selector.config.generation_mode == "grounded":
            # Generation gets broader candidates than the high-precision lexical
            # baseline. The model must abstain if their meaning is insufficient.
            candidates = [hit for hit in hits if not UNTRUSTED_INSTRUCTIONS.search(hit.chunk.text) and len(hit.chunk.text) <= 10000]
            generation_start = time.perf_counter()
            remaining = request.timeout_budget_ms - int((generation_start - started) * 1000)
            redacted = request.model_copy(update={"messages": [ContextMessage(id=message.id, role=message.role, text=redact_contacts(message.text)) for message in request.messages]})
            generated = await self.selector.generate(query, redacted, candidates, remaining)
            timings["generation"] = (time.perf_counter() - generation_start) * 1000
            if generated.kind != "answer":
                safe_text = {
                    "clarify": SAFE_RESPONSES["generated_clarify"],
                    "insufficient_knowledge": SAFE_RESPONSES["missing"],
                    "out_of_scope": SAFE_RESPONSES["generated_scope"],
                }[generated.kind]
                return result(generated.kind, safe_text, generated.reason_code)
            try:
                return result(generated.kind, generated.answer_text, generated.reason_code, generated.evidence)
            except InvalidEvidence:
                return result("insufficient_knowledge", SAFE_RESPONSES["invalid"], "invalid_generated_evidence")
        if not eligible:
            return result("insufficient_knowledge", SAFE_RESPONSES["baseline_missing"], "missing_evidence")
        if _conflicting_prices(eligible, query):
            return result("insufficient_knowledge", SAFE_RESPONSES["conflict"], "conflicting_evidence")
        if self.selector:
            generation_start = time.perf_counter()
            remaining = request.timeout_budget_ms - int((generation_start - started) * 1000)
            selected_ids = await self.selector.select(query, eligible, remaining)
            timings["generation"] = (time.perf_counter() - generation_start) * 1000
            eligible = [hit for hit in eligible if hit.chunk.chunk_id in selected_ids]
            if not eligible:
                return result("insufficient_knowledge", SAFE_RESPONSES["selector_missing"], "missing_evidence")
        selected = eligible[0]
        evidence = [Evidence(document_id=selected.chunk.document_id, chunk_id=selected.chunk.chunk_id, quote=selected.chunk.text, relevance_score=round(selected.score, 6))]
        return result("answer", render_excerpts([selected.chunk.text]), "grounded_excerpt", evidence)


def build_core(index_dir: str | Path, *, llm_config: LLMConfig | None = None) -> LocalConsultationCore:
    return LocalConsultationCore(index_dir, llm_config=llm_config)
