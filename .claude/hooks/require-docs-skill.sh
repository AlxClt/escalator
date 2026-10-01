#!/bin/bash
cmd=$(jq -r '.tool_input.command // empty')
if grep -Eq '(^|[;&|[:space:]])git[[:space:]]+commit([[:space:]]|$)' <<<"$cmd" \
   && ! grep -q 'Docs-Checked' <<<"$cmd"; then
  echo "Commit blocked: commit through the update-docs-and-commit skill." >&2
  exit 2
fi
exit 0