# Происхождение данных

## Текст Библии

Runtime-файл:

```text
packages/bible_parser_core/src/bible_parser_core/data/rst.json
```

основан на:

```text
https://github.com/bibleonline/rst
commit 2de3062388a2c067bc602399bda7149eec918ceb
parsed66/
```

Upstream-проект указывает для текста статус `Public Domain`, то есть
общественное достояние. Лицензия исходного кода LiVerse и статус данных
`bibleonline/rst` являются разными вещами.

Файл `rst.json` является форматированной сборкой для runtime LiVerse. Он не
является ручной копией неизвестного JSON-файла. Записи Псалтири с номером
стиха `0` не включаются, потому что runtime работает со ссылками вида
`книга глава:стих`.

Ожидаемый результат сборки из полного LiVerse-проекта:

- 66 книг;
- 1189 глав;
- 31 162 адресуемых стиха;
- последовательная нумерация глав и стихов без пропусков.

Wikipedia или другие справочные страницы могут использоваться только как
независимая проверка общей структуры. Источник текста и нумерации стихов -
`bibleonline/rst`, а не Wikipedia.

## Ручные исправления

Итоговый `rst.json` может отличаться от upstream-данных, потому что поверх
источника применяются документированные исправления из:

```text
packages/bible_parser_core/src/bible_parser_core/data/rst_overrides.json
```

Исправления должны фиксировать исходный текст, исправленный текст, причину и
способ проверки. Нельзя переносить формулировки из современного защищённого
авторским правом перевода без разрешения.

Итоговый `rst.json` следует описывать как текст, основанный на `bibleonline/rst`
и содержащий документированные редакционные исправления LiVerse.

## Воспроизводимая сборка

В полном LiVerse-проекте сборка выполняется так:

```bash
git clone https://github.com/bibleonline/rst.git \
  external_sources/bibleonline-rst
git -C external_sources/bibleonline-rst checkout \
  2de3062388a2c067bc602399bda7149eec918ceb

.venv/bin/python \
  packages/bible_parser_core/tools/build_rst_from_bibleonline.py
```

Проверка без изменения файла в полном LiVerse-проекте:

```bash
.venv/bin/python \
  packages/bible_parser_core/tools/build_rst_from_bibleonline.py --check
```

В этом standalone Vosk-репозитории сборщик пока не перенесён. До его переноса
`rst.json` и `rst_overrides.json` должны рассматриваться как данные,
скопированные из полного LiVerse-проекта с описанным выше происхождением.

## Альтернативная сборка из CrossWire SWORD

Более проверяемый источник для Android/LiVerse:

```text
packages/bible_parser_core/src/bible_parser_core/data/sword_russinodal.json
```

Файл собирается из модуля CrossWire SWORD:

```text
Module=RusSynodal
Version=1.9.1
SwordVersionDate=2020-12-21
DistributionLicense=Public Domain
TextSource=http://www.rbo.ru/reading/articles/show/?4&start=0 or http://www.patriarchia.ru/bible/mf
DownloadUrl=https://www.crosswire.org/ftpmirror/pub/sword/packages/rawzip/RusSynodal.zip
ArchiveSha256=b802570e1783c326552b9e810786efe3df4efcd615f28ccf3a86bae27dbc5022
```

Сборка:

```bash
.venv/bin/python tools/build_sword_russynodal.py
```

Проверка без изменения файла:

```bash
.venv/bin/python tools/build_sword_russynodal.py --check
```

Скрипт проверяет зафиксированный SHA256 архива и ключевые поля SWORD-конфига:
версию, дату, лицензию, источник текста, кодировку и versification. Если
CrossWire обновит модуль, скрипт остановится с ошибкой, пока новая версия не
будет вручную просмотрена и зафиксирована.

В отличие от старого `rst.json`, SWORD-модуль использует
`Versification=Synodal`. Поэтому ожидаемая структура:

- 66 книг;
- 1192 главы;
- 31 350 стихов.

Это не совпадает с прежними 1189 главами и 31 162 стихами, потому что
SWORD-разметка Synodal включает дополнительные главы/стихи внутри некоторых
книг. Скрипт не подгоняет данные под старую структуру, а сохраняет структуру
проверенного источника.

Известная особенность чтения модуля через `pysword`: `Пс. 114:9` возвращается
пустым, потому что его текст присоединён к `Пс. 114:8`. Сборщик явно делит
эти два стиха по уже имеющемуся в SWORD тексту:

- `Пс. 114:8`: `Ты избавил душу мою от смерти, очи мои от слез и ноги мои от преткновения.`
- `Пс. 114:9`: `Буду ходить пред лицем Господним на земле живых.`

Это исправление меняет только границу стиха, а не текст.
