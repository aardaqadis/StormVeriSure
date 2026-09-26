"""Readable command-line reports and restrained, terminal-aware progress bars.

The detector returns dictionaries so callers can use its results directly. This
module is only a presentation layer; ``--json`` keeps the original data format.
"""

import json
import os
import shutil
import sys
import threading
import time
import unicodedata


_COLOURS = {
    "heading": "1;36",
    "good": "1;32",
    "warning": "1;33",
    "bad": "1;31",
    "muted": "2",
    "progress": "1;36",
}


def _enable_windows_vt(stream):
    """Enable ANSI colours in a Windows console, without changing pipe output."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        import msvcrt

        handle = wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
        kernel = ctypes.windll.kernel32
        kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetConsoleMode.restype = wintypes.BOOL
        kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.SetConsoleMode.restype = wintypes.BOOL
        mode = wintypes.DWORD()
        if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if mode.value & 0x0004:
            return True
        return bool(kernel.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError, ValueError):
        return False


def _clean(value, limit=180):
    """Keep Workshop-supplied titles and details on one safe terminal line."""
    value = "" if value is None else str(value)
    value = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in value)
    value = " ".join(value.split())
    return value[:limit - 3] + "..." if len(value) > limit else value


def _number(value):
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "?"


def _percent(value):
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return "?"


def _duration(value):
    try:
        seconds = max(0, float(value))
    except (TypeError, ValueError):
        return "?"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes}m {seconds:02d}s"


def _position(value):
    if isinstance(value, (list, tuple)):
        return "(" + ", ".join(_clean(part, 24) for part in value[:3]) + ")"
    return _clean(value, 72) or "?"


class Console:
    """Render final dictionaries to stdout and progress events to stderr.

    Events are dictionaries with ``phase``, ``done``, ``total`` (optional), and
    ``detail`` (optional). The caller may also provide ``failed`` and ``active``.
    Call ``finish_progress`` before printing an unrelated diagnostic line.
    """

    def __init__(self, json_mode=False, color=True, stdout=None, stderr=None,
                 progress_interval=10.0, heartbeat_interval=None):
        self.json_mode = json_mode
        self.stdout = stdout if stdout is not None else sys.stdout
        self.stderr = stderr if stderr is not None else sys.stderr
        # Windows pipes and older Command Prompt code pages may reject a
        # Workshop title that contains characters outside their encoding.
        # Keep the report usable while preserving the terminal's code page.
        for stream in (self.stdout, self.stderr):
            if (stream is sys.stdout or stream is sys.stderr) and hasattr(
                    stream, "reconfigure"):
                try:
                    stream.reconfigure(errors="replace")
                except (OSError, ValueError):
                    pass
        self.progress_interval = progress_interval
        self._out_tty = bool(getattr(self.stdout, "isatty", lambda: False)())
        self._err_tty = bool(getattr(self.stderr, "isatty", lambda: False)())
        allow_color = color and not json_mode and "NO_COLOR" not in os.environ
        allow_color = allow_color and os.environ.get("TERM") != "dumb"
        self._out_color = bool(allow_color and self._out_tty and _enable_windows_vt(self.stdout))
        self._err_color = bool(allow_color and self._err_tty and _enable_windows_vt(self.stderr))
        self._phase = None
        self._last_report = float("-inf")
        self._last_width = 0
        self._progress_active = False
        self._lock = threading.RLock()
        self._phase_started = None
        self._last_event = None
        self._heartbeat_stop = None
        self._heartbeat_interval = (heartbeat_interval if heartbeat_interval is not None
                                    else 5.0 if self._err_tty else 10.0)
        if self._heartbeat_interval <= 0:
            raise ValueError("heartbeat_interval must be positive")

    def _style(self, value, colour, stream):
        enabled = self._out_color if stream is self.stdout else self._err_color
        return f"\x1b[{_COLOURS[colour]}m{value}\x1b[0m" if enabled else value

    def _line(self, text="", colour=None):
        if colour:
            text = self._style(text, colour, self.stdout)
        print(text, file=self.stdout, flush=True)

    def progress(self, event):
        """Show a concise bar; pipes receive occasional complete text lines."""
        with self._lock:
            self._progress(event)

    def _progress(self, event):
        if not isinstance(event, dict):
            raise TypeError("progress event must be a dictionary")
        warning = event.get("warning")
        if warning:
            if self._progress_active:
                print(file=self.stderr, flush=True)
                self._progress_active = False
                self._last_width = 0
            message = _clean(warning if warning is not True else
                             event.get("detail") or "Needs attention", 240)
            print(self._style("Warning: " + message, "warning", self.stderr),
                  file=self.stderr, flush=True)
        phase = _clean(event.get("phase") or "Working", 32).replace("_", " ").title()
        phase = phase.replace("Xml", "XML")
        try:
            done = max(0, int(event.get("done") or 0))
        except (TypeError, ValueError):
            done = 0
        try:
            total = int(event.get("total")) if event.get("total") is not None else None
        except (TypeError, ValueError):
            total = None
        if total is not None and total < 0:
            total = None
        detail = _clean(event.get("detail"), 120)
        if event.get("active") is not None:
            detail = (detail + " | " if detail else "") + f"{_number(event['active'])} active"
        if event.get("failed"):
            detail = (detail + " | " if detail else "") + f"{_number(event['failed'])} failed"

        now = time.monotonic()
        phase_changed = phase != self._phase
        if phase_changed:
            self.finish_progress()
            self._phase = phase
            self._last_report = float("-inf")
            self._phase_started = now

        # A warning is a one-time line, not part of the repeated heartbeat.
        self._last_event = {key: value for key, value in event.items() if key != "warning"}
        if self._phase_started is not None:
            elapsed = _duration(now - self._phase_started)
            detail = (detail + " | " if detail else "") + f"elapsed {elapsed}"

        complete = total is not None and total > 0 and done >= total
        if complete and self._heartbeat_stop is not None:
            self._heartbeat_stop.set()
            self._heartbeat_stop = None
        if not self._err_tty and not (phase_changed or complete or
                                      now - self._last_report >= self.progress_interval):
            return

        if total is not None and total > 0:
            fraction = min(1.0, done / total)
            width = 24
            filled = int(width * fraction)
            bar = "[" + "=" * filled + (">" if filled < width else "")
            bar += "." * (width - filled - (filled < width)) + "]"
            count = f"{_number(done)}/{_number(total)} ({fraction:.0%})"
        else:
            bar = "[working]"
            count = f"{_number(done)} done" if done else ""
        plain = f"{phase}: {bar} {count}".rstrip()
        if detail:
            plain += " | " + detail
        if self._err_tty:
            try:
                columns = os.get_terminal_size(self.stderr.fileno()).columns
            except (AttributeError, OSError, ValueError):
                columns = shutil.get_terminal_size((100, 24)).columns
            # A carriage return and padding work even when ANSI is unavailable.
            plain = plain[:max(1, columns - 1)]
            body = self._style(plain, "progress", self.stderr)
            padding = max(0, min(self._last_width - len(plain), columns - 1 - len(plain)))
            print("\r" + body + " " * padding,
                  end="", file=self.stderr, flush=True)
            self._last_width = len(plain)
            self._progress_active = True
        else:
            print(plain, file=self.stderr, flush=True)
        self._last_report = now
        if not complete and self._heartbeat_stop is None:
            stop = threading.Event()
            self._heartbeat_stop = stop
            threading.Thread(target=self._heartbeat, args=(stop,), daemon=True).start()

    def _heartbeat(self, stop):
        while not stop.wait(self._heartbeat_interval):
            with self._lock:
                if stop.is_set() or self._heartbeat_stop is not stop or self._last_event is None:
                    return
                self._progress(self._last_event)

    def finish_progress(self):
        """Move below the last in-place bar before another message appears."""
        with self._lock:
            self._finish_progress()

    def _finish_progress(self):
        if self._heartbeat_stop is not None:
            self._heartbeat_stop.set()
            self._heartbeat_stop = None
        if self._progress_active:
            print(file=self.stderr, flush=True)
        self._progress_active = False
        self._last_width = 0
        self._phase = None
        self._phase_started = None
        self._last_event = None

    def render(self, command, result):
        """Print a final report, using JSON only when explicitly requested."""
        with self._lock:
            self._render(command, result)

    def _render(self, command, result):
        self.finish_progress()
        if self.json_mode:
            print(json.dumps(result, indent=2), file=self.stdout, flush=True)
            return
        if command == "scan":
            self._render_scan(result)
        elif command == "status":
            self._render_status(result)
        elif command == "download":
            self._render_download(result)
        elif command == "download-workshop":
            self._render_download_workshop(result)
        elif command == "discover":
            self._render_discover(result)
        elif command == "tags":
            self._render_tags(result)
        elif command == "refresh-sizes":
            self._render_sizes(result)
        elif command == "index-dir":
            self._render_index(result)
        elif command == "upgrade-index":
            self._render_upgrade(result)
        elif command == "add-item":
            self._line("Workshop item queued", "heading")
            self._line(f"Item ID: {_clean(result.get('queued'))}")
        elif command == "setup-steamcmd":
            self._line("SteamCMD ready", "heading")
            self._line(f"Location: {_clean(result.get('steamcmd'))}")
            self._line("Installed now" if result.get("installed") else "Already installed")
        else:
            self._line(_clean(command).replace("-", " ").title() + " complete", "heading")
            for key, value in result.items():
                self._line(f"{key.replace('_', ' ').capitalize()}: {_clean(value)}")

    def _render_scan(self, result):
        status = _clean(result.get("status") or "unknown", 70)
        suspicion = _clean(result.get("suspicion_level") or "unknown", 70)
        colour = ("bad" if suspicion == "high" else
                  "warning" if suspicion == "medium" else "good" if suspicion.startswith("low") else
                  "muted")
        self._line("Comparison result", "heading")
        self._line(f"Status: {status.capitalize()}", colour)
        self._line(f"Suspicion: {suspicion.capitalize()}", colour)
        best = result.get("best_match")
        if best:
            self._line()
            title = _clean(best.get("title") or "Untitled Workshop item", 120)
            self._line(f"Best match: {title} (ID {_clean(best.get('item_id'))})")
            if best.get("url"):
                self._line(f"Workshop: {_clean(best['url'], 240)}")
            self._line(f"Geometry similarity: {_percent(best.get('similarity_percent'))} "
                       "of the submitted vehicle")
            self._line(f"Workshop coverage: {_percent(best.get('workshop_coverage_percent'))} of that item")
            if best.get("combined_similarity_percent") is not None:
                self._line(f"Multi-signal overlap: "
                           f"{_percent(best['combined_similarity_percent'])} (heuristic)")
            if best.get("minhash_jaccard_estimate_percent") is not None:
                self._line(f"MinHash Jaccard estimate: "
                           f"{_percent(best['minhash_jaccard_estimate_percent'])}")
            self._line(f"Confidence: {_clean(best.get('confidence') or 'unknown')} (heuristic)")
            self._line(f"Shared structures: {_number(best.get('shared_neighborhoods'))} neighborhoods; "
                       f"{_number(best.get('rare_shared_neighborhoods'))} rare")
            channels = best.get("channels") or {}
            for key, label in (("winnowing", "Shared component sequences"),
                               ("logic", "Shared logic links"),
                               ("microcontrollers", "Shared microcontroller patterns")):
                channel = channels.get(key) or {}
                if channel.get("shared"):
                    self._line(f"{label}: {_number(channel['shared'])} "
                               f"({_percent(channel.get('query_coverage_percent'))} "
                               "of submitted file)")
            if best.get("partial_copy_evidence"):
                self._line(f"Coherent copied section: "
                           f"{_number(best.get('largest_matching_cluster'))} "
                           "nearby rare structure anchors", "warning")
            if best.get("semantic_copy_evidence"):
                self._line("Distinctive microcontroller content also matches", "warning")
            evidence = best.get("evidence") or []
            if evidence:
                self._line()
                self._line("Evidence: matching positions")
                for item in evidence[:7]:
                    query = _position(item.get("query_position"))
                    workshop = _position(item.get("workshop_position"))
                    count = _number(item.get("matching_components"))
                    occurrence = item.get("indexed_occurrences")
                    scope = "sampled" if item.get("occurrence_scope") == "sampled" else "indexed"
                    frequency = (f"; seen in {_number(occurrence)} {scope} files"
                                 if occurrence is not None else "")
                    channel = _clean(item.get("channel") or "geometry", 20)
                    self._line(f"  {channel}: {query} <-> {workshop} | "
                               f"{count} matching{frequency}")
            others = (result.get("matches") or [])[1:]
            if others:
                self._line()
                self._line("Other close matches:")
                for item in others[:4]:
                    self._line(f"  {_clean(item.get('title') or item.get('item_id'), 100)}: "
                               f"{_percent(item.get('similarity_percent'))} "
                               f"({_clean(item.get('confidence'))} confidence)")
        else:
            self._line(result.get("message") or "No significant match was found in the local index.")
        coverage = result.get("coverage") or {}
        self._line()
        self._line("Index coverage")
        self._line(f"  {_number(coverage.get('indexed_items'))} Workshop items and "
                   f"{_number(coverage.get('indexed_files'))} vehicle files indexed "
                   f"({_number(coverage.get('known_items'))} known items)")
        if coverage.get("modern_search_files") is not None:
            self._line("  MinHash/LSH ready: " +
                       f"{_number(coverage['modern_search_files'])} of "
                       f"{_number(coverage.get('indexed_files'))} vehicle files")
        if coverage.get("discovery_complete") is not None:
            self._line("  Discovery pass complete: " +
                       ("yes" if coverage.get("discovery_complete") else "no"))
        if result.get("note"):
            self._line()
            self._line(_clean(result["note"], 500))

    def _render_status(self, result):
        self._line("Workshop cache and index", "heading")
        fields = (("Known Workshop items", "known_items"),
                  ("Cached items", "cached_items"),
                  ("Queued downloads", "queued_downloads"),
                  ("Items with known sizes", "known_sizes"),
                  ("Items with saved tags", "items_with_tags"),
                  ("Indexed Workshop items", "indexed_items"),
                  ("Indexed vehicle files", "indexed_vehicle_files"),
                  ("MinHash/LSH ready files", "lsh_ready_files"))
        for label, key in fields:
            if key in result:
                self._line(f"{label}: {_number(result[key])}")

    def _render_download(self, result):
        self._line("Workshop download finished", "heading")
        self._line(f"Selected: {_number(result.get('selected'))} | Cached: "
                   f"{_number(result.get('cached'))} | Failed: {_number(result.get('failed'))}")
        if "downloaded_and_indexed" in result:
            self._line(f"Indexed now: {_number(result.get('downloaded_and_indexed'))} | "
                       f"Cached for later indexing: {_number(result.get('cached_unindexed'))}")
        if result.get("cached_without_vehicle"):
            self._line(f"Cached without vehicle XML: {_number(result['cached_without_vehicle'])}", "warning")
        self._line(f"Batches: {_number(result.get('batches'))} | Workers: "
                   f"{_number(result.get('workers_used'))} | Elapsed: "
                   f"{_duration(result.get('elapsed_seconds'))}")
        if "reported_sizes_known" in result:
            self._line(f"Reported sizes known: {_number(result['reported_sizes_known'])} | "
                       f"Unknown: {_number(result.get('reported_sizes_unknown'))}")
        if result.get("size_refresh"):
            sizes = result["size_refresh"]
            self._line(f"Sizes checked this run: {_number(sizes.get('checked'))}; "
                       f"newly found: {_number(sizes.get('sizes_added'))}")
        if result.get("tags") or result.get("excluded_tags"):
            required = ", ".join(_clean(tag, 60) for tag in result.get("tags") or ()) or "any"
            excluded = ", ".join(_clean(tag, 60) for tag in result.get("excluded_tags") or ())
            self._line(f"Required tags: {required}" + (f" | Excluded: {excluded}" if excluded else ""))
            self._line(f"Search scope: {_clean(result.get('search_scope') or 'known items only')}")
        if result.get("discovery"):
            discovery = result["discovery"]
            self._line(f"Workshop pages searched now: {_number(discovery.get('pages'))}; "
                       f"matching items found: {_number(discovery.get('items_seen'))}")
        if result.get("tag_refresh"):
            self._line(f"Known item tags checked now: "
                       f"{_number(result['tag_refresh'].get('checked'))}")
        if result.get("cached_unindexed"):
            self._line("Run index-dir on the cache to make new files searchable.", "warning")

    def _render_download_workshop(self, result):
        self._line("Stormworks Workshop download", "heading")
        self._line(f"Folder: {_clean(result.get('workshop_folder'), 240)}")
        discovery = result.get("discovery")
        if discovery is not None:
            self._line(f"Public pages checked this run: {_number(discovery.get('pages'))} | "
                       f"Public items found: {_number(discovery.get('items_seen'))} | "
                       f"Discovery complete: {'yes' if result.get('discovery_complete') else 'no'}")
        else:
            self._line("Discovery: skipped; using already known item IDs")
        existing = result.get("existing") or {}
        self._line(f"Existing folders checked: {_number(existing.get('folders'))} | "
                   f"Likely current: {_number(existing.get('current'))} | "
                   f"Older than Workshop metadata: {_number(existing.get('stale'))}")
        self._line(f"Known items: {_number(result.get('known_items'))} | "
                   f"Ready in this folder: {_number(result.get('in_workshop_folder'))}")
        self._line(f"Tried this run: {_number(result.get('attempted'))} | "
                   f"Downloaded: {_number(result.get('downloaded'))} | "
                   f"Failed: {_number(result.get('failed'))}")
        self._line(f"Indexed now: {_number(result.get('indexed'))} | "
                   f"Still queued: {_number(result.get('remaining_pending'))} | "
                   f"Waiting to retry: {_number(result.get('waiting_to_retry'))}")
        self._line(f"Stopped: {_clean(result.get('stop_reason'))} | "
                   f"Elapsed: {_duration(result.get('elapsed_seconds'))}")
        if existing.get("unindexed") or result.get("cached_without_vehicle"):
            self._line(f"Existing folders not indexed: {_number(existing.get('unindexed'))} | "
                       f"New folders without vehicle XML: "
                       f"{_number(result.get('cached_without_vehicle'))}", "warning")
            self._line("Run index-dir on the Workshop folder to make existing XML searchable.",
                       "warning")
        if result.get("stop_reason") == "disk reserve reached":
            self._line("Free disk space reached the chosen reserve; rerun after making room.",
                       "warning")

    def _render_discover(self, result):
        self._line("Workshop discovery finished", "heading")
        self._line(f"Pages checked: {_number(result.get('pages'))} | "
                   f"Items found: {_number(result.get('items_seen'))}")
        self._line(f"Order: {_clean(result.get('sort'))} | "
                   f"Pass complete: {'yes' if result.get('complete') else 'no'}")
        if result.get("tags") or result.get("excluded_tags"):
            self._line("Required tags: " +
                       (", ".join(_clean(tag, 60) for tag in result.get("tags") or ()) or "any"))
            if result.get("excluded_tags"):
                self._line("Excluded tags: " +
                           ", ".join(_clean(tag, 60) for tag in result["excluded_tags"]))

    def _render_tags(self, result):
        self._line("Known Workshop tags", "heading")
        self._line(f"Tag details checked: {_number(result.get('items_checked'))} of "
                   f"{_number(result.get('known_items'))} known items")
        if result.get("tag_refresh"):
            self._line(f"Checked this run: {_number(result['tag_refresh'].get('checked'))}")
        tags = result.get("tags") or []
        if not tags:
            self._line("No saved tags match. Run 'tags --refresh' to fetch tags for known items.")
            return
        self._line(f"Showing {len(tags):,} of {_number(result.get('matching_tags'))} matching tags")
        for item in tags:
            count = item.get("items") or 0
            self._line(f"  {_clean(item.get('tag'), 75):<48} "
                       f"{_number(count):>8} {'item' if count == 1 else 'items'}")

    def _render_sizes(self, result):
        self._line("Workshop size lookup finished", "heading")
        self._line(f"Items checked: {_number(result.get('checked'))} | "
                   f"Sizes found: {_number(result.get('sizes_added'))} | "
                   f"Still unknown: {_number(result.get('unknown_sizes'))}")

    def _render_index(self, result):
        self._line("Vehicle indexing finished", "heading")
        self._line(f"Files checked: {_number(result.get('examined'))} | "
                   f"Added or updated: {_number(result.get('indexed_or_updated'))} | "
                   f"Already current: {_number(result.get('already_current'))}")
        self._line(f"Nonvehicle XML ignored: {_number(result.get('ignored_non_vehicle'))} | "
                   f"Errors: {_number(result.get('skipped_errors'))}")

    def _render_upgrade(self, result):
        self._line("Search index upgrade finished", "heading")
        self._line(f"Upgraded: {_number(result.get('upgraded'))} | "
                   f"Files unavailable: {_number(result.get('missing_files'))} | "
                   f"Errors: {_number(result.get('errors'))}")
        self._line(f"Files still using the old search index: "
                   f"{_number(result.get('remaining_legacy_files'))}")
