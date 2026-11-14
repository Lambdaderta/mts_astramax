"""Run a corpus-backed consultation with per-request conversation context."""
from __future__ import annotations

import re
import time
from decimal import Decimal
from pathlib import Path

from .corpus import Corpus
from .grounding import (
    CLARIFY_RESPONSES, MAX_ANSWER_WORDS, MAX_EXCERPT_CHARACTERS, SAFE_RESPONSES,
    UNTRUSTED_INSTRUCTIONS, is_verbatim_supported, render_excerpts, validate_generated_result, validate_result,
)
from .llm_client import EvidenceSelector, LLMConfig
from .embeddings import EmbeddingConfig, EmbeddingProvider, EmbeddingUnavailable, OpenAIEmbeddingClient
from .hybrid import HybridRetriever
from .retrieval_config import RetrievalConfig
from .protocol import (
    ConsultationRequest, ConsultationResult, ConsultationUnavailable, CorpusNotReady,
    Evidence, InvalidEvidence, InvalidModelOutput,
)
from .retriever import BM25Retriever, SearchHit, tokens

CONTACT_PATTERN = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\+\d[\d ()-]{5,}\d|(?<!\d)[78](?:[ ()-]*\d){10}(?!\d)")
OUT_OF_SCOPE = re.compile(
    r"прошив|пароль|схем\w*\s+подключ|техническ\w*\s+документац|"
    r"инструкци\w*\s+(?:для|по|к)\s+(?:счетчик|счётчик|прибор)|пошагов\w*\s+подключ|"
    r"настро(?:ить|йк|йт)\w*\s+(?:\w+\s+){0,2}(?:счетчик|счётчик|прибор)", re.I,
)
INJECTION = re.compile(r"игнориру|ignore\s+(?:all\s+)?(?:previous|system)|system\s*prompt|системн\w*\s+(?:промпт|инструкц)|(?:придум|выдум)\w*.*(?:цен|тариф|эконом)|(?:цен|тариф).*из\s+голов", re.I)
PRICE_QUERY = re.compile(r"\b(?:цен(?:а|ы|у|е|ой|ам|ах)?|стоимост\w*|стоить|руб\w*|окупаем\w*)\b|₽|\bсколько\s+стоит\b", re.I)
PRICE_SOURCE = re.compile(r"цен|стоим|рубл|₽|окупаем", re.I)
PRICE_AMOUNT = re.compile(r"(\d[\d \u00a0\u202f]*(?:[.,]\d+)?)\s*(тыс\.?|млн\.?|миллион\w*|тысяч\w*)?\s*(?:тестов\w*\s+)?(?:₽|руб\w*)", re.I)
COMPATIBILITY_QUERY = re.compile(r"совмест|поддержива|работа\w*\s+с\s", re.I)
BILL_CHANGE = re.compile(r"(?:вырос|увелич|больш)[^.!?]{0,30}(?:счет|счёт|расход)|(?:счет|счёт)[^.!?]{0,30}(?:вырос|увелич)", re.I)
BILL_DETAILS = re.compile(r"квт|потреблен|тариф|ценов", re.I)
FOLLOWUP = re.compile(
    r"^\s*(?:а\b|и\s+(?:ещ[её]|так|то)\b|кажется\b|примерно\b|исправляю\b)|"
    r"\b(?:эт[оа]|этого|этому|этим|этих|него|не[её]|они|так(?:ой|ая|ого|их|ие)|"
    r"с\s+чего\s+(?:нам\s+)?начать|для\s+оценки|вообще\s+стоит\s+проверить)\b", re.I,
)
DOCUMENT_DUMP = re.compile(
    r"\b(?:выгруз\w*|скопиру\w*|процитиру\w*|покаж\w*|вывед\w*|пришл\w*)\b[^.!?]{0,80}"
    r"(?:баз\w*\s+знаний|системн\w*\s+(?:промпт|инструкц)|(?:документ\w*|материал\w*)\s+целиком|весь\s+корпус)", re.I,
)


def redact_contacts(text: str) -> str:
    """Remove recognizable contacts even when the caller has no contact workflow."""
    def replace_contact(match: re.Match) -> str:
        suffix = text[match.end():match.end() + 30]
        quantity = re.fullmatch(r"\+?\d+(?: \d{3})*", match.group().strip())
        if quantity and re.match(r"\s*(?:[гмк]?вт|руб|₽|доллар|евро|%|процент|счетчик|счётчик|датчик|шт|лет|год|месяц)", suffix, re.I):
            return match.group()
        return "[контакт скрыт]"

    text = CONTACT_PATTERN.sub(replace_contact, text)
    text = re.sub(r"(?i)((?:телефон|позвоните|номер\s+телефона)\s*[:—-]?\s*)(?:\+?\d[\d ()-]{5,}\d)", r"\1[контакт скрыт]", text)
    return re.sub(r"(?i)(?:меня\s+зовут|фио\s*:)\s*[^.!?\n;]+", "[имя скрыто]", text)


