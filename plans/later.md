# Later

Ideas not planned yet. One becomes a numbered plan once it is taken up.

- **Loans** as a debt account, if the private finance repo decides so: kind `loan` in the
  accounts file, repayments as a transfer to that account. Principal vs. interest then needs a
  design of its own.
- In the file names in `data/`, a short account name from the accounts file instead of the IBAN.
- **Split large files** (pure refactor; `python -m engine.regression` confirms no output change).
  Start with the first:
  - [engine/firefly/import_normalized.py](../engine/firefly/import_normalized.py) (867 lines):
    transfers (`load_transfer_pool` … `reverse_payment`) → `transfers.py`; request building
    (`build_split`, `ensure_currencies`, asset lookup) → `split.py`; batch and follow-up
    (`count_old_rows` … `set_blocked`) → `batch.py`; own accounts step (`apply_accounts_file`,
    `apply_accounts_only`) → `accounts_step.py`. `import_file` (166 lines) needs cutting too.
    `main()` stays, so `python -m engine.firefly.import_normalized` keeps working.
  - [engine/banks/argenta_mastercard/statement.py](../engine/banks/argenta_mastercard/statement.py)
    (489 lines): `read_statement` is 180 lines. Split layout (lines, header, pages) from reading
    the statement.
  - [engine/firefly/accounts.py](../engine/firefly/accounts.py) (436 lines): file parsing and
    validation (`parse_*`, `load_accounts_file`) → `accounts_file.py`; applying to Firefly
    (`apply_accounts`, `apply_corrections`) → `accounts_apply.py`.
  - Not split: [engine/core/pdf_text.py](../engine/core/pdf_text.py), one cohesive parser.
