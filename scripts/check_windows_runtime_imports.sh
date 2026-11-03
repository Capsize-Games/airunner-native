#!/usr/bin/env bash

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 WINDOWS-EXECUTABLE..." >&2
    exit 2
fi

objdump="${AIRUNNER_MINGW_OBJDUMP:-x86_64-w64-mingw32-objdump}"
if ! command -v "$objdump" >/dev/null 2>&1; then
    echo "MinGW objdump is required to inspect Windows runtime imports" >&2
    exit 1
fi

for executable in "$@"; do
    if [[ ! -f "$executable" || -L "$executable" ]]; then
        echo "Expected a regular Windows executable: $executable" >&2
        exit 1
    fi
    mapfile -t imports < <("$objdump" -p "$executable" | awk '/DLL Name:/ { print tolower($3) }')
    if [[ ${#imports[@]} -eq 0 ]]; then
        echo "No PE DLL imports found in $executable" >&2
        exit 1
    fi
    printf 'PE imports for %s:\n' "$executable"
    for dll in "${imports[@]}"; do
        case "$dll" in
            advapi32.dll|bcrypt.dll|comctl32.dll|comdlg32.dll|gdi32.dll|imm32.dll|kernel32.dll|msvcrt.dll|ntdll.dll|ole32.dll|oleaut32.dll|shell32.dll|shlwapi.dll|user32.dll|version.dll|winmm.dll|ws2_32.dll)
                ;;
            api-ms-win-*.dll|ext-ms-win-*.dll)
                ;;
            *)
                echo "Unreviewed non-system runtime import in $executable: $dll" >&2
                exit 1
                ;;
        esac
        printf '  %s\n' "$dll"
    done
done
