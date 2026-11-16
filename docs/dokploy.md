# Запуск в Dokploy

Один `compose.yaml`: React/Nginx (`frontend`), FastAPI (`api`), PostgreSQL (`db`). Nginx проксирует `/api/v1` на API; отдельный домен API не нужен.

## Репозиторий и сервис

Релизная сборка фронтенда и бэкенда находится в ветке `main`. Dokploy должен использовать именно `main`.

В проекте Dokploy создайте **Docker Compose**, выберите `Lambdaderta/mts_huinya`, ветку **`main`** и Compose Path **`./compose.yaml`**. Тип запуска — Docker Compose; для сетевой связности включите **Isolated Deployments**. Официальные инструкции: [Compose](https://docs.dokploy.com/docs/core/docker-compose), [домены и сети](https://docs.dokploy.com/docs/core/docker-compose/domains).

## Environment

Готовый общий пример для ProxyAPI: [`.env.dokploy.example`](../.env.dokploy.example). Скопируйте его содержимое в Environment и замените только `POSTGRES_PASSWORD` и `LLM_API_KEY`. Основные значения:

```dotenv
POSTGRES_PASSWORD=replace-with-random-password
ENABLE_REVIEW=false
LLM_ENABLED=true
ALLOW_PRIVATE_CORPUS_TO_LLM=true
LLM_BASE_URL=https://api.proxyapi.ru/v1
LLM_API_KEY=replace-with-provider-key
LLM_MODEL=qwen/qwen3.8-27b
EMB_MODEL=openai/text-embedding-3-small
RETRIEVAL_MODE=hybrid
CORPUS_DIR_HOST=../files/corpus
ML_TIMEOUT_SECONDS=30
TURN_LEASE_SECONDS=45
```

Ключ создаётся в [консоли ProxyAPI](https://console.proxyapi.ru/); его баланс и доступ к моделям должны быть активны. Один ключ используется для текста и эмбеддингов; отдельный `EMBEDDER_API_KEY` не нужен. Настройки сверены с документацией [Qwen](https://www.proxyapi.ru/models/qwen/qwen3.8-27b) и [эмбеддингов](https://proxyapi.ru/docs/embeddings). Это конфигурация веб-прототипа; у организаторов может быть другой endpoint и модель эмбеддингов.

`POSTGRES_PASSWORD` — ваш новый пароль локально создаваемой БД, а не пароль от Dokploy или стороннего сервиса. Сгенерировать его на сервере можно командой `openssl rand -hex 24`. Compose сам использует одно значение для создания пользователя `energytool` и подключения API через `PGPASSWORD`; `DATABASE_URL` задавать не требуется. После первого запуска пароль уже сохранён в томе PostgreSQL: смена ENV сама по себе его не меняет. Для существующей БД используйте прежний пароль или отдельно смените его в PostgreSQL, сохранив данные.

`ALLOW_PRIVATE_CORPUS_TO_LLM=true` включает передачу разрешённому провайдеру фрагментов базы. Ключи и пароль остаются на сервере и не попадают в JavaScript. Не вставляйте реальные значения в отслеживаемый пример ENV.

Нажмите **Deploy**. Без индекса приложение запускается, но readiness возвращает 503. Для первого запуска инфраструктуры без провайдера допустимо `LLM_ENABLED=false`: это ограниченный локальный режим, а не полноценная консультация.

## База знаний

Материалы не входят в Git или Docker-образ. На сервере поместите распакованные разрешённые документы в `../files/corpus` относительно checkout Compose и дайте контейнерному UID 10001 право чтения файлов и прохода по каталогам. Эта папка находится вне пересоздаваемого checkout. Документация Dokploy рекомендует `../files` или именованные тома для [постоянных данных](https://docs.dokploy.com/docs/core/docker-compose).

В терминале сервера, из каталога Compose, выполните:

```sh
docker compose -p PROJECT_NAME --profile tools run --build --rm --no-deps ingest
docker compose -p PROJECT_NAME restart api
```

`PROJECT_NAME` возьмите из команды деплоя в Dokploy; при необходимости добавьте тот же `-f PATH_TO_COMPOSE`. Иначе Docker создаст другой том, который запущенный API не увидит. Запускать команды нужно с тем же Environment/.env, что использует Dokploy.

Постоянные тома: `postgres_data` — история и заявки, `knowledge_index` — индекс, `embedding_cache` — эмбеддинги. API читает индекс только для чтения; профиль `tools` даёт служебной команде запись. При обычном обновлении тома не удаляются. Старый индекс из прежнего bind mount автоматически не переносится: выполните импорт материалов заново.

## Домен

В **Domains → Add Domain**: сервис **frontend**, внутренний порт **80**, путь **`/`**, свой домен, HTTPS. DNS A-запись должна вести на сервер. Сохраните и выполните redeploy. Сгенерированные маршруты и сети доступны в Preview Compose. См. [настройку доменов](https://docs.dokploy.com/docs/core/docker-compose/domains).

## Проверка

В терминале контейнера `api`:

```sh
python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health/ready').read().decode())"
```

Ожидается HTTP 200, `database=true`, `knowledge=true`. Это проверяет БД и индекс; доступ к модели проверяется отдельной репликой. Затем пройдите консультацию → контакты → предпросмотр → подтверждение заявки и проверьте историю после redeploy. Браузерная приёмка этой доработки по просьбе пользователя не выполнялась.

`ENABLE_REVIEW=true` и непустой `REVIEW_TOKEN` включают `/review` и отладку после пересборки. Токен подставляет Nginx; шлюз разрешает только чтение, но сам не проверяет личность посетителя. На публичном домене оставьте review выключенным либо сначала ограничьте доступ к приложению внешней авторизацией. Локально его можно включить на loopback-порте 8080.

## Локальный запуск

```sh
docker compose up -d --build
```

Чат: `http://localhost:8080`, Swagger: `http://localhost:8000/docs`. Для загрузки локальных материалов задайте `CORPUS_DIR_HOST` и выполните ту же команду `ingest`.
