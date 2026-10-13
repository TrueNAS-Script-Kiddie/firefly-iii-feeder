# Plan 03 — Firefly as code: categories, tags, rules and hand-made data from the data repo

Status: **phase 1 done; phase 2 (proof of concept) next.** Examples are made up.

Goal: Firefly is built entirely from the private data repo by this feeder, one way. Next to the
exports and the own accounts, that repo gets the categories, tags, rules and hand-made transaction
data; private tools (not in this repo) write those files, commit and push them, and then start the
feeder. The feeder never writes back into the data repo's files, apart from archiving exports.

Hard constraint in every step: **the idle run stays free** (AGENTS.md "Idle cost").

## Order

Try first, then build: what the feeder must load depends on what the proof of concept shows, so the
commands and the infrastructure come after it.

| Phase | What | Where |
|---|---|---|
| 1 | ✅ Snapshot of Firefly's draft rule groups, rules, categories and tags (API JSON), versioned | private finance repo |
| 2 | Proof of concept: rules and a first visualisation on the current Firefly | private finance repo |
| 3 | Decide the classification engine (open point 1), then the file formats | this plan |
| 4 | Commands: `feed`, `wipe`, `rebuild`, `export` | this repo |
| 5 | Writing from the desktop, own user, SSH for the private tools | this repo and the server |

## Decisions

