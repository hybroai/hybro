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

## Releasing 0.4.0

`release-please-config.json` sets `bump-patch-for-minor-pre-major`, so `feat:`
commits on 0.x only bump the patch. The merge commit that brings in the single
image must therefore carry this footer, and the PR description must say so:

```text
Release-As: 0.4.0
```

Without it, Release Please proposes 0.3.1. The Release workflow refuses a 0.3.x
version from a tree without `docker-compose.yml`, so such a release publishes
nothing; close that release PR, add the footer in an empty commit on `main`, and
let Release Please propose 0.4.0. Never bump the versions by hand.

## 0.4.0 compatibility archives

A 0.3 CLI's `hybro upgrade` prints download steps for
`hybro-<version>-<platform>.tar.gz` and its `.sha256`. For 0.4.0 only, the
Release workflow also publishes those four archives (`darwin-arm64`,
`darwin-x64`, `linux-arm64`, `linux-x64`), each holding the shell script at
`hybro/hybro`, so the printed steps install the 0.4 script. Later releases
publish only `hybro`, `install.sh`, and `SHA256SUMS`; remove the archive step
from `release.yml` after 0.4.0 ships.

## npm platform packages

0.4 no longer publishes `@hybroai/cli-darwin-arm64`, `@hybroai/cli-darwin-x64`,
`@hybroai/cli-linux-arm64`, or `@hybroai/cli-linux-x64`. After 0.4.0 is
published, deprecate them once by hand:

```sh
for p in darwin-arm64 darwin-x64 linux-arm64 linux-x64; do
  npm deprecate "@hybroai/cli-$p" "Replaced by @hybroai/cli 0.4, which no longer needs a platform package."
done
```

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
