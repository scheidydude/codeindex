"""Install / uninstall a pre-commit hook that reports blast radius for staged files."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

HOOK_MARKER = "# blastradius-hook"

HOOK_TEMPLATE = """\
#!/usr/bin/env bash
{marker}
# Installed by blastradius. Remove this file or run `blastradius install-hook --remove` to disable.

THRESHOLD={threshold}
STRICT={strict}

# Find blastradius.json by walking up from repo root
INDEX=$(git rev-parse --show-toplevel)/blastradius.json

if [ ! -f "$INDEX" ]; then
  echo "[blastradius] No blastradius.json found — skipping impact check. Run: blastradius analyze ."
  exit 0
fi

# Get staged files (added or modified)
STAGED=$(git diff --cached --name-only --diff-filter=AM)

if [ -z "$STAGED" ]; then
  exit 0
fi

WARNED=0
while IFS= read -r FILE; do
  if [ -z "$FILE" ]; then continue; fi
  OUTPUT=$(blastradius impact "$FILE" --index "$INDEX" 2>/dev/null)
  if [ $? -ne 0 ]; then continue; fi
  SCORE=$(echo "$OUTPUT" | grep -oP 'Blast Score: \\K[0-9.]+')
  if [ -z "$SCORE" ]; then continue; fi
  SCORE_INT=$(echo "$SCORE" | cut -d. -f1)
  if [ "$SCORE_INT" -ge "$THRESHOLD" ] 2>/dev/null; then
    echo ""
    echo "[blastradius] HIGH BLAST RADIUS: $FILE (score: $SCORE)"
    echo "$OUTPUT"
    echo ""
    WARNED=1
  fi
done <<< "$STAGED"

if [ "$WARNED" -eq 1 ] && [ "$STRICT" = "1" ]; then
  echo "[blastradius] Commit blocked (--strict mode). Review impact above."
  exit 1
fi

exit 0
"""


def install(
    repo_path: str, threshold: int = 10, strict: bool = False, remove: bool = False
) -> None:
    root = Path(repo_path).resolve()
    git_dir = root / ".git"
    if not git_dir.exists():
        print(f"No .git directory found in {root}", file=sys.stderr)
        sys.exit(1)

    hook_path = git_dir / "hooks" / "pre-commit"

    if remove:
        if hook_path.exists():
            content = hook_path.read_text()
            if HOOK_MARKER in content:
                hook_path.unlink()
                print(f"Removed blastradius pre-commit hook from {hook_path}")
            else:
                print(
                    "Pre-commit hook exists but was not installed by blastradius — not removing."
                )
        else:
            print("No pre-commit hook found.")
        return

    if hook_path.exists():
        content = hook_path.read_text()
        if HOOK_MARKER not in content:
            print(
                f"A pre-commit hook already exists at {hook_path} and was not installed by blastradius.\n"
                "Add the following manually or use a hook manager (husky, pre-commit, lefthook).",
                file=sys.stderr,
            )
            sys.exit(1)

    script = HOOK_TEMPLATE.format(
        marker=HOOK_MARKER,
        threshold=threshold,
        strict="1" if strict else "0",
    )

    hook_path.parent.mkdir(exist_ok=True)
    hook_path.write_text(script)
    hook_path.chmod(
        hook_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )

    mode = "strict (blocks commit)" if strict else "warn-only"
    print(
        f"Installed blastradius pre-commit hook → {hook_path}\n"
        f"  threshold: {threshold}  mode: {mode}\n"
        "Run `blastradius install-hook --remove` to uninstall."
    )
