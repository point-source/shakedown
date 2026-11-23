#!/bin/sh
# Governance lint for REQUIREMENTS.md, SPEC.md and ROADMAP.md.
#
#   tools/governance-lint.sh
#
# Checks, in order:
#   1. markdownlint-cli2 over REQUIREMENTS.md, SPEC.md and README.md (outside
#      node_modules and dot-directories such as .venv), when it is
#      on PATH and .markdownlint.json exists (CI installs it; nothing is
#      installed here).
#   2. Every "## " section of a SPEC.md is followed by a status line,
#      exactly "*Status: not started*", "*Status: in progress*" or
#      "*Status: complete*".
#   3. Every "## " heading ends with its file's slug (§req: in REQUIREMENTS.md,
#      §spec: in SPEC.md, §road: in ROADMAP.md); deeper headings may carry one.
#      No ordinal headings ("## 8. Foo"); no duplicate slugs.
#   4. Every §req:/§spec:/§road: reference resolves to exactly one heading;
#      no numeric references such as §8.9. Code spans and fenced blocks are
#      skipped.
#   5. Vale, when .vale.ini exists and vale is on PATH.
#
# Prints one "file:line: problem" per violation and exits 1 if there are any.

set -u

cd "$(dirname "$0")/.." || exit 2

tmp=$(mktemp -d) || exit 2
trap 'rm -rf "$tmp"' EXIT
trap 'exit 2' HUP INT TERM

problems=0

# --- 1. markdownlint ---------------------------------------------------------

if [ -f .markdownlint.json ]; then
  if command -v markdownlint-cli2 >/dev/null 2>&1; then
    markdownlint-cli2 "**/REQUIREMENTS.md" "**/SPEC.md" "**/README.md" \
      "#**/node_modules" "#.*/**" || problems=$((problems + 1))
  else
    echo "note: markdownlint-cli2 not found; skipping markdown lint (CI runs it)"
  fi
fi

# --- 2-4. status lines and slugs ---------------------------------------------

find . \( -name '.?*' -o -name node_modules \) -prune -o \
  -type f \( -name REQUIREMENTS.md -o -name SPEC.md -o -name ROADMAP.md \) \
  -print | sed 's|^\./||' | sort >"$tmp/docs"

