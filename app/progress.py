"""Public progress labels derived from the redacted customer request."""
import re

from .facts import METER_WORD, explicit_topics


RESOURCE_LABELS = {
    'electricity': 'Электроэнергия',
    'water': 'Вода',
    'heat': 'Тепло',
    'gas': 'Газ',
}
KEYWORD_PATTERNS = {
    'Счётчики': re.compile(rf'\b{METER_WORD}\b', re.I),
    'Тарифы': re.compile(r'\bтариф\w*\b', re.I),
}


def progress_keywords(latest_text: str, active_topic: str | None) -> list[str]:
    topics = explicit_topics(latest_text)
    if not topics and active_topic in RESOURCE_LABELS:
        topics.add(active_topic)
    return [label for topic, label in RESOURCE_LABELS.items() if topic in topics] + [
        label for label, pattern in KEYWORD_PATTERNS.items() if pattern.search(latest_text)
    ]
