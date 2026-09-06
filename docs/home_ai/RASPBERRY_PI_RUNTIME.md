# Home AI: локальный llama.cpp на Raspberry Pi 5

Этот runbook устанавливает CPU-only `llama-server` как отдельный systemd-сервис на
Raspberry Pi 5 8 GB. GGUF лежит в `/opt/home-ai/models`, вне Git, Docker image,
Home OS data и обычных deploy backup. Home OS обращается к host runtime через
`host.docker.internal`; Caddy не имеет маршрута к AI API.

Все шаги выполняются на production Pi вручную. CI не скачивает модель и не
запускает inference. Перед началом Home OS должен быть обновлён до ревизии,
содержащей этот runbook и `extra_hosts` в `docker-compose.yml`, но оставаться с
`AI_ENABLED=false`.

## 1. Установить llama.cpp

Скрипт принимает только ARM64, создаёт системного пользователя `home-ai`, ставит
минимальные build dependencies, клонирует официальный `ggml-org/llama.cpp`,
переключается на зафиксированный `v0.4.0` и собирает Release target. Повторный
запуск обновляет ту же checkout до выбранной ревизии, не удаляет модели и не
запускает сервис.

```bash
cd /opt/recipe_budget_service
sudo bash scripts/install_llama_cpp.sh
```

Update policy: не использовать автоматически изменяемую ветку `master`. Для
осознанного обновления сначала выбрать release/tag, затем явно выполнить:

```bash
sudo LLAMA_CPP_REVISION=v0.4.0 bash scripts/install_llama_cpp.sh
```

После будущей смены tag обязательно повторить smoke test и benchmark. Проверка:

```bash
/opt/home-ai/llama.cpp/build/bin/llama-server --version
git -C /opt/home-ai/llama.cpp rev-parse HEAD
```

## 2. Скачать один GGUF

Первый кандидат — **Qwen3.5-4B Q4_K_M**. Это text-only использование базовой
Qwen3.5-4B: `mmproj` не скачивается и не настраивается. Если 4B слишком медленна
или оставляет мало RAM, второй кандидат — **Qwen3.5-2B Q4_K_M**.

GGUF-конвертации взяты из широко используемых репозиториев Unsloth и закреплены
на конкретных Hugging Face revisions. Helper заранее проверяет Content-Length и
свободное место, пишет в `.partial`, сверяет размер и SHA-256 и только затем
атомарно переименовывает файл. Существующий target не перезаписывается.

4B, 2,740,937,888 bytes:

```bash
cd /opt/recipe_budget_service
sudo -u home-ai env \
  MODEL_URL='https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/resolve/e87f176479d0855a907a41277aca2f8ee7a09523/Qwen3.5-4B-Q4_K_M.gguf' \
  MODEL_PATH='/opt/home-ai/models/Qwen3.5-4B-Q4_K_M.gguf' \
  MODEL_SHA256='00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4' \
  bash scripts/download_home_ai_model.sh
```

2B fallback, 1,280,835,840 bytes:

```bash
cd /opt/recipe_budget_service
sudo -u home-ai env \
  MODEL_URL='https://huggingface.co/unsloth/Qwen3.5-2B-GGUF/resolve/f6d5376be1edb4d416d56da11e5397a961aca8ae/Qwen3.5-2B-Q4_K_M.gguf' \
  MODEL_PATH='/opt/home-ai/models/Qwen3.5-2B-Q4_K_M.gguf' \
  MODEL_SHA256='aaf42c8b7c3cab2bf3d69c355048d4a0ee9973d48f16c731c0520ee914699223' \
  bash scripts/download_home_ai_model.sh
```

Источник модели должен проверяться перед будущим изменением revision/hash:

- <https://huggingface.co/unsloth/Qwen3.5-4B-GGUF>
- <https://huggingface.co/unsloth/Qwen3.5-2B-GGUF>
- исходные модели Qwen: <https://huggingface.co/Qwen/Qwen3.5-4B> и
  <https://huggingface.co/Qwen/Qwen3.5-2B>

## 3. Определить host gateway и ограничить сеть

`127.0.0.1` внутри `web` — сам контейнер, а не Raspberry Pi. Compose добавляет
`host.docker.internal:host-gateway`. Найти фактический адрес без загрузки нового
образа можно через уже используемый Caddy image:

