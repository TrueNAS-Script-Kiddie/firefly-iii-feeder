# Plan 03 — Archive and starting over

Status: **draft, for discussion.** Examples are made up (public repo).

Goal: you drop an export in `data/incoming/` and need to do nothing else. Everything needed to
fill Firefly again from zero comes under version control in private Forgejo by itself, and
starting over is one command.

## 1. Requirement (becomes a hard rule in AGENTS.md)

> Firefly is derived from the input. Wiping it and loading everything again must always be
> possible, with one command, and gives the result of the current code. After every structural
> change to the feeder or to Firefly that is the normal way to give old transactions the new
> shape.

1. All input is in the archive repo; `data/` and Firefly contain nothing that cannot be remade.
2. Fix nothing by hand in `data/` or Firefly: a fix goes into the parser, config or accounts
   file. Classification: the finance tool, which must also be able to reapply it.
3. Plans and reviews do not warn "then you have to wipe Firefly": that is the intention.

## 2. What stays, what is disposable

```mermaid
flowchart LR
    subgraph BLIJFT["archive/ — git, pushed to private Forgejo"]
        OR["originals/#lt;bank#gt;/#lt;account#gt;/<br/>every recognized export, byte for byte"]
        AC["accounts.yaml<br/>own accounts"]
    end
    subgraph WEG["data/ — disposable, can be remade"]
        IN[incoming/] --> NO[normalized/] --> IM[imported/]
        IX[duplicate-index/]
        FA[failed/]
        LO[logs/]
    end
    IN -->|recognized original| OR
    IN -->|not recognized| FA
    NO --> FF[(Firefly)]
    AC -->|applied before every import| FF
```

`processed/` disappears: a recognized original goes to the archive, an unrecognized one to
`failed/`.

## 3. One file: where the original goes

```mermaid
flowchart TD
    F[file in incoming/] --> R{bank and account<br/>recognized?}
    R -->|no: unknown bank, empty,<br/>all rows invalid, several accounts| X["failed/#lt;run#gt;-rejected-#lt;name#gt;<br/>+ alert<br/>NOT in the archive"]
    R -->|yes| H{byte-identical file<br/>already in the archive?}
    H -->|yes| D[gone: literally the same export]
    H -->|no| A["archive/originals/#lt;bank#gt;/#lt;account#gt;/<br/>#lt;run#gt;-#lt;first#gt;_#lt;last#gt;-#lt;sha8#gt;.#lt;ext#gt;"]
    R -->|yes| RIJ[every row: see §4]
```

- What happens to the rows does not matter for the archive: an export with only duplicates, or
  of which all rows fail in the parser, is real bank data and goes in.
- `<run>` at the front = order of arrival; on a reload the files go back in that order (§7).
  `<sha8>` = start of the SHA-256 of the content; "byte-identical" is tested on it.
- A rejected file may be real bank data (the bank changed its export format). After the fix you
  put it back in `incoming/`; until then `start-over.bash` (§7) refuses, so it does not vanish
  with `data/`.

## 4. One row in the normalizer

Unchanged compared to now.

```mermaid
flowchart TD
    R[row] --> FI{filter<br/>e.g. status not Accepted}
    FI -->|filtered out| G[counted in the log, nothing else]
    FI --> V{cells valid?}
    V -->|no| NF1["failed/…-normalize-failed.csv<br/>not in the index"]
    V -->|yes| K{duplicate key<br/>in the account's index?}
    K -->|no: new| N{normalize_row succeeds?}
    K -->|yes, all fields equal| ID[identical: skipped]
    K -->|yes, fields differ| CO["conflict: failed/…-duplicate-failed.csv<br/>Firefly keeps the first version"]
    N -->|yes| OK["normalized/ + index"]
    N -->|no| NF2["failed/…-normalize-failed.csv<br/>not in the index"]
```

A row only enters the index once it is normalized. A row that failed is therefore retried as
soon as the same export arrives again (§8).

## 5. One row in the importer

Unchanged compared to now.

