# ShipTrackerRT — Real-Time Maritime Vessel Tracker

## Описание проекта

ShipTrackerRT — полностью бесплатная и открытая система для отслеживания морских судов в реальном времени с использованием публичных данных AIS (Automatic Identification System).

Система:
- Подключается к бесплатному AIS-стриму через WebSocket
- Получает актуальные позиции, курс, скорость и MMSI судов
- Сохраняет данные в PostgreSQL
- Отображает корабли на интерактивной карте (OpenStreetMap) с иконками и информацией
- Работает локально в Docker без каких-либо платных сервисов или API

Идеально для хобби, обучения, портфолио или мониторинга морского трафика в интересующем регионе.

## Возможности

- Реал-тайм обновление позиций судов (каждые секунды/минуты)
- Интерактивная карта с маркерами кораблей и popup-информацией
- Фильтрация по региону (bounding box)
- Хранение истории позиций в БД
- Аналитические дашборды (Apache Superset)
- Полная контейнеризация через Docker Compose

## Технологический стек

- **Python** (asyncio, websockets, pandas, pydantic)
- **PostgreSQL** + **pgAdmin**
- **MinIO** (S3-совместимое хранилище)
- **Streamlit** + **Folium** — реал-тайм карта на OpenStreetMap
- **Apache Superset** — дашборды и аналитика
- **Apache Airflow** — оркестрация задач
- **Docker** & **Docker Compose**

Всё 100% open-source и бесплатно.

## Быстрый старт

### 1. Клонируйте репозиторий

git commit -m "refactor: Restructure project architecture and consolidate configuration

BREAKING CHANGES:
- The logic of streamlit_app.py is divided into modules: streamlit.py, database.py, map_utils.py
- Split db_init.py into: db_pool.py, ship_repository.py, ais_client.py, main.py
- Reorganized project structure (dags/ -> app/, init_postgres -> collector/)
- Consolidated config files into single root config.py
- Removed redundant config re-exports

Project Structure Changes:
- Renamed 'dags/' to 'app/' (Streamlit web application)
- Moved AIS collection code from 'init_postgres/' to 'collector/'
- 'init_postgres/' now contains only SQL initialization files
- All code split into logical modules following clean architecture

Configuration:
- Created unified config.py in project root
- Removed duplicate DB_CONFIG definitions
- load_dotenv() called only once
- All modules import directly from root config.py

Code Quality:
- All comments have been improved
- Improved code documentation
- Better separation of concerns

Docker:
- Updated docker-compose.yaml paths
- PYTHONPATH configured correctly"