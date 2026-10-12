"""Collect skill evidence from public GitHub repos into the profile.

A repo is evidence for a skill when the skill is a significant language in it,
or a manifest in it (package.json, requirements.txt, Cargo.toml, ...) depends
on a library we recognize. Only repos whose `pushed_at` changed are re-scanned.
"""

import json
import logging
import re
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, cast

from . import db, profile

log = logging.getLogger("tailor")

API = "https://api.github.com"
MIN_LANGUAGE_SHARE = 0.10
SCANNER_VERSION = 2  # bump when the mappings below change, to force a re-scan
PAGE_SIZE = 100

LANGUAGES = {
    "Python": "Python",
    "C": "C",
    "C++": "C++",
    "Java": "Java",
    "JavaScript": "JavaScript",
    "TypeScript": "TypeScript",
    "Rust": "Rust",
    "Go": "Go",
    "R": "R",
    "HTML": "HTML",
    "CSS": "CSS",
    "Shell": "Bash",
    "Dockerfile": "Docker",
    "CMake": "CMake",
    "Jupyter Notebook": "Jupyter",
    "Kotlin": "Kotlin",
    "C#": "C#",
    "SQL": "SQL",
    "PLpgSQL": "PostgreSQL",
    "Vue": "Vue",
    "Svelte": "Svelte",
    "Lua": "Lua",
}

# Lowercase package name -> skill(s). Unrecognized packages are ignored.
# Map only packages that show you *built* with the skill (a server framework
# shows REST API work; an HTTP client like `requests` does not).
PACKAGES: dict[str, str | tuple[str, ...]] = {
    "react": "React",
    "vue": "Vue",
    "svelte": "Svelte",
    "next": "Next.js",
    "express": ("Express", "REST APIs"),
    "@nestjs/core": "NestJS",
    "typescript": "TypeScript",
    "firebase": "Firebase",
    "firebase-admin": "Firebase",
    "pg": "PostgreSQL",
    "mysql2": "MySQL",
    "mongoose": "MongoDB",
    "mongodb": "MongoDB",
    "prisma": "Prisma",
    "@prisma/client": "Prisma",
    "jest": "Jest",
    "vitest": "Vitest",
    "vite": "Vite",
    "webpack": "Webpack",
    "three": "Three.js",
    "tailwindcss": "Tailwind CSS",
    "socket.io": "Socket.IO",
    "electron": "Electron",
    "@playwright/test": "Playwright",
    "cypress": "Cypress",
    "graphql": "GraphQL",
    "redux": "Redux",
    "@reduxjs/toolkit": "Redux",
    "bun-types": "Bun",
    "@types/bun": "Bun",
    "phaser": "Phaser",
    "torch": "PyTorch",
    "tensorflow": "TensorFlow",
    "keras": "Keras",
    "numpy": "NumPy",
    "pandas": "Pandas",
    "scikit-learn": "scikit-learn",
    "opencv-python": "OpenCV",
    "opencv-contrib-python": "OpenCV",
    "matplotlib": "Matplotlib",
    "flask": "Flask",
    "fastapi": ("FastAPI", "REST APIs"),
    "django": "Django",
    "sqlalchemy": "SQLAlchemy",
    "pytest": "pytest",
    "rclpy": "ROS2",
    "selenium": "Selenium",
    "pyautogui": "PyAutoGUI",
    "boto3": "AWS",
    "anthropic": "Claude API",
    "openai": "OpenAI API",
    "streamlit": "Streamlit",
    "pydantic": "Pydantic",
    "scipy": "SciPy",
    "pygame": "Pygame",
    "tokio": "Tokio",
    "serde": "Serde",
    "bevy": "Bevy",
    "wgpu": "wgpu",
    "clap": "clap",
    "actix-web": "Actix Web",
    "axum": "Axum",
}

# File path patterns -> skill, for tools that show up as files rather than packages.
FILES = [
    (re.compile(r"(^|/)Dockerfile$|(^|/)docker-compose\.ya?ml$|(^|/)compose\.ya?ml$"), "Docker"),
    (re.compile(r"^\.github/workflows/.+\.ya?ml$"), "GitHub Actions"),
    (re.compile(r"(^|/)package\.xml$"), "ROS2"),
    (re.compile(r"(^|/)CMakeLists\.txt$"), "CMake"),
    (re.compile(r"\.ipynb$"), "Jupyter"),
    (re.compile(r"(^|/)serverless\.ya?ml$|(^|/)template\.ya?ml$"), "AWS"),
    (re.compile(r"(^|/)firebase\.json$"), "Firebase"),
    (re.compile(r"\.tex$"), "LaTeX"),
]

