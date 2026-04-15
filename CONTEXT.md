# ALGO V2 — Единый контекстный файл проекта

> **Последнее обновление:** 15 апреля 2026  
> **Версия:** 2.0  
> **Статус:** MVP функционален, рефакторинг в процессе  
> Этот файл — единственный источник истины. Обновляется после каждого изменения.

---

## 1. ЦЕЛЬ ПРОЕКТА

**ALGO** — адаптивная B2B SaaS платформа диагностики и обучения математике (5–11 класс).

Система:
- Оценивает уровень ученика через адаптивное тестирование (IRT 3PL + Shadow CAT)
- Выявляет корневые пробелы через DAG навыков с зависимостями
- Генерирует персонализированные экзамены из банка задач
- Автоматически оцифровывает учебники (OCR → AI → SymPy → БД)
- Учится на данных ответов (φ-корреляции, Bayesian updates)

---

## 2. СТЕК ТЕХНОЛОГИЙ

| Слой | Технология | Версия |
|------|-----------|--------|
| Frontend | Next.js (App Router), React, TypeScript, Tailwind CSS, Zustand, framer-motion | 14.1, React 18 |
| Backend | FastAPI, SQLAlchemy (sync), Pydantic v2, pydantic-settings | Python 3.11+ |
| БД | PostgreSQL | 15 (Docker) / 17 (local) |
| Кэш/сессии | Redis (Pickle, TTL 1ч) + in-memory fallback | 7 |
| Миграции | Alembic (автоприменение при старте) | — |
| AI | Gemini 2.5 Flash (массовые), Gemini 2.5 Pro (точные) | — |
| OCR | Mathpix API | — |
| Мат. верификация | SymPy | 1.12+ |
| Контейнеры | Docker + docker-compose (dev/prod) | — |

---

## 3. МЕТРИКИ КОДОВОЙ БАЗЫ

| Метрика | Значение |
|---------|----------|
| Python LOC (src/) | ~23,900 |
| Python LOC (tests/) | ~3,050 |
| Python LOC (scripts/) | ~250 |
| SQL LOC (database/) | ~31,000 |
| TypeScript/TSX LOC (frontend/) | ~8,500 |
| **Всего LOC** | **~66,700** |
| API эндпоинтов | 86 |
| ORM моделей | 29 таблиц |
| Frontend компонентов | 16 |
| Frontend страниц | 28 |
| Zustand stores | 3 |

---

## 4. СТРУКТУРА ПРОЕКТА — ТЕКУЩЕЕ СОСТОЯНИЕ

