# LiVerse: принципы разработки для Codex

## Назначение

Этот документ задаёт правила разработки LiVerse, чтобы не повторять проблемы, обнаруженные при подготовке Windows-дистрибутива: расхождение Linux/Windows-сред, скрытые внешние зависимости, незаявленные runtime-зависимости, старые библиотеки, несовместимые с современными версиями Python, случайные platform-specific fixes, неясный состав build-артефактов и тесты, которые проходят только на одной машине.

Главная цель: **LiVerse должен быть воспроизводимым, переносимым и предсказуемым продуктом, а не программой, которая работает только в конкретном исторически сложившемся окружении разработчика.**

## 1. Один репозиторий — один источник истины

- Вся логика, необходимые словари, конфигурационные шаблоны и build-скрипты должны находиться в репозитории LiVerse либо быть явно объявленными build assets.
- Нельзя молча подхватывать файлы из соседних проектов, пользовательских каталогов или случайных путей.
- Если внешний ресурс действительно нужен, его путь, версия и способ получения должны быть явно задокументированы.
- Поведение программы не должно меняться только потому, что рядом с репозиторием случайно лежит другой проект.

## 2. Не полагаться на «у меня уже установлено»

Каждая реально используемая Python-библиотека должна быть объявлена прямой зависимостью проекта.

Если код делает `import numpy`, NumPy должен быть объявлен в dependency metadata, даже если он обычно устанавливается транзитивно.

Нельзя рассчитывать на случайно установленный пакет, старое виртуальное окружение, системный Python или транзитивную зависимость.

## 3. Фиксировать проверенные версии

Для production/build-среды использовать точные проверенные версии Python и критических библиотек.

Для первого Windows-дистрибутива эталон:

```text
Python 3.12.10
```

Критический legacy NLP stack нельзя модернизировать без отдельной проверки:

```text
words2numsrus==0.1.0
natasha==0.10.0
yargy==0.12.0
pymorphy2==0.8
pymorphy2-dicts==2.4.393442.3710985
DAWG-Python==0.7.2
setuptools==83.0.0
```

Известное требуемое поведение:

```text
третьему -> 3
четвёртой -> 4
двадцать второй -> 22
сто тридцать третьей -> 133
второго-третьего -> 2-3
```

Правило: **сначала regression tests, потом обновление зависимости**.

## 4. Отделять metadata-проблемы старых пакетов от реальных runtime-проблем

Старые библиотеки могут содержать устаревшие dependency metadata.

Пример: `yargy==0.12.0` требует `backports.functools-lru-cache`, хотя на Python 3.12 используется стандартный `functools.lru_cache`.

Codex не должен автоматически устанавливать древние compatibility-пакеты только ради зелёного `pip check`. Нужно проверять реальный import path, runtime behavior, regression tests и packaging behavior.

## 5. Поддерживать Linux и Windows как две реальные целевые платформы

Использовать:

```text
Debian host              -> development/source
Windows 10 VM guest      -> clean Windows reference/test/build environment
Real church Windows PC   -> dirty real-world installation
```

Если ошибка существует только на одном Windows-компьютере, сначала исследовать окружение этой машины, а не добавлять глобальный Windows-workaround.

## 6. Не смешивать fix платформы с fix конкретной машины

Перед Windows-specific patch определить:

- воспроизводится ли проблема на чистой Windows VM;
- это ошибка LiVerse или Holyrics/config;
- это остатки старой установки;
- это PATH/venv/Python/certificate issue;
- это workaround конкретного ПК.

Platform fix должен решать воспроизводимую платформенную проблему.

## 7. Все platform fixes должны иметь test или smoke test

Для Windows минимум:

```text
LiVerseEngine.exe --version
LiVerseEngine.exe --help
LiVerseEngine.exe --list-audio-devices
text reference parsing
Bible data loading
ASR model loading
live microphone recognition
Holyrics API
tray icon
GUI -> engine launch
clean process shutdown
```

## 8. Тесты должны быть автономными

Тесты не должны проходить только потому, что на машине разработчика существует внешний файл или соседний проект.

Перед релизом проверять проект без соседних проектов, developer-only data и пользовательских cache.

## 9. Красный test suite нельзя считать нормой без явного объяснения