def _query_parts(request: ConsultationRequest) -> tuple[str, str]:
    latest = redact_contacts(request.latest_user_text).strip()[:2500]
    if len(tokens(latest)) > 3 and not FOLLOWUP.search(latest):
        return latest, ""
    prior = []
    for message in reversed(request.messages):
        if message.role != "user" or message.id == request.user_message_id:
            continue
        text = redact_contacts(message.text).strip()
        if len(tokens(text)) >= 3:
            prior.append(text[:400])
        if len(prior) == 2:
            break
    return latest, "\n".join(reversed(prior))


def build_query(request: ConsultationRequest) -> str:
    latest, context = _query_parts(request)
    return f"{latest}\nКонтекст:\n{context}" if context else latest


def _eligible(hit: SearchHit, query: str) -> bool:
    query_tokens = set(tokens(query))
    if hit.overlap < min(2, len(query_tokens)) or hit.coverage < 0.75:
        return False
    if UNTRUSTED_INSTRUCTIONS.search(hit.chunk.text):
        return False
    excerpt = render_excerpts([hit.chunk.text])
    if len(excerpt.split()) > MAX_ANSWER_WORDS or len(hit.chunk.text) > MAX_EXCERPT_CHARACTERS:
        return False
    if PRICE_QUERY.search(query) and not PRICE_SOURCE.search(hit.chunk.text):
        return False
    if COMPATIBILITY_QUERY.search(query) and not COMPATIBILITY_QUERY.search(hit.chunk.text):
        return False
    return True


def _conflicting_prices(hits: list[SearchHit], query: str, context: str) -> bool:
    if not PRICE_QUERY.search(query):
        return False
    price_terms = set(tokens("стоимость цена ценой стоит рублей окупаемость тариф"))
    focus = set(tokens(query)) - price_terms
    if not focus:
        # Resolve a price-only follow-up against specific terms from the last turn.
        context_terms = set(tokens(context)) - price_terms
        if not context_terms:
            return False
        strongest_match = max((len(context_terms.intersection(tokens(hit.chunk.text))) for hit in hits), default=0)
        if strongest_match < 2:
            return False
    amounts = set()
    for hit in hits:
        source_terms = set(tokens(hit.chunk.title + " " + hit.chunk.text))
        if focus and not focus.issubset(source_terms):
            continue
        if not focus and len(context_terms.intersection(tokens(hit.chunk.text))) < strongest_match:
            continue
        price_set = set()
        for value, magnitude in PRICE_AMOUNT.findall(hit.chunk.text):
            number = Decimal(re.sub(r"\s+", "", value).replace(",", "."))
            if magnitude.lower().startswith("тыс"):
                number *= 1000
            elif magnitude:
                number *= 1000000
            price_set.add(number)
        if price_set:
            amounts.add(frozenset(price_set))
    return len(amounts) > 1


