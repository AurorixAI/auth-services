# ALGO V1 — Контекстный файл проекта
> Последнее обновление: 21 февраля 2026 (сессия 12 — очистка + rate limit + httpOnly cookies + CAT merge)
> Этот файл — единый источник истины для AI-ассистента и разработчиков.
> Обновляется после каждого значимого изменения.

---

## ЦЕЛЬ ПРОЕКТА

**ALGO** — адаптивная обучающая платформа по математике (5–11 класс).
Система учится на ошибках учеников, выявляет скрытые зависимости между навыками и строит персональные траектории обучения.

### Ключевые принципы:
- **IRT 3PL** — научно обоснованная оценка уровня учеников (theta)
- **Shadow CAT** — адаптивное тестирование исследовательского уровня
- **DAG навыков** — L1 > L2 > L3 > L4 иерархия с зависимостями
- **Дуальная архитектура** — Ядро (`knowledge_hierarchy`, чистая методика, НИКОГДА не расширяется из книг) + Учебники (`textbook_toc`, 1:1 оглавление)
- **NULLABLE skill_id** — задачи без маппинга на ядро допустимы (textbook-only). CHECK: skill_id ИЛИ toc_id обязательно
- **Пост-экзаменная аналитика** — mapped задачи → полная диагностика (дистракторы, пробелы, deep scan); unmapped → только верно/неверно
- **Обучающаяся система** — Phi-корреляции, Benjamini-Hochberg FDR, обнаружение скрытых связей
- **AI-конвейер** — Mathpix OCR > Gemini Flash > SymPy > Gemini Pro > БД

---

## АРХИТЕКТУРА

### Стек
| Слой | Технология |
|------|-----------|
| Frontend | Next.js 14.1, React 18, TypeScript, Tailwind CSS, Zustand 4, react-query 5, framer-motion |
| Backend | FastAPI, Python 3.11+, SQLAlchemy 2.0 (sync) |
| БД | PostgreSQL 17 |
| Кэш/сессии | Redis 7 (Pickle, TTL 1ч) + in-memory fallback |
| AI (дешёвая) | Gemini 2.5 Flash — извлечение, классификация, дистракторы, TOC |
| AI (дорогая) | Gemini 2.5 Pro — маппинг навыков, аудит скелета, валидация зависимостей |
| OCR | Mathpix API |
| Мат. верификация | SymPy (5 стратегий) |
| Контейнеры | Docker + docker-compose |

### Метрики кодовой базы
| Метрика | Значение |
|---------|----------|
| Python LOC (src/) | ~12,000 |
| Python LOC (scripts/) | ~3,500 |
| TypeScript/TSX LOC (frontend/) | ~8,000 |
| SQL LOC (database/) | ~2,000 |
| Тесты LOC | ~1,200 |
| **Всего LOC** | **~26,000** |
| API эндпоинтов | ~50+ |
| ORM моделей | ~25 |
| Frontend компонентов | 16 |
| Zustand stores | 3 |

### Структура проекта
```
ALGO V1/
├── frontend/                     # Next.js 14 (порт 3000)
│   └── src/
│       ├── app/                  # Страницы (App Router)
│       │   ├── page.tsx          # Landing
│       │   ├── login/            # Авторизация + dev-login
│       │   ├── register/         # 3-step регистрация организации
│       │   ├── d/[code]/         # Shadow CAT диагностика (public, 764 строки)
│       │   ├── e/[code]/         # Сдача экзамена (public, 628 строк)
│       │   ├── invite/[code]/    # Принятие приглашения
│       │   ├── admin/            # Admin панель (AuthGuard: admin/owner/superadmin)
│       │   └── teacher/          # Teacher панель (AuthGuard: teacher+)
│       ├── components/           # 16 glassmorphism UI-компонентов
│       └── lib/                  # api.ts (752 строки, 9 секций), store.ts (3 store), utils
├── src/                          # FastAPI бэкенд (порт 8000)
│   ├── api/                      # REST API (~50+ эндпоинтов)
│   │   ├── main_v2.py            # 1103 строки — главный app + legacy diagnostics
│   │   ├── cat_router.py         # 638 строк — Shadow CAT: start/answer/result
│   │   ├── exam_router.py        # 922 строки — экзамены lifecycle
│   │   ├── textbook_router.py    # ~200 строк — CRUD учебников + skills/tree
│   │   ├── textbook_exam.py      # ~150 строк — экзамены из учебника
│   │   ├── analytics_router.py   # ~200 строк — ~20 эндпоинтов аналитики
│   │   └── redis_sessions.py     # 210 строк — CAT-сессии в Redis
│   ├── auth/                     # JWT + RBAC (6 файлов)
│   ├── diagnostic/               # CAT + IRT + integrity (4 файла, ~3,500 строк)
│   ├── graph/                    # SkillGraph DAG + LearningUpdater + PG Fetcher/Writer (~2,000 строк)
│   ├── pipeline/                 # Content pipeline + TOC + distractor (~2,000 строк)
│   ├── exam_builder/             # ExamGenerator + VariantFactory + Analytics (~2,000 строк)
│   ├── models/                   # ORM (15 моделей) + repositories (8 repos)
│   └── analytics/                # StudentProgress + GroupAnalytics (~760 строк)
├── scripts/                      # CLI-утилиты
│   ├── digitization_pipeline.py  # Тонкий wrapper → src/pipeline (CLI entrypoint)
│   ├── verify_sql.py             # Проверка SQL скелетов
│   └── pipeline_config.json      # Конфигурация конвейера
├── database/                     # SQL (001–011) + seed + скелеты
│   ├── 001–003                   # Core + Presentation + Analytics схемы
│   ├── 004–007                   # Seed + Grade5 skills/tasks/textbook + Auth
│   ├── 009–010                   # textbook_toc + skeleton extension
│   ├── 011                       # Дуальная архитектура (NULLABLE skill_id, CHECK, обновлённые функции)
│   └── skeletons/                # Per-grade SQL: 05 ✅, 06 ✅, 07-11 🔲
│       └── sources/              # PDF методистов
├── tests/                        # ✅ 152 теста проходят
├── docker-compose.yml            # api + postgres:15 + redis:7
├── Dockerfile                    # Multi-stage (3.11-slim)
└── CONTEXT.md                    # ЭТОТ ФАЙЛ
```

