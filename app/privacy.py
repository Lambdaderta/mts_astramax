"""Deterministic contact handling for the lead form and consultation boundary."""
import re

PHONE_LABEL = (
    r'(?:(?:мой|наш|новый)[ \t]+)*(?:номер[ \t]+телефона|'
    r'мобильный[ \t]+телефон|телефон(?:[ \t]+для[ \t]+связи)?|тел\.)'
)
PHONE = re.compile(
    rf'(?<![\w+])(?:(?P<label>{PHONE_LABEL})[ \t:=—–-]*)?'
    r'(?P<number>[+(]?[0-9]+(?:(?:[ \t\u00a0\u202f()‐‑–—-]+|\.)[0-9]+)*)(?!\w)',
    re.I,
)
PHONE_SEPARATORS = str.maketrans('‐‑–—', '----')
MEASUREMENT = re.compile(
    r'[ \t\u00a0\u202f]*(?:[%₽]|(?:[гмк]?вт|к?дж|руб\w*|кВтч|'
    r'доллар\w*|евро|процент\w*|сч[её]тчик\w*|датчик\w*|шт|лет|год\w*|месяц\w*)\b)',
    re.I,
)
EMAIL = re.compile(r'(?:\b(?:электронная\s+почта|почта|e-?mail)\s*[:=]?\s*)?[\w.%+-]+@[\w.-]+\.[^\W\d_]{2,}', re.I)
NAME_LABEL = r'(?:меня[ \t]+зовут|(?:ф\.?[ \t]*и\.?[ \t]*о\.?|имя)[ \t]*[:=])'
COMPANY_LABEL = r'(?:компания[ \t]*[:=]|компания[ \t]+называется|организация[ \t]*[:=])'
FIELD_VALUE = r'((?:«[^»\n]+»|"[^"\n]+"|\b[^\W\d_]\.|[^,;\n.!?])+)'
NAME = re.compile(rf'\b{NAME_LABEL}[ \t]*{FIELD_VALUE}', re.I)
COMPANY = re.compile(rf'\b{COMPANY_LABEL}[ \t]*{FIELD_VALUE}', re.I)
NEXT_FIELD = re.compile(
    rf'[ \t]+(?:{PHONE_LABEL}|{NAME_LABEL}|{COMPANY_LABEL}|почта|e-?mail)(?!\w)',
    re.I,
)
CONTACT_MARKERS = re.compile(r'\[(?:контакт скрыт|телефон скрыт|имя скрыто|компания скрыта)\]')
GREETING = re.compile(r'(?:привет|здравствуйте|добрый (?:день|вечер)|доброе утро|спасибо|да|нет)', re.I)

PERSON = r'(?:(?:менеджер|специалист|оператор|консультант)\w*|(?:жив\w+\s+)?человек\w*)'
HANDOFF = re.compile(
    rf'\b(?P<negative>не\s+)?(?:'
    rf'(?:пожалуйста[, ]+)?(?:свяжите|соедините|переключите)(?:\s+меня)?\s+с(?:о)?\s+{PERSON}|'
    r'(?:пожалуйста[, ]+)?(?:позвоните|перезвоните)(?:\s+мне)?(?:[, ]+пожалуйста)?|'
    r'(?:пожалуйста[, ]+)?свяжитесь\s+со\s+мной(?:[, ]+пожалуйста)?|'
    r'не\s+могли\s+бы\s+вы\s+(?:позвонить|перезвонить)(?:\s+мне)?|'
    rf'(?:мне\s+)?(?:нужен|нужна|нужны|нуждаюсь\s+в|хочу)\s+'
    rf'(?:(?:разговор|связь)\s+с(?:о)?\s+)?{PERSON}|'
    rf'(?:хочу|можно|как|давайте|нужно|прошу)\s+(?:связаться|связываться|'
    rf'поговорить|пообщаться|созвониться)\s+с(?:о)?\s+{PERSON}|'
    rf'(?:хочу|можно|как|прошу)\s+(?:позвонить|перезвонить)\s+{PERSON}|'
    r'(?:хочу\s+|можно\s+|как\s+|давайте\s+|прошу\s+)?(?:оставить|оформить)\s+заявку'
    r'(?:\s+на\s+(?:консультацию|обратный звонок))?|'
    r'(?:хочу|заказать)\s+обратный звонок'
    r')\b',
    re.I,
)
SHORT_HANDOFF = re.compile(rf'\s*(?:{PERSON}|обратный звонок)(?:[, ]+пожалуйста)?[.!?\s]*', re.I)


