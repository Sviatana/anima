# Anima

**Anima** — backend-проект на Python, который я использую как базу для сервисов с API, автоматизаций и AI-интеграций.  
Репозиторий показывает, как я организую backend-архитектуру, работу с БД, окружением, деплоем и CI/CD.

Проект не привязан к конкретному бизнес-кейсу и задуман как расширяемая основа для дальнейшего развития.

---

## Стек

**Backend**
- Python 3
- FastAPI
- AsyncIO

**База данных**
- PostgreSQL
- SQLAlchemy (ORM / Core)
- SQL-схема и миграции

**Инфраструктура**
- Docker
- Railway (конфигурация для деплоя)
- Procfile
- GitHub Actions (CI)

---

## Структура проекта

```text
anima/
│
├── api/                     # FastAPI приложение
│   ├── main.py              # Точка входа
│   ├── routes/              # API роуты
│   └── core/                # зависимости, настройки
│
├── db/                      # Работа с базой данных
│   ├── connection.py
│   ├── schema.sql
│   └── migrations/
│
├── .github/workflows/       # CI (проверки, задачи)
├── .env.example             # Пример переменных окружения
├── requirements.txt
├── Procfile
├── railway.json
└── README.md
