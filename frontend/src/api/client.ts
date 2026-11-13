import type { components } from './schema';

export type Schema = components['schemas'];
export class ApiError extends Error {
  constructor(
    message: string,
    readonly code = 'NETWORK_ERROR',
    readonly status = 0,
    readonly retryable = true,
    readonly requestId?: string,
    readonly details: Record<string, unknown> = {},
    readonly retryAfterMs = 0,
  ) { super(message); this.name = 'ApiError'; }
}
export function retryAfterMs(value: string | null, now = Date.now()): number {
  if (!value) return 0;
  const seconds = Number(value);
  return Math.max(0, Number.isFinite(seconds) ? seconds * 1000 : Date.parse(value) - now || 0);
}
export interface ApiClientOptions { baseUrl?: string; fetch?: typeof fetch; requestTimeoutMs?: number; }
export function createApiClient({ baseUrl = '/api/v1', fetch: fetcher = globalThis.fetch.bind(globalThis), requestTimeoutMs = 12_000 }: ApiClientOptions = {}) {
  const base = (baseUrl.trim() || '/api/v1').replace(/\/$/, '');
  return {
    async request<T>(path: string, options: { method?: string; body?: unknown; token?: string; credentials?: RequestCredentials; timeoutMs?: number } = {}): Promise<T> {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), options.timeoutMs ?? requestTimeoutMs);
      try {
        const headers: Record<string, string> = { Accept: 'application/json' };
        if (options.body !== undefined) headers['Content-Type'] = 'application/json';
        if (options.token) headers.Authorization = `Bearer ${options.token}`;
        const response = await fetcher(base + path, { method: options.method ?? 'GET', headers, ...(options.body !== undefined ? { body: JSON.stringify(options.body) } : {}), signal: controller.signal, credentials: options.credentials ?? 'same-origin', cache: 'no-store' });
        const data: unknown = await response.json().catch(() => null);
        if (!response.ok) {
          const error = data && typeof data === 'object' && 'error' in data && data.error && typeof data.error === 'object' ? data.error as Partial<Schema['ErrorDetail']> : undefined;
          throw new ApiError(error?.message ?? `Не удалось выполнить запрос (${response.status}).`, error?.code ?? `HTTP_${response.status}`, response.status, error?.retryable ?? (response.status === 429 || response.status >= 500), error?.request_id ?? response.headers.get('X-Request-ID') ?? undefined, error?.details ?? {}, retryAfterMs(response.headers.get('Retry-After')));
        }
        if (data === null) throw new ApiError('Сервер вернул ответ неподдерживаемого формата.', 'INVALID_RESPONSE', response.status, false);
        return data as T;
      } catch (error) {
        if (error instanceof ApiError) throw error;
        throw new ApiError(controller.signal.aborted ? 'Не дождались ответа сервера. Повторите отправку: сообщение не продублируется.' : 'Нет связи с сервером. Проверьте подключение и повторите отправку.', controller.signal.aborted ? 'REQUEST_TIMEOUT' : 'NETWORK_ERROR');
      } finally { clearTimeout(timer); }
    },
  };
}
export type ApiClient = ReturnType<typeof createApiClient>;

export function errorWithRequest(error: unknown): string {
  if (!(error instanceof Error)) return 'Не удалось выполнить действие. Попробуйте ещё раз.';
  return error instanceof ApiError && error.requestId ? `${error.message} Код запроса: ${error.requestId}` : error.message;
}
