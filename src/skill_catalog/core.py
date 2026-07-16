from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import yaml


SCHEMA_VERSION = 1
CATALOG_FILENAME = "catalog.json"
BASELINE_FILENAME = "catalog-baseline.json"
PROFILES_FILENAME = "profiles.json"
INSTALL_MANIFEST = ".agent-skills-collection.json"

CATEGORY_LABELS = {
    "agent-workflows": "Agent Workflows",
    "ai-and-media": "AI, Image, Video & Audio",
    "authentication": "Authentication",
    "best-ui-ux": "Best UI/UX",
    "context-engineering": "Context Engineering",
    "databases": "Databases",
    "design-ui-and-templates": "Design, UI & Templates",
    "development-and-testing": "Development & Testing",
    "email-and-notion": "Email & Notion",
    "figma-and-design-tools": "Figma & Design Tools",
    "frontend-and-frameworks": "Frontend & Frameworks",
    "marketing-and-advertising": "Marketing & Advertising",
}

HARD_ISSUES = {
    "compatibility-too-long",
    "description-too-long",
    "invalid-frontmatter",
    "invalid-name",
    "missing-description",
    "missing-frontmatter",
    "missing-name",
    "name-folder-mismatch",
    "nested-skill-packages",
}

TEXT_SUFFIXES = {
    "",
    ".c",
    ".cjs",
    ".cmd",
    ".conf",
    ".cs",
    ".css",
    ".env",
    ".example",
    ".go",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsonl",
    ".md",
    ".mjs",
    ".ps1",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}

RISK_PATTERNS: Sequence[Tuple[str, str, re.Pattern[str]]] = (
    (
        "credential-shaped-value",
        "high",
        re.compile(
            r"(?:AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|gh[pousr]_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"
        ),
    ),
    (
        "remote-shell-pipe",
        "high",
        re.compile(
            r"(?i)(?:curl|wget)[^\n|]{0,300}\|\s*(?:sh|bash|zsh|python|python3|node|pwsh|powershell)"
        ),
    ),
    (
        "destructive-git",
        "high",
        re.compile(r"(?i)\bgit\s+(?:reset\s+--hard|clean\s+-[^\s]*f)\b"),
    ),
    (
        "broad-recursive-delete",
        "high",
        re.compile(r"(?i)\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r"),
    ),
    (
        "permission-bypass",
        "medium",
        re.compile(
            r"(?i)(?:--dangerously|--no-verify|chmod\s+777|disable[^\n]{0,30}(?:security|guardrail))"
        ),
    ),
    (
        "instruction-override-language",
        "medium",
        re.compile(
            r"(?i)(?:ignore|disregard)\s+(?:all\s+|any\s+)?(?:previous|prior|above)\s+instructions"
        ),
    ),
    (
        "no-confirmation-language",
        "medium",
        re.compile(
            r"(?i)(?:do\s+not\s+ask\s+(?:for\s+)?(?:confirmation|permission)|without\s+(?:asking|confirmation)|never\s+ask)"
        ),
    ),
)

LINK_PATTERN = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)", re.MULTILINE)
FRONTMATTER_PATTERN = re.compile(
    r"\A---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|\Z)", re.DOTALL
)
NAME_PATTERN = re.compile(r"\A[a-z0-9]+(?:-[a-z0-9]+)*\Z")

SEARCH_SYNONYMS = {
    "performance": ("optimize", "optimization", "optimizer", "speed", "latency"),
    "security": ("secure", "hardening", "vulnerability", "audit"),
    "testing": ("test", "tests", "qa", "verification"),
}


class CatalogError(RuntimeError):
    pass


@dataclass(frozen=True)
class RepoPaths:
    root: Path

    @property
    def catalog(self) -> Path:
        return self.root / CATALOG_FILENAME

    @property
    def baseline(self) -> Path:
        return self.root / BASELINE_FILENAME

    @property
    def profiles(self) -> Path:
        return self.root / PROFILES_FILENAME


def find_repo_root(start: Optional[Path] = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "README.md").is_file() and (candidate / ".git").exists():
            return candidate
    raise CatalogError("Could not find the Agent Skills Collection repository root")


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")


