PYTHON ?= python3
VENV ?= .venv
ARGS ?=
LIVERSE_ARGS ?= --check-updates --ask-approval-mode --slide-output holyrics --open-operator-qr --sermon-plan
ifeq ($(OS),Windows_NT)
BIN_DIR := $(VENV)\Scripts
PY := $(BIN_DIR)\python.exe
PIP := $(BIN_DIR)\pip.exe
else
BIN_DIR := $(VENV)/bin
PY := $(BIN_DIR)/python
PIP := $(BIN_DIR)/pip
endif

.PHONY: install test windows-vm-test windows-vm-engine windows-vm-installer windows-vm-inspect-holyrics windows-vm-recover-holyrics windows-release liverse gui analyze slides sword-russinodal check-sword-russinodal clean

install:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	$(PIP) install -e .

test:
	@if [ -x "$(PY)" ]; then \
		"$(PY)" -m unittest discover -s packages/bible_parser_core/tests -p 'test_*.py' -q; \
	else \
		$(PYTHON) -m unittest discover -s packages/bible_parser_core/tests -p 'test_*.py' -q; \
	fi

windows-vm-test: test
	./tools/sync_windows_build.sh --run-tests

windows-vm-engine: test
	./tools/sync_windows_build.sh --build-engine

windows-vm-installer: test
	./tools/sync_windows_build.sh --build-installer

windows-vm-recover-holyrics:
	./tools/recover_holyrics_vm.sh

windows-vm-inspect-holyrics:
	./tools/inspect_holyrics_vm.sh $(ARGS)

windows-release:
	@if [ -z "$(VERSION)" ]; then echo 'Укажите VERSION, например: make windows-release VERSION=1.2.0' >&2; exit 2; fi
	@if [ -z "$(PREVIOUS_INSTALLER)" ]; then echo 'Укажите PREVIOUS_INSTALLER — путь к установщику предыдущей версии.' >&2; exit 2; fi
	$(MAKE) test
	./tools/sync_windows_build.sh --build-installer --release-version "$(VERSION)" --upgrade-from-installer "$(PREVIOUS_INSTALLER)"

liverse:
	@if [ -x "$(PY)" ]; then \
		"$(PY)" tools/vosk_grammar_probe.py $(LIVERSE_ARGS) $(ARGS); \
	else \
		$(PYTHON) tools/vosk_grammar_probe.py $(LIVERSE_ARGS) $(ARGS); \
	fi

gui:
	@if [ -x "$(PY)" ]; then \
		"$(PY)" tools/liverse_gui.py; \
	else \
		$(PYTHON) tools/liverse_gui.py; \
	fi

analyze:
	@if [ -x "$(PY)" ]; then \
		"$(PY)" tools/analyze_vosk_probe_logs.py $(ARGS); \
	else \
		$(PYTHON) tools/analyze_vosk_probe_logs.py $(ARGS); \
	fi

slides:
	@if [ -x "$(PY)" ]; then \
		"$(PY)" tools/slide_server.py $(ARGS); \
	else \
		$(PYTHON) tools/slide_server.py $(ARGS); \
	fi

sword-russinodal:
	@if [ -x "$(PY)" ]; then \
		"$(PY)" tools/build_sword_russynodal.py $(ARGS); \
	else \
		$(PYTHON) tools/build_sword_russynodal.py $(ARGS); \
	fi

check-sword-russinodal:
	@if [ -x "$(PY)" ]; then \
		"$(PY)" tools/build_sword_russynodal.py --check $(ARGS); \
	else \
		$(PYTHON) tools/build_sword_russynodal.py --check $(ARGS); \
	fi

clean:
	rm -rf $(VENV) .cache/liverse .cache/live_verse_vosk
