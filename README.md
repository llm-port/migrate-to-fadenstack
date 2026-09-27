# migrate-to-fadenstack

LLM.Port became **Fadenstack** with release 0.4.0. The product is the same, but
every name an install carries changed with it:

| What | LLM.Port 0.3 | Fadenstack 0.4 |
| --- | --- | --- |
| Compose project, volumes | `llm_port_shared`, `llm_port_shared_pg_data`, ... | `fadenstack`, `fadenstack_pg_data`, ... |
| Containers | `llm-port-backend`, `llm-port-api`, ... | `faden-backend`, `faden-gateway`, ... |
| Databases | `llm_port_backend`, `llm_api`, `llm_mcp`, `llm_skills` | `faden_backend`, `faden_gateway`, `faden_mcp`, `faden_skills` |
| Environment variables | `LLM_PORT_*`, `LLM_PORT_API_*`, `LLMPORT_*` | `FADEN_*`, `FADEN_GATEWAY_*`, `FADEN_*` |
| CLI | `llmport` (PyPI `llmport-cli`), `llmport.yaml` | `faden` (PyPI `fadenstack`), `faden.yaml` |
| Agent | `llmport-agent`, `~/.config/llmport-agent`, `~/.local/share/llmport-agent` | `faden-agent`, `~/.config/faden-agent`, `~/.local/share/faden-agent` |
| Images | `ghcr.io/llm-port/*`, `llmport/*` | `ghcr.io/fadenstack/*`, `faden/*` |
| Scheduled jobs | `llmport-backup`, `llmport-tls-renew` | `faden-backup`, `faden-tls-renew` |

This tool moves an install across and keeps everything in it: every database
row, every file, the settings, the certificates, the machines' membership.
It is one Python file with no dependencies (Python 3.9 or later).

## Before you start

- A backup of your own does not hurt. The tool makes one too: a dump of all
  databases and the install's files, in `~/llmport-migration-<date>/`.
- The server needs room for a second copy of its Docker volumes: the tool
  copies them, it does not move them, so the old install stays intact until
  you run `cleanup`. It checks the free space first.
- Machines serving models stop while you migrate: the agent is replaced, and
  the Ray clusters start again once the machines are back.

## Order

If machines serve models, migrate in this order:

```bash
# 1. on every machine: stop the old agent (it finishes in step 3)
python3 migrate_to_fadenstack.py machine

# 2. on the server
pip install fadenstack                     # the new CLI (a checkout install brings its own)
python3 migrate_to_fadenstack.py server

# 3. on every machine again: installs faden-agent from the migrated server
python3 migrate_to_fadenstack.py machine
```

Every command takes `--dry-run`, which shows what would change and changes
nothing. Every step is recorded in `~/.faden-migration.json`, so a run that
stopped half way picks up where it stopped.

## The server

```bash
python3 migrate_to_fadenstack.py server --dry-run
python3 migrate_to_fadenstack.py server
```

It finds the install from the old CLI's config (`$LLMPORT_CONFIG`, or
`~/.config/llmport/llmport.yaml`); `--install-dir` names it directly. Then:

1. **Backup**: `pg_dumpall` of the databases, the `.env`, the TLS material and
   the CLI's config.
2. **Stop** the old containers (the volumes stay).
3. **Code**: a release install gets its new files from the new CLI. An install
   run from a git checkout is checked out at `--git-ref <commit>` (fetch it
   first); the files git does not track move from the old folders to the new
   ones (`llm_port_shared/` → `shared/`, ...).
4. **Environment**: `.env` is rewritten with the new names. Secrets (keys,
   passwords, tokens) keep their values byte for byte; the old file is kept as
   `.env.pre-fadenstack`.
5. **CLI config**: `llmport.yaml` is written again as `faden.yaml`.
6. **Volumes**: each `llm_port_shared_*` volume is copied to its `fadenstack_*`
   name.
7. **Databases**: renamed, and the few rows that carry the old name follow:
   system settings' keys, model file paths, deployment specs' API version,
   container names and the Ray application routes.
8. **Images** the server holds for machines are tagged `faden/*`, old agent
   builds are put aside, **scheduled jobs** are moved to `faden-*`.
9. **Start**: `faden upgrade -y`, which migrates the databases' schemas and
   starts Fadenstack. `--no-start` stops before this and prints the command.

## A machine

```bash
python3 migrate_to_fadenstack.py machine --dry-run
python3 migrate_to_fadenstack.py machine
```

It stops `llmport-agent` and the containers it ran, backs up and moves its
config and state (the machine's identity and certificates go with them),
renames the variables in `agent.env`, tags the runtime images `faden/*`, and
runs the server's own installer, which finds the machine already enrolled and
starts `faden-agent`. Run as root for an agent installed as a system service.

## A developer checkout

```bash
python3 migrate_to_fadenstack.py dev --workspace path/to/checkout
```

For a checkout already on the renamed code: moves the dev infrastructure
(volumes, databases), the services' `.env` files and `~/.llmport-dev`, then
`faden dev up` starts it as before.

## Afterwards

When Fadenstack runs as it should:

```bash
python3 migrate_to_fadenstack.py cleanup      # old volumes, old image tags, old CLI config
pip uninstall llmport-cli                     # the old CLI
```

The backup folders are yours to delete when you no longer need them.

## Licence

Apache License 2.0, see [LICENSE](LICENSE).
