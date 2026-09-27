#!/usr/bin/env python3
"""Move an LLM.Port install to Fadenstack.

LLM.Port 0.3 became Fadenstack 0.4: the same product under a new name. Every
name that an install carries changed with it -- the compose project and its
volumes, the containers, the databases, the environment variables, the CLI
(``llmport`` -> ``faden``), the agent (``llmport-agent`` -> ``faden-agent``),
their config and state directories and a few rows in the database. This tool
moves one install across, keeping every row, every file and every setting.

    python3 migrate_to_fadenstack.py server  [--dry-run]   # the server (compose install)
    python3 migrate_to_fadenstack.py machine [--dry-run]   # a machine running llmport-agent
    python3 migrate_to_fadenstack.py dev --workspace DIR    # a developer checkout (faden dev)
    python3 migrate_to_fadenstack.py cleanup                # remove what the migration kept

Order, when machines serve models:

    1. on every machine:  migrate machine      (stops the old agent; finishes later)
    2. on the server:     migrate server
    3. on every machine:  migrate machine      (installs faden-agent from the server)

Nothing old is deleted until ``cleanup``: the old volumes, the backup of the
databases and the old files stay where they are. Every step is recorded in a
state file, so a run that stopped half way can be started again.

Standard library only, Python 3.9+, so it runs on any server and machine as it is.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import gzip
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import ssl
from pathlib import Path
from typing import Callable, Iterable

VERSION = "0.1.0"

OLD_PROJECT = "llm_port_shared"
NEW_PROJECT = "fadenstack"

#: Databases of the server, old name -> new name. ``rag`` and ``langfuse`` keep theirs.
DATABASES = {
    "llm_port_backend": "faden_backend",
    "llm_api": "faden_gateway",
    "llm_mcp": "faden_mcp",
    "llm_skills": "faden_skills",
}
#: Postgres roles the install made for itself (``llm_user`` keeps its name).
ROLES = {"llm_port_backend": "faden_backend"}

#: Prefixes of the system settings' keys (backend DB), old -> new.
SETTING_PREFIXES = {
    "llm_port_api.": "faden_gateway.",
    "llm_port_backend.": "faden_backend.",
    "llm_port_pii.": "faden_pii.",
    "llm_port_mcp.": "faden_mcp.",
    "llm_port_skills.": "faden_skills.",
}

#: A checkout's top-level folders, old -> new (files git does not track move with them).
CHECKOUT_FOLDERS = {
    "llm_port_backend": "backend",
    "llm_port_api": "gateway",
    "llm_port_frontend": "frontend",
    "llm_port_cli": "cli",
    "llm_port_node_agent": "agent",
    "llm_port_pii": "pii",
    "llm_port_mcp": "mcp",
    "llm_port_skills": "skills",
    "llm_port_shared": "shared",
    "llm_port_runtime_image": "runtime-image",
    "llm_port_dev": "dev",
}
#: Files of the install folder that were renamed; a release install still has the old copy.
RENAMED_INSTALL_FILES = {
    "rabbitmq/10-llmport.conf": "rabbitmq/10-faden.conf",
    "grafana/provisioning/dashboards/json/llm-port-overview.json":
        "grafana/provisioning/dashboards/json/faden-overview.json",
}
#: Generated files a checkout may carry changes to; the upgrade writes them again.
GENERATED = {"rabbitmq/definitions.json", "prometheus/targets.json"}

# ── names ─────────────────────────────────────────────────────────────────────

L = r"(?<![A-Za-z0-9])"

#: Kept as they are: the certified runtime image keeps its names until its next release,
#: and a checkout's own folder is the operator's choice.
PROTECT = ["llm_port_ray_runtime", "llm-port-ray-runtime", "/opt/llm-port/runtime", "llm-port-core", "llm-port-dev"]

#: Applied in this order to environment files, the CLI's config, timers and the agent's files.
RULES: list[tuple[str, str]] = [
    (r"github\.com/llm-port/llm-port-core", "github.com/fadenstack/fadenstack"),
    (r"github\.com/llm-port\b", "github.com/fadenstack"),
    (r"ghcr\.io/llm-port\b", "ghcr.io/fadenstack"),
    (r"inference\.llmport\.ai", "inference.fadenstack.com"),
    (r"\bX-LLM-Port-", "X-Faden-"),
    # variables
    (L + r"LLM_PORT_API_", "FADEN_GATEWAY_"),
    (L + r"LLM_PORT_", "FADEN_"),
    (L + r"LLMPORT_", "FADEN_"),
    # compose project, volumes, folders
    (r"(?<![A-Za-z0-9_])llm_port_shared_", "fadenstack_"),
    (r"(?<=[/\\])llm_port_shared\b", "shared"),
    (L + r"llm_port_shared\b", "fadenstack"),
    (L + r"llm_port_models\b", "faden_models"),
    (L + r"llm_port_api\b", "faden_gateway"),
    (L + r"llm_port_node_agent\b", "faden_agent"),
    (L + r"llm_port_(backend|pii|mcp|skills)\b", r"faden_\1"),
    (L + r"llm_api\b", "faden_gateway"),
    (L + r"llm_mcp\b", "faden_mcp"),
    (L + r"llm_skills\b", "faden_skills"),
    # the CLI
    (r"-m llmport\b", "-m faden_cli"),
    (r"\bllmport\.yaml\b", "faden.yaml"),
    (r"\.llmport-dev\b", ".faden-dev"),
    (r"\.llmport-release\b", ".faden-release"),
    (r"(?<=[/\\])llmport(?=[/\\])", "faden"),  # ~/.config/llmport/, %LOCALAPPDATA%\llmport\
    # the agent, RabbitMQ users, containers, images, paths
    (L + r"llmport-agent", "faden-agent"),
    (L + r"llmport_agent", "faden_agent"),
    (L + r"llmport-api\b", "faden-gateway"),
    (L + r"llm-port-api\b", "faden-gateway"),
    (r"/srv/llm-port\b", "/srv/faden"),
    (r"/opt/llm-port\b", "/opt/faden"),
    (r"/(var/lib|var/run|run|tmp|data)/llm-port\b", r"/\1/faden"),
    (r"/etc/llmport\b", "/etc/faden"),
    (L + r"llmport/", "faden/"),
    (L + r"llmport-", "faden-"),
    (L + r"llmport_", "faden_"),
    (L + r"llmport\.", "faden."),
    (L + r"llm-port\.", "faden."),
    (L + r"llm-port-", "faden-"),
    (L + r"llmport\b", "faden"),
    (L + r"llm-port\b", "fadenstack"),
    (L + r"llm_port\b", "faden"),
    (r"\bLLM\.Port\b", "Fadenstack"),
    (r"\bLLM Port\b", "Fadenstack"),
]
_COMPILED = [(re.compile(rx), rep) for rx, rep in RULES]
OLD = re.compile(r"llm[-_.]?port|llmport|LLM_PORT|LLM[ .]Port", re.I)

#: Values of these keys are secrets: kept byte for byte, whatever they spell.
SECRET_KEY = re.compile(r"(PASS|PASSWORD|SECRET|TOKEN|CREDENTIAL|_KEY$|_KEY_|^KEY|AUTH$|SALT)", re.I)


def rename(text: str) -> str:
    """*text* with every old name in it replaced by its new one."""
    guarded = text
    for i, token in enumerate(PROTECT):
        guarded = guarded.replace(token, f"\x00KEEP{i}\x00")
    for rx, rep in _COMPILED:
        guarded = rx.sub(rep, guarded)
    for i, token in enumerate(PROTECT):
        guarded = guarded.replace(f"\x00KEEP{i}\x00", token)
    return guarded


#: Values of these keys name things inside a data store the migration does not touch: kept.
KEEP_VALUE_KEY = re.compile(r"^LANGFUSE_")


def _looks_like_path(value: str) -> bool:
    return value.startswith(("/", "~", "./", "../")) or bool(re.match(r"^[A-Za-z]:[/\\]", value))


def rename_env(text: str, keep_paths: bool = False) -> tuple[str, list[str]]:
    """An env file with its keys and (non-secret) values renamed; and what changed, without secrets.

    *keep_paths*: a value that is a path on this host keeps its old spelling when the old path
    exists and the new one does not -- a folder nothing moves (an image cache, a model store).
    """
    out, changes = [], []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        eol = line[len(body):]
        m = re.match(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*)$", body)
        if not m:
            out.append(rename(body) + eol)  # comments
            continue
        lead, key, eq, value = m.groups()
        new_key = rename(key)
        secret = bool(SECRET_KEY.search(key) or SECRET_KEY.search(new_key))
        new_value = value if secret or KEEP_VALUE_KEY.search(key) else rename(value)
        note = ""
        if keep_paths and new_value != value:
            bare_old, bare_new = value.strip("'\""), new_value.strip("'\"")
            if _looks_like_path(bare_old) and Path(bare_old).expanduser().exists() \
                    and not Path(bare_new).expanduser().exists():
                new_value, note = value, "  (path kept: it exists, the renamed one does not)"
        out.append(f"{lead}{new_key}{eq}{new_value}{eol}")
        if new_key != key or new_value != value or note:
            shown = f"  {value!r} -> {new_value!r}" if new_value != value else ""
            changes.append(f"{key} -> {new_key}{shown}{note}")
    return "".join(out), changes


def env_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if m:
            v = m.group(2).strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            values[m.group(1)] = v
    return values


def volume_new_name(old: str) -> str | None:
    """``llm_port_shared_pg_data`` -> ``fadenstack_pg_data``; ``None`` for a volume of another project."""
    prefix = OLD_PROJECT + "_"
    if not old.startswith(prefix):
        return None
    key = old[len(prefix):]
    return f"{NEW_PROJECT}_{'faden_models' if key == 'llm_port_models' else key}"


def container_new_name(old: str) -> str:
    if old in ("llm-port-api", "llm-port-api-migrator"):
        return old.replace("llm-port-api", "faden-gateway")
    return rename(old)


# ── output ────────────────────────────────────────────────────────────────────

class Out:
    dry = False

    @staticmethod
    def step(title: str) -> None:
        print(f"\n== {title}", flush=True)

    @staticmethod
    def ok(msg: str) -> None:
        print(f"   ok    {msg}", flush=True)

    @staticmethod
    def info(msg: str) -> None:
        print(f"         {msg}", flush=True)

    @staticmethod
    def warn(msg: str) -> None:
        print(f"   WARN  {msg}", flush=True)

    @staticmethod
    def would(msg: str) -> None:
        print(f"   would {msg}", flush=True)


def die(msg: str, code: int = 1) -> None:
    print(f"\nERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def run(cmd: list[str], *, check: bool = True, input: str | None = None, quiet: bool = False,
        env: dict[str, str] | None = None, cwd: Path | None = None, timeout: float | None = None,
        ) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, input=input, env=env, cwd=cwd, timeout=timeout,
                          encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        die(f"{' '.join(shlex.quote(c) for c in cmd)} failed ({proc.returncode}):\n{proc.stderr.strip() or proc.stdout.strip()}")
    if not quiet and proc.returncode != 0:
        Out.warn(f"{cmd[0]} exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:300]}")
    return proc


def act(msg: str, fn: Callable[[], object]) -> None:
    """Do *fn*, or in a dry run say what it would do."""
    if Out.dry:
        Out.would(msg)
        return
    fn()
    Out.ok(msg)


def confirm(question: str, assume_yes: bool) -> None:
    if assume_yes or Out.dry:
        return
    if not sys.stdin.isatty():
        die(f"{question} -- pass --yes to go ahead without asking.")
    if input(f"\n{question} [y/N] ").strip().lower() not in ("y", "yes"):
        die("stopped; nothing after this point was changed.", 0)


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ── progress ──────────────────────────────────────────────────────────────────

class State:
    """Which steps are done, kept in ~/.faden-migration.json under *scope*, so a second run skips them."""

    path = Path.home() / ".faden-migration.json"

    def __init__(self, scope: str) -> None:
        self.scope = scope
        whole = json.loads(self.path.read_text()) if self.path.is_file() else {}
        self.data: dict = whole.get(scope) or {"tool": VERSION, "done": {}}

    def _save(self) -> None:
        if Out.dry:
            return
        whole = json.loads(self.path.read_text()) if self.path.is_file() else {}
        whole[self.scope] = self.data
        self.path.write_text(json.dumps(whole, indent=2))

    def done(self, step: str) -> bool:
        return step in self.data["done"]

    def mark(self, step: str, detail: object = True) -> None:
        self.data["done"][step] = {"at": now(), "detail": detail}
        self._save()

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def put(self, key: str, value) -> None:
        self.data[key] = value
        self._save()


# ── docker ────────────────────────────────────────────────────────────────────

def docker(*args: str, check: bool = True, input: str | None = None, quiet: bool = False,
           timeout: float | None = None) -> subprocess.CompletedProcess:
    return run(["docker", *args], check=check, input=input, quiet=quiet, timeout=timeout)


def containers(label: str | None = None, name_prefix: str | None = None) -> list[dict]:
    args = ["ps", "-a", "--format", "{{json .}}"]
    if label:
        args += ["--filter", f"label={label}"]
    rows = [json.loads(line) for line in docker(*args).stdout.splitlines() if line.strip()]
    if name_prefix:
        rows = [r for r in rows if r["Names"].startswith(name_prefix)]
    return rows


def volumes() -> list[str]:
    return [v for v in docker("volume", "ls", "--format", "{{.Name}}").stdout.split() if v]


def image_exists(ref: str) -> bool:
    return docker("image", "inspect", ref, check=False, quiet=True).returncode == 0


def volume_bytes(name: str, helper: str) -> int:
    out = docker("run", "--rm", "--network", "none", "-v", f"{name}:/v:ro", "--entrypoint", "du", helper,
                 "-sk", "/v", check=False, quiet=True).stdout.split()
    return int(out[0]) * 1024 if out and out[0].isdigit() else 0


def docker_root_free() -> int | None:
    root = docker("info", "--format", "{{.DockerRootDir}}", check=False, quiet=True).stdout.strip()
    try:
        return shutil.disk_usage(root).free if root and Path(root).exists() else None
    except OSError:
        return None


def gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB"


# ── the server ────────────────────────────────────────────────────────────────

def find_old_config(explicit: str | None) -> Path | None:
    candidates = [explicit, os.environ.get("LLMPORT_CONFIG")]
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            candidates.append(str(Path(base) / "llmport" / "llmport.yaml"))
    candidates.append(str(Path.home() / ".config" / "llmport" / "llmport.yaml"))
    for c in candidates:
        if c and Path(c).expanduser().is_file():
            return Path(c).expanduser().resolve()
    return None


def new_config_path(old: Path) -> Path:
    """``~/.config/llmport/llmport.yaml`` -> ``~/.config/faden/faden.yaml``; elsewhere the same folder."""
    parent = old.parent
    if parent.name == "llmport":
        parent = parent.with_name("faden")
    return parent / "faden.yaml"


def yaml_value(text: str, key: str) -> str | None:
    m = re.search(rf"^{key}\s*:\s*(.*)$", text, re.M)
    if not m:
        return None
    v = m.group(1).strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        v = v[1:-1]
    return v or None


class Server:
    """A compose install: its folder (with ``.env``), maybe a checkout around it, the CLI's config."""

    def __init__(self, args: argparse.Namespace, dev: bool = False) -> None:
        self.args = args
        self.dev = dev
        self.old_config = find_old_config(args.config)
        cfg_text = self.old_config.read_text() if self.old_config else ""
        install = args.install_dir or yaml_value(cfg_text, "install_dir")
        if dev:
            install = str(Path(args.workspace) / "shared")
        if not install:
            die("where is the install? Pass --install-dir (the folder with docker-compose.yaml and .env).")
        self.old_install = Path(install).expanduser().resolve()
        # A checkout: the install is <repo>/llm_port_shared (before) or <repo>/shared (after).
        repo = self.old_install.parent
        self.checkout = repo if (repo / ".git").exists() else None
        if self.checkout and self.old_install.name == "llm_port_shared":
            self.install = repo / "shared"
        else:
            self.install = self.old_install
        home = Path.home()
        self.backup_dir = Path(args.backup_dir).expanduser() if getattr(args, "backup_dir", None) else \
            home / f"llmport-migration-{now()[:8]}"
        self.state = State(f"server:{self.install}")
        if self.state.get("backup_dir"):
            self.backup_dir = Path(self.state.get("backup_dir"))

    # -- what is there --------------------------------------------------------

    def env_file(self) -> Path:
        for d in (self.install, self.old_install):
            if (d / ".env").is_file():
                return d / ".env"
        return self.install / ".env"

    def env(self) -> dict[str, str]:
        f = self.env_file()
        return env_values(f.read_text(encoding="utf-8")) if f.is_file() else {}

    def pg_user(self) -> str:
        e = self.env()
        return e.get("POSTGRES_USER") or "postgres"

    def survey(self) -> None:
        Out.step("What is here")
        Out.info(f"install folder   {self.old_install}" + (f"  -> {self.install}" if self.install != self.old_install else ""))
        if self.checkout:
            head = run(["git", "-C", str(self.checkout), "rev-parse", "--short", "HEAD"], check=False, quiet=True).stdout.strip()
            Out.info(f"checkout         {self.checkout} at {head}")
        Out.info(f"CLI config       {self.old_config or '(none found)'}")
        Out.info(f"environment      {self.env_file()}")
        old = containers(label=f"com.docker.compose.project={OLD_PROJECT}")
        new = containers(label=f"com.docker.compose.project={NEW_PROJECT}")
        Out.info(f"containers       {len(old)} of {OLD_PROJECT} ({sum(1 for c in old if c['State'] == 'running')} running),"
                 f" {len(new)} of {NEW_PROJECT}")
        vols = [v for v in volumes() if volume_new_name(v)]
        Out.info(f"volumes          {', '.join(vols) or '(none)'}")
        Out.info(f"backup folder    {self.backup_dir}")
        Out.info(f"progress file    {State.path} ({self.state.scope})")

    # -- steps ----------------------------------------------------------------

    def backup(self) -> None:
        Out.step("Backup")
        if self.state.done("backup"):
            Out.ok(f"done before: {self.backup_dir}")
            return
        pg = next((c for c in containers(name_prefix="llm-port-postgres") if c["Names"] == "llm-port-postgres"), None)
        target = self.backup_dir
        if pg is None:
            if not self.args.skip_db_backup:
                die("there is no llm-port-postgres container, so the databases cannot be dumped first.\n"
                    "Pass --skip-db-backup if you have a backup of your own.")
            Out.warn("no llm-port-postgres: no database dump (--skip-db-backup)")
        elif pg["State"] != "running" and not self.args.skip_db_backup:
            def start_pg() -> None:
                docker("start", "llm-port-postgres")
                for _ in range(90):
                    if docker("exec", "llm-port-postgres", "pg_isready", "-U", self.pg_user(), check=False,
                              quiet=True).returncode == 0:
                        return
                    time.sleep(1)
                die("llm-port-postgres did not become ready; see: docker logs llm-port-postgres")
            act("start llm-port-postgres for the dump (it was stopped)", start_pg)
            pg["State"] = "running"
        if self.args.skip_db_backup:
            pg = None
        def dump() -> None:
            target.mkdir(parents=True, exist_ok=True)
            if pg is not None and pg["State"] == "running":
                with gzip.open(target / "pg_dumpall.sql.gz", "wb") as fh:
                    proc = subprocess.Popen(["docker", "exec", "llm-port-postgres", "pg_dumpall", "-U", self.pg_user()],
                                            stdout=subprocess.PIPE)
                    assert proc.stdout is not None
                    shutil.copyfileobj(proc.stdout, fh)
                    if proc.wait() != 0:
                        die("pg_dumpall failed")
            # The install's own files: environment, certificates, credentials, generated config.
            with tarfile.open(target / "install-files.tar.gz", "w:gz") as tar:
                for rel in (".env", ".bootstrap-credentials", "tls", "tls-ca", "rabbitmq/definitions.json",
                            "prometheus/targets.json", ".llmport-release"):
                    p = self.old_install / rel
                    if p.exists():
                        tar.add(p, arcname=f"install/{rel}")
                if self.old_config:
                    tar.add(self.old_config, arcname=f"config/{self.old_config.name}")
            if self.checkout:
                diff = run(["git", "-C", str(self.checkout), "diff", "HEAD"], check=False, quiet=True).stdout
                (target / "checkout-local-changes.diff").write_text(diff, encoding="utf-8")
                head = run(["git", "-C", str(self.checkout), "rev-parse", "HEAD"], check=False, quiet=True).stdout.strip()
                (target / "checkout-head.txt").write_text(head + "\n")
        act(f"dump the databases and the install's files into {target}", dump)
        self.state.put("backup_dir", str(target))
        self.state.mark("backup", str(target))

    def stop(self) -> None:
        Out.step("Stop the old install")
        old = containers(label=f"com.docker.compose.project={OLD_PROJECT}")
        if not old:
            Out.ok("nothing of it is running")
        for c in old:
            name = c["Names"]
            act(f"stop and remove {name}", lambda n=name: (docker("stop", "-t", "30", n, check=False, quiet=True),
                                                           docker("rm", n, check=False, quiet=True)))
        nets = [n for n in docker("network", "ls", "--format", "{{.Name}}").stdout.split() if n.startswith(OLD_PROJECT + "_")]
        for n in nets:
            act(f"remove network {n}", lambda n=n: docker("network", "rm", n, check=False, quiet=True))

    def code(self) -> None:
        Out.step("The code")
        if not self.checkout:
            Out.info("a release install: the new CLI brings the new files (faden upgrade)")
            for old, new in RENAMED_INSTALL_FILES.items():
                p = self.install / old
                if p.exists():
                    act(f"move the old {old} into the backup (its successor is {new})",
                        lambda p=p, old=old: self._to_backup(p, f"install/{old}"))
            marker = self.install / ".llmport-release"
            if marker.exists():
                act("rename .llmport-release to .faden-release", lambda: marker.rename(self.install / ".faden-release"))
            return
        ref = self.args.git_ref
        repo = self.checkout
        if ref:
            head = run(["git", "-C", str(repo), "rev-parse", "HEAD"], quiet=True).stdout.strip()
            target = run(["git", "-C", str(repo), "rev-parse", f"{ref}^{{commit}}"], check=False, quiet=True).stdout.strip()
            if not target:
                die(f"{ref} is not in {repo}; fetch it first (git fetch, or a bundle).")
            if head == target:
                Out.ok(f"checkout already at {ref}")
            else:
                dirty = [line[3:] for line in run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
                                                  quiet=True).stdout.splitlines()]
                generated = {f"{OLD_PROJECT}/{g}" for g in GENERATED} | {f"shared/{g}" for g in GENERATED}
                other = [d for d in dirty if d not in generated]
                if other:
                    die("the checkout has local changes git would have to overwrite:\n  " + "\n  ".join(other) +
                        f"\nThey are saved in {self.backup_dir}/checkout-local-changes.diff; commit or discard them, then run again.")
                def checkout() -> None:
                    for d in dirty:
                        run(["git", "-C", str(repo), "checkout", "--", d])
                    run(["git", "-C", str(repo), "checkout", "--detach", target])
                act(f"check out {ref} ({target[:10]}) in {repo}" + (f", discarding generated {', '.join(dirty)}" if dirty else ""),
                    checkout)
        elif (repo / OLD_PROJECT).is_dir() and not (repo / "shared" / "docker-compose.yaml").is_file():
            die(f"{repo} is still the old code: pass --git-ref <the Fadenstack commit> (fetched already).")
        # What git does not track stays in the old folders: move it along.
        for old, new in CHECKOUT_FOLDERS.items():
            src = repo / old
            if not src.is_dir():
                continue
            moved, kept = self._merge_tree(src, repo / new)
            if moved or kept:
                if Out.dry:
                    Out.would(f"move what git does not track from {old}/ to {new}/ (after the checkout)")
                else:
                    Out.ok(f"{old}/ -> {new}/: {moved} files moved" + (f", {kept} kept in place (both exist)" if kept else ""))

    def _merge_tree(self, src: Path, dst: Path) -> tuple[int, int]:
        moved = kept = 0
        for path in sorted(src.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir():
                continue
            rel = path.relative_to(src)
            if any(part in ("__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "node_modules") for part in rel.parts):
                continue
            target = dst / rel
            if target.exists():
                kept += 1
                continue
            if Out.dry:
                moved += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(target))
            moved += 1
        if not Out.dry:
            for d in sorted((p for p in src.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                try:
                    d.rmdir()
                except OSError:
                    pass
            # caches and virtualenvs of the old folders are rebuilt, not moved
            leftovers = [p for p in src.rglob("*") if p.is_file()]
            if all(any(part in ("__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "node_modules") for part in
                       p.relative_to(src).parts) for p in leftovers):
                shutil.rmtree(src, ignore_errors=True)
        return moved, kept

    def _to_backup(self, path: Path, rel: str) -> None:
        dest = self.backup_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest))

    def environment(self) -> None:
        Out.step("Environment (.env)")
        f = self.env_file()
        if not f.is_file():
            Out.warn(f"no {f}")
            return
        text = f.read_text(encoding="utf-8")
        if not OLD.search(text):
            Out.ok(f"{f} carries no old name")
            return
        new_text, changes = rename_env(text)
        for c in changes:
            Out.info(c)
        def write() -> None:
            shutil.copy2(f, f.with_name(".env.pre-fadenstack"))
            f.write_bytes(new_text.encode("utf-8"))
        act(f"rewrite {f} ({len(changes)} lines; the old one kept as .env.pre-fadenstack)", write)
        self.state.mark("env")

    def config(self) -> None:
        Out.step("The CLI's config")
        if not self.old_config:
            Out.info("none found (the CLI reads its settings from the install folder)")
            return
        new = new_config_path(self.old_config)
        if new.is_file() and self.state.done("config"):
            Out.ok(f"done before: {new}")
            return
        text = rename(self.old_config.read_text(encoding="utf-8"))
        if self.install != self.old_install:
            text = text.replace(str(self.old_install), str(self.install))
        def write() -> None:
            new.parent.mkdir(parents=True, exist_ok=True)
            new.write_text(text, encoding="utf-8")
        act(f"write {new} from {self.old_config} (the old one stays until cleanup)", write)
        default_dir = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or "") / "faden" \
            if os.name == "nt" else Path.home() / ".config" / "faden"
        if new.parent != default_dir and "FADEN_CONFIG" not in os.environ:
            Out.info(f"it is not in the default place: run the CLI with FADEN_CONFIG={new}")
        self.state.put("new_config", str(new))
        self.state.mark("config", str(new))

    def copy_volumes(self) -> None:
        Out.step("Volumes")
        helper = self.helper_image()
        todo = []
        existing = set(volumes())
        for old in sorted(existing):
            new = volume_new_name(old)
            if not new:
                continue
            if self.state.done(f"volume:{old}") and new in existing:
                Out.ok(f"{old} -> {new}: copied before")
                continue
            todo.append((old, new))
        if not todo:
            Out.ok("nothing to copy")
            return
        sizes = {old: volume_bytes(old, helper) for old, _ in todo}
        need, free = sum(sizes.values()), docker_root_free()
        Out.info(f"{len(todo)} volumes, {gb(need)} to copy; free on Docker's disk: {gb(free) if free is not None else 'unknown'}")
        if free is not None and need * 1.05 + 2e9 > free:
            die(f"not enough room to copy the volumes ({gb(need)} needed, {gb(free)} free). Free some space, or move "
                "the largest volume by hand, then run again.")
        for old, new in todo:
            key = new[len(NEW_PROJECT) + 1:]
            def copy(old=old, new=new, key=key) -> None:
                if new in set(volumes()):
                    docker("volume", "rm", new)  # a half copy from an earlier run
                docker("volume", "create", "--label", f"com.docker.compose.project={NEW_PROJECT}",
                       "--label", f"com.docker.compose.volume={key}", new)
                docker("run", "--rm", "--network", "none", "-v", f"{old}:/from:ro", "-v", f"{new}:/to",
                       "--entrypoint", "sh", helper, "-c", "cp -a /from/. /to/", timeout=None)
            act(f"copy {old} -> {new} ({gb(sizes[old])})", copy)
            self.state.mark(f"volume:{old}", new)

    def helper_image(self) -> str:
        for ref in ("pgvector/pgvector:pg17", "alpine:3", "busybox:latest"):
            if image_exists(ref):
                return ref
        if not Out.dry:
            docker("pull", "alpine:3")
        return "alpine:3"

    def databases(self) -> None:
        Out.step("Databases")
        if self.state.done("databases"):
            Out.ok("renamed before")
            return
        vol = f"{NEW_PROJECT}_pg_data"
        if vol not in set(volumes()):
            if Out.dry:
                Out.would(f"rename the databases in {vol} (after the copy)")
                return
            die(f"{vol} does not exist; the volume copy has not run")
        image = next((i for i in ("pgvector/pgvector:pg17",) if image_exists(i)), "pgvector/pgvector:pg17")
        name = "faden-migrate-postgres"
        user = self.pg_user()
        if Out.dry:
            Out.would(f"start {image} on {vol} as {name}, then:")
            for o, n in DATABASES.items():
                Out.would(f"  ALTER DATABASE {o} RENAME TO {n}")
            for o, n in ROLES.items():
                Out.would(f"  ALTER ROLE {o} RENAME TO {n}")
            Out.would("  rewrite settings keys, model paths, deployment specs, container names and Ray routes")
            return
        docker("rm", "-f", name, check=False, quiet=True)
        docker("run", "-d", "--name", name, "--network", "none", "-v", f"{vol}:/var/lib/postgresql/data", image)
        try:
            for _ in range(120):
                if docker("exec", name, "pg_isready", "-U", user, check=False, quiet=True).returncode == 0:
                    break
                time.sleep(1)
            else:
                die("the temporary postgres did not come up; see: docker logs " + name)
            time.sleep(1)
            psql = lambda db, sql, check=True: docker("exec", "-i", name, "psql", "-v", "ON_ERROR_STOP=1", "-U", user,
                                                      "-d", db, "-At", "-c", sql, check=check)
            present = set(psql("postgres", "select datname from pg_database").stdout.split())
            for old, new in DATABASES.items():
                if old in present and new not in present:
                    psql("postgres", f'ALTER DATABASE "{old}" RENAME TO "{new}"')
                    Out.ok(f"database {old} -> {new}")
                elif new in present:
                    Out.ok(f"database {new} is there")
            roles = set(psql("postgres", "select rolname from pg_roles").stdout.split())
            for old, new in ROLES.items():
                if old in roles and new not in roles:
                    psql("postgres", f'ALTER ROLE "{old}" RENAME TO "{new}"')
                    Out.ok(f"role {old} -> {new} (its password is kept)")
            present = set(psql("postgres", "select datname from pg_database").stdout.split())
            if "faden_backend" in present:
                self._backend_rows(lambda sql: psql("faden_backend", sql))
            if "faden_gateway" in present:
                self._gateway_rows(lambda sql: psql("faden_gateway", sql))
        finally:
            docker("stop", "-t", "20", name, check=False, quiet=True)
            docker("rm", name, check=False, quiet=True)
        self.state.mark("databases")

    @staticmethod
    def _table(q: Callable, table: str, column: str | None = None) -> bool:
        sql = (f"select 1 from information_schema.columns where table_schema='public' and table_name='{table}'"
               + (f" and column_name='{column}'" if column else "") + " limit 1")
        return bool(q(sql).stdout.strip())

    def _backend_rows(self, q: Callable) -> None:
        def update(label: str, table: str, column: str, sql: str) -> None:
            if not self._table(q, table, column):
                return
            out = q(sql).stdout.strip().splitlines()
            n = sum(int(x.split()[-1]) for x in out if x.startswith("UPDATE"))
            if n:
                Out.ok(f"{label}: {n} rows")
        for table in ("system_setting_value", "system_setting_secret"):
            for old, new in SETTING_PREFIXES.items():
                n = len(old)
                update(f"{table} keys {old}* -> {new}*", table, "key",
                       f"UPDATE {table} t SET key = '{new}' || substr(key, {n + 1}) WHERE left(key, {n}) = '{old}' "
                       f"AND NOT EXISTS (SELECT 1 FROM {table} u WHERE u.key = '{new}' || substr(t.key, {n + 1}))")
        update("model files under /srv/faden", "model_artifacts", "path",
               "UPDATE model_artifacts SET path = '/srv/faden/' || substr(path, 15) WHERE left(path, 14) = '/srv/llm-port/'")
        update("model files under \\srv\\faden (a Windows backend)", "model_artifacts", "path",
               "UPDATE model_artifacts SET path = '\\srv\\faden\\' || substr(path, 15) "
               "WHERE left(path, 14) = '\\srv\\llm-port\\'")
        update("deployment specs' api_version", "inference_deployments", "spec_json",
               "UPDATE inference_deployments SET spec_json = jsonb_set(spec_json, '{api_version}', "
               "to_jsonb('inference.fadenstack.com/v1alpha1'::text)) "
               "WHERE spec_json->>'api_version' = 'inference.llmport.ai/v1alpha1'")
        update("environments' capabilities", "inference_environments", "capabilities_json",
               "UPDATE inference_environments SET capabilities_json = replace(capabilities_json::text, "
               "'\"llmport_sync\"', '\"faden_sync\"')::jsonb WHERE capabilities_json::text LIKE '%\"llmport\\_sync\"%'")
        # Ray Serve applications are named after the deployment: llmport-<id> -> faden-<id>.
        app = r"llmport-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
        for table, column, is_json in (("llm_providers", "endpoint_url", False), ("inference_endpoints", "address", False),
                                       ("inference_endpoints", "published_json", True)):
            expr = f"regexp_replace({column}::text, '{app}', 'faden-\\1', 'g')"
            update(f"{table}.{column}: Ray routes", table, column,
                   f"UPDATE {table} SET {column} = {expr + '::jsonb' if is_json else expr} "
                   f"WHERE {column}::text ~ '{app}'")
        if self._table(q, "container_registry", "name"):
            names = [n for n in q("SELECT name FROM container_registry WHERE name LIKE 'llm-port-%'").stdout.split() if n]
            renamed = 0
            for old in names:
                new = container_new_name(old)
                if new == old:
                    continue
                q(f"UPDATE container_registry SET name = '{new}' WHERE name = '{old}' "
                  f"AND NOT EXISTS (SELECT 1 FROM container_registry WHERE name = '{new}')")
                renamed += 1
            if renamed:
                Out.ok(f"container registry: {renamed} names")

    def _gateway_rows(self, q: Callable) -> None:
        app = r"llmport-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
        if self._table(q, "llm_provider_instance", "base_url"):
            out = q(f"UPDATE llm_provider_instance SET base_url = regexp_replace(base_url, '{app}', 'faden-\\1', 'g') "
                    f"WHERE base_url ~ '{app}'").stdout.strip()
            if out and out.split()[-1] != "0":
                Out.ok(f"gateway provider instances: Ray routes ({out.split()[-1]} rows)")

    def images(self) -> None:
        Out.step("Images held for machines")
        retag_images()

    def agent_binaries(self) -> None:
        d = self.install / "agent-binaries"
        old = sorted(d.glob("llmport-agent-*")) if d.is_dir() else []
        if not old:
            return
        Out.step("Agent builds")
        for p in old:
            act(f"move {p.name} into the backup (llmport-agent; machines get faden-agent)",
                lambda p=p: self._to_backup(p, f"agent-binaries/{p.name}"))

    def timers(self) -> None:
        Out.step("Scheduled jobs (backup, certificate renewal)")
        migrate_timers(self.state)

    def cli(self) -> None:
        """The new CLI: from the checkout (uv tool), or already installed (pip install fadenstack)."""
        Out.step("The faden CLI")
        found = self.args.faden or shutil.which("faden") or \
            (str(Path.home() / ".local" / "bin" / "faden") if (Path.home() / ".local" / "bin" / "faden").exists() else None)
        uv = shutil.which("uv") or (str(Path.home() / ".local" / "bin" / "uv") if (Path.home() / ".local" / "bin" / "uv").exists() else None)
        if self.checkout and uv and (Out.dry or (self.checkout / "cli" / "pyproject.toml").is_file()):
            def install() -> None:
                run([uv, "tool", "install", "--force", str(self.checkout / "cli")])
            act(f"install it from the checkout: uv tool install --force {self.checkout / 'cli'}", install)
            found = found or str(Path.home() / ".local" / "bin" / "faden")
        elif found:
            Out.ok(f"{found}")
        else:
            Out.warn("not installed: pip install fadenstack (or: uv tool install fadenstack), then run this again")
        self.args.faden = found

    def start(self) -> None:
        Out.step("Start Fadenstack")
        faden = self.args.faden or shutil.which("faden")
        env = dict(os.environ)
        cfg = self.state.get("new_config")
        if cfg:
            env["FADEN_CONFIG"] = cfg
        cmd = [faden or "faden", "upgrade", "-y"]
        shown = (f"FADEN_CONFIG={cfg} " if cfg else "") + " ".join(cmd)
        if self.args.no_start or not faden:
            if not faden:
                Out.warn("the faden CLI is not installed here (pip install fadenstack, or uv tool install ./cli)")
            Out.info(f"then start it with:  {shown}")
            return
        if Out.dry:
            Out.would(f"run {shown}")
            return
        Out.info(f"running {shown} (builds or pulls the images, migrates, starts; minutes)")
        proc = subprocess.run(cmd, env=env, cwd=str(self.install))
        if proc.returncode != 0:
            die(f"faden upgrade failed ({proc.returncode}); fix what it says and run it again: {shown}")
        Out.ok("Fadenstack is up")
        self.state.mark("start")

    def report(self) -> None:
        Out.step("Kept until you run cleanup")
        Out.info(f"old volumes         {', '.join(v for v in volumes() if volume_new_name(v)) or '-'}")
        Out.info(f"backup              {self.backup_dir}")
        if self.old_config:
            Out.info(f"old CLI config      {self.old_config}")
        Out.info("the old CLI         pip uninstall llmport-cli (or uv tool uninstall llmport-cli)")
        Out.info("machines            run 'migrate machine' on each (see the top of this file)")


def migrate_timers(state: State) -> None:
    if sys.platform != "linux":
        Out.info("no systemd here")
        return
    unit_dir = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"
    found = False
    for old_unit in ("llmport-backup", "llmport-tls-renew"):
        timer, service = unit_dir / f"{old_unit}.timer", unit_dir / f"{old_unit}.service"
        if not timer.is_file():
            continue
        found = True
        new_unit = old_unit.replace("llmport-", "faden-")
        def move(timer=timer, service=service, old_unit=old_unit, new_unit=new_unit) -> None:
            run(["systemctl", "--user", "disable", "--now", f"{old_unit}.timer"], check=False, quiet=True)
            for src in (service, timer):
                if src.is_file():
                    dst = unit_dir / src.name.replace(old_unit, new_unit)
                    dst.write_text(rename(src.read_text()))
                    src.unlink()
            run(["systemctl", "--user", "daemon-reload"], check=False, quiet=True)
            run(["systemctl", "--user", "enable", "--now", f"{new_unit}.timer"])
        act(f"{old_unit}.timer -> {new_unit}.timer (same schedule, runs faden)", move)
    # cron, where there is no systemd user manager
    cron = run(["crontab", "-l"], check=False, quiet=True) if shutil.which("crontab") else None
    if cron is not None and cron.returncode == 0 and re.search(r"# llmport-(backup|tls-renew)$", cron.stdout, re.M):
        found = True
        new = "\n".join(rename(line) if re.search(r"# llmport-(backup|tls-renew)$", line) else line
                        for line in cron.stdout.splitlines()) + "\n"
        act("rewrite the crontab's llmport lines", lambda: run(["crontab", "-"], input=new))
    if not found:
        Out.ok("none scheduled")
    state.mark("timers")


def retag_images() -> None:
    refs = [r for r in docker("images", "--format", "{{.Repository}}:{{.Tag}}").stdout.split()
            if r.startswith("llmport/") and not r.endswith(":<none>")]
    if not refs:
        Out.ok("no llmport/* images")
    for ref in refs:
        new = "faden/" + ref[len("llmport/"):]
        if image_exists(new):
            Out.ok(f"{new} is there")
            continue
        act(f"tag {ref} as {new}", lambda ref=ref, new=new: docker("tag", ref, new))


# ── a machine ─────────────────────────────────────────────────────────────────

class Machine:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        home = Path.home()
        root = os.geteuid() == 0 if hasattr(os, "geteuid") else False
        self.root = root
        self.old_env = Path("/etc/llmport-agent.env") if root else home / ".config" / "llmport-agent" / "agent.env"
        self.new_env = Path("/etc/faden-agent.env") if root else home / ".config" / "faden-agent" / "agent.env"
        self.old_config_dir = None if root else home / ".config" / "llmport-agent"
        self.new_config_dir = None if root else home / ".config" / "faden-agent"
        self.old_state = Path("/var/lib/llmport-agent") if root else home / ".local" / "share" / "llmport-agent"
        self.new_state = Path("/var/lib/faden-agent") if root else home / ".local" / "share" / "faden-agent"
        self.unit_dir = Path("/etc/systemd/system") if root else home / ".config" / "systemd" / "user"
        self.systemctl = ["systemctl"] if root else ["systemctl", "--user"]
        self.state = State(f"machine:{self.new_state}")
        self.backup_dir = Path(self.state.get("backup_dir") or home / f"llmport-agent-migration-{now()[:8]}")

    def survey(self) -> None:
        Out.step("What is here")
        Out.info(f"scope            {'system (root)' if self.root else 'user ' + os.environ.get('USER', '')}")
        Out.info(f"old agent config {self.old_env} ({'there' if self.old_env.is_file() else 'missing'})")
        Out.info(f"old agent state  {self.old_state} ({'there' if self.old_state.is_dir() else 'missing'})")
        unit = self.unit_dir / "llmport-agent.service"
        Out.info(f"old service      {unit} ({'there' if unit.is_file() else 'missing'})")
        mine = [c["Names"] for c in containers(name_prefix="llm-port-")]
        Out.info(f"old containers   {', '.join(mine) or '-'}")

    def stop(self) -> None:
        Out.step("Stop the old agent")
        unit = self.unit_dir / "llmport-agent.service"
        if unit.is_file() or run([*self.systemctl, "is-active", "llmport-agent"], check=False, quiet=True).stdout.strip() == "active":
            act("stop and disable llmport-agent",
                lambda: run([*self.systemctl, "disable", "--now", "llmport-agent"], check=False, quiet=True))
        pids = run(["pgrep", "-f", "llmport-agent run"], check=False, quiet=True).stdout.split()
        for pid in pids:
            act(f"stop a llmport-agent process started by hand (pid {pid})",
                lambda pid=pid: run(["kill", pid], check=False, quiet=True))
        # What it ran: the Ray runtime, the model-TLS proxy, engines. The new agent starts its own.
        for c in containers(name_prefix="llm-port-"):
            name = c["Names"]
            act(f"stop and remove container {name}",
                lambda name=name: (docker("stop", "-t", "30", name, check=False, quiet=True),
                                   docker("rm", name, check=False, quiet=True)))
        self.state.mark("stopped")

    def move(self) -> None:
        Out.step("Config and state")
        if self.state.done("moved"):
            Out.ok("moved before")
            return
        def backup() -> None:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            with tarfile.open(self.backup_dir / "llmport-agent-files.tar.gz", "w:gz") as tar:
                for p in (self.old_env.parent if self.old_config_dir else self.old_env, self.old_state):
                    if p.exists():
                        tar.add(p, arcname=p.name)
                for u in (self.unit_dir / "llmport-agent.service",):
                    if u.exists():
                        tar.add(u, arcname=f"units/{u.name}")
        act(f"back up the agent's config and state into {self.backup_dir}", backup)
        self.state.put("backup_dir", str(self.backup_dir))
        # the config folder (user) and the env file
        if self.old_config_dir and self.old_config_dir.is_dir():
            act(f"move {self.old_config_dir} -> {self.new_config_dir}", lambda: self._move_dir(self.old_config_dir, self.new_config_dir))
            for p in sorted(self.new_config_dir.glob("*llmport*")) if not Out.dry and self.new_config_dir else []:
                p.rename(p.with_name(rename(p.name)))
                Out.ok(f"renamed {p.name} -> {rename(p.name)}")
        elif self.old_env.is_file():
            act(f"move {self.old_env} -> {self.new_env}", lambda: shutil.move(str(self.old_env), str(self.new_env)))
        if self.old_state.is_dir():
            act(f"move {self.old_state} -> {self.new_state}", lambda: self._move_dir(self.old_state, self.new_state))
        env_file = self.new_env if not Out.dry else self.old_env
        if env_file.is_file():
            new_text, changes = rename_env(env_file.read_text(), keep_paths=True)
            for c in changes:
                Out.info(c)
            new_text = self._keep_store(new_text)
            act(f"rewrite {self.new_env} ({len(changes)} lines)", lambda: self.new_env.write_text(new_text))
        state_json = (self.new_state if not Out.dry else self.old_state) / "state.json"
        if state_json.is_file():
            text = state_json.read_text()
            new = text.replace("/llmport-agent/", "/faden-agent/").replace("/llmport-agent\"", "/faden-agent\"")
            if new != text:
                act("rewrite the paths in state.json", lambda: state_json.write_text(new))
        self.state.mark("moved")

    def _keep_store(self, text: str) -> str:
        """A model store under /srv/llm-port moves to /srv/faden -- when it can; otherwise the agent keeps the old one."""
        values = env_values(text)
        store = values.get("FADEN_NODE_AGENT_MODEL_STORE", "")
        if not store.startswith("/srv/faden"):
            return text
        old = store.replace("/srv/faden", "/srv/llm-port", 1)
        if not Path(old).exists():
            return text
        new_root = Path("/srv/faden")
        try:
            if not Out.dry and not new_root.exists():
                Path("/srv/llm-port").rename(new_root)
            Out.ok("model store /srv/llm-port -> /srv/faden")
            return text
        except OSError:
            Out.warn(f"cannot move /srv/llm-port (no permission): the agent keeps using {old}")
            return text.replace(f"MODEL_STORE={store}", f"MODEL_STORE={old}")

    @staticmethod
    def _move_dir(src: Path, dst: Path) -> None:
        if dst.exists():
            # a second run, or the new agent already made it: merge what is missing
            for p in src.rglob("*"):
                t = dst / p.relative_to(src)
                if p.is_file() and not t.exists():
                    t.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(p), str(t))
            shutil.rmtree(src, ignore_errors=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))

    def images(self) -> None:
        Out.step("Images")
        retag_images()

    def install(self) -> None:
        Out.step("Install faden-agent")
        env = env_values(self.new_env.read_text()) if self.new_env.is_file() else env_values(self.old_env.read_text()) \
            if self.old_env.is_file() else {}
        server = (self.args.server or self._moved_to() or env.get("FADEN_NODE_AGENT_BACKEND_URL")
                  or env.get("LLM_PORT_NODE_AGENT_BACKEND_URL") or "").rstrip("/")
        if not server:
            die("which server? Pass --server https://<fadenstack server>")
        ca = self.args.ca_file or self._server_ca(env)
        Out.info(f"server {server}" + (f", trusted through {ca}" if ca else ""))
        ctx = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
        url = f"{server}/api/install/faden-agent.sh"
        try:
            script = urllib.request.urlopen(url, context=ctx, timeout=30).read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                Out.warn(f"{server} is not Fadenstack yet (no faden-agent.sh): the old agent is stopped;"
                         " run this again once the server is migrated")
                return
            die(f"{url}: {exc}")
        except (urllib.error.URLError, OSError) as exc:
            die(f"{url}: {exc}")
        if self.args.no_install:
            Out.info(f"then install it with: sh faden-agent.sh --join {server}" + ("" if self.root else " --user") +
                     (f" --ca-file {ca}" if ca else ""))
            return
        cmd = ["sh", "-s", "--", "--join", server] + ([] if self.root else ["--user"]) + (["--ca-file", ca] if ca else [])
        if Out.dry:
            Out.would(f"run the server's installer: {' '.join(cmd)}")
            return
        proc = subprocess.run(cmd, input=script)
        if proc.returncode != 0:
            die("the installer failed; its output is above")
        old_unit = self.unit_dir / "llmport-agent.service"
        if old_unit.is_file():
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old_unit), str(self.backup_dir / old_unit.name))
            run([*self.systemctl, "daemon-reload"], check=False, quiet=True)
            Out.ok("removed the old service unit (kept in the backup)")
        old_bin = shutil.which("llmport-agent")
        if old_bin and os.access(Path(old_bin).parent, os.W_OK):
            shutil.move(old_bin, str(self.backup_dir / "llmport-agent"))
            Out.ok(f"moved the old binary {old_bin} into the backup")
        self.state.mark("installed")
        Out.ok("faden-agent is running; the server's console shows the machine again within a minute")

    def _state_dir(self) -> Path:
        return self.new_state if self.new_state.is_dir() else self.old_state

    def _moved_to(self) -> str | None:
        """The HTTPS address the agent moved to by itself (server-address.json), if it did."""
        try:
            data = json.loads((self._state_dir() / "server-address.json").read_text())
        except (OSError, ValueError):
            return None
        return data.get("to") or None

    def _server_ca(self, env: dict[str, str]) -> str | None:
        """What the agent trusts the server by: its CA bundle setting, or the authority it learned."""
        for key in ("FADEN_NODE_AGENT_TLS_CA_BUNDLE", "LLM_PORT_NODE_AGENT_TLS_CA_BUNDLE"):
            bundle = env.get(key)
            for candidate in (bundle, rename(bundle) if bundle else None):
                if candidate and Path(candidate).expanduser().is_file():
                    return str(Path(candidate).expanduser())
        own = self._state_dir() / "server-ca.pem"
        return str(own) if own.is_file() else None


