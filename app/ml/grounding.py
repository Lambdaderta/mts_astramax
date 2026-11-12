"""Conservative claim gate for the extractive baseline.

A citation alone does not prove entailment. This implementation permits only
verbatim source excerpts inside fixed framing, so generated claims cannot pass.
Semantic relevance and source completeness still require corpus evaluation.
"""
from __future__ import annotations

import re
from decimal import Decimal

from .corpus import Corpus
from .protocol import ConsultationResult, InvalidEvidence

SAFE_RESPONSES = {
    'injection': 'Я отвечаю только по предоставленным материалам и не придумываю цены или характеристики. Уточните задачу, либо подготовим обращение специалисту.',
    'scope': 'Настройка приборов и технические инструкции выходят за рамки этой консультации. Могу помочь сформулировать задачу и подготовить обращение специалисту.',
    'bill': 'Уточните, пожалуйста: выросло потребление электроэнергии или увеличилась стоимость при том же объеме?',
    'clarify': 'Какую задачу нужно решить: разобраться с расходами, наладить учет или найти потери энергоресурсов?',
    'corpus': 'База знаний для продуктовой консультации пока не подключена. Я не могу подтвердить характеристики или цены. Могу помочь подготовить обращение специалисту.',
    'generated_clarify': 'Уточните, пожалуйста, какую задачу на вашем объекте нужно решить в первую очередь?',
    'missing': 'В найденных материалах недостаточно сведений для подтвержденного ответа. Предлагаю подготовить обращение специалисту.',
    'generated_scope': 'Этот вопрос выходит за рамки консультации по продуктам Energytool. Могу помочь подготовить обращение специалисту.',
    'invalid': 'Не удалось подтвердить все сведения найденными источниками. Чтобы не вводить вас в заблуждение, предлагаю уточнить вопрос у специалиста.',
    'baseline_missing': 'В доступной базе знаний не нашлось достаточно сведений для подтвержденного ответа на этот вопрос. Могу подготовить обращение специалисту.',
    'conflict': 'В материалах найдены различающиеся условия стоимости. Без уточнения применимых условий я не могу назвать цену. Предлагаю передать вопрос специалисту.',
    'selector_missing': 'Найденных материалов недостаточно для подтвержденного ответа. Предлагаю уточнить вопрос у специалиста.',
}

ANSWER_PREFIX = "В предоставленных материалах указано:\n\n"
ANSWER_SUFFIX = "\n\nМогу помочь уточнить, относится ли это к вашей задаче, или подготовить обращение специалисту."
UNTRUSTED_INSTRUCTIONS = re.compile(
    r"ignore\s+(?:all\s+)?(?:previous|system)|system\s*prompt|developer\s*message|"
    r"игнориру\w*\s+(?:все\s+)?(?:правил|инструкц)|системн\w*\s+(?:промпт|инструкц)|"
    r"<\s*/?(?:script|system|assistant|developer)\b", re.IGNORECASE,
)


def render_excerpts(quotes: list[str]) -> str:
    return ANSWER_PREFIX + "\n\n".join("«" + quote + "»" for quote in quotes) + ANSWER_SUFFIX


def validate_result(result: ConsultationResult, corpus: Corpus | None) -> None:
    if result.kind != "answer":
        if result.answer_text not in SAFE_RESPONSES.values():
            raise InvalidEvidence("Non-answer must use a local vetted message")
        if result.evidence or result.recommended_products:
            raise InvalidEvidence("Non-answer cannot claim product evidence or recommendations")
        return
    if corpus is None or not result.evidence:
        raise InvalidEvidence("Factual answer requires a loaded corpus and evidence")
    chunks = corpus.chunks_by_id
    seen = set()
    for evidence in result.evidence:
        chunk = chunks.get(evidence.chunk_id)
        if chunk is None or chunk.document_id != evidence.document_id:
            raise InvalidEvidence("Unknown document/chunk relationship")
        # Full chunks preserve adjacent restrictions, unlike selective subquotes.
        if evidence.quote != chunk.text:
            raise InvalidEvidence("Quote must be the complete unmodified source chunk")
        if UNTRUSTED_INSTRUCTIONS.search(evidence.quote):
            raise InvalidEvidence("Instruction-like source text is quarantined")
        if evidence.chunk_id in seen:
            raise InvalidEvidence("Duplicate evidence")
        seen.add(evidence.chunk_id)
    if result.answer_text != render_excerpts([item.quote for item in result.evidence]):
        raise InvalidEvidence("Extractive mode rejects unsupported generated prose")
    if result.recommended_products:
        raise InvalidEvidence("Baseline does not infer product applicability")
    if result.fact_updates:
        raise InvalidEvidence("Baseline does not infer customer facts")


