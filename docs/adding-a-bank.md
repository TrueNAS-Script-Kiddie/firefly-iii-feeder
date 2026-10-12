# Adding a bank

How a new bank (or a new export of a known bank) gets into the feeder, worked out on Argenta
(xlsx accounts + PDF card statements). The code-level checklist is AGENTS.md "Adding a New Bank";
this is the way of working around it. Examples are made up.

## Roles

- **You**: answer the questions below, provide the exports, run every command that writes on the
  server, approve each step ("go on").
- **Coordinator** (the Claude session you talk to): plans, writes the prompt for each step, checks
  each result, explains it to you, commits and pushes after your approval. It runs no step itself.
- **One subagent per step**, in its own git worktree (`isolation: "worktree"`, under
  `.claude/worktrees/`, which the SFTP watcher ignores). It starts cold: everything it needs is in
  its prompt or in files the prompt tells it to read. Follow-ups on the same step go to the same
  subagent (SendMessage): it still knows the code. After an API limit, resume it the same way.

## Phases

1. **Questions** (below), answered in chat with real data.
2. **Survey** (one subagent, reads only): every export, every row, every line kind, counted. Its
   report is the base of the plan; never plan from a sample.
3. **Plan** `plans/NN-<bank>.md`: decisions, steps (one commit each), the load.
4. **Build**, one subagent per step. Usual order: reader for a new file type → shared helpers →
   config + bank package → cross-source checks (`archive_check.py`) → importer changes → docs.
5. **Load** on the server, then the **rebuild proof** (`start-over.bash --wipe-accounts`).

## Questions to answer before the plan

About the exports:

1. Which accounts, cards, deposits, loans? One export per account, or combined?
2. File type and how to get it (portal path, period limit, how far back it goes). Can older
   history still be requested? Ask before the load: it changes opening balances.
3. Every column: name, meaning, format (dates, decimal comma, sign in a separate column or text).
4. What makes a row unique? Is the bank's reference really unique (check: it was not at Argenta)?
5. Pending rows that change later (Fintro's status column)?

About every kind of row (the survey lists them with counts; go through each one):

6. What Firefly makes of it: withdrawal, deposit, or the other side of a transfer.
7. Who the counterparty is, and in what form: IBAN, an old national account number (convert it),
   a placeholder IBAN (Argenta's `BE54 0000 0000 0000`), the own IBAN (interest), a card or
   deposit number, only a name, nothing.
8. Is it the other side of something another export also has (transfers between own accounts,
   card repayment ↔ current-account debit, card ↔ bank refund)? How many days apart are the two
   sides, in the worst case? The importer matches within `TRANSFER_MATCH_DAYS`, or the card window.
9. Mirror cases: a refund, a reversal (chargeback), a transfer back to the bank, a fee belonging to
   the line above, a cash withdrawal, a correction.
10. Foreign currency: amount, rate, which sign; check foreign ÷ rate = EUR amount.
11. Which existing type wording it maps to (consistency across banks matters for filters; check
    the real output of the other banks, not only their code).

About the load:

12. Balance per account today (bank app), to compute opening balances after the load (below).
13. New own accounts for the accounts file: name, kind, IBAN or number, `paid_from` for a card.

## Rules for every step (prompts point here)

1. Write only inside the worktree. No commit, no push: the coordinator does that.
2. The share is read-only; real exports are read in place or copied outside the repo, never into
   it. No SSH writes; a server write goes into the report as a command for the user.
3. The idle run stays free (AGENTS.md "Idle cost").
4. No meaningful information lost (AGENTS.md "Normalization principle"); drop only by a named rule.
5. Public repo: every example made up. Real values only in the report.
6. English only, except the bank's own strings and the Dutch type wording the code emits.
7. Python stdlib + pyyaml; `engine/core/` knows no bank names.
8. Follow the plan; anything that changes a decision, the normalized columns or the output
   contract, or a row kind the plan has no rule for: stop and ask in the report.
9. The same mechanical failure twice: change technique or report.
10. Tests in every step: `pre-commit run --all-files`; the regression test over every archived
    original plus the new bank's files (Fintro and other banks: zero differences); the new bank's
    real files all read, every failure explained; made-up negative cases; for bash changes the
    desktop test of AGENTS.md "Testing".
11. Report: files changed, test results verbatim, one real example per row kind (input →
    normalized row), deviations and open questions each with the real rows involved.

## Coordinator checklist per step

1. Read the diff against the plan; verify the subagent's claims on real data before passing them on.
2. Rerun pre-commit, the regression test and a scan of the added lines for IBAN-like strings,
   long numbers and the real names and values seen in the report.
3. `git diff HEAD --binary` in the worktree → `git apply` in the main checkout, uncommitted (the
   user reviews there; the watcher uploads it).
4. Report to the user in their words: what changed, what the coordinator decided (each "say so if
   you want it otherwise"), questions with a recommendation. A behaviour change is shown as a
   table of what lands in Firefly (date, type, from, to, amount, notes), with a normal case next
   to the special one, using the user's real rows.
5. After the approval: commit, push, remove the worktree and its branch.

## Example prompts

Start of a new session (you paste this):

```text
I want to add <bank> to the feeder. Read AGENTS.md, .claude/rules/architectural_patterns.md,
docs/adding-a-bank.md, docs/output-contract.md and engine/banks/argenta/README.md.
The exports are on the share in bank-csv-originals/<Bank>/. Work as in docs/adding-a-bank.md:
you coordinate, one subagent per step in a worktree, I approve each step ("go on").
Start with the questions, then the survey.
```

Survey (coordinator → subagent):

```text
Survey for adding <bank> (docs/adding-a-bank.md, phase 2). Read only; write nothing in the repo.
Read first: AGENTS.md, docs/adding-a-bank.md (rules for every step), docs/output-contract.md,
engine/banks/argenta/README.md and the Fintro README as models.
Files: //<server>/firefly-iii-feeder/bank-csv-originals/<Bank>/ (all of them).
Report: per file type and layout version (with date ranges and counts): columns and formats;
every distinct transaction kind with its count and one real example; the counterparty forms per
kind; candidates for the unique key and whether they are unique; rows that are one side of
something another export has, with the day gap between the sides; foreign currencies; balances or
totals the files state that a check could use; anything that does not fit.
```

Build step (coordinator → subagent):

```text
You implement step <n> ("<title>") of plans/<NN>-<bank>.md. Only that step.
Read first (fully): AGENTS.md, .claude/rules/architectural_patterns.md, docs/adding-a-bank.md
("Rules for every step" apply), the whole plan, <the code the step touches>, <model bank code>.
Facts established since the plan: <list>. User decisions: <list>.
Stop and ask for: <step-specific list>.
Tests: <step-specific list, on top of the rules>.
Report as in the rules; confirm no commit and nothing written outside the worktree.
```

## Gotchas met on the way

- **Opening balance**: today's balance minus the sum of *all* the account's transactions in
  Firefly after the load, dated before the oldest of them. Another bank's export may hold older
  transfers to the account than its own export goes back.
- **Two sides of a transfer** can lie further apart than `TRANSFER_MATCH_DAYS` (old Argenta
  statements: 10 days); measure it in the survey.
- **A currency Firefly lacks** is created by the importer, which needs the owner's token.
- **SFTP uploads drop the execute bit**: the cron command runs the script through `bash`.
- **`sudo` with a long file list** (a hundred names) can abort: let the target user's shell expand
  the glob, `sudo -u <user> bash -c 'cp <glob> data/incoming/'`.
- **Waiting for a run over SSH**: `pgrep -f <script>` also matches the waiting command itself.
