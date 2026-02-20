.PHONY: lint update clean deploy

lint:
	uv run ruff format .
	uv run ruff check --fix .
	uv run pyright

update:
	uv sync --upgrade --all-groups

clean:
	rm -rf .ruff_cache .venv uv.lock .python-version

# --- Deploy ---

HOST=lab
EXEC=ssh -tt $(HOST)
SYNC=rsync -avz --delete-after --exclude={'.git','.venv','.*cache','.DS_Store','*.pyc','.env',}
DDIR=~/delta-farmer
UV=~/.local/bin/uv

deploy:
	$(SYNC) ./ $(HOST):$(DDIR)
	$(EXEC) "cd $(DDIR) && $(UV) sync"
