import { ApiError, createApiClient, retryAfterMs, type Schema } from '../src/api/client';
import { createApiService, mapTrace } from '../src/api/service';

const sid = '11111111-1111-4111-8111-111111111111';
const stamp = '2026-11-13T10:00:00Z';
const state = (phase: Schema['Session']['phase'] = 'new'): Schema['Session'] => ({ id: sid, status: 'active', phase, revision: 1, opened_at: stamp, closed_at: null });
const message = (id: string, seq: number, role: 'user' | 'assistant', text: string): Schema['Message'] => ({ id, seq, role, text, created_at: stamp });
const greeting = message('greeting', 1, 'assistant', 'Серверное приветствие');
const created: Schema['SessionCreated'] = { session: state(), session_token: 'test-session-secret', greeting };
const completed = (phase: Schema['Session']['phase'] = 'clarification'): Schema['TurnCompleted'] => ({ turn_id: 'turn-1', status: 'completed', session: state(phase), user_message: message('user-1', 2, 'user', 'Вопрос'), assistant_message: message('assistant-1', 3, 'assistant', 'Ответ сервера'), decision: { kind: 'clarify', reason_code: 'missing_material_context' } });
const draft: Schema['Draft'] = { revision: 2, full_name: null, phone: '+79990000000', company: null, status: 'partial', missing_fields: ['full_name', 'company'] };
const preview: Schema['Preview'] = { preview_id: 'preview-1', snapshot_last_seq: 3, draft_revision: 3, status: 'partial', can_confirm: true, full_name: null, phone: '+79990000000', company: null, client_problem: 'Задача из API', summary: 'Серверное резюме', facts: [], recommended_products: ['Продукт из API'], open_questions: ['Уточнить мощность'], missing_fields: ['full_name', 'company'] };
const lead: Schema['Lead'] = { ...preview, id: 'lead-1', session_id: sid, status: 'partial', created_at: stamp, confirmed_at: stamp };
const error = (code: string, status = 503, retryable = true, details: Record<string, unknown> = {}, headers?: Record<string, string>) => new Response(JSON.stringify({ error: { code, message: `Ошибка ${code}`, retryable, request_id: 'request-1', details } }), { status, headers });
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
function transport(responses: (Response | Error)[]) {
  return vi.fn(async (_url: RequestInfo | URL, _options?: RequestInit) => { const response = responses.shift(); if (!response) throw new Error('Unexpected request'); if (response instanceof Error) throw response; return response; });
}
const contact = { fullName: '', phone: '+7 999 000-00-00', company: '', consent: true };
describe('API из OpenAPI', () => {
  it('создаёт одну сессию при повторной инициализации и не сохраняет секрет в storage', async () => {
    const fetch = transport([json(created, 201)]);
    const service = createApiService({ fetch });
    const [first, second] = await Promise.all([service.initialize!(), service.initialize!()]);
    expect(first.id).toBe(second.id); expect(fetch).toHaveBeenCalledOnce();
    expect(first.greeting).toBe(greeting.text);
    expect(first.source).toBe('api');
    expect(JSON.stringify(await service.listSessions())).not.toContain('test-session-secret');
    expect(localStorage.length).toBe(0); expect(sessionStorage.length).toBe(0);
    expect(JSON.parse(fetch.mock.calls[0][1]!.body as string)).toEqual({});
  });
  it('принимает синхронный ответ и заменяет optimistic message серверным без дублей', async () => {
    const fetch = transport([json(created, 201), json(completed())]);
    const service = createApiService({ fetch }); await service.createSession();
    const session = await service.sendMessage(sid, 'Вопрос');
    expect(session.messages.map(m => m.id)).toEqual(['greeting', 'user-1', 'assistant-1']);
    expect(fetch.mock.calls[1][1]?.headers).toMatchObject({ Authorization: 'Bearer test-session-secret' });
    expect(fetch.mock.calls[1][0]).toBe(`/api/v1/sessions/${sid}/turns?include_progress=true`);
  });
  it('опрашивает 202 до завершения, не создавая новый turn', async () => {
    const fetch = transport([json(created, 201), json({ turn_id: 'turn-1', status: 'processing', poll_after_ms: 1 }, 202), json({ turn_id: 'turn-1', status: 'processing', poll_after_ms: 1 }, 202), json(completed())]);
    const service = createApiService({ fetch, wait: async () => undefined }); await service.createSession();
    const session = await service.sendMessage(sid, 'Вопрос');
    expect(session.messages.at(-1)?.text).toBe('Ответ сервера');
    expect(fetch.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(2);
    expect(fetch.mock.calls[2][0]).toContain('/turns/turn-1');
  });
  it('передаёт реальные этапы и отсутствие прогресса из последовательных ответов', async () => {
    const first = { stage: 'analyzing', keywords: ['Газ', 'Тарифы'] };
    const last = { stage: 'finalizing', keywords: [] };
    const fetch = transport([json(created, 201), json({ turn_id: 'turn-1', status: 'processing', progress: first }, 202), json({ turn_id: 'turn-1', status: 'processing' }, 202), json({ turn_id: 'turn-1', status: 'processing', progress: last }, 202), json(completed())]);
    const onProgress = vi.fn(); const wait = vi.fn(async () => undefined);
    const service = createApiService({ fetch, wait }); await service.createSession();
    await service.sendMessage(sid, 'Вопрос', { onProgress });
    expect(onProgress.mock.calls.map(([value]) => value)).toEqual([first, undefined, last]);
    expect(wait).toHaveBeenCalledTimes(3);
    for (const [url] of fetch.mock.calls.slice(1)) expect(url).toContain('include_progress=true');
  });
  it('отмена во время паузы останавливает polling, повтор сохраняет исходный UUID', async () => {
    const controller = new AbortController();
    const fetch = transport([json(created, 201), json({ turn_id: 'turn-1', status: 'processing' }, 202), json(completed())]);
    const service = createApiService({ fetch, wait: async () => { controller.abort(); } }); await service.createSession();
    const interrupted = await service.sendMessage(sid, 'Вопрос', { signal: controller.signal });
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(interrupted.messages.at(-1)?.delivery).toBe('failed');
    await service.sendMessage(sid, 'Вопрос', { retryId: interrupted.messages.at(-1)!.id });
    expect(fetch.mock.calls[1][1]?.body).toBe(fetch.mock.calls[2][1]?.body);
  });
  it('игнорирует ответ запроса, завершившегося после отмены', async () => {
    const controller = new AbortController();
    const base = transport([json(created, 201), json(completed())]);
    const fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      const response = await base(url, init);
      if (init?.method === 'POST' && String(url).includes('/turns')) controller.abort();
      return response;
    });
    const service = createApiService({ fetch }); await service.createSession();
    const result = await service.sendMessage(sid, 'Вопрос', { signal: controller.signal });
    expect(result.messages.some(m => m.id === 'assistant-1')).toBe(false);
    expect(result.messages.at(-1)?.delivery).toBe('failed');
  });
  it('сохраняет client_message_id при сетевой ошибке и повторе', async () => {
    const fetch = transport([json(created, 201), new TypeError('offline'), json(completed())]);
    const service = createApiService({ fetch }); await service.createSession();
    let session = await service.sendMessage(sid, 'Вопрос');
    expect(session.messages.at(-1)?.delivery).toBe('failed');
    session = await service.sendMessage(sid, 'Вопрос', { retryId: session.messages.at(-1)!.id });
    expect(fetch.mock.calls[1][1]?.body).toBe(fetch.mock.calls[2][1]?.body);
    expect(session.messages.filter(m => m.role === 'user')).toHaveLength(1);
  });
  it('обрабатывает failed turn, не подставляя моковый ответ', async () => {
    const detail = { code: 'MODEL_UNAVAILABLE', message: 'Модель недоступна', retryable: true, request_id: 'failed-request' };
    const fetch = transport([json(created, 201), json({ status: 'processing', turn_id: 'turn-1' }, 202), json({ status: 'failed', turn_id: 'turn-1', error: detail }), json(completed())]);
    const service = createApiService({ fetch, wait: async () => undefined }); await service.createSession();
    let session = await service.sendMessage(sid, 'Вопрос');
    expect(session.messages).toHaveLength(2); expect(session.messages.at(-1)?.error).toContain('Модель недоступна');
    session = await service.sendMessage(sid, 'Вопрос', { retryId: session.messages.at(-1)!.id });
    expect(fetch.mock.calls[1][1]?.body).toBe(fetch.mock.calls[3][1]?.body);
  });
  it('останавливает бесконечный polling и сохраняет ключ для повтора', async () => {
    const fetch = transport([json(created, 201), json({ status: 'processing', turn_id: 'turn-1', poll_after_ms: 700 }, 202)]);
    const service = createApiService({ fetch, pollTimeoutMs: 10 }); await service.createSession();
    const session = await service.sendMessage(sid, 'Вопрос');
    expect(session.messages.at(-1)?.error).toContain('Ответ ещё готовится');
    expect(session.messages.at(-1)?.clientMessageId).toBeTruthy();
  });
  it('при TURN_IN_PROGRESS ожидает активную операцию и сохраняет прежнюю историю', async () => {
    const fetch = transport([json(created, 201), error('TURN_IN_PROGRESS', 409, true, { active_turn_id: 'turn-1' }), json(completed()), json({ ...completed(), turn_id: 'turn-2', user_message: message('user-2', 4, 'user', 'Второй вопрос'), assistant_message: message('assistant-2', 5, 'assistant', 'Второй ответ') })]);
    const service = createApiService({ fetch, wait: async () => undefined }); await service.createSession();
    const session = await service.sendMessage(sid, 'Второй вопрос');
    expect(session.messages.map(m => m.id)).toEqual(['greeting', 'user-1', 'assistant-1', 'user-2', 'assistant-2']);
    expect(fetch.mock.calls[1][1]?.body).toBe(fetch.mock.calls[3][1]?.body);
  });
  it.each([['VALIDATION_ERROR', 422], ['BAD_REQUEST', 400], ['IDEMPOTENCY_CONFLICT', 409], ['UNAUTHORIZED', 401]])('не повторяет автоматически %s', async (code, status) => {
    const fetch = transport([json(created, 201), error(code, status, false)]);
    const service = createApiService({ fetch }); await service.createSession();
    if (status === 400 || status === 422) await expect(service.sendMessage(sid, 'Вопрос')).rejects.toMatchObject({ code });
    else {
      const session = await service.sendMessage(sid, 'Вопрос');
      expect(session.messages.at(-1)?.retryable).toBe(false);
      if (status === 401) expect(session.accessError).toBeTruthy();
    }
    expect(fetch).toHaveBeenCalledTimes(2);
  });
  it('учитывает Retry-After и не отправляет повтор раньше разрешённого времени', async () => {
    const fetch = transport([json(created, 201), error('RATE_LIMITED', 429, true, {}, { 'Retry-After': '60' })]);
    const service = createApiService({ fetch }); await service.createSession();
    const session = await service.sendMessage(sid, 'Вопрос');
    expect(session.messages.at(-1)?.retryAt).toBeGreaterThan(Date.now() + 59_000);
    await service.sendMessage(sid, 'Вопрос', { retryId: session.messages.at(-1)!.id });
    expect(fetch).toHaveBeenCalledTimes(2);
  });
  it('пагинирует сообщения по after_seq и закрывает сессию перед созданием новой без старого контекста', async () => {
    const fetch = transport([json(created, 201), json({ session: state('clarification') }), json({ items: [greeting], next_after_seq: 1 }), json({ items: [completed().user_message, completed().assistant_message], next_after_seq: null }), json({ session: { ...state(), status: 'closed', phase: 'closed' } }), json({ ...created, session: { ...state(), id: 'second' }, session_token: 'second-secret' }, 201)]);
    const service = createApiService({ fetch }); await service.createSession();
    expect((await service.getSession!(sid)).messages).toHaveLength(3);
    expect(fetch.mock.calls[3][0]).toContain('after_seq=1');
    const fresh = await service.createSession(sid);
    expect(fresh.id).toBe('second'); expect(fresh.messages).toHaveLength(1);
    expect(fetch.mock.calls[4][0]).toContain('/close');
    expect(JSON.parse(fetch.mock.calls[5][1]!.body as string)).toEqual({});
  });
  it('сохраняет частичные контакты и показывает серверное превью до подтверждения', async () => {
    const fetch = transport([json(created, 201), json({ draft }), json({ draft: { ...draft, revision: 3 } }), json(preview), json({ lead, message: 'Заявка сохранена в демонстрационном прототипе.' }, 201)]);
    const service = createApiService({ fetch }); await service.createSession();
    const session = await service.previewLead!(sid, contact);
    expect(session.stage).toBe('preview'); expect(session.apiLead).toBeUndefined(); expect(session.preview?.summary).toBe('Серверное резюме');
    expect(JSON.parse(fetch.mock.calls[2][1]!.body as string)).toEqual({ expected_revision: 2, fields: { full_name: null, phone: '+79990000000', company: null } });
    const result = await service.confirmLead!(sid);
    expect(result.apiLead?.id).toBe('lead-1'); expect(result.confirmationMessage).toBe('Заявка сохранена в демонстрационном прототипе.');
  });
  it('сохраняет idempotency_key при повторном подтверждении после обрыва', async () => {
    const fetch = transport([json(created, 201), json({ draft }), json({ draft }), json(preview), new TypeError('offline'), json({ lead, message: 'Сохранено' }, 200)]);
    const service = createApiService({ fetch }); await service.createSession(); await service.previewLead!(sid, contact);
    await expect(service.confirmLead!(sid)).rejects.toMatchObject({ code: 'NETWORK_ERROR' });
    await service.confirmLead!(sid);
    expect(fetch.mock.calls[4][1]?.body).toBe(fetch.mock.calls[5][1]?.body);
  });
  it('PREVIEW_STALE обновляет превью и требует нового явного подтверждения', async () => {
    const fetch = transport([json(created, 201), json({ draft }), json({ draft }), json(preview), error('PREVIEW_STALE', 409, false), json({ ...preview, preview_id: 'preview-2', summary: 'Обновлённое резюме' }), json({ lead, message: 'Сохранено' }, 201)]);
    const service = createApiService({ fetch }); await service.createSession(); await service.previewLead!(sid, contact);
    await expect(service.confirmLead!(sid)).rejects.toMatchObject({ code: 'PREVIEW_STALE' });
    expect((await service.listSessions())[0].preview?.summary).toBe('Обновлённое резюме');
    expect(fetch).toHaveBeenCalledTimes(6);
    await service.confirmLead!(sid);
    const first = JSON.parse(fetch.mock.calls[4][1]!.body as string); const second = JSON.parse(fetch.mock.calls[6][1]!.body as string);
    expect(second.preview_id).toBe('preview-2'); expect(second.idempotency_key).not.toBe(first.idempotency_key);
  });
  it('DRAFT_VERSION_CONFLICT загружает актуальные контакты, не перезаписывая их автоматически', async () => {
    const fetch = transport([json(created, 201), json({ draft }), error('DRAFT_VERSION_CONFLICT', 409, false), json({ draft: { ...draft, revision: 4, company: 'Исправленная компания' } })]);
    const service = createApiService({ fetch }); await service.createSession();
    await expect(service.previewLead!(sid, contact)).rejects.toMatchObject({ code: 'DRAFT_VERSION_CONFLICT' });
    expect((await service.listSessions())[0].draft?.revision).toBe(4);
    expect(fetch).toHaveBeenCalledTimes(4);
  });
  it('не подтверждает черновик без телефона и не обращается к review без разрешения', async () => {
    const fetch = transport([json(created, 201), json({ draft }), json({ draft }), json({ ...preview, status: 'draft', can_confirm: false, phone: null })]);
    const service = createApiService({ fetch }); await service.createSession(); await service.previewLead!(sid, { ...contact, phone: '' });
    await expect(service.confirmLead!(sid)).rejects.toMatchObject({ code: 'CONTACT_REQUIRED' });
    await expect(service.getTrace!('assistant-1')).rejects.toMatchObject({ status: 403 });
    expect(fetch).toHaveBeenCalledTimes(4);
  });
  it('обращается к защищённому same-origin review без bearer в клиенте', async () => {
    const trace: Schema['Trace'] = { assistant_message_id: 'assistant-1', retrieval_query: 'запрос', decision: { kind: 'answer', reason_code: 'grounded' }, source_ids: ['doc'], evidence: [{ document_id: 'doc', chunk_id: 'chunk', quote: 'Цитата', relevance_score: .89 }], timings_ms: { total: 1234 }, corpus_version: 'v1', latency_ms: 1234, model_metadata: {}, recommended_products: [] };
    const fetch = transport([json(trace)]);
    const service = createApiService({ fetch, reviewEnabled: true });
    const result = await service.getTrace!('assistant-1');
    expect(result.confidence).toBe('не указана'); expect(result.documents[0].score).toBe(89);
    expect(fetch.mock.calls[0][0]).toBe('/review-api/v1/traces/assistant-1');
    expect(fetch.mock.calls[0][1]?.headers).not.toHaveProperty('Authorization');
    expect(() => createApiService({ reviewBaseUrl: 'https://third-party.example' })).toThrow();
    expect(mapTrace({ ...trace, evidence: [{ ...trace.evidence[0], relevance_score: null }] }).documents[0].score).toBeNull();
  });
  it('обрабатывает не-JSON ошибку сервера и Retry-After в формате даты', async () => {
    const client = createApiClient({ fetch: transport([new Response('<html>Bad gateway</html>', { status: 503 })]) });
    await expect(client.request('/sessions')).rejects.toBeInstanceOf(ApiError);
    expect(retryAfterMs('2')).toBe(2000);
    expect(retryAfterMs('invalid')).toBe(0);
    expect(retryAfterMs(new Date(10_000).toUTCString(), 5000)).toBe(5000);
  });
  it('прерывает зависший HTTP-запрос по timeout', async () => {
    const fetch = vi.fn((_url: RequestInfo | URL, options?: RequestInit) => new Promise<Response>((_resolve, reject) => { options?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError'))); }));
    await expect(createApiClient({ fetch, requestTimeoutMs: 5 }).request('/sessions')).rejects.toMatchObject({ code: 'REQUEST_TIMEOUT' });
  });
  it('читает все страницы закрытого review и различает черновики и подтверждённые заявки', async () => {
    const fetch = transport([json({ items: [state()], next_offset: 1 }), json({ items: [{ ...state(), id: 'second' }], next_offset: null }), json({ session: state(), messages: [greeting], draft, leads: [lead] }), json({ items: [{ ...draft, record_type: 'draft', session_id: sid }, { ...lead, record_type: 'confirmed' }], next_offset: null }), json({ lead })]);
    const service = createApiService({ fetch, reviewEnabled: true });
    expect(await service.listReviewSessions!()).toHaveLength(2);
    const session = await service.getReviewSession!(sid);
    expect(session.archived).toBe(true); expect(session.apiLead?.id).toBe(lead.id);
    expect((await service.listReviewLeads!()).map(item => item.record_type)).toEqual(['draft', 'confirmed']);
    expect((await service.getReviewLead!(lead.id)).id).toBe(lead.id);
    expect(fetch.mock.calls[1][0]).toContain('offset=1');
  });
});
