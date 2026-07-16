from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional

from .core import (
    BASELINE_FILENAME,
    CATALOG_FILENAME,
    CatalogError,
    baseline_from_catalog,
    build_catalog,
    canonical_json,
    compare_baseline,
    doctor,
    find_repo_root,
    install_skill,
    load_catalog,
    load_json,
    load_profiles,
    preflight_install,
    remove_skill,
    resolve_skill,
    search_catalog,
    sync_readme_text,
    validate_profiles,
    write_json,
    write_text_atomic,
)


def repository(args: argparse.Namespace) -> Path:
    return find_repo_root(Path(args.repo).resolve() if args.repo else None)


def print_skill(skill: Mapping[str, Any], *, verbose: bool = False) -> None:
    print(f"{skill['name']}  [{skill['status']}; risk={skill['risk']['level']}]")
    print(f"  ID: {skill['id']}")
    if skill.get("description"):
        print(f"  {skill['description']}")
    if verbose:
        print(f"  Category: {skill['category']} / {skill['source']}")
        print(f"  Upstream: {skill.get('upstream_url') or 'unknown'}")
        print(f"  License: {skill.get('license') or 'unknown'}")
        print(f"  Checksum: {skill['checksum']}")
        if skill["issues"]:
            print(f"  Issues: {', '.join(skill['issues'])}")
        if skill["warnings"]:
            print(
                "  Warnings: " + ", ".join(item["code"] for item in skill["warnings"])
            )
        if skill["risk"]["signals"]:
            print(
                "  Risk signals: "
                + ", ".join(item["id"] for item in skill["risk"]["signals"])
            )


def command_build(args: argparse.Namespace) -> int:
    root = repository(args)
    catalog = build_catalog(root)
    output = root / CATALOG_FILENAME
    rendered = canonical_json(catalog)
    if args.check:
        if not output.is_file() or output.read_text(encoding="utf-8") != rendered:
            print(
                f"{CATALOG_FILENAME} is stale; run `skills-collection build`",
                file=sys.stderr,
            )
            return 1
        print(f"{CATALOG_FILENAME} is current ({catalog['stats']['total']} skills)")
        return 0
    write_text_atomic(output, rendered)
    print(
        f"Wrote {output} with {catalog['stats']['total']} skills "
        f"({catalog['stats']['statuses']['quarantined']} quarantined)"
    )
    return 0


def command_baseline(args: argparse.Namespace) -> int:
    root = repository(args)
    catalog = build_catalog(root)
    baseline = baseline_from_catalog(catalog)
    write_json(root / BASELINE_FILENAME, baseline)
    print(
        f"Wrote {root / BASELINE_FILENAME} with {len(baseline['entries'])} tracked entries"
    )
    return 0


def command_validate(args: argparse.Namespace) -> int:
    root = repository(args)
    catalog = build_catalog(root)
    stats = catalog["stats"]
    print(
        f"Scanned {stats['total']} skills: {stats['statuses']['ready']} ready, "
        f"{stats['statuses']['quarantined']} quarantined, {stats['risk_levels']['high']} high-risk"
    )
    profile_problems = validate_profiles(root, catalog)
    if profile_problems:
        print("Profile validation failed:", file=sys.stderr)
        for problem in profile_problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    if args.baseline:
        baseline_path = root / args.baseline
        baseline = load_json(baseline_path)
        regressions = compare_baseline(catalog, baseline)
        if regressions:
            print("Validation regressions:", file=sys.stderr)
            for regression in regressions[:100]:
                print(f"  {regression}", file=sys.stderr)
            return 1
        print(f"No new issues or risk signals relative to {baseline_path.name}")
    if args.strict and stats["statuses"]["quarantined"]:
        print(
            "Strict validation failed because quarantined skills remain",
            file=sys.stderr,
        )
        return 1
    return 0


def command_search(args: argparse.Namespace) -> int:
    catalog = load_catalog(repository(args))
    results = search_catalog(
        catalog,
        args.query,
        limit=args.limit,
        include_quarantined=args.include_quarantined,
        include_duplicates=args.include_duplicates,
    )
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    for index, skill in enumerate(results):
        if index:
            print()
        print_skill(skill)
    return 0 if results else 1


def command_inspect(args: argparse.Namespace) -> int:
    catalog = load_catalog(repository(args))
    skill = resolve_skill(catalog, args.selector)
    if args.json:
        print(json.dumps(skill, ensure_ascii=False, indent=2))
    else:
        print_skill(skill, verbose=True)
    return 0


def selectors_for_install(args: argparse.Namespace, root: Path) -> List[str]:
    selectors: List[str] = []
    if args.selector:
        selectors.append(args.selector)
    if args.profile:
        profiles = load_profiles(root).get("profiles", {})
        profile = profiles.get(args.profile)
        if not isinstance(profile, dict):
            raise CatalogError(f"Unknown profile {args.profile!r}")
        selectors.extend(profile.get("skills", []))
    if not selectors:
        raise CatalogError("Provide a skill selector or --profile")
    return list(dict.fromkeys(selectors))


def command_install(args: argparse.Namespace) -> int:
    root = repository(args)
    catalog = load_catalog(root)
    destination = Path(args.dest)
    skills = [
        resolve_skill(catalog, selector)
        for selector in selectors_for_install(args, root)
    ]
    if args.dry_run:
        for skill in skills:
            _, target = preflight_install(
                root,
                skill,
                destination,
                accept_risk=args.accept_risk,
                unsafe=args.unsafe,
                force=args.force,
                discard_changes=args.discard_changes,
            )
            print(f"Would install {skill['id']} to {target}")
        return 0

    installed: List[Path] = []
    for skill in skills:
        installed.append(
            install_skill(
                root,
                skill,
                destination,
                accept_risk=args.accept_risk,
                unsafe=args.unsafe,
                force=args.force,
                discard_changes=args.discard_changes,
            )
        )
    for path in installed:
        print(f"Installed {path}")
    return 0