```mermaid
flowchart TD
    R[row from normalized/] --> T{counterparty is an<br/>own account?}
    T -->|yes| C{transfer already in Firefly?<br/>same accounts, amount, ±7 days}
    C -->|yes| CL[claimed: nothing new]
    C -->|no| P
    T -->|no| P[POST to Firefly]
    P -->|200| NEW[new transaction]
    P -->|422 duplicate hash| AP[already present: nothing new]
    P -->|other error| IF["failed/…-import-failed.csv<br/>already in the index"]
```

## 6. Overlapping exports

Made up: three exports of one account, arrived in this order.

```mermaid
sequenceDiagram
    participant B as incoming/
    participant N as normalizer + index
    participant A as archive
    participant F as Firefly
    B->>N: A: 2015–2024 (1000 rows)
    N->>F: 1000 new
    N->>A: A kept
    B->>N: B: 2020–2022 (300 rows, all seen before)
    Note over N: 300 identical → skipped
    N->>A: B kept (0 new rows, still real bank data)
    B->>N: C: 2023–2026 (400 rows, 150 seen before)
    Note over N: 150 identical, 250 new
    N->>F: 250 new
    N->>A: C kept
    B->>N: C again (byte-identical)
    Note over N: 400 identical
    N--xA: not kept: already in the archive
```

Firefly: 1250 transactions. The archive: A, B and C; the rows in the overlap are thus in the
archive more than once, and that is the intention: the archive keeps what the bank gave, the
index deduplicates on every (re)load.

## 7. Starting over

```mermaid
sequenceDiagram
    actor J as you (server)
    participant S as start-over.bash
    participant F as Firefly
    participant D as data/
    participant A as archive
    participant C as cron
    J->>S: ./start-over.bash, type WIPE
    S->>S: take lock (cron waits)
    Note over S: refuses if incoming/ or normalized/ is not empty,<br/>or failed/ contains a -rejected- file
    S->>F: wipe transactions + counterparties, purge
    S->>D: data/ → data-before-start-over/
    S->>D: archive/originals/** → incoming/ (names start with #lt;run#gt;)
    S->>S: release lock
    loop every minute, oldest #lt;run#gt; first
        C->>F: apply accounts.yaml
        C->>D: normalize + import (§4, §5)
        C--xA: nothing new: every file byte-identical
    end
```

Same order as the first time, so the same outcome, also on a conflict (§4: the first version
wins). The token and the one-off setup (checklist, plan 01 step 6) stay outside the reload.

## 8. A row that failed, again

```mermaid
flowchart TD
    subgraph NORM["failed in the normalizer"]
        A1[row in normalize-failed.csv<br/>not in the index] --> A2[improve the parser]
        A2 --> A3[copy the original from the archive<br/>to incoming/]
        A3 --> A4[rest identical, this row new → Firefly]
    end
    subgraph IMP["Firefly refused"]
        B1[row in import-failed.csv<br/>is in the index] --> B2[fix the cause]
        B2 --> B3[move import-failed.csv<br/>to normalized/]
        B3 --> B4[sent again → Firefly]
    end
    A4 -.-> Z[or: start-over, then everything goes again]
    B4 -.-> Z
```

## 9. Accounts file and git

```mermaid
flowchart TD
    E[accounts.yaml edited via the share] --> I{idle check:<br/>newer than stamp? builtin -nt}
    I -->|no, no export| Q[idle: nothing]
    I -->|yes| Y{yaml valid?}
    Y -->|no| AL[alert, nothing applied]
    Y -->|yes| AP[apply in Firefly, update stamp]
    AP --> G
    X[run with a new export] --> G[git add + commit in archive/]
    G --> P{push to Forgejo}
    P -->|succeeds| OK[done]
    P -->|fails| L[commit stays local<br/>next run pushes<br/>one alert per outage]
```

## 10. Open

- Forgejo is not yet in the replication to truenas-backup (todo in sync-truenas-servers): until
  then the archive and Forgejo are on one machine, with the snapshots of `app-ds`.
- Deploy key + ssh config in the cron user's second home directory (`homedir-ds`, survives a
  TrueNAS update); the archive repo points to it with `core.sshCommand`.
- Transition: the reload of plan 02 §4 becomes the first start-over, once with
  `bank-csv-originals/` as source; after that that folder can go.
- A conflict (§4) returns on every reload. How to resolve it (which version wins) is not written
  down anywhere yet.
