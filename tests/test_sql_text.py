from src.auth.sql_text import contains, like_escape


def test_like_escape_makes_the_masks_ordinary_characters():
    assert like_escape("50%_\\") == "50\\%\\_\\\\"
    assert like_escape("plain") == "plain"


def test_contains_trims_and_wraps():
    assert contains("  50%  ") == "%50\\%%"


def test_nobody_but_sql_text_escapes_a_like_pattern_by_hand():
    """E-D: the last hand-made copy (``AuthService.list_students_page``) uses ``sql_text.contains``; a pattern built by hand is the way a
    «%» of a search box becomes a mask again."""
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "src"
    offenders = [str(p.relative_to(src)) for p in src.rglob("*.py")
                 if p.name != "sql_text.py" and '.replace("%"' in p.read_text(encoding="utf-8")]
    assert offenders == []
