# GitHub Review Bot for Telegram

Телеграм-бот для проверки учебных GitHub-репозиториев.

Бот умеет:

- принимать ссылку на публичный GitHub-репозиторий;
- скачивать проект через `github.com` и `codeload.github.com`;
- делать локальный эвристический разбор;
- отдавать проверку бесплатным AI-провайдерам по fallback-цепочке;
- строго разделять режимы:
  AI доступен -> отчет только от AI;
  AI недоступен -> отчет только локальный;
- считать локальный бюджет токенов и останавливать AI-проверки при исчерпании лимита;
- принимать ТЗ в формате `pdf`, `docx`, `txt`, `md` и сравнивать требования с кодом репозитория.

## Запуск

```bash
pip install -r requirements.txt
python main.py
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
AI_MAX_PROMPT_CHARS=14000
AI_LOW_BUDGET_PROMPT_CHARS=8000
AI_MAX_OUTPUT_TOKENS=700
AI_LOW_BUDGET_MAX_OUTPUT_TOKENS=350

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

## Ограничения

- Анализируются только публичные репозитории.
- Бот не смотрит историю коммитов и pull request.
- Бинарные и слишком большие файлы пропускаются.
- Сравнение с ТЗ сейчас эвристическое: оно помогает найти расхождения, но не заменяет ручную проверку преподавателя.
- Оценка вероятности использования AI не является доказательством авторства.
