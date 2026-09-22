"""Deciding how to build a repository nobody has described.

The order of the rules is the whole design, and it runs from most explicit to
least:

1. A `Dockerfile` in the app directory. The repository has said exactly what it
   wants and the platform has nothing to add.
2. A `forge.json`. The repository has described itself in the platform's own
   terms.
3. Project settings stored in Forge. The owner overrode detection in the UI.
4. Framework signatures — a config file, then a dependency, then a lockfile.
5. A bare `index.html`.

Nothing here guesses when it could read. The Next.js rules, for instance, do
not assume standalone output: they open `next.config` and look, because
guessing wrong produces an image that builds cleanly and then 404s on every
asset, which is far worse than failing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from forge.domain import dockerfiles as gen
from forge.domain.buildplan import DEFAULT_PORT, BuildPlan
from forge.domain.dockerfiles import BUN, NPM, NPM_NO_LOCK, PNPM, YARN, NodeToolchain
from forge.domain.errors import DetectionFailed

NEXT_CONFIGS = ("next.config.js", "next.config.mjs", "next.config.cjs", "next.config.ts")
ASTRO_CONFIGS = ("astro.config.mjs", "astro.config.js", "astro.config.ts")
NUXT_CONFIGS = ("nuxt.config.ts", "nuxt.config.js", "nuxt.config.mjs")
SVELTE_CONFIGS = ("svelte.config.js", "svelte.config.mjs")
VITE_CONFIGS = ("vite.config.js", "vite.config.mjs", "vite.config.ts")


@dataclass(frozen=True, slots=True)
class Overrides:
    """What the project's settings say, overriding anything detected."""

    framework: str | None = None
    install_command: str | None = None
    build_command: str | None = None
    start_command: str | None = None
    port: int | None = None


def detect(app_dir: Path, overrides: Overrides | None = None) -> BuildPlan:
    """Produce a complete build plan for `app_dir`, or explain why not."""
    over = overrides or Overrides()
    port = over.port or DEFAULT_PORT

    dockerfile = app_dir / "Dockerfile"
    if dockerfile.is_file():
        return _from_repo_dockerfile(dockerfile, port=over.port)

    manifest = _read_json(app_dir / "forge.json")
    if manifest:
        over = _merge_manifest(over, manifest)
        port = over.port or DEFAULT_PORT

    pkg = _read_json(app_dir / "package.json")
    if pkg is not None:
        return _detect_node(app_dir, pkg, over, port)

    if _has_any(app_dir, ("requirements.txt", "pyproject.toml", "Pipfile")):
        return _detect_python(app_dir, over, port)

    if (app_dir / "go.mod").is_file():
        return gen.go_server(port=port)

    if (app_dir / "index.html").is_file():
        return gen.plain_static(port=port)

    raise DetectionFailed(
        "Could not work out how to build this directory. Forge looked for a "
        "Dockerfile, forge.json, package.json, requirements.txt, pyproject.toml, "
        f"go.mod and index.html in '{app_dir.name or '/'}' and found none of them. "
        "If the app lives in a subdirectory, set the project's root directory; "
        "otherwise add a Dockerfile and Forge will use it unchanged."
    )


# ---------------------------------------------------------------------------
# Explicit paths
# ---------------------------------------------------------------------------


