#!/usr/bin/env bash
# Replay saved sermons from the Rodnik church YouTube channel through LiVerse.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Complete locally downloaded Rodnik sermon set.  The older whisper_runs
# directory contains only six converted recordings and is retained solely as
# a source of subtitles/transcripts.
AUDIO_ROOT="$PROJECT_ROOT/.cache/liverse/replay_audio"
ASR_ENGINE="sherpa-0.54"
CITATION_DETECTION_MODE="hybrid_confirm"
RUN_REPLAY=false
CONTROL_WINDOWS=0
CONTROL_ONLY=false
PYTHON="$PROJECT_ROOT/.venv/bin/python"
BATCH_ROOT="$PROJECT_ROOT/.cache/liverse/rodnik_replay_batches"
LATEST_BATCH_FILE="$BATCH_ROOT/latest_logs_dir"
TIMED_SUBTITLE_ROOTS=(
    "$PROJECT_ROOT/../bible_parser_cli/transcripts"
    "$PROJECT_ROOT/../bible_parser_cli/.cache/whisper_runs"
)

usage() {
    cat <<'EOF'
Использование:
  tools/replay_rodnik_sermons.sh next
  tools/replay_rodnik_sermons.sh batch AUDIO_1 [AUDIO_2] [AUDIO_3]
  tools/replay_rodnik_sermons.sh audit AUDIO_1 [AUDIO_2] [AUDIO_3]
  tools/replay_rodnik_sermons.sh review
  tools/replay_rodnik_sermons.sh review-slides
  tools/replay_rodnik_sermons.sh [--run] [--engine sherpa-0.54|vosk-0.22]

next                  Автоматически выбрать до трёх ещё не обработанных
                      проповедей и запустить для них всю эмуляцию.
batch AUDIO_1 [AUDIO_2] [AUDIO_3]
                      Для одной-трёх выбранных записей: подобрать окна по субтитрам,
                      нарезать WAV и запустить Sherpa 0.54 в hybrid_confirm.
                      Исходные записи не изменяются.
audit AUDIO_1 [AUDIO_2] [AUDIO_3]
                      Отдельно проверить обычную речь на ложные срабатывания.
review                Открыть аннотатор только для последней успешной пачки.
review-slides         Разметить решения умного перелистывателя из этой пачки.

Без --run скрипт только покажет найденные записи и их длительность.
--run                 Запустить эмуляцию живой проповеди.
--engine NAME         Движок распознавания; по умолчанию sherpa-0.54.

Обычная команда next не добавляет контрольные окна. Для отдельной проверки
ложных срабатываний используйте audit с путями к одной-трём записям.
EOF
}