1. ✅ **One way.** Firefly is derived from the data repo; nothing flows back. An export from
   Firefly exists to get started (a new user, or one's draft config made in the GUI): it writes
   into a folder of one's choice and refuses when the files exist already. Optional, for users who
   do edit in the GUI: `export --update` (phase 4, last step).
2. ✅ **One entry point**, `firefly-iii-feeder.bash`, with subcommands; options adjust them.
   `start-over.bash` goes.

   ```
   firefly-iii-feeder.bash                          cron: process data/incoming/ (as now)
   firefly-iii-feeder.bash feed <kind>... [--dry-run]
   firefly-iii-feeder.bash wipe <kind>... [--dry-run]
   firefly-iii-feeder.bash rebuild <kind>... [--dry-run]     wipe, then feed
   firefly-iii-feeder.bash export <folder> [<kind>...] [--update]
   ```

   Bash keeps the idle path, the lock and git; the logic is Python (the Firefly client exists
   there, so `curl` is no longer needed). `wipe`, not `remove` or `clear`: it removes everything of
   a kind, and `clear categories` could also mean "take them off the transactions". `rebuild
   transactions` is today's `start-over.bash`.
3. ✅ **Kinds.** Expense and revenue accounts are no kind: Firefly makes them from the
   transactions, so they are output, not input. How `rules` and `classification-overrides` are
   applied depends on open point 1.

   | Kind | File | `feed` | `wipe` |
   |---|---|---|---|
   | `accounts` | `accounts.yaml` | file → Firefly (today's `--accounts-only`) | own accounts **and every transaction** (a transaction cannot exist without its accounts) |
   | `categories` | `categories.yaml` | file → Firefly | only the categories (they leave the transactions) |
   | `tags` | `tags.yaml` | file → Firefly | only the tags |
   | `rules` | `rules.yaml` | open point 1 | open point 1 |
   | `classification-overrides` | `classification-overrides.csv` | per bank row: category, tags, splits | — |
   | `transactions-without-export` | `transactions-without-export.csv` | bookings no export contains | only those bookings |
   | `transactions` | the exports | queue every archived export in `data/incoming/`; cron imports them | transactions, expense and revenue accounts, purge, and `data/` moved aside |
   | `all` | all of the above | in dependency order | all of the above |

   Wiping transactions always moves `data/` aside: a row state that names deleted Firefly ids
   would never import those rows again.
4. ✅ **`feed` adds and updates, never deletes.** Only `wipe` deletes. What differs from the file
   is overwritten and logged; what is in Firefly but not in the file stays, with an alert in plain
   words, one text for every kind, e.g. `FIREFLY HAS OWN ACCOUNTS THAT accounts.yaml DOES NOT
   LIST: <name> (<iban>). Add them to the file, or delete them in Firefly.` (replacing today's
   `FIREFLY OWN ACCOUNTS NOT IN THE ACCOUNTS FILE`). No merge, no sync back.
5. ✅ **Own key per entry**: every category and tag (and rule group and rule, if Firefly's rules
   are used) in the files has a `key`, a number its writer assigns once and never changes or
   reuses; the name is free to change. Firefly's own id cannot be that key: Firefly chooses it, and
   a rebuild or another server gives new ones. The feeder links the two by writing the key on a
   line of the Firefly object's notes or description field (`feeder-key: 12`), so Firefly itself
   says which entry an object is, also after `data/` moved aside. A feed matches by that line; an
   object without one (made in the GUI, or before this plan) by name, and gets the line. A rename
   is then just a new name in the file: `PUT` keeps the transactions attached.
   Rules name categories and tags by name (readable); the files are checked as a whole before
   anything is sent, so a rule naming a category or tag the files do not have makes the feed
   refuse, as an invalid accounts file does. `feed all` feeds categories and tags before rules.
6. ✅ **One file per kind**, at the root of the data repo next to `accounts.yaml`.
7. ✅ **`feed transactions` only queues**: a full reload takes about an hour, and cron survives a
   dropped SSH session.
8. ✅ **Who commits.** Whoever changes a file commits it, with a message saying why: the private
   tools commit and push from a desktop clone of the data repo. The feeder commits only what it
   archives itself (`originals/`, `unprocessed/`). `feed` first pulls, so Firefly always matches a
   pushed commit, and the log names that commit. Until phase 5, files change through the share as
   `accounts.yaml` does today, and cron commits them.
9. ✅ **The private tools start the feeder over SSH**, with a key of their own whose
   `authorized_keys` line forces a wrapper (`command="…"`) that allows only whitelisted
   subcommands. Output and exit code come straight back, so a tool sees its `--dry-run`.
10. ✅ **Own user for the feeder** instead of the user that also serves the SMB shares and the
    SFTP uploads: no share groups, so a bug or a leaked key reaches only the feeder. The SFTP
    watcher keeps uploading the code as the share user; the feeder only reads its code, and owns
    only `data/` and `archive/`.
11. ✅ **Share and feeder user through a shared group.** Both users in one group; `data/incoming/`
    and `archive/` (with its subfolders) owned by that group, group-writable and setgid, so what
    either user creates there gets the group. The feeder sets `umask 002`, so its files stay
    group-writable (a hand fix through the share edits a file the feeder archived). Checked with
    `ls -l`; POSIX ACLs (the dataset has `acltype posix`) only if this proves too weak.
12. ✅ **Config at every import run.** A run with work feeds `accounts`, `categories`, `tags`,
    `rules` and `transactions-without-export` before importing (as it applies the accounts file
    today), and the classification overrides after, so new rows meet the current classification.
    `feed` by hand is for "now" and for applying a changed classification to the history. From
    phase 5 on, the run pulls first (open point 2).
13. ✅ **Hand-made transaction data**, two files (fixes to the bank data itself stay hand fixes in
    the archive, AGENTS.md "Rebuild from scratch"):
    - `transactions-without-export.csv`: bookings no export contains, each with a number key
      (decision 5), date, own account, amount, counterparty, description, category and tags. They
      replace the `corrections` in `accounts.yaml`; a changed one is updated, not replaced.
    - `classification-overrides.csv`: one line per bank row (`row_key`) whose category or tags no
      rule fully decides; several lines with the same `row_key` split it, each with its amount,
      category and tags, together exactly the bank amount (else the feed refuses). Category:
      empty = as the rules give it, else replaced. Tags: empty = as the rules give them; `a,b` =
      exactly these; `+a,-b` = the rules' tags with `a` added and `b` removed (every tag prefixed,
      or none). A bank row is found through the row state, which holds the Firefly id of both
      sides of a transfer.
14. ✅ **An override wins over a rule**, and the feed lists every override line that contradicts a
    rule: many of those for one rule say the rule is wrong.

## Open

1. **One classification engine or two.** The biggest open choice; phase 2 decides it.
   - *Firefly's rules, with the overrides on top*: `rules.yaml` holds Firefly's rule groups and
     rules (triggers and actions by their API names), fed into Firefly. Firefly applies them on
     import; a changed rule needs `POST /rule-groups/{id}/trigger` over the whole history
     (~14,500 transactions, time unmeasured), and every override must be applied again after it.
     Firefly also runs rules on every update by default (`apply_rules` defaults to true on
     `PUT /transactions`), so the feeder's own updates must send `apply_rules: false`. Two engines,
     an order to keep, and slow rounds when rules change often.
   - *The feeder classifies*: `rules.yaml` in a small format of our own (matching on counterparty,
     IBAN, description, amount, account), evaluated in Python on the normalized rows together with
     the overrides. The result goes along with the POST; a change is a `PUT` of only the
     transactions whose category or tags differ. One engine, no order, a dry run without Firefly,
     testable with `engine.regression`, and usable if Firefly is ever replaced. Firefly's rules are
     not used (its GUI rule editor is the main thing lost, and editing in the GUI is not wanted).
2. **A pull that fails** (Forgejo unreachable, phase 5): it must not stop the import. Go on with the
   local checkout, alert once (as the push flag does). Today only the push needs the network.
3. **A broken classification file blocks imports** (decision 12): right for `accounts.yaml`, whose
   errors book rows wrongly. For classification, a row imported without a category is harmless and
   a row not imported is not: refuse only the classification, import the rows, alert?
4. **Splits against the importer's matching**: `reverse_payment` and `claim_transfer` search by
   amount. A payment split into −60 and −20 may no longer be found by a later reversal of +80.
   Check how Firefly's search treats split journals before splits are built.
5. **The desktop clone of the data repo holds every bank export**: a sparse checkout of the
   config files only.
6. **`wipe` on the SSH whitelist**: not at first; one line in the wrapper once wanted.
7. **The `feeder-key` line is visible and editable in Firefly's GUI**: deleted by hand, the next
   feed matches by name and writes it again; renamed and deleted in one go, it creates a second
   object. Acceptable while nobody edits in the GUI.
8. **The feeder grows** from an importer to a manager of Firefly's configuration: more code in a
   public repo to maintain, and (with Firefly's rules) a file format tied to Firefly's API, which
   changes between versions (6.7's rule and mass-edit changes).

## Phase 1 — Snapshot (done 2026-10-14)

Firefly's rule groups, rules, categories and tags as the API returned them, in the private finance
repo: the draft config is versioned before anyone experiments with it. The feeder's own `export`
(phase 4) replaces this.

## Phase 2 — Proof of concept (private tools, not this repo)

A first set of rules on the current Firefly, and a first try at visualising the result. Nothing in
Firefly needs keeping yet: a rebuild resets it. It answers open point 1 and which reporting tool
fits.

## Phase 3 — Decide

The engine (open point 1); then the formats of `rules.yaml` and both CSV files, with
`config/*.example.*` templates; then decisions 3, 5 and 12 get their final text.

## Phase 4 — Commands

1. **Dispatch**: subcommands in `firefly-iii-feeder.bash` after the idle exit (no arguments = cron,
   unchanged); every subcommand checks it runs as the folder's owner (as `start-over.bash` does),
   waits for the lock, then calls the Python command module. Re-measure the idle run.
2. **`wipe accounts | transactions | all`**: ported from `start-over.bash` (repeat `destroy` on
   504, purge, `data/` aside, `WIPE` confirmation); `--dry-run` says what would go.
3. **`feed accounts | transactions | all`**: today's accounts step; the queueing half of
   `start-over.bash`. `--accounts-only` goes. **`rebuild`** = wipe, then feed. The alert of
   decision 4 gets its new text.
4. **`export <folder> accounts`**: Firefly's own accounts → `accounts.yaml` in the format of
   [config/accounts.example.yaml](../config/accounts.example.yaml) (without corrections and
   `paid_from`, which Firefly does not hold). Then delete `start-over.bash`.
5. **Categories and tags**: `export`, `feed` (check the files as a whole, create, update, rename by
   key, drift alert), `wipe`.
6. **Rules and classification overrides**, as phase 3 decided (open points 3 and 4 settled first).
7. **`transactions-without-export`**: create and update by key; the `corrections` move out of
   `accounts.yaml`.
8. **Rebuild proof** (you): balances and counts first, `rebuild all`, compare; then `feed all
   --dry-run` gives "nothing to change".
9. **`export --update`** (optional, for other users; not used here): the existing files are read,
   entries missing from them added, entries Firefly also has updated, entries only in the files
   left alone. The files are written in the feeder's fixed layout, so comments in them are lost
   (PyYAML does not keep them); say so in the command's help.

## Phase 5 — Writing from the desktop, own user, SSH

1. **Own user** (you, on the server; the commands come with the step): create the user in the
   GUI (home on a data pool, shell bash, no share groups), then move to it: ownership of `data/`,
   `archive/`, `config/app.env`; the group of decision 11; the cron job; the sudo rule of the root
   helper; the data repo's deploy key and ssh config into its `.ssh/`.
2. **Desktop clone of the data repo** (sparse, open point 5); the config files are changed there
   and pushed, no longer through the share.
3. **Cron git on its own paths**: `git add originals unprocessed` instead of `git add -A`; a run
   with work starts with `git pull --rebase` (open point 2) and pulls again before a push that was
   refused. A hand fix to an export through the share stays committed by cron (it is in
   `originals/`). `feed` pulls first.
4. **SSH wrapper**: source in `deploy/`, installed by hand per server like the root helper; the
   key's `authorized_keys` line forces it; whitelist `feed` and `export`. The user field in the
   TrueNAS GUI ("Public SSH Key") is that same file, so the line can be set there.
5. Docs: AGENTS.md "Server access", "Running" and "Setup", docs/archive-and-reload.md (rules 7 and
   10, "Setup", "Restore on a new server").