Перед release/build все обязательные regression tests должны быть зелёными.

Codex должен сообщать:

```text
tests run
tests passed
tests failed
tests skipped
exit code
```

Не считать отсутствие сообщения об ошибке доказательством успеха.

## 10. Не менять бизнес-логику во время packaging-задачи

Если packaging выявил регрессию:

1. остановить packaging;
2. воспроизвести ошибку в source-среде;
3. найти причину;
4. сделать минимальный отдельный fix;
5. прогнать полный regression suite;
6. сделать отдельный commit;
7. вернуться к packaging.

## 11. Маленькие изменения — отдельные коммиты

Не смешивать updater, SSL, UI, dependency changes, parser changes и installer logic в одном commit, если они логически независимы.

## 12. Не допускать длительного расхождения Linux и Windows веток

После проверки Windows-specific изменений:

1. review;
2. merge в основной `main`;
3. Linux tests;
4. Windows tests.

## 13. Git working tree перед release должен быть clean

Перед release snapshot:

```bash
git status --short
```

должен быть пустым.

Build metadata:

```text
commit=<full hash>
branch=<branch>
dirty=false
timestamp=<timestamp>
```

## 14. Build assets отделять от Git source

Большие бинарные runtime-ресурсы могут оставаться build assets.

Пример:

```text
bible_index/bible_index.db
```

Build pipeline обязан проверить наличие, размер, SHA-256 и включить ресурс в snapshot/installer.

То же относится к ASR-моделям.

## 15. Для каждого build asset проверять hash

Передача:

```text
Debian source -> ISO snapshot -> Windows build workspace
```

должна подтверждаться одинаковым SHA-256.

## 16. Не копировать developer environment в Windows build

Не переносить:

```text
.venv
__pycache__
.pytest_cache
.cache
logs
.env
build
dist
temporary files
```

## 17. Использовать отдельный Windows build workspace

```text
C:\Projects\live_verse_vosk
```

— контрольная рабочая установка.

```text
C:\Build\LiVerse
```

— build workspace.

## 18. Синхронизация host -> guest должна быть воспроизводимой

Использовать автоматизированный snapshot с include/exclude policy, manifest, SHA-256 и `BUILD_SOURCE_INFO.txt`.

Синхронизация должна быть односторонней и по умолчанию не destructive.

## 19. Build environment должен быть отдельным

```text
C:\Projects\live_verse_vosk\.venv
```

— рабочее runtime-окружение.

```text
C:\Build\LiVerse\.venv-build
```

— build environment.

Build tooling не должен загрязнять runtime baseline.

## 20. Сначала воспроизвести рабочее окружение, потом собирать

Перед PyInstaller:

1. снять package baseline с реально работающей Windows установки;
2. воспроизвести его offline в `.venv-build`;
3. прогнать tests;
4. убедиться, что приложение запускается из Python;
5. только потом PyInstaller.

## 21. PyInstaller сначала onedir, потом installer

Для LiVerse сначала использовать `--onedir`, потому что так проще диагностировать modules, data и DLL.

Пользователь всё равно получит один installer через Inno Setup.

## 22. Python packages и data files — разные вещи

Явно проверять inclusion:

```text
rst.json
risk_model.json
rst_overrides.json
bible_index.db
ASR model
icons
slide_display assets
```

## 23. Не добавлять hidden imports «на всякий случай»

Добавлять hidden import только когда доказано, что модуль импортируется динамически и PyInstaller его не видит.

## 24. Каждый слой PyInstaller проверять отдельно

Порядок:

```text
1. EXE --version
2. EXE --help
3. parser text reference
4. Bible data loading
5. audio device listing
6. bible_index search
7. ASR model files
8. ASR model loading
9. microphone recognition
10. Holyrics output
11. GUI
12. tray
13. GUI -> engine
14. installer
```

## 25. Не путать application logic failure с packaging failure

Если EXE ведёт себя странно:

1. запустить тот же input через `.venv-build\python.exe`;
2. сравнить результат.

Если Python и EXE дают одинаковый результат, проблема не в PyInstaller.

## 26. Пользовательская конфигурация не должна находиться внутри installation directory

