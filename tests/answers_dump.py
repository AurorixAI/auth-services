"""PAY-TAILS Б11.34 п. А7: the REAL answer of a route is written to a file for the screens (fixtures of the screens = answers of the server, not hand-made).
Nothing is written unless ``ANSWERS_DIR`` is set (a normal run of the suite writes nothing)."""
import json
import os


def dump(name: str, body) -> None:
    folder = os.environ.get("ANSWERS_DIR")
    if folder:
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False, indent=2, sort_keys=True)
