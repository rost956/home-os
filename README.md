# Дом / Home OS

Приватное веб-приложение для совместного домашнего быта: рецепты и меню, покупки, траты и доходы, финансовая аналитика, долги, чаты, списки желаний, фильмы и сериалы, памятные моменты и календарный планировщик. Интерфейс адаптирован для компьютеров и мобильных устройств и устанавливается как PWA.

## Стек

- Python 3.12, FastAPI, SQLAlchemy, Jinja2
- SQLite с WAL и внешними ключами
- Gunicorn + Uvicorn worker
- Caddy для HTTPS и reverse proxy
- Docker Compose
- Pytest и Ruff

## Локальный запуск

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

Для разработки измените в `.env`:

```dotenv
APP_ENV=development
SECRET_KEY=development-only-secret
DATABASE_URL=sqlite:///./data/app.db
DATA_DIR=data
REGISTRATION_ENABLED=true
ENFORCE_SAME_ORIGIN=false
SECURE_COOKIES=false
BACKGROUND_JOBS_ENABLED=false
```

Запуск:

```powershell
uvicorn app.main:app --reload
```

Приложение будет доступно на `http://127.0.0.1:8000`. Каталог `data/` и `.env` локальны и никогда не должны попадать в Git.

## Переменные окружения

Полный безопасный шаблон находится в `.env.example`.

| Переменная | Назначение |
|---|---|
| `APP_ENV` | `development`, `test` или `production` |
| `SECRET_KEY` | Случайный секрет не короче 32 символов в production |
| `DATABASE_URL` | URL базы, по умолчанию `sqlite:///./data/app.db` |
| `DATA_DIR` | Каталог БД, загрузок и бэкапов |
| `CADDY_SITE_ADDRESS` | Домен, который обслуживает Caddy |
| `ALLOWED_HOSTS` | Дополнительные допустимые Host через запятую |
| `BACKUP_ADMIN_USERNAME` | Пользователь с доступом к импорту и бэкапам |
| `REGISTRATION_ENABLED` | Разрешает открытую регистрацию |
| `ENFORCE_SAME_ORIGIN` | Требует Origin/Referer для изменяющих запросов |
| `SECURE_COOKIES` | Передаёт cookie с флагом Secure |
| `BACKGROUND_JOBS_ENABLED` | Включает бэкапы и фоновые напоминания |
| `TIMER_REMINDER_MINUTES` | Через сколько минут напомнить о таймере, от 30 до 10080 |
| `HOME_PUSH_ENABLED` | Включает Web Push; по умолчанию `false` |
| `HOME_PUSH_POLL_SECONDS` | Интервал Planner scheduler, по умолчанию 60 секунд |
| `HOME_PUSH_CATCHUP_MINUTES` | Grace window после короткого простоя, по умолчанию 60 минут |
| `HOME_VAPID_PUBLIC_KEY` | Публичный application server key для браузера |
| `HOME_VAPID_PRIVATE_KEY` | Private key или путь внутри контейнера; значение не коммитить |
| `HOME_VAPID_SUBJECT` | VAPID contact URI вида `mailto:` или `https://` |
| `PUSH_ENDPOINT_HOSTS` | Дополнительные доверенные хосты Web Push |
| `HOME_FILE_SHARE_DIR` | Каталог временных передач внутри `DATA_DIR` |
| `HOME_FILE_SHARE_MAX_FILE_MB` | Максимальный размер одного файла, по умолчанию 100 МБ |
| `HOME_FILE_SHARE_MAX_TRANSFER_MB` | Максимальный общий размер передачи, по умолчанию 500 МБ |
| `HOME_FILE_SHARE_CLEANUP_SECONDS` | Интервал очистки истёкших передач, по умолчанию 3600 секунд |
| `HOME_FILE_SHARE_ORPHAN_GRACE_HOURS` | Grace period для orphan/partial файлов, по умолчанию 24 часа |
| `HOME_FILE_SHARE_MAX_STORAGE_MB` | Общий safety cap хранилища, `0` = без лимита |
| `HOME_FILE_SHARE_MAX_USER_STORAGE_MB` | Личный safety cap, `0` = без лимита |
| `HOME_FILE_SHARE_MIN_FREE_MB` | Минимальный свободный резерв диска, по умолчанию 512 МБ |
| `APP_UID`, `APP_GID` | UID/GID пользователя внутри web-контейнера |

Секрет можно создать командой:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

