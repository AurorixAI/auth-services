"""
ALGO V1 — Конвейер оцифровки учебников (thin wrapper)
scripts/digitization_pipeline.py

Вся логика перенесена в модульный пакет src/pipeline/.
Этот файл — точка входа для обратной совместимости CLI.

Использование:
  python scripts/digitization_pipeline.py pdf учебник.pdf --textbook-id <UUID> --class-level 5
  python scripts/digitization_pipeline.py json tasks.json --textbook-id <UUID>
  python scripts/digitization_pipeline.py audit --class-level 5
  python scripts/digitization_pipeline.py status
"""

import sys
from pathlib import Path

# Ensure project root is importable
_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

# Re-export public API for backward compatibility
from src.pipeline.models import ExtractedTask, DistractorResult        # noqa: F401,E402
from src.pipeline.orchestrator import DigitizationPipeline             # noqa: F401,E402
from src.pipeline.cli import main                                      # noqa: F401,E402

if __name__ == "__main__":
    main()