def normalize_phone(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    raw = value.strip().translate(PHONE_SEPARATORS)
    if re.search(r'[^0-9+()\s.-]', raw):
        raise ValueError('Use an international phone number')
    digits = re.sub(r'[^0-9]', '', raw)
    if raw.startswith('8') and len(digits) == 11:
        digits = '7' + digits[1:]
    elif not raw.startswith('+'):
        if not (raw.startswith('7') and len(digits) == 11):
            raise ValueError('International prefix required')
    if raw.count('+') > 1 or ('+' in raw and not raw.startswith('+')):
        raise ValueError('Invalid plus sign')
    if not 7 <= len(digits) <= 15 or digits.startswith('0'):
        raise ValueError('Invalid phone length or prefix')
    return '+' + digits


def _phone_number(match: re.Match) -> str | None:
    number = match['number']
    if match['label'] and len(re.sub(r'[^0-9]', '', number)) == 10 and not number.startswith('+'):
        number = '+7' + number
    try:
        return normalize_phone(number)
    except ValueError:
        return None


def _is_measurement(match: re.Match, text: str) -> bool:
    return match['number'].isdigit() and not match['label'] and bool(MEASUREMENT.match(text, match.end()))


def _field_value(match: re.Match) -> str:
    return NEXT_FIELD.split(match.group(1), maxsplit=1)[0].rstrip(' ,')


def extract_contacts(text: str) -> dict:
    result = {}
    for match in PHONE.finditer(text):
        if not _is_measurement(match, text) and (number := _phone_number(match)):
            # Later numbers in the same message may correct an earlier one.
            result['phone'] = number
    for field, pattern, max_length in [('full_name', NAME, 200), ('company', COMPANY, 300)]:
        if match := pattern.search(text):
            value = _field_value(match)
            if value:
                result[field] = value[:max_length]
    return result


def redact(text: str, contacts: dict | None = None, known_values: list[str] | None = None) -> str:
    def replace_phone(match: re.Match) -> str:
        if not _is_measurement(match, text) and (_phone_number(match) or match['label']):
            return '[телефон скрыт]'
        return match.group()

    def replace_field(match: re.Match, marker: str) -> str:
        value = _field_value(match)
        return marker + match.group(1)[len(value):] if value else match.group()

    # Explicit fields must be recognized before replacing their stored values.
    text = NAME.sub(lambda match: replace_field(match, '[имя скрыто]'), text)
    text = COMPANY.sub(lambda match: replace_field(match, '[компания скрыта]'), text)
    text = PHONE.sub(replace_phone, text)
    text = EMAIL.sub('[контакт скрыт]', text)
    values = set(known_values or [])
    values.update((contacts or {}).get(field) for field in ('full_name', 'company', 'phone'))
    known = [
        r'\s+'.join(re.escape(part) for part in value.split())
        for value in sorted(filter(None, values), key=lambda value: (-len(value), value))
        if value.strip()
    ]
    if not known:
        return text
    pattern = rf'(?P<marker>{CONTACT_MARKERS.pattern})|(?<!\w)(?:{"|".join(known)})(?!\w)'
    return re.sub(pattern, lambda match: match['marker'] or '[контакт скрыт]', text, flags=re.I)


def manager_requested(text: str) -> bool:
    return bool(SHORT_HANDOFF.fullmatch(text)) or any(
        not match['negative'] for match in HANDOFF.finditer(text)
    )


def lead_context(text: str) -> str:
    """Keep the customer's task in an already-redacted handoff or contact reply."""
    had_contact = bool(CONTACT_MARKERS.search(text))
    text = CONTACT_MARKERS.sub('', text).strip(' ,.!?:;')
    if SHORT_HANDOFF.fullmatch(text):
        return ''
    text = HANDOFF.sub(lambda match: match.group() if match['negative'] else '', text)
    text = re.sub(r'^(?:и|по вопросу)\s+', '', text.strip(' ,.!?:;—–-'), flags=re.I)
    if had_contact and re.fullmatch(
        r'(?:и|(?:вот|уточняю|исправляю|меняю|замените|обновите)(?:\s+(?:на|данные|контакты))?)',
        text, re.I,
    ):
        return ''
    return '' if GREETING.fullmatch(text) else text
