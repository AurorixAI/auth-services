# database/dev/ — Синтетические данные для разработки

Эта папка содержит SQL-файлы с **тестовыми/синтетическими данными**, которые НЕ нужны в production.

## Файлы

| Файл | Описание |
|------|----------|
| `013_grade5_full_synthetic.sql` | **994 задачи** (G5_SYN_*), 133 шаблона, 10 студентов, 800 answer_events. Полное покрытие 142 L4-навыков 5 класса. |
| `014_cleanup_synthetic.sql` | Скрипт **удаления** всех синтетических данных (подготовка к production). |

## Загрузка dev-данных

```bash
# После применения основных миграций + скелетов:
psql -U algo -d algo_v1 -f database/dev/013_grade5_full_synthetic.sql
```

## Очистка перед production

```bash
psql -U algo -d algo_v1 -f database/dev/014_cleanup_synthetic.sql
```

## Идентификация синтетических данных

- Задачи: `id LIKE 'G5_SYN_%'` или `id LIKE 'G5_GEN_%'`
- Шаблоны: `id LIKE 'TPL_SYN_%'`
- Events: `client_version = 'synthetic-1.0'`
- Студенты: `first_name LIKE 'Тест_%'`
