from collections import defaultdict, deque
from time import monotonic
from uuid import UUID, uuid4
from starlette.responses import JSONResponse
from .errors import APIError


class RequestGuard:
    """Bound request bodies, rate-limit per process, and attach safe request IDs."""
    def __init__(self, app, settings):
        self.app, self.settings = app, settings
        self.buckets = defaultdict(deque)

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

        async def send_headers(message):
            nonlocal response_started
            if message['type'] == 'http.response.start':
                response_started = True
                message['headers'] += [(b'x-request-id', request_id.encode()), (b'x-content-type-options', b'nosniff'), (b'cache-control', b'no-store')]
            await send(message)

        async def reject(status, code, text, retryable=False):
            response = JSONResponse(APIError(status, code, text, retryable).body(request_id), status_code=status,
                                    headers={'Retry-After': '60'} if status == 429 else None)
            await response(scope, receive, send_headers)

        if scope['path'].startswith('/api/'):
            stamp = monotonic()
            ip = (scope.get('client') or ('unknown',))[0]
            if len(self.buckets) > 10000:
                self.buckets = defaultdict(deque, {k:v for k,v in self.buckets.items() if v and v[-1] > stamp-60})
                if len(self.buckets) > 10000 and ip not in self.buckets:
                    return await reject(429, 'RATE_LIMITED', 'Слишком много запросов.', True)
            bucket = self.buckets[ip]
            while bucket and bucket[0] <= stamp - 60:
                bucket.popleft()
            if len(bucket) >= self.settings.requests_per_minute:
                return await reject(429, 'RATE_LIMITED', 'Слишком много запросов. Повторите позже.', True)
            bucket.append(stamp)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunk = message.get('body', b'')
            size += len(chunk)
            if size > self.settings.max_body_bytes:
                return await reject(413, 'BODY_TOO_LARGE', 'Запрос слишком большой.')
            chunks.append(chunk)
            if not message.get('more_body', False):
                break
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {'type': 'http.request', 'body': b''.join(chunks), 'more_body': False}
            return await receive()
        try:
            await self.app(scope, replay, send_headers)
        except Exception:
            if response_started:
                raise
            await reject(500,'INTERNAL_ERROR','Не удалось завершить запрос. Повторите позже.',True)
