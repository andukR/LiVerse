# LiVerse

Минимальный проект для распознавания библейских ссылок из живой русской речи через Vosk и вывода результата в Holyrics.

## Что есть внутри

- `tools/vosk_grammar_probe.py` - основной скрипт.
- `tools/analyze_vosk_probe_logs.py` - разбор логов распознавания.
- `packages/bible_parser_core` - парсер библейских ссылок, общий live-pipeline
  и данные Библии.

## Общий live-pipeline

`packages/bible_parser_core/src/bible_parser_core/live_pipeline.py` - общий
"мозг" LiVerse для desktop-версии и Android-версии. Pipeline здесь означает
цепочку обработки: взять очередной кусок текста от Vosk, помнить несколько
последних кусков, собрать возможные варианты полной ссылки, отфильтровать
старый контекст и вернуть найденную библейскую ссылку.

В этом модуле живут:

- генерация грамматики Vosk;
- буфер последних фраз Vosk;
- сборка кандидатов из нескольких фраз;
- защита от прилипания старой ссылки к новой;
- распознавание ссылок через `bible_parser_core.parser`.

`tools/vosk_grammar_probe.py` оставляет за собой desktop-части: микрофон,
логи, подтверждение оператором, web-экран и Holyrics. Android-приложение
использует тот же `live_pipeline.py` через Chaquopy, а микрофон и экран
оставляет Android-коду.

## Происхождение `rst.json`

Runtime-текст Библии в `packages/bible_parser_core/src/bible_parser_core/data/rst.json`
основан на `bibleonline/rst`, ревизия `2de3062388a2c067bc602399bda7149eec918ceb`,
набор `parsed66`. Подробности и команда воспроизводимой сборки описаны в
[`docs/DATA_PROVENANCE.md`](docs/DATA_PROVENANCE.md).

## Модель Vosk

Репозиторий включает модель:

`models/vosk-model-small-ru-0.22`

## Запуск

```bash
python3 tools/vosk_grammar_probe.py --text "неемия первая глава пятый стих"
```

Для микрофона:

```bash
python3 tools/vosk_grammar_probe.py
```

В обычном режиме консоль показывает приветствие и короткие статусы вроде
`слушаю`, `распознаю`, `найдена ссылка`. Подробный JSON выводится только с
флагом `--debug-console`; путь к JSONL-логу печатается только с
`--print-log-path`.

Для живого богослужения с подтверждением на телефоне:

```bash
python3 tools/vosk_grammar_probe.py --require-approval --slide-output both --open-operator-browser
```

При запуске будет напечатан адрес пульта подтверждения для телефона и адрес
большого web-экрана. Найденная ссылка попадёт в Holyrics только после кнопки
`Принять`. QR-код для телефона откроется отдельным окном через `eog` или
`xdg-open` на Linux, а на Windows - стандартным просмотрщиком изображений;
если это не нужно, добавьте `--no-open-operator-qr`. ASCII QR-код в консоль не
печатается; для старого консольного QR используйте `--print-operator-qr`.

При остановке `Ctrl+C` LiVerse показывает итоговое окно со списком
распознанных за сеанс ссылок и кнопкой `Поделиться в WhatsApp`. Если итоговое
окно не нужно, добавьте `--no-session-summary-popup`.

Если нужен только Holyrics без локального web-экрана:

```bash
python3 tools/vosk_grammar_probe.py --require-approval
```

Если оператор работает на том же компьютере, где запущен Holyrics, можно
использовать локальное всплывающее окно вместо web-пульта:

```bash
python3 tools/vosk_grammar_probe.py --require-approval --approval-ui popup
```

Окно показывает найденную ссылку крупным шрифтом. `Enter` отправляет цитату в
Holyrics, `Esc` или `Space` отклоняют её.

Отдельно запустить только web-экран:

```bash
python3 tools/slide_server.py --host 0.0.0.0 --port 8765
```

Большой экран: `http://127.0.0.1:8765/`

Пульт подтверждения: `http://127.0.0.1:8765/operator`

Чтобы вместе с JSONL-логом сохранить аудио последнего запуска:

```bash
python3 tools/vosk_grammar_probe.py --log-audio
```

Аудио пишется в `audio.wav` рядом с `events.jsonl`.

Если нужен вывод в Holyrics, задайте переменные окружения в `.env`:

```env
HOLYRICS_TOKEN=...
HOLYRICS_HOST=http://localhost
HOLYRICS_PORT=8091
HOLYRICS_THEME=
```