# ── a developer checkout ──────────────────────────────────────────────────────

DEV_FOLDERS = ("backend", "gateway", "pii", "mcp", "skills", "frontend", "cli", "agent")


def dev(args: argparse.Namespace) -> None:
    ws = Path(args.workspace).expanduser().resolve()
    if not (ws / "shared" / "docker-compose.yaml").is_file():
        die(f"{ws} is not a Fadenstack checkout yet (no shared/docker-compose.yaml): check out the renamed code first")
    args.install_dir = str(ws / "shared")
    s = Server(args, dev=True)
    s.survey()
    confirm("Move this developer checkout's infrastructure and settings to Fadenstack?", args.yes)
    if not args.skip_db_backup:
        s.backup()
    s.stop()
    s.code()
    Out.step("Developer data folders")
    for old, new in ((Path.home() / ".llmport-dev", Path.home() / ".faden-dev"),
                     (ws / "gateway" / "~" / ".llmport-dev", ws / "gateway" / "~" / ".faden-dev")):
        if old.is_dir() and not new.exists():
            act(f"move {old} -> {new}", lambda old=old, new=new: shutil.move(str(old), str(new)))
    Out.step("Environment files of the services")
    for folder in ("shared", *DEV_FOLDERS):
        for name in (".env", ".env.local"):
            f = ws / folder / name
            if not f.is_file():
                continue
            text = f.read_text(encoding="utf-8")
            if not OLD.search(text):
                continue
            new_text, changes = rename_env(text, keep_paths=True)
            for c in changes:
                Out.info(c)
            def write(f=f, new_text=new_text) -> None:
                shutil.copy2(f, f.with_name(f.name + ".pre-fadenstack"))
                f.write_bytes(new_text.encode("utf-8"))
            act(f"rewrite {folder}/{name} ({len(changes)} lines)", write)
    s.config()
    s.copy_volumes()
    s.databases()
    Out.step("Next")
    Out.info("faden dev up        (starts the infrastructure as project fadenstack, and the services)")
    s.report()