## Проверки

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pip_audit -r requirements-dev.txt --vulnerability-service osv
```

## Docker

Создайте серверный `.env` из шаблона, задайте случайный `SECRET_KEY`, домен и администратора. Затем:

```bash
mkdir -p data
docker compose config
docker compose build
docker compose up -d
docker compose ps
```

Порт `8000` доступен только внутри compose-сети; снаружи приложение обслуживает Caddy на `80/443`. Проверить health без публикации порта можно так:

```bash
docker compose exec -T web python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health').read().decode())"
```

Локальный Home AI по умолчанию выключен. Production-инструкция для отдельного
host `llama-server` на Raspberry Pi 5, закрытой Docker-сети и ручного benchmark:
[`docs/home_ai/RASPBERRY_PI_RUNTIME.md`](docs/home_ai/RASPBERRY_PI_RUNTIME.md).

Web-контейнер работает без root, с read-only root filesystem. На старой установке один раз перед первым обновлением исправьте владельца persistent data под UID/GID из `.env`:

```bash
sudo chown -R "${USER}:${USER}" /opt/recipe_budget_service/data
```

## Production data

Все изменяемые данные находятся вне образа в `/opt/recipe_budget_service/data`:

- `app.db`, `app.db-wal`, `app.db-shm`;
- `media/` с изображениями и вложениями;
- `push_vapid.json`;
- `backups/`;
- `deployments/` с журналами деплоя.

Деплой не удаляет и не копирует этот каталог. Не размещайте production `.env` внутри checkout self-hosted runner.

## Ручной деплой

Сначала один раз подготовьте каталог и серверный конфиг:

```bash
sudo install -d -o "$USER" -g "$USER" /opt/recipe_budget_service
cd /opt/recipe_budget_service
# Выполняется из checkout проекта или после загрузки шаблона на сервер.
cp .env.example .env
chmod 600 .env
```

После заполнения `.env` с рабочей машины:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy.ps1 `
  -ServerHost "server.example.lan" `
  -ServerUser "deploy"
```

Скрипт не отправляет локальные `.env`, БД, uploads или ключи. На сервере он:

1. блокирует параллельные деплои;
2. создаёт согласованный SQLite backup;
3. собирает новый образ, пока старая версия продолжает работать;
4. переключает контейнеры и ждёт `/health`;
5. при ошибке возвращает предыдущий код и Docker image;
6. пишет лог в `data/deployments/`.

При необходимости Docker через sudo добавьте `-UseSudoDocker`.

## Бэкап и восстановление

Ручной бэкап базы без остановки приложения:

```bash
python3 scripts/sqlite_backup.py backup \
  --database data/app.db \
  --backup-dir data/backups \
  --retention 14 \
  --prefix manual \
  --include-files
```

Восстановление останавливает только web-контейнер, предварительно сохраняет текущую БД и проверяет целостность архива:

```bash
bash scripts/restore_backup.sh \
  --target /opt/recipe_budget_service \
  --archive /opt/recipe_budget_service/data/backups/manual_YYYYMMDDTHHMMSSZ.zip
```

Для автоматизации можно добавить `--yes`. Перед восстановлением скопируйте архив на отдельный носитель и убедитесь, что на диске достаточно места.

## Rollback и логи

Неудачный CD автоматически возвращает предыдущее приложение. Бэкап БД не восстанавливается автоматически, чтобы не потерять записи, сделанные между переключением и ошибкой; для несовместимого изменения схемы используйте явное восстановление выше.

Диагностика:

```bash
cd /opt/recipe_budget_service
docker compose ps
docker compose logs --tail=200 web caddy
ls -lt data/deployments | head
tail -n 200 data/deployments/<deploy-log>.log
```

## CI

`.github/workflows/ci.yml` запускается для pull request и push в `main` на GitHub-hosted runner. Он выполняет Ruff, весь pytest suite, import smoke test, Docker build, запуск контейнера и реальный healthcheck. Dependabot еженедельно проверяет Python-пакеты и GitHub Actions.

## CD и self-hosted runner

CD запускается только через `workflow_run`, когда CI для push в `main` завершился успешно. Pull request никогда не выполняется на домашнем runner. Нужны точные labels:

```text
self-hosted, linux, ARM64, home-server, production
```

В настройках приватного GitHub-репозитория:

1. добавьте self-hosted runner для Linux ARM64;
2. назначьте labels `home-server` и `production`;
3. задайте repository variable `PRODUCTION_PATH`, если путь отличается от `/opt/recipe_budget_service`;
4. убедитесь, что пользователь runner имеет доступ к Docker и на запись в production directory;
5. храните production `.env` только в production directory, не в GitHub Secrets и не в runner checkout.

Workflow имеет только `contents: read`, разворачивает ровно SHA, прошедший CI, и не допускает параллельные production deploy.

## REG.RU DDNS

Сервис может обновлять A-запись при смене публичного IP. Учётные данные хранятся в `/etc/recipe-budget-ddns.env` с режимом `600`, вне Git:

```bash
bash scripts/install_reg_ru_ddns.sh /opt/recipe_budget_service
sudoedit /etc/recipe-budget-ddns.env
bash scripts/install_reg_ru_ddns.sh /opt/recipe_budget_service
journalctl -u reg-ru-ddns.service -n 50 --no-pager
```

REG.RU должен разрешать API-доступ с текущего публичного IP или подходящего диапазона.

## Безопасность Git

`.gitignore` и `.dockerignore` исключают `.env`, БД, WAL/SHM, uploads, backups, private keys, архивы, кеши и локальные audit artifacts. Перед публикацией проверьте:

```bash
git status --short
git diff --cached --check
git grep -n -I -E "(SECRET_KEY|PASSWORD|PRIVATE_KEY)=.+" -- ':!*.example'
```

Репозиторий должен оставаться приватным.
