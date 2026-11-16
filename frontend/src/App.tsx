import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, Route, Routes, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import type { ChatService, DebugInfo, LeadInput, Product, Session, TurnProgress } from './types';
import { TurnIndicator } from './TurnIndicator';
import { createMockService, getLastDebug } from './service';
import { appService } from './runtime';
import { errorWithRequest } from './api/client';
import { demoFixture, demoScreens, memoryStorage, type DemoState } from './demo';
import { copy, productsFor, quickReplies, suggestions } from './data';
import { Badge, Button, Composer, DebugPanel, Header, Icon, IconButton, LeadForm, Modal, ProductCards, SentLead, Sidebar, ApiLeadCard } from './ui';
import Archive from './Archive';
import Review from './Review';
import s from './App.module.css';

export default function App() {
  return <Routes>
    <Route path="/" element={<Workspace />} />
    <Route path="/chat/:sessionId" element={<Workspace />} />
    <Route path="/archive" element={<Archive service={appService} />} />
    <Route path="/review" element={<Review service={appService} />} />
    <Route path="/demo" element={<Demo />} />
    <Route path="*" element={<div className={s.notFound}><h1>Страница не найдена</h1><Link to="/">Открыть помощника</Link></div>} />
  </Routes>;
}
function Demo() {
  const [params] = useSearchParams();
  const screen = demoScreens.find(item => item[0] === params.get('screen'));
  const service = useMemo(() => createMockService(memoryStorage(demoFixture(screen?.[2] ?? 'start'))), [screen]);
  if (!screen) return <main className={s.demo}><Link to="/">← К помощнику</Link><h1>Состояния интерфейса</h1><p>22 экрана из Figma. Данные в этом режиме изолированы от вашей истории.</p><div className={s.demoGrid}>{demoScreens.map(([id, title], i) => <Link to={`/demo?screen=${encodeURIComponent(id)}`} key={id}><span>{String(i + 1).padStart(2, '0')}</span><strong>{title}</strong><span>{id}</span></Link>)}</div></main>;
  if (screen[2] === 'archive') return <Archive service={service} demo />;
  return <Workspace key={screen[0]} service={service} demoState={screen[2]} />;
}

