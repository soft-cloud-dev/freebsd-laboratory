#!/bin/sh
set -eu

IMAGES_DIR=${1:-/var/db/freebsd-laboratory/images}

if [ ! -d "$IMAGES_DIR" ]; then
    echo "Images directory does not exist: $IMAGES_DIR" >&2
    exit 0
fi

cd "$IMAGES_DIR"

ACTIVE_FILES=""

for link in *.raw *.efi; do
    [ -e "$link" ] || continue
    if [ -L "$link" ]; then
        target=$(readlink "$link")
        ACTIVE_FILES="${ACTIVE_FILES} ${link} ${target}"
        base="${target%.raw}"
        base="${base%.efi}"
        for ext in .sha256 .manifest; do
            if [ -f "${target}${ext}" ]; then
                ACTIVE_FILES="${ACTIVE_FILES} ${target}${ext}"
            elif [ -f "${base}${ext}" ]; then
                ACTIVE_FILES="${ACTIVE_FILES} ${base}${ext}"
            fi
        done
    fi
done

REMOVED_COUNT=0
REMOVED_BYTES=0

for file in *.raw *.sha256 *.manifest; do
    [ -e "$file" ] || continue
    [ -L "$file" ] && continue

    is_active=0
    for active in $ACTIVE_FILES; do
        if [ "$file" = "$active" ]; then
            is_active=1
            break
        fi
    done

    if [ "$is_active" -eq 0 ]; then
        size=$(stat -f %z "$file" 2>/dev/null || echo 0)
        rm -f "$file"
        REMOVED_COUNT=$((REMOVED_COUNT + 1))
        REMOVED_BYTES=$((REMOVED_BYTES + size))
        echo "Removed obsolete image artifact: $file"
    fi
done

if [ "$REMOVED_COUNT" -gt 0 ]; then
    echo "Garbage collection complete: removed $REMOVED_COUNT file(s) in $IMAGES_DIR."
else
    echo "No obsolete images found in $IMAGES_DIR."
fi
