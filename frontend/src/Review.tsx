import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import type { ApiLead, ChatService, DebugInfo, Session } from './types';
import type { Schema } from './api/client';
import { ApiError, errorWithRequest } from './api/client';
import { ApiLeadCard, Button, DebugPanel, Header, Icon, Sidebar } from './ui';
import s from './App.module.css';

export default function Review({ service }: { service: ChatService }) {
  const navigate = useNavigate();
  const [sessions, setSessions] = useState<Session[]>([]);
  const [selected, setSelected] = useState<Session>();
  const [leads, setLeads] = useState<Schema['ReviewLeadPage']['items']>([]);
  const [lead, setLead] = useState<ApiLead>();
  const [tab, setTab] = useState<'sessions' | 'leads'>('sessions');
  const [trace, setTrace] = useState<DebugInfo>();
  const [error, setError] = useState<string>();
  const [denied, setDenied] = useState(false);
  const [loading, setLoading] = useState(false);
  const [mobile, setMobile] = useState(false);
  const available = service.mode === 'api' && service.reviewAvailable && !denied;
  function failure(err: unknown) {
    setError(errorWithRequest(err));
    if (err instanceof ApiError && [401, 403].includes(err.status)) { setDenied(true); setSessions([]); setSelected(undefined); setLeads([]); setLead(undefined); setTrace(undefined); }
  }
  useEffect(() => {
    if (!available) return;
    let alive = true; setLoading(true);
    Promise.all([service.listReviewSessions!(), service.listReviewLeads!()]).then(([history, entries]) => { if (alive) { setSessions(history); setLeads(entries); } }).catch(err => { if (alive) failure(err); }).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [service, available]);
  async function select(id: string) {
    setLoading(true); setTrace(undefined); setError(undefined); setLead(undefined); setMobile(false);
    try { const session = await service.getReviewSession!(id); setSelected(session); setSessions(prev => prev.map(item => item.id === id ? session : item)); }
    catch (err) { failure(err); }
    finally { setLoading(false); }
  }
  async function inspect(id: string) { setError(undefined); setTrace(undefined); try { setTrace(await service.getTrace!(id)); } catch (err) { failure(err); } }
  if (!available) return <div className={s.app}><Header onAssistant={() => navigate('/')} /><main className={s.notFound}><h1>Раздел проверки закрыт</h1><p>{error || 'Для просмотра истории и источников требуется доступ через защищённый gateway.'}</p><Link to="/">Вернуться к помощнику</Link></main></div>;
  return <div className={s.app}><Header onAssistant={() => navigate('/')} /><div className={s.mobileToolbar}><Button onClick={() => setMobile(true)}>История</Button><span>Режим проверки</span></div><div className={s.workspace}>
    <Sidebar sessions={sessions} currentId={selected?.id} includeEmpty collapsed={false} mobileOpen={mobile} debug={false} debugAvailable={false} busy={loading} onNew={() => navigate('/')} onSelect={id => void select(id)} onClose={() => setMobile(false)} onCollapse={() => setMobile(false)} onDebug={() => undefined} />
    <main className={s.chat}>
      <div className={s.reviewTabs}><Button onClick={() => { setTab('sessions'); setLead(undefined); }}>Диалоги</Button><Button onClick={() => { setTab('leads'); setTrace(undefined); }}>Заявки и черновики</Button></div>
      {error && <p role="alert" className={s.archiveNotice}>{error}</p>}
      {loading && <p role="status">Загружаем данные…</p>}
      <div className={s.messages}><div className={s.reviewContent}>
        {tab === 'sessions' ? selected ? <>{selected.messages.map(message => <div className={`${s.message} ${message.role === 'user' ? s.userMessage : ''}`} key={message.id}>{message.role === 'bot' && <span className={`${s.avatar} ${s.messageAvatar}`}><Icon name="bot" /></span>}<div className={s.bubble}>{message.text}{message.role === 'bot' && <button className={s.inspectMessage} aria-label="Показать источники ответа" onClick={() => void inspect(message.id)}><Icon name="document" /></button>}</div></div>)}{selected.apiLead && <ApiLeadCard saved record={selected.apiLead} />}</> : <p>Выберите диалог из истории. Здесь доступны все сессии, включая разговоры без заявки.</p> : <>{lead ? <ApiLeadCard saved record={lead} /> : leads.map((item, i) => <article className={s.reviewLead} key={item.record_type === 'confirmed' ? item.id : `${item.session_id}-${i}`}><h3>{item.record_type === 'confirmed' ? item.client_problem : 'Черновик заявки'}</h3><p>{[item.full_name, item.phone, item.company].filter(Boolean).join(' · ') || 'Контакты не указаны'}</p><p>{item.record_type === 'confirmed' ? 'Подтверждена' : 'Черновик'} · {item.status}</p>{item.record_type === 'confirmed' && <Button onClick={() => { void service.getReviewLead!(item.id).then(setLead).catch(failure); }}>Открыть заявку</Button>}</article>)}{!leads.length && <p>Заявок и черновиков пока нет</p>}</>}
      </div></div>
    </main>{trace && <DebugPanel info={trace} mobileOpen onClose={() => setTrace(undefined)} />}
  </div></div>;
}
