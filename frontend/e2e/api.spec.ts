import { expect, test, type Page } from '@playwright/test';
import type { Schema } from '../src/api/client';

const stamp = '2026-11-13T12:00:00Z';
function fixtures() {
  let created = 0;
  let current = '';
  let revision = 0;
  let previewNumber = 0;
  const calls: { path: string; method: string; body: any; authorization?: string }[] = [];
  const contacts: Schema['ContactFields'] = { full_name: null, phone: null, company: null };
  const turns = new Map<string, Schema['TurnCompleted']>();
  const state = (phase: Schema['Session']['phase'] = 'new'): Schema['Session'] => ({ id: current, status: 'active', phase, revision: 1, opened_at: stamp, closed_at: null });
  const draft = (): Schema['Draft'] => ({ ...contacts, revision, status: contacts.phone ? contacts.full_name && contacts.company ? 'complete' : 'partial' : 'draft', missing_fields: (['full_name', 'phone', 'company'] as const).filter(key => !contacts[key]) });
  const preview = (): Schema['Preview'] => ({ ...contacts, preview_id: `00000000-0000-4000-8000-${String(previewNumber).padStart(12, '0')}`, snapshot_last_seq: 3, draft_revision: revision, status: draft().status, can_confirm: !!contacts.phone, client_problem: 'Серверная задача', summary: `Серверное резюме ${previewNumber}`, facts: [{ key: 'Объект', value: 'Кафе', source_message_id: 'user-1' }], recommended_products: ['Рекомендация из базы'], open_questions: ['Уточнить профиль потребления'], missing_fields: draft().missing_fields });
  return { calls, state, preview, async install(page: Page, options: { form?: boolean; async?: boolean; progress?: boolean; failed?: boolean; stale?: boolean; rateLimited?: boolean } = {}) {
    let failed = false; let stale = false;
    let polls = 0;
    await page.route('**/api/v1/**', async route => {
      const request = route.request(); const path = new URL(request.url()).pathname;
      const body = request.postDataJSON(); const method = request.method();
      calls.push({ path, method, body, authorization: request.headers().authorization });
      const respond = (json: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(json) });
      const failure = (code: string, status = 503) => respond({ error: { code, message: `Ошибка ${code}`, retryable: status === 503, request_id: 'api-browser-request' } }, status);
      if (path === '/api/v1/sessions' && method === 'POST') {
        created++; current = `11111111-1111-4111-8111-${String(created).padStart(12, '0')}`;
        return respond({ session: state(), session_token: `browser-session-secret-${created}`, greeting: { id: 'greeting', seq: 1, role: 'assistant', text: 'Приветствие только с сервера', created_at: stamp } }, 201);
      }
      if (path.endsWith('/close')) return respond({ session: { ...state(), status: 'closed', phase: 'closed' } });
      if (path.endsWith('/turns') && method === 'POST') {
        if (options.rateLimited && !failed) { failed = true; return route.fulfill({ status: 429, contentType: 'application/json', headers: { 'Retry-After': '1' }, body: JSON.stringify({ error: { code: 'RATE_LIMITED', message: 'Слишком много запросов', retryable: true, request_id: 'rate-request' } }) }); }
        if (options.failed && !failed) { failed = true; return failure('MODEL_UNAVAILABLE'); }
        const existing = turns.get(body.client_message_id);
        const result: Schema['TurnCompleted'] = existing ?? { turn_id: 'turn-1', status: 'completed', session: state(options.form ? 'collecting_contacts' : 'consulting'), user_message: { id: 'user-1', seq: 2, role: 'user', text: body.text, created_at: stamp }, assistant_message: { id: 'assistant-1', seq: 3, role: 'assistant', text: options.form ? 'Укажите контакты для заявки.' : 'Ответ API <script>window.apiInjected = true</script>', created_at: stamp }, decision: { kind: options.form ? 'collecting_contacts' : 'answer', reason_code: 'grounded_answer' }, lead_draft: options.form ? draft() : null };
        turns.set(body.client_message_id, result);
        if (options.progress) return respond({ status: 'processing', turn_id: result.turn_id, poll_after_ms: 1500, progress: { stage: 'preparing_answer', keywords: ['Газ', 'Тарифы'] } }, 202);
        if (options.async) return respond({ status: 'processing', turn_id: result.turn_id, poll_after_ms: 50 }, 202);
        return respond(result);
      }
      if (path.endsWith('/turns/turn-1')) {
        if (options.progress && polls++ === 0) return respond({ status: 'processing', turn_id: 'turn-1', poll_after_ms: 500, progress: { stage: 'finalizing', keywords: [] } }, 202);
        return respond([...turns.values()].at(-1));
      }
      if (path.endsWith('/lead-draft')) { if (method === 'PATCH') { expect(body.expected_revision).toBe(revision); Object.assign(contacts, body.fields); revision++; } return respond({ draft: draft() }); }
      if (path.endsWith('/lead-preview')) { previewNumber++; return respond(preview()); }
      if (path.endsWith('/leads')) {
        if (options.stale && !stale) { stale = true; return failure('PREVIEW_STALE', 409); }
        return respond({ lead: { ...preview(), id: 'saved-lead', session_id: current, status: draft().status === 'complete' ? 'complete' : 'partial', created_at: stamp, confirmed_at: stamp }, message: 'Заявка сохранена в демонстрационном прототипе.' }, 201);
      }
      return failure('UNEXPECTED_TEST_REQUEST', 400);
    });
  } };
}