class LocalConsultationCore:
    def __init__(self, index_dir: str | Path, *, llm_config: LLMConfig | None = None,
                 retrieval_config: RetrievalConfig | None = None,
                 embedding_provider: EmbeddingProvider | None = None):
        try:
            self.corpus: Corpus | None = Corpus.load(index_dir)
        except CorpusNotReady:
            self.corpus = None
        self.ready = self.corpus is not None
        self.corpus_version = self.corpus.version if self.corpus else "unavailable"
        self.retriever = BM25Retriever(self.corpus) if self.corpus else None
        self.retrieval_config = retrieval_config or RetrievalConfig()
        self.hybrid = HybridRetriever(
            self.corpus, embedding_provider, cache_dir=self.retrieval_config.cache_dir,
            candidate_pool=self.retrieval_config.candidate_pool, rrf_k=self.retrieval_config.rrf_k,
            lexical_weight=self.retrieval_config.lexical_weight, dense_weight=self.retrieval_config.dense_weight,
        ) if self.corpus and self.retrieval_config.mode == "hybrid" else None
        self._prepare_failure: str | None = None
        self.selector = EvidenceSelector(llm_config) if llm_config else None
        self.model_name = llm_config.model if llm_config else "lexical-extractive-baseline"

    async def prepare_retrieval(self, *, timeout_budget_ms: int = 120000) -> None:
        """Build the shared dense index once, before the submission worker pool."""
        if self.hybrid is None:
            return
        try:
            await self.hybrid.prepare(timeout_budget_ms=timeout_budget_ms)
        except EmbeddingUnavailable as error:
            # Do not repeat a failed full-corpus API call for every test question.
            self._prepare_failure = error.code

    def validate_result(self, result: ConsultationResult) -> None:
        if self.selector and self.selector.config.generation_mode == "grounded":
            validate_generated_result(result, self.corpus)
        else:
            validate_result(result, self.corpus)

    async def consult(self, request: ConsultationRequest) -> ConsultationResult:
        started = time.perf_counter()
        timings = dict.fromkeys(("retrieval", "rerank", "generation", "review", "validation"), 0.0)
        latest, context = _query_parts(request)
        query = f"{latest}\nКонтекст:\n{context}" if context else latest
        mode = self.selector.config.generation_mode if self.selector else "extractive"
        retrieval_metadata = {"retrieval_mode": "not_run"}

        def result(kind: str, text: str, reason: str, evidence: list[Evidence] | None = None) -> ConsultationResult:
            candidate = ConsultationResult(
                kind=kind, answer_text=text, reason_code=reason, retrieval_query=query,
                evidence=evidence or [], timings_ms=timings.copy(),
                model_metadata={"model": self.model_name, "corpus_version": self.corpus_version, "mode": mode,
                                **retrieval_metadata},
            )
            validation_start = time.perf_counter()
            try:
                self.validate_result(candidate)
            except InvalidEvidence:
                candidate = ConsultationResult(
                    kind="insufficient_knowledge", answer_text=SAFE_RESPONSES["invalid"],
                    reason_code="invalid_generated_evidence" if mode == "grounded" else "invalid_evidence",
                    retrieval_query=query, timings_ms=timings.copy(), model_metadata=candidate.model_metadata,
                )
                self.validate_result(candidate)
            candidate.timings_ms["validation"] = (time.perf_counter() - validation_start) * 1000
            candidate.timings_ms["total"] = (time.perf_counter() - started) * 1000
            return candidate

        if INJECTION.search(latest) or DOCUMENT_DUMP.search(latest):
            return result("insufficient_knowledge", SAFE_RESPONSES["injection"], "untrusted_instruction")
        if mode != "grounded" and OUT_OF_SCOPE.search(latest):
            return result("out_of_scope", SAFE_RESPONSES["scope"], "outside_scope")
        if mode != "grounded" and BILL_CHANGE.search(latest) and not BILL_DETAILS.search(latest + " " + context):
            return result("clarify", SAFE_RESPONSES["bill"], "missing_material_context")
        if not tokens(latest) or (len(tokens(latest)) < 2 and not context) or re.fullmatch(r"\s*(?:привет|здравствуйте|добрый день|помогите)[.!?\s]*", latest, re.I):
            return result("clarify", SAFE_RESPONSES["clarify"], "missing_material_context")
        if self.retriever is None:
            return result("insufficient_knowledge", SAFE_RESPONSES["corpus"], "corpus_unavailable")
        if request.corpus_version not in {"unavailable", self.corpus_version}:
            raise CorpusNotReady("The requested corpus version differs from the loaded index")

        retrieval_start = time.perf_counter()
        if self.hybrid and not self._prepare_failure:
            remaining_ms = request.timeout_budget_ms - (retrieval_start - started) * 1000
            # Keep most of a short web turn's budget for answer generation.
            retrieval_budget = max(1, int(min(self.retrieval_config.timeout_seconds * 1000, remaining_ms * 0.4)))
            searched = await self.hybrid.search(
                latest, context=context, limit=self.retrieval_config.candidate_pool,
                timeout_budget_ms=retrieval_budget,
            )
            hits = searched.hits
            retrieval_metadata["retrieval_mode"] = searched.mode
            retrieval_metadata["embedding_model"] = searched.embedding_model or "none"
            if searched.fallback_reason:
                retrieval_metadata["retrieval_fallback"] = searched.fallback_reason
            timings.update({"retrieval_" + name: value for name, value in searched.timings_ms.items()})
        else:
            hits = self.retriever.search(latest, context=context, limit=self.retrieval_config.candidate_pool)
            retrieval_metadata["retrieval_mode"] = "lexical_fallback" if self._prepare_failure else "bm25"
            if self._prepare_failure:
                retrieval_metadata["retrieval_fallback"] = self._prepare_failure
        candidates = [
            hit for hit in hits if len(hit.chunk.text) <= 10000
            and not UNTRUSTED_INSTRUCTIONS.search(hit.chunk.title + "\n" + hit.chunk.text)
        ]
        timings["retrieval"] = (time.perf_counter() - retrieval_start) * 1000
        potential_price_conflict = _conflicting_prices(candidates, latest, context)
        if mode != "grounded" and potential_price_conflict:
            return result("insufficient_knowledge", SAFE_RESPONSES["conflict"], "conflicting_evidence")

        if self.selector and mode == "grounded":
            generation_start = time.perf_counter()
            remaining = request.timeout_budget_ms - (generation_start - started) * 1000
            selected = candidates[:self.retrieval_config.top_k]
            if not selected:
                return result("insufficient_knowledge", SAFE_RESPONSES["missing"], "missing_evidence")
            generated = None
            # Reserve time for one source review or structured-output recovery.
            # No extra retrieval, model or unbounded repair loop is introduced.
            try:
                generated = await self.selector.generate(query, request, selected, remaining * 0.8)
            except ConsultationUnavailable:
                retrieval_metadata["initial_output_error"] = "model_unavailable"
            except InvalidModelOutput as error:
                retrieval_metadata["initial_output_error"] = error.code
            except InvalidEvidence:
                retrieval_metadata["initial_output_error"] = "invalid_generated_evidence"
            timings["generation"] = (time.perf_counter() - generation_start) * 1000
            direct = False
            if generated is not None and generated.kind == "answer":
                try:
                    direct = is_verbatim_supported(generated, self.corpus) and not potential_price_conflict
                except InvalidEvidence:
                    pass  # The source reviewer can correct an invalid draft.
            if direct:
                retrieval_metadata["answer_review"] = "verbatim"
            else:
                review_start = time.perf_counter()
                remaining = request.timeout_budget_ms - (review_start - started) * 1000
                review_failure = None
                try:
                    reviewed = await self.selector.review(query, request, selected, remaining, draft=generated)
                    if reviewed.kind == "answer":
                        self.validate_result(reviewed)
                except ConsultationUnavailable:
                    retrieval_metadata["answer_review"] = "unavailable"
                    review_failure = "answer_review_unavailable"
                except InvalidModelOutput as error:
                    retrieval_metadata["answer_review"] = "invalid"
                    review_failure = error.code
                except InvalidEvidence:
                    retrieval_metadata["answer_review"] = "invalid"
                    review_failure = "answer_review_invalid"
                finally:
                    timings["review"] = (time.perf_counter() - review_start) * 1000
                if review_failure:
                    return result("insufficient_knowledge", SAFE_RESPONSES["invalid"], review_failure)
                retrieval_metadata["answer_review"] = "recovered" if generated is None else "corrected_or_confirmed"
                generated = reviewed
            if generated.kind == "answer":
                return result("answer", generated.answer_text, generated.reason_code, generated.evidence)
            if generated.kind == "clarify":
                question = CLARIFY_RESPONSES.get(generated.answer_text, CLARIFY_RESPONSES["generated_clarify"])
                if generated.answer_text in CLARIFY_RESPONSES.values():
                    question = generated.answer_text
                if any(message.role == "assistant" and message.text == question for message in request.messages[-5:]):
                    return result("insufficient_knowledge", SAFE_RESPONSES["missing"], "missing_evidence")
                return result("clarify", question, generated.reason_code)
            response = "missing" if generated.kind == "insufficient_knowledge" else "generated_scope"
            return result(generated.kind, SAFE_RESPONSES[response], generated.reason_code)

        eligible = [hit for hit in candidates if _eligible(hit, latest)]
        if context:
            context_terms = set(tokens(context)) - set(tokens(latest))
            eligible = [hit for hit in eligible if context_terms.intersection(tokens(hit.chunk.title + " " + hit.chunk.text))]
        if not eligible:
            return result("insufficient_knowledge", SAFE_RESPONSES["baseline_missing"], "missing_evidence")
        if self.selector:
            generation_start = time.perf_counter()
            remaining = request.timeout_budget_ms - (generation_start - started) * 1000
            try:
                selected_ids = await self.selector.select(query, eligible[:5], remaining)
            except InvalidEvidence:
                timings["generation"] = (time.perf_counter() - generation_start) * 1000
                return result("insufficient_knowledge", SAFE_RESPONSES["invalid"], "invalid_evidence")
            timings["generation"] = (time.perf_counter() - generation_start) * 1000
            eligible = [hit for hit in eligible if hit.chunk.chunk_id in selected_ids]
            if not eligible:
                return result("insufficient_knowledge", SAFE_RESPONSES["selector_missing"], "missing_evidence")
        selected = eligible[0]
        evidence = [Evidence(document_id=selected.chunk.document_id, chunk_id=selected.chunk.chunk_id, quote=selected.chunk.text, relevance_score=round(selected.score, 6))]
        return result("answer", render_excerpts([selected.chunk.text]), "grounded_excerpt", evidence)


def build_core(index_dir: str | Path, *, llm_config: LLMConfig | None = None,
               platform: bool = False) -> LocalConsultationCore:
    retrieval = RetrievalConfig.from_env(enabled=llm_config is not None)
    provider = OpenAIEmbeddingClient(EmbeddingConfig.from_env(platform=platform, llm_config=llm_config)) if retrieval.mode == "hybrid" else None
    return LocalConsultationCore(index_dir, llm_config=llm_config, retrieval_config=retrieval, embedding_provider=provider)
