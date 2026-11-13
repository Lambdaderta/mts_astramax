"""Validate user-visible answers against complete chunks from the loaded corpus."""
from __future__ import annotations

import re

from .corpus import Corpus
from .protocol import ConsultationResult, InvalidEvidence

MAX_ANSWER_WORDS = 150
MAX_EXCERPT_CHARACTERS = 6000

CLARIFY_RESPONSES = {
    "bill": "Что изменилось сильнее: потребление электроэнергии или сумма в счете?",
    "clarify": "Какую задачу нужно решить: разобраться с расходами, наладить учет или найти потери энергоресурсов?",
    "generated_clarify": "Какую задачу на вашем объекте нужно решить в первую очередь?",
    "resource": "Какой ресурс нужно учитывать: электричество, воду, тепло или газ?",
    "meter_model": "Какая модель счетчика указана на приборе?",
    "meter_count": "Сколько приборов учета нужно подключить?",
    "archive": "Какие данные учета у вас уже есть и за какой период?",
    "load": "В какое время суток потребление электроэнергии у вас самое высокое?",
    "site": "На каком объекте нужно решить эту задачу?",
    "tariff": "Какая ценовая категория указана в вашем счете?",
}

SAFE_RESPONSES = {
    **CLARIFY_RESPONSES,
    "injection": "Я отвечаю по предоставленным материалам. Могу помочь уточнить задачу или подготовить обращение специалисту.",
    "scope": "Настройка приборов и технические инструкции выходят за рамки консультации. Могу помочь подготовить обращение специалисту.",
    "corpus": "База знаний для продуктовой консультации пока не подключена. Могу помочь подготовить обращение специалисту.",
    "missing": "В найденных материалах недостаточно сведений для подтвержденного ответа. Предлагаю подготовить обращение специалисту.",
    "generated_scope": "Этот вопрос выходит за рамки консультации по продуктам Energytool. Могу помочь подготовить обращение специалисту.",
    "invalid": "Не удалось подтвердить все сведения найденными источниками. Предлагаю уточнить вопрос у специалиста.",
    "baseline_missing": "В доступной базе знаний не нашлось достаточно сведений для подтвержденного ответа. Могу подготовить обращение специалисту.",
    "conflict": "В материалах найдены различающиеся условия стоимости. Предлагаю уточнить применимые условия у специалиста.",
    "selector_missing": "Найденных материалов недостаточно для подтвержденного ответа. Предлагаю уточнить вопрос у специалиста.",
}

ANSWER_PREFIX = "В предоставленных материалах указано:\n\n"
ANSWER_SUFFIX = "\n\nМогу помочь уточнить, относится ли это к вашей задаче, или подготовить обращение специалисту."
UNTRUSTED_INSTRUCTIONS = re.compile(
    r"ignore\s+(?:all\s+)?(?:previous|system)|system\s*prompt|developer\s*message|"
    r"игнориру\w*\s+(?:все\s+)?(?:правил|инструкц)|системн\w*\s+(?:промпт|инструкц)|"
    r"<\s*/?(?:script|system|assistant|developer)\b", re.IGNORECASE,
)
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[«\"(]*[A-ZА-ЯЁ\d])")
_BLOCK_BREAK = re.compile(r"\n\s*\n|\n(?=[ \t]*(?:[-*+]\s|\d+[.)]\s))")
_TABLE_RULE = re.compile(r"(?m)^\s*(?:-{4,}|\|[-: ]{3,})(?:\s+-{3,}|\|[-: ]*)*\s*\|?$")
_CONDITION = re.compile(
    r"^(?:(?:[-*] |\d+\. )?\*{0,2})?(?:только\b|лишь\b|если\b|при\b|кроме\b|за исключением\b|"
    r"в случае\b|услови|ограничени|примечани|важно\b|иначе\b|не примен|не действ|без\b|\[\^?\d+\])",
    re.IGNORECASE,
)
_RUBLES = re.compile(r"(?<=\d)\s*(?:₽|руб(?:ль|ля|лей|\.)?)(?!\w)", re.IGNORECASE)
_GUARANTEE = re.compile(r"гарантир|гаранти\w*", re.IGNORECASE)
_ECONOMIC_RESULT = re.compile(r"эконом|снижен|окупаем", re.IGNORECASE)
_DENIED_GUARANTEE = re.compile(r"\b(?:не|без|нет)\s+(?:\w+\s+){0,2}гарантир|\b(?:нет|без)\s+гаранти", re.IGNORECASE)
_SAFE_CLOSINGS = {
    "могу помочь подготовить обращение специалисту",
    "предлагаю подготовить обращение специалисту",
    "могу помочь уточнить, относится ли это к вашей задаче, или подготовить обращение специалисту",
}


