import { archiveSeed, copy, debugFor, makeSession, msg, seedSessions } from './data';
import type { ChatService, LeadInput, Session, Stage } from './types';

export const STORAGE_KEY = 'energytool.mock.v1';
const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v));
export const pause = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms));
export function validateLead(input: LeadInput) {
  const errors: Partial<Record<keyof LeadInput, string>> = {};
  if (!input.fullName.trim()) errors.fullName = 'Введите ФИО';
  const digits = input.phone.replace(/\D/g, '');
  if (digits.length !== 11 || !/^[78]/.test(digits)) errors.phone = 'Проверьте номер: нужно 11 цифр, например +7 900 000-00-00';
  if (!input.company.trim()) errors.company = 'Введите название компании';
  if (!input.consent) errors.consent = 'Для отправки заявки нужно согласие на обработку данных';
  return errors;
}
export function createMockService(storage: Storage = localStorage, latency = 700): ChatService {
  function read(): Session[] {
    try {
      const raw = storage.getItem(STORAGE_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed) && parsed.every(s => typeof s.id === 'string' && Array.isArray(s.messages) && s.context)) return parsed;
      }
    } catch { /* A damaged local cache is replaced with the demo fixtures. */ }
    const initial = seedSessions();
    write(initial);
    return initial;
  }
  function write(sessions: Session[]) {
    storage.setItem(STORAGE_KEY, JSON.stringify(sessions));
  }
  function update(session: Session) {
    const sessions = read();
    const index = sessions.findIndex(s => s.id === session.id);
    if (index < 0) throw new Error('Диалог не найден');
    sessions[index] = session;
    write(sessions);
    return clone(session);
  }
  function get(id: string) {
    const session = read().find(s => s.id === id);
    if (!session) throw new Error('Диалог не найден');
    return session;
  }
  return {
    async listSessions() { return clone(read()); },
    async createSession(fromId) {
      const sessions = read();
      const from = fromId ? sessions.find(s => s.id === fromId) : undefined;
      if (fromId && !from) throw new Error('Диалог не найден');
      sessions.forEach(s => { s.archived = true; });
      const next = makeSession();
      if (from) {
        next.title = from.title;
        next.context = clone(from.context);
        next.messages = clone(from.messages).map(m => ({ ...m, id: crypto.randomUUID() }));
        next.stage = ['form', 'sent'].includes(from.stage) ? 'offer' : from.stage;
      }
      write([next, ...sessions]);
      return clone(next);
    },
    async sendMessage(id, text, options = {}) {
      const session = get(id);
      if (session.archived) throw new Error('Прошлый диалог доступен только для чтения');
      const value = text.trim();
      if (!value) return clone(session);
      const existing = options.retryId ? session.messages.find(m => m.id === options.retryId) : undefined;
      if (options.retryId && (!existing || existing.role !== 'user')) throw new Error('Сообщение не найдено');
      if (existing?.delivery === 'sent') return clone(session);
      const message = existing ?? msg('user', value);
      message.delivery = options.simulateError ? 'failed' : 'sent';
      if (!existing) session.messages.push(message);
      if (session.title === 'Новый диалог') session.title = titleFor(value);
      if (options.simulateError) return update(session);
      await pause(latency);
      const [stage, answer, note] = reply(session, value);
      session.stage = stage;
      if (stage === 'solar') session.context.topic = 'solar';
      if (stage === 'return') session.context.topic = 'bill';
      if (stage === 'products') session.context.meter = value;
      if (stage === 'form') session.status = 'wait';
      if (stage === 'nokb') session.status = 'nokb';
      if (stage === 'scope') session.status = 'specialist';
      session.messages.push({ ...msg('bot', answer, stage), note });
      return update(session);
    },
    async submitLead(id, input) {
      const session = get(id);
      if (session.archived) throw new Error('Прошлый диалог доступен только для чтения');
      if (session.lead) return clone(session);
      if (session.stage !== 'form') throw new Error('Сначала откройте форму заявки');
      if (Object.keys(validateLead(input)).length) throw new Error('Проверьте контактные данные');
      await pause(latency / 2);
      session.lead = { ...input, fullName: input.fullName.trim(), company: input.company.trim(), phone: input.phone.trim(), id: crypto.randomUUID(), sessionId: id, createdAt: new Date().toISOString(), summary: session.messages.map(m => `${m.role === 'user' ? 'Клиент' : 'Помощник'}: ${m.text}`).join('\n\n') };
      session.status = 'request';
      session.stage = 'sent';
      return update(session);
    },
    async listArchive() {
      // First page mirrors Figma, further pages contain illustrative rows for pagination.
      return [...archiveSeed, ...Array.from({ length: 180 }, (_, i) => ({ ...archiveSeed[i % 7], id: `extra-${i}`, name: `devicelist_${String(i + 1).padStart(4, '0')}`, date: new Date(2023, 3, i + 1, 10, i % 60).toISOString() }))];
    },
    async downloadArchive(id) {
      const files = await this.listArchive();
      const file = files.find(f => f.id === id);
      if (!file) throw new Error('Файл не найден');
      return new Blob(['\uFEFF', `Файл;Дата загрузки;Пользователь\r\n${file.name};${file.date};${file.user}\r\n\r\nДемонстрационные данные;Реальных показаний приборов нет;\r\n`], { type: 'text/csv;charset=utf-8' });
    },
  };
}
function titleFor(text: string) {
  if (/снт|товариществ|домов/i.test(text)) return 'Счётчики для СНТ, 120 домов';
  if (/закон|должник|суд/i.test(text)) return 'Отключение должников: законно ли';
  if (/заряд|электромобил/i.test(text)) return 'Зарядные станции для электромобилей';
  if (/сч[её]т|вечер|расход/i.test(text)) return 'Вырос счёт: вечерняя нагрузка';
  return text.length > 55 ? text.slice(0, 52) + '…' : text;
}
function reply(s: Session, text: string): [Stage, string, string?] {
  const t = text.toLowerCase();
  if (/переда[йт]|передать|связаться|менеджер|специалист/.test(t) && !/не |нет/.test(t)) return ['form', copy.form];
  if (/заряд|электромобил|погода|курс валют|футбол/.test(t)) return ['nokb', copy.nokb];
  if (/закон|без суда|накажут|юридич/.test(t)) return ['scope', copy.scope];
  if (/верн[её]м|назад к|первой теме/.test(t)) return ['return', copy.return, 'Помню, о чём мы говорили: вечерняя нагрузка, обычный счётчик'];
  if (/солнеч|панел|накопител/.test(t)) return ['solar', copy.solar, 'Новая тема: солнечные станции'];
  if (/снт|товариществ/.test(t) && s.stage === 'start') { s.context.topic = 'snt'; return ['clarify', copy.snt]; }
  if (s.context.topic === 'snt' && /120|дом|сч[её]тчик/.test(t)) return ['offer', copy.sntOffer];
  if (/сколько|стоит|цен[ау]|стоимост|расч[её]т/.test(t) && s.stage !== 'start') return ['offer', copy.offer];
  if (/обычн|почасов|показани/.test(t) && s.stage !== 'start') return ['products', copy.products];
  if (s.stage === 'products' || s.stage === 'solar' || s.stage === 'return') return ['offer', copy.offer];
  if (s.stage === 'clarify' || /где.*посмотр|категори/.test(t) && s.stage !== 'start') return ['meter', copy.meter];
  if (s.stage === 'meter') return ['products', copy.products];
  if (s.stage === 'offer' && /да|соглас|хочу/.test(t)) return ['form', copy.form];
  if (s.stage === 'offer') return ['products', copy.products];
  if (s.stage === 'sent') return ['clarify', 'Заявка уже сохранена. Можем обсудить другую задачу: что вас интересует?', undefined];
  return ['clarify', copy.clarify];
}

export const getLastDebug = (session?: Session) => [...(session?.messages ?? [])].reverse().find(m => m.debug)?.debug ?? debugFor('clarify');
export const chatService = createMockService();
