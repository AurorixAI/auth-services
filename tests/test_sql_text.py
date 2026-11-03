from src.auth.sql_text import contains, like_escape


def test_like_escape_makes_the_masks_ordinary_characters():
    assert like_escape("50%_\\") == "50\\%\\_\\\\"
    assert like_escape("plain") == "plain"


def test_contains_trims_and_wraps():
    assert contains("  50%  ") == "%50\\%%"
