# Development commands

Run commands from the repository root.

## Local app reload

Set `THEATERDB_DEV_RELOAD=1` in `.env`, then run:

```bash
uv run python -m frontend.dev
```

The development launcher watches Python files under `backend` and `frontend`
and restarts the entire Streamlit process on edits. Streamlit's own module
watcher is disabled in this mode: re-importing SQLModel models in the same
process would register tables twice. Restarts reset in-memory session state.
Schema changes still require migrations.

With `THEATERDB_DEV_RELOAD=0` (default), the launcher runs Streamlit normally.
Streamlit flags can be appended, for example `--server.port=8502`.
Environment variables override `.env`. Docker and production keep their
existing launch commands and do not use this development flag.

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

## Sign-in and development access

Browsing is public. Personal lists, ratings and visits need sign-in via
Streamlit's native OIDC (`st.login`). Configure a provider in the ignored
`.streamlit/secrets.toml`:

```toml
[auth]
redirect_uri = "http://localhost:8501/oauth2callback"
cookie_secret = "REPLACE_WITH_A_STRONG_RANDOM_SECRET"

[auth.google]
client_id = "REPLACE_WITH_CLIENT_ID"
client_secret = "REPLACE_WITH_CLIENT_SECRET"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"
```

For production, use a separate OIDC client, cookie secret and HTTPS redirect URI.
The production Compose override mounts `data/streamlit` from the production
checkout read-only at `/home/nonroot/.streamlit` in the app container, where
Streamlit reads its global secrets. Without a secrets file, browsing remains
available but sign-in is not configured. On the server, from the production
checkout, install a prepared secrets file readable only by the container user
(UID 999):

```bash
sudo install -d -m 700 -o 999 -g 999 data/streamlit
sudo install -m 600 -o 999 -g 999 /path/to/production-secrets.toml data/streamlit/secrets.toml
```

Keep both copies out of Git and restrict access to the source file too. Register
the exact production `redirect_uri` with the provider. Reinstall the updated
production Compose override on the server as described below, then redeploy to
apply the mount or changes to the secrets file. The crawler worker does not
receive the secrets.

New sign-ins are readers. The **Verwaltung** pages are shown only to admins.
Verify the account's issuer and subject, then promote its unique user ID;
never select an account by display name or email:

```sql
UPDATE "user" SET is_admin = true WHERE id = 'REPLACE_WITH_VERIFIED_USER_UUID';
```

For local work without a provider, add `THEATERDB_DEV_AUTH=1` to `.env`. Every
visitor then acts as one local admin (`dev:local`), marked **Dev-Zugang** in the
sidebar. The app refuses to start with this flag when `THEATERDB_ENV=production`,
which the production Compose override sets.

## Checks

```bash
uv run --all-groups pytest
uv run --all-groups prek run
```

For responsive UI checks, use viewport widths of 390, 640, 768 and 1280 pixels.
Catalog filters sit above search; catalog and production content grids reflow
based on available width. Personal actions on text and production pages stack
at full width on viewports up to 640 pixels. Check theatre production previews
for compact spacing between tags and the next entry.

Management and catalog category filters share five-minute cached, distinct
database choices. Fields with more than 200 choices retain text search,
except controlled vocabularies such as person types (`VOCABULARY_FIELDS`);
free-text fields such as URLs and descriptions never become dropdowns.
Person details use Edit, Productions and Text Contributions tabs with link
counts. Relation tables support header sorting, editing and row deletion;
changes persist on Save. Add links using the form below the table.
Productions and Text Contributions use an `Auswahl` checkbox column (built-in row ticks never
reach Python in `st.data_editor`). With two or more ticked, Zusammenführen
(next to the green Save) opens a dialog asking which text survives and reuses
the regular text merge; Entfernen deletes the ticked links only. Text
Contributions sort alphabetically by title. Foreign-key pickers share a
five-minute `_fk_choices` cache, cleared after any management save or merge.
Productions include theatre and premiere year, newest premiere first.
Every management page stores its selection as repeated `<kind>=<UUID>` URL
parameters (`person`, `text`, `production`, `organization`, `award`, `source`)
and restores it on reload, so records can be deep-linked. Selected rows outside
the current results page remain visible until deselected; unrelated URL
parameters are preserved. The open person tab is stored as `tab=<name>`
(e.g. `tab=Productions`; omitted for Edit). In the Productions and Text
Contributions tabs, tick exactly one row to open the linked record on its
management page.
The production add form searches after two typed characters (500 ms debounce)
instead of loading all productions. Up to 50 suggestions, including theatre
and year, appear newest first with a loading spinner. Suggestions and linked
production context share a five-minute cache; production edits and link saves invalidate it.
Current person types remain selectable even when cached category choices are stale.

## Production deployments

The **Deploy production** workflow runs on every push to `main` and can also be
started manually. It tests the selected commit, publishes an amd64/arm64 image
to GHCR, and deploys its digest to the existing Compose project. The GitHub
`production` environment records the result and links to the live site.
The image embeds the tested commit SHA for the sidebar GitHub link, including
when rolling back to an older image.

### One-time setup

Configure production before enabling automatic deployment on `main`. Run these
commands on your workstation to create a key dedicated to GitHub Actions. Keep
the private key out of the repository and off the server.

```bash
ssh-keygen -t ed25519 -N '' -C theatredb-production -f ~/.ssh/theatredb-production
cat ~/.ssh/theatredb-production.pub
```

On the vserver, use Bash and log in as the non-root user who can already run
Docker for this stack. From the existing production checkout (not a new clone),
run the following as a block. It finds the running DB's Compose project by
its checkout path and refuses to proceed unless there is just one. Confirm
that the displayed containers are your existing production stack.