```
ALGOV2/
├── algo-back/                        # FastAPI бэкенд (порт 8000)
│   ├── src/
│   │   ├── main.py                   # Новая точка входа (app factory) — ЧАСТИЧНО (только auth v1)
│   │   ├── __init__.py               # Метаданные пакета
│   │   │
│   │   ├── api/                      # REST API — ОСНОВНАЯ точка входа: main_v2.py
│   │   │   ├── main_v2.py            # FastAPI app + lifespan + все роутеры (рабочий entry point)
│   │   │   ├── cat_router.py         # Shadow CAT: start/answer/result (835 LOC)
│   │   │   ├── exam_router.py        # Экзамены lifecycle (1525 LOC)
│   │   │   ├── textbook_router.py    # CRUD учебников + skills/tree
│   │   │   ├── textbook_exam.py      # Экзамены из учебника (425 LOC)
│   │   │   ├── analytics_router.py   # Аналитика (~20 эндпоинтов)
│   │   │   ├── skills_router.py      # Граф навыков
│   │   │   ├── rate_limit.py         # Redis-backed rate limiter + in-memory fallback
│   │   │   ├── redis_sessions.py     # CAT-сессии в Redis
│   │   │   ├── database.py           # Legacy get_db() (дублирует core/database.py)
│   │   │   └── v1/                   # Новая структура роутеров (в процессе)
│   │   │       ├── router.py         # Master v1 router — подключён ТОЛЬКО auth
│   │   │       └── auth/             # ✅ Auth роутер перенесён в v1
│   │   │           ├── router.py
│   │   │           └── schemas.py
│   │   │
│   │   ├── auth/                     # JWT + RBAC (legacy, используется main_v2.py)
│   │   │   ├── router.py             # Auth endpoints (612 LOC)
│   │   │   ├── service.py            # Монолитный сервис (1167 LOC) ⚠️
│   │   │   ├── models.py             # ORM: User, Organization, Student, Group...
│   │   │   ├── schemas.py            # Pydantic schemas
│   │   │   ├── dependencies.py       # JWT extraction, RoleChecker
│   │   │   └── dev_router.py         # Dev-only quick login (APP_ENV guard)
│   │   │
│   │   ├── core/                     # ✅ Инфраструктурный слой (реализован)
│   │   │   ├── config.py             # Pydantic BaseSettings
│   │   │   ├── database.py           # SQLAlchemy engine + get_db() + db_session()
│   │   │   ├── security.py           # JWT encode/decode, password hashing
│   │   │   ├── exceptions.py         # AlgoException, NotFoundError, ForbiddenError...
│   │   │   └── dependencies.py       # Shared FastAPI Depends
│   │   │
│   │   ├── models/                   # ✅ ORM модели (разделены по доменам)
│   │   │   ├── base.py               # declarative_base()
│   │   │   ├── auth.py               # User, Organization, Student, Group... (9 таблиц)
│   │   │   ├── knowledge.py          # KnowledgeHierarchy, SkillPrerequisite... (5 таблиц)
│   │   │   ├── tasks.py              # TaskMaster, TaskTemplateDB (2 таблицы)
│   │   │   ├── textbooks.py          # Textbook, TextbookStructure, TextbookToc... (5 таблиц)
│   │   │   ├── analytics.py          # AnswerEvent, SessionSummary, StudentSkillState... (5 таблиц)
│   │   │   ├── graph.py              # GraphUpdateLog, IRTCalibrationHistory (2 таблицы)
│   │   │   ├── exam.py               # GeneratedExam, ExamSession (2 таблицы)
│   │   │   ├── dataclasses.py        # Domain dataclasses + enums
│   │   │   └── repositories.py       # Legacy репозитории (дублируют repositories/)
│   │   │
│   │   ├── repositories/             # ✅ Data Access Layer (реализован)
│   │   │   ├── base.py               # Generic BaseRepository
│   │   │   ├── user_repo.py
│   │   │   ├── org_repo.py
│   │   │   ├── student_repo.py
│   │   │   ├── group_repo.py
│   │   │   ├── knowledge_repo.py
│   │   │   ├── task_repo.py
│   │   │   ├── analytics_repo.py
│   │   │   └── graph_repo.py
│   │   │
│   │   ├── services/                 # ✅ Business Logic Layer (файлы созданы)
│   │   │   ├── auth_service.py       # ⚠️ Используется только v1 auth router
│   │   │   ├── org_service.py
│   │   │   ├── student_service.py
│   │   │   ├── group_service.py
│   │   │   ├── cat_service.py        # ⚠️ Не подключён — cat_router работает напрямую с domain/
│   │   │   ├── exam_service.py       # ⚠️ Не подключён
│   │   │   ├── textbook_service.py   # ⚠️ Не подключён
│   │   │   └── analytics_service.py  # ⚠️ Не подключён
│   │   │
│   │   ├── domain/                   # Научные движки (ядро системы)
│   │   │   ├── diagnostic/           # CAT + IRT + Integrity (~3000 LOC)
│   │   │   │   ├── cat_engine.py     # ShadowCATEngine (1315 LOC)
│   │   │   │   ├── irt_engine.py     # 3PL IRT (746 LOC)
│   │   │   │   └── integrity_service.py  # 9 детекторов (932 LOC)
│   │   │   ├── exam_builder/         # Генерация экзаменов + варианты (~4500 LOC)
│   │   │   │   ├── exam_generator.py # ExamGenerator (973 LOC)
│   │   │   │   ├── sympy_step_solver.py  # Step-by-step solver (2101 LOC)
│   │   │   │   ├── variant_orchestrator.py
│   │   │   │   ├── variant_factory.py
│   │   │   │   ├── llm_templatizer.py
│   │   │   │   ├── template_cache.py
│   │   │   │   ├── template_models.py
│   │   │   │   └── analytics.py      # ExamAnalytics
│   │   │   ├── graph/                # Обучающийся граф навыков
│   │   │   │   ├── learning_updater.py  # BatchGraphUpdater (1005 LOC)
│   │   │   │   ├── pg_data_fetcher.py
│   │   │   │   └── pg_graph_writer.py
│   │   │   ├── analytics/            # Аналитика прогресса
│   │   │   │   ├── group_analytics.py
│   │   │   │   └── student_progress.py
│   │   │   └── pipeline/             # Конвейер оцифровки (13 модулей)
│   │   │       ├── orchestrator.py
│   │   │       ├── ocr.py            # Mathpix
│   │   │       ├── extraction.py     # Gemini Flash
│   │   │       ├── classification.py
│   │   │       ├── validation.py     # SymPy (5 стратегий)
│   │   │       ├── enrichment.py     # AI дистракторы
│   │   │       ├── difficulty.py
│   │   │       ├── db_writer.py
│   │   │       ├── gemini_client.py
│   │   │       ├── models.py
│   │   │       ├── config.py
│   │   │       └── cli.py
│   │   │
│   │   └── utils/
│   │       ├── answer_check.py
│   │       └── safe_eval.py
│   │
│   ├── tests/                        # Тесты (требуют httpx для запуска)
│   │   ├── conftest.py
│   │   ├── test_api_integration.py
│   │   ├── test_diagnostic_engine.py
│   │   ├── test_irt_engine.py
│   │   ├── test_learning_graph.py
│   │   ├── test_llm_templatizer.py
│   │   ├── test_template_cache.py
│   │   └── test_variant_engine.py
│   │
│   ├── database/                     # SQL схема + seed данные
│   │   ├── 001-013*.sql              # DDL + seed (применяются Docker initdb)
│   │   ├── skeletons/                # Per-grade SQL (05 ✅, 06 ✅, 07-11 🔲)
│   │   └── dev/                      # Синтетические данные (013/014)
│   │
│   ├── alembic/                      # Миграции
│   │   └── versions/
│   │       ├── 001_baseline.py       # Empty baseline
│   │       └── a55f9fecfdec_sync_nullable_constraints.py
│   │
│   ├── scripts/                      # CLI утилиты
│   ├── Dockerfile / Dockerfile.dev
│   ├── docker-compose.yml / .dev / .prod
│   ├── alembic.ini
│   └── requirements.txt
│
└── algo-front/                       # Next.js 14 фронтенд (порт 3000)
    └── src/
        ├── app/                      # 28 страниц (App Router)
        ├── components/               # 16 UI-компонентов
        ├── lib/                      # api.ts, store.ts, utils
        └── middleware.ts             # Auth cookie protection
```

