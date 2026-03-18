# GitHub Review Bot for Telegram

Телеграм-бот для проверки учебных GitHub-репозиториев.

Бот умеет:

- принимать ссылку на публичный GitHub-репозиторий;
- скачивать проект через `github.com` и `codeload.github.com`;
- делать локальный эвристический разбор;
- отдавать проверку бесплатным AI-провайдерам по fallback-цепочке;
- строго разделять режимы:
  - AI доступен -> отчет только от AI;
  - AI недоступен -> отчет только локальный;
- считать локальный бюджет токенов и останавливать AI-проверки при исчерпании лимита;
- принимать ТЗ в формате `pdf`, `docx`, `txt`, `md` и сравнивать требования с кодом репозитория;
- запускать регрессионный eval-набор для контроля качества после правок;
- собирать исследовательскую сводку по репозиторию перед AI-проверкой;
- при наличии внешнего open source packer-а использовать optional backend `repomix/repopack` для более полной AI-friendly сводки.

## Запуск

```bash
pip install -r requirements.txt
python main.py
```

Для отдельного запуска researcher как CLI:

```bash
python repo_research_runtime.py https://github.com/owner/repo
```

## Команды бота

- `/budget` — показать текущий расход AI-бюджета.
- `/lastdebug` — путь к последнему debug-файлу AI.
- `/clear_spec` — удалить сохраненное для чата ТЗ.

## Как использовать ТЗ

1. Отправь боту документ с ТЗ в формате `pdf`, `docx`, `txt` или `md`.
2. Бот сохранит его для текущего чата.
3. После этого отправь ссылку на репозиторий.
4. В отчете появится блок сравнения проекта с ТЗ.

Можно отправить документ и сразу в подписи приложить ссылку на GitHub.

## AI-провайдеры

Порядок задается переменной `AI_PROVIDER_ORDER`.

Поддерживаются:

- `gemini`
- `openrouter`
- `groq`
- `gigachat`

Если у провайдера нет ключа или он временно недоступен, бот переходит к следующему.

## Пример `.env`

```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
GITHUB_TOKEN=

AI_PROVIDER_ORDER=gemini,openrouter,groq,gigachat
AI_PROVIDER_COOLDOWN_SECONDS=300
AI_BUDGET_STATE_FILE=.cache/ai_budget_state.json
AI_DAILY_TOKEN_LIMIT=2000000
AI_MONTHLY_TOKEN_LIMIT=25000000
AI_DAILY_USD_LIMIT=1.0
AI_MONTHLY_USD_LIMIT=10.0
AI_HARD_STOP_ON_EXHAUST=true

AI_MAX_PROMPT_CHARS=40000
AI_LOW_BUDGET_PROMPT_CHARS=40000
AI_MAX_OUTPUT_TOKENS=5000
AI_LOW_BUDGET_MAX_OUTPUT_TOKENS=5000
AI_REVIEWED_FILES_LIMIT=16
AI_FILE_LIST_LIMIT=80

OPENROUTER_TIMEOUT_SECONDS=180
OPENROUTER_MAX_ATTEMPTS=3
OPENROUTER_RETRY_TOKEN_STEP=1000
OPENROUTER_RETRY_MAX_OUTPUT_TOKENS=5000

REPORT_SHOW_DEBUG_DETAILS=false
REPO_RESEARCH_BACKEND=auto
REPO_RESEARCH_TOOL_TIMEOUT_SECONDS=120

GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash-lite

OPENROUTER_API_KEY=
OPENROUTER_MODEL=openrouter/free

GROQ_API_KEY=
GROQ_MODEL=openai/gpt-oss-20b

GIGACHAT_AUTH_KEY=
GIGACHAT_SCOPE=GIGACHAT_API_PERS
GIGACHAT_MODEL=GigaChat-2-Lite
GIGACHAT_VERIFY_SSL=true
```

`REPO_RESEARCH_BACKEND` поддерживает:

- `auto` — сначала пробует внешний packer, потом падает в builtin research
- `builtin` — только встроенный researcher
- `repomix` / `repopack` — принудительно пробует внешний backend

Если хочешь использовать внешний backend, удобно поставить один из CLI-инструментов:

```bash
npm install -g repomix
```

или запускать через `npx`, если он доступен в системе.

## Регрессионный eval-набор

Для быстрой проверки качества отчетов можно использовать пример набора кейсов:

```bash
python tools/review_eval.py --cases eval_cases.example.json
```

Результат будет сохранен в `.cache/eval_report.json`.

Для отдельной исследовательской сводки без запуска Telegram-бота:

```bash
python repo_research_runtime.py https://github.com/owner/repo --backend auto --output .cache/repo_research.txt
```

В `eval_cases.example.json` можно задавать:

- `repo_url`
- `mode`: `full` или `heuristic`
- `expected_source_contains`
- `ai_probability_min`
- `ai_probability_max`
- `required_issue_markers`
- `banned_issue_markers`
- `banned_signal_markers`
- `required_summary_markers`
- `banned_summary_markers`

## Ограничения

- Анализируются только публичные репозитории.
- Бот не смотрит историю коммитов и pull request.
- Бинарные и слишком большие файлы пропускаются.
- Сравнение с ТЗ сейчас эвристическое: оно помогает найти расхождения, но не заменяет ручную проверку преподавателя.
- Оценка вероятности использования AI не является доказательством авторства.