```bash
(
  set -euo pipefail
  [[ $(git branch --show-current) == main ]]
  [[ $PWD =~ ^/[a-zA-Z0-9._/-]+$ ]]
  project=$(docker ps \
    --filter "label=com.docker.compose.project.working_dir=$PWD" \
    --filter label=com.docker.compose.service=db \
    --format '{{.Label "com.docker.compose.project"}}')
  [[ $project =~ ^[a-z0-9][a-z0-9_-]*$ ]] || {
    echo "Expected one running Compose database for $PWD" >&2
    exit 1
  }
  git pull --ff-only origin main
  docker compose -p "$project" -f docker-compose.yml ps
  docker compose up --help | grep -- --wait-timeout
  command -v flock curl
  sudo install -d -m 755 /usr/local/lib/theatredb
  sudo install -m 755 deploy/deploy-production /usr/local/lib/theatredb/deploy-production
  sudo install -m 644 deploy/compose.production.yml /usr/local/lib/theatredb/compose.production.yml
  read -r -p 'Paste the workstation public key (one line): ' public_key
  [[ $public_key =~ ^ssh-ed25519[[:space:]][A-Za-z0-9+/=]+[[:space:]]theatredb-production$ ]]
  umask 077
  mkdir -p ~/.ssh
  chmod 700 ~/.ssh
  touch ~/.ssh/authorized_keys
  chmod 600 ~/.ssh/authorized_keys
  if awk -v key="$public_key" '
    BEGIN { split(key, k, " ") }
    { for (i = 1; i < NF; i++) if ($i == k[1] && $(i+1) == k[2]) found = 1 }
    END { exit !found }
  ' ~/.ssh/authorized_keys; then
    echo "Key already exists in authorized_keys; inspect and replace its entry instead" >&2
    exit 1
  fi
  printf '\nrestrict,command="/usr/local/lib/theatredb/deploy-production %s %s" %s\n' \
    "$PWD" "$project" "$public_key" >> ~/.ssh/authorized_keys
  printf 'Compose directory: %s\nCompose project: %s\nSSH user: %s\n' "$PWD" "$project" "$(id -un)"
  cat /etc/ssh/ssh_host_ed25519_key.pub
)
```

In [GitHub environment settings](https://github.com/hno2/theatredb/settings/environments),
create `production` and restrict deployment branches to `main`. Add:

| Kind | Name | Value |
| --- | --- | --- |
| Secret | `SSH_KEY` | Contents of the workstation's `~/.ssh/theatredb-production` private key |
| Variable | `SSH_TARGET` | `user@vserver-hostname`, using a host reachable from GitHub runners |
| Variable | `SSH_KNOWN_HOSTS` | `vserver-hostname ssh-ed25519 AAAA...`, using the actual host public key printed on the server |
| Variable | `SSH_PORT` | Omit for port 22; otherwise set the port and use `[vserver-hostname]:PORT` in the known-hosts entry |

Use the host key from your trusted server session, not an unverified network
scan. The dedicated Actions key is restricted to the deploy script; the
server account's Docker access is still effectively root access.

If the deploy log says the image digest is not a file or command, SSH ran it
directly instead of invoking the forced command. On the server, check the
`authorized_keys` of the user in `SSH_TARGET`: the public key matching
`SSH_KEY` must have the `restrict,command="..."` entry shown above, with the
correct production directory and project. Remove any unrestricted or
duplicate entries for that same key, rather than merely appending a restricted
entry after them. Confirm the installed deploy script and Compose override
exist, then rerun the workflow. Do not send an unrestricted shell command from
the workflow as a workaround.

### Release and rollback

1. A push to `main` starts a deployment automatically. To rerun it manually,
   select `main` in **Actions → Deploy production → Run workflow** and leave
   **Publish image without deploying** unchecked. To publish only, check that
   option on a manual run. Before the server's first pull, make the `theatredb`
   package public in GHCR so it can pull without a registry credential. The
   image contains public source, not the server's `.env` or database.
2. Each deployment pulls the same digest for `app`, `api` and `crawl-worker`,
   backs up PostgreSQL, and replaces those containers. The app
   applies migrations at startup; deployment waits for its health check before
   starting the API and worker. The API is publicly routed at
   `https://theater.simonklug.de/api/`, with documentation at `/docs`.
   Expect a short interruption, and avoid
   deploying during active crawls. A successful deployment checks the
   public Streamlit and API endpoints and worker process, not crawler job execution.
   The public check retries transient HTTP errors while the proxy catches up;
   persistent failures still fail the deployment.
3. Keep the digest from the last successful Actions run. To roll back a
   compatible release, substitute the actual production directory, Compose
   project and prior digest in this command on the vserver:

   ```bash
   /usr/local/lib/theatredb/deploy-production /path/to/production PROJECT ghcr.io/hno2/theatredb@sha256:DIGEST
   ```

Backups are stored in `~/.local/state/theatredb/PROJECT/`. Set up off-host
retention and test restores separately. An image rollback does not roll
back database migrations; restoring a backup can lose writes made since
it was taken. For a schema-breaking release, plan a maintenance window
and verify the restore procedure before deploying.

If the base Compose file or deployment scripts change, update the server
checkout and reinstall the two files before using the workflow again. For the
API service, this one-time update must happen before deploying the release:

```bash
git pull --ff-only origin main
sudo install -m 755 deploy/deploy-production /usr/local/lib/theatredb/deploy-production
sudo install -m 644 deploy/compose.production.yml /usr/local/lib/theatredb/compose.production.yml
```

Run these commands from the existing production checkout. Subsequent releases
deploy all three services automatically until the deployment files change again.