```bash
HOST_AI_GATEWAY="$(docker run --rm --pull never \
  --add-host host.docker.internal:host-gateway \
  caddy:2-alpine getent hosts host.docker.internal | awk 'NR==1 {print $1}')"
test -n "$HOST_AI_GATEWAY"
printf 'Host gateway: %s\n' "$HOST_AI_GATEWAY"
```

Обычно это `172.17.0.1`, но значение нельзя предполагать. `llama-server` должен
слушать только этот адрес, никогда не `0.0.0.0` или публичный/LAN IP.

Дополнительно ограничить порт host firewall. Для nftables сначала определить
compose subnet и bridge interface:

```bash
AI_DOCKER_NETWORK=recipe_budget_service_default
AI_DOCKER_SUBNET="$(docker network inspect -f '{{(index .IPAM.Config 0).Subnet}}' "$AI_DOCKER_NETWORK")"
AI_DOCKER_NETWORK_ID="$(docker network inspect -f '{{.Id}}' "$AI_DOCKER_NETWORK")"
AI_DOCKER_BRIDGE="$(docker network inspect -f '{{index .Options "com.docker.network.bridge.name"}}' "$AI_DOCKER_NETWORK")"
test -n "$AI_DOCKER_BRIDGE" || AI_DOCKER_BRIDGE="br-${AI_DOCKER_NETWORK_ID:0:12}"
printf 'Subnet=%s bridge=%s gateway=%s\n' "$AI_DOCKER_SUBNET" "$AI_DOCKER_BRIDGE" "$HOST_AI_GATEWAY"
```

Добавить эквивалент этих правил в постоянную конфигурацию firewall, согласовав с
уже существующими таблицами/менеджером firewall на Pi:

```nft
table inet home_ai {
    chain input {
        type filter hook input priority -10; policy accept;
        iifname "lo" ip daddr 172.17.0.1 tcp dport 8081 accept
        iifname "br-REPLACE" ip saddr 172.18.0.0/16 ip daddr 172.17.0.1 tcp dport 8081 accept
        tcp dport 8081 reject
    }
}
```

Заменить все три примерных значения фактическими, проверить `sudo nft -c -f
<file>`, затем применить способом, принятым на сервере. Последнее правило должно
блокировать доступ к 8081 со всех прочих interfaces. API key ниже остаётся вторым
независимым уровнем защиты. Порт 8081 не публикуется Docker и не добавляется в
`Caddyfile`.

## 4. Настроить environment и systemd

Installer создаёт `/etc/home-ai/llama-server.env` только при отсутствии. Открыть
его и заменить `LLAMA_HOST` на найденный gateway. Для 4B оставить:

```dotenv
LLAMA_SERVER_BIN=/opt/home-ai/llama.cpp/build/bin/llama-server
LLAMA_WORKING_DIRECTORY=/opt/home-ai/llama.cpp
LLAMA_MODEL_PATH=/opt/home-ai/models/Qwen3.5-4B-Q4_K_M.gguf
LLAMA_MODEL_ALIAS=qwen3.5-4b-q4_k_m
LLAMA_HOST=172.17.0.1
LLAMA_PORT=8081
LLAMA_STARTUP_TIMEOUT_SECONDS=540
LLAMA_THREADS=4
LLAMA_THREADS_BATCH=4
LLAMA_CTX_SIZE=4096
LLAMA_BATCH_SIZE=256
LLAMA_UBATCH_SIZE=128
LLAMA_PARALLEL=1
LLAMA_N_PREDICT=512
LLAMA_API_KEY_FILE=/etc/home-ai/llama-api-key
```

```bash
sudoedit /etc/home-ai/llama-server.env
sudo chown root:home-ai /etc/home-ai/llama-server.env
sudo chmod 0640 /etc/home-ai/llama-server.env
```

Начальный context 4096 намеренно меньше максимума модели. 8192 можно проверить
позже benchmark'ом, но wrapper не допускает больше 8192, batch больше 512 или
несколько slots. Не включать `mlock`: MemoryHigh=5G и MemoryMax=6G сохраняют RAM
для Home OS, SQLite, Caddy и Linux. Swap не является обязательной частью профиля.

Создать отдельный случайный API key, не печатая его в shell output:

```bash
sudo python3 -c "import secrets; from pathlib import Path; Path('/etc/home-ai/llama-api-key').write_text(secrets.token_urlsafe(48) + '\n', encoding='utf-8')"
sudo chown root:home-ai /etc/home-ai/llama-api-key
sudo chmod 0640 /etc/home-ai/llama-api-key
```

