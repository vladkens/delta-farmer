.PHONY: prepare check update update-dev clean deploy logs foreach info proxy stats-was stats-now

FOREACH_CLT := $(filter-out hyperliquid vault,$(basename $(notdir $(wildcard apps/*.py))))
FOREACH_CMD := $(strip $(cmd) $(if $(filter all,$(p)),,$(p)))
FOREACH_RUN = echo "\n── $(1) ──" && uv run -m apps.$(1) $(FOREACH_CMD) --no-banner || true

prepare:
	uv run ruff format .
	uv run ruff check --fix .
	uv run ty check
	uv run pytest -v
	uv lock --check

check:
	uv lock --check
	uv sync --locked --all-groups
	uv run --locked ruff format --check .
	uv run --locked ruff check .
	uv run --locked ty check
	uv run --locked pytest -v

update:
	uv lock --upgrade
	uv audit --locked
	uv sync --locked --all-groups

update-dev:
	uv lock --upgrade-group test --upgrade-group lint
	uv audit --locked
	uv sync --locked --group test --group lint

clean:
	rm -rf .ruff_cache .venv uv.lock .python-version .pytest_cache
	find . -type f -name "*.pyc" -delete

# --- Foreach ---

foreach:
	@if [ -z "$(FOREACH_CMD)" ]; then \
		echo 'usage: make foreach cmd="<command> [args...]" [p=last|this]'; \
		exit 2; \
	fi
	@$(foreach client,$(FOREACH_CLT),$(call FOREACH_RUN,$(client));)

info:
	@$(MAKE) -s foreach cmd="info"

proxy:
	@$(MAKE) -s foreach cmd="proxy"

stats-was:
	@$(MAKE) -s foreach cmd="stats last"

stats-now:
	@$(MAKE) -s foreach cmd="stats this"

# --- Deploy ---

HOST=lab
EXEC=ssh -tt $(HOST)
SYNC=rsync -avz --delete-after \
	--exclude={'.git','docs/'} \
	--include='/configs/***' \
	--filter=':- .gitignore'
DDIR=~/delta-farmer
UV=~/.local/bin/uv

deploy:
	$(SYNC) ./ $(HOST):$(DDIR)
	$(EXEC) "cd $(DDIR) && $(UV) sync --locked"

logs:
	@$(EXEC) 'cd $(DDIR) && \
		command -v fzf >/dev/null || { echo "fzf is required on $(HOST)"; exit 127; }; \
		ls -1t logs/*.log 2>/dev/null | \
		fzf \
			--exit-0 \
			--no-sort \
			--prompt="logs> " \
			--preview="tail -n \$$FZF_PREVIEW_LINES -- {}" \
			--preview-window=down:60% \
			--bind="enter:execute(less +G -- {})" \
			--bind="ctrl-r:reload(ls -1t logs/*.log 2>/dev/null)"'
