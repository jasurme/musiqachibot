PY312 := /opt/homebrew/opt/python@3.12/libexec/bin/python3
VENV := .venv

.PHONY: setup run stop test test-net api clean

setup:
	$(PY312) -m venv $(VENV)
	$(VENV)/bin/pip install -U pip
	$(VENV)/bin/pip install -r requirements-dev.txt
	@echo "✅ Setup done. Put BOT_TOKEN in .env, then: make run"

run:
	$(VENV)/bin/python -m bot.main

# run detached (keeps running after you close the terminal); logs to bot.log
start:
	nohup $(VENV)/bin/python -m bot.main > bot.log 2>&1 &
	@echo "▶️  started in background → tail -f bot.log"

stop:
	-pkill -f "bot.main"
	@echo "⏹  stopped"

test:
	$(VENV)/bin/pytest tests/

test-net:
	$(VENV)/bin/pytest tests/ -m network

api:
	docker compose up -d telegram-bot-api

clean:
	rm -rf downloads/*.mp4 downloads/*.mp3 __pycache__ bot/__pycache__ .pytest_cache