Проверить unit и включить автозапуск после reboot:

```bash
sudo systemd-analyze verify /etc/systemd/system/home-ai-llama.service
sudo systemctl daemon-reload
sudo systemctl enable home-ai-llama.service
sudo systemctl start home-ai-llama.service
sudo systemctl status home-ai-llama.service --no-pager
journalctl -u home-ai-llama.service -n 100 --no-pager
```

Загрузка модели на Pi может занять несколько минут. `ExecStartPost` ждёт реальный
`/health` до 540 секунд внутри общего startup timeout 10 минут;
`Restart=on-failure`, resource limits и journal logging заданы unit-файлом.

## 5. Проверить runtime вручную

Health не требует ключа, `/v1/models` и chat требуют:

```bash
curl --fail --silent --show-error "http://${HOST_AI_GATEWAY}:8081/health"
AI_KEY="$(sudo sed -n '/^[^#[:space:]]/ {p;q}' /etc/home-ai/llama-api-key)"
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${AI_KEY}" \
  "http://${HOST_AI_GATEWAY}:8081/v1/models"
unset AI_KEY
```

Не помещать ключ в history как буквальное значение. Переменная существует только
в текущем shell и сразу удаляется.

## 6. Подключить Home OS

В `/opt/recipe_budget_service/.env` выставить значения. `AI_MODEL` обязан точно
совпадать с `LLAMA_MODEL_ALIAS`; `AI_API_KEY` — первая строка key file.

```dotenv
AI_ENABLED=true
AI_BASE_URL=http://host.docker.internal:8081/v1
AI_MODEL=qwen3.5-4b-q4_k_m
AI_API_KEY=replace-with-local-key
AI_CONNECT_TIMEOUT_SECONDS=5
AI_READ_TIMEOUT_SECONDS=120
AI_MAX_TOKENS=512
AI_CONTEXT_BUDGET=6000
AI_MAX_CONCURRENCY=1
AI_TEMPERATURE=0.2
AI_ENABLE_THINKING=false
```

Qwen3.5 по умолчанию может генерировать длинный reasoning block. Home AI передаёт
`chat_template_kwargs.enable_thinking=false`: для коротких router/schema-задач на
Pi это сохраняет context и latency. Включать thinking следует только отдельным
измерением с увеличенным output/context budget.

```bash
cd /opt/recipe_budget_service
chmod 600 .env
docker compose config >/dev/null
docker compose up -d --no-build web
docker compose exec -T web python -c "import socket; print(socket.gethostbyname('host.docker.internal'))"
docker compose ps
```

Обычный `/health` остаётся healthy даже при выключенном/упавшем LLM. Войти в Home
OS, открыть `/ai/settings`, проверить статус runtime и включить только необходимые
пользовательские разрешения. Не добавлять AI route в Caddy.

## 7. Полный smoke test

Скрипт проверяет active systemd unit, `/health`, `/v1/models`, доступ из `web`,
короткий русский ответ, structured JSON и четыре безопасных сценария: разбор
`Лента 1840 вчера`, finance query, recipe query и menu proposal без записи.
Никаких confirm/write endpoint он не вызывает. Для каждого запроса сохраняются
first-token/total latency, token counts/tokens per second (если сервер прислал),
RSS llama-server и свободная память системы.

Для schema-задач smoke передаёт `thinking=false`, JSON Schema и тот же явный
read-only контракт, что benchmark: фиксированные `intent`, `tool`, имена
аргументов и enum-значения надо копировать буквально. Ошибка выводит фактически
возвращённый JSON, чтобы отличить выбор неверного маршрута от нарушения схемы.
Все диагностические запросы smoke и benchmark используют `temperature=0`,
фиксированный RNG seed и отключённый prompt cache, чтобы повторные прогоны были
сопоставимыми. Это детерминированный test profile; он намеренно не имитирует
production sampling temperature и не меняет production-настройки Home AI.
Флаг `--debug` печатает без API key параметры запроса, размеры prompt/schema,
SHA-256 сериализованных messages/schema, ответ и доступные server timings:
`prompt_ms`, `predicted_ms`, token rate и `cache_n` (cache hit/miss).

