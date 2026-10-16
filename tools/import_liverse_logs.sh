#!/usr/bin/env bash
set -euo pipefail

# Usage: tools/import_liverse_logs.sh [logs_directory]
# Import every file from Logs and every ZIP member, without a filename list.
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project_root=$(cd "$script_dir/.." && pwd)
archive_dir=${1:-"/media/${USER:-$(id -un)}/ANDREY/Logs"}
destination="$project_root/.cache/liverse/vosk_probe"

for directory in "$archive_dir" "$project_root"; do
    if [[ ! -d "$directory" ]]; then
        printf 'Не найдена папка: %s\n' "$directory" >&2
        exit 2
    fi
done
for command in unzip find cmp mktemp cp; do
    command -v "$command" >/dev/null 2>&1 || {
        printf 'Не найдена необходимая команда: %s\n' "$command" >&2
        exit 2
    }
done

staging=$(mktemp -d)
trap 'rm -rf -- "$staging"' EXIT
mkdir -p "$destination"
imported=0
updated=0
skipped=0
conflicts=0
sources=0
failed_archives=0

# Missing files can be filled in; differing existing content must be preserved.
# Extra local files (such as annotations) are allowed and remain untouched.
same_session() {
    local source=$1 target=$2 file relative other
    needs_update=false
    [[ ! -L "$target" ]] || return 1
    if [[ -f "$source" ]]; then
        [[ -f "$target" ]] && cmp -s -- "$source" "$target"
        return
    fi
    [[ -d "$target" ]] || return 1
    while IFS= read -r -d '' file; do
        relative=${file#"$source/"}
        other="$target/$relative"
        [[ ! -L "$other" ]] || return 1
        if [[ ! -e "$other" ]]; then
            needs_update=true
        elif [[ -d "$file" ]]; then
            [[ -d "$other" ]] || return 1
        else
            [[ -f "$other" ]] && cmp -s -- "$file" "$other" || return 1
        fi
    done < <(find "$source" -mindepth 1 -print0)
    return 0
}

import_entry() {
    local source=$1 name target number
    name=$(basename "$source")
    target="$destination/$name"
    if [[ -e "$target" || -L "$target" ]]; then
        if ! same_session "$source" "$target"; then
            target="$destination/${name}_church"
            number=2
            while [[ -e "$target" || -L "$target" ]]; do
                same_session "$source" "$target" && break
                target="$destination/${name}_church_$number"
                ((number += 1))
            done
            ((conflicts += 1))
        fi
        if [[ -e "$target" ]]; then
            if [[ "$needs_update" == true ]]; then
                cp -an -- "$source/." "$target/"
                printf '  Дополнен: %s\n' "$(basename "$target")"
                ((updated += 1))
            else
                printf '  Уже импортирован: %s\n' "$(basename "$target")"
                ((skipped += 1))
            fi
            return
        fi
    fi
    cp -a -- "$source" "$target"
    printf '  Импортирован: %s\n' "$(basename "$target")"
    ((imported += 1))
}

reject_special_files() {
    local invalid
    invalid=$(find "$1" ! -type d ! -type f -print -quit)
    if [[ -n "$invalid" ]]; then
        printf 'Ссылки и специальные файлы не импортируются: %s\n' "$invalid" >&2
        exit 4
    fi
}

reject_special_files "$archive_dir"
while IFS= read -r -d '' source; do
    ((sources += 1))
    if [[ -f "$source" && "$source" == *.[zZ][iI][pP] ]]; then
        printf 'Проверяю %s...\n' "$(basename "$source")"
        if ! unzip -tqq "$source"; then
            printf '  ОШИБКА: архив повреждён, пропускаю: %s\n' "$(basename "$source")" >&2
            ((failed_archives += 1))
            continue
        fi
        while IFS= read -r entry; do
            case "$entry" in
                /*|[a-zA-Z]:*|*\\*)
                    printf 'Небезопасный путь внутри архива: %s\n' "$entry" >&2
                    exit 4
                    ;;
            esac
            case "/$entry/" in
                *'/../'*|*'//'*|*'/./'*)
                    printf 'Небезопасный путь внутри архива: %s\n' "$entry" >&2
                    exit 4
                    ;;
            esac
        done < <(unzip -Z1 "$source")
        # Reject archived symlinks before extracting anything.
        while read -r permissions _rest; do
            if [[ "$permissions" == l* ]]; then
                printf 'Архив содержит символическую ссылку: %s\n' "$source" >&2
                exit 4
            fi
        done < <(unzip -Z -l "$source")
        archive_stage=$(mktemp -d "$staging/archive.XXXXXX")
        unzip -qq "$source" -d "$archive_stage"
        reject_special_files "$archive_stage"
        while IFS= read -r -d '' entry; do
            import_entry "$entry"
        done < <(find "$archive_stage" -mindepth 1 -maxdepth 1 -print0)
    else
        import_entry "$source"
    fi
done < <(find "$archive_dir" -mindepth 1 -maxdepth 1 -print0)

if (( sources == 0 )); then
    printf 'Папка журналов пуста: %s\n' "$archive_dir" >&2
    exit 3
fi
printf '\nГотово. Каталог журналов: %s\n' "$destination"
printf 'Импортировано: %d; дополнено: %d; уже было: %d; совпадений имён с разным содержимым: %d.\n' \
    "$imported" "$updated" "$skipped" "$conflicts"
if (( failed_archives > 0 )); then
    printf 'Пропущено повреждённых архивов: %d. Исправьте или замените их и запустите импортёр повторно.\n' \
        "$failed_archives" >&2
    exit 5
fi
