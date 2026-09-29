# Локальный запуск (Linux, macOS, Windows)

Сервис целиком работает в Docker: одни и те же команды подходят для Linux, macOS и Windows. Устройство системы описано в [OVERVIEW.md](OVERVIEW.md), подробности развёртывания — в [README.md](README.md).

## 0. Что должно быть установлено
- **Docker с Compose v2:**
  - Windows и macOS — Docker Desktop, на Windows с бэкендом WSL 2 и Linux-контейнерами;
  - Linux — Docker Engine и плагин `docker compose`;
  - проверка: `docker compose version`.
- **Git.**
- **Ресурсы:** не меньше 8 ГБ ОЗУ (лучше 16), около 20 ГБ свободного места, доступ в интернет. При первом запуске скачиваются образы и модель эмбеддингов e5 (около 1,1 ГБ).

## 1. Получить код
```bash
git clone https://github.com/WlcM111/weak_signals.git
cd weak_signals
```

## 2. Создать файл настроек `.env`
**Linux / macOS:**
```bash
cp .env.example .env
```
**Windows (PowerShell):**
```powershell
Copy-Item .env.example .env
```

Откройте `.env` в любом редакторе и заполните значения:
- Linux — `nano .env`;
- macOS — `open -e .env`;
- Windows — `notepad .env`.

| Переменная | Зачем | Где взять |
|---|---|---|
| `WS_GIGACHAT_CREDENTIALS` | Языковая модель: расширение запроса, рубрика, тексты карточек | Личный кабинет GigaChat API, «Авторизационные данные» |
| `WS_OPENALEX_API_KEY` | Поиск научных публикаций (OpenAlex требует ключ) | openalex.org → Settings → API key |
| `WS_CONTACT_EMAIL` | Контактный адрес в заголовке запросов к открытым API | Ваш e-mail |
| `WS_API_KEY` | Защита API; интерфейс передаёт ключ сам | Любая длинная случайная строка |
| `WS_SEMANTIC_SCHOLAR_API_KEY` | Необязательно: без ключа Semantic Scholar отвечает отказом | semanticscholar.org/product/api |

Остальные значения в `.env.example` уже заданы и подходят для запуска.

## 3. Только Linux: права на каталоги для обучения
Контейнер обучения работает от пользователя с uid 10001 и пишет в каталоги репозитория. Docker Desktop на Windows и macOS права не проверяет, поэтому там шаг не нужен.
```bash
sudo chown -R 10001:10001 ml/data ml/reports docs/ml
```

## 4. Собрать и запустить сервисы
Команды одинаковы в Linux, macOS и Windows (PowerShell). Первая сборка занимает 10–30 минут.
```bash
docker compose up -d --build postgres collector analyzer insight orchestrator-api orchestrator-worker ui
docker compose ps
```
Подождите, пока у всех сервисов, кроме analyzer, появится статус `healthy`. analyzer станет готов после обучения модели на следующем шаге.

## 5. Обучить модель analyzer
Около 10–15 минут, модель эмбеддингов скачивается при первом запуске.
```bash
docker compose --profile ml run --rm trainer build-dataset
docker compose --profile ml run --rm trainer train
docker compose ps
```
analyzer подхватывает модель сам и через 15–30 секунд переходит в `healthy`.

## 6. Открыть сервис
- Интерфейс: <http://localhost:8501>
- Документация API (Swagger): <http://127.0.0.1:8080/docs>

Введите направление, например «перспективные решения в финтехе». Задание занимает 5–10 минут, ход виден на панели «Ход задания».

## Остановка и сброс
```bash
docker compose stop
docker compose down
docker compose down -v
```
- `stop` — остановить, данные сохраняются;
- `down` — удалить контейнеры, данные в томах сохраняются;
- `down -v` — полный сброс: база, модель и кеш удаляются.

## Если что-то пошло не так
- **Порт занят** (8501, 8080 или 5599): освободите его или поменяйте привязку в `.env` (`WS_UI_BIND`, `WS_API_BIND`) и в `compose.yaml`.
- **analyzer не `healthy`:** модель ещё не обучена — выполните шаг 5.
- **`Permission denied` при обучении на Linux:** выполните шаг 3.
- **Журнал сервиса:** `docker compose logs <сервис> --tail 50`, например `docker compose logs insight --tail 50`.