```bash
cd /opt/recipe_budget_service
sudo python3 scripts/home_ai_runtime_smoke.py \
  --base-url "http://${HOST_AI_GATEWAY}:8081/v1" \
  --model qwen3.5-4b-q4_k_m \
  --api-key-file /etc/home-ai/llama-api-key \
  --output /tmp/home-ai-smoke-4b.json
```

Root здесь нужен только диагностическому процессу: прочитать закрытый key file и
обратиться к Docker socket. Сам `llama-server` продолжает работать от `home-ai`.
Если Docker доступен deployment user и этот user явно входит в группу `home-ai`,
smoke можно выполнить без `sudo`.

## 8. Benchmark 4B против 2B

Benchmark использует одинаковые семь русских prompts и только синтетические ID.
Перед каждым запросом модель получает полный закрытый контракт: четыре допустимых
`intent`, пять `tool`, точную JSON Schema, разрешённые аргументы и enum-значения,
правила для короткой записи расхода и различие между `finance.summary` и
`finance.comparison`. Имена этого диагностического контракта синтетические и не
меняют production-промпты или инструменты Home AI. Benchmark ничего не записывает,
не выполняет выбранный tool и не выбирает победителя автоматически.

Результат оценивается строго и раздельно: `correct_intent`, `correct_tool`,
`correct_arguments`, `valid_structured_output` и `no_invented_ids`. Если
`correct_tool=false`, это routing failure. Если tool выбран верно, но intent или
arguments не совпали буквально, это contract/schema-name failure. Нарушение
JSON Schema отражается в `valid_structured_output=false`, а использование ID вне
выданного синтетического набора — в `no_invented_ids=false`. Для любого провала
скрипт печатает ожидаемые и фактические intent/tool/arguments; строгую проверку
не следует смягчать для улучшения результата.

Сначала запустить 4B:

```bash
sudo -u home-ai python3 scripts/home_ai_benchmark.py \
  --base-url "http://${HOST_AI_GATEWAY}:8081/v1" \
  --model qwen3.5-4b-q4_k_m \
  --candidate 'Qwen3.5-4B Q4_K_M' \
  --api-key-file /etc/home-ai/llama-api-key \
  --output /tmp/home-ai-benchmark-4b.json
```

Затем остановить сервис, изменить только `LLAMA_MODEL_PATH` и
`LLAMA_MODEL_ALIAS` на 2B, запустить и повторить:

```dotenv
LLAMA_MODEL_PATH=/opt/home-ai/models/Qwen3.5-2B-Q4_K_M.gguf
LLAMA_MODEL_ALIAS=qwen3.5-2b-q4_k_m
```

```bash
sudo systemctl restart home-ai-llama.service
sudo -u home-ai python3 scripts/home_ai_benchmark.py \
  --base-url "http://${HOST_AI_GATEWAY}:8081/v1" \
  --model qwen3.5-2b-q4_k_m \
  --candidate 'Qwen3.5-2B Q4_K_M' \
  --api-key-file /etc/home-ai/llama-api-key \
  --output /tmp/home-ai-benchmark-2b.json
python3 scripts/home_ai_benchmark.py --compare \
  /tmp/home-ai-benchmark-4b.json /tmp/home-ai-benchmark-2b.json
```

После выбора синхронно обновить `AI_MODEL` в Home OS `.env`. Начальная
рекомендация — 4B; перейти на 2B, если реальные latency/RSS мешают обычной работе
Home OS. Выбор фиксируется только после фактического запуска на Pi.

## 9. Логи, обновление и rollback

```bash
journalctl -u home-ai-llama.service -f
systemctl show home-ai-llama.service -p MemoryCurrent -p MemoryPeak -p MainPID
sudo systemctl restart home-ai-llama.service
```

Runtime и модели не входят в deploy snapshot и backup Home OS. Обычный deploy не
должен менять `/opt/home-ai` или `/etc/home-ai`.

Самое быстрое аварийное отключение — изменить production `.env`:

```dotenv
AI_ENABLED=false
```

и пересоздать только web:

```bash
cd /opt/recipe_budget_service
docker compose up -d --no-build web
```

Home OS продолжит работать без LLM, а основной `/health` останется независимым.
При необходимости runtime также можно остановить, не удаляя модели:

```bash
sudo systemctl disable --now home-ai-llama.service
```

Для rollback версии llama.cpp повторно вызвать installer с предыдущим tag и снова
выполнить smoke. Не удалять `/opt/home-ai/models` и не восстанавливать Home OS data.
