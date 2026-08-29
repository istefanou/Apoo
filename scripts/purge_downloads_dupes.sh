#!/usr/bin/env bash
# Delete the 110 files in /mnt/usbdisk/Downloads that were verified byte-identical
# (size + head/tail MD5) to a copy already present in /mnt/usbdisk/Movies or /Series.
#
# The unique keeper (The Amazing Spider-Man 2 2014, 2160p) is NOT in this list.
# The fake .exe files, the RARBG marker, and the stale Fallout .inProgress temp
# were already removed separately.
#
# Usage:
#   scripts/purge_downloads_dupes.sh          # dry run - lists what would be deleted
#   scripts/purge_downloads_dupes.sh --go     # actually delete

set -u
LIST="$(dirname "$0")/downloads_dupes.list"
GO=0
[ "${1:-}" = "--go" ] && GO=1

freed=0
missing=0
count=0
while IFS= read -r rel; do
  [ -z "$rel" ] && continue
  f="/mnt/usbdisk/$rel"
  if [ ! -f "$f" ]; then
    echo "skip (gone): $rel"
    missing=$((missing+1))
    continue
  fi
  sz=$(stat -c%s "$f")
  count=$((count+1))
  freed=$((freed+sz))
  if [ "$GO" = "1" ]; then
    rm -v -- "$f"
  else
    printf '%10d  %s\n' "$sz" "$rel"
  fi
done < "$LIST"

printf '\n%s %d files, %.1f GiB%s\n' \
  "$([ "$GO" = 1 ] && echo 'Deleted:' || echo 'Would delete:')" \
  "$count" "$(echo "scale=1; $freed/1073741824" | bc)" \
  "$([ "$missing" -gt 0 ] && echo " ($missing already gone)")"
