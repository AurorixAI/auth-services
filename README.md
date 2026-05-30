# auth-service

Сервис аутентификации и управления пользователями. Отвечает за JWT-токены, организации, учителей, студентов и группы.

- **Port**: 8000
- **DB**: `algo_auth` (PostgreSQL 15)
- **Framework**: FastAPI + SQLAlchemy ORM + Alembic
- **Main file**: `src/api/main_v2.py`

## Переменные окружения

| Переменная | Обязательная | Описание | Пример |
|---|---|---|---|
| `DATABASE_URL` | ✅ | PostgreSQL DSN | `postgresql+psycopg2://algo:pass@postgres:5432/algo_auth` |
| `REDIS_URL` | ✅ | Redis DSN | `redis://algo-redis:6379/0` |
| `SECRET_KEY` | ✅ | JWT подпись (≥32 символа) | `super-secret-32-char-key-here` |
| `JWT_SECRET_KEY` | ✅ | Второй JWT ключ | `another-secret-key` |
| `APP_ENV` | — | `production` / `development` | `production` |
| `CORS_ORIGINS` | — | JSON-массив допустимых origins | `["https://algolab.me"]` |
| `EXAM_SERVICE_URL` | — | URL exam-service для колбэков | `http://algo-exam:8003/api/v1` |
| `GEMINI_API_KEY` | — | Google Gemini API | — |
| `MATHPIX_APP_ID` | — | Mathpix OCR | — |
| `MATHPIX_APP_KEY` | — | Mathpix OCR | — |
| `GCP_PROJECT_ID` | — | GCP project ID | `algo-495713` |
| `SENTRY_DSN` | — | Sentry error tracking | — |

## Как запустить

```bash
# Dev (с авто-перезагрузкой)
cp .env.example .env   # заполнить SECRET_KEY, JWT_SECRET_KEY
docker compose up -d --build

# Миграции (если новая БД)
docker exec algo-auth alembic upgrade head

# Prod
docker compose -f docker-compose.prod.yml up -d
```

## API

| Метод | Путь | Описание |
|---|---|---|
| POST | `/api/v1/auth/login` | Получить JWT токен |
| POST | `/api/v1/auth/refresh` | Обновить токен |
| POST | `/api/v1/auth/register` | Регистрация (owner) |
| GET | `/api/v1/organizations/me` | Информация об организации |
| GET/POST | `/api/v1/students/` | Управление студентами |
| GET/POST | `/api/v1/groups/` | Управление группами |
| GET | `/health/live` | Liveness probe |
| GET | `/health/ready` | Readiness probe (DB check) |
| GET | `/metrics` | Prometheus метрики |

Swagger UI (только в `development`): http://localhost:8000/docs

## Тесты

```bash
# Установить зависимости
pip install -r requirements.txt pytest pytest-asyncio pytest-cov

# Запустить
pytest tests/ -v --cov=src --cov-report=term-missing
# Coverage: ~70%, 207 тестов
```
