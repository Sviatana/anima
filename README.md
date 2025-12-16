# Anima

**Anima** — backend-проект на Python, используемый как основа для сервисов с API, автоматизациями и интеграциями.  
Репозиторий демонстрирует организацию backend-архитектуры, работу с базой данных, окружением, деплоем и CI/CD.

Проект не привязан к конкретному бизнес-кейсу и задуман как расширяемая база для дальнейшего развития.

---

## Стек

### Backend
- Python 3
- FastAPI
- AsyncIO

### База данных
- PostgreSQL
- SQLAlchemy (ORM / Core)
- SQL-схема и миграции

### Инфраструктура
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

Основные возможности

Асинхронный backend на FastAPI

Четкое разделение слоев (API / core / DB)

Работа с PostgreSQL

Конфигурация через переменные окружения

Готовность к облачному деплою

CI через GitHub Actions

Основа для подключения дополнительных модулей и внешних API

Запуск локально
# Установка зависимостей
pip install -r requirements.txt

# Подготовка окружения
cp .env.example .env

# Применение схемы БД
psql < db/schema.sql

# Запуск сервера
uvicorn api.main:app --reload

Деплой

Проект подготовлен для деплоя:

на Railway (конфигурация включена)

в Docker-окружении

на любом хостинге с поддержкой Python

Назначение репозитория

Этот репозиторий используется как:

демонстрация моего подхода к backend-разработке

база для новых сервисов

пример production-стиля кода и структуры

Автор

Svetlana Sidorenko
Python Backend Engineer

Website: https://ai24solutions.ru
LinkedIn: https://www.linkedin.com/in/sviatanasidarenka/