---

## 5. АРХИТЕКТУРА — ЧТО РЕАЛИЗОВАНО

### 5.1 Целевая архитектура (Service-Repository Pattern)

```
Router (thin HTTP) → Service (business logic) → Repository (DB queries) → ORM Models
         ↓                    ↓                         ↓
   Pydantic schemas    core/exceptions          core/database
```

### 5.2 Текущее состояние миграции на Clean Architecture

| Слой | Цель | Статус |
|------|------|--------|
| `src/core/` | Инфраструктура (config, DB, JWT, exceptions) | ✅ Полностью реализован |
| `src/models/` | ORM модели разделены по доменам | ✅ Полностью реализован (29 таблиц, 7 файлов) |
| `src/repositories/` | Data Access Layer | ✅ Файлы созданы (8 репозиториев) |
| `src/services/` | Business Logic Layer | ⚠️ Файлы созданы, но подключён **только auth** |
| `src/api/v1/` | Новые тонкие роутеры | ⚠️ Перенесён **только auth** |
| `src/domain/` | Научные движки | ✅ Все перенесены из корня src/ |
| `src/main.py` | Новый app factory | ⚠️ Создан, но использует только v1 auth router |

**Рабочая точка входа:** `src/api/main_v2.py` — монтирует ВСЕ роутеры, lifespan с auto-migration.

