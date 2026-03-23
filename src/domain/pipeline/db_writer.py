"""
ALGO V1 — Database Writer
src/pipeline/db_writer.py

Пакетная запись задач в PostgreSQL:
  • tasks_master    — upsert по task_id
  • textbook_tasks  — bridge-таблица (textbook ↔ task)
"""

import json
import logging
import os
from typing import List

from src.domain.pipeline.config import CFG
from src.domain.pipeline.models import ExtractedTask

log = logging.getLogger("pipeline")


class DBWriter:
    """Записывает задачи в PostgreSQL (tasks_master + textbook_tasks)."""

    def __init__(self, db_url: str = ""):
        self.db_url = db_url or CFG.get("database_url", os.getenv("DATABASE_URL", ""))

    def write_batch(
        self,
        tasks: List[ExtractedTask],
        textbook_id: str = "",
        class_level: int = 5,
        prefix: str = "G5_TB",
    ) -> int:
        """Записывает пакет задач. Возвращает количество записанных."""
        if not self.db_url:
            log.error("DATABASE_URL не задан")
            return 0

        from sqlalchemy import create_engine, text

        engine = create_engine(self.db_url)
        written = 0

        with engine.begin() as conn:
            for task in tasks:
                if not task.question_text:
                    log.warning("Пропуск %s: нет текста задачи", task.temp_id)
                    continue
                if not task.skill_id and not task.toc_id:
                    log.warning(
                        "Пропуск %s: нет ни skill_id, ни toc_id (нужен хотя бы один)",
                        task.temp_id,
                    )
                    continue

                task_id = (
                    f"{prefix}_{task.paragraph_number}_{task.exercise_number}".replace(
                        ".", "_"
                    ).replace(" ", "")
                )

                irt_b = {"A": -1.0, "B": 0.5, "C": 1.5}.get(task.difficulty, 0.0)
                irt_a = 1.0
                irt_c = 0.2 if task.answer_type == "multiple_choice" else 0.0

                conn.execute(
                    text("""
                        INSERT INTO tasks_master (
                            id, skill_id, question_text, question_latex, correct_answer,
                            answer_type, difficulty, cognitive_load,
                            irt_discrimination, irt_difficulty, irt_guessing,
                            solution_steps, hints, common_mistakes, distractor_meta, answer_options,
                            toc_id, tags,
                            verification_status, source_type, source_reference,
                            is_active, created_at
                        ) VALUES (
                            :id, :skill_id, :question_text, :question_latex, :correct_answer,
                            :answer_type, :difficulty, :cognitive_load,
                            :irt_a, :irt_b, :irt_c,
                            :solution_steps, :hints, :common_mistakes, :distractor_meta, :answer_options,
                            :toc_id, :tags,
                            'verified', 'textbook', :source_ref,
                            TRUE, NOW()
                        )
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
                    """),
                    {
                        "id": task_id,
                        "skill_id": task.skill_id or None,
                        "question_text": task.question_text,
                        "question_latex": task.question_latex,
                        "correct_answer": task.sympy_answer or task.answer_raw,
                        "answer_type": task.answer_type,
                        "difficulty": task.difficulty,
                        "cognitive_load": task.cognitive_load,
                        "irt_a": irt_a,
                        "irt_b": irt_b,
                        "irt_c": irt_c,
                        "solution_steps": (
                            json.dumps(task.solution_steps, ensure_ascii=False)
                            if task.solution_steps
                            else None
                        ),
                        "hints": (
                            json.dumps(task.hints, ensure_ascii=False)
                            if task.hints
                            else None
                        ),
                        "common_mistakes": (
                            json.dumps(task.common_mistakes, ensure_ascii=False)
                            if task.common_mistakes
                            else None
                        ),
                        "distractor_meta": (
                            json.dumps(task.distractor_meta, ensure_ascii=False)
                            if task.distractor_meta
                            else None
                        ),
                        "answer_options": (
                            json.dumps(task.distractors, ensure_ascii=False)
                            if task.distractors
                            else None
                        ),
                        "toc_id": task.toc_id,
                        "tags": json.dumps(task.tags, ensure_ascii=False),
                        "source_ref": (
                            f"textbook:{textbook_id}:§{task.paragraph_number}"
                            f":ex{task.exercise_number}"
                        ),
                    },
                )

                if textbook_id:
                    conn.execute(
                        text("""
                            INSERT INTO textbook_tasks (
                                textbook_id, task_id, structure_id,
                                exercise_number, page_number, original_text
                            ) VALUES (
                                :tb_id, :task_id, :struct_id,
                                :ex_num, :page, :orig_text
                            )
                            ON CONFLICT (textbook_id, task_id) DO NOTHING
                        """),
                        {
                            "tb_id": textbook_id,
                            "task_id": task_id,
                            "struct_id": None,
                            "ex_num": task.exercise_number,
                            "page": task.page or None,
                            "orig_text": task.question_text,
                        },
                    )

                written += 1

        log.info("Записано в БД: %d/%d задач", written, len(tasks))
        return written
