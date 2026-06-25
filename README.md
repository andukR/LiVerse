# Live Verse Vosk

Минимальный проект для распознавания библейских ссылок из живой русской речи через Vosk и вывода результата в Holyrics.

## Что есть внутри

- `tools/vosk_grammar_probe.py` - основной скрипт.
- `tools/analyze_vosk_probe_logs.py` - разбор логов распознавания.
- `packages/bible_parser_core` - парсер библейских ссылок и данные `rst.json`.

## Что нужно отдельно

Модель Vosk не лежит в репозитории. Положите её в:

`models/vosk-model-small-ru-0.22`

## Запуск

```bash
python3 tools/vosk_grammar_probe.py --text "неемия первая глава пятый стих"
```

Для микрофона:

```bash
python3 tools/vosk_grammar_probe.py
```

Если нужен вывод в Holyrics, задайте переменные окружения:

- `HOLYRICS_URL`
- `HOLYRICS_TOKEN`
- `HOLYRICS_ACTION`
- `HOLYRICS_THEME`

## Установка на Windows 10

1. Установите Python 3.10+.
2. Распакуйте проект.
3. Положите модель Vosk в `models\vosk-model-small-ru-0.22`.
4. Запустите `install-windows.ps1`.
5. Запускайте `run-live-verse-vosk.cmd`.

## Установка на Linux

1. Установите Python 3.10+ и `make`.
2. Если `python3 -m venv` не работает, поставьте пакет `python3-venv` или аналогичный для вашего дистрибутива.
3. Положите модель Vosk в `models/vosk-model-small-ru-0.22`.
4. Запустите:

```bash
./install-linux.sh
make liverse
```

После установки можно запускать и напрямую:

```bash
liverse
```