def _from_repo_dockerfile(path: Path, *, port: int | None) -> BuildPlan:
    """Use the repository's own Dockerfile, reading its EXPOSE for the port.

    This is the escape hatch that makes the platform able to host anything at
    all: a language with no detection rule here still deploys, as long as it
    can be containerised. Which is every language.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    detected = port or _exposed_port(text) or DEFAULT_PORT
    notes: tuple[str, ...] = ()
    if port is None and _exposed_port(text) is None:
        notes = (
            f"No EXPOSE in the Dockerfile — assuming the app listens on {detected}. "
            "Set the project's port if that is wrong.",
        )
    return BuildPlan(
        framework="dockerfile",
        reason="Dockerfile in the repository — building it unchanged",
        dockerfile=text,
        port=detected,
        from_repo=True,
        notes=notes,
    )


def _exposed_port(dockerfile: str) -> int | None:
    """The last EXPOSE wins, matching how a reader would understand the file."""
    found = None
    for match in re.finditer(r"^\s*EXPOSE\s+(\d{1,5})", dockerfile, re.MULTILINE):
        value = int(match.group(1))
        if 0 < value < 65536:
            found = value
    return found


def _merge_manifest(over: Overrides, manifest: dict) -> Overrides:
    """`forge.json` fills in whatever the project settings left unset.

    Project settings win: a value typed into Forge is a deliberate override of
    what the repository claims, usually made precisely because the repository
    was wrong.
    """
    return Overrides(
        framework=over.framework or _str(manifest.get("framework")),
        install_command=over.install_command or _str(manifest.get("installCommand")),
        build_command=over.build_command or _str(manifest.get("buildCommand")),
        start_command=over.start_command or _str(manifest.get("startCommand")),
        port=over.port or _port(manifest.get("port")),
    )


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


def _detect_node(app_dir: Path, pkg: dict, over: Overrides, port: int) -> BuildPlan:
    tc = _toolchain(app_dir, over)
    deps = _dependencies(pkg)
    scripts = pkg.get("scripts") or {}
    build_script = over.build_command or ("build" if "build" in scripts else None)
    notes: list[str] = []
    if tc.lockfile is None:
        notes.append(
            "No lockfile found, so dependency versions are resolved at build "
            "time and two deploys of the same commit can differ. Commit a "
            "lockfile."
        )

    framework = over.framework or _node_framework(app_dir, deps)

    if over.start_command:
        return gen.node_server(
            framework=framework or "node",
            reason=f"Start command set on the project: {over.start_command}",
            tc=tc,
            build_script=build_script,
            start=over.start_command,
            port=port,
            notes=tuple(notes),
        )

    match framework:
        case "next":
            return _detect_next(app_dir, tc, port, tuple(notes))
        case "astro":
            return _detect_astro(deps, tc, port, tuple(notes))
        case "nuxt":
            return gen.node_server(
                framework="nuxt",
                reason="Nuxt config found — serving the Nitro server bundle",
                tc=tc,
                build_script=build_script or "build",
                start="node .output/server/index.mjs",
                port=port,
                prune=False,
                notes=tuple(notes),
            )
        case "sveltekit":
            return _detect_sveltekit(deps, tc, port, tuple(notes))
        case "remix":
            return gen.node_server(
                framework="remix",
                reason="Remix detected — running its own server",
                tc=tc,
                build_script=build_script or "build",
                start=scripts.get("start") or "npx remix-serve build/server/index.js",
                port=port,
                notes=tuple(notes),
            )
        case "vite":
            return gen.node_static(
                framework="vite",
                reason="Vite config with no SSR adapter — serving the built assets",
                tc=tc,
                build_script=build_script or "build",
                output_dir="dist",
                spa=True,
                port=port,
                notes=tuple(notes),
            )
        case "cra":
            return gen.node_static(
                framework="cra",
                reason="react-scripts detected — serving the built assets",
                tc=tc,
                build_script=build_script or "build",
                output_dir="build",
                spa=True,
                port=port,
                notes=tuple(notes),
            )

    if "start" in scripts:
        return gen.node_server(
            framework="node",
            reason="package.json has a start script — running it",
            tc=tc,
            build_script=build_script,
            start=f"{tc.run_prefix} start",
            port=port,
            notes=tuple(notes),
        )

    raise DetectionFailed(
        "This looks like a Node project, but there is no framework Forge "
        "recognises and no `start` script in package.json. Add a `start` "
        "script, or set a start command on the project."
    )


def _node_framework(app_dir: Path, deps: dict[str, str]) -> str | None:
    """Config file first, dependency second.

    A config file on disk is a stronger signal than a dependency, because a
    dependency can be transitive or left over from a migration — plenty of
    repositories still list `next` months after moving off it.
    """
    if _has_any(app_dir, NEXT_CONFIGS) or "next" in deps:
        return "next"
    if _has_any(app_dir, NUXT_CONFIGS) or "nuxt" in deps:
        return "nuxt"
    if _has_any(app_dir, ASTRO_CONFIGS) or "astro" in deps:
        return "astro"
    if _has_any(app_dir, SVELTE_CONFIGS) or "@sveltejs/kit" in deps:
        return "sveltekit"
    if any(d.startswith("@remix-run/") for d in deps):
        return "remix"
    if "react-scripts" in deps:
        return "cra"
    if _has_any(app_dir, VITE_CONFIGS) or "vite" in deps:
        return "vite"
    return None


def _detect_next(
    app_dir: Path, tc: NodeToolchain, port: int, notes: tuple[str, ...]
) -> BuildPlan:
    config = _read_first(app_dir, NEXT_CONFIGS) or ""
    output = _next_output_mode(config)

    if output == "export":
        return gen.node_static(
            framework="next-export",
            reason="Next.js with output: 'export' — serving the exported site",
            tc=tc,
            build_script="build",
            output_dir="out",
            spa=False,
            port=port,
            notes=notes,
        )
    if output == "standalone":
        return gen.next_standalone(tc=tc, port=port, notes=notes)

    return gen.node_server(
        framework="next",
        reason="Next.js — running `next start`",
        tc=tc,
        build_script="build",
        start=f"npx next start -p {port} -H 0.0.0.0",
        port=port,
        prune=False,
        notes=notes
        + (
            "Setting `output: 'standalone'` in next.config would cut this "
            "image from roughly 1 GB to under 200 MB and start it faster. "
            "Forge switches automatically once it is set.",
        ),
    )


def _next_output_mode(config: str) -> str | None:
    """Read `output:` out of next.config without executing it.

    A regex rather than a parser because the alternative is running the user's
    config file, and a config file is arbitrary JavaScript. The cost of the
    regex being fooled is a slower image; the cost of executing the file is
    arbitrary code running in the control plane.
    """
    stripped = re.sub(r"//[^\n]*|/\*.*?\*/", "", config, flags=re.DOTALL)
    match = re.search(r"""\boutput\s*:\s*['"](\w+)['"]""", stripped)
    return match.group(1) if match else None


