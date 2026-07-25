PYTHON ?= python3.12
VENV := .venv
PIDFILE := .musiqa_bot.pid

.PHONY: setup doctor run start stop test test-net api clean

setup:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install -U pip
	$(VENV)/bin/pip install --require-hashes -r requirements.lock
	$(VENV)/bin/pip install -r requirements-dev.txt
	$(MAKE) doctor
	@echo "✅ Setup done. Put BOT_TOKEN in .env, then: make run"

doctor:
	@command -v ffmpeg >/dev/null || { echo "ffmpeg is required"; exit 1; }
	@command -v deno >/dev/null || { echo "Deno >=2.3 is required for YouTube"; exit 1; }
	@$(VENV)/bin/python -c "import yt_dlp_ejs" || { echo "yt-dlp-ejs is missing"; exit 1; }
	@echo "✅ ffmpeg, Deno and yt-dlp-ejs are available"

run:
	$(VENV)/bin/python -m bot.main

# run detached (keeps running after you close the terminal); logs to bot.log
start:
	@if [ -f "$(PIDFILE)" ] && kill -0 "$$(cat "$(PIDFILE)")" 2>/dev/null; then \
		echo "Bot is already running (PID $$(cat "$(PIDFILE)"))"; exit 1; \
	fi
	@nohup $(VENV)/bin/python -m bot.main > bot.log 2>&1 & echo $$! > "$(PIDFILE)"
	@echo "▶️  started in background → tail -f bot.log"

stop:
	@if [ -f "$(PIDFILE)" ]; then \
		kill "$$(cat "$(PIDFILE)")" 2>/dev/null || true; rm -f "$(PIDFILE)"; \
		echo "⏹  stopped"; \
	else echo "Bot PID file not found"; fi

test:
	$(VENV)/bin/pytest tests/

test-net:
	$(VENV)/bin/pytest tests/ -m network

api:
	docker compose up -d --build
	@echo "Local Bot API + bot started; do not also run 'make run'"

clean:
	rm -rf downloads/*.mp4 downloads/*.mp3 __pycache__ bot/__pycache__ .pytest_cache
