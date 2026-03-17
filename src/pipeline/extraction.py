"""
ALGO V1 — Text Extraction Modules
src/pipeline/extraction.py

Три класса для извлечения контента из OCR-текста:
  • LegendExtractor  — легенда маркеров учебника (★, ●, ◆ …)
  • ParagraphSplitter — разбивка текста на параграфы по TOC
  • TaskExtractor     — извлечение задач из параграфа (Gemini Flash)
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

from src.pipeline.config import CFG
from src.pipeline.gemini_client import call_gemini, get_api_key, get_model, parse_json_response
from src.pipeline.models import ExtractedTask

log = logging.getLogger("pipeline")


# ============================================================================
# LegendExtractor
# ============================================================================

class LegendExtractor:
    """Извлекает легенду условных обозначений из первых страниц учебника.

    Результат: ``{символ: категория}``
    Пример: ``{"★": "advanced", "●": "oral", "◆": "research"}``
    """

    CATEGORIES: Dict[str, str] = {
        "standard": "обычная задача (без маркера)",
        "advanced": "повышенной сложности (★, 🔴, и т.д.)",
        "olympiad": "олимпиадная / ★★",
        "oral": "устно / подумай (●, П)",
        "research": "исследование / проект (◆)",
        "project": "проект / практическая работа",
        "with_drawing": "задача с чертежом / рисунком (📐)",
    }

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or get_api_key()

    def extract_legend(self, full_text: str, max_chars: int = 6000) -> Dict[str, str]:
        intro = full_text[:max_chars]
        prompt = self._build_prompt(intro)
        try:
            response = call_gemini(prompt, api_key=self.api_key, temperature=0.0, max_tokens=2048)
            legend = self._parse(response)
            log.info("LegendExtractor: найдено %d маркеров: %s", len(legend), legend)
            return legend
        except Exception as e:
            log.warning("LegendExtractor: не удалось извлечь легенду: %s", e)
            return self._default()

    # ------------------------------------------------------------------

    def _build_prompt(self, text: str) -> str:
        cats = "\n".join(f"  - {k}: {v}" for k, v in self.CATEGORIES.items())
        return (
            "Ты — эксперт по российским школьным учебникам математики.\n\n"
            "Перед тобой начало учебника (введение, условные обозначения, предисловие).\n"
            "Извлеки ВСЕ условные обозначения (маркеры), которые используются для типов задач.\n\n"
            f"Текст:\n---\n{text}\n---\n\n"
            f"Категории:\n{cats}\n\n"
            "Верни JSON-объект {символ: категория}. Если не найдено — {}.\n"
            "Только JSON, без комментариев."
        )

    def _parse(self, response: str) -> Dict[str, str]:
        raw = parse_json_response(response)
        valid: Dict[str, str] = {}
        if not isinstance(raw, dict):
            return self._default()
        for marker, category in raw.items():
            if category in self.CATEGORIES:
                valid[marker] = category
            else:
                log.warning("LegendExtractor: неизвестная категория '%s' для '%s'", category, marker)
                valid[marker] = "advanced"
        return valid

    @staticmethod
    def _default() -> Dict[str, str]:
        return {"★": "advanced", "*": "advanced", "●": "oral", "◆": "research"}


# ============================================================================
# ParagraphSplitter
# ============================================================================

class ParagraphSplitter:
    """Разбивает OCR-текст на параграфы по оглавлению (``textbook_toc``)."""

    def __init__(self, toc: List[Dict]):
        """``toc`` — список dict: ``{id, number, title, page_start, level}``."""
        self.toc = sorted(toc, key=lambda x: x.get("page_start", 0) or 0)

    def split(self, text: str) -> List[Dict]:
        """Возвращает ``[{toc_id, number, title, text}, …]``."""
        if not self.toc:
            return [{"toc_id": None, "number": "1", "title": "Весь текст", "text": text}]

        paragraphs: List[Dict] = []
        lines = text.split("\n")

        for i, entry in enumerate(self.toc):
            pattern = self._make_pattern(entry)
            start_idx = self._find_line(lines, pattern)
            if start_idx is None:
                continue

            end_idx = len(lines)
            if i + 1 < len(self.toc):
                next_pat = self._make_pattern(self.toc[i + 1])
                nxt = self._find_line(lines, next_pat, start_from=start_idx + 1)
                if nxt is not None:
                    end_idx = nxt

            chunk = "\n".join(lines[start_idx:end_idx]).strip()
            if chunk:
                paragraphs.append({
                    "toc_id": entry.get("id"),
                    "number": entry.get("number", ""),
                    "title": entry.get("title", ""),
                    "text": chunk,
                })

        if not paragraphs:
            log.warning("Не удалось разбить текст по TOC, возвращаем целиком")
            return [{"toc_id": None, "number": "1", "title": "Неразмеченный текст", "text": text}]
        return paragraphs

    # ------------------------------------------------------------------

    @staticmethod
    def _make_pattern(entry: Dict) -> str:
        num = re.escape(entry.get("number", "").strip())
        title = re.escape(entry.get("title", "").strip()[:40])
        if num and title:
            return f"{num}.*{title}"
        return num or title

    @staticmethod
    def _find_line(lines: List[str], pattern: str, start_from: int = 0) -> Optional[int]:
        if not pattern:
            return None
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            return None
        for i in range(start_from, len(lines)):
            if regex.search(lines[i]):
                return i
        return None


# ============================================================================
# TaskExtractor
# ============================================================================

class TaskExtractor:
    """Извлекает задачи из текста параграфа через Gemini Flash."""

    def __init__(self, api_key: str = "", legend: Optional[Dict[str, str]] = None):
        self.api_key = api_key or get_api_key()
        self.legend = legend or {}

    def set_legend(self, legend: Dict[str, str]) -> None:
        self.legend = legend

    def extract(
        self,
        paragraph_text: str,
        paragraph_number: str = "",
        paragraph_title: str = "",
        toc_id: Optional[int] = None,
    ) -> List[ExtractedTask]:
        if not paragraph_text.strip():
            return []
        prompt = self._build_prompt(paragraph_text, paragraph_number, paragraph_title)
        try:
            response = call_gemini(
                prompt, api_key=self.api_key, temperature=0.1, max_tokens=8192,
            )
            tasks = self._parse(response, paragraph_number, paragraph_title, toc_id)
            log.info("§%s: извлечено %d задач", paragraph_number, len(tasks))
            return tasks
        except Exception as e:
            log.error("Ошибка извлечения §%s: %s", paragraph_number, e)
            return []

    # ------------------------------------------------------------------

    def _build_prompt(self, text: str, number: str, title: str) -> str:
        if self.legend:
            legend_lines = "\n".join(f"  {m} → {c}" for m, c in self.legend.items())
            legend_section = (
                f"\nУСЛОВНЫЕ ОБОЗНАЧЕНИЯ УЧЕБНИКА:\n{legend_lines}\n"
                "Используй эту легенду для task_marker и task_category.\n"
                'Если нет маркера — task_marker="", task_category="standard".'
            )
        else:
            legend_section = (
                '\nМаркеры: если ★ или «повышенной сложности» → '
                'task_marker="★", task_category="advanced".'
            )
        return (
            "Ты — эксперт по математике. Извлеки ВСЕ задачи из параграфа.\n\n"
            f"Параграф: {number} «{title}»\n{legend_section}\n\n"
            f"Текст:\n---\n{text[:8000]}\n---\n\n"
            "Для каждой задачи верни JSON:\n"
            '{"exercise_number":"3.15","question_text":"...","question_latex":"...",'
            '"answer_raw":"11/12","answer_type":"fraction","difficulty":"B",'
            '"cognitive_load":"apply","task_marker":"★","task_category":"advanced",'
            '"image_description":""}\n\n'
            "answer_type: exact_number, expression, multiple_choice, text, fraction, "
            "equation_solution, set, coordinate, inequality\n"
            "difficulty: A (лёгкий), B (средний), C (сложный)\n"
            "task_category: standard, advanced, olympiad, oral, research, project, with_drawing\n\n"
            "Верни JSON-массив. Только JSON."
        )

    def _parse(
        self, response: str, para_num: str, para_title: str, toc_id: Optional[int],
    ) -> List[ExtractedTask]:
        items = parse_json_response(response)
        if isinstance(items, dict):
            items = items.get("tasks", [items])
        if not isinstance(items, list):
            log.error("Неожиданный формат ответа: %s", type(items))
            return []

        valid_cats = {
            "standard", "advanced", "olympiad", "oral",
            "research", "project", "with_drawing",
        }
        tasks: List[ExtractedTask] = []
        for i, item in enumerate(items):
            task_marker = str(item.get("task_marker", "")).strip()
            task_category = str(item.get("task_category", "standard")).strip()
            if task_category not in valid_cats:
                task_category = "advanced" if task_marker else "standard"
            is_star = task_category in ("advanced", "olympiad")

            tasks.append(ExtractedTask(
                temp_id=f"TEMP_{para_num}_{i + 1:03d}",
                exercise_number=str(item.get("exercise_number", f"{i + 1}")),
                paragraph_number=para_num,
                paragraph_title=para_title,
                question_text=item.get("question_text", ""),
                question_latex=item.get("question_latex", ""),
                answer_raw=str(item.get("answer_raw", "")),
                answer_type=item.get("answer_type", "exact_number"),
                difficulty=item.get("difficulty", "B"),
                cognitive_load=item.get("cognitive_load", "apply"),
                is_star=is_star,
                task_marker=task_marker,
                task_category=task_category,
                image_description=item.get("image_description", ""),
                toc_id=toc_id,
                tags={
                    "exercise": item.get("exercise_number", ""),
                    "paragraph": para_num,
                    "marker": task_marker,
                    "category": task_category,
                    "star": is_star,
                },
            ))
        return tasks