def parse_frontmatter(path: Path) -> Tuple[Dict[str, Any], List[str], str]:
    text = read_text(path)
    match = FRONTMATTER_PATTERN.match(text)
    if not match:
        return {}, ["missing-frontmatter"], text
    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}, ["invalid-frontmatter"], text
    if not isinstance(parsed, dict):
        return {}, ["invalid-frontmatter"], text
    return parsed, [], text


def validate_metadata(
    path: Path, metadata: Mapping[str, Any], initial: Iterable[str]
) -> List[str]:
    issues = list(initial)
    if issues:
        return sorted(set(issues))

    name = str(metadata.get("name", "")).strip()
    description = str(metadata.get("description", "")).strip()
    if not name:
        issues.append("missing-name")
    else:
        if len(name) > 64 or not NAME_PATTERN.fullmatch(name):
            issues.append("invalid-name")
        if name != path.parent.name:
            issues.append("name-folder-mismatch")
    if not description:
        issues.append("missing-description")
    elif len(description) > 1024:
        issues.append("description-too-long")
    compatibility = str(metadata.get("compatibility", "")).strip()
    if len(compatibility) > 500:
        issues.append("compatibility-too-long")
    return sorted(set(issues))


def decode_link_target(raw: str) -> Optional[str]:
    value = raw.strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1]
    else:
        value = re.split(r"\s+[\"']", value, maxsplit=1)[0]
    if not value or re.match(
        r"(?i)^(?:#|https?:|mailto:|data:|file:|vscode:|skill:)", value
    ):
        return None
    if "{{" in value or re.match(
        r"^(?:URL|LOG_URL|figmaUrl|path/to|src/|apps/|packages/)", value
    ):
        return None
    value = re.split(r"[?#]", value, maxsplit=1)[0]
    if not re.search(
        r"(?:^|/)(?:references?|scripts?|assets?|tools?|templates?|resources?)/|\.(?:md|json|ya?ml|py|sh|js|mjs|cjs|ts|tsx|png|svg|csv|txt)$",
        value,
        re.IGNORECASE,
    ):
        return None
    return value.replace("%20", " ")


def find_missing_references(
    skill_file: Path, text: str, root: Path
) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    for match in LINK_PATTERN.finditer(text):
        target = decode_link_target(match.group(1))
        if target is None:
            continue
        resolved = (skill_file.parent / target).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            findings.append({"target": target, "reason": "outside-repository"})
            continue
        if not resolved.exists():
            findings.append({"target": target, "reason": "missing"})
    unique = {(item["target"], item["reason"]): item for item in findings}
    return [unique[key] for key in sorted(unique)]


def iter_skill_files(root: Path) -> Iterable[Path]:
    for category in sorted(CATEGORY_LABELS):
        category_dir = root / category
        if category_dir.is_dir():
            yield from sorted(category_dir.rglob("SKILL.md"))


def iter_skill_content(skill_dir: Path) -> Iterable[Path]:
    for path in sorted(skill_dir.rglob("*")):
        if path.is_file() and not path.is_symlink():
            yield path


