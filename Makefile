PYTHON ?= python3
VENV ?= .venv
ARGS ?=
ifeq ($(OS),Windows_NT)
BIN_DIR := $(VENV)\Scripts
PY := $(BIN_DIR)\python.exe
PIP := $(BIN_DIR)\pip.exe
else
BIN_DIR := $(VENV)/bin
PY := $(BIN_DIR)/python
PIP := $(BIN_DIR)/pip
endif

.PHONY: install liverse analyze clean

install:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	$(PIP) install -e .

liverse:
	@if [ -x "$(PY)" ]; then \
		"$(PY)" tools/vosk_grammar_probe.py $(ARGS); \
	else \
		$(PYTHON) tools/vosk_grammar_probe.py $(ARGS); \
	fi

analyze:
	@if [ -x "$(PY)" ]; then \
		"$(PY)" tools/analyze_vosk_probe_logs.py $(ARGS); \
	else \
		$(PYTHON) tools/analyze_vosk_probe_logs.py $(ARGS); \
	fi

clean:
	rm -rf $(VENV) .cache/live_verse_vosk