### 5.3 Дуальная точка входа (текущая ситуация)

```
src/api/main_v2.py  ← РАБОЧИЙ (все 86 эндпоинтов, Alembic auto-migration)
src/main.py         ← НОВЫЙ (app factory, только auth v1, нет auto-migration)
```

Все Docker/uvicorn конфигурации указывают на `src.api.main_v2:app`.

---

## 6. КЛЮЧЕВЫЕ МОДУЛИ

### 6.1 Диагностика (CAT) — `src/domain/diagnostic/`

| Компонент | Файл | Описание |
|-----------|------|----------|
| ShadowCATEngine | cat_engine.py | 4 фазы: WARMUP → CORE → DEEP_SCAN → REFINEMENT |
| IRT Engine | irt_engine.py | 3PL модель, EAP/MLE/MAP theta estimation |
| IntegrityService | integrity_service.py | 9 детекторов (lz, Guttman, CUSUM, RT, RANDOM_MC...) |

**Алгоритмы:**
- 3PL: `P(θ) = c + (1-c)/(1+exp(-a*(θ-b)))`
- Stopping: `MinItems(8) AND (SE<0.30 OR ΔΘ<0.05) AND ContentCoverage OR MaxItems(40)`
- DEEP_SCAN: при обнаружении пробела — проверка prerequisite-цепочки до корневого

### 6.2 Генератор экзаменов — `src/domain/exam_builder/`

| Компонент | Описание |
|-----------|----------|
| ExamGenerator | Сборка экзамена из банка задач (by curriculum / by textbook) |
| VariantOrchestrator | Оркестрация генерации вариантов |
| VariantFactory | Создание вариантов задач |
| LLM Templatizer | Gemini Flash + SymPy → параметрические шаблоны |
| StepDistractorEngine | 6+1 стратегий дистракторов (broken_step, sign_error, off_by_one...) |
| SymPy StepSolver | Step-by-step решения (8 типов: fraction, power, system, trig...) |

### 6.3 Обучающийся граф — `src/domain/graph/`

BatchGraphUpdater: φ-корреляции → Benjamini-Hochberg FDR → обнаружение скрытых зависимостей.

### 6.4 Конвейер оцифровки — `src/domain/pipeline/`

```
PDF → Mathpix OCR → TaskExtractor (Gemini Flash) → SymPy Solver
    → AIEnricher → StepDistractorEngine → SkeletonTextbookMapper → DBWriter
```

CLI: `python scripts/digitization_pipeline.py pdf <файл.pdf> --textbook-id <UUID> --class-level 5`

### 6.5 Auth & RBAC — `src/auth/`

- JWT: httpOnly cookies (algo_auth_token 1ч, algo_refresh_token 30д) + Bearer fallback
- RBAC роли: `superadmin > owner > admin > teacher > student`
- Rate limiting: `/auth/login` (5/min), `/auth/apply` (3/min), `/auth/refresh` (10/min)
- Student: без JWT, доступ только по invite-коду

---

## 7. БАЗА ДАННЫХ

### 7.1 Схема (29 ORM-таблиц + 6 unmanaged)

**Core (ядро диагностики):**

| Таблица | Колонок | Описание |
|---------|---------|----------|
| knowledge_hierarchy | 22 | L1-L4 иерархия навыков |
| skill_prerequisites | 13 | DAG зависимостей |
| skill_benchmarks | 16 | Статистика по навыкам |
| skill_response_times | 15 | Времена ответов |
| skill_correlations | 16 | φ-корреляции между навыками |
| tasks_master | 40 | Банк задач |
| task_templates | 20 | Параметрические шаблоны |

**Учебники (презентационный слой):**

