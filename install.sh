#!/bin/sh
# Install the hybro command: curl -fsSL https://hybro.ai/install.sh | sh
#
# Downloads the latest release's hybro script and SHA256SUMS, verifies the
# script, installs it into ~/.local/bin (or a writable /usr/local/bin when
# ~/.local/bin is not on PATH), and pulls the image. It never runs sudo and
# never edits shell profiles.
set -u

DOWNLOAD_BASE="https://github.com/hybroai/hybro/releases/latest/download"
SYSTEM_BIN=/usr/local/bin

say() { printf '%s\n' "$*"; }
die() {
    printf 'hybro install: %s\n' "$*" >&2
    exit 1
}

on_path() {
    case ":$PATH:" in
    *":$1:"*) return 0 ;;
    *) return 1 ;;
    esac
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{ print $1 }'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{ print $1 }'
    else
        die "neither sha256sum nor shasum is installed"
    fi
}

# The whole script is read before anything runs, so a cut-off download
# through a pipe does nothing.
main() {
    case $(uname -s) in
    Darwin | Linux) ;;
    *) die "Hybro supports macOS and Linux; this system is $(uname -s)." ;;
    esac
    case $(uname -m) in
    x86_64 | amd64 | arm64 | aarch64) ;;
    *) die "Hybro supports amd64 and arm64; this machine is $(uname -m)." ;;
    esac
    command -v curl >/dev/null 2>&1 || die "curl is required."
    command -v docker >/dev/null 2>&1 ||
        die "Docker is required. Install it from https://docs.docker.com/get-docker/ and run this again."

    : "${HOME:?HOME is not set}"
    target=$HOME/.local/bin
    if ! on_path "$target" && [ -d "$SYSTEM_BIN" ] && [ -w "$SYSTEM_BIN" ]; then
        target=$SYSTEM_BIN
    fi
    mkdir -p -- "$target" || die "cannot create $target"

    # Temporary files in the target directory, so the final mv is atomic.
    new=$target/.hybro-install.$$
    sums=$new.sums
    trap 'rm -f "$new" "$sums"' EXIT
    trap 'exit 130' INT TERM
    say "Downloading hybro..."
    if ! curl -fsSL --max-time 60 -o "$new" "$DOWNLOAD_BASE/hybro" ||
        ! curl -fsSL --max-time 60 -o "$sums" "$DOWNLOAD_BASE/SHA256SUMS"; then
        rm -f "$new" "$sums"
        die "the download failed; nothing was installed."
    fi
    expected=$(awk '$2 == "hybro" || $2 == "*hybro" { print $1 }' "$sums")
    actual=$(sha256_of "$new")
    rm -f "$sums"
    if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
        rm -f "$new"
        die "the downloaded script does not match SHA256SUMS; nothing was installed."
    fi
    version=$(sed -n 's/^HYBRO_VERSION="\([0-9][0-9.]*\)".*/\1/p' "$new")
    if ! chmod 755 "$new" || ! mv -f "$new" "$target/hybro"; then
        rm -f "$new"
        die "cannot install into $target"
    fi
    say "Installed hybro $version at $target/hybro"

    if [ -z "$version" ]; then
        say "The script has no version; hybro pulls its image on the first start."
    elif docker info >/dev/null 2>&1; then
        say "Pulling the Hybro image..."
        docker pull "ghcr.io/hybroai/hybro:$version" ||
            say "The image pull failed; hybro pulls it on its first start."
    else
        say "Docker is not running. Start it before running hybro."
    fi

    if ! on_path "$target"; then
        say ""
        say "$target is not on your PATH. Add this line to your shell profile:"
        say "  export PATH=\"$target:\$PATH\""
        say "Then start Hybro with: hybro"
    elif [ "$(command -v hybro)" != "$target/hybro" ]; then
        say ""
        say "Another hybro comes first on your PATH: $(command -v hybro)"
        say "Remove it (for an npm install: npm uninstall -g @hybroai/cli), or run $target/hybro"
    else
        say "Start Hybro with: hybro"
    fi
}

main "$@"
