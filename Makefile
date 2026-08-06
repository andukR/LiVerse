PYTHON ?= python3
VENV ?= .venv
ARGS ?=
LIVERSE_ARGS ?= --ask-approval-mode --slide-output holyrics --open-operator-qr --sermon-plan
ifeq ($(OS),Windows_NT)
BIN_DIR := $(VENV)\Scripts
PY := $(BIN_DIR)\python.exe
PIP := $(BIN_DIR)\pip.exe
else
BIN_DIR := $(VENV)/bin
PY := $(BIN_DIR)/python
PIP := $(BIN_DIR)/pip
endif

.PHONY: install liverse analyze slides sword-russinodal check-sword-russinodal clean

install:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	$(PIP) install -e .

liverse:
	@if [ -x "$(PY)" ]; then \
		"$(PY)" tools/vosk_grammar_probe.py $(LIVERSE_ARGS) $(ARGS); \
	else \
		$(PYTHON) tools/vosk_grammar_probe.py $(LIVERSE_ARGS) $(ARGS); \
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
