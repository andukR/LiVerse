#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
project_dir="$(pwd)"
desktop_only=false

if [ "${1:-}" = "--desktop-only" ]; then
    desktop_only=true
    shift
fi

if [ "$#" -gt 0 ]; then
    printf 'Использование: %s [--desktop-only]\n' "$0" >&2
    exit 2
fi

if [ "$desktop_only" = false ]; then
    rm -rf "$project_dir/liverse.egg-info"
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -r requirements.txt
    .venv/bin/pip install -e .
    .venv/bin/python -c \
        'import sys; from pathlib import Path; from bible_parser_core.sherpa_streaming import ensure_sherpa_model; ensure_sherpa_model(Path(sys.argv[1]))' \
        "$project_dir/.cache/liverse/models/vosk-model-small-streaming-ru-0.54"
fi

apps_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
icons_dir="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/256x256/apps"
desktop_file="$apps_dir/liverse.desktop"
icon_file="$icons_dir/liverse.png"

mkdir -p "$apps_dir" "$icons_dir"
cp LiVerse.png "$icon_file"

cat > "$desktop_file" <<EOF
[Desktop Entry]
Type=Application
Name=LiVerse
Comment=Распознавание библейских ссылок и показ через Holyrics
Categories=Utility;
Terminal=false
Path=$project_dir
Exec=$project_dir/.venv/bin/python $project_dir/tools/liverse_gui.py
Icon=$icon_file
StartupWMClass=Liverse
StartupNotify=false
EOF
chmod +x "$desktop_file"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$apps_dir" >/dev/null 2>&1 || true
fi

printf '\nГотово. Запускайте так:\n'
printf '  make gui\n'
printf '\nДиагностический запуск с консолью:\n'
printf '  make liverse\n'
printf '\nТакже создан ярлык в меню приложений: LiVerse\n'