### Граф зависимостей
```
Frontend (Next.js) ──→ api.ts ──→ FastAPI (/api/v1/*)
  ├─→ cat_router ──→ ShadowCATEngine ──→ IRTEngine ──→ Redis
  ├─→ exam_router ──→ ExamGenerator ──→ VariantFactory ──→ DB
  ├─→ auth/router ──→ AuthService ──→ DB (PostgreSQL)
  ├─→ analytics_router ──→ StudentProgress / GroupAnalytics ──→ DB
  ├─→ textbook_router ──→ DB (skills/tree)
  └─→ main_v2.py (legacy diagnostics) ──→ ProfessionalEngine ──→ in-memory

Scripts (CLI) ──→ Mathpix OCR + Gemini + SymPy ──→ DB
Self-Learning ──→ BatchGraphUpdater ──→ φ-correlations ──→ skill_correlations
```

### API маршруты (монтирование в main_v2.py)
| Роутер | Префикс | Файл |
|--------|---------|------|
| auth_router | /api/v1/auth/* | src/auth/router.py |
| cat_router | /api/v1/cat/* | src/api/cat_router.py |
| exam_router | /api/v1/exams/* | src/api/exam_router.py |
| textbook_router | /api/v1/textbooks/* | src/api/textbook_router.py |
| textbook_exam_router | /api/v1/* | src/api/textbook_exam.py |
| analytics_router | /api/v1/analytics/* | src/api/analytics_router.py |
| skills_router | /api/v1/skills/* | src/api/skills_router.py |
| dev_router | /api/v1/auth/dev-* | src/auth/dev_router.py (если APP_ENV=development) |

### Auth поток (end-to-end)
1. Login/Register -> authApi -> бэкенд возвращает {user, tokens, org} + ставит **httpOnly cookies** (algo_auth_token, algo_refresh_token)
2. Zustand store сохраняет user/org в localStorage["algo-auth"]
3. api() в api.ts делает fetch с `credentials: "include"` — браузер автоматически отправляет cookie
4. 401 -> auto-refresh через tryRefreshToken() -> /auth/refresh
5. **middleware.ts** → проверяет cookie "algo_auth_token", защищает /admin/*, /teacher/*, /owner/*
6. **Rate limiting**: /auth/apply (3/min), /auth/login (5/min), /auth/refresh (10/min) — Redis-backed + in-memory fallback
7. RBAC на бэкенде: httpOnly cookie -> verify_access_token() -> RoleChecker
8. JWT payload: {sub: user_id, role, org_id, org_role, type, exp, iat}
9. **CORS** — env-configurable: CORS_ORIGINS (default: http://localhost:3000), allow_credentials=True
10. **Dev router** — только в APP_ENV=development
11. **POST /auth/logout** — удаляет httpOnly cookies

### RBAC роли
- **superadmin** — полный доступ
- **owner / admin** — администратор организации (owner маппится на admin в sidebar)
- **teacher** — учитель (экзамены, группы)
- **student** — без JWT, доступ только по invite-коду

---

## ДУАЛЬНАЯ АРХИТЕКТУРА (Сессия 9 — 21.02.2026)

Система разделена на **две независимые подсистемы**:

### 1. Ядро (knowledge_hierarchy) — чистая методика
- Создаётся и поддерживается **только методистами** (skeletons/grade_XX.sql)
- **НИКОГДА** не расширяется автоматически из учебников (skeleton_extender.py удалён)
- Используется для: CAT-диагностики (IRT/SkillGraph), экзаменов BY_CURRICULUM, полной аналитики
- L4-навыки с графом prerequisites

### 2. Учебники (textbook_toc) — 1:1 оглавление
- Создаётся **автоматически** конвейером оцифровки (src/pipeline)
- Все задачи учебника привязаны через `toc_id` (каноничный путь), `textbook_tasks` — доп. метаданные
- Некоторые темы связаны с ядром через `textbook_skill_map`, некоторые — нет

### Таблицы дуального маппинга
```
tasks_master
├── skill_id  → knowledge_hierarchy.id  (FK, **NULLABLE** — NULL для textbook-only)
├── toc_id    → textbook_toc.id         (FK, NULL если не из учебника)
├── tags      → JSONB {"page": 42, "exercise": "3.15", "variant": "а"}
└── CHECK: skill_id IS NOT NULL OR toc_id IS NOT NULL

textbook_toc (таблица 009)
├── id, textbook_id, parent_id (рекурсивная)
├── level: textbook/part/chapter/section/paragraph
├── number: "§3.2", title: "Сложение дробей"
└── page_start, page_end, sort_order

textbook_skill_map (мост: параграф ↔ навык ядра)
├── toc_id → textbook_toc.id
├── skill_id → knowledge_hierarchy.id
├── role: primary/secondary/prerequisite
├── confidence: 0.0–1.0
└── is_main: bool (главный навык параграфа)
```

### Пост-экзаменная аналитика (src/exam_builder/analytics.py)
| Тип задачи | Аналитика |
|---|---|
| **skill_id есть** (mapped) | Дистрактор-анализ, определение пробелов навыков, prerequisite-цепочка, рекомендации |
| **skill_id = NULL** (unmapped) | Только верно/неверно + правильный ответ |

### Удалённое
- `skeleton_extender.py` — удалён, ядро не расширяется из книг
- `generate_skeleton.py`, `parse_pdf.py` — удалены ранее (сессия 8)

---

## ЧТО РЕАЛИЗОВАНО

### Модуль 1: Диагностика (CAT)
| Компонент | Файл | Качество | Статус |
|-----------|------|----------|--------|
| IRT Engine (3PL) | src/diagnostic/irt_engine.py | 10/10 | Работает |
| Shadow CAT Engine | src/diagnostic/cat_engine.py | 10/10 | Работает |
| Integrity Service | src/diagnostic/integrity_service.py | 9/10 | Работает |
| Professional Engine | src/diagnostic/professional_engine.py | 8/10 | Исправлен баг prior |
| Learning Updater | src/graph/learning_updater.py | 9/10 | Работает |

**Алгоритмы:**
- **3PL**: P(t) = c + (1-c)/(1+exp(-a*(t-b))) — EAP(61pt), MLE, MAP
- **Shadow CAT**: a-stratification(warmup) -> Shadow test+balancing(core) -> KL-divergence(refine)
- **Stopping**: MinItems(8) AND (SE<0.30 OR DeltaTheta<0.05) AND ContentCoverage OR MaxItems(40)
- **Integrity**: lz person-fit, Guttman errors, CUSUM change-point, Log-Normal RT
- **Learning**: Phi-коэффициент, Benjamini-Hochberg FDR, EMA weight updates, time-decay(30d)

### Модуль 2: Генератор экзаменов
| Компонент | Файл | Статус |
|-----------|------|--------|
| ExamGenerator | src/exam_builder/exam_generator.py | Работает |
| Template variants | (внутри exam_generator) | Parametric + constraints |
| Exam Router (API) | src/api/exam_router.py | Полный lifecycle |
| Textbook Exam API | src/api/textbook_exam.py | **НОВЫЙ** — экзамен из учебника |

### Модуль 3: AI-конвейер оцифровки
| Компонент | Файл | AI модель | Статус |
|-----------|------|-----------|--------|
| MathpixProcessor | src/pipeline/ocr.py | Mathpix API | Готов |
| GeminiClassifier | src/pipeline/classification.py | gemini-2.5-flash-preview-04-17 | Обновлён |
| SymPyValidator | src/pipeline/validation.py | SymPy | 5 стратегий |
| Orchestrator | src/pipeline/orchestrator.py | — | auto-approve |
| DifficultyAssessor | src/pipeline/difficulty.py | gemini-2.5-flash-preview-04-17 | Обновлён |
| StepDistractorEngine | src/exam_builder/sympy_step_solver.py | SymPy | 6+1 стратегий (broken_step) |
| LLM Templatizer | src/exam_builder/llm_templatizer.py | Gemini Flash + SymPy | Параметрические шаблоны |
| Template Cache | src/exam_builder/template_cache.py | SQLAlchemy | Кэш шаблонов |
| **DigitizationPipeline** | src/pipeline/ (13 модулей) | Полный стек | **Модуляризован** |
| **ExamAnalytics** | src/exam_builder/analytics.py | — | **НОВЫЙ** (Session 9) |

### Модуль 3b: Конвейер оцифровки (scripts/digitization_pipeline.py)
11 модулей в едином CLI:
0. **LegendExtractor** — читает первые страницы учебника, извлекает легенду маркеров (★→advanced, ●→oral, ◆→research и т.д.)
1. **MathpixOCR** — PDF → LaTeX/Markdown (постраничный, кэш)
2. **ParagraphSplitter** — текст учебника → параграфы по TOC
3. **TaskExtractor** — Gemini Flash → structured JSON (answer_type, difficulty, LaTeX, task_marker, task_category)
4. **SymPySolver** — 5 стратегий верификации (direct, equation, simplify, numerical, expression)
5. **AIEnricher** — Gemini Flash → solution_steps, hints, common_mistakes
6. **StepDistractorEngine** — 6+1 стратегий дистракторов (broken_step, sign_error, off_by_one, digit_swap, magnitude_error, rounding_error + random_nearby)
7. **SkeletonTextbookMapper** — Gemini Pro → dual mapping (skill_id + toc_id + tags)
8. **DBWriter** — пакетная запись в PostgreSQL (upsert)
9. **DigitizationPipeline** — оркестратор, инкрементальная обработка по параграфам
10. **CLI** — команды: `pdf`, `json`, `audit`, `status`

#### Маркеры задач (task_marker / task_category)
LegendExtractor автоматически определяет условные обозначения из учебника.
Поддерживаемые категории:
| Категория | Примеры маркеров | is_star | Описание |
|-----------|-----------------|---------|----------|
| standard | (нет маркера) | false | Обычная задача |
| advanced | ★, 🔴, «повыш. сл.» | true | Повышенной сложности |
| olympiad | ★★ | true | Олимпиадная |
| oral | ●, П, «устно» | false | Устная задача |
| research | ◆, «исследование» | false | Исследовательская |
| project | «проект» | false | Проектная работа |
| with_drawing | 📐, «с чертежом» | false | С чертежом/рисунком |

Поток: OCR → **LegendExtractor** (стр. 1-3) → легенда → **TaskExtractor** (промпт с легендой) → task_marker + task_category в каждой задаче.

### Модуль 3c: Генерация скелетов (scripts/generate_skeleton.py)
10-шаговый пайплайн генерации скелета навыков из оглавления учебника:

| Шаг | Класс | AI модель | Описание |
|-----|-------|-----------|----------|
| 1 | TOCExtractor | Flash (multimodal) | Извлечение TOC из PDF-изображений / текста / JSON |
| 2 | ParagraphClassifier | Flash | Классификация: new_topic / review / project / appendix |
| 3 | SkeletonGenerator | Pro | TOC → L1-L3 иерархия |
| 4 | SkeletonGenerator | Pro | L3 → атомарные L4 (few-shot из grade_05.sql) |
| 5 | CrossGradeDedup | Pro | Дедупликация + межклассовые зависимости |
| 6 | PrerequisiteGenerator | Pro | Предпосылки + проверка DAG циклов (BFS) |
| 7 | SkeletonValidator | — | Эвристики: атомарность, структура, префиксы, сироты |
| 8 | SkeletonAuditor | Pro | 6-аспектный AI-аудит |
| 9-10 | SQLGenerator | — | → SQL в формате grade_05.sql |

CLI: `generate` (--pdf-images/--toc-text/--toc-json + --class-level), `review`, `apply`, `cross-check`.
Кэш: `.skeleton_cache/` (промежуточные JSON).

### Фронтенд — страницы
| Страница | Маршрут | Статус |
|----------|---------|--------|
| Лендинг | / | Готов |
| Логин | /login | Готов (+ dev-login) |
| Регистрация | /register | Готов |
| Приглашение | /invite/[code] | Готов |
| Диагностика | /d/[code] | CAT полный |
| Экзамен | /e/[code] | Полный |
| Admin: Диагностики | /admin/diagnostics | Готов |
| Admin: Создать ученика | /admin/students/new | Полная форма + QR |
| Admin: Ученики | /admin/students | Read-only |
| Admin: Настройки | /admin/settings | Готов |
| Teacher: Диагностика | /teacher/diagnostic | Готов |
| Teacher: Экзамены | /teacher/exams | Исправлен (examsApi) |
| Teacher: Создать экзамен | /teacher/exams/new | 5-step wizard |
| Teacher: Настройки | /teacher/settings | Готов |

| Заглушки | dashboard, groups, analytics | Заглушки |

### Sidebar
- Рабочие разделы отделены от «В разработке» визуальным разделителем
- Coming soon элементы приглушены (opacity-50, не кликабельные)
- Owner маппится на admin (owner видит admin-навигацию)

### База данных (seed данные)
| Класс | L1 | L2 | L3 | L4 | Prerequisites | Задачи | Статус |
|-------|----|----|----|----|---------------|--------|--------|
| 5 | 2 | 10 | 46 | ~130 | 80+ | 314 | ✅ Полный (скелет + задачи + учебник) |
| 6 | 2 | 7 | 18 | 34 | 45 | 0 | ✅ Скелет готов (grade_06.sql) |
| 7–11 | — | — | — | — | — | — | 🔲 Шаблоны |

- Файлы 5 класса: `database/005_grade5_skills.sql`, `006_grade5_tasks.sql`, `007_grade5_textbook.sql`
- Файлы 6 класса: `database/skeletons/grade_06.sql`
- **difficulty**: A / B / C — в БД, на фронтенде: Лёгкий / Средний / Сложный

---

## РЕШЕНИЯ И ПРИНЦИПЫ (Сессия 4)

### 1. Тестовые данные → Реальные
Тестовые данные (seed, G5_GEN_*) остаются для разработки. Реальные оцифрованные задачи
получат префикс G5_TB_*. Системную логику менять не нужно — только заменить/добавить данные.

### 2. Единый скелет (knowledge_hierarchy)
ВСЕ навыки — и ФГОС, и из учебников — живут в одной таблице. Нет разделения на is_mandatory.
Колонка `origin` — только метаданные для аналитики, никогда не используется как фильтр.

### 3. ★-задачи (is_advanced)
Повышенной сложности, олимпиадные. CAT показывает такие задачи только ученикам с θ > 1.5.
В обычных экзаменах — по выбору учителя.

### 4. Сложность: A / B / C
- **A** (лёгкий): IRT b = -2.0 … -0.5
- **B** (средний): IRT b = -0.5 … +1.0
- **C** (сложный): IRT b = +1.0 … +2.5
- На фронтенде отображается как: A → «Лёгкий» (зелёный), B → «Средний» (жёлтый), C → «Сложный» (красный)

### 5. difficulty_distribution
При генерации экзамена `difficulty_distribution: {"A": 0.3, "B": 0.5, "C": 0.2}` определяет
пропорцию задач по сложности. Реализовано в exam generation (main_v2.py).

---

## ИСПРАВЛЕННЫЕ БАГИ

### Сессия 11 февраля
1. **NameError: prior** в professional_engine.py — prior = node.mastery_probability
2. **Gemini модели** обновлены на gemini-2.5-flash-preview-04-17 (5 мест)
3. **Модалка** удалена из /admin/diagnostics — ссылка на /admin/students/new
4. **teacher/exams** — fetch с algo-token заменён на examsApi.list()
5. **8 type errors** Next.js: setUser>updateUser, changePassword, class_level и др.
6. **Owner sidebar** — owner маппится на admin (убрана отдельная навигация)
7. ~~**load_from_db()** в skill_graph.py~~ — УДАЛЁН (функциональность в learning_updater.py + professional_engine.py)

### Сессия 12 февраля — P0 & P1 фиксы
8. **CORS** — `allow_origins=["*"]` → env CORS_ORIGINS (default: http://localhost:3000)
9. **Dev router** — обёрнут в `APP_ENV == "development"` guard
10. **Exception handler** — трейсбек только при `DEBUG=true`
11. **middleware.ts** — полностью переписан: cookie-auth, route protection, redirect to /login
12. **syncTokenCookie()** — cookie sync добавлен в store.ts (все auth actions)
13. **Register auto-login** — после applyOrg() авто-редирект по роли вместо кнопки
14. **Teacher diagnostic link** — /teacher/diagnostic/new → /admin/students/new
15. **Sidebar collapse** — useSidebarStore (Zustand), динамический margin в layouts
16. **textbook-api.ts** — объединён в api.ts (textbookApi), удалён дубликат
17. **db.execute("SELECT 1")** — обёрнут в text() (SQLAlchemy 2.0)

### Сессия 3 (12.02.2026) — CAT + экзамены: данные и баг-фиксы
18. **Redirect 404 fix** — /diagnostic/[code] перенаправлял на /d?diagnostic_code= (не существует) → /d/${code}
19. **Seed SQL column fix** — difficulty_level → difficulty в 005_grade5_skills.sql (все 34 задачи)
20. **class_level parsing** — /d/[code] парсил класс из regex "G5-XXXX" (коды рандомные) → user?.class_level
21. **CAT class_level filter** — _load_cat_tasks() теперь фильтрует задачи по class_level_start/end навыка
22. **006_grade5_tasks.sql** — 314 задач на все 124 L4-навыка (100% покрытие), IRT 3PL параметры, 3 уровня сложности
23. **007_grade5_textbook.sql** — учебник «Математика 5 класс» (Виленкин), 10 глав, 47 параграфов, привязка задач к структуре
24. ~~**seed_tasks_grade5.py**~~ — УДАЛЁН (одноразовый генератор, SQL уже в database/)
25. ~~**seed_textbook_grade5.py**~~ — УДАЛЁН (одноразовый генератор, SQL уже в database/)

### Сессия 4 (15.02.2026) — Конвейер оцифровки, дуальный маппинг
26. **Difficulty mapping bug** — /e/[code] проверял "hard"/"medium"/"easy", а БД отдаёт "A"/"B"/"C" → все задачи показывались как «Лёгкий». Исправлено: A/B/C → правильные лейблы
27. **difficulty_distribution не использовался** — генерация экзамена (main_v2.py) не учитывала распределение сложности, задачи выбирались случайно. Исправлено: задачи выбираются с учётом пропорции A/B/C

### Сессия 6 (17.02.2026) — Аудит + очистка
28. Удалено ~2,236 строк мёртвого кода
29. Создан .gitignore
30. database/skeletons/ — структура папки
31. repositories.py: `from .models` → `from .` (после удаления models.py)

### Сессия 7 (17-18.02.2026) — 6 класс + полный аудит
32. `grade_06.sql`: 2 L1, 7 L2, 18 L3, 34 L4, 45 prerequisites — загружен в PostgreSQL
33. `\encoding UTF8` добавлен в grade_06.sql (fix WIN1251 → UTF8)
34. README.md скелетов обновлён (grade_06 ✅)
35. Временные .txt файлы парсинга удалены из sources/
36. Полный проектный аудит: ~25,000 LOC → 20 проблем задокументировано

### Сессия 8 (19.02.2026) — generate_skeleton.py + очистка seed
37. `generate_skeleton.py`: ~960 строк, 10-шаговый пайплайн (TOC→Classification→L1-L4→Dedup→Prereqs→Validation→Audit→SQL)
38. Классы: GeminiClient, TOCExtractor, ParagraphClassifier, SkeletonGenerator, CrossGradeDedup, PrerequisiteGenerator, SkeletonValidator, SkeletonAuditor, SQLGenerator, SkeletonPipeline
39. CLI: 4 подкоманды (generate, review, apply, cross-check)
40. `008_seed_data.sql`: удалена параллельная иерархия ARITH/ALG/GEOM (3 L1, 8 L2, 11 L3, 24 L4, 25 prereqs, 35 tasks, 11 benchmarks). Оставлены только учебники.
41. Тесты: все 84/84 проходят после очистки seed

### Сессия 9 (21.02.2026) — Дуальная архитектура Core + Textbooks
42. **Архитектурный пивот**: Ядро (knowledge_hierarchy) — чистая методика, НИКОГДА не расширяется из книг. Учебники (textbook_toc) — 1:1 оглавление с ВСЕМИ задачами.
43. **011_dual_architecture.sql**: `tasks_master.skill_id` DROP NOT NULL, `task_templates.skill_id` DROP NOT NULL, CHECK (skill_id OR toc_id). Обновлены SQL-функции: `select_exam_tasks` (LEFT JOIN), `get_skills_for_toc` (+unmapped_tasks_count), `v_textbook_coverage` (+tasks_unmapped).
44. **skeleton_extender.py УДАЛЁН** — маппинг задач на ядро только через pipeline (classification.py)
45. **Pipeline обновлён**: `ExtractedTask.skill_id` → `Optional[str] = None`, `db_writer.py` принимает задачи без skill_id (при наличии toc_id), `classification.py` — confidence < 0.5 → `skill_id = None`
46. **exam_generator.py**: `TaskInfo.skill_id` → `Optional[str]`, добавлен `.has_core_mapping` property, `_get_tasks_by_structure()` возвращает unmapped задачи, `_get_tasks_for_class()` фильтрует `WHERE skill_id IS NOT NULL`, JSON export включает `has_core_mapping`
47. **analytics.py СОЗДАН** (src/exam_builder/analytics.py): `ExamAnalytics.analyze()` → `ExamAnalyticsReport` с двумя секциями: mapped (полная диагностика: дистракторы, пробелы, prerequisite-chain) и basic (только верно/неверно)

---

## ИЗВЕСТНЫЕ ПРОБЛЕМЫ (Аудит 21.02.2026)

### 🔴 P0 — Критические
1. ~~**Тесты полностью сломаны**~~ ✅ ИСПРАВЛЕНО — 152 теста проходят.
2. ~~**Два диагностических движка**~~ Частично решено: ShadowCAT обогащён prereq-filter + DEEP_SCAN + skill mastery. ProfessionalEngine остаётся для обратной совместимости (legacy endpoints).
3. ~~**In-memory sessions**~~ ✅ ИСПРАВЛЕНО — ExposureController теперь в Redis.

### 🟠 P1 — Важные
4. **ORM ↔ SQL рассинхрон**: textbook_toc, textbook_skill_map (миграция 009) — нет ORM-моделей. Колонки origin, is_advanced, toc_id, tags — нет в ORM.
5. ~~**`eval()` в variant_factory.py**~~ ✅ Заменён на ast/sympy (Session 7).
6. ~~**Дублирование seed**~~ ✅ ИСПРАВЛЕНО (Session 8).
7. ~~**docker-compose: asyncpg**~~ ✅ ИСПРАВЛЕНО → psycopg2.
8. ~~**Alembic vs raw SQL**~~ ✅ Выбран Alembic (Session 8).

### 🟡 P2 — Средние
9. ~~**dev_router default APP_ENV**~~ ✅ ИСПРАВЛЕНО — default "production" (Session 8).
10. ~~**JWT в localStorage**~~ ✅ ИСПРАВЛЕНО — httpOnly cookies (algo_auth_token, algo_refresh_token).
11. ~~**Нет rate limiting**~~ ✅ ИСПРАВЛЕНО — Redis-backed rate limiter на auth endpoints.
12. ~~**DAG cycle detection**~~ ✅ ИСПРАВЛЕНО (Session 8).
13. ~~**ExposureController in-memory**~~ ✅ ИСПРАВЛЕНО — Redis persistence.
14. **i18n language.tsx** — существует, но почти нигде не используется.

### 🟢 P3 — Низкие
15. _chi_squared_p_value (Wilson-Hilferty) — неточен при df < 3.
16. _latex_to_sympy — regex-based, ограниченная поддержка.
17. ContentPipelineOrchestrator — нет retry/resume.

---

## ROADMAP

### Фаза 0–2: ✅ DONE
- Данные для CAT, баг-фиксы, конвейер оцифровки (код готов)

### Фаза 3: Стабилизация кода ✅ DONE
- [x] Перезаписать тесты под актуальные API-сигнатуры (Session 7)
- [x] Синхронизировать ORM с SQL (Session 7)
- [x] Заменить eval() → ast/sympy в variant_factory (Session 7)
- [x] Удалить legacy diagnosticApi из api.ts (Session 7)
- [x] Удалить aioredis из requirements.txt (Session 7)
- [x] Fix docker-compose DATABASE_URL asyncpg → psycopg2 (Session 7)
- [x] Убрать дублирование seed: 004→008, ON CONFLICT, fix difficulty_level→difficulty (Session 8)
- [x] dev_router guard: default "development"→"production" (Session 8)
- [x] DAG cycle detection в propose_new_prerequisites() (Session 8)
- [x] Выбрать систему миграций → **Alembic** (Session 8)
  - env.py: все 28 ORM-моделей (17 core + 9 auth + 2 exam)
  - 002_baseline — stamp текущей схемы
  - SQL нумерация: устранены дубли (003/006/007 → уникальные номера)
  - SQL файлы остаются для Docker initdb (чистые окружения)

### Фаза 4: Оцифровка учебников (ТЕКУЩАЯ)
- [x] Создать generate_skeleton.py — 10-шаговый пайплайн генерации скелета (Session 8)
- [x] Очистить 008_seed_data.sql от параллельной иерархии ARITH/ALG/GEOM (Session 8)
- [ ] Настроить API ключи (Mathpix, Gemini)
- [ ] Прогнать generate_skeleton.py для 7–11 классов (по одному учебнику)
- [ ] Прогнать audit_skeleton() для 5 класса
- [ ] Оцифровать учебник 5 класса (pipeline)
- [ ] QA + IRT калибровка
- [ ] Оцифровать учебник 6 класса

### Фаза 5: Масштабирование 7–11 классы
- [ ] Скелеты L1-L4 для 7–11 классов
- [ ] Оцифровка учебников + межклассовые зависимости

### Фаза 6: Обучающаяся система
- [ ] BatchGraphUpdater на реальных данных
- [ ] φ-анализ, паттерны ошибок, IRT-рекалибровка

### Фаза 7: Фронтенд доработки
- [ ] Dashboard, группы, аналитика (вместо заглушек)
- [ ] Профиль ученика, /teacher/diagnostic/new
- [ ] i18n на все страницы

---

## КОНФИГУРАЦИЯ

### .env
```
DATABASE_URL=postgresql://algo:algo@localhost:5432/algo_v1
REDIS_URL=redis://localhost:6379
GEMINI_API_KEY=<ключ Google AI>
MATHPIX_APP_ID=<ID>
MATHPIX_APP_KEY=<ключ>
SECRET_KEY=<JWT секрет>
APP_ENV=development          # production → отключает dev_router
CORS_ORIGINS=http://localhost:3000
DEBUG=true                    # false в production
```

### Запуск
```bash
# Бэкенд
cd src && uvicorn api.main_v2:app --reload --port 8000

# Фронтенд
cd frontend && node node_modules/next/dist/bin/next dev --port 3000

# Docker
docker-compose up -d

# Конвейер оцифровки
python scripts/digitization_pipeline.py pdf <путь.pdf> --textbook-id <UUID> --class-level 5
python scripts/digitization_pipeline.py audit --class-level 5
python scripts/digitization_pipeline.py status
```

### Проблемы среды
- PowerShell: кириллический путь (Арслан) — использовать относительные пути
- .next кэш: удалять перед dev после build
- npx заблокирован ExecutionPolicy — node node_modules/next/dist/bin/next

---

## AI-МОДЕЛИ

| Модель | Назначение | Тип |
|--------|-----------|-----|
| gemini-2.5-flash-preview-04-17 | Извлечение, классификация, дистракторы, обогащение | Дешёвая |
| gemini-2.5-pro-preview-05-06 | Маппинг, аудит, зависимости, расширение скелета | Дорогая |
| Mathpix API | OCR учебников (PDF > LaTeX) | Отдельная |

Бюджет: $300 Google Cloud > ~$80–160 на AI (все классы)

---

## ТАБЛИЦЫ БД

### Core (ядро)
KnowledgeHierarchy (+origin, +is_advanced, +source_textbook_id), SkillPrerequisite, TaskMaster (+toc_id, +tags),
SkillBenchmark

### Presentation (учебники)
Textbook, TextbookStructure, TextbookTask, **TextbookToc** (НОВАЯ), **TextbookSkillMap** (НОВАЯ)

### Analytics (ML)
AnswerEvent, SkillCorrelation, ErrorPattern, StudentSkillState

### Exams
GeneratedExam, ExamSession

### Auth
Organization, User, OrganizationMember, Invitation, Student, Group,
StudentGroup, DiagnosticInvite, StudentDiagnosticResult

---

## ЗАМЕТКИ ДЛЯ AI

1. Путь: c:\Users\Арслан\Desktop\ALGO V1\
2. PowerShell: относительные пути из-за кириллицы
3. Frontend: cd frontend + node node_modules/next/dist/bin/next dev --port 3000
4. Backend: cd src + uvicorn api.main_v2:app --reload --port 8000
5. Flash = массовые дешёвые, Pro = точные дорогие
6. Seed данные: skeletons/grade_05.sql (142 скилла, 40 задач), skeletons/grade_06.sql; dev/013 (994 задачи, dev only); **008_seed_data.sql** (учебники)
7. PDF методиста: 5 grade(L1-L4).pdf в корне
8. Тесты: pytest tests/
9. Redis fallback: in-memory dict если Redis недоступен
10. ~~eval() в exam_generator~~: ✅ исправлено — ast/sympy
11. Auth: httpOnly cookies (algo_auth_token, algo_refresh_token), `credentials: "include"` в api.ts
12. API клиент: всегда использовать api() из lib/api.ts, НЕ прямой fetch
13. Middleware: проверяет httpOnly cookie "algo_auth_token"
14. CORS: env CORS_ORIGINS, dev router: APP_ENV guard, allow_credentials=True
15. Sidebar state: useSidebarStore (Zustand) в sidebar.tsx, layouts подписаны
16. **Difficulty хранится как A/B/C в БД**, отображается как Лёгкий/Средний/Сложный на фронтенде
17. **origin в knowledge_hierarchy — ТОЛЬКО метаданные**, НИКОГДА не фильтр. Новые навыки с origin!='base' НЕ создаются автоматически
18. **is_advanced — для ★-задач**, CAT показывает при θ > 1.5
19. **Конвейер оцифровки**: src/pipeline/ (14 модулей, CLI через scripts/digitization_pipeline.py)
20. **Дуальная архитектура**: skill_id (NULLABLE, ядро) + toc_id (учебник) + tags (JSONB). CHECK: хотя бы один
21. **skeleton_extender.py УДАЛЁН** — ядро не расширяется из книг, только методисты
21. **ORM↔SQL синхронизировано** (Session 7): textbook_toc, textbook_skill_map добавлены в ORM; origin, is_advanced, toc_id, tags — добавлены
22. **Два диагностических движка**: ProfessionalDiagnosticEngine (main_v2, in-memory) vs ShadowCAT (shadow_cat_engine, Redis)
23. **docker-compose**: DATABASE_URL=postgresql+psycopg2 (исправлено)
24. **skills/tree API**: GET /api/v1/textbooks/skills/tree?class_level=N — единственный endpoint для дерева навыков
25. **Seed SQL**: 004→008 переименование, ON CONFLICT DO NOTHING, difficulty_level→difficulty (Session 8)
26. **dev_router**: APP_ENV default "production" — для включения установить APP_ENV=development
27. **DAG cycle detection**: propose_new_prerequisites() проверяет циклы через DFS перед добавлением ребра
28. **Миграции — Alembic**: `alembic revision --autogenerate -m "описание"` → `alembic upgrade head`. RAW SQL — только для Docker initdb.
29. **Rate limiting**: src/api/rate_limit.py — Redis-backed + in-memory fallback. Applied: /auth/apply (3/min), /auth/login (5/min), /auth/refresh (10/min)
30. **httpOnly cookies**: algo_auth_token (1h), algo_refresh_token (30d). Cookie-first extraction в dependencies.py, fallback на Bearer header
31. **ExposureController**: Redis persistence (exposure:params, exposure:meta). Global singleton get_exposure_controller()
32. **PG DataFetcher/GraphWriter**: src/graph/pg_data_fetcher.py, pg_graph_writer.py — sync wrappers over repos
33. **CAT DEEP_SCAN**: ShadowCAT обогащён prereq-filter + DEEP_SCAN phase + skill mastery (Beta-binomial)
34. **Cleanup (Session 12)**: Удалены: validators/ (420 LOC), mapping/ (936 LOC), template_generator.py (81 LOC), GCP scripts, prometheus.yml, PDFs. Очищены: requirements.txt (-13 пакетов), .env.example, docker-compose.dev.yml. Извлечён: skills_router.py из main_v2.py

### Сессия 10 (22.02.2026) — LLM Templatizer + Консолидация
48. **sympy_template_engine.py УДАЛЁН** (1038 LOC) — заменён на LLM Templatizer (260 LOC)
49. **llm_templatizer.py СОЗДАН** — templatize_via_llm() через Gemini Flash + SymPy верификация
50. **template_models.py СОЗДАН** — TaskTemplate + ParamInfo dataclasses (46 LOC)
51. **distractor_generator.py УДАЛЁН** (305 LOC) — консолидация на StepDistractorEngine
52. **StepDistractorEngine** — единственный движок дистракторов (6+1 стратегий, broken_step)
53. **variant_orchestrator.py** — переведён на templatize_via_llm + TEMPLATABLE_TYPES фильтр
54. **skill_graph.py УДАЛЁН** (811 LOC) — мёртвый код, 0 импортов, функциональность в learning_updater + professional_engine
55. **4 seed генератора УДАЛЕНЫ** (4377 LOC) — seed_grade5_full, seed_synthetic, seed_tasks_grade5, seed_textbook_grade5
56. **189 тестов** проходят (27 новых для LLM templatizer)
57. **005_grade5_tasks.sql УДАЛЁН** (329 строк, 314 задач G5_GEN) — заменён скелетом + 013
58. **013/014 → database/dev/** — синтетические данные вынесены из основных миграций
59. **001/002 очищены** — удалены stale seed-данные (ARITH/ALG/GEOM, старые UUID a0eebc99-*)
60. **012b_exam_tables.sql СОЗДАН** — DDL для generated_exams + exam_sessions (ранее только ORM)
61. **099 UUID унифицирован** — a0eebc99-* → a1b2c3d4-* (канонический UUID учебника)
62. **database/dev/README.md** — инструкция по загрузке/очистке синтетических данных

### Сессия 11 — Distractor Engine + Error Classification + Smart Format
63. **8 новых step solvers** в sympy_step_solver.py — fraction, power_root, system, inequality, trigonometry, derivative, integral, limit (5-11 класс, ~500 LOC)
64. **TaskMaster.distractor_meta** — JSONB колонка для предрассчитанных дистракторов [{value, error_type, explanation, plausibility, broken_step}]
65. **AnswerEvent.error_type/error_explanation/matched_distractor** — 3 новых колонки для классификации ошибок
66. **CATTask** — добавлены distractor_meta + common_mistakes поля
67. **_determine_answer_format()** — правило: exact_number/decimal с len≤5 → open_input, остальное → multiple_choice (только диагностика)
68. **_build_mc_options()** — сборка MC: distractor_meta → StepDistractorEngine (на лету) → answer_options (fallback)
69. **_match_distractor()** — серверная классификация ошибок: сопоставляет ответ ученика с дистракторами (точное + числовое с допуском)
70. **submit_cat_answer** — интегрирована классификация ошибок: error_type/explanation сохраняются в answer_events
71. **distractor_generator.py УДАЛЁН** (305 LOC) — variant_factory.py переведён на StepDistractorEngine
72. **Pipeline orchestrator** — _generate_distractors() теперь: templatize → StepSolver → StepDistractorEngine (вместо пустого вызова)
73. **ExtractedTask.distractor_meta** — новое поле, DBWriter пишет в tasks_master.distractor_meta
74. **IntegrityService: 9-й детектор RANDOM_MC** — >60% wrong + >50% fast + θ<−1 → подозрение на случайный клик
75. **013_distractor_error_columns.sql** — миграция: distractor_meta JSONB + error columns + индексы
### Сессия 12 — Дистракторы для ВСЕХ типов ответов + очистка мёртвого кода
76. **professional_engine.py УДАЛЁН** (540 LOC) — устаревший 4-фазный движок (LIFT→ADAPTIVE→DEEP_SCAN→SUMMARY), полностью заменён ShadowCATEngine
77. **generate_for_answer()** — единый API в StepDistractorEngine: принимает (correct_answer: str, answer_type: str), маршрутизирует на _fraction_distractors / _expression_distractors / _multi_value_distractors / числовые
78. **AIDistractorGenerator** — enrichment.py, Gemini Pro для текстовых дистракторов (open_text): 3 дистрактора с {value, error_type, explanation, plausibility}
79. **Orchestrator**: _generate_distractors() маршрутизация по типам — exact_number/decimal → SymPy templatize, fraction/expression/multiple_values → generate_for_answer, open_text → AIDistractorGenerator
80. **cat_router._build_mc_options()** — fallback заменён с numeric-only engine.generate() на generate_for_answer() (все типы ответов)
81. **cat_router._match_distractor()** — заменён числовой matching на type-aware: generate_for_answer + _match_answer_value
82. **_match_answer_value()** — новый хелпер для сопоставления ответов по типу: fraction→Fraction, expression→sympy.simplify, numeric→float tolerance, else→string normalize
83. **variant_factory._generate_distractors()** — step 2 fallback заменён с float-only на generate_for_answer() (все типы)
84. **4 мёртвых импорта SymPyTemplateEngine** исправлены → templatize_via_llm (variant_factory ×2, orchestrator, template_generator) — sympy_template_engine.py был удалён в сессии 10
85. **Правило**: дистракторы генерируются ВСЕГДА серверно (для error classification). MC/open формат — только отображение в диагностике, учитель выбирает формат в экзаменах