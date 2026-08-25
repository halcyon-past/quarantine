"""The ``quarantine`` command line: list, show, retry, debug, clear, stats.

All output goes through :func:`quarantine.reporting.emit`, so a Windows
console that cannot encode ``✓`` gets ASCII instead of a traceback.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ._version import __version__
from .core import Quarantine
from .errors import QuarantineError, StorageError
from .record import Record
from .reporting import columnize, emit
from .resolve import ResolutionError, resolve_function, unwrap_quarantined
from .store import Store, default_dir
from .ui import start_server

__all__ = ["main"]

EXIT_OK = 0
EXIT_PROBLEM = 1
EXIT_USAGE = 2

EPILOG = """\
exit codes:
  0  everything asked for succeeded
  1  the command ran but something is still wrong (e.g. a retry failed again)
  2  bad usage, or the quarantine folder could not be read

examples:
  quarantine list                 # what is in the sick bay
  quarantine show 2               # the full error and input for record 2
  quarantine retry                # re-run everything, drop what now works
  quarantine retry 2 5            # re-run just these
  quarantine debug 2              # a debugger, sitting on the failing frame
  quarantine clear --yes          # empty the folder
  quarantine retry -i job.py      # functions defined in a script (ran as __main__)
"""


def out(text: str = "") -> None:
    """Write a line to stdout, with an ASCII fallback for limited consoles."""
    emit(text, sys.stdout)


def err(text: str) -> None:
    """Write a line to stderr."""
    emit(text, sys.stderr)


# -- argument parsing ----------------------------------------------------


def _add_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-d",
        "--dir",
        default=None,
        metavar="PATH",
        help="quarantine folder (default: $QUARANTINE_DIR or ./.quarantine)",
    )


def build_parser() -> argparse.ArgumentParser:
    """Construct the full argument parser."""
    parser = argparse.ArgumentParser(
        prog="quarantine",
        description="Inspect, retry and debug items your loop set aside.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"quarantine {__version__}")
    subs = parser.add_subparsers(dest="command", metavar="COMMAND")

    listing = subs.add_parser("list", help="show what is in quarantine", aliases=["ls"])
    _add_dir(listing)
    listing.add_argument("-f", "--function", default=None, help="only this function")
    listing.add_argument("-n", "--limit", type=int, default=None, help="show at most N records")
    listing.add_argument("--json", action="store_true", help="machine-readable output")
    listing.set_defaults(handler=cmd_list)

    show = subs.add_parser("show", help="show one record in full")
    _add_dir(show)
    show.add_argument("ids", nargs="+", type=int, metavar="ID")
    show.add_argument("--json", action="store_true", help="machine-readable output")
    show.set_defaults(handler=cmd_show)

    retry = subs.add_parser("retry", help="re-run quarantined items")
    _add_dir(retry)
    retry.add_argument("ids", nargs="*", type=int, metavar="ID", help="default: everything")
    retry.add_argument("-f", "--function", default=None, help="only this function")
    retry.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be retried without running anything",
    )
    retry.add_argument(
        "-i",
        "--import",
        dest="import_from",
        default=None,
        metavar="FILE.py",
        help=(
            "import this file to find the functions (needed when they live in a "
            "script that ran as __main__; the file's top level is executed)"
        ),
    )
    retry.add_argument("--json", action="store_true", help="machine-readable output")
    retry.set_defaults(handler=cmd_retry)

    ui = subs.add_parser("ui", help="start a local web dashboard to view records")
    _add_dir(ui)
    ui.add_argument("--port", type=int, default=8080, help="port to bind to (default: 8080)")
    ui.set_defaults(handler=cmd_ui)

    debug = subs.add_parser("debug", help="open a debugger on a quarantined item")
    _add_dir(debug)
    debug.add_argument("id", type=int, metavar="ID")
    debug.add_argument(
        "--no-post-mortem",
        action="store_true",
        help="do not re-run the function; just hand you the input",
    )
    debug.add_argument(
        "-i",
        "--import",
        dest="import_from",
        default=None,
        metavar="FILE.py",
        help="import this file to find the function (see `quarantine retry --help`)",
    )
    debug.add_argument(
        "-p",
        "--print",
        dest="print_only",
        action="store_true",
        help="print the input and traceback instead of starting a debugger",
    )
    debug.set_defaults(handler=cmd_debug)

    clear = subs.add_parser("clear", help="delete records", aliases=["rm"])
    _add_dir(clear)
    clear.add_argument("ids", nargs="*", type=int, metavar="ID", help="default: everything")
    clear.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    clear.set_defaults(handler=cmd_clear)

    stats = subs.add_parser("stats", help="summarise the folder")
    _add_dir(stats)
    stats.add_argument("--json", action="store_true", help="machine-readable output")
    stats.set_defaults(handler=cmd_stats)

    reindex = subs.add_parser("reindex", help="rebuild index.json from the record folders")
    _add_dir(reindex)
    reindex.set_defaults(handler=cmd_reindex)

    return parser


# -- helpers -------------------------------------------------------------


def _store(args: argparse.Namespace) -> Store:
    return Store(Path(args.dir) if args.dir else default_dir())


def _quarantine(args: argparse.Namespace) -> Quarantine:
    return Quarantine(
        Path(args.dir) if args.dir else default_dir(),
        halt_after=None,
        max_items=None,
        skip_known_bad=False,
        report=False,
    )


def _load(store: Store, function: str | None = None) -> list[Record]:
    records = store.records()
    for problem in store.problems:
        err(f"quarantine: skipping unreadable record: {problem}")
    if function:
        records = [r for r in records if function in (r.function, r.qualified_name)]
    return records


def _terminal_width(default: int = 100) -> int:
    try:
        return max(60, shutil.get_terminal_size((default, 24)).columns)
    except OSError:  # pragma: no cover - very unusual environments
        return default


def _empty_note(store: Store) -> None:
    if not store.exists():
        out(f"Nothing quarantined - {store.dir} does not exist yet.")
    else:
        out(f"Nothing quarantined in {store.dir}. ✓")


# -- commands ------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    """``quarantine list``."""
    store = _store(args)
    records = _load(store, args.function)
    if args.limit is not None:
        records = records[: max(0, args.limit)]
    if args.json:
        out(json.dumps([r.to_meta() for r in records], indent=2))
        return EXIT_OK
    if not records:
        _empty_note(store)
        return EXIT_OK

    width = _terminal_width()
    error_width = max(24, (width - 40) // 2)
    rows = [[r.id, r.when, r.function, r.summary, r.preview or "(no input)"] for r in records]
    lines = columnize(
        rows,
        ["#", "when", "function", "error", "input preview"],
        widths=[4, 8, 22, error_width, max(20, width - 40 - error_width)],
    )
    for line in lines:
        out(line)
    out()
    out(f"{len(records)} in {store.dir} - run `quarantine retry` after fixing your code.")
    return EXIT_OK


def cmd_show(args: argparse.Namespace) -> int:
    """``quarantine show``."""
    store = _store(args)
    status = EXIT_OK
    found = []
    for record_id in args.ids:
        try:
            found.append(store.get(record_id))
        except StorageError as exc:
            err(f"quarantine: {exc}")
            status = EXIT_PROBLEM
    if args.json:
        out(json.dumps([r.to_meta() for r in found], indent=2))
        return status
    for record in found:
        out(f"── record {record.id:04d} " + "─" * 40)
        out(f"function   {record.qualified_name}")
        out(f"error      {record.summary}")
        out(f"first seen {record.created_at}")
        out(f"last seen  {record.last_failed_at}   attempts: {record.attempts}")
        out(f"stored as  {record.payload_format}" + ("  (lossy)" if record.payload_lossy else ""))
        if record.redacted:
            out(f"redacted   {', '.join(record.redacted)}")
        out(f"folder     {record.path}")
        out()
        out("--- input ---")
        out(record.input_text().rstrip())
        out()
        out("--- traceback ---")
        out(record.traceback_text().rstrip())
        out()
    return status


def cmd_retry(args: argparse.Namespace) -> int:
    """``quarantine retry``."""
    instance = _quarantine(args)
    if not instance.store.exists():
        _empty_note(instance.store)
        return EXIT_OK
    result = instance.retry(
        args.ids or None,
        function=args.function,
        dry_run=args.dry_run,
        import_from=args.import_from,
    )
    if args.json:
        out(json.dumps(result.as_dict(), indent=2))
    elif args.dry_run:
        out(f"would retry {len(result.recovered)} record(s): {result.recovered}")
        for record_id, reason in result.unretryable:
            out(f"  ✗ {record_id:04d} cannot be retried: {reason}")
    else:
        parts = [f"✓ {len(result.recovered)} recovered"]
        if result.still_failing:
            parts.append(f"✗ {len(result.still_failing)} still failing (kept in quarantine)")
        if not result.recovered and not result.still_failing and not result.unretryable:
            out("Nothing to retry.")
            return EXIT_OK
        out(" · ".join(parts))
        for record_id, reason in result.unretryable:
            out(f"  ! {record_id:04d} skipped: {reason}")
    if result.still_failing or result.unretryable:
        return EXIT_PROBLEM
    return EXIT_OK


def cmd_debug(args: argparse.Namespace) -> int:
    """``quarantine debug``."""
    store = _store(args)
    try:
        record = store.get(args.id)
    except StorageError as exc:
        err(f"quarantine: {exc}")
        return EXIT_USAGE

    out(f"record {record.id:04d}  {record.qualified_name}")
    out(f"error   {record.summary}")
    if args.print_only:
        out()
        out(record.input_text().rstrip())
        out()
        out(record.traceback_text().rstrip())
        return EXIT_OK

    try:
        call = record.load_call()
    except StorageError as exc:
        err(f"quarantine: {exc}")
        return EXIT_PROBLEM

    target: Callable[..., Any] | None = None
    if not args.no_post_mortem:
        try:
            target = unwrap_quarantined(resolve_function(record, args.import_from))
        except ResolutionError as exc:
            err(f"quarantine: cannot re-run the function ({exc}); handing you the input instead")

    if target is not None:
        return _post_mortem(target, call)
    return _inspect(record, call)


def _post_mortem(target: Callable[..., Any], call: Any) -> int:
    import pdb  # noqa: PLC0415 - a debugger command is exactly when this belongs

    out("re-running the failing call; you will land in the frame that raised.")
    try:
        target(*call.args, **call.kwargs)
    except Exception:  # noqa: BLE001 - reproducing the failure is the whole point
        pdb.post_mortem(sys.exc_info()[2])
        return EXIT_OK
    out("it succeeded this time - nothing to debug. `quarantine retry` will clear it.")
    return EXIT_OK


def _inspect(record: Record, call: Any) -> int:
    import pdb  # noqa: PLC0415 - a debugger command is exactly when this belongs

    item = call.item  # noqa: F841 - deliberately in scope for the debugger
    args = call.args  # noqa: F841
    kwargs = call.kwargs  # noqa: F841
    out(
        f"record {record.id:04d}: `item`, `args`, `kwargs` and `record` are in scope. "
        f"`c` to continue, `q` to quit."
    )
    pdb.Pdb().set_trace()
    return EXIT_OK


def cmd_clear(args: argparse.Namespace) -> int:
    """``quarantine clear``."""
    store = _store(args)
    if args.ids:
        status = EXIT_OK
        for record_id in args.ids:
            try:
                store.get(record_id)
            except StorageError as exc:
                err(f"quarantine: {exc}")
                status = EXIT_PROBLEM
                continue
            store.delete(record_id)
            out(f"deleted {record_id:04d}")
        return status

    total = store.count()
    if not total:
        _empty_note(store)
        return EXIT_OK
    if not args.yes and not _confirm(f"Delete all {total} record(s) in {store.dir}?"):
        out("Left alone.")
        return EXIT_OK
    removed = store.clear()
    store.purge_temp()
    out(f"deleted {removed} record(s) from {store.dir}")
    return EXIT_OK


def _confirm(question: str) -> bool:
    if not sys.stdin or not sys.stdin.isatty():
        err("quarantine: refusing to delete without --yes when input is not a terminal")
        return False
    out(f"{question} [y/N] ")
    try:
        answer = sys.stdin.readline()
    except (OSError, KeyboardInterrupt):  # pragma: no cover - interactive only
        return False
    return answer.strip().lower() in {"y", "yes"}


def cmd_stats(args: argparse.Namespace) -> int:
    """``quarantine stats``."""
    store = _store(args)
    records = _load(store)
    by_function = Counter(r.function for r in records)
    by_error = Counter(r.error_type for r in records)
    disk = _folder_size(store.dir)
    payload = {
        "dir": str(store.dir),
        "exists": store.exists(),
        "records": len(records),
        "bytes": disk,
        "by_function": dict(by_function.most_common()),
        "by_error": dict(by_error.most_common()),
        "oldest": records[0].created_at if records else None,
        "newest": max((r.last_failed_at for r in records), default=None),
        "unreadable": len(store.problems),
    }
    if args.json:
        out(json.dumps(payload, indent=2))
        return EXIT_OK
    if not records:
        _empty_note(store)
        return EXIT_OK
    out(f"{len(records)} record(s) in {store.dir}  ({_human_bytes(disk)} on disk)")
    out(f"oldest {payload['oldest']}   newest {payload['newest']}")
    out()
    out("by function:")
    for name, count in by_function.most_common():
        out(f"  {count:>6}  {name}")
    out("by error:")
    for name, count in by_error.most_common():
        out(f"  {count:>6}  {name}")
    if store.problems:
        out(f"unreadable records: {len(store.problems)} (run `quarantine reindex`)")
    return EXIT_OK


def _folder_size(directory: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(directory):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:  # pragma: no cover - racing deletion
                continue
    return total


KIB = 1024


def _human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < KIB or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= KIB
    return f"{size:.1f} GB"  # pragma: no cover - unreachable, loop always returns


def cmd_reindex(args: argparse.Namespace) -> int:
    """``quarantine reindex``."""
    store = _store(args)
    if not store.exists():
        _empty_note(store)
        return EXIT_OK
    rows = store.rebuild_index()
    stale = store.purge_temp()
    out(f"indexed {len(rows)} record(s) into {store.index_path}")
    if stale:
        out(f"cleaned up {stale} leftover temp entr{'y' if stale == 1 else 'ies'}")
    if store.problems:
        for problem in store.problems:
            err(f"quarantine: {problem}")
        return EXIT_PROBLEM
    return EXIT_OK


def cmd_ui(args: argparse.Namespace) -> int:
    """Start the local web dashboard."""
    return start_server(args.port, Path(args.dir) if args.dir else None)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``quarantine`` command."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return EXIT_USAGE
    try:
        return int(handler(args))
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        err("interrupted")
        return 130
    except QuarantineError as exc:
        err(f"quarantine: {exc}")
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
