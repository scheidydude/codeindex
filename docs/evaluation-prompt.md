# blastradius Evaluation Prompt

Copy and paste the following prompt into Claude Code from inside the repo you want to evaluate.
Requires blastradius v0.3.6+ installed and `blastradius analyze` already run.

---

```
You are evaluating blastradius against this repo. Your job is to run 5 realistic tasks two ways each — once with standard shell tools (grep, git, find, cat) and once with blastradius — then produce a structured comparison report. Make no changes to this repo or the blastradius repo.

## Setup check

First, verify blastradius is available and the DB exists:

```bash
blastradius db status
```

If the DB is missing, run `blastradius analyze .` first, then continue.

## The 5 tasks

Run each task both ways. Time both approaches (use `time`). Record what you actually ran, what output you got, and how many files you had to open to get a complete answer.

---

### Task 1 — Locate an entry point

Pick the most important user-facing concept in this codebase (auth, payment, scheduling, API handler, etc. — infer it from the file structure). Answer: "Where does this process begin? What function is the entry point?"

**Without blastradius:**
```bash
grep -r "<concept>" --include="*.py" --include="*.ts" --include="*.go" -l | grep -v test | grep -v __pycache__ | head -20
# then open the most likely file and read it
```

**With blastradius:**
```bash
blastradius search "<concept>"
blastradius lookup <ClassName or function_name you spotted>
```

Note: `lookup` prints the definition line plus 4 lines of source context. If a symbol isn't found, it's likely a third-party import (only repo-defined symbols are indexed).

---

### Task 2 — Blast radius before a risky change

Pick the file that looks most central (lots of imports, core module name, etc.). Answer: "How many files would be affected if this changed? Is it safe to touch?"

**Without blastradius:**
```bash
grep -r "from <module>\|import <module>" --include="*.py" --include="*.ts" --include="*.go" -l | grep -v test | grep -v __pycache__
# count the results and note: this is only direct importers
```

**With blastradius:**
```bash
blastradius impact <file>
```

---

### Task 3 — Understand a module's neighborhood

Pick a mid-level module (not the God object, not a leaf utility). Answer: "What does this depend on, and what depends on it?"

**Without blastradius:**
```bash
grep -n "^import\|^from" <file> | head -20   # what it imports
grep -r "from.*<module>\|import.*<module>" --include="*.py" -l | grep -v test  # who imports it
```

**With blastradius:**
```bash
blastradius dependencies <file>
```

---

### Task 4 — Identify the riskiest files before a refactor

Answer: "If I had 30 minutes to read code before a major refactor, which files must I understand first?"

**Without blastradius:**
No direct equivalent. Describe what you would do manually (read entry points, follow imports, build a mental model).

**With blastradius:**
```bash
blastradius high-blast --threshold <pick a threshold based on repo size>
```

---

### Task 5 — Structural drift since a recent commit

Pick a commit from 1–3 weeks ago (or a release tag if one exists).
Answer: "What new dependencies were introduced? What was removed?"

**Without blastradius:**
```bash
git log --oneline -10          # pick a ref
git diff --name-only <ref>..HEAD
```

**With blastradius:**
```bash
git log --oneline -10          # same ref
blastradius changed-since <ref>
```

Note: if you see "Warning: Git history has not been backfilled", run `blastradius history .` first, then repeat the command.

---

### Task 6 — Per-export blast radius

Pick a high-blast file that exports multiple symbols (a schema, a utility module, a shared config). Answer: "Which specific exports are actually used, and by which files? Is it safe to change just one of them?"

**Without blastradius:**
```bash
# For each exported name, grep importers separately:
grep -r "ExportedName" --include="*.ts" --include="*.py" -l
# repeat for each export — tedious for files with 5+ exports
```

**With blastradius:**
```bash
blastradius symbol-blast <file>
```

This lists every exported symbol with a count and the exact importer files that reference it by name — so you can see that changing `userSchema` affects 8 routes while `legacySchema` affects only 1.

---

## Scoring rubric

After running all 6 tasks, fill in this table:

| Task | Without: steps to answer | Without: files opened | With: steps to answer | With: files opened | Winner |
|------|--------------------------|-----------------------|-----------------------|--------------------|--------|
| 1. Entry point | | | | | |
| 2. Blast radius | | | | | |
| 3. Neighborhood | | | | | |
| 4. Riskiest files | | | | | |
| 5. Structural drift | | | | | |
| 6. Per-export blast | | | | | |

Then answer:
- Which blastradius result surprised you most? (Something you wouldn't have found with grep alone?)
- Did any blastradius command return wrong or misleading results?
- What query or task did you try that blastradius couldn't answer?

## Report format

Write the final report as:

**Repo:** <name and rough size>
**Languages:** <what the analyzer detected>

**Task results:** <the filled-in table above>

**Most surprising finding:** <one concrete example>

**Rough edges found:** <anything that returned wrong results, unhelpful output, or no output>

**Verdict:** one sentence — would you add blastradius to your workflow for this repo?
```
