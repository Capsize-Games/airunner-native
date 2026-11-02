#!/usr/bin/env bash

set -euo pipefail

if [[ $# -lt 2 ]]; then
    echo "Usage: $0 TAG ASSET [ASSET ...]" >&2
    exit 2
fi

tag="$1"
shift
assets=("$@")
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${GITHUB_SHA:?GITHUB_SHA is required}"
: "${GITHUB_REF:?GITHUB_REF is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"

if [[ ! "$GITHUB_SHA" =~ ^[0-9a-f]{40}$ ]] || \
    [[ "$GITHUB_REF" != "refs/tags/$tag" ]] || ! git check-ref-format "refs/tags/$tag"; then
    echo "Invalid source SHA or release tag" >&2
    exit 1
fi

tag_ref="refs/tags/$tag"
if [[ "$(git cat-file -t "$tag_ref" 2>/dev/null || true)" != "tag" ]]; then
    echo "Release ref must be an annotated local Git tag: $tag" >&2
    exit 1
fi
local_tag_object="$(git rev-parse "$tag_ref")"
local_source="$(git rev-parse "$tag_ref^{commit}")"
head="$(git rev-parse HEAD)"
if [[ "$local_source" != "$GITHUB_SHA" || "$head" != "$GITHUB_SHA" ]]; then
    echo "Local tag target, checkout HEAD, and GITHUB_SHA must match" >&2
    exit 1
fi

remote_ref="$(gh api "repos/$GITHUB_REPOSITORY/git/ref/tags/$tag" --jq '[.object.sha, .object.type] | @tsv')"
read -r remote_tag_object remote_ref_type <<< "$remote_ref"
if [[ "$remote_ref_type" != "tag" || "$remote_tag_object" != "$local_tag_object" ]]; then
    echo "Remote release ref is not the same annotated tag object" >&2
    exit 1
fi
remote_tag="$(gh api "repos/$GITHUB_REPOSITORY/git/tags/$remote_tag_object" --jq '[.object.sha, .object.type] | @tsv')"
read -r remote_source remote_target_type <<< "$remote_tag"
if [[ "$remote_target_type" != "commit" || "$remote_source" != "$GITHUB_SHA" ]]; then
    echo "Remote annotated tag does not peel to GITHUB_SHA" >&2
    exit 1
fi

release_tag="$(gh release view "$tag" --repo "$GITHUB_REPOSITORY" --json tagName --jq .tagName)"
if [[ "$release_tag" != "$tag" ]]; then
    echo "Release tag identity mismatch: $release_tag" >&2
    exit 1
fi
existing_assets="$(gh release view "$tag" --repo "$GITHUB_REPOSITORY" --json assets --jq '.assets[].name')"
declare -A requested_names=()
for asset in "${assets[@]}"; do
    if [[ ! -f "$asset" || -L "$asset" ]]; then
        echo "Release asset is not a regular file: $asset" >&2
        exit 1
    fi
    name="${asset##*/}"
    if [[ -v "requested_names[$name]" ]]; then
        echo "Duplicate release asset name in upload set: $name" >&2
        exit 1
    fi
    requested_names["$name"]=1
    if printf '%s\n' "$existing_assets" | grep -Fqx -- "$name"; then
        echo "Release asset already exists; refusing replacement: $name" >&2
        exit 1
    fi
done

# gh release upload does not replace existing assets unless --clobber is passed.
# The complete collision check above runs before this one upload invocation.
gh release upload "$tag" "${assets[@]}" --repo "$GITHUB_REPOSITORY"
