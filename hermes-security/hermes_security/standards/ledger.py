"""Versioned review aids and conservative, explicit standards closure.

Path-based surfaces are hints, not proof of architecture or absence of a control.
Only explicit lane/control units establish review; findings never close a lane.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path, PurePosixPath
import re


@lru_cache(maxsize=3)
def _load(filename: str) -> dict:
    return json.loads((Path(__file__).parent / filename).read_text(encoding="utf-8"))


def load_top10() -> dict:
    """Return an isolated copy of the cached OWASP Top 10:2025 document."""
    return deepcopy(_load("owasp-top10-2025.json"))


def load_asvs() -> dict:
    """Return the curated ASVS 5.0.0 document, not a certification checklist."""
    return deepcopy(_load("asvs-5.0.0.json"))


def load_cwe_map() -> dict:
    """Return official OWASP memberships and editorial ASVS associations."""
    return deepcopy(_load("cwe-map.json"))


_SOURCE = {".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".kts", ".swift",
           ".go", ".rs", ".c", ".cc", ".cpp", ".h", ".cs", ".php", ".rb", ".scala",
           ".sh", ".ex", ".exs", ".vue", ".svelte", ".m", ".mm", ".dart"}
_MANIFESTS = {"package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "pyproject.toml",
              "poetry.lock", "uv.lock", "pipfile", "pipfile.lock", "go.mod", "go.sum", "cargo.toml",
              "cargo.lock", "gemfile", "gemfile.lock", "pom.xml", "build.gradle", "build.gradle.kts",
              "composer.json", "composer.lock", "packages.lock.json", "pubspec.yaml"}
SURFACES = frozenset({"web-route", "api", "graphql", "auth", "session", "authz", "crypto", "secrets",
                     "file-upload", "file-io", "config", "logging", "data-store", "client-web", "mobile",
                     "iac", "ci", "dependencies", "llm-agent", "websocket", "deserialization", "outbound-http"})


def detect_surfaces(inventory_files: list[dict]) -> set[str]:
    """Infer review hints only from paths/extensions; never open or execute files."""
    found: set[str] = set()
    for entry in inventory_files:
        raw = entry.get("path", "") if isinstance(entry, dict) else str(entry)
        path = PurePosixPath(raw.replace("\\", "/").lower())
        name, suffix, text = path.name, path.suffix, str(path)
        tokens = set(filter(None, re.split(r"[^a-z0-9]+", text)))
        if suffix in _SOURCE:
            # Any source file may read config or log; routes need route-like paths (below).
            found.update({"config", "logging"})
        if name in _MANIFESTS or name.startswith("requirements") or suffix == ".csproj":
            found.add("dependencies")
        if ".github/workflows/" in text or name in {".gitlab-ci.yml", "jenkinsfile", "azure-pipelines.yml"}:
            found.add("ci")
        if name.startswith("dockerfile") or suffix in {".tf", ".tfvars"} or tokens & {"k8s", "kubernetes", "helm"} or name in {"docker-compose.yml", "compose.yml", "compose.yaml"}:
            found.update({"iac", "config"})
        if name == "androidmanifest.xml" or suffix in {".swift", ".kt", ".kts"}:
            found.add("mobile")
        if suffix in {".graphql", ".gql"} or "graphql" in tokens:
            found.update({"graphql", "api"})
        if tokens & {"routes", "controllers", "flask", "fastapi", "express", "api", "rpc"}:
            found.update({"web-route", "api", "authz"})
        if any(part in text for part in ("auth", "login", "session", "jwt", "oauth")):
            found.update({"auth", "session"})
        if tokens & {"authz", "permissions", "roles", "policy", "tenant"}:
            found.add("authz")
        if any(part in text for part in ("crypto", "cipher", "sign")):
            found.add("crypto")
        if "upload" in text:
            found.update({"file-upload", "file-io"})
        if tokens & {"file", "files", "storage", "archive", "extract"}:
            found.add("file-io")
        if tokens & {"secret", "secrets", "vault", "credentials"} or name.startswith(".env"):
            found.update({"secrets", "config"})
        if any(part in text for part in ("llm", "agent", "prompt", "mcp")):
            found.add("llm-agent")
        if tokens & {"ws", "websocket", "websockets", "socket", "sockets", "webrtc", "turn"}:
            found.add("websocket")
        if tokens & {"deserialize", "deserialization", "pickle", "marshal", "serializer", "serializers"}:
            found.add("deserialization")
        if tokens & {"http", "requests", "fetch", "proxy", "outbound", "httpx", "webhook"}:
            found.add("outbound-http")
        if tokens & {"db", "database", "models", "migrations", "store", "repository", "redis"} or suffix in {".sql", ".sqlite"}:
            found.add("data-store")
        if suffix in {".html", ".jsx", ".tsx", ".vue", ".svelte"} or tokens & {"frontend", "client", "browser"}:
            found.add("client-web")
        if suffix in {".toml", ".ini", ".cfg", ".yaml", ".yml"} or tokens & {"config", "settings"}:
            found.add("config")
        if tokens & {"log", "logs", "logging", "audit"}:
            found.add("logging")
    return found


def applicable_controls(surfaces) -> list[dict]:
    """Select controls with any matching surface (an OR, not an AND)."""
    present = set(surfaces)
    return [c for c in load_asvs()["controls"] if present.intersection(c["surfaces"])]


def map_cwe(cwe: str) -> dict:
    """Map a literal CWE identifier; do not repair malformed identifiers."""
    row = _load("cwe-map.json")["entries"].get(cwe, {})
    return {"owasp": list(row.get("owasp", [])), "asvs": list(row.get("asvs", []))}


_STATES = {"reviewed", "not_applicable", "deferred", "unsupported", "unknown", "failed"}
# Conflicting independently submitted closures must not hide an outstanding gap.
_PRIORITY = {"reviewed": 0, "not_applicable": 1, "deferred": 2, "unsupported": 3, "unknown": 4, "failed": 5}


def _units_by_id(units: list[dict]) -> dict:
    grouped: dict[str, list[dict]] = {}
    for unit in units:
        ident = unit.get("unitId", unit.get("unit", unit.get("unit_id")))
        if not isinstance(ident, str):
            continue
        state = unit.get("state", "unknown")
        valid = state in _STATES
        evidence = unit.get("evidence", [])
        row = {"state": state if valid else "unknown",
               "reason": unit.get("reason") or unit.get("note") or ("explicit coverage unit" if valid else "invalid coverage state"),
               "evidence": sorted({e for e in evidence if isinstance(e, str)}) if isinstance(evidence, list) else []}
        grouped.setdefault(ident, []).append(row)
    result = {}
    for ident, rows in grouped.items():
        states = {r["state"] for r in rows}
        # Reviewed and not-applicable are incompatible assertions, not two successes.
        if states == {"reviewed", "not_applicable"}:
            state = "unknown"
        else:
            state = max(states, key=_PRIORITY.__getitem__)
        result[ident] = {"state": state,
                         "reason": "; ".join(sorted({r["reason"] for r in rows})),
                         "evidence": sorted({e for r in rows for e in r["evidence"]})}
        if len(states) > 1:
            result[ident]["reason"] = "conflicting coverage units: " + result[ident]["reason"]
    return result


def build_ledger(surfaces, coverage_units: list[dict], findings: list[dict]) -> dict:
    """Build the coverage.json standards object using explicit unit closures only.

    All ten OWASP lanes require explicit disposition. ASVS controls without
    matching path hints default to not_applicable, never to reviewed. Explicit
    control units override applicability hints. A mapped finding with no unit
    remains unknown even if path heuristics missed the corresponding surface.
    """
    units = _units_by_id(coverage_units)
    mapped_owasp: set[str] = set()
    mapped_asvs: set[str] = set()
    for finding in findings:
        if finding.get("evidenceState") in {"rejected", "inconclusive"}:
            continue
        taxonomy = finding.get("taxonomy") or {}
        mapped_owasp.update(taxonomy.get("owasp", []))
        mapped_asvs.update(taxonomy.get("asvs", []))
        for cwe in taxonomy.get("cwe", []):
            mapping = map_cwe(cwe)
            mapped_owasp.update(mapping["owasp"])
            mapped_asvs.update(mapping["asvs"])
    categories = []
    for cat in load_top10()["categories"]:
        cid = cat["id"]
        row = units.get("lane:" + cid)
        if row is None:
            row = {"state": "unknown", "evidence": [], "reason":
                   "finding mapped but lane not closed" if cid in mapped_owasp else "no explicit lane coverage unit"}
        elif cid in mapped_owasp and row["state"] == "not_applicable":
            row = {**row, "state": "unknown", "reason": "finding contradicts not_applicable lane"}
        categories.append({"id": cid, "name": cat["name"], **deepcopy(row)})
    applicable = {c["id"] for c in applicable_controls(surfaces)}
    controls = []
    for control in load_asvs()["controls"]:
        cid = control["id"]
        row = units.get("asvs:" + cid)
        if row is None:
            if cid in mapped_asvs:
                row = {"state": "unknown", "evidence": [], "reason": "finding mapped but control not closed"}
            elif cid in applicable:
                row = {"state": "unknown", "evidence": [], "reason": "no explicit control coverage unit"}
            else:
                row = {"state": "not_applicable", "evidence": [], "reason": "no matching surface in path-based inventory hints"}
        elif cid in mapped_asvs and row["state"] == "not_applicable":
            row = {**row, "state": "unknown", "reason": "finding contradicts not_applicable control"}
        controls.append({"id": cid, **deepcopy(row)})
    return {"owaspTop10": {"version": "2025", "categories": categories},
            "asvs": {"version": "5.0.0", "controls": controls}}


def mandatory_gaps(ledger: dict) -> list[str]:
    """Explain each unclosed lane or applicable control in plain language."""
    gaps = []
    for section, key, label in (("owaspTop10", "categories", "OWASP lane"), ("asvs", "controls", "ASVS control")):
        for row in ledger.get(section, {}).get(key, []):
            if row["state"] not in {"reviewed", "not_applicable"}:
                gaps.append(f"{label} {row['id']} is {row['state']}: {str(row['reason']).rstrip(' .')}.")
    return gaps


def specialist_profiles_for(surfaces) -> list[str]:
    """Return deterministic profile basenames (without .md) for present hints."""
    present = set(surfaces)
    rules = {
        "web-api": {"web-route", "api", "graphql", "websocket", "outbound-http", "file-upload", "deserialization"},
        "auth": {"auth", "session", "authz"},
        "crypto": {"crypto", "secrets"},
        "business-logic": {"web-route", "api", "data-store"},
        "supply-chain": {"dependencies", "ci"},
        "client": {"client-web"},
        "cloud-iac": {"iac"},
        "mobile": {"mobile"},
        "agentic-ai": {"llm-agent"},
        "exceptional-conditions": SURFACES,
    }
    return [name for name, hints in rules.items() if present.intersection(hints)]
