# Releasing Hybro

This guide is for maintainers. Users install and upgrade Hybro as described in
the [README](README.md).

## Release flow

`release-please` owns version numbers; everything else is produced from them.

1. Merge the release-please PR. It bumps `VERSION`, `backend/pyproject.toml`,
   `backend/uv.lock`, `frontend/package.json`, `frontend/package-lock.json`,
   `packaging/npm/cli/package.json`, and the
   `HYBRO_VERSION` line in `scripts/hybro` together, and creates the
   `v<version>` GitHub Release.
2. That same workflow then calls the Release workflow as a reusable workflow,
   which publishes for that one version:
   - the `ghcr.io/hybroai/hybro:<version>` image (and `latest`), built from the
     root `Dockerfile` on native amd64 and arm64 runners and merged into one
     manifest once both exist;
   - the release assets `hybro` (the script), `install.sh`, and `SHA256SUMS`;
   - the `@hybroai/cli` npm package, whose bin is `scripts/hybro`.
3. The final job confirms the image exists for both architectures, installs the
   npm package and downloads the `hybro` asset and requires both to print
   `hybro <version>`, and runs `install.sh` in a clean Ubuntu container.

The Release workflow first checks that the requested version equals `VERSION`
and the `HYBRO_VERSION` line in `scripts/hybro`, so a mismatched tree publishes
nothing.

The Release workflow is called rather than triggered by `release: published`
because release-please acts with `GITHUB_TOKEN`, and GitHub does not start
workflows for events that token triggers — a release event would never arrive.
`workflow_dispatch` remains for re-publishing after a failure; give it the
version so a retry cannot silently publish whatever `VERSION` happens to say.
The same rule explains why release-please PRs get no automatic CI: GitHub does
start those runs, but holds them until someone with write access approves them.

## The script's version marker

`scripts/hybro` pins the image it pulls with one line:

```sh
HYBRO_VERSION="0.4.0" # x-release-please-version
```

Release Please updates it through the `generic` entry in
`release-please-config.json`. `install.sh` and `hybro upgrade` read the same
line. Keep the value and the comment on one line, or the release stops updating
it.

The `backend/uv.lock` entry must filter with `@.name.value`, not `@.name`.
Release Please's TOML parser wraps every scalar as `{start, end, value}`, so
`@.name == 'hybro-backend'` never matches
([googleapis/release-please#2455](https://github.com/googleapis/release-please/issues/2455)).

## Minor versions

`release-please-config.json` sets `bump-patch-for-minor-pre-major`, so `feat:`
commits on 0.x only bump the patch. To release the next minor version, give a
commit on `main` a footer such as:

```text
Release-As: 0.5.0
```

A squash merge must keep the footer in the squash commit message. Never bump the
versions by hand.

## Requirements

The workflow needs an `NPM_TOKEN` repository secret with publish rights for the
`@hybroai` scope, and the scope must already exist on npm. The
`ghcr.io/hybroai/hybro` package must be public; GitHub creates it private, and a
private image makes `hybro` fail with an authorization error.

`install.sh` downloads from `releases/latest`, so a release must be marked as
the latest one for `install.sh` to install it.

## Building the image locally

From a checkout, `./scripts/hybro start --build` builds `hybro:dev` from the
working tree and runs it on port 3100. `docker build -t hybro:dev .` builds the
same image without starting it.