# ── cleanup ───────────────────────────────────────────────────────────────────

def cleanup(args: argparse.Namespace) -> None:
    Out.step("What the migration kept")
    old_vols = [v for v in volumes() if volume_new_name(v)]
    for v in old_vols:
        new = volume_new_name(v)
        if new not in set(volumes()):
            Out.warn(f"{v}: its copy {new} does not exist -- kept")
            continue
        act(f"remove volume {v} (its copy is {new})", lambda v=v: docker("volume", "rm", v))
    old_imgs = [r for r in docker("images", "--format", "{{.Repository}}:{{.Tag}}").stdout.split() if r.startswith("llmport/")]
    for ref in old_imgs:
        if image_exists("faden/" + ref[len("llmport/"):]):
            act(f"remove the old tag {ref}", lambda ref=ref: docker("rmi", ref, check=False, quiet=True))
    cfg = find_old_config(args.config)
    if cfg:
        act(f"remove the old CLI config {cfg}", lambda: cfg.unlink())
    Out.info("the backup folders (~/llmport-migration-*) are yours to delete when you no longer need them")


# ── entry ─────────────────────────────────────────────────────────────────────

def server(args: argparse.Namespace) -> None:
    s = Server(args)
    s.survey()
    confirm("Stop LLM.Port and move this install to Fadenstack?", args.yes)
    s.backup()
    s.stop()
    s.code()
    s.environment()
    s.config()
    s.copy_volumes()
    s.databases()
    s.images()
    s.agent_binaries()
    s.cli()
    s.timers()
    s.start()
    s.report()


