"""Public progress labels derived locally from redacted conversation text."""
import re
from typing import Literal

from .facts import METER_WORD
from .privacy import lead_context


ProgressKeyword = Literal[
    'Электроэнергия', 'Вода', 'Тепло', 'Газ', 'Счётчики', 'Тарифы',
    'Рост расходов', 'Потери ресурсов', 'Утечки', 'Контроль потребления',
    'Передача показаний', 'Показания', 'Совместимость оборудования', 'Поверка счётчиков',
    'Отчёты и аналитика', 'Интеграция', 'Оповещения', 'Экономия', 'Стоимость', 'Подбор решения',
]
MAX_PROGRESS_KEYWORDS = 6

BILL = r'сч[её]т(?:а|у|е|ом|ы|ов|ам|ами|ах)?|плат[её]ж\w*|расход\w*|затрат\w*'
INCREASE = r'вырос\w*|раст[её]\w*|увелич\w*|подорож\w*|рост|повыш\w*'
READINGS = r'показани(?:е|я|й|ям|ями|ях|ю|ем)'
METER = rf'{METER_WORD}|(?:водо|электро|тепло)сч[её]тчик\w*|водомер\w*|расходомер\w*'
EQUIPMENT = rf'{METER}|датчик\w*|оборудовани\w*'
COMPATIBILITY = r'подойд(?:[её]|у)т|подход\w*|поддержива\w*|modbus|lorawan|m-bus|rs-?485'
LOSS = r'потер(?:и|ь|ю|ей|ям|ями|ях)'
LOSS_RESOURCE = r'электр\w*|энерги\w*|ресурс\w*|вод[аеуы]|газ(?:а|у)?|тепл[оа]'


def paired(first: str, second: str) -> str:
    """Match two signals in either order within the same clause."""
    gap = r'\b[^.!?;\n]{0,60}\b'
    return rf'(?:{first}){gap}(?:{second})|(?:{second}){gap}(?:{first})'


# The progress vocabulary includes colloquial terms and device names. It is
# independent of the stricter resource detection used to persist measurements.
RESOURCE_PATTERNS: dict[ProgressKeyword, re.Pattern[str]] = {
    'Электроэнергия': re.compile(
        rf'\b(?:электроэнерг\w*|электричеств\w*|электрическ\w*|электроснабжен\w*|электросет\w*|'
        rf'электросч[её]тчик\w*|[кмг]вт(?:\s*[-·⋅*]?\s*ч)?|(?:{BILL}|плат\w*|тариф\w*)\s+за\s+свет)\b', re.I,
    ),
    'Вода': re.compile(r'\b(?:вод(?:а|е|у|ы|ой|ою|ам|ами|ах)?|водоснабжен\w*|водоотведен\w*|'
                       r'водомер\w*|водосч[её]тчик\w*|гвс|хвс)\b', re.I),
    'Тепло': re.compile(r'\b(?:тепл(?:о|а|у|ом|е)|тепло(?:снабжен|потреблен|сч[её]тчик|энерг)\w*|'
                        r'теплов\w*|отоплен\w*|гкал)\b', re.I),
    'Газ': re.compile(r'\b(?:газ(?:а|ом|у|е|ов)?|газов\w*|газоснабжен\w*)\b', re.I),
}

INTENT_TERMS: dict[ProgressKeyword, str] = {
    'Рост расходов': paired(BILL, INCREASE),
    'Потери ресурсов': rf'{LOSS}\s+(?:{LOSS_RESOURCE}|в\s+(?:электрическ\w*\s+)?сет\w*)|'
                       rf'(?:технологическ\w*|теплов\w*)\s+{LOSS}|куда\s+уход(?:ит|ят)\s+(?:{LOSS_RESOURCE})',
    'Утечки': r'(?:утеч\w*|протеч\w*)(?!\s+(?:данных|информации)\b)',
    'Контроль потребления': paired(r'потреблен\w*|расход\w*', r'контрол\w*|отслеж\w*|монитор\w*|график\w*'),
    'Передача показаний': paired(READINGS, r'переда\w*|отправ\w*|сда(?:ть|вать|ча|чи|че|чу)'),
    'Совместимость оборудования': rf'совместим\w*|{paired(EQUIPMENT, COMPATIBILITY)}',
    'Поверка счётчиков': r'поверк\w*|поверить|межповерочн\w*',
    'Отчёты и аналитика': r'отч[её]т(?:а|у|ом|е|ы|ов|ам|ами|ах|ност\w*)?|аналитик\w*|дашборд\w*',
    'Интеграция': r'интегр\w*|синхрониз\w*|api|1[сc]',
    'Оповещения': r'уведомлен\w*|оповещен\w*|оповещать|предупрежден\w*',
    'Экономия': r'экономи(?:я|и|ю|ей|ть|м)|сэконом\w*|окупаем\w*',
    'Стоимость': r'стоимост\w*|сколько\s+(?:это\s+)?сто(?:ит|ят)|цен(?:а|ы|у|е|ой|ам|ами|ах)|расценк\w*',
    'Подбор решения': r'(?:подобр\w*|подбор|посовет\w*|како[ей]|какую)\s+(?:решени\w*|систем\w*|оборудовани\w*)',
    'Показания': READINGS,
    'Счётчики': METER,
    'Тарифы': r'тариф\w*',
}
INTENT_PATTERNS = {label: re.compile(rf'\b(?:{pattern})\b', re.I) for label, pattern in INTENT_TERMS.items()}
CONTEXT_REFERENCE = re.compile(
    r'^(?:(?:а|и)\s+)?(?:сколько\s+(?:это\s+)?сто(?:ит|ят)$|'
    r'(?:сколько|как|где|когда|можно|подойд[её]т)\b[^.!?;\n]{0,60}\b'
    r'(?:это|для\s+него|для\s+не[её]|с\s+этим(?:и)?)\b|'
    r'(?:тогда|для\s+этого|исправляю|поправка|уточняю)\b)', re.I,
)


def resource_labels(text: str) -> list[ProgressKeyword]:
    return [label for label, pattern in RESOURCE_PATTERNS.items() if pattern.search(text)]


def progress_keywords(messages: list[dict[str, str]]) -> list[ProgressKeyword]:
    text = lead_context(messages[-1]['text'])
    resources = resource_labels(text)
    intents = [label for label, pattern in INTENT_PATTERNS.items() if pattern.search(text)]
    if 'Передача показаний' in intents and 'Показания' in intents:
        intents.remove('Показания')

    if not resources and intents and CONTEXT_REFERENCE.search(text):
        for message in reversed(messages[:-1]):
            if message['role'] == 'user' and (previous := lead_context(message['text'])):
                previous_resources = resource_labels(previous)
                if len(previous_resources) == 1:
                    resources = previous_resources
                break
    return (resources + intents)[:MAX_PROGRESS_KEYWORDS]
