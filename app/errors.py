class APIError(Exception):
    def __init__(self, status: int, code: str, message: str, retryable: bool = False, details: dict | None = None):
        self.status, self.code, self.message = status, code, message
        self.retryable, self.details = retryable, details or {}
        super().__init__(code)

    def body(self, request_id: str):
        return {'error': {'code': self.code, 'message': self.message, 'request_id': request_id,
                          'retryable': self.retryable, 'details': self.details}}