| Таблица | Колонок | Описание |
|---------|---------|----------|
| textbooks | 19 | Каталог учебников |
| textbook_structure | 21 | Структура (главы, параграфы) |
| textbook_toc | 13 | 1:1 оглавление учебника |
| textbook_skill_map | 8 | Мост: параграф ↔ навык ядра |
| textbook_tasks | 12 | Привязка задач к учебникам |

**Аналитика:**

| Таблица | Колонок | Описание |
|---------|---------|----------|
| answer_events | 24 | Каждый ответ ученика |
| session_summaries | 21 | Итоги диагностических сессий |
| student_skill_states | 19 | Текущий уровень по навыкам |
| error_patterns | 13 | Паттерны ошибок |
| graph_updates_log | 13 | Лог обновлений графа |
| irt_calibration_history | 12 | История IRT калибровки |

**Экзамены:**

| Таблица | Колонок | Описание |
|---------|---------|----------|
| generated_exams | 16 | Сгенерированные экзамены |
| exam_sessions | 14 | Сессии сдачи экзаменов |

**Auth (9 таблиц):**
organizations, users, organization_members, invitations, students, groups, student_groups, diagnostic_invites, student_diagnostic_results

**Unmanaged (в БД, нет ORM — Alembic игнорирует):**
audit_log, digitization_log, task_distractors, task_tags, topic_synonyms, exam_variant_cache

### 7.2 Дуальная архитектура

```
tasks_master
├── skill_id  → knowledge_hierarchy (NULLABLE — NULL для textbook-only задач)
├── toc_id    → textbook_toc (NULL если не из учебника)
├── tags      → JSONB {"page": 42, "exercise": "3.15"}
└── CHECK: skill_id IS NOT NULL OR toc_id IS NOT NULL
```

- **skill_id есть** (mapped): полная диагностика, дистракторы, prerequisite-анализ
- **skill_id NULL** (unmapped): только верно/неверно

### 7.3 Данные в БД

| Данные | Количество |
|--------|-----------|
| L1 разделов | 7 |
| L2 тем | 21 |
| L3 пунктов | 67 |
| L4 навыков | 171 |
| Задач | 321 |
| Организаций | 6 |
| Пользователей | 8 |
| Студентов | 33 |
| Учебников | 4 |

**Скелеты навыков по классам:**

| Класс | L4 навыков | Задачи | Статус |
|-------|-----------|--------|--------|
| 5 | 124 | 321 | ✅ Полный (скелет + seed + учебник) |
| 6 | 37 | 0 | ✅ Скелет готов |
| 7 | 2 | 0 | 🔲 Шаблон |
| 8 | 8 | 0 | 🔲 Шаблон |
| 9–11 | 0 | 0 | 🔲 Шаблон |

### 7.4 Миграции

- **SQL файлы** (001–013) — для Docker initdb (первый запуск пустой БД)
- **Alembic** — auto-upgrade при каждом старте приложения (идемпотентно)
- Текущие миграции: `001_baseline` + `a55f9fecfdec_sync_nullable_constraints`
- include_object фильтр игнорирует 6 unmanaged-таблиц
- Workflow: изменил модель → `alembic revision --autogenerate -m "desc"` → применится на следующем запуске

---

## 8. ФРОНТЕНД

### 8.1 Страницы

