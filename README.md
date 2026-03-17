# 🎯 ALGO V1 - Adaptive Learning & Gap Optimization

**Интеллектуальная платформа диагностики математических навыков**

Система точной диагностики уровня ученика с адаптивным тестированием и поиском корневых пробелов.

---

## 📋 Содержание

- [Архитектура](#архитектура)
- [Быстрый старт](#быстрый-старт)
- [Структура проекта](#структура-проекта)
- [База данных](#база-данных)
- [Компоненты](#компоненты)
- [API Reference](#api-reference)

---

## 🏗️ Архитектура

### Гибридный подход

ALGO использует **гибридную архитектуру** для максимальной точности диагностики и гибкости интерфейса:

```
┌─────────────────────────────────────────────────────────────┐
│                    PRESENTATION LAYER                        │
│  (Учебники, структура "как в книге", удобство учителей)     │
├─────────────────────────────────────────────────────────────┤
│  textbooks │ textbook_structure │ textbook_tasks             │
└─────────────────────────┬───────────────────────────────────┘
                          │ Маппинг
                          ↓
┌─────────────────────────────────────────────────────────────┐
│                      CORE LAYER                              │
│  (Точная диагностика, атомарные навыки, граф зависимостей)  │
├─────────────────────────────────────────────────────────────┤
│  knowledge_hierarchy (L1-L4) │ skill_prerequisites           │
│  skill_benchmarks │ tasks_master │ task_templates            │
└─────────────────────────────────────────────────────────────┘
```

### Иерархия знаний (L1-L4)

| Уровень | Название | Пример |
|---------|----------|--------|
| **L1** | Domain | Алгебра |
| **L2** | Cluster | Уравнения |
| **L3** | Topic | Квадратные уравнения |
| **L4** | Atomic Skill | Вычисление дискриминанта |

### Правило атомарности

> **L4 навык должен проверяться ОДНИМ вопросом и НЕ зависеть от других навыков.**

❌ **Плохо:** "Решение линейных уравнений"  
✅ **Хорошо:** "Перенос числа через знак равенства"

---

## 🚀 Быстрый старт

### 1. Клонирование и настройка

```bash
# Клонируйте репозиторий
git clone <repo-url>
cd "ALGO V1"

# Создайте виртуальное окружение
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # Linux/Mac

# Установите зависимости
pip install -r requirements.txt

# Скопируйте конфигурацию
copy .env.example .env
# Отредактируйте .env и добавьте API ключи
```

### 2. Настройка базы данных

```bash
# Создайте базу данных PostgreSQL
createdb algo_db

# Примените миграции
psql -d algo_db -f database/001_core_schema.sql
psql -d algo_db -f database/002_presentation_schema.sql
```

### 3. Проверка установки

```bash
# Запуск тестов
pytest tests/ -x -q

# Запуск API сервера
uvicorn src.api.main_v2:app --reload --port 8000
# Документация API: http://localhost:8000/docs

# Запуск фронтенда
cd frontend && npm install && npm run dev
# http://localhost:3000
```

---

## 📁 Структура проекта

```
ALGO V1/
├── frontend/                       # Next.js 14 (порт 3000)
│   └── src/
│       ├── app/                    # Страницы (App Router)
│       ├── components/             # UI-компоненты
│       └── lib/                    # api.ts, store.ts, utils
│
├── src/                            # FastAPI бэкенд (порт 8000)
│   ├── api/                        # REST API роутеры
│   │   ├── main_v2.py              # FastAPI app + health + CORS
│   │   ├── cat_router.py           # Shadow CAT: start/answer/result
│   │   ├── exam_router.py          # Экзамены lifecycle
│   │   ├── skills_router.py        # Граф навыков CRUD
│   │   ├── textbook_router.py      # CRUD учебников
│   │   ├── analytics_router.py     # Аналитика
│   │   ├── rate_limit.py           # Rate limiting (Redis + in-memory)
│   │   └── redis_sessions.py       # CAT-сессии в Redis
│   ├── auth/                       # JWT + RBAC + httpOnly cookies
│   ├── diagnostic/                 # CAT + IRT + integrity
│   ├── graph/                      # LearningUpdater + PG DataFetcher/Writer
│   ├── pipeline/                   # Конвейер оцифровки (13 модулей)
│   ├── exam_builder/               # ExamGenerator + VariantFactory + Analytics
│   ├── models/                     # ORM (15+ моделей) + repositories
│   └── analytics/                  # StudentProgress + GroupAnalytics
│
├── database/
│   ├── 001-003                     # DDL: Core + Presentation + Analytics
│   ├── 005-008                     # Seed: grade5 skills/tasks/textbook
│   ├── 009-012                     # textbook_toc, skeleton ext, dual arch, variants
│   └── skeletons/                  # Per-grade SQL: 05 ✅, 06 ✅, 07-11 🔲
│
├── scripts/                        # CLI-утилиты
├── tests/                          # pytest (189 тестов)
├── alembic/                        # Миграции БД
├── docker-compose.yml              # api + postgres + redis
└── requirements.txt
```├── .env.example                    # Шаблон конфигурации
├── requirements.txt                # Python зависимости
└── README.md                       # Документация
```

---

## 🗄️ База данных

### CORE Layer (Ядро диагностики)

| Таблица | Описание |
|---------|----------|
| `knowledge_hierarchy` | Иерархия L1-L4, атомарные навыки |
| `skill_prerequisites` | Граф зависимостей между навыками |
| `skill_benchmarks` | Статистика по навыкам (время, успешность) |
| `tasks_master` | Банк задач |
| `task_templates` | Параметрические шаблоны для генерации |

### PRESENTATION Layer (Слой учебников)

| Таблица | Описание |
|---------|----------|
| `textbooks` | Каталог учебников |
| `textbook_structure` | Структура учебника (главы, параграфы) |
| `textbook_tasks` | Привязка задач к учебникам |
| `topic_synonyms` | Синонимы названий тем |
| `digitization_log` | Лог оцифровки |

### Пример запроса: Получить навыки с пробелами

```sql
SELECT 
    kh.id,
    kh.name_ru,
    sb.success_rate_grade_8 as success_rate,
    COUNT(sp.prerequisite_id) as blocked_skills_count
FROM knowledge_hierarchy kh
JOIN skill_benchmarks sb ON kh.id = sb.skill_id
LEFT JOIN skill_prerequisites sp ON kh.id = sp.prerequisite_id
WHERE kh.level = 'L4'
  AND sb.success_rate_grade_8 < 50
GROUP BY kh.id, kh.name_ru, sb.success_rate_grade_8
ORDER BY blocked_skills_count DESC;
```

---

## 🧩 Компоненты

### 1. Shadow CAT Engine (Модуль 1)

Адаптивное тестирование на основе IRT 3PL:
- **WARMUP** → a-stratification для начальной оценки
- **CORE** → Shadow test + item balancing + prereq-filter
- **DEEP_SCAN** → целенаправленная проверка пробелов
- **REFINEMENT** → KL-divergence для уточнения θ

### 2. ExamGenerator (Модуль 2)

Генератор тестов из оригинальных задач. Шаблоны — дополнительная опция.

```python
from src.exam_builder.exam_generator import ExamGenerator, ExamConfig, ExamType, VariantMode

generator = ExamGenerator()

config = ExamConfig(
    title="Контрольная работа",
    exam_type=ExamType.TEST,
    textbook_id="MAKARYCHEV_8",
    paragraphs=["1.1", "1.2"],
    tasks_count=10,
    variant_mode=VariantMode.ORIGINAL
)

exam = generator.generate_exam(config)
```

### 3. ContentPipeline (Модуль 3)

Конвейер оцифровки учебников (13 модулей):

```
OCR (Mathpix) → TaskExtractor (Gemini Flash) → SymPySolver
→ AIEnricher → StepDistractorEngine → SkeletonTextbookMapper
→ DBWriter → DigitizationPipeline (оркестратор)
```

CLI: `python scripts/digitization_pipeline.py pdf <путь.pdf> --textbook-id <UUID> --class-level 5`

### 4. Variant Engine (Модуль 4)

Генерация вариантов задач через LLM-темплатизацию (Gemini Flash + SymPy верификация).
StepDistractorEngine — 6+1 стратегий дистракторов. Кэш шаблонов в БД (TaskTemplateDB).

---

## 🔄 Алгоритм диагностики

### Фазы

1. **Калибровка** (3-5 вопросов) — определение начального уровня
2. **Лифт-алгоритм** — адаптивное ветвление A→B→C
3. **Deep Scan** — поиск корневых пробелов через prerequisites

### Лифт-алгоритм

```
           ┌───────────────┐
           │  Уровень C    │ ← Мастерство
           └───────┬───────┘
      ✓ правильно  │  ✗ ошибка
           ↑       │       ↓
           ┌───────┴───────┐
           │  Уровень B    │ ← Стандарт
           └───────┬───────┘
      ✓ правильно  │  ✗ ошибка
           ↑       │       ↓
           ┌───────┴───────┐
           │  Уровень A    │ ← Базовый
           └───────┬───────┘
                   │  ✗ ошибка
                   ↓
           ┌───────────────┐
           │   ПРОБЕЛ      │ → Deep Scan
           └───────────────┘
```

### Deep Scan

При обнаружении пробела система проверяет prerequisites:

```
ALG_EQ_QUAD_SOLVE (❌ пробел)
    ↓ Deep Scan
ALG_EQ_QUAD_FORMULA (проверяем...)
    ↓ если ❌
ALG_EQ_QUAD_DISCRIM (🔴 КОРЕНЬ ПРОБЕЛА!)
```

---

## 📊 Метрики качества

### Автоутверждение задач (~70%)

Задача утверждается автоматически, если:
- ✅ Уверенность AI ≥ 90%
- ✅ Сложность A или B
- ✅ SymPy подтвердил ответ
- ✅ Quality score шаблона ≥ 85%
- ✅ Не композитный навык

### Ручная проверка (~30%)

- Все Level C задачи
- Конфликты (SymPy vs учебник)
- Композитные навыки
- Низкая уверенность AI

---

## 🛠️ Разработка

### Запуск тестов

```bash
pytest tests/ -v --cov=src
```

### Линтинг

```bash
ruff check src/
black src/
mypy src/
```

---

## 📝 Лицензия

MIT License

---

## 👥 Команда

- **Архитектура** — Hybrid Graph Design
- **AI** — Gemini для классификации
- **Math** — SymPy для валидации
- **OCR** — Mathpix для оцифровки
