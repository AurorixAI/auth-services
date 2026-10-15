"""N13 P2.2: pure helpers of the payment cheques (file names, response headers).  No database."""
import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars-ok")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-must-be-at-least-32")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_test")


def test_stored_filename_is_a_display_name_with_the_right_extension():
    from src.auth.payments_files import stored_filename

    assert stored_filename("chek.png", "image/png") == "chek.png"
    assert stored_filename("../../etc/passwd", "image/png") == "passwd.png"
    assert stored_filename("a\r\nSet-Cookie: x=1.jpg", "image/jpeg") == "a_Set-Cookie_ x=1.jpg"
    assert stored_filename(None, "application/pdf") == "check.pdf"
    assert stored_filename("   ", "image/jpeg") == "check.jpg"
    assert stored_filename("фото чека.JPG", "image/jpeg") == "фото чека.JPG"
    assert len(stored_filename("x" * 500, "image/png")) <= 200


def test_content_disposition_cannot_be_broken_out_of():
    from src.auth.payments_files import content_disposition, file_headers

    for hostile in ('a";\r\nX-Evil: 1', "a\\b;c", "\r\n", "чек №1 .png"):
        header = content_disposition(hostile)
        assert "\r" not in header and "\n" not in header
        assert header.startswith('inline; filename="') and "filename*=UTF-8''" in header
        quoted = header.split('filename="', 1)[1].split('"', 1)[0]
        assert '"' not in quoted and "\\" not in quoted and ";" not in quoted
        assert all(0x20 <= ord(ch) < 0x7F for ch in header)
    assert content_disposition("чек.pdf").endswith("filename*=UTF-8''%D1%87%D0%B5%D0%BA.pdf")
    headers = file_headers("x.pdf")
    assert headers["X-Content-Type-Options"] == "nosniff" and headers["Content-Disposition"].startswith("inline;")
    assert headers["Cache-Control"] == "private, no-store" and headers["Content-Security-Policy"] == "sandbox"


def test_person_name_is_last_first_patronymic_like_the_models():
    from src.auth.payments_files import person_name
    from src.models.auth import Student, User

    assert person_name("Тест", "Claude") == "Тест Claude"
    assert person_name("Каримова", "Алия", "Ахмедовна") == "Каримова Алия Ахмедовна"
    assert person_name("  Каримова ", " Алия ", "  ") == "Каримова Алия"
    assert person_name("", None) is None and person_name(" ", "  ", None) is None
    user = User(first_name="Алия", last_name="Каримова", patronymic="Ахмедовна")
    assert person_name(user.last_name, user.first_name, user.patronymic) == user.full_name
    student = Student(first_name="Claude", last_name="Тест")
    assert person_name(student.last_name, student.first_name) == student.full_name


def test_staff_name_is_first_last_like_teachers_on_groups():
    from src.auth.payments_files import staff_name

    assert staff_name("Дилноза", "Каримова") == "Дилноза Каримова"
    assert staff_name(" Arslan ", "Djemilov ") == "Arslan Djemilov"
    assert staff_name("", None) is None and staff_name(" ", " ") is None