def hash_directory(skill_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in iter_skill_content(skill_dir):
        relative = path.relative_to(skill_dir).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def scan_risks(skill_dir: Path, root: Path) -> Dict[str, Any]:
    signals: Dict[str, Dict[str, Any]] = {}
    rank = {"low": 0, "medium": 1, "high": 2}
    level = "low"
    for path in iter_skill_content(skill_dir):
        if (
            path.suffix.lower() not in TEXT_SUFFIXES
            or path.stat().st_size > 1024 * 1024
        ):
            continue
        text = read_text(path)
        for signal_id, signal_level, pattern in RISK_PATTERNS:
            matches = list(pattern.finditer(text))
            if not matches:
                continue
            if rank[signal_level] > rank[level]:
                level = signal_level
            item = signals.setdefault(
                signal_id, {"id": signal_id, "level": signal_level, "evidence": []}
            )
            for match in matches[:3]:
                line = text.count("\n", 0, match.start()) + 1
                evidence = {
                    "path": path.relative_to(root).as_posix(),
                    "line": line,
                }
                if evidence not in item["evidence"] and len(item["evidence"]) < 5:
                    item["evidence"].append(evidence)
    return {"level": level, "signals": [signals[key] for key in sorted(signals)]}


def parse_upstreams(readme: Path) -> Dict[str, str]:
    if not readme.is_file():
        return {}
    pattern = re.compile(
        r"^\| \[`[^`]+`\]\(([^)]+)\) \| [^|]+ \| \[[^\]]+\]\((https://github\.com/[^)]+)\) \|$"
    )
    mapping: Dict[str, str] = {}
    for line in read_text(readme).splitlines():
        match = pattern.match(line)
        if match:
            mapping[match.group(1).rstrip("/")] = match.group(2)
    return mapping


def nearest_upstream(relative_dir: str, upstreams: Mapping[str, str]) -> Optional[str]:
    matches = [
        (prefix, url)
        for prefix, url in upstreams.items()
        if relative_dir == prefix or relative_dir.startswith(prefix + "/")
    ]
    if not matches:
        return None
    return max(matches, key=lambda item: len(item[0]))[1]


def license_value(metadata: Mapping[str, Any], skill_dir: Path) -> Optional[str]:
    declared = str(metadata.get("license", "")).strip()
    if declared:
        return declared
    candidates = sorted(
        path.name
        for path in skill_dir.iterdir()
        if path.is_file() and path.name.lower().startswith("license")
    )
    return candidates[0] if candidates else None


def build_entry(
    skill_file: Path, root: Path, upstreams: Mapping[str, str]
) -> Dict[str, Any]:
    metadata, initial, text = parse_frontmatter(skill_file)
    issues = validate_metadata(skill_file, metadata, initial)
    skill_dir = skill_file.parent
    relative_dir = skill_dir.relative_to(root).as_posix()
    parts = Path(relative_dir).parts
    files = list(iter_skill_content(skill_dir))
    nested_skills = [
        path.relative_to(skill_dir).as_posix()
        for path in files
        if path.name == "SKILL.md" and path != skill_file
    ]
    if nested_skills:
        issues = sorted(set([*issues, "nested-skill-packages"]))
    missing_refs = find_missing_references(skill_file, text, root)
    warnings: List[Dict[str, Any]] = []
    if missing_refs:
        warnings.append(
            {
                "code": "missing-local-references",
                "count": len(missing_refs),
                "examples": missing_refs[:10],
            }
        )
    line_count = text.count("\n") + (0 if text.endswith("\n") else 1)
    if line_count > 500:
        warnings.append({"code": "skill-over-500-lines", "lines": line_count})
    if any(len(path.relative_to(root).as_posix()) > 240 for path in files):
        warnings.append({"code": "long-paths"})
    if nested_skills:
        warnings.append(
            {
                "code": "nested-skill-packages",
                "count": len(nested_skills),
                "examples": nested_skills[:10],
            }
        )

    name = str(metadata.get("name", "")).strip() or skill_dir.name
    description = str(metadata.get("description", "")).strip()
    return {
        "id": relative_dir,
        "name": name,
        "description": description,
        "category": parts[0],
        "source": parts[1] if len(parts) > 1 else parts[0],
        "path": relative_dir,
        "upstream_url": nearest_upstream(relative_dir, upstreams),
        "license": license_value(metadata, skill_dir),
        "compatibility": str(metadata.get("compatibility", "")).strip() or None,
        "allowed_tools": str(metadata.get("allowed-tools", "")).strip() or None,
        "status": "quarantined" if HARD_ISSUES.intersection(issues) else "ready",
        "audit_status": "unreviewed",
        "duplicate_of": None,
        "issues": issues,
        "warnings": warnings,
        "risk": scan_risks(skill_dir, root),
        "metrics": {
            "bytes": sum(path.stat().st_size for path in files),
            "files": len(files),
            "skill_lines": line_count,
        },
        "checksum": "sha256:" + hash_directory(skill_dir),
    }


def build_catalog(root: Path) -> Dict[str, Any]:
    upstreams = parse_upstreams(root / "README.md")
    entries = [build_entry(path, root, upstreams) for path in iter_skill_files(root)]
    entries.sort(key=lambda entry: entry["id"])
    checksum_groups: Dict[str, List[Dict[str, Any]]] = {}
    for entry in entries:
        checksum_groups.setdefault(entry["checksum"], []).append(entry)
    duplicate_groups = 0
    duplicate_entries = 0
    for group in checksum_groups.values():
        if len(group) < 2:
            continue
        duplicate_groups += 1
        canonical = min(
            group,
            key=lambda entry: (
                entry["status"] != "ready",
                "_upstream-collections" in entry["id"],
                "https%3A%2F%2F" in entry["id"],
                len(entry["id"]),
                entry["id"],
            ),
        )
        for entry in group:
            if entry is not canonical:
                entry["duplicate_of"] = canonical["id"]
                duplicate_entries += 1
    categories: Dict[str, int] = {}
    risk: Dict[str, int] = {"low": 0, "medium": 0, "high": 0}
    statuses: Dict[str, int] = {"ready": 0, "quarantined": 0}
    for entry in entries:
        categories[entry["category"]] = categories.get(entry["category"], 0) + 1
        risk[entry["risk"]["level"]] += 1
        statuses[entry["status"]] += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "stats": {
            "total": len(entries),
            "categories": dict(sorted(categories.items())),
            "statuses": statuses,
            "risk_levels": risk,
            "exact_duplicate_packages": {
                "groups": duplicate_groups,
                "redundant_entries": duplicate_entries,
            },
        },
        "skills": entries,
    }


