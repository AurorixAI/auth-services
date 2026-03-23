# SYSTEM_STATE.md — ALGO V1 Backend

> **Status:** Phase 1 ✅ Complete | Phase 2 ✅ **Architecture Fully Designed** | Phase 3 🔄 Ready to begin  
> **Last Updated:** 2026-03-22  
> **Author:** Antigravity (AI Senior Architect)

---

## Table of Contents

1. [Phase 1 — Contextual Audit](#phase-1--contextual-audit)
   - [Directory Structure](#directory-structure)
   - [Key Components](#key-components)
   - [Data Flow](#data-flow)
   - [Bottlenecks & Code Smells](#bottlenecks--code-smells)
   - [Context Snapshot](#context-snapshot)
2. [Phase 2 — Architectural Blueprint](#phase-2--architectural-blueprint)
   - [Target Architecture](#target-architecture)
   - [Rationale](#rationale)
3. [Phase 3 — Incremental Refactoring](#phase-3--incremental-refactoring)
   - [Change Log](#change-log)
   - [Next Steps](#next-steps)

---

## Phase 1 — Contextual Audit

### Directory Structure

```
algo-back/
├── alembic/              # DB migrations (14+ versions)
│   └── versions/
├── scripts/              # CLI utilities
├── src/
│   ├── __init__.py       # Package root — re-exports top-level API objects
│   ├── analytics/        # Group & student progress analytics
│   │   ├── group_analytics.py        (~16 KB)
│   │   └── student_progress.py       (~11 KB)
│   ├── api/              # FastAPI routers + infrastructure (FLAT — no versioning)
│   │   ├── main_v2.py                (~7 KB) ← App entry point
│   │   ├── database.py               (~1 KB) ← DB engine + get_db()
│   │   ├── rate_limit.py             (~4 KB) ← Redis-backed rate limiter
│   │   ├── redis_sessions.py         (~10 KB)
│   │   ├── cat_router.py             (~30 KB) ⚠️ Very large router
│   │   ├── exam_router.py            (~56 KB) ⚠️ Largest file in project
│   │   ├── textbook_router.py        (~13 KB)
│   │   ├── textbook_exam.py          (~14 KB)
│   │   ├── analytics_router.py       (~10 KB)
│   │   └── skills_router.py          (~4 KB)
│   ├── auth/             # Authentication & RBAC
│   │   ├── router.py                 (~24 KB) ⚠️ Very large router
│   │   ├── service.py                (~45 KB) ⚠️ Monolithic service
│   │   ├── models.py                 (~18 KB) ← SQLAlchemy models (auth domain)
│   │   ├── schemas.py                (~10 KB) ← Pydantic v2 schemas
│   │   ├── dependencies.py           (~5 KB)  ← JWT + RBAC dependencies
│   │   └── dev_router.py             (~6 KB)  ← Dev-only quick login
│   ├── diagnostic/       # CAT Engine (Module 1)
│   │   ├── cat_engine.py             (~53 KB) ← ShadowCATEngine
│   │   ├── integrity_service.py      (~37 KB)
│   │   └── irt_engine.py             (~30 KB)
│   ├── exam_builder/     # Exam generation (Module 2)
│   │   ├── exam_generator.py         (~39 KB)
│   │   ├── variant_orchestrator.py   (~22 KB)
│   │   ├── variant_factory.py        (~17 KB)
│   │   ├── analytics.py              (~18 KB)
│   │   ├── sympy_step_solver.py      (~75 KB) ⚠️ Largest file overall
│   │   ├── template_cache.py         (~10 KB)
│   │   ├── llm_templatizer.py        (~12 KB)
│   │   └── template_models.py        (~2 KB)
│   ├── graph/            # Learning graph updater
│   │   ├── learning_updater.py       (~40 KB)
│   │   ├── pg_data_fetcher.py        (~2 KB)
│   │   └── pg_graph_writer.py        (~3 KB)
│   ├── models/           # Shared SQLAlchemy models + repositories
│   │   ├── __init__.py               (~35 KB) ⚠️ Monolithic models file
│   │   ├── dataclasses.py            (~4 KB)  ← Domain dataclasses + enums
│   │   ├── exam.py                   (~3 KB)
│   │   └── repositories.py           (~28 KB) ← Repository layer
│   ├── pipeline/         # OCR + digitization pipeline
│   │   ├── orchestrator.py           (~15 KB)
│   │   ├── extraction.py             (~13 KB)
│   │   ├── enrichment.py             (~7 KB)
│   │   ├── classification.py         (~5 KB)
│   │   ├── difficulty.py             (~7 KB)
│   │   ├── validation.py             (~4 KB)
│   │   ├── db_writer.py              (~7 KB)
│   │   ├── gemini_client.py          (~5 KB)
│   │   ├── ocr.py                    (~5 KB)
│   │   ├── models.py                 (~3 KB)
│   │   ├── config.py                 (~1 KB)
│   │   └── cli.py                    (~7 KB)
│   └── utils/            # Shared utilities
│       ├── answer_check.py           (~2 KB)
│       └── safe_eval.py              (~8 KB)
├── tests/
├── Dockerfile / Dockerfile.dev
├── docker-compose.yml / .dev / .prod
├── alembic.ini
├── requirements.txt
└── .env.example
```

---

### Key Components

#### Application Entry Point
| File | Role |
|------|------|
| `src/api/main_v2.py` | FastAPI app factory, CORS, exception handlers, router mounting |
| `src/api/database.py` | Centralised SQLAlchemy engine + `get_db()` dependency |
| `src/api/redis_sessions.py` | Redis session persistence for CAT state |

#### Routers (API Layer)
| Router | Prefix | Responsibility |
|--------|--------|----------------|
| `auth/router.py` | `/api/v1/auth` | Registration, login, invitations, students, groups |
| `api/cat_router.py` | `/api/v1/cat` | CAT diagnostic session management |
| `api/exam_router.py` | `/api/v1/exam` | Exam generation & submission |
| `api/textbook_router.py` | `/api/v1/textbooks` | Textbook CRUD & TOC management |
| `api/textbook_exam.py` | `/api/v1` | Textbook-based exam generation (Module 2b) |
| `api/analytics_router.py` | `/api/v1/analytics` | Student & group analytics endpoints |
| `api/skills_router.py` | `/api/v1/skills` | Knowledge hierarchy browsing |
| `auth/dev_router.py` | `/api/v1/dev` | Dev-only quick login (guarded by `APP_ENV`) |

#### Services / Business Logic
| Module | Responsibility |
|--------|----------------|
| `auth/service.py` | JWT generation/verification, password hashing, org/user CRUD — **monolithic** (~45 KB) |
| `diagnostic/cat_engine.py` | Shadow CAT algorithm — IRT-based adaptive question selection |
| `diagnostic/irt_engine.py` | 3PL IRT model, Fisher Information, theta estimation |
| `diagnostic/integrity_service.py` | Answer integrity monitoring (speed, pattern analysis) |
| `exam_builder/exam_generator.py` | Exam assembly from task bank |
| `exam_builder/variant_orchestrator.py` | Variant generation orchestration |
| `exam_builder/sympy_step_solver.py` | SymPy-backed step-by-step solver |
| `graph/learning_updater.py` | Self-updating Bayesian skill graph |
| `pipeline/orchestrator.py` | OCR + Gemini digitization pipeline |
| `analytics/group_analytics.py` | Group-level learning analytics |
| `analytics/student_progress.py` | Individual student progress tracking |

#### Models (SQLAlchemy)
| File | Tables |
|------|--------|
| `models/__init__.py` | `knowledge_hierarchy`, `skill_prerequisites`, `skill_benchmarks`, `tasks_master`, `task_templates`, `textbooks`, `textbook_structure`, `textbook_toc`, `textbook_skill_map`, `textbook_tasks`, `answer_events`, `skill_correlations`, `error_patterns`, `session_summaries`, `student_skill_states`, `graph_updates_log`, `irt_calibration_history` |
| `auth/models.py` | `organizations`, `users`, `organization_members`, `invitations`, `students`, `groups`, `student_groups`, `diagnostic_invites`, `student_diagnostic_results` |

#### Repository Layer
| Class | File | Pattern |
|-------|------|---------|
| `KnowledgeRepository` | `models/repositories.py` | Session-scoped repository |
| `PrerequisiteRepository` | `models/repositories.py` | Session-scoped repository |
| `TaskRepository` | `models/repositories.py` | Session-scoped repository |
| `AnswerEventRepository` | `models/repositories.py` | Session-scoped repository |
| `SessionRepository` | `models/repositories.py` | Session-scoped repository |
| `StudentSkillRepository` | `models/repositories.py` | Session-scoped repository |
| `CorrelationRepository` | `models/repositories.py` | Session-scoped repository |
| `GraphUpdateRepository` | `models/repositories.py` | Session-scoped repository |

#### Auth & Security
| Component | Implementation |
|-----------|---------------|
| **JWT** | `python-jose` — HS256, httpOnly cookie + Bearer header dual extraction |
| **RBAC** | `RoleChecker` callable dependency — roles: `superadmin`, `owner`, `admin`, `teacher` |
| **Rate Limiting** | Redis-backed INCR/EXPIRE with sliding-window in-memory fallback |
| **Password** | `passlib[bcrypt]` with pinned bcrypt `<4.1` |

#### Infrastructure
| Component | Choice |
|-----------|--------|
| Database | PostgreSQL (psycopg2-binary) |
| ORM | SQLAlchemy 2.0 (synchronous Session) |
| Migrations | Alembic (14+ versions) |
| Cache / Sessions | Redis 5.0+ |
| AI | Google Gemini (`google-generativeai`) |
| Math | SymPy 1.12+ |
| OCR | Mathpix API |
| Containerisation | Docker + docker-compose (dev/prod variants) |

---

### Data Flow

```
HTTP Request
    │
    ▼
FastAPI (main_v2.py)
    │
    ├─ CORS Middleware
    ├─ Exception Handlers (HTTPException + global)
    │
    ▼
Router (e.g. cat_router.py)
    │
    ├─ Dependencies
    │   ├─ get_db() → SQLAlchemy Session
    │   ├─ get_current_user_payload() → JWT decode (cookie or Bearer)
    │   ├─ RoleChecker → RBAC
    │   └─ rate_limit() → Redis INCR check
    │
    ▼
Business Logic
    │
    ├─ Repositories (KnowledgeRepository, TaskRepository, etc.)
    │       └─ SQLAlchemy queries → PostgreSQL
    │
    ├─ CAT Engine (ShadowCATEngine)
    │       ├─ IRT Engine (theta estimation)
    │       └─ Redis Session (persistent CAT state)
    │
    ├─ Exam Builder (ExamGenerator)
    │       └─ SymPy solver / Variant factory
    │
    └─ Auth Service (JWT generation, user/org management)

HTTP Response (Pydantic serialized)
```

---

### Bottlenecks & Code Smells

#### 🔴 Critical Issues

1. **Monolithic `auth/service.py` (~45 KB)**  
   Contains JWT logic, password hashing, user CRUD, org management, student management, group management, diagnostic invite management, and invitation workflows — all in a single file. This violates Single Responsibility Principle and makes testing and microservice extraction very difficult.

2. **Monolithic `models/__init__.py` (~35 KB)**  
   All 17 SQLAlchemy models from completely different domains (knowledge graph, textbooks, analytics, CAT sessions) are defined in a single `__init__.py`. This creates a "god module" with circular import risks and makes domain separation impossible without a full refactor.

3. **Giant Routers acting as services**  
   - `exam_router.py` is ~56 KB — contains business logic, DB queries, and Pydantic schemas all inline.  
   - `cat_router.py` is ~30 KB — similar problem.  
   - `auth/router.py` is ~24 KB — handlers directly contain service logic instead of delegating.

4. **No separation between API schemas and domain models**  
   Pydantic schemas defined in `auth/schemas.py` are mixed with response models. Some routers define Pydantic models inline (e.g., `HealthResponse` in `main_v2.py`). There are no dedicated `schemas/` directories per domain.

#### 🟡 Moderate Issues

5. **No async DB support** — `get_db()` uses synchronous `Session`. With an I/O-heavy workload (CAT sessions, analytics), switching to `AsyncSession` would significantly improve throughput.

6. **`models/repositories.py` missing auth-domain repositories** — Auth-related DB access (`User`, `Organization`, `Student`, `Group`) is done directly inside `auth/service.py` using raw session queries, bypassing the clean repository pattern established in `models/repositories.py`.

7. **Settings defined as a plain class without Pydantic BaseSettings** — `Settings` in `main_v2.py` is a plain Python class. It should use `pydantic-settings` (`BaseSettings`) for environment variable validation, type safety, and `.env` file integration.

8. **`src/__init__.py` creates eager cross-layer imports** — importing `DigitizationPipeline`, `ShadowCATEngine`, and `ExamGenerator` at package-root level means every module import triggers loading heavy dependencies (SymPy, TensorFlow-adjacent code).

9. **No dependency injection for services** — Services like `ShadowCATEngine`, `ExamGenerator`, and `DigitizationPipeline` are instantiated ad-hoc inside routers. There's no container or factory pattern, making unit testing with mocked dependencies very difficult.

10. **Rate limiter uses lazy global Redis connection** — `_get_redis()` is a module-level global, not injected via FastAPI `Depends`, making it untestable in isolation.

11. **`to_dict()` methods on ORM models** — Models like `User`, `Organization`, `Student` implement `to_dict()` serialization. This creates a second serialization path that competes with Pydantic schemas, leading to divergence over time.

12. **No `ExamSession` / `CATSession` persistence model** — CAT session state is stored entirely in Redis with no backup. If Redis restarts, active diagnostic sessions are lost with no recovery path.

13. **Missing API versioning structure** — All routers are mounted under `/api/v1` but live in a flat `api/` folder. There's no `api/v1/` subdirectory, making future v2 routes awkward to introduce.

14. **Tests directory appears empty** — Given the complexity of the CAT engine and IRT model, the absence of unit tests is a high-risk technical debt item.

#### 🟢 What's Already Good

- ✅ `database.py` is properly centralised (single source of truth for DB config).
- ✅ RBAC via `RoleChecker` is well-structured and reusable.
- ✅ Dual JWT extraction (cookie + Bearer) is cleanly implemented in `dependencies.py`.
- ✅ Repository pattern established in `models/repositories.py` is clean and correct.
- ✅ Alembic migrations are in place.
- ✅ Rate limiter has Redis + in-memory fallback.
- ✅ Docker + docker-compose setup exists for dev and prod.
- ✅ Pydantic v2 is already adopted (`field_validator`, `BaseModel`, etc.).

---

### Context Snapshot

- **Phase:** 1 (Audit) — COMPLETE
- **Codebase language:** Python 3.11+, FastAPI 0.109+, SQLAlchemy 2.0, Pydantic v2, Alembic
- **Domain:** EdTech — math diagnostic platform with CAT (Computerized Adaptive Testing), IRT (Item Response Theory), textbook digitization, and exam generation
- **Team model:** B2B SaaS — Organizations → Members (owner/admin/teacher) → Students
- **Biggest risks:** Monolithic service file, missing test coverage, no async DB, Redis as only CAT state store
- **Readiness for Phase 3:** High — repository pattern exists, Pydantic v2 adopted, clean DB layer

---

## Phase 2 — Architectural Blueprint

> **Status:** ✅ Complete — Full specification ready for Phase 3 execution

---

### 2.1 Architectural Style: Layered Service-Repository

The target architecture follows a **strict layered pattern** with one-directional dependency flow:

```
┌─────────────────────────────────────────────────────────────────┐
│                        HTTP / Transport                          │
│   src/api/v1/{domain}/router.py  ←  Pydantic schemas only       │
│   (Thin handlers: validate → call service → return schema)       │
└──────────────────────────┬──────────────────────────────────────┘
                           │  calls
┌──────────────────────────▼──────────────────────────────────────┐
│                      Service Layer                               │
│   src/services/{domain}_service.py                               │
│   (Business rules, orchestration, no HTTP knowledge)             │
└──────────────────────────┬──────────────────────────────────────┘
                           │  calls
┌──────────────────────────▼──────────────────────────────────────┐
│                    Repository Layer                              │
│   src/repositories/{domain}_repo.py                              │
│   (DB queries only, returns ORM models or domain dataclasses)    │
└──────────────────────────┬──────────────────────────────────────┘
                           │  reads/writes
┌──────────────────────────▼──────────────────────────────────────┐
│                   Persistence / Infrastructure                   │
│   PostgreSQL (SQLAlchemy ORM)  +  Redis (CAT session state)      │
└─────────────────────────────────────────────────────────────────┘

Cross-cutting (imported by any layer):
  src/core/config.py       ← App settings
  src/core/database.py     ← DB session factory
  src/core/security.py     ← JWT encode/decode
  src/core/exceptions.py   ← Typed exception hierarchy
  src/core/dependencies.py ← FastAPI Depends helpers
```

**Dependency Rule:** Each layer may only import from the layer directly below it and from `src/core/`. Routers never touch repositories directly. Services never import from `src/api/`.

---

### 2.2 Target Directory Structure

```
algo-back/
├── src/
│   │
│   ├── core/                             # Cross-cutting infrastructure
│   │   ├── __init__.py
│   │   ├── config.py                     # [MODIFY] Pydantic BaseSettings
│   │   ├── database.py                   # [MOVE]   from src/api/database.py
│   │   ├── security.py                   # [NEW]    JWT encode/decode/verify
│   │   ├── exceptions.py                 # [NEW]    Custom exception types
│   │   └── dependencies.py               # [NEW]    Shared FastAPI Depends
│   │
│   ├── models/                           # ORM models — split by bounded context
│   │   ├── base.py                       # [NEW]    Base = declarative_base()
│   │   ├── auth.py                       # [MOVE]   from src/auth/models.py
│   │   ├── knowledge.py                  # [SPLIT]  from models/__init__.py
│   │   ├── tasks.py                      # [SPLIT]  from models/__init__.py
│   │   ├── textbooks.py                  # [SPLIT]  from models/__init__.py
│   │   ├── analytics.py                  # [SPLIT]  from models/__init__.py
│   │   ├── graph.py                      # [SPLIT]  from models/__init__.py
│   │   └── dataclasses.py                # [KEEP]   domain dataclasses + enums
│   │
│   ├── repositories/                     # Data access layer (one file per domain)
│   │   ├── __init__.py                   # Re-exports all repos
│   │   ├── base.py                       # [NEW]    Generic BaseRepository[ModelT]
│   │   ├── user_repo.py                  # [NEW]    User, OrganizationMember
│   │   ├── org_repo.py                   # [NEW]    Organization, Invitation
│   │   ├── student_repo.py               # [NEW]    Student, DiagnosticInvite, StudentDiagnosticResult
│   │   ├── group_repo.py                 # [NEW]    Group, StudentGroup
│   │   ├── knowledge_repo.py             # [MOVE]   KnowledgeRepository
│   │   ├── task_repo.py                  # [MOVE]   TaskRepository
│   │   ├── session_repo.py               # [MOVE]   SessionRepository
│   │   ├── answer_event_repo.py          # [MOVE]   AnswerEventRepository
│   │   ├── student_skill_repo.py         # [MOVE]   StudentSkillRepository
│   │   ├── correlation_repo.py           # [MOVE]   CorrelationRepository
│   │   └── graph_update_repo.py          # [MOVE]   GraphUpdateRepository
│   │
│   ├── services/                         # Business logic layer
│   │   ├── __init__.py
│   │   ├── auth_service.py               # [REFACTOR] JWT auth + token refresh
│   │   ├── org_service.py                # [EXTRACT]  Org registration, approval, update
│   │   ├── user_service.py               # [EXTRACT]  Profile, password, invitation accept
│   │   ├── student_service.py            # [EXTRACT]  Student CRUD, status workflow
│   │   ├── group_service.py              # [EXTRACT]  Group CRUD, member management
│   │   ├── cat_service.py                # [EXTRACT]  CAT session lifecycle
│   │   ├── exam_service.py               # [EXTRACT]  Exam creation, submission, scoring
│   │   ├── textbook_service.py           # [EXTRACT]  Textbook + TOC management
│   │   └── analytics_service.py          # [EXTRACT]  Student + group analytics facade
│   │
│   ├── api/
│   │   ├── v1/                           # Versioned API — thin routers only
│   │   │   ├── __init__.py
│   │   │   ├── router.py                 # Master v1 router (includes all below)
│   │   │   ├── auth/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── router.py             # [REFACTOR] Thin handlers → auth_service
│   │   │   │   └── schemas.py            # [MOVE]     from src/auth/schemas.py
│   │   │   ├── cat/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── router.py             # [REFACTOR] Thin handlers → cat_service
│   │   │   │   └── schemas.py            # [EXTRACT]  from cat_router.py
│   │   │   ├── exam/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── router.py             # [REFACTOR] Thin handlers → exam_service
│   │   │   │   └── schemas.py            # [EXTRACT]  from exam_router.py
│   │   │   ├── textbook/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── router.py
│   │   │   │   └── schemas.py
│   │   │   ├── analytics/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── router.py
│   │   │   │   └── schemas.py
│   │   │   └── skills/
│   │   │       ├── __init__.py
│   │   │       ├── router.py
│   │   │       └── schemas.py
│   │   └── middleware/
│   │       ├── rate_limit.py             # [MOVE]   from src/api/rate_limit.py
│   │       └── logging.py               # [NEW]    Structured request logging
│   │
│   ├── domain/                           # Scientific engines — internal structure unchanged
│   │   ├── diagnostic/
│   │   │   ├── cat_engine.py             # [MOVE]   from src/diagnostic/
│   │   │   ├── irt_engine.py
│   │   │   └── integrity_service.py
│   │   ├── exam_builder/                 # [MOVE]   from src/exam_builder/
│   │   ├── graph/                        # [MOVE]   from src/graph/
│   │   ├── analytics/                    # [MOVE]   from src/analytics/
│   │   └── pipeline/                     # [MOVE]   from src/pipeline/
│   │
│   ├── utils/                            # Shared pure utilities
│   │   ├── answer_check.py               # [KEEP]   unchanged
│   │   └── safe_eval.py                  # [KEEP]   unchanged
│   │
│   └── main.py                           # [REPLACE] main_v2.py → clean app factory
│
├── tests/
│   ├── conftest.py                       # [NEW] Shared fixtures (TestClient, DB session)
│   ├── unit/
│   │   ├── test_irt_engine.py
│   │   ├── test_cat_engine.py
│   │   ├── test_auth_service.py
│   │   ├── test_org_service.py
│   │   └── test_repositories.py
│   └── integration/
│       ├── test_auth_routes.py
│       ├── test_cat_routes.py
│       └── test_exam_routes.py
│
├── alembic/                              # [KEEP]   Unchanged
├── Dockerfile / Dockerfile.dev           # [KEEP]   Unchanged (CMD still uvicorn src.main:app)
├── docker-compose.*.yml                  # [KEEP]   Unchanged
├── requirements.txt                      # [UPDATE] Add pydantic-settings
└── .env.example                          # [UPDATE] Add new env var documentation
```

---

### 2.3 Module Dependency Rules

```
ALLOWED imports (→ means "may import from"):

  api/v1/**        → services/, core/, models/dataclasses.py
  services/**      → repositories/, domain/**, core/, models/
  repositories/**  → models/, core/database.py
  domain/**        → models/dataclasses.py, utils/, (repositories/ via DI only)
  core/**          → (nothing internal — only stdlib and third-party)
  models/**        → models/base.py only (no cross-domain model imports)
  utils/**         → (nothing internal)

FORBIDDEN:
  ✗ services/** importing from api/**
  ✗ repositories/** importing from services/**
  ✗ Any layer importing from src/__init__.py (remove the eager re-exports)
  ✗ models/** importing from repositories/** or services/**
```

---

### 2.4 Concrete Module Specifications

#### `src/core/config.py` — Application Settings

```python
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, PostgresDsn, RedisDsn
from typing import List
from functools import lru_cache

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Application
    app_name: str = "ALGO V1 API"
    app_version: str = "1.0.0"
    app_env: str = Field(default="production", pattern="^(development|production|testing)$")
    debug: bool = False

    # Database
    database_url: PostgresDsn = Field(
        default="postgresql://postgres:password@localhost:5432/algo_v1"
    )

    # Redis
    redis_url: RedisDsn = Field(default="redis://localhost:6379")

    # Auth — JWT
    secret_key: str = Field(..., min_length=32)   # required, no default
    access_token_expire_minutes: int = 60
    refresh_token_expire_days: int = 30
    algorithm: str = "HS256"

    # External APIs
    mathpix_app_id: str = ""
    mathpix_app_key: str = ""
    gemini_api_key: str = ""

    # CORS
    cors_origins: List[str] = ["http://localhost:3000"]

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"

    @property
    def is_testing(self) -> bool:
        return self.app_env == "testing"


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton — use as FastAPI dependency."""
    return Settings()
```

**Why:** `@lru_cache` ensures `.env` is parsed exactly once at startup. `Field(..., min_length=32)` for `secret_key` makes missing secrets a startup crash, not a runtime surprise.

---

#### `src/core/database.py` — Database Session Factory

```python
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from src.core.config import get_settings

def _make_engine():
    s = get_settings()
    return create_engine(str(s.database_url), echo=s.debug, pool_pre_ping=True)

engine = _make_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """FastAPI dependency — yields a scoped DB session."""
    db: Session = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def db_session():
    """Synchronous context manager for use outside FastAPI (scripts, CLI)."""
    db: Session = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
```

**Why:** `pool_pre_ping=True` prevents stale connection errors on long-running servers. The `db_session()` context manager enables `pipeline/` and `graph/` modules to access the DB without importing FastAPI.

---

#### `src/core/security.py` — JWT Operations

```python
from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from src.core.config import get_settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(payload: dict) -> str:
    s = get_settings()
    data = payload.copy()
    data["exp"] = datetime.now(timezone.utc) + timedelta(minutes=s.access_token_expire_minutes)
    data["type"] = "access"
    return jwt.encode(data, s.secret_key, algorithm=s.algorithm)


def create_refresh_token(payload: dict) -> str:
    s = get_settings()
    data = payload.copy()
    data["exp"] = datetime.now(timezone.utc) + timedelta(days=s.refresh_token_expire_days)
    data["type"] = "refresh"
    return jwt.encode(data, s.secret_key, algorithm=s.algorithm)


def decode_token(token: str) -> Optional[dict]:
    """Returns payload dict on success, None on any error."""
    try:
        s = get_settings()
        return jwt.decode(token, s.secret_key, algorithms=[s.algorithm])
    except JWTError:
        return None
```

**Why:** Extracted from `auth/service.py`. Pure functions with no DB dependency — trivially unit-testable without a DB fixture. `auth_service.py` calls these; `core/dependencies.py` uses `decode_token()` directly in the JWT middleware.

---

#### `src/core/exceptions.py` — Typed Exception Hierarchy

```python
from fastapi import HTTPException, status


class AlgoException(Exception):
    """Base application exception."""
    def __init__(self, message: str, code: str = "UNKNOWN_ERROR"):
        self.message = message
        self.code = code
        super().__init__(message)


class NotFoundError(AlgoException):
    def __init__(self, entity: str, entity_id: str):
        super().__init__(f"{entity} '{entity_id}' not found", "NOT_FOUND")


class ConflictError(AlgoException):
    def __init__(self, message: str):
        super().__init__(message, "CONFLICT")


class ForbiddenError(AlgoException):
    def __init__(self, message: str = "Access denied"):
        super().__init__(message, "FORBIDDEN")


class ValidationError(AlgoException):
    def __init__(self, message: str):
        super().__init__(message, "VALIDATION_ERROR")


# Mapping from domain exceptions → HTTP status codes (used in main.py handler)
EXCEPTION_STATUS_MAP = {
    NotFoundError: status.HTTP_404_NOT_FOUND,
    ConflictError: status.HTTP_409_CONFLICT,
    ForbiddenError: status.HTTP_403_FORBIDDEN,
    ValidationError: status.HTTP_422_UNPROCESSABLE_ENTITY,
}
```

**Why:** Services raise domain exceptions (`NotFoundError`, `ConflictError`). The global handler in `main.py` maps them to HTTP status codes. This keeps HTTP concerns out of the service layer.

---

#### `src/core/dependencies.py` — Shared FastAPI Dependencies

```python
from typing import Annotated, Optional
from fastapi import Cookie, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.core.security import decode_token

security = HTTPBearer(auto_error=False)

DbDep = Annotated[Session, Depends(get_db)]


def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    """Resolves JWT from cookie (primary) or Bearer header (fallback)."""
    token = request.cookies.get("algo_auth_token")
    if not token and credentials:
        token = credentials.credentials
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    return payload


def get_optional_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[dict]:
    """Non-raising version of get_current_user — returns None for unauthenticated."""
    try:
        return get_current_user(request, credentials)
    except HTTPException:
        return None


CurrentUser = Annotated[dict, Depends(get_current_user)]
OptionalUser = Annotated[Optional[dict], Depends(get_optional_user)]
```

**Why:** `Annotated` type aliases (`DbDep`, `CurrentUser`) let route handlers declare dependencies cleanly with zero boilerplate:
```python
@router.get("/me")
def get_me(db: DbDep, user: CurrentUser):
    ...
```

---

#### `src/repositories/base.py` — Generic Repository Base

```python
from typing import Generic, TypeVar, Type, Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import select

ModelT = TypeVar("ModelT")


class BaseRepository(Generic[ModelT]):
    """Generic session-scoped repository.

    Usage:
        class UserRepo(BaseRepository[User]):
            model = User
    """
    model: Type[ModelT]

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_id(self, pk) -> Optional[ModelT]:
        return self.session.get(self.model, pk)

    def list_all(self, limit: int = 100, offset: int = 0) -> List[ModelT]:
        stmt = select(self.model).limit(limit).offset(offset)
        return list(self.session.scalars(stmt))

    def add(self, obj: ModelT) -> ModelT:
        self.session.add(obj)
        self.session.flush()
        return obj

    def delete(self, obj: ModelT) -> None:
        self.session.delete(obj)
        self.session.flush()

    def exists(self, pk) -> bool:
        return self.get_by_id(pk) is not None
```

**Why:** Eliminates trivial `get_by_id` / `add` / `delete` boilerplate across all repositories. Each concrete repo inherits and adds domain-specific query methods.

---

#### `src/services/` — Service Contract Pattern

All services follow the same injection pattern — repositories and domain engines are passed in via `__init__`, never constructed internally:

```python
# src/services/auth_service.py  (abbreviated contract)

from src.repositories.user_repo import UserRepo
from src.repositories.org_repo import OrgRepo
from src.core.security import hash_password, verify_password, create_access_token, create_refresh_token
from src.core.exceptions import NotFoundError, ConflictError, ForbiddenError
from src.auth.schemas import LoginRequest, AuthResponse  # → will move to api/v1/auth/schemas.py


class AuthService:
    def __init__(self, user_repo: UserRepo, org_repo: OrgRepo) -> None:
        self.user_repo = user_repo
        self.org_repo = org_repo

    def login(self, payload: LoginRequest) -> AuthResponse:
        """Verify credentials and issue token pair."""
        user = self.user_repo.get_by_email(payload.email)
        if not user or not verify_password(payload.password, user.password_hash):
            raise ForbiddenError("Invalid email or password")
        if not user.is_active:
            raise ForbiddenError("Account is deactivated")
        tokens = self._issue_tokens(user)
        return AuthResponse(user=user.to_response(), tokens=tokens)

    def _issue_tokens(self, user) -> dict:
        base = {"sub": str(user.id), "role": user.role}
        return {
            "access_token": create_access_token(base),
            "refresh_token": create_refresh_token(base),
        }
```

**FastAPI DI for services** — services are created per-request via `Depends`:

```python
# src/api/v1/auth/router.py

from fastapi import APIRouter, Depends
from src.core.dependencies import DbDep
from src.repositories.user_repo import UserRepo
from src.repositories.org_repo import OrgRepo
from src.services.auth_service import AuthService
from .schemas import LoginRequest, AuthResponse

router = APIRouter(prefix="/auth", tags=["Auth"])


def get_auth_service(db: DbDep) -> AuthService:
    return AuthService(user_repo=UserRepo(db), org_repo=OrgRepo(db))


@router.post("/login", response_model=AuthResponse)
def login(
    payload: LoginRequest,
    service: AuthService = Depends(get_auth_service),
):
    return service.login(payload)
```

**Why this DI approach:**
- Services receive repo instances → easy to mock in unit tests (`AuthService(user_repo=MockUserRepo(), ...)`)
- `get_auth_service` is the composition root — one place to swap implementations
- No global state, no singletons, no circular imports

---

#### `src/api/v1/router.py` — Master v1 Router

```python
from fastapi import APIRouter
from src.api.v1.auth.router import router as auth_router
from src.api.v1.cat.router import router as cat_router
from src.api.v1.exam.router import router as exam_router
from src.api.v1.textbook.router import router as textbook_router
from src.api.v1.analytics.router import router as analytics_router
from src.api.v1.skills.router import router as skills_router

v1_router = APIRouter(prefix="/api/v1")

v1_router.include_router(auth_router)
v1_router.include_router(cat_router)
v1_router.include_router(exam_router)
v1_router.include_router(textbook_router)
v1_router.include_router(analytics_router)
v1_router.include_router(skills_router)
```

---

#### `src/main.py` — Clean App Factory

```python
from contextlib import asynccontextmanager
import logging
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.core.config import get_settings
from src.core.exceptions import AlgoException, EXCEPTION_STATUS_MAP
from src.api.v1.router import v1_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting ALGO V1...")
    yield
    logger.info("Shutting down ALGO V1...")


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title=s.app_name,
        version=s.app_version,
        docs_url="/docs" if s.is_development else None,
        redoc_url="/redoc" if s.is_development else None,
        lifespan=lifespan,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Domain exception handler
    @app.exception_handler(AlgoException)
    async def domain_exception_handler(request: Request, exc: AlgoException):
        status_code = EXCEPTION_STATUS_MAP.get(type(exc), 500)
        return JSONResponse(status_code=status_code, content={"detail": exc.message, "code": exc.code})

    # Routers
    app.include_router(v1_router)

    return app


app = create_app()
```

**Why:** Docs exposed only in development. `create_app()` factory pattern makes the app testable (`client = TestClient(create_app())`). Exception handler maps domain exceptions to HTTP codes without polluting the service layer.

---

### 2.5 Pydantic Schema Conventions

All API schemas follow this structure per domain:

```python
# src/api/v1/{domain}/schemas.py

from pydantic import BaseModel, Field, model_validator
from typing import Optional
from datetime import datetime

# ── Request schemas (input) ──

class CreateFooRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    ...

class UpdateFooRequest(BaseModel):
    name: Optional[str] = Field(None, max_length=200)
    ...

# ── Response schemas (output) ──

class FooResponse(BaseModel):
    model_config = {"from_attributes": True}   # Pydantic v2 ORM mode

    id: str
    name: str
    created_at: Optional[datetime] = None
```

**Key rules:**
- `model_config = {"from_attributes": True}` on all response schemas → replaces `to_dict()` on ORM models
- `Request` suffix for inputs, `Response` suffix for outputs
- No `Optional` on required fields — use `Field(...)` explicitly
- All UUID fields serialised as `str` in responses (consistent with existing API contract)

---

### 2.6 Naming Conventions

| Element | Convention | Example |
|---------|-----------|---------|
| Router files | `router.py` per domain folder | `api/v1/auth/router.py` |
| Service classes | `{Domain}Service` | `AuthService`, `CATService` |
| Repository classes | `{Domain}Repo` | `UserRepo`, `TaskRepo` |
| Schema classes | `{Action}{Entity}Request/Response` | `CreateStudentRequest`, `StudentResponse` |
| Dependency factories | `get_{domain}_service` | `get_auth_service` |
| Annotated types | `{Name}Dep` | `DbDep`, `CurrentUser` |
| Model files | domain noun (singular) | `models/auth.py`, `models/tasks.py` |
| Exception classes | `{Description}Error` | `NotFoundError`, `ConflictError` |
| Settings fields | `snake_case` | `database_url`, `secret_key` |
| Env var names | `UPPER_SNAKE_CASE` | `DATABASE_URL`, `SECRET_KEY` |

---

### 2.7 Microservice Extraction Boundaries

The architecture is designed so the following domains can be extracted into independent services with minimal coupling:

| Potential Microservice | Current Location | Extraction Trigger |
|------------------------|------------------|--------------------|
| **Auth & IAM** | `services/auth_service.py`, `services/org_service.py`, `services/user_service.py`, `services/student_service.py`, `services/group_service.py` | When auth latency impacts CAT performance; when multi-tenancy needs isolation |
| **CAT Diagnostic Engine** | `services/cat_service.py` + `domain/diagnostic/` | When concurrent diagnostic sessions exceed single-node capacity; GPU acceleration for IRT |
| **Exam Builder** | `services/exam_service.py` + `domain/exam_builder/` | When SymPy compute time degrades API response times; can become a worker queue consumer |
| **Digitization Pipeline** | `domain/pipeline/` | Already stateless — extract as a background worker with a message queue (e.g. Celery/ARQ) |
| **Learning Graph Updater** | `domain/graph/` | Naturally async — should become a scheduled batch job long before microservice extraction |
| **Analytics** | `services/analytics_service.py` + `domain/analytics/` | When analytics queries compete with transactional DB load; needs read replica routing |

**Extraction readiness checklist for each candidate:**
- [ ] Service has no direct HTTP imports (✅ achieved by thin-router pattern)
- [ ] Service communicates via repository interfaces only (✅ achieved by repo pattern)
- [ ] Service has its own bounded DB models (✅ achieved by model split)
- [ ] Service has integration tests (🔄 Phase 3 task)
- [ ] Service serialisation is Pydantic-only (🔄 Phase 3 — deprecate `to_dict()`)

---

### 2.8 Rationale Summary

| Decision | Addresses | Why |
|----------|-----------|-----|
| **`core/` layer** | Issues #7, #10 | Single import target for config, DB, security — no circular deps possible |
| **`api/v1/` versioning** | Issue #13 | Future `/api/v2` routes co-exist cleanly without URL changes to existing clients |
| **Thin routers** | Issues #3, #4 | Routes become 10-30 lines; business logic moves to services; zero direct DB in router |
| **`services/` layer** | Issues #1, #3, #9 | Replaces monolithic `auth/service.py`; all services accept repo deps via `__init__` |
| **`repositories/` split** | Issues #2, #6 | One file per domain; auth-domain repos `UserRepo`/`OrgRepo`/`StudentRepo`/`GroupRepo` fill the gap |
| **`models/` split** | Issue #2 | Eliminates 35 KB god module; bounded context imports prevent cross-domain coupling |
| **Pydantic `BaseSettings`** | Issue #7 | Required field validation at startup; `.env` parsed once via `@lru_cache` |
| **`AlgoException` hierarchy** | Issues #3 | Services raise typed domain errors; HTTP mapping is in one central handler |
| **`from_attributes=True` schemas** | Issue #11 | Unified serialization — deprecates `to_dict()` on ORM models |
| **`Annotated` dependency aliases** | Issue #9 | Cleaner route signatures; easier to swap implementations in tests |

---

---

## Phase 3 — Incremental Refactoring

### Execution Order

Each step leaves the application in a **runnable state**. Steps are ordered from lowest-risk (structural moves, zero logic change) to highest-risk (service extraction, thin routers).

| # | Step | Type | Risk | Description |
|---|------|------|------|-------------|
| 1 | Create `src/models/base.py` | Structural | 🟢 Low | Extract `Base = declarative_base()` — zero logic change |
| 2 | Split `models/__init__.py` | Structural | 🟢 Low | One file per domain: `knowledge.py`, `tasks.py`, `textbooks.py`, `analytics.py`, `graph.py` |
| 3 | Move `auth/models.py` → `models/auth.py` | Structural | 🟢 Low | Update all imports, no logic change |
| 4 | Add `pydantic-settings` to requirements | Infrastructure | 🟢 Low | `pip install pydantic-settings` |
| 5 | Create `src/core/config.py` | Infrastructure | 🟡 Medium | Pydantic `BaseSettings` — replaces `Settings` class in `main_v2.py` |
| 6 | Move `src/api/database.py` → `src/core/database.py` | Infrastructure | 🟡 Medium | Update all `from src.api.database import ...` imports |
| 7 | Create `src/core/security.py` | Extract | 🟡 Medium | Move JWT/password functions from `auth/service.py` |
| 8 | Create `src/core/exceptions.py` | New | 🟢 Low | New file, no existing code touched |
| 9 | Create `src/core/dependencies.py` | Refactor | 🟡 Medium | Replace `auth/dependencies.py` with core version |
| 10 | Create `src/repositories/base.py` + migrate repos | Extract | 🟡 Medium | Move existing repos from `models/repositories.py` to `src/repositories/` |
| 11 | Create new auth-domain repos (`UserRepo`, `OrgRepo`, `StudentRepo`, `GroupRepo`) | New | 🟡 Medium | Extract raw DB queries from `auth/service.py` |
| 12 | Create `src/services/auth_service.py` | Refactor | 🔴 High | Thin down `auth/service.py`; delegates to security + repos |
| 13 | Create `src/services/org_service.py`, `student_service.py`, `group_service.py` | Extract | 🔴 High | Pull org/student/group logic from the monolithic service |
| 14 | Create `src/services/cat_service.py` | Extract | 🔴 High | Pull CAT session logic out of `cat_router.py` |
| 15 | Create `src/services/exam_service.py` | Extract | 🔴 High | Pull exam logic out of `exam_router.py` |
| 16 | Create `src/api/v1/` domain structure + thin routers | Refactor | 🔴 High | Thin handlers — each delegates to one service method |
| 17 | Create `src/main.py` (app factory replace) | Refactor | 🟡 Medium | Clean `create_app()` factory, remove `main_v2.py` |
| 18 | Move `domain/` engines (`diagnostic/`, `exam_builder/`, `graph/`, `analytics/`, `pipeline/`) | Structural | 🟡 Medium | Import path updates only |
| 19 | Write unit tests | Testing | 🟢 Low | `tests/unit/` — IRT engine, auth service, repositories |
| 20 | Write integration tests | Testing | 🟡 Medium | `tests/integration/` — auth routes, CAT session flow |
| 21 | Async DB migration | Optional | 🔴 High | `AsyncSession` + `asyncpg` — separate PR, after all above |

---

### Change Log

| # | Date | Change | Status |
|---|------|--------|--------|
| 0 | 2026-03-22 | Phase 1 audit complete — SYSTEM_STATE.md initialized | ✅ Done |
| 1 | 2026-03-22 | Phase 2 architecture designed — full spec, code skeletons, microservice boundaries | ✅ Done |
| 2 | 2026-03-22 | Phase 3 structual foundation completed — models split, core layer, repos, and services created | ✅ Done |

---

### Next Steps

> **Immediate:** Begin **Step 1** → Create `src/models/base.py`

```bash
# Step 1 — Extract declarative_base (zero-risk, enables all subsequent model splitting)
# 1a. Create src/models/base.py
# 1b. In src/models/__init__.py: replace `Base = declarative_base()` with `from .base import Base`
# 1c. Verify:
python -c "from src.models import Base; print('✅ Base OK')"
python -c "from src.models.base import Base; print('✅ base.py OK')"

# Step 4 — Install pydantic-settings (prerequisite for Step 5)
pip install pydantic-settings
```

**Session handoff:** All Phase 1 context and Phase 2 specs are in this document. In the next session, begin at Step 1 and execute sequentially. The application must remain runnable after each step. Run `python -m pytest` (if tests exist) or start the server to verify after each step.

---

*State Saved. Phase 1 ✅ | Phase 2 ✅ | Phase 3 ready. Next: Step 1 — `src/models/base.py`.*