def _detect_astro(
    deps: dict[str, str],
    tc: NodeToolchain,
    port: int,
    notes: tuple[str, ...],
) -> BuildPlan:
    if "@astrojs/node" in deps:
        return gen.node_server(
            framework="astro-node",
            reason="Astro with the Node adapter — running its server entry",
            tc=tc,
            build_script="build",
            start="node ./dist/server/entry.mjs",
            port=port,
            prune=False,
            notes=notes,
        )
    return gen.node_static(
        framework="astro",
        reason="Astro with no server adapter — serving the built site",
        tc=tc,
        build_script="build",
        output_dir="dist",
        spa=False,
        port=port,
        notes=notes,
    )


def _detect_sveltekit(
    deps: dict[str, str],
    tc: NodeToolchain,
    port: int,
    notes: tuple[str, ...],
) -> BuildPlan:
    if "@sveltejs/adapter-static" in deps:
        return gen.node_static(
            framework="sveltekit-static",
            reason="SvelteKit with adapter-static — serving the built site",
            tc=tc,
            build_script="build",
            output_dir="build",
            spa=False,
            port=port,
            notes=notes,
        )
    if "@sveltejs/adapter-auto" in deps and "@sveltejs/adapter-node" not in deps:
        notes = notes + (
            "SvelteKit is using adapter-auto, which only works on hosts it "
            "recognises and will fail here. Install @sveltejs/adapter-node "
            "and set it in svelte.config.js.",
        )
    return gen.node_server(
        framework="sveltekit",
        reason="SvelteKit with the Node adapter — running the built server",
        tc=tc,
        build_script="build",
        start="node build/index.js",
        port=port,
        notes=notes,
    )


def _toolchain(app_dir: Path, over: Overrides) -> NodeToolchain:
    """Whichever lockfile is present decides. Ties break toward the stricter
    tool, and a custom install command keeps the detected manager but replaces
    its command."""
    for tc in (PNPM, YARN, BUN, NPM):
        if tc.lockfile and (app_dir / tc.lockfile).is_file():
            chosen = tc
            break
    else:
        chosen = NPM_NO_LOCK

    if over.install_command:
        return NodeToolchain(
            name=chosen.name,
            lockfile=chosen.lockfile,
            install=over.install_command,
            setup=chosen.setup,
        )
    return chosen


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------


