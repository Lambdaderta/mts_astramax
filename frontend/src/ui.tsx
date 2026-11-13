import { useEffect, useRef, useState, type ButtonHTMLAttributes, type ReactNode, type RefObject } from 'react';
import { Link } from 'react-router-dom';
import type { ApiLead, ApiPreview, DebugInfo, LeadInput, Product, Session, SessionStatus } from './types';
import { validateLead } from './service';
import s from './App.module.css';

const icons = { sparkle: 'bd9fe', bot: '59236', bell: 'd65a4', apps: '02b7e', user: '88c32', collapse: '28c66', search: 'a7f89', send: '109f7', sendActive: '0ae4f', sendBusy: 'da84e', building: 'a38c0', document: '2b387', success: 'b77e2', check: 'f1fef', new: '1c301', down: '5b33e', paginationDown: '027ef', next: 'a5c2a', breadcrumb: '5fa29', activity: '91793', settings: '2c9b2', assistant: '540ef', chevron: '8f94a', smallDown: '5468a' };
export function Icon({ name }: { name: keyof typeof icons }) { return <img className={s.icon} src={`/assets/${icons[name]}.svg`} alt="" aria-hidden="true" />; }
export function IconButton({ icon, label, className = '', ...props }: { icon: keyof typeof icons; label: string } & ButtonHTMLAttributes<HTMLButtonElement>) {
  return <button type="button" className={`${s.iconButton} ${className}`} aria-label={label} title={label} {...props}><Icon name={icon} /></button>;
}
function usePanelFocus(ref: RefObject<HTMLElement | null>, open: boolean, onClose: () => void) {
  const close = useRef(onClose); close.current = onClose;
  useEffect(() => {
    if (!open || window.innerWidth > 760) return;
    const previous = document.activeElement as HTMLElement | null;
    const nodes = () => Array.from(ref.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),a[href]') ?? []).filter(el => el.getBoundingClientRect().width > 0);
    nodes()[0]?.focus();
    function key(e: KeyboardEvent) {
      if (e.key === 'Escape') { e.preventDefault(); close.current(); }
      if (e.key !== 'Tab') return;
      const list = nodes(); const first = list[0]; const last = list.at(-1);
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); }
      if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
    }
    document.addEventListener('keydown', key);
    return () => { document.removeEventListener('keydown', key); previous?.focus(); };
  }, [ref, open]);
}
export function Button({ children, primary = false, className = '', ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { primary?: boolean }) {
  return <button type="button" className={`${s.button} ${primary ? s.primary : ''} ${className}`} {...props}>{children}</button>;
}
export function Header({ active = true, onAssistant }: { active?: boolean; onAssistant: () => void }) {
  const [menu, setMenu] = useState<string>();
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const close = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setMenu(undefined); };
    const escape = (e: KeyboardEvent) => { if (e.key === 'Escape') setMenu(undefined); };
    document.addEventListener('click', close); document.addEventListener('keydown', escape);
    return () => { document.removeEventListener('click', close); document.removeEventListener('keydown', escape); };
  }, []);
  return <header className={s.header}>
    <Link to="/archive" className={s.logo} aria-label="МТС Energytool — архив результатов"><span className={s.mark}><span>М</span><span>Т</span><span>С</span></span><span>ENERGYTOOL</span></Link>
    <div className={s.headerActions} ref={ref}>
      <button className={`${s.assistantButton} ${active ? s.assistantActive : ''}`} onClick={onAssistant}><Icon name={active ? 'sparkle' : 'assistant'} /><span>Помощник</span></button>
      <IconButton icon="bell" label="Уведомления" className={s.headerIcon} aria-expanded={menu === 'bell'} onClick={() => setMenu(menu === 'bell' ? undefined : 'bell')} />
      <IconButton icon="apps" label="Приложения" className={s.headerIcon} aria-expanded={menu === 'apps'} onClick={() => setMenu(menu === 'apps' ? undefined : 'apps')} />
      <IconButton icon="user" label="Профиль" aria-expanded={menu === 'user'} onClick={() => setMenu(menu === 'user' ? undefined : 'user')} />
      {menu && <div className={s.popover} role="region" aria-label={menu === 'bell' ? 'Уведомления' : menu === 'apps' ? 'Приложения' : 'Профиль'}>
        {menu === 'bell' ? <><h3>Уведомления</h3><p>Новых уведомлений нет</p></> : menu === 'user' ? <><h3>Личный кабинет</h3><p>Демонстрационный профиль</p></> : <><Link to="/archive" onClick={() => setMenu(undefined)}>Архив результатов</Link><button onClick={() => { setMenu(undefined); onAssistant(); }}>Помощник Energytool</button></>}
      </div>}
    </div>
  </header>;
}
const statuses: Record<SessionStatus, [string, string]> = { none: ['Без заявки', 'neutral'], wait: ['Ждёт контактов', 'violet'], specialist: ['Передано специалисту', 'violet'], nokb: ['Без ответа', 'red'], request: ['Заявка', 'success'] };
export function Badge({ children, tone = 'neutral' }: { children: ReactNode; tone?: string }) { return <span className={`${s.badge} ${s[tone] ?? ''}`}>{children}</span>; }
export const formatDate = (value: string) => new Intl.DateTimeFormat('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(value));
export function Sidebar({ sessions, currentId, collapsed, mobileOpen, debug, busy, debugAvailable = true, includeEmpty = false, onSelect, onNew, onCollapse, onClose, onDebug }: {
  sessions: Session[]; currentId?: string; collapsed: boolean; mobileOpen: boolean; debug: boolean; busy: boolean;
  debugAvailable?: boolean; includeEmpty?: boolean;
  onSelect: (id: string) => void; onNew: () => void; onCollapse: () => void; onClose: () => void; onDebug: () => void;
}) {
  const [searching, setSearching] = useState(false);
  const [query, setQuery] = useState('');
  const panelRef = useRef<HTMLElement>(null);
  usePanelFocus(panelRef, mobileOpen, onClose);
  const visible = sessions.filter(session => (includeEmpty || session.messages.length) && session.title.toLocaleLowerCase('ru').includes(query.toLocaleLowerCase('ru')));
  return <>
    {mobileOpen && <button className={s.mobileScrim} aria-label="Закрыть историю" onClick={onClose} />}
    <aside ref={panelRef} className={`${s.sidebar} ${collapsed ? s.collapsed : ''} ${mobileOpen ? s.mobileOpen : ''}`} aria-label="История диалогов">
      <div className={s.sideTop}>
        <div className={s.sidebarHeading}><span className={s.avatar}><Icon name="bot" /></span><div className={s.sidebarTitles}><h3>Помощник Energytool</h3><p>На связи</p></div><IconButton icon="collapse" label={collapsed ? 'Развернуть историю' : 'Свернуть историю'} onClick={onCollapse} /><button className={s.mobileClose} aria-label="Закрыть историю" onClick={onClose}>×</button></div>
        <div className={s.sideContent}>
          <Button onClick={onNew} disabled={busy} className={s.newDialog}><span className={s.expandedLabel}>Новый диалог</span><span className={s.collapsedLabel}><Icon name="new" /></span></Button>
          <div className={s.historyHeading}><h3>История диалогов</h3><IconButton icon="search" label="Поиск диалогов" aria-expanded={searching} onClick={() => { setSearching(!searching); setQuery(''); if (collapsed) onCollapse(); }} /></div>
          {searching && <input className={s.search} autoFocus type="search" placeholder="Найти диалог" aria-label="Найти диалог" value={query} onChange={e => setQuery(e.target.value)} />}
          <div className={s.sessions}>
            {visible.length ? visible.map(session => <button key={session.id} className={`${s.session} ${currentId === session.id ? s.selected : ''}`} onClick={() => onSelect(session.id)} title={session.title} aria-current={currentId === session.id ? 'true' : undefined}>
              <span className={s.sessionTexts}><span className={s.sessionTitle}>{session.title}</span><span className={s.caption}>{formatDate(session.createdAt)}</span></span><Badge tone={statuses[session.status][1]}>{statuses[session.status][0]}</Badge>
            </button>) : <div className={s.emptyHistory}>{query ? <p>Диалоги не найдены. Попробуйте другой запрос.</p> : <><p>Пока нет диалогов</p><p className={s.caption}>Начните разговор, и он появится здесь</p></>}</div>}
          </div>
        </div>
      </div>
      {debugAvailable && <label className={s.debugToggle}><span>Отладка</span><input type="checkbox" checked={debug} onChange={onDebug} /><span className={s.switch} /></label>}
    </aside>
  </>;
}
export function Composer({ value, setValue, onSend, busy, compact = false, sending = busy }: { value: string; setValue: (value: string) => void; onSend: () => void; busy: boolean; compact?: boolean; sending?: boolean }) {
  return <form className={`${s.composer} ${compact ? s.startComposer : ''}`} onSubmit={e => { e.preventDefault(); if (!busy && value.trim()) onSend(); }}>
    <textarea aria-label="Сообщение" rows={1} placeholder={sending ? 'Отправляем…' : 'Напишите сообщение'} value={value} onChange={e => setValue(e.target.value)} disabled={busy} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); if (!busy && value.trim()) onSend(); } }} />
    <button type="submit" className={s.send} disabled={busy || !value.trim()} aria-label="Отправить сообщение"><Icon name={sending ? 'sendBusy' : !busy && value.trim() ? 'sendActive' : 'send'} /></button>
  </form>;
}
export function ProductCards({ products, onDetails, readonly }: { products: Product[]; onDetails: (p: Product) => void; readonly: boolean }) {
  return <div className={s.products}>{products.map(product => <article key={product.id} className={s.product}>
    <Badge tone={product.recommended ? 'success' : 'violet'}>{product.recommended ? 'Лучше всего подходит' : 'Стандарт'}</Badge>
    <div className={s.productHeading}><span className={s.productIcon}><Icon name="building" /></span><div><h3>{product.name}</h3><p className={s.caption}>{product.subtitle}</p></div></div>
    <p>{product.description}</p><Button disabled={readonly} onClick={() => onDetails(product)}>Подробнее</Button>
  </article>)}</div>;
}
export const defaultContact: LeadInput = { fullName: '', phone: '', company: '', consent: false };
export function LeadForm({ onSubmit, pending, initial = defaultContact, showPhoneError = false, error, api = false }: { onSubmit: (input: LeadInput) => Promise<void>; pending: boolean; initial?: LeadInput; showPhoneError?: boolean; error?: string; api?: boolean }) {
  const [input, setInput] = useState(initial);
  const [errors, setErrors] = useState<Partial<Record<keyof LeadInput, string>>>(showPhoneError ? { phone: 'Проверьте номер: нужно 11 цифр, например +7 900 000-00-00' } : {});
  const firstError = useRef<HTMLFormElement>(null);
  const update = (key: keyof LeadInput, value: string | boolean) => { setInput(prev => ({ ...prev, [key]: value })); setErrors(prev => ({ ...prev, [key]: undefined })); };
  return <form className={s.leadForm} ref={firstError} noValidate onSubmit={async e => {
    e.preventDefault(); const next = api ? validateApiContact(input) : validateLead(input); setErrors(next);
    if (Object.keys(next).length) { const name = Object.keys(next)[0]; firstError.current?.querySelector<HTMLInputElement>(`[name="${name}"]`)?.focus(); return; }
    await onSubmit(input);
  }}>
    <div className={s.leadIntro}><h2>Контакты для связи</h2><p>{api ? 'Для заявки достаточно телефона. Имя и компанию можно добавить позже. Перед сохранением покажем резюме.' : 'Менеджер пресейла свяжется с вами в рабочее время и обсудит задачу.'}</p></div>
    <div className={s.fields}>{(['fullName', 'phone', 'company'] as const).map((key, i) => <label className={s.field} key={key}><span id={`${key}-label`}>{['ФИО', 'Телефон', 'Компания'][i]}</span><input name={key} aria-labelledby={`${key}-label`} type={key === 'phone' ? 'tel' : 'text'} autoComplete={key === 'fullName' ? 'name' : key === 'phone' ? 'tel' : 'organization'} inputMode={key === 'phone' ? 'tel' : undefined} placeholder={['Иванов Иван Иванович', '+7 900 000-00-00', 'ООО «Вечерний свет»'][i]} value={input[key]} onChange={e => update(key, e.target.value)} aria-invalid={!!errors[key]} aria-describedby={errors[key] ? `${key}-error` : undefined} disabled={pending} />{errors[key] && <span className={s.fieldError} id={`${key}-error`}>{errors[key]}</span>}</label>)}
      <label className={s.consent}><input name="consent" type="checkbox" checked={input.consent} onChange={e => update('consent', e.target.checked)} disabled={pending} aria-describedby={errors.consent ? 'consent-error' : undefined} /><span className={s.checkbox}>{input.consent && <Icon name="check" />}</span><span>Согласен на обработку персональных данных</span></label>
      {errors.consent && <p id="consent-error" className={s.fieldError} role="alert">{errors.consent}</p>}
    </div>
    {error && <p className={s.fieldError} role="alert">{error}</p>}
    <Button type="submit" primary disabled={pending}>{pending ? api ? 'Готовим резюме…' : 'Отправляем заявку…' : api ? 'Проверить заявку' : 'Отправить заявку'}</Button>
  </form>;
}
export function validateApiContact(input: LeadInput) {
  const errors: Partial<Record<keyof LeadInput, string>> = {};
  const digits = input.phone.replace(/\D/g, '');
  if (input.phone.trim() && (/[^\d\s()+.-]/u.test(input.phone) || digits.length < 7 || digits.length > 15 || (!input.phone.trim().startsWith('+') && !(digits.length === 11 && /^[78]/.test(digits))))) errors.phone = 'Введите телефон в международном формате: + и от 7 до 15 цифр.';
  if (input.fullName.length > 200) errors.fullName = 'ФИО должно быть не длиннее 200 символов';
  if (input.company.length > 300) errors.company = 'Название компании должно быть не длиннее 300 символов';
  if (!input.consent) errors.consent = 'Для продолжения нужно согласие на обработку данных';
  return errors;
}
export function ApiLeadCard({ record, saved = false, message, pending, error, onConfirm, onEdit }: { record: ApiPreview | ApiLead; saved?: boolean; message?: string; pending?: boolean; error?: string; onConfirm?: () => void; onEdit?: () => void }) {
  const canConfirm = 'can_confirm' in record && record.can_confirm;
  const label: Record<string, string> = { full_name: 'ФИО', phone: 'Телефон', company: 'Компания' };
  return <div className={s.sentRow}>
    <section className={s.sentCard}>
      {saved && <span className={s.successIcon}><Icon name="success" /></span>}
      <div><h2>{saved ? 'Заявка сохранена' : 'Проверьте заявку'}</h2><p>{saved ? message || 'Заявка сохранена в демонстрационном прототипе.' : 'Проверьте контакты и резюме разговора перед подтверждением.'}</p></div>
      <Badge tone={saved ? 'success' : 'violet'}>{record.status === 'complete' ? 'Все контакты указаны' : record.status === 'partial' ? 'Частичные контакты' : 'Черновик'}</Badge>
      {!!record.missing_fields.length && <p>Не указано: {record.missing_fields.map(field => label[field] ?? field).join(', ')}.</p>}
      {!saved && <div className={s.previewActions}><Button primary disabled={pending || !canConfirm} onClick={onConfirm}>{pending ? 'Сохраняем…' : 'Подтвердить заявку'}</Button><Button disabled={pending} onClick={onEdit}>Изменить контакты</Button></div>}
      {error && <p role="alert" className={s.fieldError}>{error}</p>}
    </section>
    <section className={s.summary}><Badge tone="violet">Резюме для менеджера</Badge><h2>{record.client_problem}</h2><dl><div><dt>Резюме</dt><dd>{record.summary}</dd></div><div><dt>Что выяснено</dt><dd>{record.facts.length ? record.facts.map(fact => [fact.key, fact.value].filter(v => v != null).map(value => typeof value === 'object' ? JSON.stringify(value) : String(value)).join(': ')).join('\n') : 'Не указано'}</dd></div><div><dt>Что осталось неясным</dt><dd>{record.open_questions.join('\n') || 'Нет открытых вопросов'}</dd></div><div><dt>Продукты</dt><dd>{record.recommended_products.join('\n') || 'Не подобраны'}</dd></div><div><dt>Контакт</dt><dd>{[record.full_name, record.phone, record.company].filter(Boolean).join('\n') || 'Контакты не указаны'}</dd></div></dl></section>
  </div>;
}
export function SentLead({ session }: { session: Session }) {
  const solarTopic = session.context.topic === 'solar';
  const summary = session.context.topic === 'snt' ? ['Организовать учёт электроэнергии в СНТ', 'СНТ', 'Около 120 домов. Старые счётчики, показания собирают вручную.', 'Количество точек пилота и состав оборудования'] : [solarTopic ? 'Подобрать солнечную станцию для вечернего потребления' : 'Выяснить, почему вырос счёт за электроэнергию после роста вечерней нагрузки', 'Малый и средний бизнес', 'Нагрузка выросла вечером\nУчёт обычный, показания раз в месяц\nКлиент спрашивал стоимость', 'Присоединённая мощность\nТекущая ценовая категория\nЧисло точек учёта'];
  return <div className={s.sentRow}>
    <section className={s.sentCard}><span className={s.successIcon}><Icon name="success" /></span><div><h2>Заявка отправлена</h2><p>Менеджер свяжется с вами. Он уже видит краткое содержание нашего разговора, повторять ничего не придётся.</p></div><Badge tone="success">Передано менеджеру</Badge></section>
    <section className={s.summary}><Badge tone="violet">Резюме для менеджера</Badge><h2>{session.title}</h2><dl>{['Задача', 'Сегмент', 'Что выяснено', 'Что осталось неясным', 'Продукты', 'Контакт'].map((label, i) => <div key={label}><dt>{label}</dt><dd>{i < 4 ? summary[i] : i === 4 ? 'Облачная платформа учёта (основной). Обсуждалась гибридная солнечная станция с накопителями, клиент отложил.' : `${session.lead?.fullName}, ${session.lead?.phone}\n${session.lead?.company}`}</dd></div>)}</dl></section>
  </div>;
}
export function DebugPanel({ info, mobileOpen, onClose, loading = false, error }: { info?: DebugInfo; mobileOpen: boolean; onClose: () => void; loading?: boolean; error?: string }) {
  const panelRef = useRef<HTMLElement>(null);
  usePanelFocus(panelRef, mobileOpen, onClose);
  return <><button className={`${s.mobileScrim} ${!mobileOpen ? s.hidden : ''}`} aria-label="Закрыть отладку" onClick={onClose} /><aside ref={panelRef} className={`${s.debugPanel} ${mobileOpen ? s.debugMobileOpen : ''}`} aria-label="Режим отладки">
    <div><h3>Режим отладки</h3><p className={s.caption}>Показывает, откуда бот взял ответ</p><button className={s.mobileClose} onClick={onClose} aria-label="Закрыть отладку">×</button></div>
    {loading && <p role="status">Загружаем источники…</p>}{error && <p role="alert">{error}</p>}
    {info && <><dl className={s.debugStats}><div><dt>Время ответа</dt><dd><span className={`${s.timeBadge} ${info.responseMs < 2000 ? s.fast : info.responseMs > 5000 ? s.slow : ''}`}><span />{(info.responseMs / 1000).toLocaleString('ru-RU')} с</span></dd></div><div><dt>Уверенность</dt><dd><Badge tone={info.confidence === 'высокая' ? 'success' : info.confidence === 'низкая' ? 'red' : 'neutral'}>{info.confidence}</Badge></dd></div><div><dt>Уточняющий вопрос</dt><dd><Badge tone="violet">{info.clarification ? 'да' : 'нет'}</Badge></dd></div>{info.tag && <div><dt>Метка</dt><dd><Badge tone="red">{info.tag}</Badge></dd></div>}</dl>
    <div className={s.documents}><h4>Найденные документы</h4>{info.documents.length ? info.documents.map((doc, i) => <div className={s.sourceDetail} key={`${doc.path}-${doc.chunkId ?? i}`}><div className={s.document}><Icon name="document" /><div><p>{doc.path}</p><span className={s.caption}>{doc.chunkId || 'База знаний'}</span></div>{doc.score !== null && <Badge tone={doc.score >= 80 ? 'success' : 'violet'}>{doc.score}%</Badge>}</div>{doc.quote && <details><summary>Фрагмент источника</summary><p>{doc.quote}</p></details>}</div>) : <p className={s.noDocuments}>Подходящих документов не найдено</p>}</div>
    {info.reason && <div className={s.traceDetails}><p>Причина: {info.reason}</p><p>Поисковый запрос: {info.query}</p><p>Версия базы: {info.corpusVersion}</p>{info.timings && Object.entries(info.timings).map(([key, value]) => <p key={key}>{key}: {value} мс</p>)}</div>}</>}
  </aside></>;
}
export function Modal({ title, children, onClose, actions }: { title: string; children: ReactNode; onClose: () => void; actions: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose); closeRef.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const el = ref.current;
    const focusables = () => el?.querySelectorAll<HTMLElement>('button:not(:disabled),a[href],input:not(:disabled),[tabindex="0"]');
    focusables()?.[0]?.focus();
    function key(e: KeyboardEvent) {
      if (e.key === 'Escape') closeRef.current();
      if (e.key === 'Tab') { const nodes = focusables(); if (!nodes?.length) return; const first = nodes[0]; const last = nodes[nodes.length - 1]; if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); } else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); } }
    }
    document.addEventListener('keydown', key);
    return () => { document.removeEventListener('keydown', key); previous?.focus(); };
  }, []);
  return <div className={s.scrim} onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}><div className={s.modal} ref={ref} role="dialog" aria-modal="true" aria-labelledby="modal-title"><div><h2 id="modal-title">{title}</h2><div className={s.modalDescription}>{children}</div></div><div className={s.modalActions}>{actions}</div></div></div>;
}