if [ -s "$tmp/docs" ]; then
  # Emits tab-separated records:
  #   2 <tab> message          status-line problem
  #   3 <tab> message          heading/slug problem
  #   4 <tab> message          numeric reference
  #   D <tab> key <tab> loc    slug definition
  #   R <tab> key <tab> loc    slug reference
  set --
  while IFS= read -r doc; do
    set -- "$@" "$doc"
  done <"$tmp/docs"
  awk '
    function finish_section() {
      if (pending) {
        printf "2\t%s:%d: \"%s\" needs a status line next: *Status: not started|in progress|complete*\n", file, hline, htext
      }
      pending = 0
    }
    FNR == 1 {
      finish_section()
      file = FILENAME
      fence = ""
      n = split(FILENAME, parts, "/")
      base = parts[n]
      if (base == "REQUIREMENTS.md") prefix = "req"
      else if (base == "SPEC.md") prefix = "spec"
      else prefix = "road"
    }
    {
      line = $0
      sub(/\r$/, "", line)
    }
    fence != "" {
      if (index(line, fence) && line ~ /^ *(```|~~~)/) fence = ""
      next
    }
    line ~ /^ *```/ { fence = "```"; next }
    line ~ /^ *~~~/ { fence = "~~~"; next }
    pending && line !~ /^[ \t]*$/ {
      if (line !~ /^\*Status: (not started|in progress|complete)\*$/) {
        printf "2\t%s:%d: \"%s\" needs a status line next: *Status: not started|in progress|complete*; found line %d: %s\n", file, hline, htext, FNR, line
      }
      pending = 0
    }
    {
      scan = line
    }
    line ~ /^#+ / {
      level = index(line, " ") - 1
      text = substr(line, level + 2)
      if (text ~ /^[0-9]+\./) {
        printf "3\t%s:%d: ordinal heading; drop the number: %s\n", file, FNR, line
      }
      if (match(text, /§[a-z]+:[A-Za-z0-9-]*[ \t]*$/)) {
        tok = substr(text, RSTART, RLENGTH)
        sub(/[ \t]+$/, "", tok)
        sub(/^§/, "", tok)
        split(tok, kv, ":")
        if (kv[1] != prefix) {
          printf "3\t%s:%d: heading slug must use §%s: in %s: %s\n", file, FNR, prefix, base, line
        } else if (kv[2] !~ /^[a-z0-9]+(-[a-z0-9]+)*$/) {
          printf "3\t%s:%d: malformed slug (lowercase words joined by -): %s\n", file, FNR, line
        } else {
          printf "D\t§%s\t%s:%d\n", tok, file, FNR
        }
        scan = substr(line, 1, level + 1 + RSTART - 1)
      } else if (level == 2) {
        printf "3\t%s:%d: \"## \" heading must end with its §%s: slug: %s\n", file, FNR, prefix, line
      }
      if (level == 2 && prefix == "spec") {
        pending = 1
        hline = FNR
        htext = line
      }
    }
    {
      gsub(/`[^`]*`/, "", scan)
      rest = scan
      while (match(rest, /§[0-9]/)) {
        printf "4\t%s:%d: numeric reference; use a §req:/§spec:/§road: slug: %s\n", file, FNR, line
        rest = substr(rest, RSTART + RLENGTH)
      }
      rest = scan
      while (match(rest, /§(req|spec|road):[A-Za-z0-9-]*/)) {
        printf "R\t%s\t%s:%d\n", substr(rest, RSTART, RLENGTH), file, FNR
        rest = substr(rest, RSTART + RLENGTH)
      }
    }
    END { finish_section() }
  ' "$@" >"$tmp/scan"

  # Check 2, then 3 (with duplicates), then 4 (with numeric references).
  grep '^2	' "$tmp/scan" | cut -f2- >"$tmp/report"
  grep '^3	' "$tmp/scan" | cut -f2- >>"$tmp/report"
  grep '^D	' "$tmp/scan" | cut -f2- >"$tmp/defs"
  awk -F '	' '
    { n[$1]++; locs[$1] = locs[$1] " " $2; at[NR] = $2; key[NR] = $1 }
    END {
      for (i = 1; i <= NR; i++)
        if (n[key[i]] > 1)
          printf "%s: duplicate slug %s (defined at%s)\n", at[i], key[i], locs[key[i]]
    }
  ' "$tmp/defs" >>"$tmp/report"
  grep '^R	' "$tmp/scan" | cut -f2- >"$tmp/refs"
  { grep '^4	' "$tmp/scan" | cut -f2-
    awk -F '	' '
      NR == FNR { n[$1]++; next }
      !($1 in n) { printf "%s: dangling reference %s: no heading defines it\n", $2, $1; next }
      n[$1] > 1 { printf "%s: ambiguous reference %s: %d headings define it\n", $2, $1, n[$1] }
    ' "$tmp/defs" "$tmp/refs"
  } | sort -t: -k1,1 -k2,2n >>"$tmp/report"

  cat "$tmp/report"
  problems=$((problems + $(wc -l <"$tmp/report")))
fi

# --- 5. Vale -----------------------------------------------------------------

if [ -f .vale.ini ]; then
  if command -v vale >/dev/null 2>&1; then
    set --
    for doc in REQUIREMENTS.md SPEC.md; do
      [ -f "$doc" ] && set -- "$@" "$doc"
    done
    if [ "$#" -gt 0 ]; then
      vale --minAlertLevel=warning "$@" || problems=$((problems + 1))
    fi
  else
    echo "note: vale not found; skipping prose lint"
  fi
fi

if [ "$problems" -gt 0 ]; then
  echo "governance-lint: $problems problem(s)"
  exit 1
fi
echo "governance-lint: ok"
