#!/bin/bash
# validate.sh — structure self-test for the auditing-match-quality skill.
# Checks SKILL.md against the procedure-skill requirements and trigger-eval.json minimums.
# Usage: ./validate.sh [path-to-SKILL.md]   (default: sibling ../SKILL.md)
# Exit 0 = pass, 1 = fail.

set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
FILE="${1:-$HERE/../SKILL.md}"
EVAL="$HERE/../trigger-eval.json"
ERRORS=0
fail() { echo "FAIL: $1"; ERRORS=$((ERRORS + 1)); }

[ -f "$FILE" ] || { echo "FAIL: SKILL.md not found at $FILE"; exit 1; }

# Frontmatter present and closed.
head -1 "$FILE" | grep -q '^---' || fail "SKILL.md must start with YAML frontmatter (---)"
grep -q '^name:' "$FILE" || fail "missing 'name:' in frontmatter"
grep -q '^description:' "$FILE" || fail "missing 'description:' in frontmatter"

# Third-person, scope-bounded description.
grep -qi 'This skill should be used when' "$FILE" || fail "description must use third-person trigger phrasing"
grep -qi 'does NOT cover\|It does NOT' "$FILE" || fail "description must state a scope boundary (NOT ...)"

# Required procedure-skill body sections.
for section in "## Inputs" "## Steps" "## Output" "## Edge Cases" "## Gotchas"; do
  grep -qF "$section" "$FILE" || fail "missing body section: $section"
done

# trigger-eval.json: >= 4 cases, with both should_trigger true and false present.
if [ -f "$EVAL" ]; then
  cases=$(grep -c 'should_trigger' "$EVAL")
  [ "$cases" -ge 4 ] || fail "trigger-eval.json needs >= 4 cases (found $cases)"
  grep -q '"should_trigger": *true' "$EVAL" || fail "trigger-eval.json needs a should_trigger:true case"
  grep -q '"should_trigger": *false' "$EVAL" || fail "trigger-eval.json needs a should_trigger:false case"
else
  fail "trigger-eval.json not found"
fi

if [ "$ERRORS" -eq 0 ]; then
  echo "PASS: $(basename "$FILE") meets the skill-authoring structure requirements"
  exit 0
fi
echo "$ERRORS error(s)."
exit 1