for (const width of [1440, 390]) {
  test(`API: этапы подготовки и метки на ширине ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 960 });
    const api = fixtures(); await api.install(page, { progress: true });
    await page.goto('/');
    await page.getByRole('textbox', { name: 'Сообщение' }).fill('Какой тариф на газ?');
    await page.getByRole('button', { name: 'Отправить сообщение' }).click();
    const status = page.getByRole('status');
    await expect(status).toContainText('Готовлю ответ');
    await expect(status).toContainText('Газ');
    await expect(status).toContainText('Тарифы');
    await page.screenshot({ path: testInfo.outputPath(`progress-${width}.png`) });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await expect(status).toContainText('Сохраняю результат');
    await expect(status).not.toContainText('Газ');
    await expect(page.getByText('Ответ API <script>window.apiInjected = true</script>', { exact: true })).toBeVisible();
    await expect(status).toHaveCount(0);
  });
}

test('API: уход со страницы останавливает опрос подготовки', async ({ page }) => {
  const api = fixtures(); await api.install(page, { progress: true });
  await page.goto('/');
  await page.getByRole('textbox', { name: 'Сообщение' }).fill('Какой тариф на газ?');
  await page.getByRole('button', { name: 'Отправить сообщение' }).click();
  await expect(page.getByRole('status')).toContainText('Газ');
  await page.getByRole('link', { name: 'МТС Energytool — архив результатов' }).click();
  await expect(page).toHaveURL(/\/archive$/);
  await expect(page.getByRole('status')).toHaveCount(0);
  await page.waitForTimeout(1800);
  expect(api.calls.filter(call => call.path.includes('/turns/turn-1'))).toHaveLength(0);
});

test('API: приветствие, 202, безопасный текст и новая сессия после reload', async ({ page }) => {
  const api = fixtures(); await api.install(page, { async: true });
  await page.goto('/');
  await expect(page.getByText('Приветствие только с сервера')).toBeVisible();
  await expect(page.getByLabel('Отладка', { exact: true })).toHaveCount(0);
  await page.getByRole('textbox', { name: 'Сообщение' }).fill('Мой вопрос');
  await page.getByRole('button', { name: 'Отправить сообщение' }).click();
  await expect(page.getByText('Ответ API <script>window.apiInjected = true</script>', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => 'apiInjected' in window)).toBe(false);
  expect(api.calls.filter(call => call.path.endsWith('/turns')).length).toBe(1);
  expect(api.calls.some(call => call.path.includes('/turns/turn-1'))).toBe(true);
  await expect(page.getByRole('heading', { name: 'Облачная платформа учёта' })).toHaveCount(0);
  expect(await page.evaluate(() => [localStorage.length, sessionStorage.length])).toEqual([0, 0]);
  await page.reload();
  await expect(page.getByText('Приветствие только с сервера')).toBeVisible();
  expect(api.calls.filter(call => call.path === '/api/v1/sessions')).toHaveLength(2);
  expect(api.calls.filter(call => call.path.includes('/review/'))).toHaveLength(0);
});

test('API: частичная заявка требует предпросмотра и повторного подтверждения после PREVIEW_STALE', async ({ page }) => {
  const api = fixtures(); await api.install(page, { form: true, stale: true });
  await page.goto('/');
  await page.getByRole('textbox', { name: 'Сообщение' }).fill('Свяжите с менеджером');
  await page.getByRole('button', { name: 'Отправить сообщение' }).click();
  await page.getByLabel('Телефон', { exact: true }).fill('+49 30 1234567');
  await page.getByLabel('Согласен на обработку персональных данных').check();
  await page.getByRole('button', { name: 'Проверить заявку' }).click();
  await expect(page.getByText('Серверное резюме 1')).toBeVisible();
  expect(api.calls.filter(call => call.path.endsWith('/leads'))).toHaveLength(0);
  await page.getByRole('button', { name: 'Подтвердить заявку' }).click();
  await expect(page.getByText('Серверное резюме 2')).toBeVisible();
  await expect(page.getByText(/Проверьте обновлённое резюме/)).toBeVisible();
  expect(api.calls.filter(call => call.path.endsWith('/leads'))).toHaveLength(1);
  await page.getByRole('button', { name: 'Подтвердить заявку' }).click();
  await expect(page.getByRole('heading', { name: 'Заявка сохранена', exact: true })).toBeVisible();
  await expect(page.getByText('Заявка сохранена в демонстрационном прототипе.')).toBeVisible();
  await expect(page.getByText('Передано менеджеру', { exact: true })).toHaveCount(0);
  const confirms = api.calls.filter(call => call.path.endsWith('/leads'));
  expect(confirms[0].body.preview_id).not.toBe(confirms[1].body.preview_id);
  await page.screenshot({ path: 'test-results-api/screens/lead-saved.png' });
});

test('API: 503 и повтор используют тот же UUID без двойной реплики', async ({ page }) => {
  const api = fixtures(); await api.install(page, { failed: true });
  await page.goto('/');
  await page.getByRole('textbox', { name: 'Сообщение' }).fill('Проверка повтора');
  await page.getByRole('button', { name: 'Отправить сообщение' }).click();
  await expect(page.getByText(/Ошибка MODEL_UNAVAILABLE/)).toBeVisible();
  await page.getByRole('button', { name: 'Повторить', exact: true }).click();
  await expect(page.getByText('Ответ API <script>window.apiInjected = true</script>', { exact: true })).toBeVisible();
  const turns = api.calls.filter(call => call.path.endsWith('/turns'));
  expect(turns[0].body.client_message_id).toBe(turns[1].body.client_message_id);
  await expect(page.getByRole('log').getByText('Проверка повтора', { exact: true })).toHaveCount(1);
});

test('API: /demo сохраняет изолированные моки, /review закрыт по умолчанию', async ({ page }) => {
  const api = fixtures(); await api.install(page);
  await page.goto('/demo?screen=75%3A719');
  await expect(page.getByRole('heading', { name: 'Облачная платформа учёта' })).toBeVisible();
  expect(api.calls).toHaveLength(0);
  await page.goto('/review');
  await expect(page.getByRole('heading', { name: 'Раздел проверки закрыт' })).toBeVisible();
  expect(api.calls).toHaveLength(0);
});

test('API: Retry-After блокирует кнопку до истечения срока и сохраняет UUID', async ({ page }) => {
  const api = fixtures(); await api.install(page, { rateLimited: true });
  await page.goto('/');
  await page.getByRole('textbox', { name: 'Сообщение' }).fill('Проверка лимита');
  await page.getByRole('button', { name: 'Отправить сообщение' }).click();
  await expect(page.getByRole('button', { name: /^Повторить через/ })).toBeDisabled();
  const retry = page.getByRole('button', { name: 'Повторить', exact: true });
  await expect(retry).toBeEnabled();
  await retry.click();
  await expect(page.getByText('Ответ API <script>window.apiInjected = true</script>', { exact: true })).toBeVisible();
  const turns = api.calls.filter(call => call.path.endsWith('/turns'));
  expect(turns[0].body.client_message_id).toBe(turns[1].body.client_message_id);
});
