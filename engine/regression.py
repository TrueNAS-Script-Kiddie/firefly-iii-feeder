"""
Regression test for the normalizer: run every row of the given bank exports
through two versions of the code — a git ref (default HEAD) and the working
tree — and report every row whose result changes.

Usage (desktop, in the repo; the exports are only read, e.g. from the share):
  python -m engine.regression [--base REF] [--show N] <file-or-glob> [...]
  python -m engine.regression "//<server>/firefly-iii-feeder/archive/originals/**/*"

A glob is recursive (** crosses folders), so the archive's originals of every
bank and file type are covered at once. Rows are identified as the row state
does, by account and duplicate key, each computed with that version's own code
and config, so overlapping exports are counted once; a row without a key by
<file>:<line>. A file a version cannot read gives that version no rows and is
listed with the reason. Categories: fixed (failed -> ok), broken (ok ->
failed), changed (ok -> ok, other output), failing differently (failed ->
failed, other reason), new / gone in this version (the row exists in one
version only), readable in one version only. Exit code 1 when anything
changed, so it can gate a change.
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


def read_file(root: str, csv_runtime, csv_validation, configs: dict, path: str):
    """Validated rows, bank config and bank module of one file, as process_csv reads it."""
    # A version from before load_rows reads only CSV, numbering its lines itself
    if hasattr(csv_runtime, "load_rows"):
        raw_rows, source_lines, _ = csv_runtime.load_rows(path, configs)
        prepare_args = (source_lines,)
    else:
        raw_rows, prepare_args = csv_runtime.load_csv_rows(path), ()
    bank_cfg = csv_validation.autodetect_bank(raw_rows, configs)
    bank = import_from(root, f"engine.banks.{bank_cfg['bank']}")
    return csv_validation.validate_and_prepare(raw_rows, bank_cfg, *prepare_args)[0], bank_cfg, bank


def row_identity(csv_validation, row: dict, bank_cfg: dict, where: str) -> str:
    """'<account>|<duplicate key>' as the row state has it (account as process_csv names it); else `where`."""
    key = csv_validation.extract_duplicate_key(row, bank_cfg)
    if not key:
        return where
    partition_by = bank_cfg.get("duplicate_key", {}).get("partition_by")
    account = row.get(partition_by, "").replace(" ", "").upper() if partition_by else bank_cfg["bank"]
    return f"{account}|{key}"


def run(root: str, paths: list[str]) -> tuple[dict[str, tuple[str, object, str]], dict[str, str]]:
    """
    identity -> (status, result, 'file:line'), status ok / failed / invalid; and
    path -> reason for every file this version cannot read.
    """
    csv_runtime, csv_validation = load_engine(root)
    configs = csv_runtime.load_all_bank_configs(os.path.join(root, "config"))
    results: dict[str, tuple[str, object, str]] = {}
    unreadable: dict[str, str] = {}
    for path in paths:
        try:
            validated, bank_cfg, bank = read_file(root, csv_runtime, csv_validation, configs, path)
        except Exception as exc:  # noqa: BLE001 - an unreadable file is a result to compare
            unreadable[path] = f"{type(exc).__name__}: {exc}"
            continue
        for row in validated:
            where = f"{os.path.basename(path)}:{row.get('_source_line', '?')}"
            identity = row_identity(csv_validation, row, bank_cfg, where)
            if identity in results:
                continue
            if "_validation_error" in row:
                results[identity] = ("invalid", row["_validation_error"], where)
                continue
            try:
                results[identity] = ("ok", bank.normalize_row(row), where)
            except Exception as exc:  # noqa: BLE001 - any failure is a result to compare
                results[identity] = ("failed", str(exc), where)
    return results, unreadable


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
    parser.add_argument("files", nargs="+", help="bank exports or glob patterns (** is recursive)")
    args = parser.parse_args()

    paths: set[str] = set()
    for pattern in args.files:
        matched = [p for p in glob.glob(pattern, recursive=True) if os.path.isfile(p)]
        if not matched:
            print(f"no file matches {pattern}", file=sys.stderr)
            return 2
        paths.update(matched)
    paths_sorted = sorted(paths)
    with tempfile.TemporaryDirectory() as base_root:
        checkout(args.base, base_root)
        old, old_unreadable = run(base_root, paths_sorted)
    new, new_unreadable = run(REPO_ROOT, paths_sorted)

    categories: dict[str, list[str]] = collections.defaultdict(list)
    changed_fields: collections.Counter = collections.Counter()
    new_rows: collections.Counter = collections.Counter()  # ok / failed among rows new in this version
    for identity in sorted(set(old) | set(new)):
        old_status, old_result, where = old.get(identity, ("absent", None, "?"))
        new_status, new_result, where = new.get(identity, ("absent", None, where))
        label = where if identity == where else f"{where} {identity}"
        if old_status == "absent":
            new_rows["ok" if new_status == "ok" else "failed"] += 1
            outcome = "ok" if new_status == "ok" else f"{new_status}: {new_result}"
            categories["new in this version"].append(f"{label}\n      {outcome}")
        elif new_status == "absent":
            categories["gone in this version"].append(f"{label}\n      was {old_status}")
        elif old_status == new_status == "ok":
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

    unreadable_in_both = []
    for path in sorted(set(old_unreadable) | set(new_unreadable)):
        name = os.path.basename(path)
        if path in old_unreadable and path in new_unreadable:
            unreadable_in_both.append(f"{name}\n      {new_unreadable[path]}")
        else:
            was, now = old_unreadable.get(path, "readable"), new_unreadable.get(path, "readable")
            categories["readable in one version only"].append(f"{name}\n      was: {was}\n      now: {now}")

    print(f"base {args.base} vs working tree, {len(paths)} files, {len(new)} unique rows")
    print(f"  failing: {sum(s != 'ok' for s, _, _ in old.values())} -> {sum(s != 'ok' for s, _, _ in new.values())}")
    print(f"  unreadable files: {len(old_unreadable)} -> {len(new_unreadable)}")
    if unreadable_in_both:
        print(f"\n== unreadable in both versions (not a change): {len(unreadable_in_both)}")
        for item in unreadable_in_both:
            print(f"   {item}")
    for category, items in categories.items():
        if category == "new in this version":
            print(f"\n== {category}: {len(items)} (ok {new_rows['ok']}, failed {new_rows['failed']})")
        else:
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
