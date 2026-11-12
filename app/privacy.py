"""Deterministic contact handling. Not a general-purpose PII detector."""
import re
from .contracts import normalize_phone

PHONE = re.compile(r'(?<!\w)(?:\+\d|8)[\d ()-]{7,23}\d(?!\w)')
NAME = re.compile(r'(?:меня зовут|фио\s*[:=])\s*([^,;\n.!?]+)', re.I)
COMPANY = re.compile(r'(?:компания\s*[:=]|компания называется)\s*([^;\n.!?]+)', re.I)
NEXT_FIELD = re.compile(r',?\s+(?:телефон|фио|компания|меня зовут)\s*[:=]?\s*', re.I)


def extract_contacts(text: str) -> dict:
    result = {}
    match = PHONE.search(text)
    if match:
        try:
            result['phone'] = normalize_phone(match.group())
        except ValueError:
            pass
    for field, pattern, max_length in [('full_name', NAME, 200), ('company', COMPANY, 300)]:
        match = pattern.search(text)
        if match:
            result[field] = NEXT_FIELD.split(match.group(1), maxsplit=1)[0].strip(' ,')[:max_length] or None
    return result


def redact(text: str, contacts: dict | None = None, known_values: list[str] | None = None) -> str:
    for value in sorted(known_values or [], key=len, reverse=True):
        if value:
            text = re.sub(re.escape(value), '[контакт скрыт]', text, flags=re.I)
    for field in ('full_name', 'company', 'phone'):
        value = (contacts or {}).get(field)
        if value:
            text = re.sub(re.escape(value), '[контакт скрыт]', text, flags=re.I)
    text = PHONE.sub('[телефон скрыт]', text)
    text = NAME.sub('[имя скрыто]', text)
    return COMPANY.sub('[компания скрыта]', text)


def manager_requested(text: str) -> bool:
    low = text.lower()
    if re.search(r'\bне\s+(?:хочу|нужен|нужна|звоните|связывайте)', low):
        return False
    return bool(re.search(r'(?:свяж|позвон|перезвон|оставить\s+заявк|нужен\s+(?:менеджер|специалист)|хочу\s+(?:менеджера|специалиста)|менеджер[а]?\s*$)', low))
