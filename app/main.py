import asyncio
from contextlib import asynccontextmanager
import hmac
import sqlite3
from typing import Annotated
from uuid import UUID
from fastapi import FastAPI, Depends, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.exceptions import HTTPException

from . import contracts as c
from .errors import APIError
from .middleware import RequestGuard
from .settings import Settings
from .storage import Database, StorageError
from .services import Service
from .ml.protocol import ConsultationCore


def create_app(settings: Settings | None = None, core: ConsultationCore | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        selected_core = core
        if selected_core is None:
            from .ml.adapter import build_core, LLMConfig
            llm_config = LLMConfig.from_env(platform=False) if settings.llm_enabled else None
            selected_core = build_core(settings.index_dir, llm_config=llm_config)
        database = Database(settings.database_url or settings.database_path)
        app.state.service = Service(database, selected_core, settings)
        try:
            yield
        finally:
            tasks = list(app.state.service.tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    responses={code:{'model':c.ErrorResponse} for code in (400,401,403,404,409,413,422,429,500,503)}
    responses[413]['description']='Request Entity Too Large'
    responses[422]['description']='Unprocessable Entity'
    app = FastAPI(title='MTS Energytool Backend', version='1.0.0', lifespan=lifespan,
                  description='Locally reconstructed team API. Product knowledge is supplied separately.',
                  responses=responses)
    app.add_middleware(RequestGuard, settings=settings)
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins),
                       allow_methods=['GET','POST','PATCH','OPTIONS'],
                       allow_headers=['Authorization','Content-Type','X-Request-ID'],
                       expose_headers=['X-Request-ID','Retry-After'])

    def svc() -> Service:
        return app.state.service

    security = HTTPBearer(auto_error=False)
    Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(security)]

    def session_auth(session_id: UUID, credentials: Credentials):
        svc().authorize(str(session_id), credentials.credentials if credentials else None)
        return str(session_id)

    def review_auth(credentials: Credentials):
        if not credentials:
            raise APIError(401,'UNAUTHORIZED','Требуется токен проверки.')
        if not settings.review_token or not hmac.compare_digest(credentials.credentials.encode(),settings.review_token.encode()):
            raise APIError(403,'FORBIDDEN','Нет доступа к разделу проверки.')

    SessionID = Annotated[str, Depends(session_auth)]

    @app.exception_handler(APIError)
    async def api_error(request, exc):
        request.state.error_code=exc.code
        return JSONResponse(exc.body(request.state.request_id), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's raw errors can contain user input. Return field paths only.
        if any(error['type'] == 'json_invalid' for error in exc.errors()):
            error=APIError(400,'BAD_REQUEST','Тело запроса должно содержать корректный JSON.')
            return await api_error(request,error)
        details={'fields':['.'.join(map(str,error['loc'])) for error in exc.errors()]}
        error=APIError(422,'VALIDATION_ERROR','Проверьте формат и обязательные поля.',details=details)
        return await api_error(request,error)

    @app.exception_handler(sqlite3.Error)
    @app.exception_handler(StorageError)
    async def storage_error(request, exc):
        return await api_error(request,APIError(503,'STORAGE_UNAVAILABLE','Не удалось сохранить данные. Повторите запрос.',True))

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return await api_error(request,APIError(exc.status_code,'NOT_FOUND' if exc.status_code==404 else 'BAD_REQUEST','Запрос не поддерживается.'))

    @app.get('/health/live',tags=['health'])
    def live():
        return {'status':'ok'}

    @app.get('/health/ready',response_model=c.Readiness,responses={503:{'model':c.Readiness}},tags=['health'])
    def ready(response: Response):
        try:
            database=svc().db.ready()
        except (StorageError, sqlite3.Error):
            database=False
        knowledge=bool(svc().core.ready)
        available=database and knowledge
        response.status_code=200 if available else 503
        return {'status':'ready' if available else 'not_ready','database':database,'knowledge':knowledge,
                'corpus_version':svc().core.corpus_version}

    @app.post('/api/v1/sessions',status_code=201,response_model=c.SessionCreated,tags=['sessions'])
    def create_session(body: c.Empty):
        return svc().create_session()

    @app.get('/api/v1/sessions/{session_id}',response_model=c.SessionResponse,tags=['sessions'])
    def get_session(session_id: SessionID):
        with svc().db.read() as conn:
            return {'session':svc().session(conn,session_id)}

    @app.post('/api/v1/sessions/{session_id}/close',response_model=c.SessionResponse,tags=['sessions'])
    def close(session_id: SessionID,request: Request):
        return svc().close_session(session_id,request.state.request_id)

    @app.get('/api/v1/sessions/{session_id}/messages',response_model=c.MessagePage,tags=['sessions'])
    def messages(session_id: SessionID,after_seq: int=Query(0,ge=0),limit: int=Query(50,ge=1,le=200)):
        return svc().messages(session_id,after_seq,limit)

    @app.post('/api/v1/sessions/{session_id}/turns',response_model=c.TurnCompleted|c.TurnProcessing,
              responses={202:{'model':c.TurnProcessing}},response_model_exclude_unset=True,tags=['turns'])
    async def post_turn(session_id: SessionID,body: c.TurnInput,request: Request,response: Response,
                        include_progress: bool=Query(False,description='Include current processing stage and topic labels in HTTP 202.')):
        result=await svc().post_turn(session_id,body,request.state.request_id,include_progress=include_progress)
        if result['status']=='processing':
            response.status_code=202
        return result

    @app.get('/api/v1/sessions/{session_id}/turns/{turn_id}',response_model=c.TurnCompleted|c.TurnProcessing|c.TurnFailed,
             responses={202:{'model':c.TurnProcessing}},response_model_exclude_unset=True,tags=['turns'])
    def get_turn(session_id: SessionID,turn_id: UUID,request: Request,response: Response,
                 include_progress: bool=Query(False,description='Include current processing stage and topic labels in HTTP 202.')):
        result=svc().get_turn(session_id,str(turn_id),request.state.request_id,include_progress=include_progress)
        if result['status']=='processing':
            response.status_code=202
        return result

    @app.get('/api/v1/sessions/{session_id}/lead-draft',response_model=c.DraftResponse,tags=['leads'])
    def get_draft(session_id: SessionID):
        return svc().get_draft(session_id)

    @app.patch('/api/v1/sessions/{session_id}/lead-draft',response_model=c.DraftResponse,tags=['leads'])
    def patch_draft(session_id: SessionID,body:c.DraftPatch):
        return svc().patch_draft(session_id,body)

    @app.post('/api/v1/sessions/{session_id}/lead-preview',response_model=c.Preview,tags=['leads'])
    def preview(session_id: SessionID):
        return svc().preview(session_id)

    @app.post('/api/v1/sessions/{session_id}/leads',status_code=201,response_model=c.LeadResponse,
              responses={200:{'model':c.LeadResponse}},tags=['leads'])
    def confirm(session_id: SessionID,body:c.LeadInput,response:Response):
        result,created=svc().confirm(session_id,body)
        response.status_code=201 if created else 200
        return result

    @app.get('/api/v1/review/sessions',response_model=c.SessionPage,dependencies=[Depends(review_auth)],tags=['review'])
    def review_sessions(offset:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=200)):
        return svc().review_sessions(offset,limit)

    @app.get('/api/v1/review/sessions/{session_id}',response_model=c.ReviewSession,dependencies=[Depends(review_auth)],tags=['review'])
    def review_session(session_id:UUID):
        return svc().review_session(str(session_id))

    @app.get('/api/v1/review/leads',response_model=c.ReviewLeadPage,dependencies=[Depends(review_auth)],tags=['review'])
    def review_leads(offset:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=200)):
        return svc().review_leads(offset,limit)

    @app.get('/api/v1/review/leads/{lead_id}',response_model=c.ReviewLead,dependencies=[Depends(review_auth)],tags=['review'])
    def review_lead(lead_id:UUID):
        return svc().review_lead(str(lead_id))

    @app.get('/api/v1/review/traces/{assistant_message_id}',response_model=c.Trace,dependencies=[Depends(review_auth)],tags=['review'])
    def review_trace(assistant_message_id:UUID):
        return svc().trace(str(assistant_message_id))

    return app


app = create_app()
