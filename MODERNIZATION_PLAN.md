# ALGO V2 — План модернизации

> Статус: В работе  
> Дата создания: 2025-04-19  
> Приоритеты: P0 (критично) → P1 (важно) → P2 (улучшение) → P3 (R&D)

---

## Оглавление

1. [P0-1: task_skills M2M таблица](#p0-1-task_skills-m2m-таблица)
2. [P0-2: Backfill существующих связей](#p0-2-backfill-существующих-связей)
3. [P0-3: Фикс db_writer — потеря данных при записи](#p0-3-фикс-db_writer--потеря-данных-при-записи)
4. [P0-4: TaskValidator — чеклист обязательных полей](#p0-4-taskvalidator--чеклист-обязательных-полей)
5. [P0-5: ON CONFLICT — фикс повторной оцифровки](#p0-5-on-conflict--фикс-повторной-оцифровки)
6. [P1-1: Удалить tasks_master.skill_id, обновить ORM](#p1-1-удалить-tasks_masterskill_id-обновить-orm)
7. [P1-2: Новый classification.py — multi-skill маппинг](#p1-2-новый-classificationpy--multi-skill-маппинг)
8. [P1-3: Подключить HybridDifficultyAssessor](#p1-3-подключить-hybriddifficultyassessor)
9. [P1-4: Разделить skeleton-файлы на 2 слоя](#p1-4-разделить-skeleton-файлы-на-2-слоя)
10. [P1-5: Колонки *_source / *_confidence в knowledge_hierarchy](#p1-5-колонки-_source--_confidence-в-knowledge_hierarchy)
11. [P2-1: CATEngine — multi-skill theta update](#p2-1-catengine--multi-skill-theta-update)
12. [P2-2: IRT MFI — weighted information для выбора задач](#p2-2-irt-mfi--weighted-information-для-выбора-задач)
13. [P2-3: Убрать дубликат TaskRepo / TaskRepository](#p2-3-убрать-дубликат-taskrepo--taskrepository)
14. [P2-4: Отчёт после каждого прогона пайплайна](#p2-4-отчёт-после-каждого-прогона-пайплайна)
15. [P3-1: MIRT для композитных задач](#p3-1-mirt-для-композитных-задач)
16. [P3-2: Заполнение grade_07 — grade_11](#p3-2-заполнение-grade_07--grade_11)

---

## P0-1: task_skills M2M таблица

### Как сейчас

**Файл:** `database/001_core_schema.sql:139`
```sql
CREATE TABLE tasks_master (
    skill_id VARCHAR(30) NOT NULL,
    ...
    FOREIGN KEY (skill_id) REFERENCES knowledge_hierarchy(id)
);
```

**ORM:** `src/models/tasks.py:33`
```python
skill_id = Column(String(30), ForeignKey("knowledge_hierarchy.id"), nullable=True)
```

Одна задача → один навык. Жёсткая связь 1:1.

### Что не так

1. **70%+ задач из учебников — композитные.** Пример: «Найдите НОД(24,36) и НОК(12,18), проверьте НОД·НОК = a·b» проверяет 3 навыка (НОД, НОК, свойство произведения), но в БД хранится только один.

2. **Рассинхрон SQL и ORM.** SQL говорит `NOT NULL`, ORM говорит `nullable=True`. Один из них врёт → тонкие баги при миграциях.

3. **CAT теряет информацию.** Ученик решил задачу на 3 навыка → обновляется θ только одного. Два других навыка не получают сигнала.

4. **Поиск задач на навык неполный.** `WHERE skill_id = 'G5_S15_02'` не найдёт задачу, для которой G5_S15_02 — вторичный навык.

### Что даст изменение

- **Корректная диагностика:** CAT увидит все навыки, на которые задача даёт сигнал.
- **weight (0.0–1.0):** распределяет «информацию» 3PL-модели между навыками. Если задача 70% про дроби и 30% про сложение, то θ_дроби обновится сильнее.
- **is_primary:** сохраняет обратную совместимость с UI/аналитикой, где нужен «главный» навык.
- **role ('primary'/'secondary'/'prerequisite'):** задача неявно требует навыка, но не оценивает его.
- **Без этого изменения все следующие шаги (theta update, MFI, pipeline) бессмысленны.**

### Реализация

```sql
CREATE TABLE task_skills (
    task_id    VARCHAR(30) NOT NULL REFERENCES tasks_master(id) ON DELETE CASCADE,
    skill_id   VARCHAR(30) NOT NULL REFERENCES knowledge_hierarchy(id) ON DELETE RESTRICT,
    weight     REAL NOT NULL DEFAULT 1.0 CHECK (weight > 0 AND weight <= 1),
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    role       VARCHAR(20) NOT NULL DEFAULT 'primary'
               CHECK (role IN ('primary','secondary','prerequisite')),
    confidence REAL DEFAULT NULL,  -- уверенность AI в маппинге (0.0–1.0)
    source     VARCHAR(20) DEFAULT 'ai'
               CHECK (source IN ('expert','ai','calibration')),
    PRIMARY KEY (task_id, skill_id)
);

-- Быстрый поиск задач по навыку
CREATE INDEX idx_ts_skill ON task_skills(skill_id);

-- Быстрый поиск навыков по задаче
CREATE INDEX idx_ts_task ON task_skills(task_id);

-- Ровно один primary на задачу
CREATE UNIQUE INDEX idx_ts_primary
    ON task_skills(task_id) WHERE is_primary = TRUE;
```

### Затронутые файлы

| Файл | Что менять |
|------|-----------|
| `alembic/versions/` | Новая миграция |
| `src/models/tasks.py` | Новая ORM-модель `TaskSkill`, relationship на `TaskMaster` |
| `database/001_core_schema.sql` | Добавить CREATE TABLE (для справки) |

---

## P0-2: Backfill существующих связей

### Как сейчас

19 задач уже имеют `skill_id NOT NULL` (seed файлы 008/009 + skeletons).

### Что не так

Если создать `task_skills` и потом удалить колонку `skill_id` — потеряем все 19 существующих связей.

### Что даст изменение

Гарантия что ни одна привязка не потеряется. Все старые задачи получают `is_primary=TRUE, weight=1.0`.

### Реализация

Внутри той же Alembic-миграции:
```sql
INSERT INTO task_skills (task_id, skill_id, weight, is_primary, role, source)
SELECT id, skill_id, 1.0, TRUE, 'primary', 'expert'
FROM tasks_master
WHERE skill_id IS NOT NULL
ON CONFLICT DO NOTHING;

-- Sanity check: убеждаемся, что ничего не потерялось
DO $$
DECLARE
    old_count INTEGER;
    new_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO old_count FROM tasks_master WHERE skill_id IS NOT NULL;
    SELECT COUNT(*) INTO new_count FROM task_skills;
    IF new_count < old_count THEN
        RAISE EXCEPTION 'Backfill lost rows: tasks_master=% task_skills=%', old_count, new_count;
    END IF;
END $$;
```

### Затронутые файлы

| Файл | Что менять |
|------|-----------|
| `alembic/versions/` | Та же миграция что и P0-1 |

---

## P0-3: Фикс db_writer — потеря данных при записи

### Как сейчас

**Файл:** `src/domain/pipeline/db_writer.py:69`

INSERT записывает 23 колонки. Но 5 вычисленных полей **не пишутся**:

| Поле | Вычисляется? | Пишется в INSERT? |
|------|-------------|-------------------|
| `mapping_confidence` | ✅ classification.py | ❌ **теряется** |
| `difficulty_score` (0.00-1.00) | ❌ мёртвый код | ❌ **не пишется** |
| `sympy_solution` | ✅ validation.py | ❌ **не пишется** |
| `sympy_confidence` | ✅ validation.py | ❌ **нет колонки в БД** |
| `verification_status` | — | ⚠️ хардкод 'verified' (ложь) |

### Что не так

1. **`mapping_confidence` теряется.** Pipeline вычисляет уверенность AI в маппинге (0.0–1.0), но db_writer это **игнорирует**. После оцифровки невозможно найти задачи с плохим маппингом — данные потеряны навсегда.

2. **`sympy_solution` не пишется.** SymPy верифицирует ответ и генерирует решение → но в БД колонка `sympy_solution` остаётся NULL. Потеря 100% верификации.

3. **`verification_status = 'verified'` — ложь.** Каждая AI-оцифрованная задача автоматически получает статус «проверена». Ни один человек её не видел. Это обман UI, аналитики и всех downstream-систем.

4. **Нет колонки `mapping_confidence` в БД.** Даже если исправить db_writer — писать некуда. Нужен ALTER TABLE.

### Что даст изменение

- Можно выбрать задачи с `mapping_confidence < 0.7` для ручной проверки.
- `sympy_solution` доступен ученику как проверенное решение.
- `verification_status` честный → UI может показать «эта задача ожидает проверки».
- После M2M: confidence хранится в `task_skills.confidence` per-skill.

### Реализация

1. **ALTER TABLE:**
```sql
ALTER TABLE tasks_master ADD COLUMN mapping_confidence REAL;
ALTER TABLE tasks_master ADD COLUMN sympy_confidence REAL;
```

2. **Фикс db_writer INSERT** — добавить поля:
```python
"mapping_confidence": task.mapping_confidence if task.mapping_confidence > 0 else None,
"sympy_solution": task.sympy_answer or None,
"sympy_confidence": task.sympy_confidence if task.sympy_confidence > 0 else None,
```

3. **Честный verification_status:**
```python
if task.sympy_verified and task.mapping_confidence >= 0.8:
    status = "verified"
elif task.mapping_confidence >= 0.5:
    status = "needs_review"
else:
    status = "pending"
```

### Затронутые файлы

| Файл | Что менять |
|------|-----------|
| `alembic/versions/` | ALTER TABLE (2 новые колонки) |
| `src/domain/pipeline/db_writer.py` | Добавить 3 поля в INSERT, честный status |
| `src/models/tasks.py` | Добавить `mapping_confidence`, `sympy_confidence` в ORM |

---

## P0-4: TaskValidator — чеклист обязательных полей

### Как сейчас

**Файл:** `src/domain/pipeline/db_writer.py:49-53`
```python
if not task.question_text:
    continue
if not task.skill_id and not task.toc_id:
    continue
```

Всего 2 проверки. Задача с пустым ответом, невалидным answer_type, без solution_steps — **запишется в БД без ошибки**.

### Что не так

1. **Нет валидации `correct_answer`.** Задача с пустым ответом запишется → CAT не сможет оценить правильность ответа ученика → 500 ошибка при диагностике.

2. **Нет валидации `answer_type`.** Если Gemini вернёт `"answer_type": "numeric"` вместо `"exact_number"` — CHECK constraint в БД выбросит ошибку, но транзакция может потерять весь пакет задач.

3. **Нет проверки `skill_id` на существование.** AI может вернуть `"G5_NAT_S99_01"` (несуществующий навык) → FK error на INSERT → потеря задачи.

4. **Нет отчёта о пропусках.** Сколько задач потеряно за прогон? Какие? Почему? Узнаем только когда ученик столкнётся с пустотой.

### Что даст изменение

- Гарантия: если задача в БД — она **функциональна** (есть ответ, есть хотя бы 1 шаг решения, маппинг валиден).
- Задачи с неполными данными → `verification_status = 'needs_review'`, а не silent drop.
- Отчёт: «из 150 задач: 130 complete, 15 review, 5 reject (причины: ...)»
- CAT/экзамен никогда не получит битую задачу.

### Реализация

Новый файл `src/domain/pipeline/validator.py`:
```python
@dataclass
class ValidationResult:
    task: ExtractedTask
    level: str           # "complete" | "review" | "reject"
    missing: List[str]   # какие MUST-поля пустые
    warnings: List[str]  # какие SHOULD-поля пустые

VALID_ANSWER_TYPES = {
    'exact_number', 'decimal', 'fraction', 'multiple_values',
    'expression', 'multiple_choice', 'true_false', 'open_text',
    'equation_solution', 'set', 'coordinate', 'inequality'
}

class TaskValidator:
    MUST_HAVE = {
        "question_text": lambda t: len(t.question_text or "") > 10,
        "correct_answer": lambda t: len(t.answer_raw or t.sympy_answer or "") > 0,
        "answer_type": lambda t: t.answer_type in VALID_ANSWER_TYPES,
        "difficulty": lambda t: t.difficulty in ("A", "B", "C"),
        "binding": lambda t: bool(t.skill_id or t.toc_id),
        "solution_steps": lambda t: bool(t.solution_steps and len(t.solution_steps) >= 1),
    }

    SHOULD_HAVE = {
        "skill_id": lambda t: bool(t.skill_id),
        "toc_id": lambda t: bool(t.toc_id),
        "high_confidence": lambda t: t.mapping_confidence >= 0.7,
        "hints": lambda t: bool(t.hints and len(t.hints) >= 1),
        "distractors": lambda t: bool(t.distractor_meta and len(t.distractor_meta) >= 2),
    }

    def validate(self, task: ExtractedTask) -> ValidationResult:
        missing = [k for k, check in self.MUST_HAVE.items() if not check(task)]
        warnings = [k for k, check in self.SHOULD_HAVE.items() if not check(task)]

        if missing:
            level = "reject"
        elif warnings:
            level = "review"
        else:
            level = "complete"

        return ValidationResult(task, level, missing, warnings)
```

Встраивается в оркестратор между mapper и db_writer:
```
Extract → SymPy → Enrich → Distractors → Map → **Validate** → DB Write
```

### Затронутые файлы

| Файл | Что менять |
|------|-----------|
| `src/domain/pipeline/validator.py` | Новый файл |
| `src/domain/pipeline/orchestrator.py` | Добавить этап валидации |
| `src/domain/pipeline/db_writer.py` | Принимать `ValidationResult.level` для `verification_status` |

---

## P0-5: ON CONFLICT — фикс повторной оцифровки

### Как сейчас

**Файл:** `src/domain/pipeline/db_writer.py:85-95`
```sql
ON CONFLICT (id) DO UPDATE SET
    question_text = EXCLUDED.question_text,
    question_latex = EXCLUDED.question_latex,
    correct_answer = EXCLUDED.correct_answer,
    solution_steps = EXCLUDED.solution_steps,
    hints = EXCLUDED.hints,
    common_mistakes = EXCLUDED.common_mistakes,
    distractor_meta = EXCLUDED.distractor_meta,
    answer_options = EXCLUDED.answer_options,
    toc_id = EXCLUDED.toc_id,
    tags = EXCLUDED.tags,
    updated_at = NOW()
```

### Что не так

**11 полей НЕ обновляются** при повторном прогоне:
```
skill_id, difficulty, cognitive_load, answer_type,
irt_discrimination, irt_difficulty, irt_guessing,
source_type, source_reference, verification_status, is_active
```

Сценарий: первый прогон дал неправильный `skill_id = 'G5_NAT_S01_01'` (confidence 0.5). Перезапускаем с улучшенным промптом → Gemini Pro возвращает правильный `'G5_NAT_S15_03'` (confidence 0.95). Но `ON CONFLICT` **не обновит** skill_id → ошибка навсегда в БД.

### Что даст изменение

- Повторный прогон **исправляет** все ошибки первого.
- Можно улучшать промпт Gemini и перезапускать → каждый раз данные лучше.
- `COALESCE(EXCLUDED.x, tasks_master.x)` — если новое значение NULL, оставляем старое (не затираем).

### Реализация

```sql
ON CONFLICT (id) DO UPDATE SET
    question_text = EXCLUDED.question_text,
    question_latex = EXCLUDED.question_latex,
    correct_answer = EXCLUDED.correct_answer,
    answer_type = EXCLUDED.answer_type,
    difficulty = EXCLUDED.difficulty,
    cognitive_load = EXCLUDED.cognitive_load,
    -- skill_id: бери новый если есть, иначе оставь старый
    skill_id = COALESCE(EXCLUDED.skill_id, tasks_master.skill_id),
    irt_discrimination = EXCLUDED.irt_discrimination,
    irt_difficulty = EXCLUDED.irt_difficulty,
    irt_guessing = EXCLUDED.irt_guessing,
    solution_steps = EXCLUDED.solution_steps,
    hints = EXCLUDED.hints,
    common_mistakes = EXCLUDED.common_mistakes,
    distractor_meta = EXCLUDED.distractor_meta,
    answer_options = EXCLUDED.answer_options,
    toc_id = COALESCE(EXCLUDED.toc_id, tasks_master.toc_id),
    tags = EXCLUDED.tags,
    source_reference = EXCLUDED.source_reference,
    mapping_confidence = EXCLUDED.mapping_confidence,
    sympy_solution = COALESCE(EXCLUDED.sympy_solution, tasks_master.sympy_solution),
    verification_status = EXCLUDED.verification_status,
    updated_at = NOW()
```

### Затронутые файлы

| Файл | Что менять |
|------|-----------|
| `src/domain/pipeline/db_writer.py` | Расширить ON CONFLICT SET |

---

## P1-1: Удалить tasks_master.skill_id, обновить ORM

### Как сейчас

`tasks_master.skill_id` читается в **5+ местах**:

| Файл | Строка | Что делает |
|------|--------|-----------|
| `src/domain/diagnostic/cat_engine.py` | L144 | `skill_quota = self.skill_quotas.get(task.skill_id, 0)` — content blueprint |
| `src/repositories/task_repo.py` | L26 | `WHERE TaskMaster.skill_id == skill_id` — поиск задач для навыка |
| `src/models/repositories.py` | L227 | То же (дубликат!) |
| `src/api/exam_router.py` | L1106 | `TaskMaster.skill_id == skill_id` — замена задач в черновике |
| `src/api/exam_router.py` | L1247 | Альтернативные задачи |

### Что не так

1. **Дубликат:** `TaskRepo` в `repositories/` И `TaskRepository` в `models/repositories.py` — разный код, одинаковая задача. Источник путаницы.

2. **Прямой доступ `task.skill_id`** рассыпан по коду. После M2M каждое использование — баг (читает только primary, игнорирует secondary).

3. **Если оставить колонку «для совместимости»** — она будет десинхронизироваться с `task_skills`. Два источника истины = тонкие баги.

### Что даст изменение

- `Pylance/mypy` подсветит ВСЕ 5+ мест красным → не пропустишь ни одно.
- Один источник истины: `task_skills` (через ORM relationship `Task.skills`).
- Вычищается дубликат `TaskRepository`.
- Централизованный доступ: `task.primary_skill_id` (property) и `task.skills` (relationship).

### Реализация (двухфазная)

**Фаза A (эта задача):** Удалить колонку, обновить ORM.
```python
# src/models/tasks.py
class TaskMaster(Base):
    # skill_id УДАЛЁН
    skills = relationship("TaskSkill", back_populates="task", lazy="selectin")

    @property
    def primary_skill_id(self) -> Optional[str]:
        for s in self.skills:
            if s.is_primary:
                return s.skill_id
        return self.skills[0].skill_id if self.skills else None

    @property  
    def skill_id(self) -> Optional[str]:
        """Deprecated backward-compat property."""
        return self.primary_skill_id
```

**Фаза B (P2-3):** Переписать 5 мест + убрать дубликат.

### Затронутые файлы

| Файл | Что менять |
|------|-----------|
| `alembic/versions/` | DROP COLUMN skill_id (отдельная миграция, ПОСЛЕ тестов!) |
| `src/models/tasks.py` | Убрать Column, добавить relationship + property |
| `src/models/` | Новый файл `task_skills.py` или добавить в `tasks.py` |
| `src/domain/diagnostic/cat_engine.py` | L144: `task.skill_id` → `task.primary_skill_id` |
| `src/repositories/task_repo.py` | L26: JOIN task_skills |
| `src/api/exam_router.py` | L1106, L1247: JOIN task_skills |
| `src/models/repositories.py` | **УДАЛИТЬ** дубликат TaskRepository |

### ⚠️ Порядок выполнения

```
P0-1 (create task_skills) →
P0-2 (backfill) →
P1-1 Фаза A (ORM + property) →
Тесты проходят? →
P1-1 Фаза B (DROP COLUMN — отдельная миграция)
```

НЕ удалять колонку в той же миграции что создаёшь task_skills. Минимум 1 день между ними.

---

## P1-2: Новый classification.py — multi-skill маппинг

### Как сейчас

**Файл:** `src/domain/pipeline/classification.py:72-77`
```python
prompt = (
    'Определи ОДИН L4-навык (level="L4").\n'
    'Верни JSON: {"skill_id": "...", "confidence": 0.92, "reasoning": "..."}\n'
)
```

Один навык. Один confidence. Точка.

### Что не так

1. **Композитная задача получает только один skill_id.** «Найди НОД и НОК» → получит либо НОД, либо НОК. Второй навык потерян навсегда.

2. **Нет весов.** Даже если бы мы хранили 2 навыка — как CAT узнает, что задача на 70% про дроби и 30% про сложение?

3. **confidence не хранится.** Gemini возвращает confidence, db_writer его игнорирует. После оцифровки невозможно найти сомнительные маппинги.

4. **Нет валидации skill_id.** AI может вернуть несуществующий ID → FK error → задача потеряна.

### Что даст изменение

- Все навыки задачи сохранены с весами.
- Веса нормализованы: `sum(weights) = 1.0` — можно использовать для theta update.
- Confidence per-skill → можно автоматически находить сомнительные маппинги.
- Валидация: несуществующий skill_id → warning, а не crash.

### Реализация

Новый промпт:
```python
prompt = (
    "Определи ВСЕ L4-навыки, которые задача проверяет.\n"
    "Для каждого укажи:\n"
    "  - weight (0.0–1.0, сумма ВСЕХ weights = 1.0)\n"
    "  - role: 'primary' (главное действие), 'secondary' (промежуточный шаг), "
    "    'prerequisite' (нужен, но не проверяется)\n"
    "  - confidence (0.0–1.0, насколько уверен)\n"
    "  - reasoning (почему именно этот навык)\n\n"
    'Верни JSON:\n'
    '{"skills": [\n'
    '  {"skill_id": "...", "weight": 0.4, "role": "primary", '
    '"confidence": 0.95, "reasoning": "..."},\n'
    '  ...\n'
    ']}\n'
)
```

Валидация ответа:
```python
# 1. Нормализация весов
weights = [s["weight"] for s in data["skills"]]
if abs(sum(weights) - 1.0) > 0.05:
    total = sum(weights)
    for s in data["skills"]:
        s["weight"] /= total

# 2. Валидация skill_id — существует ли в скелете
valid_ids = {s["id"] for s in self.skills_parsed}
for s in data["skills"]:
    if s["skill_id"] not in valid_ids:
        log.warning("AI вернул несуществующий skill_id=%s", s["skill_id"])
        data["skills"].remove(s)

# 3. Ровно 1 primary
primaries = [s for s in data["skills"] if s["role"] == "primary"]
if len(primaries) == 0 and data["skills"]:
    data["skills"][0]["role"] = "primary"
elif len(primaries) > 1:
    # Оставляем primary с максимальным weight
    primaries.sort(key=lambda s: s["weight"], reverse=True)
    for s in primaries[1:]:
        s["role"] = "secondary"
```

Запись через db_writer → INSERT INTO task_skills (N строк на задачу).

### Как AI назначает weight — эвристика

| Сигнал в задаче | Weight |
|----------------|--------|
| Навык — главное вычисление/действие | 0.4–0.6 |
| Навык нужен для промежуточного шага | 0.2–0.3 |
| Навык — предусловие (должен уметь, но не про него задача) | 0.05–0.15 |
| Навык в проверке/обосновании ответа | 0.1–0.2 |

Gemini Pro получает текст + скелет навыков → видит контекст. На confidence < 0.5 мы фильтруем (удаляем навык из списка).

### Затронутые файлы

| Файл | Что менять |
|------|-----------|
| `src/domain/pipeline/classification.py` | Новый промпт, парсинг массива, валидация |
| `src/domain/pipeline/models.py` | `skills: List[Dict]` вместо одного `skill_id` |
| `src/domain/pipeline/db_writer.py` | INSERT INTO task_skills (N строк) |

---

## P1-3: Подключить HybridDifficultyAssessor

### Как сейчас

**Файл:** `src/domain/pipeline/difficulty.py` — **МЁРТВЫЙ КОД**

Класс `HybridDifficultyAssessor` полностью написан (100+ строк). Умеет:
1. Алгоритмический анализ (SymPy): подсчёт операций, вложенности, дробей
2. AI-коррекция (Gemini Flash): нестандартность, ловушки
3. IRT-калибровка из исторических данных

Но оркестратор его **не вызывает**. Сложность назначает Gemini Flash при extraction — одной буквой «B» (default).

### Что не так

1. **`difficulty_score` (0.00-1.00) НИКОГДА не вычисляется.** Колонка в БД пустая для 100% задач из пайплайна.

2. **IRT-параметры хардкодятся:** `A→-1.0, B→0.5, C→1.5` (db_writer.py:65). Это грубая оценка из 3 точек вместо непрерывной шкалы.

3. **Cognitive load назначается AI без перепроверки.** Gemini Flash может сказать «apply» для задачи уровня «analyze».

4. **estimated_time_sec не записывается вообще.** Колонки `avg_time_seconds` в db_writer нет.

### Что даст изменение

- `difficulty_score = 0.73` вместо грубого «B» → IRT точнее с первого дня.
- `irt_difficulty` вычисляется алгоритмически (не хардкод из таблицы A/B/C).
- `cognitive_load` перепроверяется двумя источниками.
- Готовность к автокалибровке: когда будут реальные ответы → `HybridDifficultyAssessor._calibrate()` использует их.

### Реализация

В `orchestrator.py._process_paragraph()`, после extraction и перед SymPy verify:

```python
# После Extract, перед SymPy
from src.domain.pipeline.difficulty import HybridDifficultyAssessor

assessor = HybridDifficultyAssessor(api_key)

for task in tasks:
    result = assessor.assess({
        "text": task.question_text,
        "latex": task.question_latex,
        "answer": task.answer_raw,
        "answer_type": task.answer_type,
    })
    task.difficulty = result["difficulty_abc"]
    task.difficulty_score = result["irt_difficulty"]  # нужно добавить поле
    task.cognitive_load = result["cognitive_load"]
```

Добавить `difficulty_score` в ExtractedTask и в db_writer INSERT.

### Затронутые файлы

| Файл | Что менять |
|------|-----------|
| `src/domain/pipeline/orchestrator.py` | Подключить assessor в _process_paragraph |
| `src/domain/pipeline/models.py` | Добавить `difficulty_score: float = 0.0` |
| `src/domain/pipeline/db_writer.py` | Добавить `difficulty_score` в INSERT |

---

## P1-4: Разделить skeleton-файлы на 2 слоя

### Как сейчас

**Файл:** `database/skeletons/grade_05.sql`

В одном файле смешано:
```sql
INSERT INTO knowledge_hierarchy (
    id, level, parent_id, name, name_ru, description,
    class_level_start, class_level_end,
    importance,              -- ← экспертная оценка
    cognitive_type,          -- ← экспертная оценка
    typical_time_seconds,    -- ← заглушка (30)
    sequence_order
)
```

### Что не так

1. **Невозможно отделить факт от гипотезы.** `id`, `level`, `parent_id`, `name` — это методист (Сардор), факт. `importance=8`, `cognitive_type='apply'` — это экспертная оценка, может меняться.

2. **При переименовании навыка** (структурное изменение) — нельзя не задеть калибровку. Один файл = один коммит = смешанная история.

3. **Data scientist не может сказать «обнули все экспертные оценки и пересчитай»** — потому что они в seed-файле, а не отдельно.

4. **`typical_time_seconds = 30`** для ВСЕХ навыков — это заглушка, притворяющаяся данными.

### Что даст изменение

- **Чистые контракты:** методист не трогает калибровку, DS не трогает структуру.
- **Версионирование:** можно откатить только калибровку без отката структуры.
- **Воспроизводимость:** «свежий старт» = skeletons/ + calibration/. Через 6 месяцев = skeletons/ + реальные данные.
- **Честность:** файл `_expert.sql` явно говорит «это экспертная оценка, не факт».

### Реализация

**Файл 1:** `database/skeletons/grade_05.sql` — только дерево
```sql
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description,
                                 class_level_start, class_level_end, sequence_order)
VALUES ('G5_NAT', 'L1', NULL, 'Natural Numbers', 'Натуральные числа', '...', 5, 5, 1);
```

**Файл 2:** `database/calibration/grade_05_expert.sql` — экспертные оценки
```sql
-- Источник: Сардор Бозоров, 2025-03
-- Тип: экспертная оценка (не эмпирика)

UPDATE knowledge_hierarchy SET
    importance = 10,
    importance_source = 'expert',
    importance_confidence = 0.8
WHERE id = 'G5_NAT';

UPDATE knowledge_hierarchy SET
    cognitive_type = 'apply',
    cognitive_type_source = 'expert',
    typical_time_seconds = 120,
    typical_time_source = 'expert'
WHERE id = 'G5_NAT_S01_01';
```

### Затронутые файлы

| Файл | Что менять |
|------|-----------|
| `database/skeletons/grade_05.sql` | Убрать importance, cognitive_type, typical_time_seconds |
| `database/calibration/grade_05_expert.sql` | Новый файл — UPDATE-стейтменты |
| `database/skeletons/grade_06.sql` | То же |
| `scripts/setup-local.sh` | Добавить выполнение calibration/ после skeletons/ |

---

## P1-5: Колонки *_source / *_confidence в knowledge_hierarchy

### Как сейчас

**Файл:** `database/001_core_schema.sql:32-42`
```sql
importance INTEGER DEFAULT 5,
cognitive_type VARCHAR(20),
typical_time_seconds INTEGER DEFAULT 30,
```

Просто числа. Нет метаданных: откуда взялось, насколько верить, сколько данных.

### Что не так

1. **CAT не может различить** «эксперт сказал importance=8» от «по 10 000 ответам — importance=7.3 ± 0.2».

2. **Если эксперт ошибся** — нет механизма автоматического переопределения. Вручную менять SQL? А потом менять обратно когда данных мало?

3. **UI методиста не может показать** «эту оценку нужно перепроверить» (нет данных о confidence).

### Что даст изменение

Bayesian updating:
$$importance = \alpha \cdot v_{expert} + (1-\alpha) \cdot v_{empirical}, \quad \alpha = \frac{N_0}{N_0 + n_{samples}}$$

→ Система **автоматически** переходит от «доверяю эксперту» к «доверяю данным» по мере накопления ответов. $N_0 \approx 100$ — гиперпараметр (сколько ответов нужно, чтобы начать доверять данным больше чем эксперту).

### Реализация

```sql
ALTER TABLE knowledge_hierarchy ADD COLUMN importance_source VARCHAR(20)
    DEFAULT 'expert' CHECK (importance_source IN ('expert','empirical','mixed'));
ALTER TABLE knowledge_hierarchy ADD COLUMN importance_confidence REAL;
ALTER TABLE knowledge_hierarchy ADD COLUMN importance_n_samples INTEGER DEFAULT 0;

ALTER TABLE knowledge_hierarchy ADD COLUMN cognitive_type_source VARCHAR(20)
    DEFAULT 'expert' CHECK (cognitive_type_source IN ('expert','empirical','mixed'));

ALTER TABLE knowledge_hierarchy ADD COLUMN typical_time_source VARCHAR(20)
    DEFAULT 'expert' CHECK (typical_time_source IN ('expert','empirical','mixed'));
ALTER TABLE knowledge_hierarchy ADD COLUMN typical_time_n_samples INTEGER DEFAULT 0;
```

### Затронутые файлы

| Файл | Что менять |
|------|-----------|
| `alembic/versions/` | ALTER TABLE (6 новых колонок) |
| `src/models/` | Обновить ORM KnowledgeHierarchy |
| `database/calibration/*.sql` | Заполнять *_source при INSERT |

---

## P2-1: CATEngine — multi-skill theta update

### Как сейчас

**Файл:** `src/domain/diagnostic/cat_engine.py:144`
```python
skill_quota = self.skill_quotas.get(task.skill_id, 0)
```

Один ответ → обновляется θ одного навыка.

### Что не так

1. Ученик решил задачу на 3 навыка неправильно → CAT не знает, в каком ошибка → θ всех трёх обновляется одинаково (или только одного, что хуже).

2. Content blueprint считает «1 задача = 1 покрытие навыка». Если задача про 3 навыка — blueprint думает, что покрыт только 1.

### Что даст изменение

Корректное обновление по формуле:

Для каждого навыка k задачи:
$$\theta_k^{new} = \theta_k^{old} + w_k \cdot \frac{\partial \ln L}{\partial \theta_k}$$

где $w_k$ — вес навыка k. На практике: **для каждого навыка вычисляется свой EAP-update**, но information уменьшается пропорционально weight. Это НЕ `w_k · Δθ`, а корректное уменьшение Fisher information:

$$I_k^{effective} = w_k \cdot I(\theta_k)$$

**Результат:** одна составная задача в начале теста даёт информацию по нескольким навыкам → CAT может сократиться с 25 до 18-20 задач без потери точности.

### Реализация

```python
# БЫЛО:
theta[task.skill_id] += delta_theta

# СТАНЕТ:
for ts in task.skills:  # task_skills relationship
    info_k = ts.weight * item_information(theta[ts.skill_id], task.irt_params)
    delta_k = compute_eap_update(theta[ts.skill_id], correct, info_k)
    theta[ts.skill_id] += delta_k
```

### Затронутые файлы

| Файл | Что менять |
|------|-----------|
| `src/domain/diagnostic/cat_engine.py` | `update_theta()` — цикл по task.skills |
| `src/domain/diagnostic/cat_engine.py` | Content blueprint — покрытие всех навыков |
| `tests/test_diagnostic_engine.py` | Тест: задача на 3 навыка → все 3 θ обновились |

---

## P2-2: IRT MFI — weighted information для выбора задач

### Как сейчас

**Файл:** `src/domain/diagnostic/irt_engine.py:60`

`information(theta)` считается для одного скаляра θ. CAT выбирает задачу через `argmax(item.information(θ_skill))` для одного skill_id.

### Что не так

1. Композитная задача оценивается как информативная только по одному навыку.
2. Задача, дающая побочную информацию по другим навыкам, не получает «бонус» при выборе.

### Что даст изменение

Расширенная формула информативности:
$$I_{combined}(task) = \sum_k w_k \cdot I_k(\theta_k)$$

→ задачи, дающие информацию сразу по нескольким «слабым» навыкам, будут предпочтительнее.

**Эффект:** уменьшение длины теста на 15-20% для широких диагностик (5+ навыков).

### Реализация

```python
def combined_information(task, theta_dict):
    total = 0.0
    for ts in task.skills:
        theta_k = theta_dict.get(ts.skill_id, 0.0)
        info_k = item_information_3pl(theta_k, task.irt_a, task.irt_b, task.irt_c)
        total += ts.weight * info_k
    return total
```

### Затронутые файлы

| Файл | Что менять |
|------|-----------|
| `src/domain/diagnostic/irt_engine.py` | Новая функция `combined_information()` |
| `src/domain/diagnostic/cat_engine.py` | `select_next_task()` использует combined MFI |
| `tests/test_irt_engine.py` | Тест: задача на 2 слабых навыка > задача на 1 слабый |

---

## P2-3: Убрать дубликат TaskRepo / TaskRepository

### Как сейчас

Два разных класса делают одно и то же:

| Файл | Класс | Используется в |
|------|-------|---------------|
| `src/repositories/task_repo.py` | `TaskRepo` | cat_router, skill_router |
| `src/models/repositories.py` | `TaskRepository` | exam_router |

### Что не так

1. **Два источника правды.** Один может быть обновлён (JOIN task_skills), другой — нет.
2. **Разный код для одного запроса.** Один использует ORM relationships, другой — raw SQL.
3. **При обновлении забудешь один из двух** → половина системы видит старые данные.

### Что даст изменение

- Один `TaskRepo` с методами: `get_by_skill(skill_id)`, `get_by_toc(toc_id)`, `get_active(limit)`.
- Все consumers (cat_router, exam_router, skill_router) используют один класс.
- Любое изменение в запросах → применяется везде.

### Реализация

1. Удалить `TaskRepository` из `src/models/repositories.py`.
2. Перенести нужные методы в `src/repositories/task_repo.py`.
3. Обновить импорты в `exam_router.py`.

### Затронутые файлы

| Файл | Что менять |
|------|-----------|
| `src/repositories/task_repo.py` | Централизовать все методы |
| `src/models/repositories.py` | Удалить класс TaskRepository |
| `src/api/exam_router.py` | Поменять импорт |

---

## P2-4: Отчёт после каждого прогона пайплайна

### Как сейчас

`db_writer.write_batch()` возвращает `int` (количество записанных). Нет информации о пропусках, warnings, low-confidence маппингах.

### Что не так

Если из 150 задач записалось 120 — где остальные 30? Почему? Нет ответа. Узнаем когда ученик получит 500 ошибку или пустой тест.

### Что даст изменение

JSON-отчёт после каждого прогона:
```json
{
    "timestamp": "2025-04-19T14:30:00",
    "textbook_id": "vilenkin_5",
    "total_extracted": 150,
    "written": 130,
    "needs_review": 12,
    "rejected": 8,
    "rejected_reasons": {
        "no_answer": 3,
        "no_binding": 2,
        "invalid_answer_type": 1,
        "empty_question": 2
    },
    "low_confidence_mappings": [
        {"task_id": "G5_TB_3_45", "confidence": 0.52, "skill_id": "G5_NAT_S09_01"}
    ],
    "skills_coverage": {
        "G5_NAT_S01_01": 5,
        "G5_NAT_S01_02": 3,
        "G5_NAT_S15_03": 0  // ← навык без задач!
    }
}
```

### Затронутые файлы

| Файл | Что менять |
|------|-----------|
| `src/domain/pipeline/db_writer.py` | Собирать статистику, вернуть dict |
| `src/domain/pipeline/orchestrator.py` | Сохранять отчёт в `.pipeline_report_{id}.json` |

---

## P3-1: MIRT для композитных задач

### Как сейчас

3PL — одномерная модель: $P(\theta) = c + (1-c)/(1 + e^{-a(\theta - b)})$. Один θ = один навык.

### Что не так

На композитных задачах теряется математическая корректность: вероятность правильного ответа зависит от вектора $\vec{\theta} = (\theta_1, ..., \theta_K)$, а не одного скаляра.

Workaround через взвешенное усреднение (P2-1) работает практически, но смещён теоретически.

### Что даст изменение

Compensatory MIRT:
$$P(\vec{\theta}) = c + (1-c) \cdot \sigma\left(\sum_k a_k(\theta_k - b)\right)$$

— математически корректная оценка для композитных задач, +5-10% точности на длинных тестах.

### Почему P3 (не сейчас)

- Это R&D на 2-3 недели.
- Workaround через weighted 3PL работает для 90% задач (у большинства 1-2 навыка, не 5).
- Нет реальных данных для калибровки MIRT.
- Делать после стабилизации основной системы и накопления 1000+ ответов.

---

## P3-2: Заполнение grade_07 — grade_11

### Как сейчас

В `database/skeletons/grade_07.sql` и далее — 5 строк-заглушек. CAT для 7-11 классов не работает.

### Что не так

1. Платформа продаётся как «5-11 классы», но реально функционирует только для 5-6.
2. При попытке диагностики в 7 классе движок не найдёт навыков → 500 ошибка.

### Что даст изменение

- Работающий продукт для всей школы (60% рынка — 7-11).
- Единообразие данных: все классы по одному шаблону.

### Почему P3

Это задача **методиста (Сардора)**, не разработчика. Наша задача:
1. Дать шаблон (чистый skeleton формат из P1-4).
2. Дать инструкцию «что заполнять, что оставить пустым».
3. Проверить результат валидатором.

**Выдумывать навыки за методиста** = повторить ошибку, которую сами критикуем.

---

## Порядок выполнения

```
Неделя 1 (P0 — фундамент):
├── P0-1: CREATE TABLE task_skills
├── P0-2: Backfill из tasks_master.skill_id
├── P0-3: Фикс db_writer (потеря данных)
├── P0-4: TaskValidator
└── P0-5: ON CONFLICT фикс

Неделя 2 (P1 — архитектура):
├── P1-1 Фаза A: ORM + property (skill_id остаётся)
├── P1-2: Новый classification.py (multi-skill)
├── P1-3: Подключить HybridDifficultyAssessor
├── P1-4: Разделить skeleton-файлы
└── P1-5: *_source / *_confidence колонки

Неделя 3 (P1 → P2):
├── P1-1 Фаза B: DROP COLUMN skill_id
├── P2-1: CATEngine multi-skill theta update
├── P2-2: IRT MFI с весами
├── P2-3: Убрать дубликат TaskRepo
└── P2-4: Отчёт пайплайна

Roadmap Q3+:
├── P3-1: MIRT
└── P3-2: grade_07-11 (методист)
```

---

## Метрики успеха

| Метрика | Сейчас | После P0-P1 | После P2 |
|---------|--------|-------------|----------|
| Навыков на задачу | 1 (max) | 1-4 | 1-4 |
| Потеря данных при оцифровке | ~15% (silent) | 0% (reject + report) | 0% |
| mapping_confidence доступен | ❌ | ✅ | ✅ |
| difficulty_score (numeric) | ❌ | ✅ | ✅ |
| Повторный прогон исправляет ошибки | ❌ | ✅ | ✅ |
| Длина CAT-теста | 8-25 задач | 8-25 | 7-20 |
| Задачи с честным verification_status | 0% | 100% | 100% |
| Один TaskRepo | ❌ (дубликат) | ❌ | ✅ |
| Скелеты отделены от калибровки | ❌ | ✅ | ✅ |
| Bayesian updating оценок | ❌ | Подготовлено | Работает |