В будущем разделять immutable program files и `%LOCALAPPDATA%\LiVerse` для config/logs/state/cache.

## 27. `.env` с token не включать в installer

Holyrics token и другие secrets:

- не хранить в Git;
- не включать в source snapshot;
- не включать в installer.

## 28. Binary updater не должен использовать Git/pip

Для пользователя:

```text
LiVerse 1.1.0
-> скачать LiVerse-Setup-1.1.1.exe
-> установить
-> сохранить config/state
```

Пользователь не должен иметь Git, Python, pip, venv или PowerShell scripts для обновления.

## 29. Legacy tooling явно обозначать как legacy

Старые scripts можно сохранять отдельно, но нельзя смешивать их с актуальным installer workflow.

## 30. Не модернизировать legacy stack во время release work

Стратегия:

```text
release first
modernize later
```

## 31. Для критической заброшенной библиотеки рассмотреть fork/vendor

Если `words2numsrus` продолжает тянуть устаревший NLP stack, после стабилизации release рассмотреть внутренний fork или vendor минимально нужного функционала — только при наличии regression tests.

## 32. Windows VM должна оставаться clean reference environment

Не превращать clean VM во второй development workstation.

Использовать её для build, installer tests, runtime tests и clean-install tests.

## 33. Реальный церковный ПК тестировать отдельно

Если clean VM работает, а church PC нет — сначала исследовать разницу окружений.

## 34. Release candidate проверять без исходников и venv

Проверить, что distribution работает независимо от:

```text
C:\Projects\live_verse_vosk
C:\Build\LiVerse\.venv-build
Python installation
```

## 35. Сборка должна стать автоматизированной

После первого рабочего installer создать повторяемый pipeline.

Желаемая команда:

```text
build-windows-release <version>
```

Pipeline должен автоматически:

1. проверить clean Git;
2. прогнать tests;
3. проверить build assets/hash;
4. создать source snapshot;
5. синхронизировать Windows build workspace;
6. создать/reuse pinned build environment;
7. собрать LiVerseEngine;
8. собрать LiVerse GUI;
9. выполнить smoke tests;
10. собрать installer;
11. вычислить SHA-256;
12. выдать release report.

# Обязательная стратегия Codex при изменениях

Перед изменением:

1. прочитать соответствующий код;
2. воспроизвести проблему;
3. определить platform-specific или general;
4. проверить Git status;
5. сформулировать минимальный patch.

После изменения:

1. связанные tests;
2. полный regression suite;
3. `git diff --check`;
4. показать `git diff --stat`;
5. объяснить изменение;
6. не push без явного разрешения, если задача этого не требует.

# Что Codex не должен делать автоматически

Без явного обоснования нельзя:

- обновлять Python;
- обновлять legacy NLP stack;
- заменять dependency современной;
- удалять старый рабочий код;
- делать massive refactor во время packaging;
- добавлять случайные hidden imports;
- копировать `.venv`;
- включать `.env`;
- делать destructive sync;
- считать church-PC workaround общим Windows fix;
- считать тесты успешными без exit code;
- игнорировать failing regression tests;
- подхватывать внешние данные из соседних проектов;
- строить release из dirty working tree.

# Главный принцип

> **Воспроизводимость важнее новизны.**

Сначала LiVerse должен одинаково и предсказуемо работать из clean Git, на заявленной версии Python, с зафиксированными dependencies, с явно перечисленными build assets, на Linux и Windows, без скрытых файлов и случайного окружения.

И только после этого модернизировать зависимости, архитектуру и Python.

# Короткий release checklist

```text
[ ] git status clean
[ ] commit hash зафиксирован
[ ] tests Linux OK
[ ] tests Windows OK
[ ] no hidden external project dependency
[ ] Python build version pinned
[ ] dependency baseline pinned
[ ] bible_index.db present + SHA-256 verified
[ ] ASR model present + verified
[ ] LiVerseEngine.exe --version OK
[ ] parser smoke test OK
[ ] audio devices OK
[ ] ASR live microphone test OK
[ ] Holyrics test OK
[ ] GUI test OK
[ ] tray test OK
[ ] installer clean-install test OK
[ ] installer upgrade test OK
[ ] release SHA-256 generated
```