def _detect_python(app_dir: Path, over: Overrides, port: int) -> BuildPlan:
    has_requirements = (app_dir / "requirements.txt").is_file()
    install = over.install_command or (
        "pip install -r requirements.txt" if has_requirements else "pip install ."
    )
    blob = _dependency_blob(app_dir)

    if over.start_command:
        return gen.python_server(
            framework=over.framework or "python",
            reason=f"Start command set on the project: {over.start_command}",
            start=over.start_command,
            install=install,
            port=port,
        )

    if "django" in blob:
        module = _find_python_module(app_dir, "wsgi.py")
        if module:
            return gen.python_server(
                framework="django",
                reason=f"Django detected — serving {module}:application with gunicorn",
                start=(
                    f"gunicorn {module}:application --bind 0.0.0.0:{port} "
                    "--workers 3 --access-logfile -"
                ),
                install=f"{install} && pip install gunicorn",
                port=port,
                notes=(
                    "Static files are served by the app. Run collectstatic in "
                    "the build, or put WhiteNoise in the middleware stack.",
                ),
            )

    target = _find_asgi_app(app_dir)
    if target:
        module, kind = target
        if kind == "asgi":
            return gen.python_server(
                framework="python-asgi",
                reason=f"ASGI app found at {module}:app — serving it with uvicorn",
                start=(
                    f"uvicorn {module}:app --host 0.0.0.0 --port {port} "
                    "--proxy-headers --forwarded-allow-ips=*"
                ),
                install=f"{install} && pip install 'uvicorn[standard]'",
                port=port,
            )
        return gen.python_server(
            framework="python-wsgi",
            reason=f"WSGI app found at {module}:app — serving it with gunicorn",
            start=(
                f"gunicorn {module}:app --bind 0.0.0.0:{port} "
                "--workers 3 --access-logfile -"
            ),
            install=f"{install} && pip install gunicorn",
            port=port,
        )

    raise DetectionFailed(
        "This looks like a Python project, but Forge could not find the "
        "application object. It looked for `app = FastAPI(...)`, "
        "`app = Flask(...)` and a Django `wsgi.py` in the usual places. "
        "Set a start command on the project — for example "
        f"`uvicorn myapp.main:app --host 0.0.0.0 --port {port}`."
    )


#: Where an application object usually lives, most conventional first. The
#: search stops at the first hit, so this order is the tie-break.
PY_ENTRY_CANDIDATES = (
    "main.py",
    "app.py",
    "asgi.py",
    "wsgi.py",
    "src/main.py",
    "app/main.py",
    "api/main.py",
    "src/app.py",
    "application.py",
)

_ASGI_APP = re.compile(r"^\s*app\s*(?::\s*\w+\s*)?=\s*(FastAPI|Starlette)\s*\(", re.M)
_WSGI_APP = re.compile(r"^\s*app\s*(?::\s*\w+\s*)?=\s*Flask\s*\(", re.M)


def _find_asgi_app(app_dir: Path) -> tuple[str, str] | None:
    """Find the module that actually constructs the application.

    Reading the file is the point. Assuming `main:app` because `main.py` exists
    produces an image that builds, starts, fails its health check and reports
    nothing more useful than "container exited" — and the real cause is a
    single line the platform could have read.
    """
    for relative in PY_ENTRY_CANDIDATES:
        path = app_dir / relative
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        module = relative[: -len(".py")].replace("/", ".")
        if _ASGI_APP.search(source):
            return module, "asgi"
        if _WSGI_APP.search(source):
            return module, "wsgi"
    return None


def _find_python_module(app_dir: Path, filename: str) -> str | None:
    """`myproject/wsgi.py` -> `myproject.wsgi`, searching only one level deep.

    Django's layout puts it exactly there, and walking the whole tree would
    find the copy inside a vendored dependency instead.
    """
    for child in sorted(app_dir.iterdir()):
        if child.is_dir() and (child / filename).is_file():
            return f"{child.name}.{filename[: -len('.py')]}"
    return None


def _dependency_blob(app_dir: Path) -> str:
    """Every declared dependency as one lowercase string, for substring tests.

    Crude on purpose: the question being asked is only ever "is Django in here
    at all", and a real parser for three different manifest formats would be a
    lot of code to answer it.
    """
    parts = []
    for name in ("requirements.txt", "pyproject.toml", "Pipfile"):
        path = app_dir / name
        if path.is_file():
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts).lower()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _dependencies(pkg: dict) -> dict[str, str]:
    merged: dict[str, str] = {}
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        section = pkg.get(key)
        if isinstance(section, dict):
            merged.update(section)
    return merged


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise DetectionFailed(f"{path.name} is not valid JSON: {exc}") from exc
    return value if isinstance(value, dict) else None


def _read_first(app_dir: Path, names: tuple[str, ...]) -> str | None:
    for name in names:
        path = app_dir / name
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    return None


def _has_any(app_dir: Path, names: tuple[str, ...]) -> bool:
    return any((app_dir / name).is_file() for name in names)


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _port(value: object) -> int | None:
    if isinstance(value, int) and 0 < value < 65536:
        return value
    return None
