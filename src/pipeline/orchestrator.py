"""
ALGO V1 — Pipeline Orchestrator
src/pipeline/orchestrator.py

Главный оркестратор конвейера оцифровки.
Этапы: OCR → Legend → Split → Extract → SymPy → Enrich → Distractors → Map → DB

Поддерживает:
  • Полную обработку PDF (process_pdf)
  • Импорт из JSON (process_json)
  • Инкрементальную обработку (resume по progress-файлу)
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, List

from src.pipeline.classification import SkeletonTextbookMapper
from src.pipeline.config import CFG
from src.pipeline.db_writer import DBWriter
from src.pipeline.enrichment import AIEnricher
from src.pipeline.extraction import LegendExtractor, ParagraphSplitter, TaskExtractor
from src.pipeline.models import ExtractedTask
from src.pipeline.ocr import MathpixOCR
from src.pipeline.validation import SymPySolver

log = logging.getLogger("pipeline")


class DigitizationPipeline:
    """Главный оркестратор конвейера оцифровки учебников."""

    def __init__(self, textbook_id: str, class_level: int = 5):
        self.textbook_id = textbook_id
        self.class_level = class_level

        api_key = CFG.get("gemini", {}).get("api_key", os.getenv("GEMINI_API_KEY", ""))
        db_url = CFG.get("database_url", os.getenv("DATABASE_URL", ""))

        self.ocr = MathpixOCR()
        self.legend_extractor = LegendExtractor(api_key)
        self.extractor = TaskExtractor(api_key)
        self.solver = SymPySolver()
        self.enricher = AIEnricher(api_key)
        self._step_solver = None  # lazy init
        self._distractor_engine = None  # lazy init
        self.mapper = SkeletonTextbookMapper(api_key)
        self.writer = DBWriter(db_url)

        if db_url:
            self.mapper.load_skills_from_db(db_url, class_level)

        # Прогресс (resume)
        self.progress_file = Path(f".pipeline_progress_{textbook_id[:8]}.json")
        self.progress: dict = self._load_progress()

    # ------------------------------------------------------------------
    # Public: PDF
    # ------------------------------------------------------------------

    def process_pdf(self, pdf_path: str) -> None:
        """Полный конвейер: PDF → БД."""
        log.info("=== Начало оцифровки: %s ===", pdf_path)
        start = time.time()

        # 1. OCR
        log.info("Шаг 1/8: OCR (Mathpix)")
        text = self.ocr.process_pdf(pdf_path)

        # 1b. Легенда маркеров
        log.info("Шаг 1b: Извлечение легенды маркеров")
        legend = self.legend_extractor.extract_legend(text)
        self.extractor.set_legend(legend)

        # 2. TOC из БД
        toc = self._load_toc_from_db()

        # 3. Разбиение на параграфы
        log.info("Шаг 2/8: Разбиение на параграфы")
        splitter = ParagraphSplitter(toc)
        paragraphs = splitter.split(text)
        log.info("   Найдено %d параграфов", len(paragraphs))

        # 4-8. Обработка параграфов
        total_tasks = 0
        for para in paragraphs:
            toc_id = para.get("toc_id")
            para_key = f"{para['number']}_{toc_id}"

            if para_key in self.progress.get("done_paragraphs", []):
                log.info("   §%s — уже обработан (resume)", para["number"])
                continue

            tasks = self._process_paragraph(para)
            total_tasks += len(tasks)

            self.progress.setdefault("done_paragraphs", []).append(para_key)
            self._save_progress()

        elapsed = time.time() - start
        log.info("=== Оцифровка завершена: %d задач за %.0fs ===", total_tasks, elapsed)

    # ------------------------------------------------------------------
    # Public: JSON import
    # ------------------------------------------------------------------

    def process_json(self, json_path: str) -> None:
        """Импорт задач из JSON-файла (ручная оцифровка)."""
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        items = data if isinstance(data, list) else data.get("tasks", [])
        tasks: List[ExtractedTask] = []
        for item in items:
            tasks.append(ExtractedTask(
                temp_id=item.get("temp_id", ""),
                exercise_number=str(item.get("exercise_number", "")),
                paragraph_number=str(item.get("paragraph_number", "")),
                paragraph_title=item.get("paragraph_title", ""),
                question_text=item.get("question_text", ""),
                question_latex=item.get("question_latex", ""),
                answer_raw=str(item.get("answer_raw", item.get("correct_answer", ""))),
                answer_type=item.get("answer_type", "exact_number"),
                difficulty=item.get("difficulty", "B"),
                cognitive_load=item.get("cognitive_load", "apply"),
                is_star=bool(item.get("is_star", False)),
                task_marker=item.get("task_marker", ""),
                task_category=item.get("task_category", "standard"),
                skill_id=item.get("skill_id", ""),
                toc_id=item.get("toc_id"),
                tags=item.get("tags", {}),
            ))

        log.info("Загружено %d задач из JSON", len(tasks))

        for task in tasks:
            task = self.solver.verify(task)
            task = self.enricher.enrich(task)
            task = self._generate_distractors(task)
            if not task.skill_id:
                task = self.mapper.map_task(task)

        self.writer.write_batch(tasks, self.textbook_id, self.class_level)

    # ------------------------------------------------------------------
    # Distractor generation (all answer types)
    # ------------------------------------------------------------------

    def _generate_distractors(self, task: ExtractedTask) -> ExtractedTask:
        """
        Генерирует дистракторы для ЛЮБОГО типа ответа.

        Маршрутизация:
          • exact_number / decimal → SymPy templatize → StepSolver → StepDistractorEngine
          • fraction / expression / multiple_values → StepDistractorEngine.generate_for_answer()
          • open_text / text → AIDistractorGenerator (Gemini Pro)
        """
        answer = (task.answer_raw or "").strip()
        if not answer:
            return task

        answer_type = (task.answer_type or "exact_number").lower().strip()

        try:
            # Lazy init
            if self._step_solver is None:
                from src.exam_builder.sympy_step_solver import StepSolver, StepDistractorEngine
                self._step_solver = StepSolver()
                self._distractor_engine = StepDistractorEngine()

            # ── Числовые типы: templatize → StepSolver → float-based distractors ──
            if answer_type in ("exact_number", "decimal"):
                distractors_list = self._generate_numeric_distractors(task, answer)
                if distractors_list:
                    self._apply_distractors(task, distractors_list)
                    return task

            # ── SymPy-парсимые типы: fraction, expression, multiple_values ──
            if answer_type in ("fraction", "expression", "multiple_values",
                               "equation_solution", "inequality"):
                distractors_list = self._distractor_engine.generate_for_answer(
                    correct_answer=answer,
                    answer_type=answer_type,
                    count=3,
                )
                if distractors_list:
                    self._apply_distractors(task, distractors_list)
                    return task

            # ── Текстовые типы: AI (Gemini Pro) ──
            if answer_type in ("open_text", "text"):
                self._generate_ai_distractors(task)
                return task

            # ── Fallback: попробовать generate_for_answer для любого типа ──
            distractors_list = self._distractor_engine.generate_for_answer(
                correct_answer=answer,
                answer_type=answer_type,
                count=3,
            )
            if distractors_list:
                self._apply_distractors(task, distractors_list)

        except Exception as e:
            log.debug("Distractor generation failed (%s): %s", task.temp_id, e)

        return task

    def _generate_numeric_distractors(self, task: ExtractedTask, answer: str):
        """Числовые дистракторы через templatize → StepSolver."""
        try:
            val = float(answer.replace(",", "."))
        except ValueError:
            return []

        steps = None
        task_type = (task.answer_type or "arithmetic").lower()

        # 1) templatize → точный тип и формула
        try:
            from src.exam_builder.llm_templatizer import templatize_via_llm
            tpl = templatize_via_llm(
                task.question_text or "",
                task.question_latex or "",
                answer,
                task.answer_type or "exact_number",
            )
            if tpl is not None:
                task_type = tpl.task_type
                steps = self._step_solver.solve(
                    formula_text=tpl.answer_formula_text,
                    values=dict(tpl.original_values),
                    task_type=tpl.task_type,
                    original_text=tpl.text_template,
                )
        except Exception:
            pass

        # 2) Fallback: прямой StepSolver
        if not steps:
            steps = self._step_solver.solve(
                formula_text=task.question_text or "",
                values={},
                task_type=task_type,
            )

        return self._distractor_engine.generate(steps, val, task_type, count=3)

    def _generate_ai_distractors(self, task: ExtractedTask) -> None:
        """Текстовые дистракторы через Gemini Pro."""
        try:
            from src.pipeline.enrichment import AIDistractorGenerator
            gen = AIDistractorGenerator()
            meta = gen.generate(
                question_text=task.question_text or "",
                correct_answer=task.answer_raw or "",
                answer_type=task.answer_type or "open_text",
                count=3,
            )
            if meta:
                task.distractors = [d["value"] for d in meta]
                task.distractor_meta = meta
        except Exception as e:
            log.debug("AI distractor generation failed (%s): %s", task.temp_id, e)

    @staticmethod
    def _apply_distractors(task: ExtractedTask, distractors) -> None:
        """Записывает список Distractor в поля задачи."""
        task.distractors = [d.value for d in distractors]
        task.distractor_meta = [
            {
                "value": d.value,
                "error_type": d.error_type,
                "broken_step": d.broken_step,
                "plausibility": d.plausibility,
                "explanation": d.explanation,
            }
            for d in distractors
        ]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _process_paragraph(self, para: Dict) -> List[ExtractedTask]:
        number = para.get("number", "?")
        log.info("§%s: извлечение задач", number)

        tasks = self.extractor.extract(
            para["text"], number, para.get("title", ""), para.get("toc_id"),
        )
        if not tasks:
            return []

        log.info("   SymPy: верификация %d задач", len(tasks))
        tasks = [self.solver.verify(t) for t in tasks]

        log.info("   AI: обогащение")
        tasks = [self.enricher.enrich(t) for t in tasks]

        log.info("   Дистракторы")
        tasks = [self._generate_distractors(t) for t in tasks]

        log.info("   Маппинг на скелет")
        tasks = self.mapper.map_batch(tasks)

        log.info("   Запись в БД")
        self.writer.write_batch(tasks, self.textbook_id, self.class_level)

        verified = sum(1 for t in tasks if t.sympy_verified)
        mapped = sum(1 for t in tasks if t.skill_id)
        log.info(
            "   §%s: %d задач, %d верифицировано, %d привязано",
            number, len(tasks), verified, mapped,
        )
        return tasks

    def _load_toc_from_db(self) -> List[Dict]:
        db_url = CFG.get("database_url", os.getenv("DATABASE_URL", ""))
        if not db_url:
            return []
        try:
            from sqlalchemy import create_engine, text

            engine = create_engine(db_url)
            with engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT id, number, title, page_start, level "
                        "FROM textbook_toc "
                        "WHERE textbook_id = :tb_id AND level = 'paragraph' "
                        "ORDER BY sort_order"
                    ),
                    {"tb_id": self.textbook_id},
                ).fetchall()
            return [
                {"id": r[0], "number": r[1], "title": r[2], "page_start": r[3], "level": r[4]}
                for r in rows
            ]
        except Exception as e:
            log.warning("Не удалось загрузить TOC из БД: %s", e)
            return []

    def _load_progress(self) -> dict:
        if self.progress_file.exists():
            with open(self.progress_file, "r") as f:
                return json.load(f)
        return {"done_paragraphs": []}

    def _save_progress(self) -> None:
        with open(self.progress_file, "w") as f:
            json.dump(self.progress, f)