MANIFESTS = re.compile(r"(^|/)(package\.json|requirements[^/]*\.txt|pyproject\.toml|Cargo\.toml)$")
SKIP = re.compile(r"(^|/)(node_modules|vendor|\.venv|venv|dist|build|target)/")


def _get_text(url: str) -> str:
    if not url.startswith(("https://api.github.com/", "https://raw.githubusercontent.com/")):
        msg = f"unexpected GitHub URL: {url}"
        raise ValueError(msg)
    headers = {"User-Agent": "resume-tailor", "Accept": "application/vnd.github+json"}
    req = urllib.request.Request(url, headers=headers)  # noqa: S310
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
        return str(r.read().decode("utf-8", "replace"))


def _get_json(url: str) -> Any:  # noqa: ANN401
    return json.loads(_get_text(url))


def _packages(path: str, text: str) -> set[str]:
    name = path.rsplit("/", 1)[-1]
    try:
        if name == "package.json":
            d = json.loads(text)
            return {k.lower() for sec in ("dependencies", "devDependencies") for k in d.get(sec, {})}
        if name == "Cargo.toml":
            return {k.lower() for k in tomllib.loads(text).get("dependencies", {})}
        if name == "pyproject.toml":
            deps = tomllib.loads(text).get("project", {}).get("dependencies", [])
            return {re.split(r"[\s<>=~!;\[]", d, maxsplit=1)[0].lower() for d in deps}
        return {
            re.split(r"[\s<>=~!;\[]", line, maxsplit=1)[0].lower()  # requirements*.txt
            for line in text.splitlines()
            if line.strip() and not line.startswith(("#", "-"))
        }
    except (ValueError, tomllib.TOMLDecodeError):
        return set()


def scan_repo(owner: str, repo: dict[str, Any]) -> dict[str, str]:
    """Skill -> one-line reason, for one repo."""
    name, branch = repo["name"], repo["default_branch"]
    found: dict[str, str] = {}

    langs = cast("dict[str, int]", _get_json(f"{API}/repos/{owner}/{name}/languages"))
    total = sum(langs.values()) or 1
    for lang, size in langs.items():
        if lang in LANGUAGES and size / total >= MIN_LANGUAGE_SHARE:
            found.setdefault(LANGUAGES[lang], f"{size / total:.0%} of code")

    tree = _get_json(f"{API}/repos/{owner}/{name}/git/trees/{branch}?recursive=1").get("tree", [])
    paths = [t["path"] for t in tree if t["type"] == "blob" and not SKIP.search(t["path"])]
    for path in paths:
        for pattern, skill in FILES:
            if pattern.search(path):
                found.setdefault(skill, path)
        if MANIFESTS.search(path):
            quoted = urllib.parse.quote(path)
            text = _get_text(f"https://raw.githubusercontent.com/{owner}/{name}/{branch}/{quoted}")
            for pkg in _packages(path, text):
                skills = PACKAGES.get(pkg, ())
                for skill in (skills,) if isinstance(skills, str) else skills:
                    found.setdefault(skill, f"{pkg} in {path}")
    return found


def refresh(owner: str) -> dict[str, Any]:
    cache = load()
    repos = [r for r in _list_repos(owner) if not r["fork"]]
    for r in repos:
        cached = cache.get(r["name"])
        if cached and cached["pushed_at"] == r["pushed_at"] and cached.get("scanner") == SCANNER_VERSION:
            continue
        log.info("scanning %s", r["name"])
        try:
            skills = scan_repo(owner, r)
        except (urllib.error.URLError, ValueError) as e:  # empty repo (409), rate limit, bad JSON
            log.warning("skipping %s: %s", r["name"], e)
            continue
        cache[r["name"]] = {
            "url": r["html_url"],
            "pushed_at": r["pushed_at"],
            "scanner": SCANNER_VERSION,
            "description": r["description"],
            "skills": skills,
        }
        _save(cache)  # keep progress if a later repo fails or the rate limit hits
    for gone in cache.keys() - {r["name"] for r in repos}:
        del cache[gone]
    _save(cache)
    return cache


def _list_repos(owner: str) -> list[dict[str, Any]]:
    repos: list[dict[str, Any]] = []
    for page in range(1, 50):
        batch = _get_json(f"{API}/users/{owner}/repos?per_page={PAGE_SIZE}&page={page}")
        repos += batch
        if len(batch) < PAGE_SIZE:
            break
    return repos


def _save(cache: dict[str, Any]) -> None:
    profile.put(db.connect(), "github", json.dumps(dict(sorted(cache.items())), indent=1) + "\n")


def load() -> dict[str, Any]:
    return profile.load("github")