def render_excerpts(quotes: list[str]) -> str:
    return ANSWER_PREFIX + "\n\n".join("«" + quote + "»" for quote in quotes) + ANSWER_SUFFIX


def _safe_answer(text: str) -> None:
    if not text.strip() or len(text.split()) > MAX_ANSWER_WORDS:
        raise InvalidEvidence("Answer exceeds the consultation length limit or is empty")
    if UNTRUSTED_INSTRUCTIONS.search(text):
        raise InvalidEvidence("Answer contains instruction-like text")
    for sentence in _SENTENCE_BREAK.split(text):
        if _GUARANTEE.search(sentence) and _ECONOMIC_RESULT.search(sentence) and not _DENIED_GUARANTEE.search(sentence):
            raise InvalidEvidence("Answer promises a guaranteed economic result")


def _validated_quotes(result: ConsultationResult, corpus: Corpus | None) -> list[str]:
    _safe_answer(result.answer_text)
    if result.recommended_products or result.fact_updates:
        raise InvalidEvidence("Local core cannot propose products or customer facts")
    if result.kind != "answer":
        if result.answer_text not in SAFE_RESPONSES.values() or result.evidence:
            raise InvalidEvidence("Non-answer requires a vetted message and no citations")
        if result.kind == "clarify" and result.answer_text not in CLARIFY_RESPONSES.values():
            raise InvalidEvidence("Clarification requires a vetted question")
        return []
    if corpus is None or not 1 <= len(result.evidence) <= 3:
        raise InvalidEvidence("Factual answer requires one to three sources from the loaded corpus")
    quotes = []
    seen = set()
    for evidence in result.evidence:
        chunk = corpus.chunks_by_id.get(evidence.chunk_id)
        if chunk is None or chunk.document_id != evidence.document_id or chunk.text != evidence.quote:
            raise InvalidEvidence("Evidence must be the complete unmodified text of a known chunk")
        if UNTRUSTED_INSTRUCTIONS.search(chunk.text) or evidence.chunk_id in seen:
            raise InvalidEvidence("Untrusted or duplicate evidence")
        seen.add(evidence.chunk_id)
        quotes.append(evidence.quote)
    return quotes


def validate_result(result: ConsultationResult, corpus: Corpus | None) -> None:
    quotes = _validated_quotes(result, corpus)
    if quotes and result.answer_text != render_excerpts(quotes):
        raise InvalidEvidence("Extractive mode requires exact source excerpts")


def _normalize(text: str) -> str:
    text = _RUBLES.sub(" ₽", text)
    text = re.sub(r"(?<=\d)[\u00a0\u202f](?=\d)", " ", text)
    return " ".join(text.strip(' \t\r\n.«»\"').split())


def _source_units(quote: str) -> set[str]:
    """Keep adjacent standalone restrictions with the statement they qualify."""
    units = []
    for block in _BLOCK_BREAK.split(quote):
        segments = [block] if _TABLE_RULE.search(block) else _SENTENCE_BREAK.split(block)
        for segment in segments:
            if units and _CONDITION.match(segment.strip()):
                units[-1] += " " + segment
            elif segment.strip():
                units.append(segment)
    return {_normalize(unit) for unit in units}


def validate_generated_result(result: ConsultationResult, corpus: Corpus | None) -> None:
    """Allow verbatim claims and limited formatting changes, with a vetted closing."""
    quotes = _validated_quotes(result, corpus)
    if not quotes:
        return
    if _normalize(result.answer_text) in {_normalize(quote) for quote in quotes}:
        if len(_source_units(result.answer_text)) > 1:
            raise InvalidEvidence("Generated answer must select supporting statements, not reproduce a complete multi-statement chunk")
        return
    claims = {
        claim for claim in _source_units(result.answer_text.removeprefix(ANSWER_PREFIX))
        if claim.casefold() not in _SAFE_CLOSINGS
    }
    source_units = [_source_units(quote) for quote in quotes]
    if not claims or not claims.issubset(set().union(*source_units)):
        raise InvalidEvidence("Generated claim lacks matching text in its cited sources")
    if any(not units.intersection(claims) for units in source_units):
        raise InvalidEvidence("Generated answer cites evidence without using it")
    if any(len(units) > 1 and units.issubset(claims) for units in source_units):
        raise InvalidEvidence("Generated answer reproduces a complete multi-statement chunk")