| Страница | Маршрут | Доступ | Статус |
|----------|---------|--------|--------|
| Лендинг | `/` | Public | ✅ |
| Логин | `/login` | Public | ✅ (+ dev-login) |
| Регистрация | `/register` | Public | ✅ (3-step wizard) |
| Приглашение | `/invite/[code]` | Public | ✅ |
| Диагностика | `/d/[code]` | Public (invite code) | ✅ Shadow CAT |
| Экзамен | `/e/[code]` | Public (exam code) | ✅ |
| Admin: Dashboard | `/admin/dashboard` | admin/owner | 🔲 Заглушка |
| Admin: Диагностики | `/admin/diagnostics` | admin/owner | ✅ |
| Admin: Экзамены | `/admin/exams` | admin/owner | ✅ |
| Admin: Ученики | `/admin/students` | admin/owner | ✅ |
| Admin: Создать ученика | `/admin/students/new` | admin/owner | ✅ (+ QR) |
| Admin: Группы | `/admin/groups` | admin/owner | 🔲 Заглушка |
| Admin: Аналитика | `/admin/analytics` | admin/owner | 🔲 Заглушка |
| Admin: Настройки | `/admin/settings` | admin/owner | ✅ |
| Owner: Dashboard | `/owner/dashboard` | owner | 🔲 Заглушка |
| Owner: Учителя | `/owner/teachers` | owner | 🔲 Заглушка |
| Owner: Группы | `/owner/groups` | owner | 🔲 Заглушка |
| Owner: Аналитика | `/owner/analytics` | owner | 🔲 Заглушка |
| Owner: Настройки | `/owner/settings` | owner | ✅ |
| Teacher: Dashboard | `/teacher/dashboard` | teacher | 🔲 Заглушка |
| Teacher: Диагностика | `/teacher/diagnostic` | teacher | ✅ |
| Teacher: Экзамены | `/teacher/exams` | teacher | ✅ |
| Teacher: Создать экзамен | `/teacher/exams/new` | teacher | ✅ (5-step wizard) |
| Teacher: Черновик | `/teacher/exams/[id]/draft` | teacher | ✅ |
| Teacher: Ученики | `/teacher/students` | teacher | 🔲 Заглушка |
| Teacher: Группы | `/teacher/groups` | teacher | 🔲 Заглушка |
| Teacher: Библиотека | `/teacher/library` | teacher | 🔲 Заглушка |
| Teacher: Аналитика | `/teacher/analytics` | teacher | 🔲 Заглушка |
| Teacher: Настройки | `/teacher/settings` | teacher | ✅ |

### 8.2 Компоненты

