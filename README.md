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

### Быстрый полный прогон на своей машине (пакет #140)

```bash
pip install pytest-xdist==3.8.0      # один раз
scripts/test_fast.sh                 # весь набор: до 4 воркеров, у каждого своя БД
scripts/test_fast.sh tests/test_payroll_pg.py -k rates   # те же ключи, что у pytest
WORKERS=2 scripts/test_fast.sh       # если Mac греется (выше 4 скрипт не поднимется)
```

- Без `A2_AUTH_TEST_DATABASE_URL` скрипт сам поднимает одноразовый `postgres:15-alpine` (fsync выключен) и убирает его в конце. Со своими переменными работает на вашем сервере: воркер `gwK` берёт копию БД `<имя>_gwK` (её создаёт `tests/conftest.py`), в конце копии удаляются (`KEEP_DBS=1` — оставить).
- Файлы тестов целиком достаются одному воркеру (`--dist loadfile`), длинные файлы стартуют первыми (по `.github/ci_test_durations.json`). Порядок тестов внутри файла такой же, как в CI.
- Замер 01.11.2026 (Mac, 10 ядер, 4 воркера): весь набор около 13 минут против 46 минут одним потоком.
- Пароли в тестах хэшируются bcrypt с ценой 4, а не 12 (`tests/conftest.py`); тест про боевую цену помечается `@pytest.mark.real_bcrypt`.
- Даты в тестах: `date.today()` и «now − N дней» запрещены (`tests/test_no_real_clock_guard.py`, LESSONS 76/82): день — `business_today()`, запись ученика — `ENROLLED_SINCE` из `tests/pg_world.py`.

### CI (GitHub Actions)

- 8 потоков по целым файлам тестов (`.github/scripts/ci_shard.py`, замеры в `.github/ci_test_durations.json`; обновить: `ci_shard.py --rebuild junit-*.xml` по артефактам последних прогонов).
- Ветка `main`: если тот же sha уже прошёл все тест-джобы на `develop`, потоки пропускаются, в логе строка «sha … прошёл тесты в run N (develop)» (`.github/scripts/ci_gate.py`; при любом сомнении тесты идут).
- Деплой `main` берёт образ, собранный для stage, если на сервере есть образ с той же ревизией, архивом исходников, отпечатком сборки и сервисом (`.github/scripts/release_image.py`); иначе собирает, как раньше.
