import argparse
import os
from pathlib import Path
import sys

from . import search_index
from .bulk import download_workshop
from .console import Console
from .index import (connect, ensure_download_columns, index_directory, scan, upsert_item,
                    upgrade_search_index)
from .steam import discover, download_pending, refresh_sizes, refresh_tags, setup_steamcmd
from .web import serve


def _scan_path(value, prompt_stream=None):
    if value is None:
        try:
            prompt = "Paste or drag a Stormworks vehicle XML file here, then press Enter: "
            if prompt_stream is None:
                value = input(prompt)
            else:
                print(prompt, end="", file=prompt_stream, flush=True)
                value = input()
        except EOFError as exc:
            raise ValueError("No XML file path was entered") from exc
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]
    if not value:
        raise ValueError("No XML file path was entered")
    path = Path(value).expanduser()
    if not path.is_file():
        raise ValueError(f"XML file not found: {path}")
    return path


def main():
    parser = argparse.ArgumentParser(prog="stormcopy", description="Stormworks Workshop vehicle overlap detector")
    parser.add_argument("--db", default="stormcopy.sqlite", help="persistent SQLite index path")
    parser.add_argument("--json", action="store_true", help="machine-readable result instead of a report")
    parser.add_argument("--no-color", action="store_true", help="disable terminal colours")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("index-dir", help="index existing steamapps/workshop/content/573090 or an XML tree")
    p.add_argument("directory")
    p = sub.add_parser("upgrade-index", help="upgrade existing indexed files to MinHash/LSH search")
    p.add_argument("--max-files", type=int, default=0,
                   help="existing XML files to upgrade this run; 0 means all available")
    p = sub.add_parser("add-item", help="queue a known public Workshop item ID")
    p.add_argument("item_id")
    p.add_argument("--title")
    p.add_argument("--size-bytes", type=int, help="optional reported Workshop file size")
    p = sub.add_parser("discover", help="discover public item metadata with a Steam Web API key")
    p.add_argument("--pages", type=int, default=1, help="pages this run; 0 means all remaining pages")
    p.add_argument("--delay", type=float, default=0.5, help="seconds between API pages")
    p.add_argument("--restart", action="store_true")
    p.add_argument("--sort", choices=("published", "updated"), default="published")
    p.add_argument("--tag", action="append", default=[], help="required Workshop tag; repeat to refine")
    p.add_argument("--exclude-tag", action="append", default=[], help="tag to exclude; repeatable")
    p.add_argument("--match-any", action="store_true", help="match any required tag instead of all")
    p = sub.add_parser("tags", help="list known Workshop tags and their item counts")
    p.add_argument("--refresh", action="store_true", help="fetch tags for known items from Steam")
    p.add_argument("--force", action="store_true", help="recheck all known item tags now")
    p.add_argument("--match", default="", help="show tag names containing this text")
    p.add_argument("--limit", type=int, default=40, help="maximum tags to show")
    p = sub.add_parser("refresh-sizes", help="look up sizes for known items, without an API key")
    p.add_argument("--max-items", type=int, default=0, help="IDs to check; 0 means all")
    p.add_argument("--delay", type=float, default=0.5, help="seconds between 50-item metadata calls")
    p.add_argument("--force", action="store_true", help="recheck previously unavailable sizes now")
    p = sub.add_parser("setup-steamcmd", help="install Valve's SteamCMD bootstrap into a local cache")
    p.add_argument("--cache", default="steam-cache")
    p.add_argument("--force", action="store_true", help="replace an existing bootstrap executable")
    p = sub.add_parser("download", help="cache known items smallest-first with SteamCMD")
    p.add_argument("--steamcmd", default="steamcmd")
    p.add_argument("--cache", default="steam-cache")
    p.add_argument("--max-items", type=int, default=50, help="items this run; 0 means all queued")
    p.add_argument("--batch-size", type=int, default=10, help="items per SteamCMD login (1-50)")
    p.add_argument("--workers", type=int, default=4 if os.name == "nt" else 1,
                   help="parallel isolated SteamCMD downloads (1-8; Windows default 4)")
    p.add_argument("--delay", type=float, default=0.5, help="seconds between SteamCMD batches")
    p.add_argument("--login", default="anonymous", help="Steam account name if anonymous access fails")
    p.add_argument("--item-id", help="download one queued Workshop item ID")
    p.add_argument("--cache-only", action="store_true", help="download now; index vehicle XML later with index-dir")
    p.add_argument("--no-size-refresh", action="store_true", help="skip public size lookup; unknown sizes sort last")
    p.add_argument("--metadata-delay", type=float, default=0.5, help="seconds between size lookup calls")
    p.add_argument("--force", action="store_true", help="redownload selected items even if previously cached")
    p.add_argument("--tag", action="append", default=[], help="download only items with this tag; repeatable")
    p.add_argument("--exclude-tag", action="append", default=[], help="skip items with this tag; repeatable")
    p.add_argument("--match-any", action="store_true", help="match any required tag instead of all")
    p.add_argument("--discover-pages", type=int, default=None,
                   help="matching Workshop pages to search first; 0 means all remaining")
    p.add_argument("--known-only", action="store_true", help="skip Workshop search; filter known IDs")
    p = sub.add_parser("download-workshop", help="fill your existing Steam Stormworks Workshop folder")
    p.add_argument("--workshop-folder", help="existing steamapps/workshop/content/573090 folder")
    p.add_argument("--cache", default="steam-cache", help="separate SteamCMD staging folder")
    p.add_argument("--steamcmd", default="steamcmd", help="SteamCMD executable or command name")
    p.add_argument("--known-only", action="store_true", help="download indexed IDs without API discovery")
    p.add_argument("--pages", type=int, default=0, help="discovery pages this run; 0 means all remaining")
    p.add_argument("--restart-discovery", action="store_true", help="start public Workshop discovery again")
    p.add_argument("--max-items", type=int, default=0, help="downloads to try this run; 0 means all queued")
    p.add_argument("--chunk-size", type=int, default=50, help="maximum items held in each download queue")
    p.add_argument("--batch-size", type=int, default=10, help="items per SteamCMD login (1-50)")
    p.add_argument("--workers", type=int, default=4 if os.name == "nt" else 1,
                   help="parallel SteamCMD sessions (1-8; Windows default 4)")
    p.add_argument("--delay", type=float, default=0.5, help="seconds between SteamCMD batches")
    p.add_argument("--metadata-delay", type=float, default=0.5,
                   help="seconds between Workshop metadata requests")
    p.add_argument("--login", default="anonymous", help="Steam account name if anonymous access fails")
    p.add_argument("--reserve-free-gb", type=float, default=20.0,
                   help="stop starting new chunks when free space reaches this amount")
    p.add_argument("--download-only", action="store_true",
                   help="save files now and build the search index later with index-dir")
    p = sub.add_parser("scan", help="compare a vehicle XML; prompts for a path if omitted")
    p.add_argument("xml", nargs="?", help="XML file path; omit to paste or drag it into Command Prompt")
    p.add_argument("--limit", type=int, default=5)
    sub.add_parser("status", help="show how many Workshop files are currently indexed")
    p = sub.add_parser("serve", help="localhost upload interface")
    p.add_argument("--port", type=int, default=8765)
    for command_parser in sub.choices.values():
        command_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                                    help="machine-readable result instead of a report")
        command_parser.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS,
                                    help="disable terminal colours")
    args = parser.parse_args()
    console = Console(json_mode=args.json, color=not args.no_color)
    try:
        if args.command == "serve":
            serve(args.db, args.port)
            return
        if args.command == "setup-steamcmd":
            console.render(args.command, setup_steamcmd(args.cache, args.force))
            return
        db = connect(args.db)
        try:
            if args.command == "index-dir":
                result = index_directory(db, args.directory, progress=console.progress)
            elif args.command == "upgrade-index":
                result = upgrade_search_index(db, args.max_files, progress=console.progress)
            elif args.command == "add-item":
                if not args.item_id.isdigit():
                    raise ValueError("Item ID must be numeric")
                if args.size_bytes is not None and args.size_bytes < 1:
                    raise ValueError("size-bytes must be positive")
                ensure_download_columns(db)
                upsert_item(db, args.item_id, args.title, size_bytes=args.size_bytes)
                result = {"queued": args.item_id}
            elif args.command == "discover":
                result = discover(db, args.pages, args.delay, resume=not args.restart,
                                  sort=args.sort, tags=args.tag,
                                  excluded_tags=args.exclude_tag,
                                  match_all=not args.match_any, progress=console.progress)
            elif args.command == "tags":
                if args.limit < 1:
                    raise ValueError("limit must be positive")
                ensure_download_columns(db)
                lookup = (refresh_tags(db, force=args.force, progress=console.progress)
                          if args.refresh or args.force else None)
                rows = db.execute("SELECT MIN(tag) AS tag,COUNT(*) AS items FROM item_tags "
                                  "GROUP BY tag_key ORDER BY items DESC,tag_key ASC").fetchall()
                matching = [dict(row) for row in rows
                            if args.match.casefold() in row["tag"].casefold()]
                result = {"tags": matching[:args.limit], "matching_tags": len(matching),
                          "known_items": db.execute("SELECT COUNT(*) FROM items").fetchone()[0],
                          "items_checked": db.execute(
                              "SELECT COUNT(*) FROM items WHERE tag_checked>0").fetchone()[0]}
                if lookup is not None:
                    result["tag_refresh"] = lookup
            elif args.command == "refresh-sizes":
                result = refresh_sizes(db, args.max_items, args.delay, force=args.force,
                                       progress=console.progress)
            elif args.command == "download":
                filtered = bool(args.tag or args.exclude_tag)
                if args.known_only and args.discover_pages is not None:
                    raise ValueError("--known-only cannot be combined with --discover-pages")
                if args.discover_pages is not None and args.discover_pages < 0:
                    raise ValueError("discover-pages must be nonnegative")
                api_key = os.environ.get("STEAM_API_KEY")
                if args.discover_pages is not None and not api_key:
                    raise ValueError("Set STEAM_API_KEY to search new Workshop pages")
                pages = (args.discover_pages if args.discover_pages is not None else
                         1 if filtered and api_key and not args.known_only else None)
                discovery = None
                if pages is not None:
                    discovery = discover(db, pages, api_key=api_key, tags=args.tag,
                                         excluded_tags=args.exclude_tag,
                                         match_all=not args.match_any,
                                         progress=console.progress)
                tag_lookup = (refresh_tags(db, delay=args.metadata_delay,
                                           progress=console.progress)
                              if filtered else None)
                sizes = None if args.no_size_refresh else refresh_sizes(
                    db, delay=args.metadata_delay, pending_only=not args.force,
                    tags=args.tag, excluded_tags=args.exclude_tag,
                    match_all=not args.match_any, progress=console.progress)
                result = download_pending(db, args.cache, args.steamcmd, args.max_items,
                                          args.delay, args.login, args.batch_size, args.force,
                                          args.item_id, args.cache_only, args.workers,
                                          args.tag, args.exclude_tag, not args.match_any,
                                          console.progress)
                if sizes is not None:
                    result["size_refresh"] = sizes
                if filtered:
                    result["tag_refresh"] = tag_lookup
                    result["search_scope"] = ("matching Workshop pages and known items"
                                              if discovery is not None else "known items only")
                if discovery is not None:
                    result["discovery"] = discovery
            elif args.command == "download-workshop":
                result = download_workshop(
                    db, workshop_folder=args.workshop_folder, cache=args.cache,
                    steamcmd=args.steamcmd, known_only=args.known_only,
                    pages=args.pages, restart_discovery=args.restart_discovery,
                    max_items=args.max_items, chunk_size=args.chunk_size,
                    workers=args.workers, batch_size=args.batch_size,
                    delay=args.delay, metadata_delay=args.metadata_delay,
                    login=args.login, reserve_free_gb=args.reserve_free_gb,
                    download_only=args.download_only, progress=console.progress)
            elif args.command == "status":
                result = {"indexed_vehicle_files": db.execute("SELECT COUNT(*) FROM files").fetchone()[0],
                          "lsh_ready_files": db.execute(
                              "SELECT COUNT(*) FROM search_files WHERE version=?",
                              (search_index.SEARCH_INDEX_VERSION,)).fetchone()[0],
                          "indexed_items": db.execute("SELECT COUNT(DISTINCT item_id) FROM files").fetchone()[0],
                          "known_items": db.execute("SELECT COUNT(*) FROM items").fetchone()[0],
                          "cached_items": db.execute("SELECT COUNT(*) FROM items WHERE downloaded>0").fetchone()[0],
                          "items_with_tags": db.execute(
                              "SELECT COUNT(DISTINCT item_id) FROM item_tags").fetchone()[0]}
                if "size_bytes" in {row["name"] for row in db.execute("PRAGMA table_info(items)")}:
                    result["known_sizes"] = db.execute(
                        "SELECT COUNT(*) FROM items WHERE size_bytes IS NOT NULL").fetchone()[0]
                    result["queued_downloads"] = db.execute(
                        "SELECT COUNT(*) FROM items WHERE id != '' AND id NOT GLOB '*[^0-9]*' "
                        "AND (downloaded=0 OR updated>downloaded)").fetchone()[0]
            else:
                result = scan(db, _scan_path(args.xml, sys.stderr if args.json else None), args.limit)
        finally:
            db.close()
        console.render(args.command, result)
    except KeyboardInterrupt:
        console.finish_progress()
        parser.exit(130, "\nStopped. Indexed files are saved; rerun the same command to resume.\n")
    except (ValueError, OSError, RuntimeError) as exc:
        console.finish_progress()
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()
