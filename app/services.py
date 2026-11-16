from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from uuid import uuid4

from .errors import APIError
from .facts import active_topic, current_facts, meter_count, save_fact
from .privacy import extract_contacts, lead_context, manager_requested, redact
from .progress import progress_keywords
from .ml.protocol import (ConsultationRequest, ConsultationResult, ConsultationUnavailable,
                          CorpusNotReady, InvalidEvidence)
from .ml.grounding import SAFE_RESPONSES
from .storage import StorageError
from .telemetry import record


DATABASE_MAX_INT = 2**63 - 1
CONTEXT_MESSAGES = 32
TURN_STAGES = ('retrieval','rerank','generation','review','validation')


def uid():
    return str(uuid4())


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def packed(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def public_session(row):
    return {k: row[k] for k in ('id','status','phase','revision','opened_at','closed_at')}


def public_message(row):
    return {k: row[k] for k in ('id','seq','role','text','created_at')}


def draft_view(row):
    fields = {k: row[k] for k in ('full_name','phone','company')}
    missing = [k for k,v in fields.items() if not v]
    return {**fields, 'revision': row['revision'], 'missing_fields': missing,
            'status': 'draft' if not fields['phone'] else ('partial' if missing else 'complete')}


class Service:
    def __init__(self, db, core, settings):
        self.db, self.core, self.settings = db, core, settings
        self.tasks: set[asyncio.Task] = set()

    def authorize(self, sid, token):
        if not token:
            raise APIError(401,'UNAUTHORIZED','Требуется токен сессии.')
        with self.db.read() as c:
            row = c.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
        if row is None:
            raise APIError(404,'SESSION_NOT_FOUND','Сессия не найдена.')
        if not hmac.compare_digest(row['token_hash'], hashlib.sha256(token.encode()).hexdigest()):
            raise APIError(403,'FORBIDDEN','Нет доступа к этой сессии.')
        return public_session(row)

    def create_session(self):
        sid, token, stamp = uid(), secrets.token_urlsafe(32), now()
        with self.db.transaction() as c:
            c.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?)',
                      (sid,hashlib.sha256(token.encode()).hexdigest(),'active','new',1,stamp,None))
            c.execute('INSERT INTO lead_drafts VALUES (?,0,NULL,NULL,NULL,?)',(sid,stamp))
            greeting = self.append_message(c,sid,'assistant','Здравствуйте! Расскажите, с какой задачей по энергоресурсам нужна помощь.')
            session = self.session(c,sid)
        return {'session':session,'session_token':token,'greeting':greeting}

    def session(self,c,sid):
        row=c.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
        if not row:
            raise APIError(404,'SESSION_NOT_FOUND','Сессия не найдена.')
        return public_session(row)

    def active(self,c,sid):
        session=self.session(c,sid)
        if session['status']=='closed':
            raise APIError(409,'SESSION_CLOSED','Диалог закрыт. Создайте новый.')
        return session

    def recover(self,c,sid):
        c.execute("""UPDATE turns SET status='failed',error_code='INTERRUPTED',retryable=1,lease_until=NULL
            WHERE session_id=? AND status='processing' AND (lease_until IS NULL OR lease_until<=?)""",(sid,time.time()))

    def idle(self,c,sid):
        self.recover(c,sid)
        row=c.execute("SELECT id FROM turns WHERE session_id=? AND status='processing'",(sid,)).fetchone()
        if row:
            raise APIError(409,'TURN_IN_PROGRESS','Предыдущий запрос еще обрабатывается.',True,{'active_turn_id':row['id']})

    def append_message(self,c,sid,role,text):
        seq=c.execute('SELECT COALESCE(MAX(seq),0)+1 FROM messages WHERE session_id=?',(sid,)).fetchone()[0]
        mid,stamp=uid(),now()
        c.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',(mid,sid,seq,role,text,stamp))
        return {'id':mid,'seq':seq,'role':role,'text':text,'created_at':stamp}

    def close_session(self,sid,request_id=None):
        with self.db.transaction(session_id=sid) as c:
            self.idle(c,sid)
            changed=c.execute("UPDATE sessions SET status='closed',phase='closed',closed_at=?,revision=revision+1 WHERE id=? AND status='active'",(now(),sid)).rowcount
            result={'session':self.session(c,sid)}
        if changed:
            record('session_closed',session_id=sid,request_id=request_id)
        return result

    def messages(self,sid,after_seq,limit):
        if after_seq > DATABASE_MAX_INT:
            return {'items':[],'next_after_seq':None}
        with self.db.read() as c:
            rows=c.execute('SELECT * FROM messages WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?', (sid,after_seq,limit+1)).fetchall()
        items=[public_message(r) for r in rows[:limit]]
        return {'items':items,'next_after_seq':items[-1]['seq'] if len(rows)>limit else None}

    def get_draft(self,sid):
        with self.db.read() as c:
            return {'draft':draft_view(c.execute('SELECT * FROM lead_drafts WHERE session_id=?',(sid,)).fetchone())}

    def patch_draft(self,sid,patch):
        with self.db.transaction(session_id=sid) as c:
            self.active(c,sid)
            self.idle(c,sid)
            draft=c.execute('SELECT * FROM lead_drafts WHERE session_id=?',(sid,)).fetchone()
            if draft['revision']!=patch.expected_revision:
                raise APIError(409,'DRAFT_VERSION_CONFLICT','Контакты изменились. Обновите форму.')
            fields=patch.fields.model_dump(exclude_unset=True)
            if fields:
                self.update_contacts(c,sid,fields)
                c.execute("UPDATE sessions SET phase='collecting_contacts',revision=revision+1 WHERE id=?",(sid,))
            return {'draft':draft_view(c.execute('SELECT * FROM lead_drafts WHERE session_id=?',(sid,)).fetchone())}

    def update_contacts(self,c,sid,fields):
        # Keys are whitelisted by the typed form/extractor, never client SQL.
        fields={k:v for k,v in fields.items() if k in ('full_name','phone','company')}
        if not fields:
            return
        for field,value in fields.items():
            previous=c.execute(f'SELECT {field} FROM lead_drafts WHERE session_id=?',(sid,)).fetchone()[0]
            for contact in (previous,value):
                if contact:
                    c.execute('INSERT INTO contact_history VALUES (?,?,?) ON CONFLICT DO NOTHING',(sid,field,contact))
        assignments=','.join(k+'=?' for k in fields)
        c.execute(f'UPDATE lead_drafts SET {assignments},revision=revision+1,updated_at=? WHERE session_id=?',(*fields.values(),now(),sid))

    def accept_turn(self,sid,payload):
        text,client_id=payload.text,str(payload.client_message_id)
        fingerprint=hashlib.sha256(text.encode()).hexdigest()
        with self.db.transaction(session_id=sid) as c:
            self.recover(c,sid)
            old=c.execute('SELECT * FROM turns WHERE session_id=? AND client_message_id=?',(sid,client_id)).fetchone()
            if old:
                if old['request_hash']!=fingerprint:
                    raise APIError(409,'IDEMPOTENCY_CONFLICT','Этот ключ уже использован для другого сообщения.')
                if old['status']!='failed' or not old['retryable']:
                    return dict(old),False
                self.active(c,sid)
                self.idle(c,sid)
                last=c.execute("SELECT id FROM messages WHERE session_id=? AND role='user' ORDER BY seq DESC LIMIT 1",(sid,)).fetchone()
                if last['id']!=old['user_message_id']:
                    raise APIError(409,'RETRY_SUPERSEDED','После этой реплики начат новый запрос. Повторите вопрос новым сообщением.')
                c.execute("""UPDATE turns SET status='processing',attempt=attempt+1,lease_until=?,
                    error_code=NULL,retryable=0,progress_stage='queued',progress_keywords='[]' WHERE id=?""",
                    (time.time()+self.settings.turn_lease_seconds,old['id']))
                tid=old['id']
            else:
                self.active(c,sid)
                self.idle(c,sid)
                message=self.append_message(c,sid,'user',text)
                tid=uid()
                c.execute('INSERT INTO turns(id,session_id,client_message_id,request_hash,status,user_message_id,lease_until,created_at) VALUES (?,?,?,?,?,?,?,?)',
                          (tid,sid,client_id,fingerprint,'processing',message['id'],time.time()+self.settings.turn_lease_seconds,now()))
                self.update_contacts(c,sid,extract_contacts(text))
                draft,known_values=self.contact_context(c,sid)
                messages=self.recent_context(c,sid,message['seq'],draft,known_values)
                previous_facts=self.facts(c,sid,draft,known_values)
                fact=meter_count(messages[-1],active_topic(messages),previous_facts)
                if fact:
                    save_fact(c,sid,fact,now())
            return dict(c.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone()),True

    def track(self,coroutine):
        task=asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.forget_task)
        return task

    def forget_task(self,task):
        self.tasks.discard(task)
        # A disconnected caller may no longer be waiting on the acceptance task.
        if not task.cancelled():
            task.exception()

    async def start_turn(self,sid,payload,request_id):
        turn,start=await asyncio.to_thread(self.accept_turn,sid,payload)
        if start:
            if turn['attempt'] > 1:
                record('turn_retried',session_id=sid,turn_id=turn['id'],request_id=request_id,
                       corpus_version=self.core.corpus_version,attempt=turn['attempt'])
            return turn,self.track(self.run_turn(turn,request_id))
        return turn,None

    async def post_turn(self,sid,payload,request_id,*,include_progress=False):
        # A client disconnect must not abandon a committed turn before scheduling it.
        turn,task=await asyncio.shield(self.track(self.start_turn(sid,payload,request_id)))
        if task is not None:
            await asyncio.wait({task},timeout=self.settings.sync_wait_seconds)
        result=await asyncio.to_thread(self.get_turn,sid,turn['id'],request_id,include_progress=include_progress)
        if result['status']=='failed':
            error=result['error']
            raise APIError(503 if error['retryable'] else 500,error['code'],error['message'],error['retryable'])
        return result

    def get_turn(self,sid,tid,request_id,*,include_progress=False):
        with self.db.read() as c:
            row=c.execute('SELECT * FROM turns WHERE id=? AND session_id=?',(tid,sid)).fetchone()
        if not row:
            raise APIError(404,'TURN_NOT_FOUND','Запрос не найден.')
        if row['status']=='processing' and (row['lease_until'] is None or row['lease_until']<=time.time()):
            with self.db.transaction(session_id=sid) as c:
                self.recover(c,sid)
                row=c.execute('SELECT * FROM turns WHERE id=? AND session_id=?',(tid,sid)).fetchone()
            if row['status']=='failed' and row['error_code']=='INTERRUPTED':
                record('turn_interrupted',session_id=sid,turn_id=tid,request_id=request_id,
                       corpus_version=self.core.corpus_version,error_code='INTERRUPTED')
        if row['status']=='completed':
            return json.loads(row['response_json'])
        if row['status']=='processing':
            result={'turn_id':tid,'status':'processing','poll_after_ms':700}
            if include_progress:
                result['progress']={'stage':row['progress_stage'],
                                    'keywords':json.loads(row['progress_keywords'])}
            return result
        return {'turn_id':tid,'status':'failed','error':{
            'code':row['error_code'],'message':('Не удалось завершить ответ. Повторите запрос с тем же ключом.' if row['retryable'] else 'Ответ не прошел проверку. Уточните вопрос новым сообщением.'),
            'request_id':request_id,'retryable':bool(row['retryable']),'details':{}}}

    def facts(self,c,sid,draft,known_values):
        return current_facts(c,sid,draft,known_values)

    def contact_context(self,c,sid):
        draft=draft_view(c.execute('SELECT * FROM lead_drafts WHERE session_id=?',(sid,)).fetchone())
        known_values=[row[0] for row in c.execute('SELECT value FROM contact_history WHERE session_id=?',(sid,))]
        return draft,known_values

    def recent_context(self,c,sid,last_seq,draft,known_values):
        rows=c.execute('SELECT * FROM messages WHERE session_id=? AND seq<=? ORDER BY seq DESC LIMIT ?',
                       (sid,last_seq,CONTEXT_MESSAGES)).fetchall()
        return [{'id':row['id'],'role':row['role'],'text':redact(row['text'],draft,known_values)}
                for row in reversed(rows)]

    def turn_context(self,sid,user_message_id):
        with self.db.read() as c:
            session=self.session(c,sid)
            latest=dict(c.execute('SELECT * FROM messages WHERE id=? AND session_id=?', (user_message_id,sid)).fetchone())
            draft,known_values=self.contact_context(c,sid)
            messages=self.recent_context(c,sid,latest['seq'],draft,known_values)
            facts=self.facts(c,sid,draft,known_values)
            topic=active_topic(messages)
            if topic is None and any(fact['source_message_id']==user_message_id for fact in facts):
                topic='general'
            facts=[fact for fact in facts if fact['topic_key']==topic]
            retained={message['id'] for message in messages}
            # Old factual source messages stay available when their facts are active.
            older=[]
            for fact in facts:
                source_id=fact['source_message_id']
                if source_id not in retained:
                    source=c.execute('SELECT id,role,text,seq FROM messages WHERE id=?',(source_id,)).fetchone()
                    if source['seq']<latest['seq']:
                        older.append(source)
                    retained.add(source_id)
            older_context=[{'id':row['id'],'role':row['role'],'text':redact(row['text'],draft,known_values)}
                           for row in sorted(older,key=lambda row:row['seq'])]
        return session,latest,older_context+messages,draft,known_values,topic,facts

    def update_turn_progress(self, turn, stage, keywords):
        with self.db.transaction(session_id=turn['session_id']) as c:
            return bool(c.execute("""UPDATE turns SET progress_stage=?,progress_keywords=?
                WHERE id=? AND attempt=? AND status='processing' AND lease_until>?""",
                (stage,packed(keywords),turn['id'],turn['attempt'],time.time())).rowcount)

    async def run_turn(self,turn,request_id):
        started=time.perf_counter()
        sid,tid,attempt=turn['session_id'],turn['id'],turn['attempt']
        event={'session_id':sid,'turn_id':tid,'request_id':request_id,
               'corpus_version':self.core.corpus_version,'attempt':attempt}
        try:
            if not await asyncio.to_thread(self.update_turn_progress,turn,'analyzing',[]):
                return
            session,latest,messages,draft,known_values,topic,facts=await asyncio.to_thread(
                self.turn_context,sid,turn['user_message_id'])
            latest_text=messages[-1]['text']
            contact_reply=(session['phase'] in {'collecting_contacts','reviewing_lead'} and
                           bool(extract_contacts(latest['text'])) and not lead_context(latest_text))
            contact_flow=manager_requested(latest['text']) or contact_reply
            keywords=progress_keywords(latest_text,topic)
            if contact_flow:
                missing=draft['missing_fields']
                labels={'full_name':'ФИО','phone':'телефон','company':'название компании'}
                answer=('Для заявки укажите '+', '.join(labels[k] for k in missing)+'. Перед сохранением покажу резюме для подтверждения.' if missing else 'Контакты заполнены. Проверьте резюме заявки и подтвердите сохранение в форме.')
                result=ConsultationResult(kind='clarify',answer_text=answer,reason_code='contact_request')
                decision={'kind':'collecting_contacts','reason_code':'contact_request'}
                phase='collecting_contacts'
            else:
                request=ConsultationRequest(request_id=request_id,session_id=sid,user_message_id=latest['id'],
                    latest_user_text=latest_text,messages=messages,active_topic=topic,facts=facts,
                    current_phase=session['phase'],corpus_version=event['corpus_version'],
                    timeout_budget_ms=int(self.settings.ml_timeout_seconds*1000))
                if not await asyncio.to_thread(self.update_turn_progress,turn,'preparing_answer',keywords):
                    return
                try:
                    result=await asyncio.wait_for(self.core.consult(request),timeout=self.settings.ml_timeout_seconds)
                    result=ConsultationResult.model_validate(result)
                    if result.kind=='answer' and not result.evidence:
                        raise InvalidEvidence('Missing evidence')
                    self.core.validate_result(result)
                except InvalidEvidence:
                    result=ConsultationResult(kind='insufficient_knowledge',answer_text=SAFE_RESPONSES['invalid'],
                                              reason_code='invalid_evidence')
                decision={'kind':result.kind,'reason_code':result.reason_code}
                phase='clarification' if result.kind=='clarify' else 'consulting'
            timings={stage:ms for stage,ms in result.timings_ms.items()
                     if stage in (*TURN_STAGES,'total') and math.isfinite(ms) and ms>=0}
            trace={'retrieval_query':redact(result.retrieval_query,draft,known_values),'decision':decision,
                   'source_ids':list(dict.fromkeys(e.document_id for e in result.evidence)),
                   'evidence':[e.model_dump() for e in result.evidence],'timings_ms':timings,
                   'corpus_version':event['corpus_version'],'latency_ms':round((time.perf_counter()-started)*1000,3),
                   'model_metadata':{key:redact(value,draft,known_values) for key,value in result.model_metadata.items()
                                     if key in {'model','mode','corpus_version'}},
                   'recommended_products':[redact(value,draft,known_values) for value in result.recommended_products]}
            if not await asyncio.to_thread(self.update_turn_progress,turn,'finalizing',keywords):
                return
            storage_started=time.perf_counter()
            completed=await asyncio.to_thread(self.complete_turn,turn,latest,result.answer_text,phase,
                                              draft if contact_flow else None,trace)
            if completed:
                stages={f'{stage}_ms':timings[stage] for stage in TURN_STAGES if stage in timings}
                record('turn_completed',**event,decision=decision['kind'],
                       elapsed_ms=round((time.perf_counter()-started)*1000,3),
                       storage_ms=round((time.perf_counter()-storage_started)*1000,3),**stages)
        except asyncio.CancelledError:
            await asyncio.to_thread(self.fail_turn,tid,attempt,'INTERRUPTED',True,event,started)
            raise
        except CorpusNotReady:
            await asyncio.to_thread(self.fail_turn,tid,attempt,'KNOWLEDGE_NOT_READY',True,event,started)
        except (ConsultationUnavailable,TimeoutError):
            await asyncio.to_thread(self.fail_turn,tid,attempt,'MODEL_UNAVAILABLE',True,event,started)
        except (StorageError, sqlite3.Error):
            await asyncio.to_thread(self.fail_turn,tid,attempt,'STORAGE_UNAVAILABLE',True,event,started)
        except Exception:
            # Never disclose prompt contents, provider responses, DB paths, or PII.
            await asyncio.to_thread(self.fail_turn,tid,attempt,'INTERNAL_ERROR',True,event,started)

    def complete_turn(self,turn,latest,answer,phase,draft,trace):
        sid,tid,attempt=turn['session_id'],turn['id'],turn['attempt']
        with self.db.transaction(session_id=sid) as c:
            current=c.execute('SELECT status,attempt,lease_until FROM turns WHERE id=?',(tid,)).fetchone()
            # A worker that lost its lease cannot append an answer.
            if current['status']!='processing' or current['attempt']!=attempt or not current['lease_until'] or current['lease_until']<=time.time():
                return False
            assistant=self.append_message(c,sid,'assistant',answer)
            c.execute('UPDATE sessions SET phase=?,revision=revision+1 WHERE id=?',(phase,sid))
            trace['assistant_message_id']=assistant['id']
            c.execute('INSERT INTO answer_traces VALUES (?,?,?)',(assistant['id'],sid,packed(trace)))
            response={'turn_id':tid,'status':'completed','session':self.session(c,sid),
                'user_message':public_message(latest),'assistant_message':assistant,'decision':trace['decision'],
                'lead_draft':draft}
            c.execute("UPDATE turns SET status='completed',assistant_message_id=?,response_json=?,lease_until=NULL WHERE id=?",(assistant['id'],packed(response),tid))
        return True

    def fail_turn(self,tid,attempt,code,retryable,event,started):
        try:
            with self.db.transaction(session_id=event['session_id']) as c:
                changed=c.execute("""UPDATE turns SET status='failed',error_code=?,retryable=?,lease_until=NULL
                    WHERE id=? AND attempt=? AND status='processing' AND lease_until>?""",
                    (code,int(retryable),tid,attempt,time.time())).rowcount
        except (StorageError, sqlite3.Error):
            # Lease recovery remains available after the storage failure clears.
            return
        if changed:
            record('turn_failed',**event,error_code=code,elapsed_ms=round((time.perf_counter()-started)*1000,3))

    def preview(self,sid):
        with self.db.transaction(session_id=sid) as c:
            self.active(c,sid)
            self.idle(c,sid)
            draft=draft_view(c.execute('SELECT * FROM lead_drafts WHERE session_id=?',(sid,)).fetchone())
            messages=[dict(r) for r in c.execute('SELECT * FROM messages WHERE session_id=? ORDER BY seq',(sid,))]
            known_values=[r[0] for r in c.execute('SELECT value FROM contact_history WHERE session_id=?',(sid,))]
            customer=[lead_context(redact(m['text'],draft,known_values)) for m in messages if m['role']=='user']
            customer=[text for text in customer if text]
            # An extractive synopsis preserves correction order without inventing facts.
            excerpt=customer[-6:]
            summary=('Последние сведения клиента (поздние исправления имеют приоритет):\n'+'\n'.join(f'— {s[:400]}' + ('…' if len(s)>400 else '') for s in excerpt)) if excerpt else 'Клиент оставляет заявку. Задача пока не описана.'
            if len(customer)>6:
                summary+='\nБолее ранние сведения сохранены в истории диалога.'
            traces=[
                (json.loads(row['payload_json']), row['text'])
                for row in c.execute(
                    'SELECT t.payload_json, m.text FROM answer_traces t '
                    'JOIN messages m ON m.id=t.assistant_message_id WHERE t.session_id=? ORDER BY m.seq',
                    (sid,),
                )
            ]
            products=list(dict.fromkeys(
                redact(product,draft,known_values)
                for trace,_ in traces for product in trace.get('recommended_products',[])
            ))
            consultation=next(
                ((trace,text) for trace,text in reversed(traces)
                 if trace['decision']['kind'] != 'collecting_contacts'),
                None,
            )
            questions=(
                [redact(consultation[1],draft,known_values)]
                if consultation and consultation[0]['decision']['kind']=='clarify' else []
            )
            preview={'preview_id':uid(),'snapshot_last_seq':messages[-1]['seq'],'draft_revision':draft['revision'],
                     **{k:draft[k] for k in ('status','missing_fields','full_name','phone','company')},
                     'can_confirm':bool(draft['phone']),'client_problem':' / '.join(s[:300] for s in customer[-3:]),
                     'summary':summary,'facts':self.facts(c,sid,draft,known_values),
                     'recommended_products':products,'open_questions':questions}
            c.execute('INSERT INTO lead_previews VALUES (?,?,?,?,?,?)',(preview['preview_id'],sid,preview['snapshot_last_seq'],draft['revision'],packed(preview),now()))
            c.execute("UPDATE sessions SET phase='reviewing_lead',revision=revision+1 WHERE id=?",(sid,))
        return preview

    def confirm(self,sid,payload):
        key,pid=str(payload.idempotency_key),str(payload.preview_id)
        with self.db.transaction(session_id=sid) as c:
            old=c.execute('SELECT * FROM leads WHERE session_id=? AND idempotency_key=?',(sid,key)).fetchone()
            if old:
                if old['preview_id']!=pid:
                    raise APIError(409,'IDEMPOTENCY_CONFLICT','Этот ключ уже использован для другого превью.')
                return {'lead':json.loads(old['payload_json']),'message':'Заявка сохранена в демонстрационном прототипе.'},False
            self.active(c,sid)
            self.idle(c,sid)
            row=c.execute('SELECT * FROM lead_previews WHERE id=? AND session_id=?',(pid,sid)).fetchone()
            if not row:
                raise APIError(404,'PREVIEW_NOT_FOUND','Предпросмотр не найден.')
            if c.execute('SELECT 1 FROM leads WHERE preview_id=?',(pid,)).fetchone():
                raise APIError(409,'PREVIEW_ALREADY_CONFIRMED','Это превью уже подтверждено.')
            draft=c.execute('SELECT * FROM lead_drafts WHERE session_id=?',(sid,)).fetchone()
            last=c.execute('SELECT MAX(seq) FROM messages WHERE session_id=?',(sid,)).fetchone()[0]
            if last!=row['snapshot_last_seq'] or draft['revision']!=row['draft_revision']:
                raise APIError(409,'PREVIEW_STALE','Переписка или контакты изменились. Обновите предпросмотр.')
            snapshot=json.loads(row['snapshot_json'])
            if not snapshot['can_confirm']:
                raise APIError(422,'CONTACT_REQUIRED','Для заявки нужен телефон. Черновик сохранен.')
            stamp=now()
            lead={k:v for k,v in snapshot.items() if k not in ('preview_id','snapshot_last_seq','draft_revision','can_confirm')}
            lead.update(id=uid(),session_id=sid,created_at=stamp,confirmed_at=stamp)
            c.execute('INSERT INTO leads VALUES (?,?,?,?,?,?,?,?)',(lead['id'],sid,pid,key,lead['status'],lead['phone'],packed(lead),stamp))
            c.execute("UPDATE sessions SET phase='lead_saved',revision=revision+1 WHERE id=?",(sid,))
        record('lead_confirmed', lead_id=lead['id'], session_id=sid, status=lead['status'])
        return {'lead':lead,'message':'Заявка сохранена в демонстрационном прототипе.'},True

    def review_sessions(self,offset,limit):
        if offset > DATABASE_MAX_INT:
            return {'items':[],'next_offset':None}
        with self.db.read() as c:
            rows=c.execute('SELECT * FROM sessions ORDER BY opened_at,id LIMIT ? OFFSET ?',(limit+1,offset)).fetchall()
        return {'items':[public_session(r) for r in rows[:limit]],'next_offset':offset+limit if len(rows)>limit else None}

    def review_session(self,sid):
        with self.db.read() as c:
            session=self.session(c,sid)
            messages=[public_message(r) for r in c.execute('SELECT * FROM messages WHERE session_id=? ORDER BY seq',(sid,))]
            draft=draft_view(c.execute('SELECT * FROM lead_drafts WHERE session_id=?',(sid,)).fetchone())
            leads=[json.loads(r[0]) for r in c.execute('SELECT payload_json FROM leads WHERE session_id=? ORDER BY created_at,id',(sid,))]
        return {'session':session,'messages':messages,'draft':draft,'leads':leads}

    def review_leads(self,offset,limit):
        if offset > DATABASE_MAX_INT:
            return {'items':[],'next_offset':None}
        with self.db.read() as c:
            rows=c.execute('''SELECT 'confirmed' AS record_type, id AS record_id, session_id, created_at AS stamp FROM leads
              UNION ALL SELECT 'draft', session_id, session_id, updated_at FROM lead_drafts
              ORDER BY stamp,record_id,record_type LIMIT ? OFFSET ?''',(limit+1,offset)).fetchall()
            items=[]
            for r in rows[:limit]:
                if r['record_type']=='confirmed':
                    obj=json.loads(c.execute('SELECT payload_json FROM leads WHERE id=?',(r['record_id'],)).fetchone()[0])
                else:
                    obj=draft_view(c.execute('SELECT * FROM lead_drafts WHERE session_id=?',(r['session_id'],)).fetchone())
                    obj['session_id']=r['session_id']
                items.append({'record_type':r['record_type'],**obj})
        return {'items':items,'next_offset':offset+limit if len(rows)>limit else None}

    def review_lead(self,lid):
        with self.db.read() as c:
            row=c.execute('SELECT payload_json FROM leads WHERE id=?',(lid,)).fetchone()
        if not row:
            raise APIError(404,'LEAD_NOT_FOUND','Заявка не найдена.')
        return {'lead':json.loads(row[0])}

    def trace(self,mid):
        with self.db.read() as c:
            row=c.execute('SELECT payload_json FROM answer_traces WHERE assistant_message_id=?',(mid,)).fetchone()
        if not row:
            raise APIError(404,'TRACE_NOT_FOUND','Трассировка не найдена.')
        return json.loads(row[0])