export function Workspace({ service = appService, demoState }: { service?: ChatService; demoState?: DemoState }) {
  const { sessionId } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const [sessions, setSessions] = useState<Session[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [demoId, setDemoId] = useState(demoState === 'readonly' ? 'snt' : 'demo-current');
  const [draft, setDraft] = useState('');
  const [pendingId, setPendingId] = useState<string>();
  const [progress, setProgress] = useState<TurnProgress>();
  const operationRef = useRef<{ id: string; controller: AbortController } | undefined>(undefined);
  const mounted = useRef(true);
  const [submitting, setSubmitting] = useState(false);
  const [collapsed, setCollapsed] = useState(demoState === 'collapsed');
  const [debug, setDebug] = useState(demoState?.startsWith('debug') || demoState === 'scope');
  const [mobileHistory, setMobileHistory] = useState(false);
  const [mobileDebug, setMobileDebug] = useState(false);
  const [resetOpen, setResetOpen] = useState(demoState === 'reset');
  const [details, setDetails] = useState<Product>();
  const [error, setError] = useState<string>();
  const [leadError, setLeadError] = useState<string>();
  const [demoTyping, setDemoTyping] = useState(demoState === 'typing');
  const [trace, setTrace] = useState<DebugInfo>();
  const [traceError, setTraceError] = useState<string>();
  const [traceLoading, setTraceLoading] = useState(false);
  const [selectedMessage, setSelectedMessage] = useState<string>();
  const [clock, setClock] = useState(Date.now());
  const busyRef = useRef(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const nearBottom = useRef(true);
  const currentId = demoState ? demoId : sessionId;
  const session = sessions.find(item => item.id === currentId);
  const readonly = !!session?.archived;
  const api = service.mode === 'api';
  const canDebug = !api || !!service.reviewAvailable;
  const failed = session?.messages.find(m => m.delivery === 'failed');
  const typing = demoTyping || !!pendingId && pendingId === currentId;
  const stage = session?.stage ?? 'start';
  const emptyWelcome = demoState === 'empty' && session?.stage === 'start';
  const start = (!session || session.messages.length === 0 || api && session.stage === 'start' && !session.messages.some(m => m.role === 'user')) && !emptyWelcome;
  const latestBot = [...(session?.messages ?? [])].reverse().find(m => m.role === 'bot');
  const replies = api ? [] : quickReplies(stage);
  useEffect(() => {
    let alive = true;
    const prepare = async () => {
      if (service.initialize) {
        const initial = await service.initialize();
        if (alive && currentId !== initial.id) navigate(`/chat/${initial.id}`, { replace: true });
      }
      const items = await service.listSessions();
      if (alive) { setSessions(items); setLoaded(true); }
    };
    prepare().catch(err => { if (alive) { setError(errorText(err)); setLoaded(true); } });
    return () => { alive = false; };
  }, [service]);
  useEffect(() => {
    setClock(Date.now());
    if (!failed?.retryAt || failed.retryAt <= Date.now()) return;
    const timer = setInterval(() => { setClock(Date.now()); if (Date.now() >= failed.retryAt!) clearInterval(timer); }, 500);
    return () => clearInterval(timer);
  }, [failed?.retryAt]);
  useEffect(() => {
    if (!api || !debug || !canDebug || !service.getTrace) return;
    const id = selectedMessage ?? (latestBot?.seq !== 1 ? latestBot?.id : undefined);
    if (!id) { setTrace(undefined); setTraceError('Источники появятся после первого ответа.'); return; }
    let alive = true; setTrace(undefined); setTraceError(undefined); setTraceLoading(true);
    service.getTrace(id).then(value => { if (alive) setTrace(value); }).catch(err => { if (alive) setTraceError(errorText(err)); }).finally(() => { if (alive) setTraceLoading(false); });
    return () => { alive = false; };
  }, [api, debug, canDebug, selectedMessage, latestBot?.id, service]);
  useEffect(() => {
    setDraft(''); setMobileHistory(false); setError(undefined); setSelectedMessage(undefined); nearBottom.current = true;
    const operation = operationRef.current;
    if (operation && operation.id !== currentId) {
      operation.controller.abort(); operationRef.current = undefined;
      setPendingId(undefined); setProgress(undefined); busyRef.current = false;
    }
  }, [currentId]);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; operationRef.current?.controller.abort(); };
  }, []);
  useEffect(() => {
    if (nearBottom.current) bottomRef.current?.scrollIntoView?.({ block: 'end' });
  }, [session?.messages.length, session?.stage, typing, currentId, debug, loaded]);
  const select = (id: string) => { if (demoState) setDemoId(id); else navigate(`/chat/${id}`); setMobileHistory(false); };
  async function refresh() { setSessions(await service.listSessions()); }
  async function send(text = draft, retryId?: string) {
    if (busyRef.current || readonly || !text.trim()) return;
    busyRef.current = true; setError(undefined); nearBottom.current = true;
    setSelectedMessage(undefined);
    let operation: { id: string; controller: AbortController } | undefined;
    try {
      let active = session;
      if (!active) { active = await service.createSession(); await refresh(); select(active.id); }
      setPendingId(active.id); setDraft(''); setDemoTyping(false);
      setProgress(undefined);
      operation = { id: active.id, controller: new AbortController() };
      operationRef.current = operation;
      // Optimistic feedback; service retains the canonical persisted message.
      if (!retryId) setSessions(prev => prev.map(item => item.id === active!.id ? { ...item, messages: [...item.messages, { id: 'optimistic', role: 'user', text: text.trim(), delivery: 'sent' }] } : item));
      await service.sendMessage(active.id, text, { retryId, simulateError: !navigator.onLine, signal: operation.controller.signal, onProgress: value => {
        if (mounted.current && operationRef.current === operation && !operation!.controller.signal.aborted) setProgress(value);
      } });
      if (mounted.current) await refresh();
    } catch (err) {
      if (mounted.current && !operation?.controller.signal.aborted) { setError(errorText(err)); setDraft(text); await refresh().catch(() => undefined); }
    } finally {
      if (!operation || operationRef.current === operation) {
        operationRef.current = undefined; busyRef.current = false;
        if (mounted.current) { setPendingId(undefined); setProgress(undefined); }
      }
    }
  }
  async function newDialog(fromId?: string) {
    if (busyRef.current) return;
    busyRef.current = true;
    try {
      const created = await service.createSession(fromId);
      await refresh(); setResetOpen(false); setDraft(''); setDemoTyping(false); select(created.id);
    } catch (err) { setError(errorText(err)); }
    finally { busyRef.current = false; }
  }
  async function submitLead(input: LeadInput) {
    if (!session || submitting) return;
    setSubmitting(true); setLeadError(undefined);
    try { await service.submitLead(session.id, input); await refresh(); }
    catch (err) { setLeadError(errorText(err)); await refresh().catch(() => undefined); }
    finally { setSubmitting(false); }
  }
  async function confirmLead() {
    if (!session || !service.confirmLead || submitting) return;
    setSubmitting(true); setLeadError(undefined);
    try { await service.confirmLead(session.id); await refresh(); }
    catch (err) { setLeadError(errorText(err)); await refresh().catch(() => undefined); }
    finally { setSubmitting(false); }
  }
  const requestNew = () => { if (session?.messages.length && !readonly) setResetOpen(true); else void newDialog(); };
  const openDebug = () => { if (canDebug) { setDebug(true); setMobileDebug(true); } };
  const composer = <Composer value={draft} setValue={setDraft} onSend={() => void send()} busy={!loaded || !!pendingId || demoTyping || submitting || !!failed || !!session?.accessError} sending={!!pendingId && !demoTyping} compact={start} />;
  const retryBlocked = !!pendingId || failed?.retryable === false || (failed?.retryAt ?? 0) > clock;
  return <div className={s.app}>
    <Header onAssistant={() => { const active = sessions.find(item => !item.archived); if (active) select(active.id); else if (!demoState) navigate('/'); }} />
    <div className={s.mobileToolbar}><IconButton icon="collapse" label="Открыть историю диалогов" onClick={() => setMobileHistory(true)} /><span>Помощник Energytool</span>{canDebug && <IconButton icon="document" label="Открыть отладку" onClick={openDebug} />}</div>
    <div className={`${s.workspace} ${debug ? s.withDebug : ''}`}>
      <Sidebar sessions={sessions} currentId={currentId} collapsed={collapsed} mobileOpen={mobileHistory} debug={!!debug} debugAvailable={canDebug} busy={!loaded || !!pendingId || submitting} onSelect={select} onNew={requestNew} onCollapse={() => setCollapsed(!collapsed)} onClose={() => setMobileHistory(false)} onDebug={() => { setDebug(!debug); setMobileDebug(!debug); setMobileHistory(false); }} />
      <main className={`${s.chat} ${start ? s.startChat : ''}`} aria-label="Диалог с помощником">
        {!loaded ? <div className={s.loading} role="status">Загружаем диалоги…</div> : currentId && !session ? <div className={s.loading}><h2>Диалог не найден</h2><Button onClick={() => demoState ? setDemoId('demo-current') : navigate('/')}>Начать новый диалог</Button></div> : start ? <div className={s.start}>
          <div className={s.startHeading}><h1>{api ? 'Помощник МТС EnergyTool' : 'Здравствуйте! Я помощник МТС EnergyTool'}</h1><p>{api ? session?.greeting || 'Создайте новый диалог, чтобы начать консультацию.' : 'Расскажите своими словами, что вас беспокоит с электроэнергией, а я подскажу, чем можем помочь. Если не найду ответ, подключу специалиста'}</p></div>
          {composer}{!api && <div className={s.suggestions}><p>Подобрано по разделу «Аномалии»</p><div>{suggestions.map(text => <button key={text} onClick={() => void send(text)} disabled={!!pendingId}>{text}</button>)}</div></div>}
          {api && !session && <Button onClick={() => void newDialog()}>Повторить подключение</Button>}
        </div> : <>
          <div className={s.messages} ref={scrollRef} role="log" aria-label="Сообщения" aria-live="polite" onScroll={() => { const el = scrollRef.current; if (el) nearBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80; }}>
            <div className={s.messageStack}>
              {emptyWelcome && <><div className={s.message}><span className={`${s.avatar} ${s.messageAvatar}`}><Icon name="bot" /></span><p className={s.bubble}>Здравствуйте! Я помощник МТС EnergyTool<br />Расскажите своими словами, что вас беспокоит с электроэнергией, а я подскажу, чем можем помочь. Если не найду ответ, подключу специалиста.</p></div><div className={s.quickReplies}>{['Вырос счёт за электроэнергию', 'Учёт по цехам и объектам', 'Счётчики для товарищества', 'Найти хищения в сети'].map(text => <button key={text} onClick={() => void send(text)}>{text}</button>)}</div><p className={s.emptyHint}>Или опишите ситуацию своими словами</p></>}
              {session?.messages.map(message => <div className={s.messageGroup} key={message.id}>
                {message.note && <p className={s.topicNote}>{message.note}</p>}
                <div className={`${s.message} ${message.role === 'user' ? s.userMessage : ''}`}>
                  {message.role === 'bot' && <span className={`${s.avatar} ${s.messageAvatar}`}><Icon name="bot" /></span>}
                  <div className={`${s.bubble} ${selectedMessage === message.id ? s.inspectedMessage : ''}`}>
                    {message.text}{api && canDebug && debug && message.role === 'bot' && <button className={s.inspectMessage} aria-label="Показать источники ответа" onClick={() => setSelectedMessage(message.id)}><Icon name="document" /></button>}
                  </div>
                </div>
                {message.delivery === 'failed' && <button className={s.failedBadge} onClick={() => void send(message.text, message.id)} disabled={retryBlocked}>{retryBlocked && message.retryable === false ? 'Отправку не удалось завершить' : 'Не отправлено. Нажмите, чтобы повторить'}</button>}
              </div>)}
              {typing && <TurnIndicator progress={progress} />}
              {!typing && failed && <><div className={s.message}><span className={`${s.avatar} ${s.messageAvatar}`}><Icon name="bot" /></span><p className={s.bubble}>{failed.error || 'Нет связи. Ваше сообщение сохранено. Проверьте интернет и повторите'}</p></div>{session?.accessError ? <Button className={s.retry} onClick={requestNew}>Новый диалог</Button> : <Button className={s.retry} disabled={retryBlocked} onClick={() => void send(failed.text, failed.id)}>{(failed.retryAt ?? 0) > clock ? `Повторить через ${Math.ceil(((failed.retryAt ?? 0) - clock) / 1000)} с` : 'Повторить'}</Button>}</>}
              {!typing && !failed && session && <>
                {!api && ['products', 'solar'].includes(stage) && <ProductCards products={productsFor(stage).map(p => debug && p.id === 'platform' ? { ...p, description: p.description.replace(' Ограничений по числу приборов нет.', '') } : p)} onDetails={setDetails} readonly={readonly} />}
                {stage === 'form' && !readonly && <div className={s.leadRow}><LeadForm key={`${session.id}-${session.draft?.revision ?? 0}`} onSubmit={submitLead} pending={submitting} error={leadError} api={api} showPhoneError={demoState === 'phone-error'} initial={api ? { fullName: session.draft?.full_name ?? '', phone: session.draft?.phone ?? '', company: session.draft?.company ?? '', consent: false } : demoState === 'phone-error' ? { fullName: 'Иванов Иван Иванович', phone: '+7 900 000-00', company: 'ООО «Вечерний свет»', consent: true } : demoState === 'form' ? { fullName: '', phone: '', company: '', consent: true } : undefined} /></div>}
                {stage === 'sent' && session.lead && <div className={s.leadRow}><SentLead session={session} /></div>}
                {api && session.preview && <div className={s.leadRow}><ApiLeadCard record={session.preview} pending={submitting} error={leadError} onConfirm={() => void confirmLead()} onEdit={() => { void service.editLead?.(session.id).then(() => { setLeadError(undefined); void refresh(); }); }} /></div>}
                {api && session.apiLead && <div className={s.leadRow}><ApiLeadCard record={session.apiLead} saved message={session.confirmationMessage} /></div>}
                {!!replies.length && !readonly && demoState !== 'debug-meter' && <div className={s.quickReplies}>{replies.map(text => <button key={text} onClick={() => void send(text)}>{text}</button>)}</div>}
              </>}
              <div ref={bottomRef} className={s.scrollAnchor} />
            </div>
          </div>
          {readonly ? <div className={s.readonlyBar}><p>Прошлый диалог от {new Date(session!.createdAt).toLocaleDateString('ru-RU')}. Только чтение</p><Button primary onClick={() => void newDialog(api ? undefined : session!.id)}>{api ? 'Начать новый диалог' : 'Продолжить в новом диалоге'}</Button></div> : composer}
        </>}
        {error && <div className={s.errorNotice} role="alert"><span>{error}</span><button onClick={() => setError(undefined)} aria-label="Закрыть сообщение об ошибке">×</button></div>}
      </main>
      {debug && canDebug && <DebugPanel info={api ? trace : latestBot?.debug ?? getLastDebug(session)} loading={traceLoading} error={traceError} mobileOpen={mobileDebug} onClose={() => { setMobileDebug(false); setDebug(false); }} />}
    </div>
    {resetOpen && <Modal title="Начать новый диалог?" onClose={() => setResetOpen(false)} actions={<><Button onClick={() => setResetOpen(false)}>Остаться</Button><Button primary onClick={() => void newDialog()}>Начать заново</Button></>}><p>Текущий разговор закроется, и я забуду, о чём мы говорили. Он останется в истории.</p></Modal>}
    {details && <Modal title={details.name} onClose={() => setDetails(undefined)} actions={<><Button onClick={() => setDetails(undefined)}>Закрыть</Button><Button primary onClick={() => { setDetails(undefined); void send('Сколько это стоит?'); }}>Обсудить стоимость</Button></>}><p>{details.subtitle}</p><p>{details.description}</p></Modal>}
    {demoState && <a className={s.demoBack} href="/demo" aria-label="Все состояния">Все состояния</a>}
    <span className={s.srOnly}>{location.pathname}</span>
  </div>;
}
function errorText(err: unknown) { return errorWithRequest(err); }