- **auth/**: AuthGuard — клиентская защита маршрутов
- **layout/**: Header, Sidebar (collapsible, Zustand store)
- **exam/**: TextbookSelector, TopicTree
- **ui/**: GlassCard, Button, Input, Select, QuestionCard, OptionGrid, TextInputAnswer, TestBackground, TestHeader, AIProgressPill, AnalyzingToast, ComingSoon

### 8.3 Ключевые решения

- Auth: httpOnly cookies + middleware.ts (SSR) + AuthGuard (CSR)
- API: единый `api()` wrapper в `lib/api.ts` с `credentials: "include"`
- State: Zustand с persist в localStorage
- Sidebar: Coming Soon элементы приглушены (opacity-50, не кликабельные)

---

## 9. API МАРШРУТЫ

| Роутер | Префикс | Файл | Эндпоинтов |
|--------|---------|------|------------|
| auth_router | /api/v1/auth/* | src/auth/router.py | ~20 |
| cat_router | /api/v1/cat/* | src/api/cat_router.py | ~10 |
| exam_router | /api/v1/exams/* | src/api/exam_router.py | ~25 |
| textbook_router | /api/v1/textbooks/* | src/api/textbook_router.py | ~10 |
| textbook_exam_router | /api/v1/* | src/api/textbook_exam.py | ~5 |
| analytics_router | /api/v1/analytics/* | src/api/analytics_router.py | ~10 |
| skills_router | /api/v1/skills/* | src/api/skills_router.py | ~3 |
| dev_router | /api/v1/auth/dev-* | src/auth/dev_router.py | ~3 |

---

## 10. КОНФИГУРАЦИЯ

### .env

```
DATABASE_URL=postgresql://postgres:password@localhost:5432/algo_v1
REDIS_URL=redis://localhost:6379
SECRET_KEY=<min 32 chars>
APP_ENV=development         # production → отключает dev_router + скрывает docs
DEBUG=true                  # false → скрывает traceback
CORS_ORIGINS=["http://localhost:3000"]
GEMINI_API_KEY=<ключ>
MATHPIX_APP_ID=<ID>
MATHPIX_APP_KEY=<ключ>
```

### Запуск

**Локально:**
```bash
# Backend
cd algo-back
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
uvicorn src.api.main_v2:app --reload --port 8000

# Frontend
cd algo-front
npm install && npm run dev
```

**Docker:**
```bash
cd algo-back
docker-compose up -d          # Production (postgres + redis + api)
docker-compose -f docker-compose.dev.yml up  # Dev (+ adminer + hot reload)
```

---

## 11. ПРИНЦИПЫ И РЕШЕНИЯ

### Архитектура ядра

- **Скелет навыков** — создаётся ТОЛЬКО методистами (skeletons/grade_XX.sql). Никогда не расширяется автоматически из учебников
- **Дуальный маппинг**: skill_id (ядро, NULLABLE) + toc_id (учебник). CHECK: хотя бы один
- **Difficulty**: A/B/C в БД → "Лёгкий/Средний/Сложный" на фронтенде

### Сложность (IRT)

| Уровень | IRT b | Лейбл |
|---------|-------|-------|
| A | -2.0 … -0.5 | Лёгкий (зелёный) |
| B | -0.5 … +1.0 | Средний (жёлтый) |
| C | +1.0 … +2.5 | Сложный (красный) |

### AI модели

| Модель | Назначение | Стоимость |
|--------|-----------|-----------|
| Gemini 2.5 Flash | Извлечение, классификация, дистракторы, обогащение | $ |
| Gemini 2.5 Pro | Маппинг навыков, аудит, зависимости | $$$ |
| Mathpix API | OCR учебников (PDF → LaTeX) | Отдельная |

---

## 12. ИЗВЕСТНЫЕ ПРОБЛЕМЫ

### 🔴 Критические

1. **Тесты не запускаются** — отсутствует `httpx` в requirements.txt (нужен для FastAPI TestClient)
2. **auth/service.py — монолит (1167 LOC)** — JWT, password, user CRUD, org management, student management, groups — всё в одном файле

### 🟡 Средние

3. **Dual entry point** — `main_v2.py` (рабочий) и `main.py` (частичный) сосуществуют
4. **Legacy `models/repositories.py`** — дублирует `repositories/` (нужно удалить после миграции роутеров)
5. **Legacy `api/database.py`** — дублирует `core/database.py`
6. **Роутеры содержат бизнес-логику** — cat_router (835 LOC), exam_router (1525 LOC) — должны делегировать в services/
7. **Services не подключены** — cat_service, exam_service, textbook_service, analytics_service созданы, но роутеры их не используют
8. **Нет async DB** — синхронный SQLAlchemy Session
9. **Redis единственное хранилище CAT-сессий** — нет backup, при рестарте Redis сессии теряются
10. **Фронт: 10 страниц-заглушек** — dashboard, groups, analytics, library, owner panel

### 🟢 Низкие

11. **i18n (language.tsx)** — существует, но почти нигде не используется
12. **_chi_squared_p_value** — неточен при df < 3
13. **Pipeline** — нет retry/resume при сбое

---

## 13. ROADMAP

### ✅ Фаза 0–3: Фундамент (DONE)

- [x] MVP: CAT диагностика + экзамены + auth + RBAC
- [x] Seed данные 5 класса (скелет + 321 задача + учебник)
- [x] Скелет 6 класса
- [x] Конвейер оцифровки (13 модулей, CLI)
- [x] Дуальная архитектура (skill_id + toc_id)
- [x] StepDistractorEngine (6+1 стратегий)
- [x] LLM Templatizer (Gemini Flash + SymPy)
- [x] httpOnly cookies + rate limiting + CORS
- [x] Clean Architecture: core/ + models/ + repositories/ + services/ (каркас)
- [x] Alembic auto-migration при старте
- [x] ORM ↔ DB полная синхронизация (29 таблиц)

### 🔄 Фаза 4: Завершение рефакторинга (В ПЛАНАХ)

- [ ] Перенести cat_router → api/v1/cat/ + cat_service
- [ ] Перенести exam_router → api/v1/exam/ + exam_service
- [ ] Перенести textbook_router → api/v1/textbook/ + textbook_service
- [ ] Перенести analytics_router → api/v1/analytics/ + analytics_service
- [ ] Перенести skills_router → api/v1/skills/
- [ ] Удалить main_v2.py → main.py единственная точка входа
- [ ] Удалить legacy models/repositories.py и api/database.py
- [ ] Разбить auth/service.py (1167 LOC) на auth_service + org_service + student_service
- [ ] Добавить httpx в requirements.txt и починить тесты

### 🔜 Фаза 5: Оцифровка учебников

- [ ] Настроить API ключи (Mathpix, Gemini)
- [ ] Прогнать generate_skeleton.py для 7–11 классов
- [ ] Оцифровать учебник 5 класса (pipeline)
- [ ] QA + IRT калибровка
- [ ] Оцифровать учебник 6 класса
- [ ] Масштабировать на 7–11 классы

### 📋 Фаза 6: Фронтенд — функциональные страницы

- [ ] Dashboard (admin + teacher + owner)
- [ ] Группы (CRUD + привязка учеников)
- [ ] Аналитика (student progress + group analytics)
- [ ] Библиотека задач (teacher/library)
- [ ] Owner panel (управление учителями)
- [ ] Профиль ученика
- [ ] i18n (русский + казахский)

### 🔮 Фаза 7: Продвинутые функции

- [ ] BatchGraphUpdater на реальных данных
- [ ] φ-анализ, IRT-рекалибровка на реальных ответах
- [ ] Async DB (AsyncSession)
- [ ] CAT session backup (Redis → DB)
- [ ] Monitoring (Prometheus + Grafana)
- [ ] CI/CD pipeline
- [ ] Тестовое покрытие > 80%

---

## 14. ИСТОРИЯ ИЗМЕНЕНИЙ

### Апрель 2026 — Стабилизация и миграции

- Исправлена CORS конфигурация (os.getenv → pydantic Settings)
- Добавлена колонка `student_phone` в БД
- Полная синхронизация ORM ↔ DB (10 расхождений исправлено)
- Настроен Alembic: baseline + auto-upgrade при старте + include_object фильтр
- Применены миграции 009, 011 (частично), 012 к БД
- Обновлены модели: analytics.py (org_id), tasks.py (nullable fixes)
- Документация: объединена из 5 файлов в единый CONTEXT.md

### Март 2026 — Clean Architecture каркас

- Создан `src/core/` (config, database, security, exceptions, dependencies)
- Разделён `models/__init__.py` на 7 файлов по доменам
- Созданы `repositories/` (8 файлов) и `services/` (8 файлов)
- Перенесён auth router в `api/v1/auth/`
- Перемещены domain модули в `src/domain/`
- Создан `src/main.py` (app factory)

### Февраль 2026 — Функциональное ядро

- Дуальная архитектура (dual mapping skill_id + toc_id)
- LLM Templatizer + StepDistractorEngine
- Error classification (distractor_meta, error_type)
- Конвейер оцифровки (13 модулей)
- generate_skeleton.py (10-шаговый pipeline)
- httpOnly cookies, rate limiting, RBAC
- Удалён professional_engine.py, skeleton_extender.py

---

## 15. ЗАМЕТКИ ДЛЯ AI-АССИСТЕНТА

1. **Рабочая точка входа:** `uvicorn src.api.main_v2:app --reload --port 8000` (НЕ src.main:app)
2. **Путь:** `c:\Users\Арслан\Desktop\ALGOV2\`
3. **PowerShell:** кириллический путь — использовать относительные пути
4. **Frontend:** `cd algo-front && npm run dev` (порт 3000)
5. **PostgreSQL:** user=postgres, password=password, db=algo_v1, port=5432
6. **Redis:** не установлен локально — in-memory fallback
7. **.env:** уже создан в корне algo-back/
8. **Difficulty**: A/B/C в БД (не easy/medium/hard)
9. **origin в knowledge_hierarchy** — ТОЛЬКО метаданные, НИКОГДА фильтр
10. **Alembic auto-migration:** встроен в lifespan main_v2.py
11. **Unmanaged tables** (не трогать): audit_log, digitization_log, task_distractors, task_tags, topic_synonyms, exam_variant_cache
12. **После каждого значимого изменения** — обновить этот файл (секции 3, 5.2, 7.3, 12, 13, 14)