def validate_generated_result(result: ConsultationResult, corpus: Corpus | None) -> None:
    """Structural/lexical checks for opt-in generation, not entailment proof.

Unsupported numbers are rejected conservatively, including echoed user values
unless those values also occur in cited sources. Corpus-backed QA is still needed
for paraphrase correctness, omitted conditions, and changed negations.
"""
    if result.kind != "answer":
        if result.answer_text not in SAFE_RESPONSES.values():
            raise InvalidEvidence("Non-answer must use a local vetted message")
        if result.evidence or result.recommended_products or result.fact_updates:
            raise InvalidEvidence("Non-answer cannot include factual claims")
        return
    if corpus is None or not result.evidence or len(result.evidence) > 3:
        raise InvalidEvidence("Generated answer needs one to three real sources")
    chunks = corpus.chunks_by_id
    seen = set()
    for evidence in result.evidence:
        chunk = chunks.get(evidence.chunk_id)
        if chunk is None or evidence.document_id != chunk.document_id or evidence.quote != chunk.text:
            raise InvalidEvidence("Generated answer references invalid evidence")
        if UNTRUSTED_INSTRUCTIONS.search(chunk.text) or evidence.chunk_id in seen:
            raise InvalidEvidence("Untrusted or duplicate evidence")
        seen.add(evidence.chunk_id)
    source = "\n".join(item.quote for item in result.evidence)
    if not _numbers(result.answer_text).issubset(_numbers(source)):
        raise InvalidEvidence("Generated answer contains an unsupported number")
    if not _quantities(result.answer_text).issubset(_quantities(source)):
        raise InvalidEvidence("Generated answer changes a numeric scale, currency or unit")
    for sentence in re.split(r"[.!?\n]", result.answer_text):
        if re.search(r"гарантир", sentence, re.I) and re.search(r"эконом|снижен|окупаем", sentence, re.I):
            # Even a sourced economic guarantee is not promised by this baseline.
            # Explicitly negative statements remain useful and safe.
            if not re.search(r"\bне\s+(?:\w+\s+){0,2}гарантир", sentence, re.I):
                raise InvalidEvidence("Generated answer promises a guaranteed economic result")
    if result.recommended_products or result.fact_updates:
        raise InvalidEvidence("Model cannot mutate products or customer facts in this baseline")
    if UNTRUSTED_INSTRUCTIONS.search(result.answer_text):
        raise InvalidEvidence("Generated answer contains instruction-like material")


_NUMBER = r"\d(?:[\d \u00a0\u202f]*\d)?(?:[.,]\d+)?"
_SCALE = r"тыс(?:яч[аиу]?)?\.?|миллион\w*|млн\.?|миллиард\w*|млрд\.?"
_UNIT = (r"%|процент\w*|руб\w*|р\.(?=\s|$)|₽|доллар\w*|usd|\$|евро|eur|€|"
         r"[гмк]?вт(?:[·*⋅-]?\s*ч)?|[гмк]?ва(?:р)?|(?:гига|мега|кило)?ватт\w*(?:[- ]час\w*)?|лет|год\w*|месяц\w*|дн(?:ей|я)|час\w*|"
         r"счетчик\w*|счётчик\w*|датчик\w*|объект\w*|панел\w*|штук\w*|м[²2³3]|метр\w*")


def _decimal(text: str) -> Decimal:
    return Decimal(re.sub(r"[\s\u00a0\u202f]", "", text).replace(",", "."))


def _numbers(text: str) -> set[Decimal]:
    return {_decimal(value) for value in re.findall(_NUMBER, text)}


def _quantities(text: str) -> set[tuple[Decimal, str]]:
    """Normalize explicit number/scale/unit phrases, conservatively.

    Numeric equivalence does not establish semantic equivalence; this prevents
    common price, scale and unit substitutions while other gates check sources.
    """
    pattern = rf"({_NUMBER})\s*({_SCALE})?\s*(?:тестов\w*\s+)?({_UNIT})(?![а-яa-z])"
    quantities = set()
    for number, magnitude, unit in re.findall(pattern, text.lower()):
        value = _decimal(number)
        if magnitude.startswith(("тыс",)):
            value *= 1000
        elif magnitude.startswith(("миллион", "млн")):
            value *= 1000000
        elif magnitude.startswith(("миллиард", "млрд")):
            value *= 1000000000
        if unit.startswith("процент") or unit == "%": unit = "%"
        elif unit.startswith("руб") or unit in {"₽", "р."}: unit = "RUB"
        elif unit.startswith("доллар") or unit in {"usd", "$"}: unit = "USD"
        elif unit in {"евро", "eur", "€"}: unit = "EUR"
        elif re.match(r"(?:гига|мега|кило)?ватт", unit):
            for prefix, multiplier in [("гига", 1000000000), ("мега", 1000000), ("кило", 1000)]:
                if unit.startswith(prefix):
                    value *= multiplier
                    break
            unit = "втч" if "час" in unit else "вт"
        elif re.match(r"[гмк]?в[та]", unit):
            if unit[0] in {"г", "м", "к"}:
                value *= {"г": 1000000000, "м": 1000000, "к": 1000}[unit[0]]
                unit = unit[1:]
            unit = re.sub(r"[·*⋅\s-]", "", unit)
        elif unit.startswith("год") or unit == "лет": unit = "year"
        elif unit.startswith("месяц"): unit = "month"
        elif unit.startswith("дн"): unit = "day"
        elif unit.startswith("час"): unit = "hour"
        elif unit.startswith(("счетчик", "счётчик")): unit = "meter_device"
        elif unit.startswith("датчик"): unit = "sensor"
        elif unit.startswith("объект"): unit = "site"
        elif unit.startswith("панел"): unit = "panel"
        elif unit.startswith("штук"): unit = "piece"
        quantities.add((value, unit))
    # A scale without an explicit unit still cannot be silently introduced.
    for number, magnitude in re.findall(rf"({_NUMBER})\s*({_SCALE})(?![а-яa-z])", text.lower()):
        value = _decimal(number)
        multiplier = 1000 if magnitude.startswith("тыс") else 1000000000 if magnitude.startswith(("миллиард", "млрд")) else 1000000
        quantities.add((value * multiplier, "scaled_number"))
    return quantities