def machine(args: argparse.Namespace) -> None:
    m = Machine(args)
    m.survey()
    confirm("Stop llmport-agent (and what it runs) and move this machine to faden-agent?", args.yes)
    m.stop()
    m.move()
    m.images()
    m.install()


def rewrite_env(args: argparse.Namespace) -> None:
    f = Path(args.file)
    new_text, changes = rename_env(f.read_text(encoding="utf-8"))
    for c in changes:
        print(c)
    if args.write:
        shutil.copy2(f, f.with_name(f.name + ".pre-fadenstack"))
        f.write_bytes(new_text.encode("utf-8"))
        print(f"written ({len(changes)} lines changed)")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="migrate_to_fadenstack", description=__doc__.split("\n\n")[0])
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--dry-run", action="store_true", help="say what would happen; change nothing")
        sp.add_argument("-y", "--yes", action="store_true", help="do not ask")

    sp = sub.add_parser("server", help="move the server (compose install)")
    common(sp)
    sp.add_argument("--install-dir", help="the folder with docker-compose.yaml and .env (default: from llmport.yaml)")
    sp.add_argument("--config", help="the old CLI's llmport.yaml (default: $LLMPORT_CONFIG, ~/.config/llmport/)")
    sp.add_argument("--git-ref", help="a checkout install: the Fadenstack commit to check out (fetched already)")
    sp.add_argument("--backup-dir", help="where the backup goes (default ~/llmport-migration-<date>)")
    sp.add_argument("--skip-db-backup", action="store_true", help="no database dump (you have one)")
    sp.add_argument("--faden", help="the new CLI (default: faden on PATH)")
    sp.add_argument("--no-start", action="store_true", help="stop before 'faden upgrade'; print it instead")
    sp.set_defaults(fn=server)

    sp = sub.add_parser("machine", help="move a machine from llmport-agent to faden-agent")
    common(sp)
    sp.add_argument("--server", help="the Fadenstack server (default: the agent's BACKEND_URL)")
    sp.add_argument("--ca-file", help="the server's CA, when its certificate is its own (default: what the agent trusts)")
    sp.add_argument("--no-install", action="store_true", help="move everything, but do not run the installer")
    sp.set_defaults(fn=machine)

    sp = sub.add_parser("dev", help="move a developer checkout's infrastructure and settings")
    common(sp)
    sp.add_argument("--workspace", required=True, help="the checkout (with shared/, backend/, ...)")
    sp.add_argument("--config", help="the old CLI's llmport.yaml")
    sp.add_argument("--backup-dir")
    sp.add_argument("--skip-db-backup", action="store_true")
    sp.set_defaults(fn=dev, git_ref=None, faden=None, no_start=True, install_dir=None)

    sp = sub.add_parser("cleanup", help="remove the old volumes, image tags and config the migration kept")
    common(sp)
    sp.add_argument("--config")
    sp.set_defaults(fn=cleanup)

    sp = sub.add_parser("rewrite-env", help="show (or write) an env file with the new names")
    sp.add_argument("file")
    sp.add_argument("--write", action="store_true")
    sp.set_defaults(fn=rewrite_env, dry_run=False)

    args = p.parse_args(argv)
    Out.dry = bool(getattr(args, "dry_run", False))
    if Out.dry:
        print("DRY RUN: nothing is changed.")
    args.fn(args)


if __name__ == "__main__":
    main()
