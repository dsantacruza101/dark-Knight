"""Radar tecnologico compartido de la Batifamilia.

Mantiene a cada especialista (`batcave/specialists/<nombre>.yaml`) al dia con
las dependencias de sus repos y dispara parches proactivos:

    python3 -m batcave.radar --specialist nightwing [--dry-run] [--weekly]

Inventario: por cada repo de `repos` del perfil se usa una copia de SOLO
LECTURA en `radar.workspace_dir/<repo>` (default `~/projects/.radar-workspace`):
`git clone` si no existe, si no `fetch` + `reset --hard origin/<rama>`, con
la rama de trabajo del repo (`work_branch` de su entrada en `repos` del
perfil, default `github.work_branch` = `dev`; si no existe en origin,
`main`/`master`). Nunca se tocan los directorios de produccion
(`repo_paths`), nunca se comitea ni se pushea.

Dependencias directas: `package.json` (+ version instalada de
`package-lock.json`), `requirements.in` si existe (version tomada del pin de
`requirements.txt`/`requirements*.lock`; si no existe, `requirements.txt`),
`requirements-dev.txt` (dev), `pyproject.toml` y `pom.xml`. Transitivas:
todo lo demas de `package-lock.json`, `requirements.txt` (pip freeze) y
`requirements*.lock` (pip-compile).

Chequeo diario (sin tokens de Claude):
  - Vulnerabilidades: OSV (`/v1/querybatch` + `/v1/vulns/{id}`), ecosistemas
    npm, PyPI y Maven, sobre TODAS las dependencias (directas y transitivas).
  - Ultima version: registry de npm, API JSON de PyPI, Maven Central.
  - Clasificacion: `security_patch` (hay version corregida en el mismo
    major), `security_watch` (ningun advisory tiene version corregida y la
    dependencia es solo de desarrollo, directa o transitiva),
    `security_major` (el fix exige otro major, o no hay fix en una
    dependencia de produccion), y solo para dependencias directas
    `outdated_major`, `outdated_minor`.

Acciones:
  - `security_patch` -> issue (o actualizacion del existente) con labels
    `night-agent` + label del especialista + `dependencies`, con advisories,
    versiones, comando exacto, bloque ```radar-patch``` (JSON: package,
    from, to, ecosystem, direct, dev, manifest, advisories) y seccion
    `Archivos:`; el modo Desarrollo de Batman lo aplica esa noche en la
    rama de trabajo del repo de forma determinista (sin Aider ni Claude).
  - `security_watch` -> sin issue (si habia uno abierto se cierra con un
    comentario explicando la politica); se lista en el resumen semanal bajo
    "Sin parche disponible (vigilando)".
  - `security_major` -> issue con `needs-human` + label del especialista +
    `dependencies` (NUNCA `night-agent`); en modo semanal incluye la nota de
    Claude con cambios incompatibles y plan de migracion.
  - Deduplicacion por marcador oculto `<!-- radar:<repo>:<paquete>:<advisories> -->`
    en el cuerpo de los issues abiertos (mismo repo+paquete = se actualiza).
  - Telegram inmediato ("📡 Radar de <Especialista>") solo para hallazgos de
    seguridad critical/high en dependencias de produccion (una vez por
    advisory); todo lo demas va al resumen semanal.

Modo semanal (`--weekly`, tras el reset de tokens del domingo 23:00): para
cada paquete `security_major`/`outdated_major` (max
`radar.max_weekly_summaries`, default 5) se bajan las release notes de GitHub
y `claude -p` (solo herramientas de lectura) escribe una nota en
`batcave/knowledge/<especialista>/<paquete>-<version>.md`. Luego se envia el
resumen semanal a Alfred.

`--dry-run`: sincroniza el workspace e imprime inventario y hallazgos, sin
issues, sin Telegram, sin Claude y sin escribir estado.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests
import yaml
from packaging.version import InvalidVersion, Version

from batcave.secrets import mask_secrets
from modes.development import (
    build_clone_url,
    git_cmd,
    github_auth_env,
    load_env,
    load_specialists,
    profile_repos,
    resolve_project_path,
    resolve_work_branch,
    run_cli,
    specialist_display,
)

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.yaml"
MEMORY_DIR = BASE_DIR / "batcave" / "memory"
REPORTS_DIR = BASE_DIR / "reports"
LOG_PATH = BASE_DIR / "radar.log"

DEFAULT_WORKSPACE_DIR = "~/projects/.radar-workspace"
DEFAULT_KNOWLEDGE_DIR = "batcave/knowledge"
DEFAULT_MAX_WEEKLY_SUMMARIES = 5
DEFAULT_HTTP_TIMEOUT = 15
DEFAULT_CLAUDE_TIMEOUT = 600
DEFAULT_ALERT_SEVERITIES = ("CRITICAL", "HIGH")
DEFAULT_OWNER = "dsantacruza101"
DEPENDENCIES_LABEL = "dependencies"
NEEDS_HUMAN_LABEL = "needs-human"
WORK_BRANCH_FALLBACKS = ("main", "master")

CLONE_TIMEOUT = 300
GIT_TIMEOUT = 120
HTTP_WORKERS = 8
OSV_BATCH_SIZE = 500
OSV_URL = "https://api.osv.dev/v1"
NPM_REGISTRY = "https://registry.npmjs.org"
PYPI_URL = "https://pypi.org/pypi"
MAVEN_SEARCH_URL = "https://search.maven.org/solrsearch/select"
GITHUB_API = "https://api.github.com"
RELEASE_NOTES_MAX_CHARS = 15000
RELEASE_NOTE_MAX_CHARS = 4000
MAX_USAGE_LINES = 20
EVENTS_RETENTION_DAYS = 45
DIGEST_MAX_ITEMS = 10

ECOSYSTEM_NPM = "npm"
ECOSYSTEM_PYPI = "PyPI"
ECOSYSTEM_MAVEN = "Maven"

SECURITY_PATCH = "security_patch"
SECURITY_MAJOR = "security_major"
SECURITY_WATCH = "security_watch"
OUTDATED_MAJOR = "outdated_major"
OUTDATED_MINOR = "outdated_minor"
CATEGORY_ORDER = (SECURITY_MAJOR, SECURITY_PATCH, SECURITY_WATCH, OUTDATED_MAJOR, OUTDATED_MINOR)
PATCH_BLOCK_LANG = "radar-patch"
WATCH_CLOSE_COMMENT = (
    "📡 Cerrado por la nueva politica del radar: esta vulnerabilidad no tiene ninguna version "
    "corregida publicada y el paquete solo se usa en desarrollo (directa o transitiva), asi que "
    "no hay nada que aplicar ni riesgo en produccion. Ya no se abren issues para estos casos: "
    "el radar lo sigue vigilando a diario y lo lista en el resumen semanal bajo "
    "\"Sin parche disponible (vigilando)\". Si aparece un fix, se abre un issue nuevo automaticamente."
)
SEVERITY_RANK = {"CRITICAL": 4, "HIGH": 3, "MODERATE": 2, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0}

GITHUB_REPO_RE = re.compile(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/#?]|$)")
VERSION_IN_TAG_RE = re.compile(r"(\d+(?:\.\d+)+(?:[-.]?[A-Za-z]+[\w.]*)?)$")
NPM_SKIP_SPEC_RE = re.compile(r"^(?:file:|link:|workspace:|git|github:|http|npm:|\*|latest$|x$)", re.IGNORECASE)
NPM_RANGE_VERSION_RE = re.compile(r"(\d+)(?:\.(\d+|x|\*))?(?:\.(\d+|x|\*))?(-[\w.]+)?")
REQ_LINE_RE = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(===|==|~=|>=|>|<=|<|!=)?\s*([A-Za-z0-9.*+!_-]*)"
)
MAVEN_NS_RE = re.compile(r"^\{[^}]+\}")
PY_SKIP_NAMES = {"python", "pip", "setuptools", "wheel"}
SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".java")
SKIP_DIRS = {"node_modules", ".git", "dist", "build", "venv", ".venv", "__pycache__", "target", "coverage"}

log = logging.getLogger("radar")


class MaskingFilter(logging.Filter):
    """Enmascara secretos en cada registro antes de escribirlo."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask_secrets(record.getMessage())
        record.args = None
        return True


