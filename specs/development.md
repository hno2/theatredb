# Development commands

Run commands from the repository root.

## Database

```bash
uv run python -m backend.db status
uv run python -m backend.db migrate
```

After changing models, generate and review a migration, then apply it:

```bash
uv run python -m backend.db revision -m "describe the change"
uv run python -m backend.db migrate
```

Back up before migrations. In Docker, run database commands with
`docker compose run --rm app python -m backend.db ...`.

## Checks

```bash
uv run --all-groups pytest
uv run --all-groups prek run
```