def command_update(args: argparse.Namespace) -> int:
    args.force = True
    return command_install(args)


def command_remove(args: argparse.Namespace) -> int:
    path = remove_skill(Path(args.dest), args.selector, force=args.force)
    print(f"Removed {path}")
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    problems = doctor(Path(args.dest))
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    print("Installed skills are healthy")
    return 0


def command_profiles(args: argparse.Namespace) -> int:
    profiles = load_profiles(repository(args)).get("profiles", {})
    for name, profile in sorted(profiles.items()):
        print(
            f"{name}: {profile.get('description', '')} ({len(profile.get('skills', []))} skills)"
        )
    return 0


def command_readme(args: argparse.Namespace) -> int:
    root = repository(args)
    catalog = build_catalog(root)
    path = root / "README.md"
    current = path.read_text(encoding="utf-8")
    rendered = sync_readme_text(current, catalog)
    if args.check:
        if current != rendered:
            print("README.md catalog counts/status are stale", file=sys.stderr)
            return 1
        print("README.md catalog counts/status are current")
        return 0
    write_text_atomic(path, rendered)
    print("Updated README.md catalog counts/status")
    return 0


def command_check(args: argparse.Namespace) -> int:
    root = repository(args)
    catalog = build_catalog(root)
    failures: List[str] = []

    catalog_path = root / CATALOG_FILENAME
    if not catalog_path.is_file() or catalog_path.read_text(
        encoding="utf-8"
    ) != canonical_json(catalog):
        failures.append(f"{CATALOG_FILENAME} is stale; run `skills-collection build`")

    baseline = load_json(root / BASELINE_FILENAME)
    failures.extend(compare_baseline(catalog, baseline))
    failures.extend(validate_profiles(root, catalog))

    readme_path = root / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    if readme != sync_readme_text(readme, catalog):
        failures.append("README.md catalog counts/status are stale")

    if failures:
        print("Repository check failed:", file=sys.stderr)
        for failure in failures[:100]:
            print(f"  {failure}", file=sys.stderr)
        return 1
    stats = catalog["stats"]
    print(
        f"Repository check passed: {stats['total']} skills, "
        f"{stats['statuses']['ready']} ready, {stats['statuses']['quarantined']} quarantined"
    )
    return 0


def add_repo_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", help="repository root (auto-detected by default)")


def add_install_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "selector", nargs="?", help="exact catalog ID or unique skill name"
    )
    parser.add_argument("--profile", help="install a curated profile")
    parser.add_argument("--dest", required=True, help="agent skills directory")
    parser.add_argument(
        "--accept-risk", action="store_true", help="accept static-analysis risk signals"
    )
    parser.add_argument(
        "--unsafe", action="store_true", help="allow quarantined nonconforming skills"
    )
    parser.add_argument(
        "--force", action="store_true", help="replace the same tracked installation"
    )
    parser.add_argument(
        "--discard-changes",
        action="store_true",
        help="discard local changes when updating a tracked installation",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and show changes without writing",
    )
    add_repo_argument(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skills-collection")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="generate catalog.json")
    build.add_argument("--check", action="store_true")
    add_repo_argument(build)
    build.set_defaults(handler=command_build)

    baseline = subparsers.add_parser(
        "baseline", help="record current validation and risk debt"
    )
    add_repo_argument(baseline)
    baseline.set_defaults(handler=command_baseline)

    validate = subparsers.add_parser("validate", help="validate all source skills")
    validate.add_argument("--baseline", nargs="?", const=BASELINE_FILENAME)
    validate.add_argument("--strict", action="store_true")
    add_repo_argument(validate)
    validate.set_defaults(handler=command_validate)

    search = subparsers.add_parser("search", help="search the generated catalog")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--include-quarantined", action="store_true")
    search.add_argument("--include-duplicates", action="store_true")
    search.add_argument("--json", action="store_true")
    add_repo_argument(search)
    search.set_defaults(handler=command_search)

    inspect = subparsers.add_parser(
        "inspect", help="inspect provenance and risk before install"
    )
    inspect.add_argument("selector")
    inspect.add_argument("--json", action="store_true")
    add_repo_argument(inspect)
    inspect.set_defaults(handler=command_inspect)

    install = subparsers.add_parser("install", help="safely install a skill or profile")
    add_install_arguments(install)
    install.set_defaults(handler=command_install)

    update = subparsers.add_parser(
        "update", help="update the same tracked skill installation"
    )
    add_install_arguments(update)
    update.set_defaults(handler=command_update)

    remove = subparsers.add_parser("remove", help="remove a tracked installation")
    remove.add_argument("selector")
    remove.add_argument("--dest", required=True)
    remove.add_argument("--force", action="store_true")
    remove.set_defaults(handler=command_remove)

    check = subparsers.add_parser(
        "doctor", help="check installed skills and local modifications"
    )
    check.add_argument("--dest", required=True)
    check.set_defaults(handler=command_doctor)

    profiles = subparsers.add_parser(
        "profiles", help="list curated installation profiles"
    )
    add_repo_argument(profiles)
    profiles.set_defaults(handler=command_profiles)

    readme = subparsers.add_parser(
        "readme", help="synchronize generated README counts/status"
    )
    readme.add_argument("--check", action="store_true")
    add_repo_argument(readme)
    readme.set_defaults(handler=command_readme)

    check_all = subparsers.add_parser(
        "check", help="run all repository integrity checks with one source scan"
    )
    add_repo_argument(check_all)
    check_all.set_defaults(handler=command_check)
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.handler(args))
    except CatalogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