def baseline_from_catalog(catalog: Mapping[str, Any]) -> Dict[str, Any]:
    entries: Dict[str, Any] = {}
    for skill in catalog["skills"]:
        hard = sorted(set(skill["issues"]).intersection(HARD_ISSUES))
        risks = sorted(signal["id"] for signal in skill["risk"]["signals"])
        if hard or risks:
            entries[skill["id"]] = {"issues": hard, "risk_signals": risks}
    return {"schema_version": SCHEMA_VERSION, "entries": entries}


def canonical_json(data: Mapping[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=str(path.parent))
    os.close(fd)
    temp_path = Path(temporary)
    try:
        temp_path.write_text(content, encoding="utf-8")
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def write_json(path: Path, data: Mapping[str, Any]) -> None:
    write_text_atomic(path, canonical_json(data))


def load_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError(f"Could not read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CatalogError(f"Expected a JSON object in {path}")
    return value


def load_catalog(root: Path) -> Dict[str, Any]:
    path = root / CATALOG_FILENAME
    if not path.is_file():
        raise CatalogError(f"Missing {CATALOG_FILENAME}; run `skills-collection build`")
    catalog = load_json(path)
    if catalog.get("schema_version") != SCHEMA_VERSION:
        raise CatalogError("Unsupported catalog schema; rebuild the catalog")
    return catalog


def catalog_is_current(root: Path, catalog: Mapping[str, Any]) -> bool:
    return canonical_json(build_catalog(root)) == canonical_json(catalog)


def compare_baseline(
    catalog: Mapping[str, Any], baseline: Mapping[str, Any]
) -> List[str]:
    current = baseline_from_catalog(catalog).get("entries", {})
    expected = baseline.get("entries", {})
    regressions: List[str] = []
    for skill_id, state in current.items():
        old = expected.get(skill_id, {})
        for issue in sorted(set(state.get("issues", [])) - set(old.get("issues", []))):
            regressions.append(f"{skill_id}: new issue {issue}")
        for signal in sorted(
            set(state.get("risk_signals", [])) - set(old.get("risk_signals", []))
        ):
            regressions.append(f"{skill_id}: new risk signal {signal}")
    return regressions


def resolve_skill(catalog: Mapping[str, Any], selector: str) -> Dict[str, Any]:
    exact = [skill for skill in catalog["skills"] if skill["id"] == selector]
    if exact:
        return exact[0]
    matches = [skill for skill in catalog["skills"] if skill["name"] == selector]
    if not matches:
        raise CatalogError(f"No skill matches {selector!r}")
    canonical_matches = [skill for skill in matches if not skill.get("duplicate_of")]
    if len(canonical_matches) == 1:
        return canonical_matches[0]
    if len(matches) > 1:
        choices = "\n  ".join(skill["id"] for skill in matches[:20])
        raise CatalogError(f"{selector!r} is ambiguous; use a full ID:\n  {choices}")
    return matches[0]


def search_catalog(
    catalog: Mapping[str, Any],
    query: str,
    *,
    limit: int = 20,
    include_quarantined: bool = False,
    include_duplicates: bool = False,
) -> List[Dict[str, Any]]:
    terms = [term for term in re.split(r"[^a-z0-9]+", query.lower()) if term]
    scored: List[Tuple[int, Dict[str, Any]]] = []
    for skill in catalog["skills"]:
        if skill["status"] == "quarantined" and not include_quarantined:
            continue
        if skill.get("duplicate_of") and not include_duplicates:
            continue
        name = skill["name"].lower()
        description = skill["description"].lower()
        category = skill["category"].lower()
        source = skill["source"].lower()
        compact_name = re.sub(r"[^a-z0-9]+", "", name)
        compact_description = re.sub(r"[^a-z0-9]+", "", description)
        score = 0
        matched_terms = 0
        for term in terms:
            term_score = 0
            variants = (term, *SEARCH_SYNONYMS.get(term, ()))
            for variant in variants:
                if variant == name:
                    term_score = max(term_score, 100)
                if variant in name:
                    term_score = max(term_score, 30)
                elif variant in compact_name:
                    term_score = max(term_score, 20)
                term_score = max(term_score, description.count(variant) * 4)
                if variant not in description and variant in compact_description:
                    term_score = max(term_score, 10)
            score += term_score
            if term_score:
                matched_terms += 1
            if term in category:
                score += 5
            if term in source:
                score += 3
        if terms and matched_terms == len(terms):
            score += 50
        if score:
            scored.append((score, skill))
    scored.sort(key=lambda item: (-item[0], item[1]["name"], item[1]["id"]))
    return [skill for _, skill in scored[:limit]]


def ensure_no_symlinks(source: Path) -> None:
    symlinks = [path for path in source.rglob("*") if path.is_symlink()]
    if symlinks:
        raise CatalogError(
            f"Refusing to install a skill containing symlinks: {symlinks[0]}"
        )


def load_install_manifest(destination: Path) -> Dict[str, Any]:
    path = destination / INSTALL_MANIFEST
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "installed": {}}
    manifest = load_json(path)
    if not isinstance(manifest.get("installed"), dict):
        raise CatalogError(f"Invalid install manifest at {path}")
    return manifest


def write_install_manifest(destination: Path, manifest: Mapping[str, Any]) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    write_text_atomic(destination / INSTALL_MANIFEST, canonical_json(manifest))


def preflight_install(
    root: Path,
    skill: Mapping[str, Any],
    destination: Path,
    *,
    accept_risk: bool = False,
    unsafe: bool = False,
    force: bool = False,
    discard_changes: bool = False,
) -> Tuple[Path, Path]:
    root = root.resolve()
    if skill["status"] == "quarantined" and not unsafe:
        raise CatalogError(
            f"{skill['id']} is quarantined ({', '.join(skill['issues'])}); pass --unsafe to override"
        )
    if skill["risk"]["level"] in {"medium", "high"} and not accept_risk:
        signals = ", ".join(signal["id"] for signal in skill["risk"]["signals"])
        raise CatalogError(
            f"{skill['id']} has {skill['risk']['level']} risk signals ({signals}); pass --accept-risk after review"
        )
    destination = destination.resolve()
    source = (root / skill["path"]).resolve()
    try:
        source.relative_to(root)
    except ValueError as exc:
        raise CatalogError("Skill source escaped the repository") from exc
    ensure_no_symlinks(source)
    current_source_checksum = "sha256:" + hash_directory(source)
    if current_source_checksum != skill["checksum"]:
        raise CatalogError(
            "Skill source changed after catalog generation; rebuild and review catalog.json before installing"
        )
    live_metadata, live_initial, _ = parse_frontmatter(source / "SKILL.md")
    live_issues = validate_metadata(source / "SKILL.md", live_metadata, live_initial)
    if any(path != source / "SKILL.md" for path in source.rglob("SKILL.md")):
        live_issues = sorted(set([*live_issues, "nested-skill-packages"]))
    live_hard_issues = sorted(set(live_issues).intersection(HARD_ISSUES))
    if live_hard_issues and not unsafe:
        raise CatalogError(
            f"Live validation quarantined {skill['id']} ({', '.join(live_hard_issues)}); pass --unsafe to override"
        )
    live_risk = scan_risks(source, root)
    if live_risk["level"] in {"medium", "high"} and not accept_risk:
        signals = ", ".join(signal["id"] for signal in live_risk["signals"])
        raise CatalogError(
            f"Live scan found {live_risk['level']} risk signals ({signals}); pass --accept-risk after review"
        )
    target = (destination / skill["name"]).resolve()
    try:
        target.relative_to(destination)
    except ValueError as exc:
        raise CatalogError("Unsafe target skill name") from exc

    manifest = load_install_manifest(destination)
    installed = manifest["installed"]
    existing = installed.get(skill["name"])
    if target.exists():
        if not force:
            raise CatalogError(
                f"Target already exists: {target}; use update or --force"
            )
        if not existing or existing.get("id") != skill["id"]:
            raise CatalogError(
                f"Refusing to replace untracked or differently tracked directory: {target}"
            )
        current = "sha256:" + hash_directory(target)
        if current != existing.get("checksum") and not discard_changes:
            raise CatalogError(
                "Installed skill has local changes; pass --discard-changes to replace it"
            )
    return source, target


def install_skill(
    root: Path,
    skill: Mapping[str, Any],
    destination: Path,
    *,
    accept_risk: bool = False,
    unsafe: bool = False,
    force: bool = False,
    discard_changes: bool = False,
) -> Path:
    source, target = preflight_install(
        root,
        skill,
        destination,
        accept_risk=accept_risk,
        unsafe=unsafe,
        force=force,
        discard_changes=discard_changes,
    )
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    manifest = load_install_manifest(destination)
    installed = manifest["installed"]

    temp_dir = Path(tempfile.mkdtemp(prefix=f".{skill['name']}-", dir=str(destination)))
    staged = temp_dir / skill["name"]
    backup: Optional[Path] = None
    try:
        shutil.copytree(source, staged)
        if target.exists():
            backup = temp_dir / "previous"
            target.replace(backup)
            try:
                staged.replace(target)
            except Exception:
                backup.replace(target)
                raise
        else:
            staged.replace(target)
        installed[skill["name"]] = {
            "id": skill["id"],
            "checksum": skill["checksum"],
            "risk_level": skill["risk"]["level"],
        }
        try:
            write_install_manifest(destination, manifest)
        except Exception:
            if target.exists():
                shutil.rmtree(target)
            if backup is not None and backup.exists():
                backup.replace(target)
            raise
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return target


def remove_skill(destination: Path, selector: str, *, force: bool = False) -> Path:
    destination = destination.resolve()
    manifest = load_install_manifest(destination)
    installed = manifest["installed"]
    matches = [
        (name, data)
        for name, data in installed.items()
        if name == selector or data.get("id") == selector
    ]
    if not matches:
        raise CatalogError(f"No tracked installed skill matches {selector!r}")
    if len(matches) > 1:
        raise CatalogError(f"Installed selector {selector!r} is ambiguous")
    name, data = matches[0]
    target = (destination / name).resolve()
    try:
        target.relative_to(destination)
    except ValueError as exc:
        raise CatalogError("Unsafe installed skill path") from exc
    if not target.is_dir():
        raise CatalogError(f"Tracked directory is missing: {target}")
    current = "sha256:" + hash_directory(target)
    if current != data.get("checksum") and not force:
        raise CatalogError(
            "Installed skill has local changes; pass --force to remove it"
        )
    shutil.rmtree(target)
    del installed[name]
    write_install_manifest(destination, manifest)
    return target


def doctor(destination: Path) -> List[str]:
    destination = destination.resolve()
    manifest = load_install_manifest(destination)
    problems: List[str] = []
    for name, data in sorted(manifest["installed"].items()):
        target = destination / name
        if not target.is_dir():
            problems.append(f"{name}: tracked directory is missing")
            continue
        if not (target / "SKILL.md").is_file():
            problems.append(f"{name}: SKILL.md is missing")
            continue
        current = "sha256:" + hash_directory(target)
        if current != data.get("checksum"):
            problems.append(
                f"{name}: local contents differ from the installed checksum"
            )
        metadata, initial, _ = parse_frontmatter(target / "SKILL.md")
        issues = validate_metadata(target / "SKILL.md", metadata, initial)
        if issues:
            problems.append(f"{name}: {', '.join(issues)}")
    return problems


def load_profiles(root: Path) -> Dict[str, Any]:
    if not (root / PROFILES_FILENAME).is_file():
        return {"schema_version": SCHEMA_VERSION, "profiles": {}}
    return load_json(root / PROFILES_FILENAME)


def validate_profiles(root: Path, catalog: Mapping[str, Any]) -> List[str]:
    profiles = load_profiles(root).get("profiles", {})
    if not isinstance(profiles, dict):
        return ["profiles.json: profiles must be an object"]
    by_id = {skill["id"]: skill for skill in catalog["skills"]}
    problems: List[str] = []
    for name, profile in sorted(profiles.items()):
        if not isinstance(profile, dict) or not isinstance(profile.get("skills"), list):
            problems.append(f"profile {name}: expected an object with a skills list")
            continue
        seen = set()
        for skill_id in profile["skills"]:
            if skill_id in seen:
                problems.append(f"profile {name}: duplicate skill {skill_id}")
                continue
            seen.add(skill_id)
            skill = by_id.get(skill_id)
            if skill is None:
                problems.append(f"profile {name}: unknown skill {skill_id}")
            elif skill["status"] != "ready":
                problems.append(f"profile {name}: quarantined skill {skill_id}")
            elif skill["risk"]["level"] != "low":
                problems.append(
                    f"profile {name}: {skill['risk']['level']}-risk skill {skill_id} is not allowed"
                )
    return problems


def sync_readme_text(readme_text: str, catalog: Mapping[str, Any]) -> str:
    total = catalog["stats"]["total"]
    text = re.sub(
        r"(library of \*\*)[\d,]+( agent skills\*\*)",
        lambda m: f"{m.group(1)}{total:,}{m.group(2)}",
        readme_text,
        count=1,
    )
    counts = catalog["stats"]["categories"]
    for slug, count in counts.items():
        label = CATEGORY_LABELS[slug]
        text = re.sub(
            rf"(- \[(?:⭐ )?{re.escape(label)}[^\]]*\]\([^\n]+?\) — )[\d,]+( skills)",
            rf"\g<1>{count:,}\g<2>",
            text,
            count=1,
        )
        folder_pattern = re.compile(
            rf"\*\*[\d,]+ skills\*\* · folder: \[`{re.escape(slug)}/`\]\({re.escape(slug)}\)"
        )
        text = folder_pattern.sub(
            f"**{count:,} skills** · folder: [`{slug}/`]({slug})", text, count=1
        )

    begin = "<!-- BEGIN GENERATED CATALOG STATUS -->"
    end = "<!-- END GENERATED CATALOG STATUS -->"
    stats = catalog["stats"]
    block = (
        f"{begin}\n"
        "## Catalog status\n\n"
        f"- **{stats['total']:,}** discovered skills\n"
        f"- **{stats['statuses']['ready']:,}** format-valid and installable by default\n"
        f"- **{stats['statuses']['quarantined']:,}** quarantined until their format issues are fixed\n"
        f"- **{stats['risk_levels']['high']:,}** with high-risk static-analysis signals requiring explicit acceptance\n"
        f"- **{stats['exact_duplicate_packages']['redundant_entries']:,}** exact duplicate packages hidden from search by default\n\n"
        "Counts and status are generated by `skills-collection readme`; do not edit them manually.\n"
        f"{end}"
    )
    if begin in text and end in text:
        text = re.sub(
            re.escape(begin) + r".*?" + re.escape(end), block, text, flags=re.DOTALL
        )
    else:
        marker = re.search(r"^## Contents\s*$", text, flags=re.MULTILINE)
        if not marker:
            raise CatalogError("README is missing the Contents heading")
        text = text[: marker.start()] + block + "\n\n" + text[marker.start() :]
    return text
