import type { ApiDraft, ApiLead, ChatService, DebugInfo, LeadInput, Message, Session, Stage } from '../types';
import { ApiError, createApiClient, errorWithRequest, type ApiClientOptions, type Schema } from './client';

type Turn = Schema['TurnCompleted'] | Schema['TurnProcessing'] | Schema['TurnFailed'];
type Pending = { clientId: string; text: string; turnId?: string };
export interface ApiServiceOptions extends ApiClientOptions {
  reviewEnabled?: boolean;
  reviewBaseUrl?: string;
  pollTimeoutMs?: number;
  wait?: (ms: number) => Promise<void>;
}
const clone = <T,>(value: T): T => structuredClone(value);
const encode = encodeURIComponent;
const endpoint = (id: string) => `/sessions/${encode(id)}`;
export function mapMessage(message: Schema['Message']): Message {
  return { id: message.id, role: message.role === 'assistant' ? 'bot' : 'user', text: message.text, seq: message.seq, ...(message.role === 'user' ? { delivery: 'sent' as const } : {}) };
}
export function mapSession(session: Schema['Session'], previous?: Session): Session {
  const phase: Record<Schema['Session']['phase'], Stage> = { new: 'start', clarification: 'clarify', consulting: 'consulting', collecting_contacts: 'form', reviewing_lead: 'preview', lead_saved: 'sent', closed: previous?.stage ?? 'consulting' };
  return { ...previous, id: session.id, title: previous?.title ?? 'Новый диалог', createdAt: session.opened_at, archived: session.status === 'closed', stage: phase[session.phase], status: session.phase === 'lead_saved' ? 'request' : ['collecting_contacts', 'reviewing_lead'].includes(session.phase) ? 'wait' : previous?.status ?? 'none', messages: previous?.messages ?? [], context: { topic: 'bill' }, source: 'api' };
}
export function mapTrace(trace: Schema['Trace']): DebugInfo {
  return { responseMs: trace.latency_ms, confidence: 'не указана', clarification: trace.decision.kind === 'clarify', tag: trace.decision.kind === 'insufficient_knowledge' ? 'нет в базе' : trace.decision.kind === 'out_of_scope' ? 'вне компетенции' : undefined, query: trace.retrieval_query, reason: trace.decision.reason_code, corpusVersion: trace.corpus_version, timings: trace.timings_ms, documents: trace.evidence.map(e => ({ path: e.document_id, score: e.relevance_score == null ? null : Math.round(e.relevance_score * 100), chunkId: e.chunk_id, quote: e.quote })) };
}
export function createApiService(options: ApiServiceOptions = {}): ChatService {
  const client = createApiClient(options);
  const reviewUrl = options.reviewBaseUrl || '/review-api/v1';
  if (!reviewUrl.startsWith('/') || reviewUrl.startsWith('//')) throw new Error('Review должен использовать защищённый прокси того же origin.');
  const review = createApiClient({ ...options, baseUrl: reviewUrl });
  const sessions = new Map<string, Session>();
  // Secrets and idempotency state deliberately live only in this JS instance.
  const tokens = new Map<string, string>();
  const pending = new Map<string, Pending>();
  const confirmations = new Map<string, string>();
  const wait = options.wait ?? ((ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms)));
  let initializing: Promise<Session> | undefined;
  let activeId: string | undefined;
  const save = (session: Session) => { sessions.set(session.id, session); return clone(session); };
  const get = (id: string) => { const value = sessions.get(id); if (!value) throw new ApiError('Сессия недоступна. Начните новый диалог.', 'SESSION_NOT_FOUND', 404, false); return value; };
  const token = (id: string) => { const value = tokens.get(id); if (!value) throw new ApiError('Сессия недоступна. Начните новый диалог.', 'UNAUTHORIZED', 401, false); return value; };
  const request = <T,>(id: string, path = '', method = 'GET', body?: unknown, timeoutMs?: number) => client.request<T>(endpoint(id) + path, { token: token(id), method, body, timeoutMs });
  const reviewRequest = <T,>(path: string) => {
    if (!options.reviewEnabled) throw new ApiError('Отладка доступна только в защищённом режиме проверки.', 'FORBIDDEN', 403, false);
    return review.request<T>(path, { credentials: 'same-origin' });
  };
  async function messages(id: string) {
    const all: Schema['Message'][] = []; let after = 0;
    do {
      const page = await request<Schema['MessagePage']>(id, `/messages?after_seq=${after}&limit=200`);
      all.push(...page.items);
      if (page.next_after_seq === null) break;
      if (page.next_after_seq <= after) throw new ApiError('Сервер вернул некорректную страницу истории.', 'INVALID_RESPONSE', 200, false);
      after = page.next_after_seq;
    } while (true);
    return [...new Map(all.map(item => [item.id, item])).values()].sort((a, b) => a.seq - b.seq).map(mapMessage);
  }
  async function poll(id: string, initial: Turn, deadline: number): Promise<Schema['TurnCompleted']> {
    let turn = initial;
    let failures = 0;
    while (turn.status === 'processing') {
      const remaining = deadline - Date.now();
      const delay = Math.max(50, Math.min(5000, turn.poll_after_ms ?? 700));
      if (remaining <= delay) throw new ApiError('Ответ ещё готовится. Повторите запрос, чтобы получить результат той же операции.', 'TURN_TIMEOUT');
      await wait(delay);
      try { turn = await request<Turn>(id, `/turns/${encode(turn.turn_id)}`, 'GET', undefined, Math.min(12_000, deadline - Date.now())); }
      catch (error) {
        if (!(error instanceof ApiError) || !error.retryable || Date.now() >= deadline || ++failures > 3) throw error;
        const delay = Math.max(Math.min(2000, 250 * 2 ** failures) + Math.random() * 100, error.retryAfterMs);
        if (Date.now() + delay >= deadline) throw error;
        await wait(delay);
      }
    }
    if (turn.status === 'failed') throw new ApiError(turn.error.message, turn.error.code, 503, turn.error.retryable, turn.error.request_id, turn.error.details);
    return turn;
  }
  async function applyTurn(result: Schema['TurnCompleted'], clientId?: string) {
    const previous = get(result.session.id);
    const session = mapSession(result.session, previous);
    const filtered = clientId ? session.messages.filter(m => m.id !== clientId && m.clientMessageId !== clientId) : session.messages;
    const returned = [mapMessage(result.user_message), mapMessage(result.assistant_message)];
    session.messages = [...new Map([...filtered, ...returned].map(m => [m.id, m])).values()].sort((a, b) => (a.seq ?? Number.MAX_SAFE_INTEGER) - (b.seq ?? Number.MAX_SAFE_INTEGER));
    if (session.title === 'Новый диалог') session.title = result.user_message.text.slice(0, 60);
    if (result.decision.kind === 'insufficient_knowledge') { session.stage = 'nokb'; session.status = 'nokb'; }
    if (result.decision.kind === 'out_of_scope') { session.stage = 'scope'; session.status = 'specialist'; }
    if (result.lead_draft) session.draft = result.lead_draft;
    // A new turn invalidates any preview, even if the contacts did not change.
    session.preview = undefined;
    save(session);
    if (session.stage === 'form' && !session.draft) session.draft = (await request<Schema['DraftResponse']>(session.id, '/lead-draft')).draft;
    if (session.stage === 'preview') session.preview = await request<Schema['Preview']>(session.id, '/lead-preview', 'POST');
    return save(session);
  }
  async function paginated<T>(path: string): Promise<T[]> {
    let offset = 0; const all: T[] = [];
    do {
      const page = await reviewRequest<{ items: T[]; next_offset: number | null }>(`${path}?offset=${offset}&limit=100`);
      all.push(...page.items);
      if (page.next_offset === null) return all;
      if (page.next_offset <= offset) throw new ApiError('Некорректная страница истории.', 'INVALID_RESPONSE', 200, false);
      offset = page.next_offset;
    } while (true);
  }
  const service: ChatService = {
    mode: 'api', reviewAvailable: !!options.reviewEnabled,
    async initialize() {
      if (activeId && sessions.has(activeId)) return clone(get(activeId));
      // React StrictMode must not create two independent sessions.
      initializing ??= service.createSession().finally(() => { initializing = undefined; });
      return initializing;
    },
    async listSessions() { return clone([...sessions.values()].reverse()); },
    async createSession() {
      if (activeId && sessions.has(activeId) && !get(activeId).archived) {
        const old = get(activeId);
        try { const response = await request<Schema['SessionResponse']>(activeId, '/close', 'POST'); save(mapSession(response.session, old)); }
        catch (error) {
          if (!(error instanceof ApiError) || ![401, 403, 404].includes(error.status)) throw error;
          save({ ...old, archived: true });
        }
      }
      const response = await client.request<Schema['SessionCreated']>('/sessions', { method: 'POST', body: {} });
      tokens.set(response.session.id, response.session_token);
      activeId = response.session.id;
      return save({ ...mapSession(response.session), greeting: response.greeting.text, messages: [mapMessage(response.greeting)] });
    },
    async getSession(id) {
      const response = await request<Schema['SessionResponse']>(id);
      return save({ ...mapSession(response.session, get(id)), messages: await messages(id) });
    },
    async sendMessage(id, text, opts = {}) {
      const original = get(id);
      if (original.archived) throw new ApiError('Этот диалог закрыт. Начните новый.', 'SESSION_CLOSED', 409, false);
      const value = text.trim();
      if (!value || [...value].length > 4000) throw new ApiError('Сообщение должно содержать от 1 до 4000 символов.', 'VALIDATION_ERROR', 422, false);
      const oldMessage = opts.retryId ? original.messages.find(m => m.id === opts.retryId) : undefined;
      if (opts.retryId && !oldMessage) throw new ApiError('Сообщение для повтора не найдено.', 'MESSAGE_NOT_FOUND', 404, false);
      if (oldMessage?.retryAt && Date.now() < oldMessage.retryAt) return clone(original);
      const clientId = oldMessage?.clientMessageId ?? crypto.randomUUID();
      const operation = pending.get(clientId) ?? { clientId, text: value };
      if (operation.text !== value) throw new ApiError('Для повтора необходимо исходное сообщение.', 'IDEMPOTENCY_CONFLICT', 409, false);
      pending.set(clientId, operation);
      const local: Message = { id: oldMessage?.id ?? clientId, clientMessageId: clientId, role: 'user', text: value, delivery: 'sent' };
      save({ ...original, messages: [...original.messages.filter(m => m.id !== local.id), local] });
      const deadline = Date.now() + (options.pollTimeoutMs ?? 45_000);
      try {
        let result: Turn;
        try { result = await request<Turn>(id, '/turns', 'POST', { client_message_id: clientId, text: operation.text }); }
        catch (error) {
          if (error instanceof ApiError && error.code === 'TURN_IN_PROGRESS' && typeof error.details.active_turn_id === 'string') {
            const active = await poll(id, { status: 'processing', turn_id: error.details.active_turn_id, poll_after_ms: 700 }, deadline);
            await applyTurn(active);
            result = await request<Turn>(id, '/turns', 'POST', { client_message_id: clientId, text: operation.text });
          } else throw error;
        }
        operation.turnId = result.turn_id;
        const complete = await poll(id, result, deadline);
        const next = await applyTurn(complete, clientId);
        pending.delete(clientId);
        return next;
      } catch (error) {
        const current = get(id);
        if (error instanceof ApiError && [400, 413, 422].includes(error.status)) {
          save({ ...current, messages: current.messages.filter(m => m.clientMessageId !== clientId) }); pending.delete(clientId); throw error;
        }
        const expired = error instanceof ApiError && [401, 403, 404].includes(error.status);
        return save({ ...current, accessError: expired ? errorWithRequest(error) : undefined, messages: current.messages.map(m => m.clientMessageId === clientId ? { ...m, delivery: 'failed', error: errorWithRequest(error), retryable: error instanceof ApiError ? error.retryable && !expired : true, retryAt: error instanceof ApiError && error.retryAfterMs > 0 ? Date.now() + error.retryAfterMs : undefined } : m) });
      }
    },
    async getDraft(id) {
      const response = await request<Schema['DraftResponse']>(id, '/lead-draft');
      save({ ...get(id), draft: response.draft }); return response.draft;
    },
    async previewLead(id, input) {
      if (!input.consent) throw new ApiError('Для продолжения нужно согласие на обработку данных.', 'VALIDATION_ERROR', 422, false);
      const draft = get(id).draft ?? await service.getDraft!(id);
      const fields: Schema['ContactFields'] = { full_name: input.fullName.trim() || null, phone: normalizePhone(input.phone), company: input.company.trim() || null };
      try {
        const updated = await request<Schema['DraftResponse']>(id, '/lead-draft', 'PATCH', { expected_revision: draft.revision, fields } satisfies Schema['DraftPatch']);
        save({ ...get(id), draft: updated.draft, preview: undefined });
      } catch (error) {
        if (error instanceof ApiError && error.code === 'DRAFT_VERSION_CONFLICT') { await service.getDraft!(id); throw new ApiError('Контакты изменились. Проверьте обновлённые поля и повторите предпросмотр.', error.code, 409, false, error.requestId); }
        throw error;
      }
      const preview = await request<Schema['Preview']>(id, '/lead-preview', 'POST');
      return save({ ...get(id), preview, stage: 'preview' });
    },
    async confirmLead(id) {
      const session = get(id); const preview = session.preview;
      if (!preview?.can_confirm) throw new ApiError('Укажите телефон перед подтверждением заявки.', 'CONTACT_REQUIRED', 422, false);
      let key = confirmations.get(preview.preview_id);
      if (!key) { key = crypto.randomUUID(); confirmations.set(preview.preview_id, key); }
      try {
        const response = await request<Schema['LeadResponse']>(id, '/leads', 'POST', { idempotency_key: key, preview_id: preview.preview_id, confirm: true } satisfies Schema['LeadInput']);
        return save({ ...get(id), stage: 'sent', status: 'request', apiLead: response.lead, confirmationMessage: response.message, preview: undefined });
      } catch (error) {
        if (error instanceof ApiError && error.code === 'PREVIEW_STALE') {
          const refreshed = await request<Schema['Preview']>(id, '/lead-preview', 'POST');
          save({ ...get(id), preview: refreshed, stage: 'preview' });
          throw new ApiError('Данные изменились. Проверьте обновлённое резюме и подтвердите заявку ещё раз.', error.code, 409, false, error.requestId);
        }
        throw error;
      }
    },
    async editLead(id) { return save({ ...get(id), preview: undefined, stage: 'form' }); },
    async submitLead(id, input) { return service.previewLead!(id, input); },
    async getTrace(id) { return mapTrace(await reviewRequest<Schema['Trace']>(`/traces/${encode(id)}`)); },
    async listReviewSessions() { return (await paginated<Schema['Session']>('/sessions')).map(item => ({ ...mapSession(item), archived: true, title: `Диалог ${new Date(item.opened_at).toLocaleString('ru-RU')}` })); },
    async getReviewSession(id) {
      const result = await reviewRequest<Schema['ReviewSession']>(`/sessions/${encode(id)}`);
      const lead = result.leads.at(-1);
      return { ...mapSession(result.session), archived: true, title: lead?.client_problem || result.messages.find(m => m.role === 'user')?.text.slice(0, 60) || 'Новый диалог', messages: result.messages.map(mapMessage), draft: result.draft, apiLead: lead };
    },
    async listReviewLeads() { return paginated<Schema['ReviewLeadPage']['items'][number]>('/leads'); },
    async getReviewLead(id) { return (await reviewRequest<Schema['ReviewLead']>(`/leads/${encode(id)}`)).lead; },
    async listArchive() { throw new ApiError('В API консультанта нет архива файлов. Демонстрация архива доступна в разделе макетов.', 'NOT_SUPPORTED', 404, false); },
    async downloadArchive() { throw new ApiError('Скачивание файлов не описано в API консультанта.', 'NOT_SUPPORTED', 404, false); },
  };
  return service;
}
function normalizePhone(value: string): string | null {
  if (!value.trim()) return null;
  let digits = value.replace(/\D/g, '');
  if (digits.length === 11 && digits.startsWith('8')) digits = '7' + digits.slice(1);
  if (/[^\d\s()+.-]/u.test(value) || digits.length < 7 || digits.length > 15 || (!value.trim().startsWith('+') && !(digits.length === 11 && digits.startsWith('7')))) throw new ApiError('Введите телефон в международном формате: + и от 7 до 15 цифр.', 'VALIDATION_ERROR', 422, false);
  return '+' + digits;
}
