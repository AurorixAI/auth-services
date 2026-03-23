"""
ALGO V1 — Mathpix OCR Module
src/pipeline/ocr.py

PDF → LaTeX/Markdown через Mathpix API с кэшированием по хэшу файла.
"""

import hashlib
import json
import logging
import os
import time
from pathlib import Path

from src.domain.pipeline.config import CFG

log = logging.getLogger("pipeline")


class MathpixOCR:
    """Конвертирует PDF учебника в LaTeX/Markdown через Mathpix API."""

    API_URL = "https://api.mathpix.com/v3/pdf"

    def __init__(self, app_id: str = "", app_key: str = ""):
        self.app_id = app_id or CFG.get("mathpix", {}).get(
            "app_id", os.getenv("MATHPIX_APP_ID", "")
        )
        self.app_key = app_key or CFG.get("mathpix", {}).get(
            "app_key", os.getenv("MATHPIX_APP_KEY", "")
        )
        self.cache_dir = Path(CFG.get("cache_dir", ".pipeline_cache"))
        self.cache_dir.mkdir(exist_ok=True)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def process_pdf(self, pdf_path: str) -> str:
        """PDF → текст (LaTeX + Markdown). Кэшируется по SHA-256."""
        pdf = Path(pdf_path)
        if not pdf.exists():
            raise FileNotFoundError(f"PDF не найден: {pdf_path}")

        file_hash = self._hash_file(pdf)
        cache_file = self.cache_dir / f"{file_hash}.md"

        if cache_file.exists():
            log.info("OCR из кэша: %s", cache_file)
            return cache_file.read_text(encoding="utf-8")

        if not self.app_id or not self.app_key:
            raise ValueError("MATHPIX_APP_ID и MATHPIX_APP_KEY не заданы")

        log.info(
            "Отправка в Mathpix: %s (%d KB)",
            pdf.name,
            pdf.stat().st_size // 1024,
        )

        import requests

        with open(pdf, "rb") as f:
            resp = requests.post(
                self.API_URL,
                headers={"app_id": self.app_id, "app_key": self.app_key},
                files={"file": (pdf.name, f, "application/pdf")},
                data={
                    "options_json": json.dumps(
                        {
                            "conversion_formats": {"md": True},
                            "math_inline_delimiters": ["$", "$"],
                            "math_display_delimiters": ["$$", "$$"],
                            "enable_tables_fallback": True,
                        }
                    )
                },
                timeout=120,
            )

        resp.raise_for_status()
        result = resp.json()
        pdf_id = result.get("pdf_id")

        if not pdf_id:
            raise RuntimeError(f"Mathpix не вернул pdf_id: {result}")

        text = self._poll_result(pdf_id)
        cache_file.write_text(text, encoding="utf-8")
        log.info("OCR завершён: %d символов → %s", len(text), cache_file)
        return text

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _poll_result(self, pdf_id: str, max_wait: int = 600) -> str:
        """Опрос статуса Mathpix до завершения."""
        import requests

        url = f"{self.API_URL}/{pdf_id}"
        headers = {"app_id": self.app_id, "app_key": self.app_key}
        start = time.time()

        while time.time() - start < max_wait:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status", "")

            if status == "completed":
                md_url = data.get("md", {}).get("url")
                if md_url:
                    md_resp = requests.get(md_url, timeout=60)
                    md_resp.raise_for_status()
                    return md_resp.text
                return json.dumps(data, ensure_ascii=False)

            if status == "error":
                raise RuntimeError(f"Mathpix ошибка: {data}")

            log.info("Mathpix: %s... (%ds)", status, int(time.time() - start))
            time.sleep(10)

        raise TimeoutError(f"Mathpix таймаут ({max_wait}s)")

    @staticmethod
    def _hash_file(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()[:16]