def setup_logging(quiet_stdout: bool) -> None:
    file_handler = logging.FileHandler(LOG_PATH)
    stream_handler = logging.StreamHandler(sys.stderr if quiet_stdout else sys.stdout)
    stream_handler.setLevel(logging.WARNING if quiet_stdout else logging.INFO)
    for handler in (file_handler, stream_handler):
        handler.addFilter(MaskingFilter())
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, stream_handler])
    for noisy in ("httpx", "httpx2", "httpcore", "urllib3", "github"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# --------------------------------------------------------------------------
# Modelo
# --------------------------------------------------------------------------


@dataclass
class Dependency:
    repo: str
    ecosystem: str
    name: str
    version: Optional[str]
    version_source: str  # lock | exacta | rango | minima | pom
    manifest: str
    dev: bool = False
    direct: bool = True

    @property
    def scope(self) -> str:
        return "directa" if self.direct else "transitiva"


@dataclass
class Advisory:
    id: str
    aliases: list[str]
    summary: str
    severity: str
    fixed: Optional[str]
    url: str


@dataclass
class Finding:
    dep: Dependency
    category: str
    target: Optional[str]
    latest: Optional[str]
    advisories: list[Advisory] = field(default_factory=list)
    other_versions: list[str] = field(default_factory=list)  # mismo paquete transitivo en otras versiones

    @property
    def max_severity(self) -> str:
        if not self.advisories:
            return "UNKNOWN"
        return max((a.severity for a in self.advisories), key=lambda s: SEVERITY_RANK.get(s, 0))

    @property
    def advisory_key(self) -> str:
        return "+".join(sorted(a.id for a in self.advisories)) if self.advisories else str(self.target or self.latest)


# --------------------------------------------------------------------------
# Versiones
# --------------------------------------------------------------------------


def version_key(ecosystem: str, raw: Optional[str]) -> Optional[tuple]:
    """Clave comparable (release de 6 numeros, estable?, texto) para npm/PyPI/Maven."""
    if not raw:
        return None
    text = raw.strip().lstrip("vV=").strip()
    if ecosystem == ECOSYSTEM_PYPI:
        try:
            parsed = Version(text)
            release = (tuple(parsed.release) + (0,) * 6)[:6]
            return release + (0 if (parsed.is_prerelease or parsed.is_devrelease) else 1, text)
        except InvalidVersion:
            pass
    match = re.match(r"^(\d+(?:\.\d+)*)(.*)$", text)
    if not match:
        return None
    numbers = [int(part) for part in match.group(1).split(".")]
    suffix = match.group(2)
    # `+build` (npm) y sufijos Maven tipo `.RELEASE`/`.Final` son estables.
    stable = int(not suffix or suffix.startswith("+") or bool(re.fullmatch(r"(?i)[.-]?(release|final|ga)", suffix)))
    return (tuple(numbers) + (0,) * 6)[:6] + (stable, text)


def is_stable(ecosystem: str, raw: Optional[str]) -> bool:
    key = version_key(ecosystem, raw)
    return bool(key) and key[6] == 1


def major_of(ecosystem: str, raw: Optional[str]) -> Optional[tuple]:
    """Major semantico: para 0.x el minor tambien rompe compatibilidad."""
    key = version_key(ecosystem, raw)
    if not key:
        return None
    return (0, key[1]) if key[0] == 0 else (key[0],)


def vgt(ecosystem: str, a: Optional[str], b: Optional[str]) -> bool:
    ka, kb = version_key(ecosystem, a), version_key(ecosystem, b)
    return bool(ka and kb) and ka[:7] > kb[:7]


def vmax(ecosystem: str, versions: list[str]) -> Optional[str]:
    valid = [v for v in versions if version_key(ecosystem, v)]
    return max(valid, key=lambda v: version_key(ecosystem, v)[:7]) if valid else None


# --------------------------------------------------------------------------
# Configuracion y workspace
# --------------------------------------------------------------------------


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def radar_config(config: dict) -> dict:
    return config.get("radar", {}) or {}


def http_timeout(config: dict) -> int:
    return int(radar_config(config).get("http_timeout", DEFAULT_HTTP_TIMEOUT))


def repo_full_name(config: dict, short: str) -> str:
    """Nombre `owner/repo`: el de `github.repos` si esta ahi; si no, owner de
    `radar.github_owner` (o el del primer repo de `github.repos`)."""
    repos = config.get("github", {}).get("repos", []) or []
    for full in repos:
        if full.split("/")[-1].lower() == short.lower():
            return full
    owner = radar_config(config).get("github_owner") or (repos[0].split("/")[0] if repos else DEFAULT_OWNER)
    return f"{owner}/{short}"


def production_paths(config: dict) -> list[Path]:
    paths = [Path(p).expanduser().resolve() for p in (config.get("repo_paths") or {}).values()]
    paths += [Path(p).expanduser().resolve() for p in config.get("red_hood", {}).get("local_repos", []) or []]
    return paths


def get_workspace_dir(config: dict) -> Path:
    """Workspace del radar. Se niega a usar una ruta que sea (o contenga, o
    este dentro de) un directorio de produccion."""
    workspace = Path(radar_config(config).get("workspace_dir", DEFAULT_WORKSPACE_DIR)).expanduser().resolve()
    for prod in production_paths(config):
        if workspace == prod or prod.is_relative_to(workspace) or workspace.is_relative_to(prod):
            raise RuntimeError(f"radar.workspace_dir ({workspace}) se cruza con produccion ({prod}); se aborta")
    return workspace


def sync_workspace_repo(full_name: str, workspace_dir: Path, work_branch: str) -> Optional[tuple[Path, str]]:
    """Copia de solo lectura: clone o fetch + reset --hard a
    origin/<work_branch> (la del repo segun el perfil, `resolve_work_branch`;
    si no existe, main/master). Devuelve (ruta, rama) o None. Nunca comitea
    ni pushea."""
    path = workspace_dir / full_name.split("/")[-1]
    auth = github_auth_env()
    if not (path / ".git").exists():
        workspace_dir.mkdir(parents=True, exist_ok=True)
        clone = run_cli(git_cmd(["clone", build_clone_url(full_name), str(path)]), timeout=CLONE_TIMEOUT, extra_env=auth)
        if clone.returncode != 0:
            log.error("No se pudo clonar %s: %s", full_name, mask_secrets(clone.stderr[-400:]))
            return None
    else:
        fetch = run_cli(git_cmd(["fetch", "origin", "--prune"]), cwd=path, timeout=GIT_TIMEOUT, extra_env=auth)
        if fetch.returncode != 0:
            log.warning("fetch fallo en %s (se usa la copia local): %s", full_name, mask_secrets(fetch.stderr[-300:]))

    for branch in (work_branch, *WORK_BRANCH_FALLBACKS):
        if run_cli(["git", "rev-parse", "--verify", "--quiet", f"origin/{branch}"], cwd=path, timeout=GIT_TIMEOUT).returncode != 0:
            continue
        run_cli(["git", "checkout", "-f", "-B", branch, f"origin/{branch}"], cwd=path, timeout=GIT_TIMEOUT)
        reset = run_cli(["git", "reset", "--hard", f"origin/{branch}"], cwd=path, timeout=GIT_TIMEOUT)
        run_cli(["git", "clean", "-fdq"], cwd=path, timeout=GIT_TIMEOUT)
        if reset.returncode != 0:
            log.error("reset --hard origin/%s fallo en %s", branch, full_name)
            return None
        return path, branch
    log.error("%s no tiene ninguna de las ramas %s en origin", full_name, (work_branch, *WORK_BRANCH_FALLBACKS))
    return None


# --------------------------------------------------------------------------
# Inventario
# --------------------------------------------------------------------------


def npm_version_from_spec(spec: str) -> Optional[str]:
    match = NPM_RANGE_VERSION_RE.search(spec)
    if not match:
        return None
    parts = [match.group(1), match.group(2) or "0", match.group(3) or "0"]
    parts = ["0" if p in ("x", "*") else p for p in parts]
    return ".".join(parts) + (match.group(4) or "")


def lock_versions(lock: dict) -> dict[str, str]:
    versions: dict[str, str] = {}
    for key, meta in (lock.get("packages") or {}).items():
        if key.startswith("node_modules/") and "/node_modules/" not in key[len("node_modules/"):]:
            if isinstance(meta, dict) and meta.get("version"):
                versions[key[len("node_modules/"):]] = meta["version"]
    for name, meta in (lock.get("dependencies") or {}).items():  # lockfileVersion 1
        if isinstance(meta, dict) and meta.get("version"):
            versions.setdefault(name, meta["version"])
    return versions


def lock_all_packages(lock: dict) -> list[tuple[str, str, bool]]:
    """Todos los paquetes instalados del lock (anidados incluidos), como
    (nombre, version, dev). Sin duplicados de nombre+version."""
    found: dict[tuple[str, str], bool] = {}

    def add(name: str, version, dev: bool) -> None:
        if name and isinstance(version, str) and version:
            key = (name, version)
            found[key] = found.get(key, True) and dev

    for key, meta in (lock.get("packages") or {}).items():
        if "node_modules/" not in key or not isinstance(meta, dict) or meta.get("link"):
            continue
        add(key.rsplit("node_modules/", 1)[1], meta.get("version"), bool(meta.get("dev")))

    def walk(deps: dict) -> None:  # lockfileVersion 1
        for name, meta in (deps or {}).items():
            if isinstance(meta, dict):
                add(name, meta.get("version"), bool(meta.get("dev")))
                walk(meta.get("dependencies") or {})

    if not lock.get("packages"):
        walk(lock.get("dependencies") or {})
    return [(name, version, dev) for (name, version), dev in found.items()]


def inventory_npm(repo: str, path: Path) -> list[Dependency]:
    manifest = path / "package.json"
    if not manifest.exists():
        return []
    try:
        pkg = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("%s: package.json invalido: %s", repo, exc)
        return []
    lock: dict = {}
    lock_path = path / "package-lock.json"
    if lock_path.exists():
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("%s: package-lock.json invalido: %s", repo, exc)
    locked = lock_versions(lock)
    deps: dict[str, Dependency] = {}
    # Directas = dependencies/devDependencies; optional/peer quedan como
    # transitivas del lock (se revisan vulnerabilidades, no outdated_*).
    for section, dev in (("dependencies", False), ("devDependencies", True)):
        for name, spec in (pkg.get(section) or {}).items():
            if name in deps or not isinstance(spec, str):
                continue
            if name in locked:
                version, source = locked[name], "lock"
            elif NPM_SKIP_SPEC_RE.match(spec.strip()):
                version, source = None, "no-registry"
            else:
                version, source = npm_version_from_spec(spec), "rango"
            deps[name] = Dependency(repo, ECOSYSTEM_NPM, name, version, source, "package.json", dev)
    direct = {(d.name, d.version) for d in deps.values()}
    transitive = [
        Dependency(repo, ECOSYSTEM_NPM, name, version, "lock", "package-lock.json", dev, direct=False)
        for name, version, dev in lock_all_packages(lock)
        if (name, version) not in direct
    ]
    return list(deps.values()) + transitive


def parse_requirement(line: str, repo: str, manifest: str, dev: bool) -> Optional[Dependency]:
    line = line.split("#", 1)[0].split(";", 1)[0].strip()
    if not line or line.startswith(("-", "git+", "http")) or "://" in line or " @ " in line:
        return None
    match = REQ_LINE_RE.match(line)
    if not match:
        return None
    name, _extras, op, version = match.groups()
    if name.lower() in PY_SKIP_NAMES:
        return None
    if not op or not version or op in ("<", "<=", "!=") or "*" in version:
        return Dependency(repo, ECOSYSTEM_PYPI, name, None, "sin-version", manifest, dev)
    source = "exacta" if op in ("==", "===") else "minima"
    return Dependency(repo, ECOSYSTEM_PYPI, name, version, source, manifest, dev)


def poetry_spec_version(spec) -> Optional[str]:
    if isinstance(spec, dict):
        spec = spec.get("version")
    if not isinstance(spec, str):
        return None
    return npm_version_from_spec(spec) if spec.strip() not in ("*", "") else None


def py_key(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def read_requirements(path: Path, repo: str, dev: bool) -> list[Dependency]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return [d for d in (parse_requirement(line, repo, path.name, dev) for line in lines) if d]


def inventory_python(repo: str, path: Path) -> list[Dependency]:
    """Directas: `requirements.in` (si existe; si no, `requirements.txt`) +
    `requirements-dev.txt` + `pyproject.toml`. Transitivas: el resto de los
    pines exactos de `requirements.txt` (pip freeze) y `requirements*.lock`."""
    deps: dict[str, Dependency] = {}

    def add(dep: Optional[Dependency]) -> None:
        if dep and py_key(dep.name) not in deps:
            deps[py_key(dep.name)] = dep

    req_deps = read_requirements(path / "requirements.txt", repo, False)
    lock_deps = [d for lock in sorted(path.glob("requirements*.lock")) for d in read_requirements(lock, repo, False)]
    pinned: dict[str, Dependency] = {}
    for dep in req_deps + lock_deps:
        if dep.version_source == "exacta":
            pinned.setdefault(py_key(dep.name), dep)

    req_in = path / "requirements.in"
    if req_in.exists():
        # requirements.in = intencion (paquetes importados); la version real es el
        # pin, o si no hay pin exacto, la minima declarada en requirements.txt.
        fallback = {py_key(d.name): d for d in req_deps if d.version}
        for dep in read_requirements(req_in, repo, False):
            pin = pinned.get(py_key(dep.name)) or fallback.get(py_key(dep.name))
            if pin and dep.version_source != "exacta":
                dep.version, dep.version_source, dep.manifest = pin.version, pin.version_source, pin.manifest
            add(dep)
    else:
        for dep in req_deps:
            add(dep)
    for dep in read_requirements(path / "requirements-dev.txt", repo, True):
        pin = pinned.get(py_key(dep.name))
        if pin and dep.version_source != "exacta":
            dep.version, dep.version_source, dep.manifest = pin.version, "exacta", pin.manifest
        add(dep)
    pyproject = path / "pyproject.toml"
    if pyproject.exists():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            log.error("%s: pyproject.toml invalido: %s", repo, exc)
            data = {}
        project = data.get("project") or {}
        for line in project.get("dependencies") or []:
            add(parse_requirement(line, repo, "pyproject.toml", False))
        for lines in (project.get("optional-dependencies") or {}).values():
            for line in lines:
                add(parse_requirement(line, repo, "pyproject.toml", True))
        poetry = (data.get("tool") or {}).get("poetry") or {}
        groups = [(poetry.get("dependencies") or {}, False), (poetry.get("dev-dependencies") or {}, True)]
        groups += [((g or {}).get("dependencies") or {}, True) for g in (poetry.get("group") or {}).values()]
        for table, dev in groups:
            for name, spec in table.items():
                if name.lower() in PY_SKIP_NAMES:
                    continue
                version = poetry_spec_version(spec)
                add(Dependency(repo, ECOSYSTEM_PYPI, name, version, "rango" if version else "sin-version", "pyproject.toml", dev))
    # Transitivas: se marcan como produccion (no se sabe que paquete las trae).
    transitive: dict[str, Dependency] = {}
    for dep in req_deps + lock_deps:
        key = py_key(dep.name)
        if dep.version_source == "exacta" and key not in deps and key not in transitive:
            dep.direct = False
            transitive[key] = dep
    return list(deps.values()) + list(transitive.values())


def inventory_maven(repo: str, path: Path) -> list[Dependency]:
    pom = path / "pom.xml"
    if not pom.exists():
        return []
    try:
        root = ET.parse(pom).getroot()
    except (OSError, ET.ParseError) as exc:
        log.error("%s: pom.xml invalido: %s", repo, exc)
        return []

    def strip(tag: str) -> str:
        return MAVEN_NS_RE.sub("", tag)

    def child_text(node: ET.Element, name: str) -> Optional[str]:
        for child in node:
            if strip(child.tag) == name:
                return (child.text or "").strip()
        return None

    props: dict[str, str] = {}
    for child in root:
        if strip(child.tag) == "properties":
            props = {strip(p.tag): (p.text or "").strip() for p in child}
    props["project.version"] = child_text(root, "version") or ""

    def resolve(value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        resolved = re.sub(r"\$\{([^}]+)\}", lambda m: props.get(m.group(1), m.group(0)), value)
        return None if "${" in resolved or resolved.startswith(("[", "(")) else resolved

    deps: dict[str, Dependency] = {}
    for node in root.iter():
        if strip(node.tag) != "dependency":
            continue
        group, artifact = child_text(node, "groupId"), child_text(node, "artifactId")
        if not group or not artifact:
            continue
        name = f"{group}:{artifact}"
        version = resolve(child_text(node, "version"))
        dev = (child_text(node, "scope") or "") == "test"
        if name not in deps:
            deps[name] = Dependency(repo, ECOSYSTEM_MAVEN, name, version, "pom" if version else "sin-version", "pom.xml", dev)
    return list(deps.values())


def build_inventory(repo: str, path: Path) -> list[Dependency]:
    return inventory_npm(repo, path) + inventory_python(repo, path) + inventory_maven(repo, path)


# --------------------------------------------------------------------------
# OSV
# --------------------------------------------------------------------------


def osv_name(dep: Dependency) -> str:
    if dep.ecosystem == ECOSYSTEM_PYPI:
        return re.sub(r"[-_.]+", "-", dep.name).lower()
    return dep.name


def cvss3_score(vector: str) -> Optional[float]:
    """Puntaje base CVSS v3.x a partir del vector (para advisories sin
    `database_specific.severity`, comunes en PYSEC)."""
    try:
        metrics = dict(part.split(":", 1) for part in vector.split("/")[1:])
        scope_changed = metrics["S"] == "C"
        av = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}[metrics["AV"]]
        ac = {"L": 0.77, "H": 0.44}[metrics["AC"]]
        pr = {"N": 0.85, "L": 0.68 if scope_changed else 0.62, "H": 0.5 if scope_changed else 0.27}[metrics["PR"]]
        ui = {"N": 0.85, "R": 0.62}[metrics["UI"]]
        cia = {"H": 0.56, "L": 0.22, "N": 0.0}
        iss = 1 - (1 - cia[metrics["C"]]) * (1 - cia[metrics["I"]]) * (1 - cia[metrics["A"]])
    except (KeyError, ValueError):
        return None
    impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15 if scope_changed else 6.42 * iss
    if impact <= 0:
        return 0.0
    exploitability = 8.22 * av * ac * pr * ui
    total = (1.08 if scope_changed else 1.0) * (impact + exploitability)
    return math.ceil(min(total, 10) * 10) / 10


def severity_from_vuln(vuln: dict) -> str:
    db_severity = str((vuln.get("database_specific") or {}).get("severity") or "").upper()
    if db_severity in SEVERITY_RANK:
        return "MEDIUM" if db_severity == "MODERATE" else db_severity
    for entry in vuln.get("severity") or []:
        if str(entry.get("type", "")).startswith("CVSS_V3"):
            score = cvss3_score(entry.get("score", ""))
            if score is None:
                continue
            if score >= 9:
                return "CRITICAL"
            if score >= 7:
                return "HIGH"
            if score >= 4:
                return "MEDIUM"
            return "LOW" if score > 0 else "UNKNOWN"
    return "UNKNOWN"


def fixed_version_for(vuln: dict, dep: Dependency) -> Optional[str]:
    """Menor version `fixed` de OSV mayor que la actual, para este paquete."""
    wanted = osv_name(dep).lower()
    candidates: list[str] = []
    for affected in vuln.get("affected") or []:
        pkg = affected.get("package") or {}
        if pkg.get("ecosystem") != dep.ecosystem:
            continue
        name = pkg.get("name", "")
        name = re.sub(r"[-_.]+", "-", name).lower() if dep.ecosystem == ECOSYSTEM_PYPI else name.lower()
        if name != wanted:
            continue
        for rng in affected.get("ranges") or []:
            for event in rng.get("events") or []:
                fixed = event.get("fixed")
                if fixed and vgt(dep.ecosystem, fixed, dep.version):
                    candidates.append(fixed)
    if not candidates:
        return None
    return min(candidates, key=lambda v: version_key(dep.ecosystem, v)[:7])


def osv_query(session: requests.Session, deps: list[Dependency], timeout: int) -> dict[int, list[str]]:
    """IDs de vulnerabilidades por indice de `deps` (querybatch, con paginacion)."""
    found: dict[int, list[str]] = {}
    for start in range(0, len(deps), OSV_BATCH_SIZE):
        chunk = deps[start:start + OSV_BATCH_SIZE]
        queries = [{"package": {"name": osv_name(d), "ecosystem": d.ecosystem}, "version": d.version} for d in chunk]
        try:
            resp = session.post(f"{OSV_URL}/querybatch", json={"queries": queries}, timeout=timeout)
            resp.raise_for_status()
            results = resp.json().get("results") or []
        except (requests.RequestException, ValueError) as exc:
            log.error("OSV querybatch fallo: %s", exc)
            continue
        for offset, result in enumerate(results):
            ids = [v["id"] for v in result.get("vulns") or [] if v.get("id")]
            token = result.get("next_page_token")
            while token:
                try:
                    page = session.post(
                        f"{OSV_URL}/querybatch",
                        json={"queries": [{**queries[offset], "page_token": token}]},
                        timeout=timeout,
                    )
                    page.raise_for_status()
                    page_result = (page.json().get("results") or [{}])[0]
                except (requests.RequestException, ValueError, IndexError) as exc:
                    log.warning("OSV paginacion fallo para %s: %s", queries[offset]["package"]["name"], exc)
                    break
                ids += [v["id"] for v in page_result.get("vulns") or [] if v.get("id")]
                token = page_result.get("next_page_token")
            if ids:
                found[start + offset] = ids
    return found


def osv_details(session: requests.Session, ids: set[str], timeout: int) -> dict[str, dict]:
    def fetch(vuln_id: str) -> tuple[str, Optional[dict]]:
        try:
            resp = session.get(f"{OSV_URL}/vulns/{quote(vuln_id)}", timeout=timeout)
            resp.raise_for_status()
            return vuln_id, resp.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("OSV detalle %s fallo: %s", vuln_id, exc)
            return vuln_id, None

    with ThreadPoolExecutor(max_workers=HTTP_WORKERS) as pool:
        return {vid: data for vid, data in pool.map(fetch, sorted(ids)) if data}


def advisories_for(dep: Dependency, vuln_ids: list[str], details: dict[str, dict]) -> list[Advisory]:
    """Advisories unicos (GHSA primero; PYSEC/CVE alias del mismo se omiten)."""
    seen: set[str] = set()
    advisories: list[Advisory] = []
    for vuln_id in sorted(vuln_ids, key=lambda v: (not v.startswith("GHSA-"), v)):
        vuln = details.get(vuln_id)
        if vuln_id in seen:
            continue
        aliases = list((vuln or {}).get("aliases") or [])
        seen.update([vuln_id, *aliases])
        if not vuln:
            advisories.append(Advisory(vuln_id, [], "(sin detalle de OSV)", "UNKNOWN", None, f"https://osv.dev/vulnerability/{vuln_id}"))
            continue
        summary = (vuln.get("summary") or vuln.get("details") or "").strip().splitlines()[0:1]
        advisories.append(
            Advisory(
                id=vuln_id,
                aliases=aliases,
                summary=(summary[0] if summary else "")[:200],
                severity=severity_from_vuln(vuln),
                fixed=fixed_version_for(vuln, dep),
                url=f"https://osv.dev/vulnerability/{vuln_id}",
            )
        )
    return advisories


# --------------------------------------------------------------------------
# Ultimas versiones y metadata
# --------------------------------------------------------------------------


@dataclass
class PackageMeta:
    latest: Optional[str]
    github_repo: Optional[str]


def github_repo_from(urls: list[str]) -> Optional[str]:
    for url in urls:
        match = GITHUB_REPO_RE.search(url or "")
        if match and match.group(1).lower() not in ("sponsors", "orgs"):
            return f"{match.group(1)}/{match.group(2)}"
    return None


def fetch_meta(session: requests.Session, ecosystem: str, name: str, timeout: int) -> PackageMeta:
    try:
        if ecosystem == ECOSYSTEM_NPM:
            resp = session.get(f"{NPM_REGISTRY}/{name.replace('/', '%2F')}/latest", timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            repository = data.get("repository")
            repo_url = repository.get("url") if isinstance(repository, dict) else repository
            return PackageMeta(data.get("version"), github_repo_from([repo_url or "", data.get("homepage") or ""]))
        if ecosystem == ECOSYSTEM_PYPI:
            resp = session.get(f"{PYPI_URL}/{quote(name)}/json", timeout=timeout)
            resp.raise_for_status()
            info = resp.json().get("info") or {}
            urls = list((info.get("project_urls") or {}).values()) + [info.get("home_page") or ""]
            return PackageMeta(info.get("version"), github_repo_from(urls))
        if ecosystem == ECOSYSTEM_MAVEN:
            group, artifact = name.split(":", 1)
            resp = session.get(
                MAVEN_SEARCH_URL,
                params={"q": f'g:"{group}" AND a:"{artifact}"', "rows": 1, "wt": "json"},
                timeout=timeout,
            )
            resp.raise_for_status()
            docs = (resp.json().get("response") or {}).get("docs") or []
            return PackageMeta(docs[0].get("latestVersion") if docs else None, None)
    except (requests.RequestException, ValueError, KeyError) as exc:
        log.warning("No se pudo consultar la ultima version de %s (%s): %s", name, ecosystem, exc)
    return PackageMeta(None, None)


def fetch_all_meta(session: requests.Session, deps: list[Dependency], timeout: int) -> dict[tuple[str, str], PackageMeta]:
    keys = sorted({(d.ecosystem, d.name) for d in deps})
    with ThreadPoolExecutor(max_workers=HTTP_WORKERS) as pool:
        metas = pool.map(lambda key: fetch_meta(session, key[0], key[1], timeout), keys)
        return dict(zip(keys, metas))


# --------------------------------------------------------------------------
# Clasificacion
# --------------------------------------------------------------------------


def classify(dep: Dependency, advisories: list[Advisory], latest: Optional[str]) -> Optional[Finding]:
    eco = dep.ecosystem
    if advisories:
        current_major = major_of(eco, dep.version)
        fixes = [a.fixed for a in advisories]
        if all(fixes) and all(major_of(eco, f) == current_major for f in fixes):
            return Finding(dep, SECURITY_PATCH, vmax(eco, fixes), latest, advisories)
        if dep.dev and not any(fixes):
            # Sin ninguna version corregida y solo de desarrollo: no hay nada
            # que aplicar ni riesgo en produccion; se vigila sin abrir issue.
            return Finding(dep, SECURITY_WATCH, None, latest, advisories)
        return Finding(dep, SECURITY_MAJOR, vmax(eco, [f for f in fixes if f]), latest, advisories)
    # Desactualizadas: solo directas (las transitivas las mueve su paquete padre).
    if not dep.direct or not latest or not is_stable(eco, latest) or not vgt(eco, latest, dep.version):
        return None
    if major_of(eco, latest) != major_of(eco, dep.version):
        return Finding(dep, OUTDATED_MAJOR, latest, latest)
    return Finding(dep, OUTDATED_MINOR, latest, latest)


@dataclass
class RepoScan:
    repo: str
    full_name: str
    path: Optional[Path]
    branch: Optional[str]
    deps: list[Dependency] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


def scan(config: dict, profile: dict, session: requests.Session) -> tuple[list[RepoScan], dict]:
    timeout = http_timeout(config)
    workspace_dir = get_workspace_dir(config)
    scans: list[RepoScan] = []
    for repo in profile_repos(profile):
        full = repo_full_name(config, repo)
        synced = sync_workspace_repo(full, workspace_dir, resolve_work_branch(config, repo, preferred=profile))
        if not synced:
            scans.append(RepoScan(repo, full, None, None))
            continue
        path, branch = synced
        deps = build_inventory(repo, path)
        direct = sum(1 for d in deps if d.direct)
        log.info("%s (%s): %d dependencias directas, %d transitivas", full, branch, direct, len(deps) - direct)
        scans.append(RepoScan(repo, full, path, branch, deps))

    all_deps = [d for s in scans for d in s.deps]
    queryable = [d for d in all_deps if d.version]
    vuln_ids = osv_query(session, queryable, timeout)
    details = osv_details(session, {vid for ids in vuln_ids.values() for vid in ids}, timeout)
    by_dep = {id(queryable[i]): ids for i, ids in vuln_ids.items()}
    # Ultima version solo hace falta para directas y para transitivas vulnerables.
    metas = fetch_all_meta(session, [d for d in queryable if d.direct or id(d) in by_dep], timeout)
    for repo_scan in scans:
        findings: list[Finding] = []
        for dep in repo_scan.deps:
            if not dep.version:
                continue
            advisories = advisories_for(dep, by_dep.get(id(dep), []), details)
            finding = classify(dep, advisories, metas.get((dep.ecosystem, dep.name), PackageMeta(None, None)).latest)
            if finding:
                findings.append(finding)
        findings.sort(
            key=lambda f: (CATEGORY_ORDER.index(f.category), -SEVERITY_RANK.get(f.max_severity, 0), not f.dep.direct, f.dep.name)
        )
        # Un hallazgo por paquete (el issue se deduplica por repo+paquete): si
        # hay varias versiones instaladas, gana la peor y las demas se anotan.
        by_name: dict[tuple[str, str], Finding] = {}
        for finding in findings:
            key = (finding.dep.ecosystem, finding.dep.name)
            if key in by_name:
                by_name[key].other_versions.append(str(finding.dep.version))
            else:
                by_name[key] = finding
        repo_scan.findings = list(by_name.values())
    return scans, metas


# --------------------------------------------------------------------------
# Issues de GitHub
# --------------------------------------------------------------------------


def marker_prefix(repo: str, package: str) -> str:
    return f"<!-- radar:{repo}:{package}:"


def marker(finding: Finding) -> str:
    return f"{marker_prefix(finding.dep.repo, finding.dep.name)}{finding.advisory_key} -->"


def apply_command(finding: Finding) -> str:
    dep, target = finding.dep, finding.target
    if dep.ecosystem == ECOSYSTEM_NPM:
        if not dep.direct:
            return (f"npm update {dep.name}  # transitiva: debe quedar >= {target} en package-lock.json; "
                    f"si el rango del paquete padre no lo permite, agregar \"overrides\": {{\"{dep.name}\": \"^{target}\"}} en package.json")
        return f"npm install {'--save-dev ' if dep.dev else ''}{dep.name}@{target}"
    if dep.ecosystem == ECOSYSTEM_PYPI:
        return f"pip install '{dep.name}=={target}'  # y fijar `{dep.name}=={target}` en {dep.manifest}"
    return f"# pom.xml: <version>{target}</version> para {dep.name}"


def files_to_edit(dep: Dependency) -> list[str]:
    if dep.ecosystem == ECOSYSTEM_NPM:
        return ["package.json", "package-lock.json"]
    return [dep.manifest]


def patch_block(finding: Finding) -> str:
    """Bloque legible por maquina que el modo Desarrollo aplica de forma
    determinista (`modes.development.parse_radar_patch`), sin Aider ni Claude."""
    dep = finding.dep
    data = {
        "package": dep.name,
        "from": dep.version,
        "to": finding.target,
        "ecosystem": dep.ecosystem,
        "direct": dep.direct,
        "dev": dep.dev,
        "manifest": dep.manifest,
        "advisories": sorted(a.id for a in finding.advisories),
    }
    return f"```{PATCH_BLOCK_LANG}\n{json.dumps(data, ensure_ascii=False)}\n```"


def note_path(config: dict, specialist: str, ecosystem: str, package: str, version: str) -> Path:
    knowledge_dir = resolve_project_path(config.get("specialists", {}).get("knowledge_dir", DEFAULT_KNOWLEDGE_DIR))
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", package.lstrip("@")).strip("-")
    return knowledge_dir / specialist / f"{slug}-{version}.md"


def issue_title(finding: Finding) -> str:
    dep = finding.dep
    target = finding.target or "sin fix"
    kind = "parche de seguridad" if finding.category == SECURITY_PATCH else "seguridad (requiere major)"
    return f"[radar] {kind}: {dep.name} {dep.version} -> {target}"


def issue_body(config: dict, profile: dict, finding: Finding) -> str:
    dep = finding.dep
    scope = "desarrollo" if dep.dev else "produccion"
    others = f" (tambien instalada en: {', '.join(finding.other_versions)})" if finding.other_versions else ""
    lines = [
        marker(finding),
        f"## 📡 Radar de {profile.get('display_name') or profile['name'].capitalize()}",
        "",
        f"- **Paquete:** `{dep.name}` ({dep.ecosystem}, dependencia {dep.scope} de {scope}, `{dep.manifest}`)",
        f"- **Version actual:** `{dep.version}` (fuente: {dep.version_source}){others}",
        f"- **Version objetivo:** `{finding.target or 'no hay version corregida publicada'}`",
        f"- **Ultima publicada:** `{finding.latest or '?'}`",
        f"- **Severidad maxima:** {finding.max_severity}",
        "",
        "### Advisories",
    ]
    for adv in finding.advisories:
        aliases = f" ({', '.join(adv.aliases[:3])})" if adv.aliases else ""
        lines.append(f"- [{adv.id}]({adv.url}){aliases} — {adv.severity} — {adv.summary} — corregido en `{adv.fixed or 'sin fix'}`")
    if finding.category == SECURITY_PATCH:
        lines += [
            "",
            f"### Como aplicarlo (rama `{resolve_work_branch(config, dep.repo, preferred=profile)}`)",
            "```bash",
            apply_command(finding),
            "```",
            "El fix existe dentro del mismo major: no deberia haber cambios incompatibles. "
            "Verificar con build + lint + tests del perfil antes de comitear.",
            "",
            "El modo Desarrollo lo aplica de forma determinista (sin Aider ni Claude) con este bloque:",
            patch_block(finding),
            "",
            "Archivos:",
            *files_to_edit(dep),
            "",
        ]
    else:
        lines += [
            "",
            "### Por que necesita a un humano",
            "La correccion exige un nuevo major (o aun no hay version corregida): puede haber cambios "
            "incompatibles. No lleva `night-agent`, el especialista no lo aplica solo.",
        ]
        if finding.target:
            lines += ["", "Comando sugerido tras revisar la migracion:", "```bash", apply_command(finding), "```"]
        note = note_path(config, profile["name"], dep.ecosystem, dep.name, finding.target or finding.latest or "")
        if note.exists():
            content = note.read_text(encoding="utf-8")
            content = "\n".join(l for l in content.splitlines() if not l.lower().startswith("keywords:"))
            lines += ["", "### Cambios incompatibles y plan de migracion (Claude, modo semanal)", content.strip()]
    lines += ["", "_Generado por `batcave/radar.py`. El radar solo lee y abre issues: nunca comitea ni pushea._"]
    return "\n".join(lines)


def issue_labels(config: dict, profile: dict, finding: Finding) -> tuple[list[str], str]:
    """(labels a poner, label opuesto a quitar)."""
    night_label = config.get("github", {}).get("issue_label", "night-agent")
    first, opposite = (night_label, NEEDS_HUMAN_LABEL) if finding.category == SECURITY_PATCH else (NEEDS_HUMAN_LABEL, night_label)
    return [first, str(profile["label"]), DEPENDENCIES_LABEL], opposite


class IssueManager:
    """Crea o actualiza issues del radar, deduplicando por marcador oculto."""

    def __init__(self) -> None:
        from github import Github

        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise RuntimeError("GITHUB_TOKEN no esta definido: el radar no puede abrir issues")
        self.gh = Github(token, timeout=DEFAULT_HTTP_TIMEOUT)
        self._repos: dict = {}
        self._issues: dict[str, list] = {}

    def _repo(self, full: str):
        if full not in self._repos:
            self._repos[full] = self.gh.get_repo(full)
        return self._repos[full]

    def _open_issues(self, full: str) -> list:
        if full not in self._issues:
            self._issues[full] = [i for i in self._repo(full).get_issues(state="open") if i.pull_request is None]
        return self._issues[full]

    def _ensure_labels(self, full: str, labels: list[str]) -> None:
        from github import GithubException

        repo = self._repo(full)
        for name in labels:
            try:
                repo.get_label(name)
            except GithubException:
                try:
                    repo.create_label(name=name, color="0e8a16" if name == DEPENDENCIES_LABEL else "fbca04")
                except GithubException as exc:
                    log.warning("No se pudo crear el label %s en %s: %s", name, full, exc)

    def upsert(self, full: str, finding: Finding, title: str, body: str, labels: list[str], remove: str) -> str:
        """Crea el issue o actualiza el abierto con el mismo repo+paquete.
        `remove` es el label opuesto (`night-agent` <-> `needs-human`): si
        el hallazgo cambio de categoria, se quita para que el especialista
        no intente aplicar solo un major. Devuelve 'creado'/'actualizado'/
        'sin cambios' + URL."""
        self._ensure_labels(full, labels)
        prefix = marker_prefix(finding.dep.repo, finding.dep.name)
        for issue in self._open_issues(full):
            if prefix not in (issue.body or ""):
                continue
            current_labels = {l.name for l in issue.labels}
            if (issue.body or "") == body and issue.title == title and set(labels) <= current_labels and remove not in current_labels:
                return f"sin cambios {issue.html_url}"
            issue.edit(title=title, body=body, labels=sorted((current_labels - {remove}) | set(labels)))
            return f"actualizado {issue.html_url}"
        issue = self._repo(full).create_issue(title=title, body=body, labels=labels)
        self._issues[full].append(issue)
        return f"creado {issue.html_url}"

    def close_watched(self, full: str, finding: Finding) -> Optional[str]:
        """Cierra (con `WATCH_CLOSE_COMMENT`) el issue abierto del mismo
        repo+paquete si el hallazgo paso a `security_watch`. Devuelve la URL
        cerrada o None si no habia issue."""
        prefix = marker_prefix(finding.dep.repo, finding.dep.name)
        for issue in list(self._open_issues(full)):
            if prefix not in (issue.body or ""):
                continue
            issue.create_comment(WATCH_CLOSE_COMMENT)
            issue.edit(state="closed", state_reason="not_planned")
            self._issues[full].remove(issue)
            return issue.html_url
        return None


# --------------------------------------------------------------------------
# Estado, alertas y digest
# --------------------------------------------------------------------------


def state_path(specialist: str) -> Path:
    return MEMORY_DIR / f"radar_{specialist}_state.json"


def load_state(specialist: str) -> dict:
    try:
        return json.loads(state_path(specialist).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"advisories": {}, "alerted": {}, "events": [], "notes": []}


def save_state(specialist: str, state: dict) -> None:
    cutoff = (datetime.now() - timedelta(days=EVENTS_RETENTION_DAYS)).isoformat()
    state["events"] = [e for e in state.get("events", []) if e.get("date", "") >= cutoff]
    state["notes"] = [n for n in state.get("notes", []) if n.get("date", "") >= cutoff]
    path = state_path(specialist)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def advisory_state_key(finding: Finding, advisory: Advisory) -> str:
    return f"{finding.dep.repo}:{finding.dep.name}:{advisory.id}"


def update_state(state: dict, scans: list[RepoScan]) -> None:
    """Registra eventos `found`/`fixed` (solo para repos inventariados hoy)."""
    now = datetime.now().isoformat(timespec="seconds")
    ok_repos = {s.repo for s in scans if s.path}
    current: dict[str, dict] = {}
    for repo_scan in scans:
        for finding in repo_scan.findings:
            for adv in finding.advisories:
                current[advisory_state_key(finding, adv)] = {"severity": adv.severity, "package": finding.dep.name}
    previous = state.setdefault("advisories", {})
    events = state.setdefault("events", [])
    for key, meta in current.items():
        if key not in previous:
            events.append({"date": now, "type": "found", "key": key, "severity": meta["severity"]})
            previous[key] = {**meta, "first_seen": now}
    for key in list(previous):
        if key not in current and key.split(":", 1)[0] in ok_repos:
            events.append({"date": now, "type": "fixed", "key": key, "severity": previous[key].get("severity")})
            del previous[key]
    state["alerted"] = {k: v for k, v in state.get("alerted", {}).items() if k in previous}
    state["last_run"] = now


class Notifier:
    """Envia mensajes HTML a Alfred; si faltan credenciales solo loguea."""

    def __init__(self) -> None:
        token, self.chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        self.bot = None
        if token and self.chat_id:
            from telegram import Bot

            self.bot = Bot(token=token)
        else:
            log.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID no definidos: no se avisara a Alfred")

    async def send(self, text: str) -> None:
        if not self.bot:
            return
        from telegram.constants import ParseMode
        from telegram.error import TelegramError

        try:
            await asyncio.wait_for(
                self.bot.send_message(chat_id=self.chat_id, text=text[:4000], parse_mode=ParseMode.HTML), timeout=30
            )
        except (TelegramError, asyncio.TimeoutError) as exc:
            log.error("No se pudo enviar mensaje a Telegram: %s", exc)


def alert_candidates(config: dict, scans: list[RepoScan], state: dict) -> list[tuple[Finding, Advisory]]:
    severities = {s.upper() for s in radar_config(config).get("alert_severities", DEFAULT_ALERT_SEVERITIES)}
    alerted = state.setdefault("alerted", {})
    found: list[tuple[Finding, Advisory]] = []
    for repo_scan in scans:
        for finding in repo_scan.findings:
            if finding.dep.dev:
                continue
            for adv in finding.advisories:
                if adv.severity in severities and advisory_state_key(finding, adv) not in alerted:
                    found.append((finding, adv))
    return found


def format_alert(profile: dict, items: list[tuple[Finding, Advisory]], issue_urls: dict[str, str]) -> str:
    lines = [f"📡 <b>Radar de {html.escape(profile.get('display_name') or profile['name'])}</b>", "Vulnerabilidades en dependencias de produccion:"]
    for finding, adv in items:
        dep = finding.dep
        accion = "parche automatico esta noche" if finding.category == SECURITY_PATCH else "requiere humano (major)"
        url = issue_urls.get(f"{dep.repo}:{dep.name}")
        lines.append(
            f"• <b>{html.escape(adv.severity)}</b> <code>{html.escape(dep.name)}</code> {html.escape(str(dep.version))}"
            f" → {html.escape(finding.target or 'sin fix')} en <code>{html.escape(dep.repo)}</code>"
            f" ({dep.scope}, {html.escape(adv.id)}) — {accion}" + (f" — <a href=\"{html.escape(url)}\">issue</a>" if url else "")
        )
    return "\n".join(lines)


def format_digest(profile: dict, scans: list[RepoScan], state: dict, new_notes: list[str]) -> str:
    week_ago = (datetime.now() - timedelta(days=7)).isoformat()
    events = [e for e in state.get("events", []) if e.get("date", "") >= week_ago]
    found = [e for e in events if e["type"] == "found"]
    fixed = [e for e in events if e["type"] == "fixed"]
    findings = [f for s in scans for f in s.findings]
    by_cat = {cat: [f for f in findings if f.category == cat] for cat in CATEGORY_ORDER}
    name = html.escape(profile.get("display_name") or profile["name"])
    lines = [
        f"📡 <b>Radar de {name} — resumen semanal</b>",
        f"Vulnerabilidades (7 dias): {len(found)} nuevas, {len(fixed)} corregidas",
        f"Abiertas: {len(by_cat[SECURITY_PATCH])} parche (night-agent), {len(by_cat[SECURITY_MAJOR])} major (needs-human), "
        f"{len(by_cat[SECURITY_WATCH])} sin parche (vigilando)",
    ]
    failed = [s.repo for s in scans if not s.path]
    if failed:
        lines.append("⚠️ Sin inventario: " + ", ".join(f"<code>{html.escape(r)}</code>" for r in failed))
    pending = by_cat[SECURITY_MAJOR] + by_cat[OUTDATED_MAJOR]
    if pending:
        lines.append(f"\n<b>Majors pendientes ({len(pending)})</b>")
        for f in pending[:DIGEST_MAX_ITEMS]:
            tag = "🔒 " if f.category == SECURITY_MAJOR else ""
            lines.append(
                f"• {tag}<code>{html.escape(f.dep.name)}</code> {html.escape(str(f.dep.version))} → "
                f"{html.escape(str(f.target or f.latest))} (<code>{html.escape(f.dep.repo)}</code>)"
            )
        if len(pending) > DIGEST_MAX_ITEMS:
            lines.append(f"… y {len(pending) - DIGEST_MAX_ITEMS} mas")
    watched = by_cat[SECURITY_WATCH]
    if watched:
        lines.append(f"\n<b>Sin parche disponible (vigilando) ({len(watched)})</b>")
        for f in watched[:DIGEST_MAX_ITEMS]:
            lines.append(
                f"• [{html.escape(f.max_severity)}] <code>{html.escape(f.dep.name)}</code> {html.escape(str(f.dep.version))} "
                f"(<code>{html.escape(f.dep.repo)}</code>, {f.dep.scope} dev; "
                f"{html.escape(', '.join(a.id for a in f.advisories))})"
            )
        if len(watched) > DIGEST_MAX_ITEMS:
            lines.append(f"… y {len(watched) - DIGEST_MAX_ITEMS} mas")
    lines.append(f"Menores/parches desactualizados: {len(by_cat[OUTDATED_MINOR])}")
    if new_notes:
        lines.append(f"\n<b>Notas de conocimiento nuevas ({len(new_notes)})</b>")
        lines += [f"• <code>{html.escape(n)}</code>" for n in new_notes]
    return "\n".join(lines)


def write_report(profile: dict, scans: list[RepoScan], weekly: bool) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"radar-{profile['name']}-{datetime.now():%Y-%m-%d}{'-semanal' if weekly else ''}.md"
    path.write_text(mask_secrets(render_text(profile, scans, markdown=True)), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Modo semanal: notas de Claude
# --------------------------------------------------------------------------


def github_headers() -> dict:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_release_notes(session: requests.Session, gh_repo: str, ecosystem: str, current: str, target: str, timeout: int) -> str:
    """Release notes de GitHub entre `current` (exclusivo) y `target`; si no
    hay releases, el inicio del CHANGELOG.md."""
    parts: list[str] = []
    try:
        resp = session.get(f"{GITHUB_API}/repos/{gh_repo}/releases", params={"per_page": 100}, headers=github_headers(), timeout=timeout)
        resp.raise_for_status()
        releases = []
        for rel in resp.json():
            match = VERSION_IN_TAG_RE.search(rel.get("tag_name") or "")
            version = match.group(1) if match else None
            if version and is_stable(ecosystem, version) and vgt(ecosystem, version, current) and not vgt(ecosystem, version, target):
                releases.append((version_key(ecosystem, version)[:7], rel.get("tag_name"), rel.get("body") or ""))
        for _, tag, body in sorted(releases):
            parts.append(f"## {tag}\n{body.strip()[:RELEASE_NOTE_MAX_CHARS]}")
    except (requests.RequestException, ValueError) as exc:
        log.warning("No se pudieron leer releases de %s: %s", gh_repo, exc)
    text = "\n\n".join(parts)
    if not text:
        try:
            resp = session.get(
                f"{GITHUB_API}/repos/{gh_repo}/contents/CHANGELOG.md",
                headers={**github_headers(), "Accept": "application/vnd.github.raw"},
                timeout=timeout,
            )
            if resp.ok:
                text = "CHANGELOG.md:\n" + resp.text
        except requests.RequestException as exc:
            log.warning("No se pudo leer CHANGELOG.md de %s: %s", gh_repo, exc)
    if len(text) > RELEASE_NOTES_MAX_CHARS:
        # Lo mas importante para una migracion es el salto de major: se conserva el final.
        text = "…(recortado)…\n" + text[-RELEASE_NOTES_MAX_CHARS:]
    return text


def find_usages(repo_path: Path, dep: Dependency) -> list[str]:
    if dep.ecosystem == ECOSYSTEM_NPM:
        pattern = re.compile(r"""(?:from\s+|require\(\s*|import\(\s*)['"]""" + re.escape(dep.name) + r"""(?:/[^'"]*)?['"]""")
    elif dep.ecosystem == ECOSYSTEM_PYPI:
        module = re.escape(re.sub(r"[-.]", "_", dep.name).lower())
        pattern = re.compile(rf"^\s*(?:from|import)\s+{module}\b", re.IGNORECASE)
    else:
        pattern = re.compile(re.escape(dep.name.split(":")[0]))
    hits: list[str] = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if not name.endswith(SOURCE_SUFFIXES):
                continue
            file_path = Path(root) / name
            try:
                for number, line in enumerate(file_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if pattern.search(line):
                        hits.append(f"{file_path.relative_to(repo_path)}:{number}: {line.strip()[:160]}")
                        if len(hits) >= MAX_USAGE_LINES:
                            return hits
            except OSError:
                continue
    return hits


def build_summary_prompt(profile: dict, finding: Finding, target: str, notes: str, usages: dict[str, list[str]]) -> str:
    dep = finding.dep
    usage_text = "\n".join(f"[{repo}]\n" + ("\n".join(lines) or "(sin imports directos encontrados)") for repo, lines in usages.items())
    advisories = "\n".join(f"- {a.id} ({a.severity}): {a.summary} — fix en {a.fixed or 'sin fix'}" for a in finding.advisories)
    return "\n\n".join(
        part
        for part in [
            f"Eres el radar tecnologico de {profile.get('display_name') or profile['name']} "
            f"(rol {profile.get('role')}, stack: {', '.join(profile.get('frameworks') or [])}).",
            f"Paquete {dep.ecosystem} `{dep.name}`: usamos `{dep.version}`, objetivo `{target}`.",
            f"Advisories de seguridad:\n{advisories}" if advisories else "",
            f"Donde lo usamos (repos en el directorio actual o sus vecinos):\n{usage_text}",
            f"Release notes / changelog:\n{notes or '(no se encontraron release notes; usa tu conocimiento y dilo)'}",
            "Escribe en espanol una nota Markdown concisa (max 50 lineas) con exactamente estas secciones:\n"
            "## Que cambio\n## Cambios incompatibles\n## Pasos de migracion\n## Relevancia para nuestro codigo\n"
            "En 'Relevancia' cita archivos concretos de los usos de arriba y di si la migracion es urgente. "
            "Puedes leer archivos del repo para precisar, pero NO edites nada ni ejecutes comandos. "
            "Responde SOLO con la nota, sin preambulo.",
        ]
        if part
    )


def run_claude_summary(config: dict, prompt: str, cwd: Path) -> Optional[str]:
    claude_bin = config.get("models", {}).get("claude", "claude")
    if shutil.which(claude_bin) is None:
        log.error("Claude no esta disponible ('%s' no encontrado)", claude_bin)
        return None
    timeout = int(radar_config(config).get("claude_timeout", DEFAULT_CLAUDE_TIMEOUT))
    cmd = [
        claude_bin, "-p", "--output-format", "text",
        "--allowedTools", "Read,Grep,Glob",
        "--disallowedTools", "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch",
    ]
    try:
        proc = subprocess.run(cmd, cwd=cwd, input=prompt, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.error("claude excedio el timeout de %ss", timeout)
        return None
    except OSError as exc:
        log.error("Fallo ejecutando claude: %s", exc)
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        log.error("claude devolvio codigo %s: %s", proc.returncode, mask_secrets(proc.stderr[-400:]))
        return None
    return mask_secrets(proc.stdout.strip())


def weekly_candidates(scans: list[RepoScan]) -> list[tuple[Finding, str, list[RepoScan]]]:
    """(hallazgo representativo, version objetivo, repos afectados): un solo
    resumen por paquete+version aunque aparezca en varios repos."""
    grouped: dict[tuple, tuple[Finding, str, list[RepoScan]]] = {}
    for repo_scan in scans:
        for finding in repo_scan.findings:
            if finding.category not in (SECURITY_MAJOR, OUTDATED_MAJOR):
                continue
            target = finding.target or finding.latest
            if not target:
                continue
            key = (finding.dep.ecosystem, finding.dep.name, target)
            if key in grouped:
                grouped[key][2].append(repo_scan)
            else:
                grouped[key] = (finding, target, [repo_scan])
    return sorted(grouped.values(), key=lambda g: (CATEGORY_ORDER.index(g[0].category), -SEVERITY_RANK.get(g[0].max_severity, 0)))


def run_weekly_summaries(
    config: dict, profile: dict, scans: list[RepoScan], metas: dict, session: requests.Session, dry_run: bool
) -> list[str]:
    cap = int(radar_config(config).get("max_weekly_summaries", DEFAULT_MAX_WEEKLY_SUMMARIES))
    timeout = http_timeout(config)
    created: list[str] = []
    used = 0
    for finding, target, repo_scans in weekly_candidates(scans):
        dep = finding.dep
        path = note_path(config, profile["name"], dep.ecosystem, dep.name, target)
        if path.exists():
            log.info("Nota ya existe, se omite: %s", path.name)
            continue
        if used >= cap:
            log.info("Tope de %d resumenes semanales alcanzado; %s queda para la proxima semana", cap, dep.name)
            continue
        used += 1
        gh_repo = metas.get((dep.ecosystem, dep.name), PackageMeta(None, None)).github_repo
        notes = fetch_release_notes(session, gh_repo, dep.ecosystem, dep.version, target, timeout) if gh_repo else ""
        if dry_run:
            print(f"  [semanal] {dep.name} {dep.version} -> {target}: release notes de {gh_repo or '?'} "
                  f"({len(notes)} caracteres) -> {path.relative_to(BASE_DIR)}")
            continue
        usages = {s.repo: find_usages(s.path, dep) for s in repo_scans if s.path}
        prompt = build_summary_prompt(profile, finding, target, notes, usages)
        summary = run_claude_summary(config, prompt, repo_scans[0].path)
        if not summary:
            continue
        keywords = {dep.name.lstrip("@").replace("/", "-").lower(), *re.split(r"[/:@-]+", dep.name.lower())}
        header = [
            f"keywords: {', '.join(sorted(k for k in keywords if len(k) >= 3))}",
            f"# {dep.name} {dep.version} -> {target} ({dep.ecosystem})",
            f"_Radar {datetime.now():%Y-%m-%d} · repos: {', '.join(s.repo for s in repo_scans)} · fuente: {gh_repo or 'sin repo GitHub'}_",
            "",
        ]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(header) + summary + "\n", encoding="utf-8")
        log.info("Nota de conocimiento creada: %s", path)
        created.append(path.name)
    return created


# --------------------------------------------------------------------------
# Salida
# --------------------------------------------------------------------------


def render_text(profile: dict, scans: list[RepoScan], markdown: bool = False) -> str:
    out: list[str] = []
    title = f"Radar de {profile.get('display_name') or profile['name']} — {datetime.now():%Y-%m-%d %H:%M}"
    out.append(f"# {title}" if markdown else f"=== {title} ===")
    for repo_scan in scans:
        out.append("")
        out.append(f"## repo: {repo_scan.repo}" if markdown else f"--- repo: {repo_scan.repo} ---")
        out.append(f"  {repo_scan.full_name} [{repo_scan.branch or 'SIN WORKSPACE'}]")
        if not repo_scan.path:
            out.append("  (no se pudo clonar/sincronizar; se omite)")
            continue
        direct = [d for d in repo_scan.deps if d.direct]
        out.append(f"  Inventario ({len(direct)} dependencias directas):")
        for dep in sorted(direct, key=lambda d: (d.ecosystem, d.dev, d.name.lower())):
            scope = "dev " if dep.dev else "prod"
            out.append(f"    {dep.ecosystem:<5} {scope} {dep.name:<45} {dep.version or '-':<16} ({dep.version_source}, {dep.manifest})")
        out.append(f"  Transitivas: {len(repo_scan.deps) - len(direct)} (solo se revisan vulnerabilidades)")
        out.append(f"  Hallazgos ({len(repo_scan.findings)}):")
        if not repo_scan.findings:
            out.append("    (ninguno)")
        for finding in repo_scan.findings:
            dep = finding.dep
            sev = f" [{finding.max_severity}]" if finding.advisories else ""
            ids = f" {', '.join(a.id for a in finding.advisories)}" if finding.advisories else ""
            others = f", tambien {'/'.join(finding.other_versions)}" if finding.other_versions else ""
            out.append(
                f"    {finding.category:<15}{sev} {dep.name} {dep.version} -> {finding.target or 'sin fix'}"
                f" ({dep.scope}, ultima {finding.latest or '?'}{', dev' if dep.dev else ''}{others}){ids}"
            )
    findings = [f for s in scans for f in s.findings]
    counts = ", ".join(f"{cat}={sum(1 for f in findings if f.category == cat)}" for cat in CATEGORY_ORDER)
    out += ["", f"Total: {counts}"]
    return "\n".join(out)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


async def run(specialist: str, dry_run: bool, weekly: bool) -> int:
    load_env()
    config = load_config()
    specialists = load_specialists(config)
    if specialist not in specialists:
        log.error("No existe el especialista '%s' (disponibles: %s)", specialist, ", ".join(specialists))
        return 2
    profile = specialists[specialist]
    log.info("📡 Radar de %s (%s%s)", specialist_display(profile), "dry-run" if dry_run else "real", ", semanal" if weekly else "")

    session = requests.Session()
    session.headers["User-Agent"] = "batcave-radar/1.0"
    scans, metas = scan(config, profile, session)

    if dry_run:
        print(render_text(profile, scans))
        if weekly:
            print("\nResumenes semanales que se pedirian a Claude:")
            run_weekly_summaries(config, profile, scans, metas, session, dry_run=True)
        return 0

    state = load_state(specialist)
    update_state(state, scans)

    new_notes: list[str] = []
    if weekly:
        new_notes = run_weekly_summaries(config, profile, scans, metas, session, dry_run=False)
        state.setdefault("notes", []).extend({"date": datetime.now().isoformat(timespec="seconds"), "file": n} for n in new_notes)

    issue_urls: dict[str, str] = {}
    try:
        issues = IssueManager()
    except Exception as exc:  # sin token o sin PyGithub: se sigue con alertas/digest
        log.error("No se pueden gestionar issues: %s", exc)
        issues = None
    if issues:
        for repo_scan in scans:
            for finding in repo_scan.findings:
                if finding.category == SECURITY_WATCH:
                    try:
                        closed = issues.close_watched(repo_scan.full_name, finding)
                        if closed:
                            log.info("Issue %s:%s cerrado (sin parche, solo dev): %s", repo_scan.repo, finding.dep.name, closed)
                    except Exception as exc:
                        log.error("No se pudo cerrar el issue de %s en %s: %s", finding.dep.name, repo_scan.full_name, exc)
                    continue
                if finding.category not in (SECURITY_PATCH, SECURITY_MAJOR):
                    continue
                labels, opposite = issue_labels(config, profile, finding)
                try:
                    result = issues.upsert(
                        repo_scan.full_name, finding, issue_title(finding), issue_body(config, profile, finding), labels, opposite
                    )
                    issue_urls[f"{finding.dep.repo}:{finding.dep.name}"] = result.split(" ")[-1]
                    log.info("Issue %s:%s -> %s", repo_scan.repo, finding.dep.name, result)
                except Exception as exc:
                    log.error("No se pudo crear/actualizar el issue de %s en %s: %s", finding.dep.name, repo_scan.full_name, exc)

    notifier = Notifier()
    alerts = alert_candidates(config, scans, state)
    if alerts:
        await notifier.send(format_alert(profile, alerts, issue_urls))
        now = datetime.now().isoformat(timespec="seconds")
        for finding, adv in alerts:
            state["alerted"][advisory_state_key(finding, adv)] = now
    if weekly:
        await notifier.send(format_digest(profile, scans, state, new_notes))
        state["last_weekly"] = datetime.now().isoformat(timespec="seconds")

    save_state(specialist, state)
    report = write_report(profile, scans, weekly)
    log.info("Reporte: %s", report)
    print(render_text(profile, scans))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Radar tecnologico de un especialista de la Batifamilia")
    parser.add_argument("--specialist", required=True, help="nombre del perfil en batcave/specialists/ (p. ej. nightwing)")
    parser.add_argument("--dry-run", action="store_true", help="solo inventario y hallazgos: sin issues, Telegram, Claude ni estado")
    parser.add_argument("--weekly", action="store_true", help="modo semanal: notas de Claude para majors + resumen a Alfred")
    args = parser.parse_args()
    setup_logging(quiet_stdout=args.dry_run)
    sys.exit(asyncio.run(run(args.specialist, args.dry_run, args.weekly)))


if __name__ == "__main__":
    main()
