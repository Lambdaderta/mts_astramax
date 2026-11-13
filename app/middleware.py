from collections import defaultdict, deque
from time import monotonic
from uuid import UUID, uuid4
from starlette.responses import JSONResponse
from .errors import APIError
from .telemetry import record


class RequestGuard:
    """Bound request bodies, rate-limit per process, and attach safe request IDs."""
    MAX_CLIENT_BUCKETS = 10000
    RATE_WINDOW_SECONDS = 60

    def __init__(self, app, settings):
        self.app, self.settings = app, settings
        self.buckets = defaultdict(deque)
        self.last_bucket_sweep = float('-inf')

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = dict(scope['headers'])
        try:
            request_id = str(UUID(headers.get(b'x-request-id', b'').decode('ascii')))
        except (ValueError, UnicodeDecodeError):
            request_id = str(uuid4())
        scope.setdefault('state', {})['request_id'] = request_id
        response_started = False
        started_at = monotonic()
        is_api = scope['path'].startswith('/api/')
        state = scope['state']

        async def send_headers(message):
            nonlocal response_started
            if message['type'] == 'http.response.start':
                response_started = True
                message['headers'] += [(b'x-request-id', request_id.encode()), (b'x-content-type-options', b'nosniff'), (b'cache-control', b'no-store')]
                if is_api:
                    route = getattr(scope.get('route'), 'path', None)
                    record('http_request', request_id=request_id, route=route,
                           status=message['status'], elapsed_ms=round((monotonic() - started_at) * 1000, 2),
                           error_code=state.get('error_code'))
            await send(message)

        async def reject(status, code, text, retryable=False):
            state['error_code'] = code
            response = JSONResponse(APIError(status, code, text, retryable).body(request_id), status_code=status,
                                    headers={'Retry-After': '60'} if status == 429 else None)
            await response(scope, receive, send_headers)

        if is_api:
            stamp = started_at
            ip = (scope.get('client') or ('unknown',))[0]
            cutoff = stamp - self.RATE_WINDOW_SECONDS
            if self.last_bucket_sweep <= cutoff:
                self.last_bucket_sweep = stamp
                for address in [address for address, bucket in self.buckets.items() if bucket[-1] <= cutoff]:
                    del self.buckets[address]
            if ip not in self.buckets and len(self.buckets) >= self.MAX_CLIENT_BUCKETS:
                return await reject(429, 'RATE_LIMITED', 'Слишком много запросов.', True)
            bucket = self.buckets[ip]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.settings.requests_per_minute:
                return await reject(429, 'RATE_LIMITED', 'Слишком много запросов. Повторите позже.', True)
            bucket.append(stamp)
        body = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunk = message.get('body', b'')
            if len(body) + len(chunk) > self.settings.max_body_bytes:
                return await reject(413, 'BODY_TOO_LARGE', 'Запрос слишком большой.')
            body.extend(chunk)
            if not message.get('more_body', False):
                break
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
            return await receive()
        try:
            await self.app(scope, replay, send_headers)
        except Exception:
            if response_started:
                raise
            await reject(500, 'INTERNAL_ERROR', 'Не удалось завершить запрос. Повторите позже.', True)