`HOLYRICS_TOKEN` - это token из `Holyrics -> Settings -> API Server -> Manage permissions`.
Это не Web `API_KEY`. Для Local API token передаётся в URL.
`HOLYRICS_PORT=8091` - порт Holyrics API Server по умолчанию. Если в настройках
Holyrics API Server указан другой порт, запишите его в `.env`.
`HOLYRICS_THEME` - запасной параметр для неинтерактивного запуска. При обычном
запуске LiVerse спрашивает тему после выбора режима работы и способа
подтверждения. Если в API token есть разрешение `GetThemes`, LiVerse покажет
список тем Holyrics. Enter или `0` оставляет тему Bible module по умолчанию.
Если `GetThemes` не включён, LiVerse сообщит об этом и продолжит с темой по
умолчанию.

В Holyrics API Server Local должны быть разрешены:

- `GetAPIServerInfo` - стартовая проверка, что Holyrics API Server доступен
- `ShowVerse`
- `SetBibleSettings`
- `GetThemes` - для показа списка тем при запуске

LiVerse также вызывает служебный запрос `GetTokenInfo`, чтобы проверить версию
Holyrics и список разрешений текущего token. Отдельно включать `GetTokenInfo`
в `Manage permissions` обычно не нужно. Эта проверка не заменяет первичную
настройку Holyrics: API Server должен быть включён, token должен быть создан и
записан в `.env`.

LiVerse больше не отправляет собственный текст Библии в Holyrics через
`ShowQuickPresentation` для распознанных библейских ссылок. Вместо этого он:

1. вычисляет Holyrics verse id в формате `BBCCCVVV`;
2. вызывает `SetBibleSettings` с `show_x_verses`: `1` для одного стиха или
   количеством стихов для диапазона;
3. вызывает `ShowVerse` с `{"id": "<verse_id>"}`.

Например, `Иоанн 3:16` превращается в `43003016`.
`Иоанн 3:16-19` отправляется как `SetBibleSettings {"show_x_verses": 4}`,
затем `ShowVerse {"id": "43003016"}`.

## Установка на Windows 10

1. Установите Python 3.10+.
2. Распакуйте проект.
3. Запустите `install-windows.ps1`.
4. Откройте `.env`, вставьте `HOLYRICS_TOKEN` и проверьте `HOLYRICS_PORT`.
5. Запускайте `run-liverse.cmd`.

### Установка Windows 10 с нуля из консоли

Если браузер на старом компьютере сильно тормозит, можно поставить Git,
скачать последнюю версию LiVerse и создать виртуальное окружение одной
командой PowerShell:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "iwr https://raw.githubusercontent.com/andukR/LiVerse/main/bootstrap-windows.ps1 -UseBasicParsing | iex"
```

Скрипт делает следующее:

1. ищет `git`; если Git не найден, пробует поставить Git for Windows через
   `winget`, затем через официальный релиз Git for Windows на GitHub;
2. скачивает или обновляет репозиторий `https://github.com/andukR/LiVerse.git`
   из ветки `main`;
3. проверяет Python 3.10+;
4. создаёт `.venv`;
5. устанавливает зависимости и сам LiVerse.

По умолчанию проект будет лежать в `%USERPROFILE%\LiVerse`. Запуск:

```powershell
cd $HOME\LiVerse
.\run-liverse.cmd
```

Если LiVerse уже скачан через Git, эта же команда обновит его до последней
версии ветки `main`.

Если запустить `run-liverse.cmd` без параметров, он включает тот же рабочий
режим, что и `make liverse`: подтверждение через телефон, вывод в Holyrics и
QR-код в отдельном окне. Если передать свои параметры, они используются вместо
настроек по умолчанию.

Для режима со всплывающим окном из PowerShell или Command Prompt:

```powershell
.\run-liverse.cmd --require-approval --approval-ui popup
```

## Установка на Linux

1. Установите Python 3.10+ и `make`.
2. Если `python3 -m venv` не работает, поставьте пакет `python3-venv` или аналогичный для вашего дистрибутива.
3. Запустите:

```bash
./install-linux.sh
make liverse
```

`make liverse` запускает режим богослужения с подтверждением через телефон,
выводом в Holyrics и QR-кодом в отдельном окне. При запуске LiVerse спросит:
работать полностью автоматически или с подтверждением оператора. `Enter` -
подтверждение оператора, `Space` - полностью автоматический режим. Если выбрано
подтверждение, второй вопрос выбирает интерфейс: `Enter` - web-пульт,
`Space` - всплывающее окно.

Дополнительные параметры можно передать через `ARGS`, например:

Запуск с подтверждением:

```bash
make liverse ARGS="--require-approval --slide-output both --open-operator-browser"
```

Если нужен полностью ручной набор параметров без настроек по умолчанию:

```bash
make liverse LIVERSE_ARGS= ARGS="--text 'Иоанн 3:16' --slide-output none"
```

После установки можно запускать и напрямую:

```bash
liverse
```
