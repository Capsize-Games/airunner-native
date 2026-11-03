#!/usr/bin/env bash

set -euo pipefail

if [[ $# -ne 1 || ! "$1" =~ ^(linux|windows)$ ]]; then
    echo "Usage: $0 linux|windows" >&2
    exit 2
fi

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${GITHUB_SHA:?GITHUB_SHA is required}"
: "${GITHUB_REF:?GITHUB_REF is required}"
if [[ ! "$GITHUB_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    echo "GITHUB_SHA must be a full lowercase commit SHA" >&2
    exit 1
fi

platform="$1"
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bundle_dir="${AIRUNNER_SIDECAR_BUNDLE_DIR:-$root_dir/build/runtime-sidecars/$platform}"
manifest="$bundle_dir/share/airunner/runtime_manifest.env"
producer_recipe="$bundle_dir/share/airunner/producer-recipe.json"
output_dir="${AIRUNNER_SIDECAR_RELEASE_OUTPUT_DIR:-$root_dir}"
mkdir -p -- "$output_dir"
archive="$output_dir/runtime-sidecars-$platform.tar.gz"
checksum="$archive.sha256"
provenance="$output_dir/runtime-sidecars-$platform-provenance.txt"

if [[ ! -d "$bundle_dir" || -L "$bundle_dir" || ! -f "$manifest" || -L "$manifest" ]]; then
    echo "Expected built bundle and regular runtime manifest for $platform" >&2
    exit 1
fi
if [[ ! -f "$producer_recipe" || -L "$producer_recipe" ]]; then
    echo "Expected regular producer recipe receipt for $platform" >&2
    exit 1
fi
python3 "$root_dir/scripts/native_recipe_identity.py" verify \
    --profile cpu --output "$producer_recipe"
source_provenance="$bundle_dir/share/airunner/source_provenance.env"
if [[ -L "$source_provenance" || ( -e "$source_provenance" && ! -f "$source_provenance" ) ]]; then
    echo "Refusing unexpected source provenance path: $source_provenance" >&2
    exit 1
fi
expected_source_provenance="$(printf 'source_repository=%s\nsource_sha=%s\nsource_ref=%s\ntarget_platform=%s\n' \
    "$GITHUB_REPOSITORY" "$GITHUB_SHA" "$GITHUB_REF" "$platform")"
if [[ -e "$source_provenance" ]]; then
    if [[ "$(<"$source_provenance")" != "$expected_source_provenance" ]]; then
        echo "Existing source provenance does not match this workflow revision" >&2
        exit 1
    fi
else
    printf '%s' "$expected_source_provenance" > "$source_provenance"
fi

# Normalize archive ordering, timestamps, ownership, and permissions. This does
# not claim that compiled binaries are byte-reproducible across toolchains.
tar --sort=name --mtime='@0' --owner=0 --group=0 --numeric-owner \
    --mode='u+rwX,go+rX,go-w' --format=ustar -C "$bundle_dir" -cf - . \
    | gzip -n -9 > "$archive"
archive_sha="$(sha256sum "$archive" | awk '{print $1}')"
printf '%s  %s\n' "$archive_sha" "$(basename "$archive")" > "$checksum"

{
    printf 'format_version=1\n'
    printf 'source_repository=%s\n' "$GITHUB_REPOSITORY"
    printf 'source_sha=%s\n' "$GITHUB_SHA"
    printf 'source_ref=%s\n' "$GITHUB_REF"
    printf 'embedded_source_provenance=share/airunner/source_provenance.env\n'
    printf 'target_platform=%s\n' "$platform"
    printf 'archive=%s\n' "$(basename "$archive")"
    printf 'archive_sha256=%s\n' "$archive_sha"
    printf 'tar_version=%s\n' "$(tar --version | sed -n '1p')"
    printf 'gzip_version=%s\n' "$(gzip --version | sed -n '1p')"
    printf '\n# Build tool versions, pinned native source revisions, and binary hashes:\n'
    cat "$manifest"
    printf '\n# Committed producer recipe receipt:\n'
    cat "$producer_recipe"
    printf '\n# Archive metadata is normalized; compiled binaries are not claimed byte-reproducible.\n'
} > "$provenance"

printf 'Created %s, %s, and %s\n' "$archive" "$checksum" "$provenance"
