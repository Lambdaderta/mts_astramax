"""Dependency-free lexical BM25 baseline; scores are not confidence values."""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from .corpus import Chunk, Corpus

STOPWORDS = set("а и но или в во на по за для из к ко от с со у о об это этот эта эти как что чем ли бы был была были быть есть не нет же уже мне меня нас нам вы вам он она они его ее их мы я твой ваш нужно нужен нужна можно хочу пожалуйста подскажите расскажите про так то где когда почему какой какая какие сколько чем еще также".split())


def _stem(word: str) -> str:
    endings = r"(?:иями|ями|ами|ого|ему|ому|ыми|ими|ий|ый|ой|ая|яя|ое|ее|ые|ие|ов|ев|ей|ам|ям|ах|ях|ом|ем|ую|юю|ть|ет|ют|ит|ат|ят|у|ю|а|я|ы|и|е|о|ь)$"
    stem = re.sub(endings, "", word)
    return stem if len(stem) >= 3 else word


def tokens(text: str) -> list[str]:
    # Lightweight morphology and generic synonym expansion. These are retrieval
    # heuristics, not product facts or a confidence model.
    result = []
    aliases = {
        "крыша": "кровл", "крыше": "кровл", "крыши": "кровл", "крышу": "кровл",
        "фото": "фото", "фотографии": "фото", "фотография": "фото", "фотографий": "фото",
        "цена": "стоимост", "цену": "стоимост", "цены": "стоимост", "стоит": "стоимост",
        "симки": "сим", "симка": "сим", "сим": "сим", "sim": "сим",
        "электричество": "электроэнерг", "электричества": "электроэнерг", "электроэнергия": "электроэнерг", "электроэнергии": "электроэнерг",
    }
    for word in re.findall(r"[a-zа-яё0-9]+", text.lower().replace("ё", "е")):
        if word in STOPWORDS or word in {"пользователь", "ассистент"} or len(word) < 2:
            continue
        term = aliases.get(word, _stem(word) if re.fullmatch(r"[а-я]+", word) else word)
        if term.startswith(("наращ", "расшир")): term = "расшир"
        result.append(term)
        if term == "фасад": result.extend(["вертикальн", "поверхност"])
    return result


@dataclass(frozen=True)
class SearchHit:
    chunk: Chunk
    score: float
    overlap: int
    coverage: float


class BM25Retriever:
    def __init__(self, corpus: Corpus):
        self.corpus = corpus
        self.frequencies = [Counter(tokens(chunk.title + " " + chunk.text)) for chunk in corpus.chunks]
        self.lengths = [sum(freq.values()) for freq in self.frequencies]
        self.average_length = sum(self.lengths) / max(len(self.lengths), 1)
        self.document_frequency = Counter(term for freq in self.frequencies for term in freq)

    def search(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        query_tokens = set(tokens(query))
        if not query_tokens:
            return []
        results = []
        total = len(self.frequencies)
        for chunk, frequencies, length in zip(self.corpus.chunks, self.frequencies, self.lengths):
            score, overlap = 0.0, 0
            for term in query_tokens:
                count = frequencies.get(term, 0)
                if not count:
                    continue
                overlap += 1
                frequency = self.document_frequency[term]
                idf = math.log(1 + (total - frequency + 0.5) / (frequency + 0.5))
                score += idf * count * 2.5 / (count + 1.5 * (0.25 + 0.75 * length / max(self.average_length, 1)))
            if score:
                results.append(SearchHit(chunk, score, overlap, overlap / len(query_tokens)))
        return sorted(results, key=lambda item: (-item.score, item.chunk.chunk_id))[:limit]
