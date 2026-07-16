# Security policy

This repository mirrors third-party agent skills. Inclusion is not an audit or an endorsement.

## Trust model

- Every imported skill starts as `unreviewed`.
- Format-invalid skills are quarantined and cannot be installed by default.
- Static-analysis findings are included in `catalog.json`.
- Medium- and high-risk skills require `--accept-risk` before installation.
- The installer rejects symlinks, writes through a staging directory, tracks checksums, and refuses to replace or remove untracked folders.
- Upstream code is never executed during catalog generation.

Static analysis is a warning system, not proof of safety. Review `SKILL.md`, scripts, dependencies, network calls, and requested permissions before granting an installed skill tool access.

## Reporting a vulnerability

Do not open a public issue for a live credential or an exploitable vulnerability. Use GitHub's private vulnerability reporting for this repository. Include the catalog skill ID, affected path, impact, and a minimal reproduction that does not expose real secrets.

## Maintainer response

Maintainers should quarantine affected skills immediately, revoke exposed credentials, preserve evidence privately, and only restore installability after validation and review.

