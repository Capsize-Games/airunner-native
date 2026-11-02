#!/usr/bin/env bash
set -euo pipefail

API_VERSION=2026-03-10
EXPECTED_REPOSITORY=Capsize-Games/airunner-native

usage() {
    echo "Usage: $0 --check-tag TAG | --verify-published-tag TAG | TAG ASSET [ASSET ...]" >&2
}
if [[ $# -lt 2 ]]; then usage; exit 2; fi
mode=promote
if [[ "$1" == --check-tag || "$1" == --verify-published-tag ]]; then
    mode=${1#--}; shift
    [[ $# -eq 1 ]] || { usage; exit 2; }
    tag=$1
    assets=()
else
    tag=$1; shift; assets=("$@")
fi
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${GITHUB_SHA:?GITHUB_SHA is required}"
: "${GITHUB_REF:?GITHUB_REF is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"
if [[ "$GITHUB_REPOSITORY" != "$EXPECTED_REPOSITORY" ||
      ! "$tag" =~ ^v[0-9][A-Za-z0-9.+_-]{0,126}$ ||
      ! "$GITHUB_SHA" =~ ^[0-9a-f]{40}$ ||
      "$GITHUB_REF" != "refs/tags/$tag" ]] || ! git check-ref-format "refs/tags/$tag"; then
    echo "Invalid source SHA, repository, or version tag" >&2; exit 1
fi

tag_ref="refs/tags/$tag"
if [[ "$(git cat-file -t "$tag_ref" 2>/dev/null || true)" != tag ]]; then
    echo "Release ref must be an annotated local Git tag: $tag" >&2; exit 1
fi
local_tag_object=$(git rev-parse "$tag_ref")
local_source=$(git rev-parse "$tag_ref^{commit}")
head=$(git rev-parse HEAD)
if [[ "$local_source" != "$GITHUB_SHA" || "$head" != "$GITHUB_SHA" ]]; then
    echo "Local tag target, checkout HEAD, and GITHUB_SHA must match" >&2; exit 1
fi
remote_ref=$(gh api "repos/$GITHUB_REPOSITORY/git/ref/tags/$tag" -H "X-GitHub-Api-Version: $API_VERSION" --jq '[.object.sha, .object.type] | @tsv')
read -r remote_tag_object remote_ref_type <<< "$remote_ref"
if [[ "$remote_ref_type" != tag || "$remote_tag_object" != "$local_tag_object" ]]; then
    echo "Remote release ref is not the same annotated tag object" >&2; exit 1
fi
remote_tag=$(gh api "repos/$GITHUB_REPOSITORY/git/tags/$remote_tag_object" -H "X-GitHub-Api-Version: $API_VERSION" --jq '[.object.sha, .object.type] | @tsv')
read -r remote_source remote_target_type <<< "$remote_tag"
if [[ "$remote_target_type" != commit || "$remote_source" != "$GITHUB_SHA" ]]; then
    echo "Remote annotated tag does not peel to GITHUB_SHA" >&2; exit 1
fi
release_route="repos/$GITHUB_REPOSITORY/releases/tags/$tag"
fetch_release() { gh api "$release_route" -H "X-GitHub-Api-Version: $API_VERSION"; }

if [[ "$mode" == check-tag ]]; then
    echo "verified annotated source tag $tag"; exit 0
fi
if [[ "$mode" == verify-published-tag ]]; then
    release_json=$(fetch_release)
    jq -e --arg tag "$tag" '.tag_name == $tag and .draft == false and .prerelease == false and .immutable == true' <<< "$release_json" >/dev/null || {
        echo "Published release is not immutable and stable" >&2; exit 1;
    }
    release_id=$(jq -r .id <<< "$release_json")
    assets_json=$(gh api "repos/$GITHUB_REPOSITORY/releases/$release_id/assets?per_page=100" -H "X-GitHub-Api-Version: $API_VERSION")
    for name in runtime-sidecars-linux.tar.gz runtime-sidecars-linux.tar.gz.sha256 runtime-sidecars-linux-provenance.txt \
        runtime-sidecars-windows.tar.gz runtime-sidecars-windows.tar.gz.sha256 runtime-sidecars-windows-provenance.txt; do
        jq -e --arg name "$name" 'any(.[]; .name == $name and .state == "uploaded" and (.digest | type == "string" and test("^sha256:[0-9a-f]{64}$")))' <<< "$assets_json" >/dev/null || {
            echo "Published release is missing verified asset: $name" >&2; exit 1;
        }
    done
    echo "verified immutable native sidecar release $tag"; exit 0
fi

expected_names=(runtime-sidecars-linux.tar.gz runtime-sidecars-linux.tar.gz.sha256 runtime-sidecars-linux-provenance.txt \
    runtime-sidecars-windows.tar.gz runtime-sidecars-windows.tar.gz.sha256 runtime-sidecars-windows-provenance.txt)
if [[ ${#assets[@]} -ne ${#expected_names[@]} ]]; then
    echo "Promotion needs the six Linux and Windows bundle, checksum, and provenance assets" >&2; exit 2
fi
declare -A requested_names=()
for index in "${!assets[@]}"; do
    asset=${assets[$index]}; expected=${expected_names[$index]}
    if [[ "${asset##*/}" != "$expected" || ! -f "$asset" || -L "$asset" || -v "requested_names[$expected]" ]]; then
        echo "Unexpected, unsafe, or duplicate native release asset: $asset" >&2; exit 1
    fi
    requested_names[$expected]=1
done

for platform in linux windows; do
    archive="runtime-sidecars-$platform.tar.gz"; checksum="$archive.sha256"
    provenance="runtime-sidecars-$platform-provenance.txt"
    for asset in "${assets[@]}"; do
        case "${asset##*/}" in
            "$archive") archive_path=$asset ;;
            "$checksum") checksum_path=$asset ;;
            "$provenance") provenance_path=$asset ;;
        esac
    done
    actual_sha=$(sha256sum "$archive_path" | awk '{print $1}')
    if [[ "$(cat "$checksum_path")" != "$actual_sha  $archive" ]]; then
        echo "Sidecar archive checksum does not match: $archive" >&2; exit 1
    fi
    if ! grep -Fqx "source_repository=$GITHUB_REPOSITORY" "$provenance_path" ||
       ! grep -Fqx "source_sha=$GITHUB_SHA" "$provenance_path" ||
       ! grep -Fqx "source_ref=$GITHUB_REF" "$provenance_path" ||
       ! grep -Fqx "target_platform=$platform" "$provenance_path"; then
        echo "Sidecar provenance does not match the selected tag and source: $platform" >&2; exit 1
    fi
done

release_json=""
if release_json=$(fetch_release 2>/dev/null); then
    release_id=$(jq -er --arg tag "$tag" 'if .tag_name == $tag and .draft == true and .prerelease == false and .immutable != true then .id else error("unexpected release") end' <<< "$release_json") || {
        echo "Existing release is not the expected mutable draft" >&2; exit 1;
    }
else
    gh release create "$tag" --draft --verify-tag --title "$tag" --notes "" --target "$GITHUB_SHA" --repo "$GITHUB_REPOSITORY"
    release_json=$(fetch_release)
    release_id=$(jq -er --arg tag "$tag" 'if .tag_name == $tag and .draft == true and .prerelease == false then .id else error("bad created draft") end' <<< "$release_json")
fi

tmp_dir=$(mktemp -d)
trap 'rm -rf -- "$tmp_dir"' EXIT
expected_file="$tmp_dir/expected.tsv"; actual_file="$tmp_dir/actual.tsv"
for asset in "${assets[@]}"; do
    digest=$(sha256sum "$asset" | awk '{print $1}')
    printf '%s\tsha256:%s\n' "${asset##*/}" "$digest" >> "$expected_file"
done
sort -o "$expected_file" "$expected_file"
assets_route="repos/$GITHUB_REPOSITORY/releases/$release_id/assets?per_page=100"
assets_json=$(gh api "$assets_route" -H "X-GitHub-Api-Version: $API_VERSION")
jq -e 'type == "array" and all(.[]; .state == "uploaded" and (.digest | type == "string"))' <<< "$assets_json" >/dev/null || {
    echo "Draft release asset records are invalid" >&2; exit 1;
}
jq -r '.[] | [.name, .digest] | @tsv' <<< "$assets_json" | sort > "$actual_file"
while IFS=$'\t' read -r name digest; do
    expected_digest=$(awk -F '\t' -v n="$name" '$1 == n {print $2}' "$expected_file")
    if [[ -z "$expected_digest" || "$expected_digest" != "$digest" ]]; then
        echo "Draft has an unexpected or changed asset: $name" >&2; exit 1
    fi
done < "$actual_file"
missing_assets=()
while IFS=$'\t' read -r name _digest; do
    if ! cut -f1 "$actual_file" | grep -Fqx -- "$name"; then
        for asset in "${assets[@]}"; do
            if [[ "${asset##*/}" == "$name" ]]; then missing_assets+=("$asset"); break; fi
        done
    fi
done < "$expected_file"
if [[ ${#missing_assets[@]} -gt 0 ]]; then
    gh release upload "$tag" "${missing_assets[@]}" --repo "$GITHUB_REPOSITORY"
fi
assets_json=$(gh api "$assets_route" -H "X-GitHub-Api-Version: $API_VERSION")
jq -r '.[] | [.name, .digest] | @tsv' <<< "$assets_json" | sort > "$actual_file"
if ! cmp -s "$expected_file" "$actual_file"; then
    echo "Draft release assets differ from the verified local files" >&2; exit 1
fi
published_json=$(gh api "repos/$GITHUB_REPOSITORY/releases/$release_id" --method PATCH -F draft=false -H "X-GitHub-Api-Version: $API_VERSION")
jq -e --arg tag "$tag" '.tag_name == $tag and .draft == false and .prerelease == false and .immutable == true' <<< "$published_json" >/dev/null || {
    echo "GitHub did not publish an immutable stable release" >&2; exit 1;
}
final_json=$(fetch_release)
jq -e --arg tag "$tag" '.tag_name == $tag and .draft == false and .prerelease == false and .immutable == true' <<< "$final_json" >/dev/null || {
    echo "Published immutable release could not be confirmed" >&2; exit 1;
}
assets_json=$(gh api "$assets_route" -H "X-GitHub-Api-Version: $API_VERSION")
jq -r '.[] | [.name, .digest] | @tsv' <<< "$assets_json" | sort > "$actual_file"
if ! cmp -s "$expected_file" "$actual_file"; then
    echo "Published release asset digests changed" >&2; exit 1
fi
echo "published and verified immutable native sidecar release $tag"
