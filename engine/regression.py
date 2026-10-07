"""
Regression test for the normalizer: run every row of the given bank CSVs
through two versions of the code — a git ref (default HEAD) and the working
tree — and report every row whose result changes.

Usage (desktop, in the repo; the CSVs are only read, e.g. from the share):
  python -m engine.regression [--base REF] [--show N] <csv-or-glob> [...]

Rows are identified by (account IBAN, Volgnummer), so overlapping exports are
counted once. Categories: fixed (failed -> ok), broken (ok -> failed),
changed (ok -> ok, other output), failing differently (failed -> failed, other
reason). Exit code 1 when anything changed, so it can gate a change.
"""

import argparse
import collections
import glob
import importlib
import io
import os
import subprocess
import sys
import tarfile
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def import_from(root: str, module_name: str):
    """
    Import `module_name` from `root` only. The repo root (the working directory
    of `python -m`) stays off sys.path meanwhile: Python prefers a package with an
    __init__.py anywhere on the path over one without, so an old ref without them
    would silently load the working tree instead.
    """
    saved_path = sys.path[:]
    sys.path[:] = [root] + [p for p in saved_path if os.path.abspath(p or os.curdir) != REPO_ROOT]
    try:
        module = importlib.import_module(module_name)
    finally:
        sys.path[:] = saved_path
    if not os.path.abspath(module.__file__).startswith(os.path.abspath(root) + os.sep):
        raise RuntimeError(f"{module_name} loaded from {module.__file__}, not from {root}")
    return module


def load_engine(root: str):
    """Import csv_runtime/csv_validation from `root`, dropping any engine modules loaded before."""
    for name in [m for m in sys.modules if m == "engine" or m.startswith("engine.")]:
        del sys.modules[name]
    return import_from(root, "engine.core.csv_runtime"), import_from(root, "engine.core.csv_validation")


def run(root: str, paths: list[str]) -> dict[tuple[str, str], tuple[str, object, str]]:
    """(account, external_id) -> (status, result, 'file:line'); status ok / failed / invalid."""
    csv_runtime, csv_validation = load_engine(root)
    configs = csv_runtime.load_all_bank_configs(os.path.join(root, "config"))
    results: dict[tuple[str, str], tuple[str, object, str]] = {}
    for path in paths:
        raw_rows = csv_runtime.load_csv_rows(path)
        bank_cfg = csv_validation.autodetect_bank(raw_rows, configs)
        bank = import_from(root, f"engine.banks.{bank_cfg['bank']}")
        validated = csv_validation.validate_and_prepare(raw_rows, bank_cfg)[0]
        for row in validated:
            key = (row.get("asset_account_iban", ""), row.get("external_id", ""))
            if key in results:
                continue
            where = f"{os.path.basename(path)}:{row.get('_source_line', '?')}"
            if "_validation_error" in row:
                results[key] = ("invalid", row["_validation_error"], where)
                continue
            try:
                results[key] = ("ok", bank.normalize_row(row), where)
            except Exception as exc:  # noqa: BLE001 - any failure is a result to compare
                results[key] = ("failed", str(exc), where)
    return results


def checkout(ref: str, target: str) -> None:
    archive = subprocess.run(
        ["git", "-C", REPO_ROOT, "archive", "--format=tar", ref, "engine", "config"],
        capture_output=True,
        check=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(target, filter="data")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="HEAD", help="git ref to compare against (default HEAD)")
    parser.add_argument("--show", type=int, default=15, help="examples per category (default 15, 0 = all)")
    parser.add_argument("csv", nargs="+", help="bank CSV files or glob patterns")
    args = parser.parse_args()

    paths = sorted({p for pattern in args.csv for p in (glob.glob(pattern) or [pattern])})
    with tempfile.TemporaryDirectory() as base_root:
        checkout(args.base, base_root)
        old = run(base_root, paths)
    new = run(REPO_ROOT, paths)

    categories: dict[str, list[str]] = collections.defaultdict(list)
    changed_fields: collections.Counter = collections.Counter()
    for key in sorted(set(old) | set(new)):
        old_status, old_result, where = old.get(key, ("absent", None, "?"))
        new_status, new_result, where = new.get(key, ("absent", None, where))
        label = f"{where} {key[1]}"
        if old_status == new_status == "ok":
            # Union, so a field dropped from (or added to) the output is reported too
            diffs = [f for f in {**old_result, **new_result} if old_result.get(f) != new_result.get(f)]
            if diffs:
                changed_fields.update(diffs)
                lines = "".join(f"\n      {f}: {old_result.get(f)!r} -> {new_result.get(f)!r}" for f in diffs)
                categories["changed (ok -> ok, other output)"].append(f"{label}{lines}")
        elif old_status != "ok" and new_status == "ok":
            name = new_result.get("opposing_account_name", "")
            categories["fixed (failed -> ok)"].append(f"{label}\n      was: {old_result}\n      name: {name!r}")
        elif old_status == "ok" and new_status != "ok":
            categories["broken (ok -> failed)"].append(f"{label}\n      now: {new_result}")
        elif old_status == new_status and old_result != new_result:
            categories["failing differently"].append(f"{label}\n      was: {old_result}\n      now: {new_result}")

    print(f"base {args.base} vs working tree, {len(paths)} files, {len(new)} unique rows")
    print(f"  failing: {sum(s != 'ok' for s, _, _ in old.values())} -> {sum(s != 'ok' for s, _, _ in new.values())}")
    for category, items in categories.items():
        print(f"\n== {category}: {len(items)}")
        if category.startswith("changed"):
            print(f"   fields: {dict(changed_fields)}")
        for item in items if args.show == 0 else items[: args.show]:
            print(f"   {item}")
        if args.show and len(items) > args.show:
            print(f"   ... {len(items) - args.show} more (--show 0 for all)")
    if not categories:
        print("\nno differences")
    return 1 if categories else 0


if __name__ == "__main__":
    sys.exit(main())