run_batch() {
    if (($# < 1 || $# > 3)); then
        echo "Для batch нужен от одного до трёх WAV-файлов проповедей." >&2
        usage >&2
        return 2
    fi
    if [[ ! -x "$PYTHON" ]]; then
        echo "Не найдено виртуальное окружение Python: $PYTHON" >&2
        return 1
    fi

    local audio_file
    for audio_file in "$@"; do
        if [[ ! -f "$audio_file" ]]; then
            echo "Не найден аудиофайл: $audio_file" >&2
            return 1
        fi
    done

    local batch_id batch_dir plans_dir audio_dir logs_dir
    batch_id="$(date +%Y%m%d_%H%M%S)"
    batch_dir="$BATCH_ROOT/$batch_id"
    plans_dir="$batch_dir/plans"
    audio_dir="$batch_dir/audio"
    logs_dir="$batch_dir/logs"
    mkdir -p "$plans_dir"

    local plan_command=(
        "$PYTHON" tools/replay_audio_files.py
        --plan-subtitle-windows
        --window-plan-dir "$plans_dir"
        --control-windows "$CONTROL_WINDOWS"
    )
    if [[ "$CONTROL_ONLY" == true ]]; then
        plan_command+=(--control-only)
    fi
    for audio_file in "$@"; do
        plan_command+=(--audio "$audio_file")
    done
    "${plan_command[@]}"

    shopt -s nullglob
    local plans=("$plans_dir"/*.json)
    shopt -u nullglob
    if ((${#plans[@]} != $#)); then
        echo "Ожидались $# разных плана окон, создано: ${#plans[@]}. Проверьте video_id в путях аудио." >&2
        return 1
    fi

    local extract_args=()
    local replay_args=()
    local plan
    for plan in "${plans[@]}"; do
        extract_args+=(--extract-window-plan "$plan")
        replay_args+=(--replay-window-plan "$plan")
    done

    echo "Пачка: $batch_id"
    echo "Сначала будет показан объём нарезки, затем начнётся нарезка и эмуляция."
    "$PYTHON" tools/replay_audio_files.py "${extract_args[@]}" --window-audio-dir "$audio_dir"
    "$PYTHON" tools/replay_audio_files.py "${extract_args[@]}" --window-audio-dir "$audio_dir" --run
    "$PYTHON" tools/replay_audio_files.py \
        "${replay_args[@]}" \
        --window-audio-dir "$audio_dir" \
        --log-dir "$logs_dir" \
        --asr-engine "$ASR_ENGINE" \
        --citation-detection-mode "$CITATION_DETECTION_MODE" \
        --long-range-slide-mode one_verse \
        --include-processed \
        --run

    if [[ "$CONTROL_ONLY" != true ]]; then
        local processed_file="$batch_dir/processed_audio.txt"
        : > "$processed_file"
        for audio_file in "$@"; do
            realpath -e "$audio_file" >> "$processed_file"
        done
    fi
    printf '%s\n' "$logs_dir" > "$LATEST_BATCH_FILE"
    echo ""
    echo "Эмуляция завершена. Для разметки этой пачки выполните:"
    echo "  tools/replay_rodnik_sermons.sh review"
}

run_next_batch() {
    if [[ ! -d "$AUDIO_ROOT" ]]; then
        echo "Не найдена папка с записями Родника: $AUDIO_ROOT" >&2
        return 1
    fi

    shopt -s nullglob
    local available=("$AUDIO_ROOT"/Воскресное*.webm)
    shopt -u nullglob
    if ((${#available[@]} == 0)); then
        echo "Не найдены загруженные проповеди «Воскресное богослужение» в: $AUDIO_ROOT" >&2
        return 1
    fi

    declare -A processed=()
    declare -A timed_subtitles=()
    local marker_file processed_path processed_id subtitle_path subtitle_id
    for subtitle_root in "${TIMED_SUBTITLE_ROOTS[@]}"; do
        [[ -d "$subtitle_root" ]] || continue
        while IFS= read -r -d '' subtitle_path; do
            subtitle_id="$(youtube_id_from_path "$subtitle_path")"
            [[ -n "$subtitle_id" ]] && timed_subtitles["$subtitle_id"]=1
        done < <(find "$subtitle_root" -type f \( -iname '*.srt' -o -iname '*.vtt' \) -print0)
    done
    if [[ -d "$BATCH_ROOT" ]]; then
        while IFS= read -r -d '' marker_file; do
            while IFS= read -r processed_path || [[ -n "$processed_path" ]]; do
                if [[ -n "$processed_path" ]]; then
                    processed["$processed_path"]=1
                    processed_id="$(youtube_id_from_path "$processed_path")"
                    [[ -n "$processed_id" ]] && processed["id:$processed_id"]=1
                fi
            done < "$marker_file"
        done < <(find "$BATCH_ROOT" -type f -name processed_audio.txt -print0)
    fi

    local selected=()
    local completed_count=0
    local unavailable_subtitles=0
    local audio_file canonical_path video_id
    for audio_file in "${available[@]}"; do
        canonical_path="$(realpath -e "$audio_file")"
        video_id="$(youtube_id_from_path "$canonical_path")"
        if [[ -z "$video_id" || -z "${timed_subtitles[$video_id]+present}" ]]; then
            ((unavailable_subtitles += 1))
            continue
        fi
        if [[ -n "${processed[$canonical_path]+present}" || -n "${processed[id:$video_id]+present}" ]]; then
            ((completed_count += 1))
        elif ((${#selected[@]} < 3)); then
            selected+=("$audio_file")
        fi
    done

    if ((${#selected[@]} == 0)); then
        echo "Нет новых записей с субтитрами, содержащими таймкоды: обработано $completed_count, без таймкодов $unavailable_subtitles, всего ${#available[@]}."
        return 0
    fi
    echo "Всего записей: ${#available[@]}; без субтитров с таймкодами: $unavailable_subtitles; ранее обработано: $completed_count; выбрано сейчас: ${#selected[@]}."
    printf '  - %s\n' "${selected[@]}"
    run_batch "${selected[@]}"
}

youtube_id_from_path() {
    local path="$1" base parent
    base="$(basename "$path")"
    if [[ "$base" =~ ([A-Za-z0-9_-]{11})\.(webm|wav|mp3|m4a|opus|ogg|flac|srt|vtt|txt)$ ]]; then
        printf '%s\n' "${BASH_REMATCH[1]}"
        return 0
    fi
    parent="$(basename "$(dirname "$(dirname "$path")")")"
    if [[ "$parent" =~ ^[A-Za-z0-9_-]{11}$ ]]; then
        printf '%s\n' "$parent"
    fi
}

review_latest_batch() {
    if [[ ! -s "$LATEST_BATCH_FILE" ]]; then
        echo "Нет последней успешной пачки. Сначала выполните batch для трёх записей." >&2
        return 1
    fi
    local logs_dir
    logs_dir="$(<"$LATEST_BATCH_FILE")"
    if [[ ! -d "$logs_dir" ]]; then
        echo "Папка логов последней пачки не найдена: $logs_dir" >&2
        return 1
    fi
    exec "$PYTHON" tools/review_trigger_cases.py --runs-dir "$logs_dir" --all-unreviewed
}

review_latest_smart_slides() {
    if [[ ! -s "$LATEST_BATCH_FILE" ]]; then
        echo "Нет последней успешной пачки. Сначала выполните next или batch." >&2
        return 1
    fi
    local logs_dir
    logs_dir="$(<"$LATEST_BATCH_FILE")"
    if [[ ! -d "$logs_dir" ]]; then
        echo "Папка логов последней пачки не найдена: $logs_dir" >&2
        return 1
    fi
    exec "$PYTHON" tools/review_trigger_cases.py --smart-slides --runs-dir "$logs_dir"
}

case "${1:-}" in
    next)
        if (($# != 1)); then
            echo "У next нет дополнительных параметров." >&2
            exit 2
        fi
        cd "$PROJECT_ROOT"
        run_next_batch
        exit $?
        ;;
    batch)
        shift
        cd "$PROJECT_ROOT"
        run_batch "$@"
        exit $?
        ;;
    audit)
        shift
        CONTROL_WINDOWS=3
        CONTROL_ONLY=true
        cd "$PROJECT_ROOT"
        run_batch "$@"
        exit $?
        ;;
    review)
        if (($# != 1)); then
            echo "У review нет дополнительных параметров." >&2
            exit 2
        fi
        cd "$PROJECT_ROOT"
        review_latest_batch
        ;;
    review-slides)
        if (($# != 1)); then
            echo "У review-slides нет дополнительных параметров." >&2
            exit 2
        fi
        cd "$PROJECT_ROOT"
        review_latest_smart_slides
        ;;
esac

while (($#)); do
    case "$1" in
        --run)
            RUN_REPLAY=true
            ;;
        --engine)
            shift
            ASR_ENGINE="${1:-}"
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "Неизвестный параметр: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

case "$ASR_ENGINE" in
    sherpa-0.54|vosk-0.22) ;;
    *)
        echo "Допустимы только sherpa-0.54 или vosk-0.22." >&2
        exit 2
        ;;
esac

if [[ ! -d "$AUDIO_ROOT" ]]; then
    echo "Не найдена папка с записями Родника: $AUDIO_ROOT" >&2
    exit 1
fi

shopt -s nullglob
SERMON_AUDIO=("$AUDIO_ROOT"/Воскресное*.webm)
if ((${#SERMON_AUDIO[@]} == 0)); then
    echo "Не найдены загруженные проповеди «Воскресное богослужение» в: $AUDIO_ROOT" >&2
    exit 1
fi

args=(
    .venv/bin/python tools/replay_audio_files.py
    --asr-engine "$ASR_ENGINE"
    --citation-detection-mode "$CITATION_DETECTION_MODE"
)
for audio_file in "${SERMON_AUDIO[@]}"; do
    args+=(--audio "$audio_file")
done
if [[ "$RUN_REPLAY" == true ]]; then
    args+=(--run)
fi
args+=(--include-processed)

cd "$PROJECT_ROOT"
exec "${args[@]}"
