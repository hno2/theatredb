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

## Sign-in and development access

Browsing is public. Personal lists, ratings and visits need sign-in via
Streamlit's native OIDC (`st.login`). Configure a provider in the ignored
`.streamlit/secrets.toml` (production: mount it read-only at
`/app/.streamlit/secrets.toml` in the app container):

```toml
[auth]
redirect_uri = "http://localhost:8501/oauth2callback"
cookie_secret = "REPLACE_WITH_A_STRONG_RANDOM_SECRET"

[auth.google]
client_id = "REPLACE_WITH_CLIENT_ID"
client_secret = "REPLACE_WITH_CLIENT_SECRET"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"
```

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

## Production deployments

The **Deploy production** workflow runs manually on `main`. It tests the
selected commit, publishes an amd64/arm64 image to GHCR, and deploys its digest
to the existing Compose project. The GitHub `production` environment records
the result and links to the live site.

### One-time setup

After merging the workflow, run these commands on your workstation to create
a key dedicated to GitHub Actions. Keep the private key out of the repository
and off the server.

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

1. In GitHub **Actions → Deploy production → Run workflow**, select `main`.
   For the first run only, check **Publish image without deploying**. After
   it succeeds, make the `theatredb` package public in its GHCR package
   settings so the vserver can pull without a registry credential. The
   image contains public source, not the server's `.env` or database.
2. Run the workflow again, this time with **Publish image without deploying**
   unchecked. Each run pulls the same digest for `app` and `crawl-worker`,
   backs up PostgreSQL, and replaces only those two containers. The app
   applies migrations at startup. Expect a short interruption, and avoid
   deploying during active crawls. A successful deployment checks the
   public Streamlit endpoint and worker process, not crawler job execution.
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
checkout and reinstall the two files before using the workflow again.
