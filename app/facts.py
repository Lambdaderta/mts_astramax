"""Extract explicit customer measurements and keep their message provenance."""
import json
import re

from .privacy import redact


RESOURCE_WORDS = {
    'electricity': re.compile(r'\b(?:электр\w*|квт(?:\s*[·⋅*-]?\s*ч)?)\b', re.I),
    'water': re.compile(r'\b(?:вод[аеуыо]|водоснабжен\w*|водомер\w*|гвс|хвс)\b', re.I),
    'heat': re.compile(r'\b(?:тепл\w*|отоплен\w*|гкал)\b', re.I),
    'gas': re.compile(r'\bгаз(?:а|ом|у|е|ов|ов\w*|оснабжен\w*)?\b', re.I),
}
METER_WORD = r'(?:сч[её]тчик\w*|прибор\w*\s+уч[её]та)'
COUNT = r'(?P<count>0|[1-9][0-9]{0,7})(?!\d)'
METER_COUNT = re.compile(
    rf'\b(?:у\s+нас\s+(?:(?:установлен\w*|есть|работа\w*)\s+)?|установлен\w*\s+)'
    rf'{COUNT}\s+{METER_WORD}\b', re.I,
)
METER_FIELD = re.compile(
    rf'^\s*количество\s+{METER_WORD}\s*[:=—-]\s*{COUNT}\s*$', re.I,
)
CORRECTION = re.compile(
    rf'^\s*(?:исправляю(?:сь)?|поправка|уточняю|на\s+самом\s+деле)'
    rf'(?:[\s,:—-]+)(?:не\s+[0-9]+(?:\s+{METER_WORD})?\s*,?\s+а\s+)?'
    rf'{COUNT}(?:\s+{METER_WORD}\b|\s*$)', re.I,
)
NON_ASSERTION = re.compile(
    r'\b(?:если|допустим|предполож\w*|гипотетически|пример\w*|будет|будут|'
    r'около|не\s+установлен\w*|план\w*|хотим|нужно|можно|или|либо)\b|[?«»"]', re.I,
)
FOLLOW_UP = re.compile(
    r'^\s*(?:а\b|и\b|для\s+этого\b|тогда\b|что\s+(?:дальше|нужно)\b|'
    r'исправляю\b|поправка\b|уточняю\b|на\s+самом\s+деле\b)', re.I,
)


def explicit_topics(text):
    return {topic for topic, words in RESOURCE_WORDS.items() if words.search(text)}


def active_topic(messages):
    latest = messages[-1]['text']
    topics = explicit_topics(latest)
    if len(topics) == 1:
        return next(iter(topics))
    if topics or not FOLLOW_UP.search(latest):
        return None
    for message in reversed(messages[:-1]):
        if message['role'] == 'user':
            topics = explicit_topics(message['text'])
            if topics:
                return next(iter(topics)) if len(topics) == 1 else None
    return 'general'


def meter_count(message, topic, previous_facts):
    text = message['text']
    if len(explicit_topics(text)) > 1:
        return None
    topic = topic or 'general'
    values = set()
    previous = any(fact['topic_key'] == topic and fact['key'] == 'meter_count' for fact in previous_facts)
    for sentence in re.split(r'[;.!\n]', text):
        if NON_ASSERTION.search(sentence):
            continue
        corrected = CORRECTION.match(sentence) if previous else None
        if corrected:
            values.add(int(corrected['count']))
        if field := METER_FIELD.match(sentence):
            values.add(int(field['count']))
        values.update(int(stated['count']) for stated in METER_COUNT.finditer(sentence))
    if len(values) == 1:
        return {'topic_key':topic,'key':'meter_count','value':values.pop(),'source_message_id':message['id']}
    return None


def current_facts(conn, session_id, draft, known_values):
    rows = conn.execute('''SELECT f.topic_key, f.fact_key, f.value_json, f.source_message_id, m.text
        FROM facts f JOIN messages m ON m.id=f.source_message_id AND m.session_id=f.session_id
        WHERE f.session_id=? AND f.superseded_at IS NULL AND m.role='user' ORDER BY f.id''', (session_id,))
    facts = []
    for row in rows:
        value = json.loads(row['value_json'])
        source = redact(row['text'], draft, known_values)
        if re.search(rf'(?<!\d){value}(?!\d)', source):
            facts.append({'topic_key':row['topic_key'],'key':row['fact_key'],'value':value,
                          'source_message_id':row['source_message_id']})
    return facts


def save_fact(conn, session_id, fact, stamp):
    key = (session_id, fact['topic_key'], fact['key'])
    value = json.dumps(fact['value'])
    current = conn.execute('''SELECT value_json FROM facts
        WHERE session_id=? AND topic_key=? AND fact_key=? AND superseded_at IS NULL''', key).fetchone()
    if current is not None:
        if current['value_json'] == value:
            return
        conn.execute('''UPDATE facts SET superseded_at=?
            WHERE session_id=? AND topic_key=? AND fact_key=? AND superseded_at IS NULL''', (stamp, *key))
    conn.execute('''INSERT INTO facts(session_id,topic_key,fact_key,value_json,source_message_id)
        VALUES(?,?,?,?,?)''', (*key, value, fact['source_message_id']))
