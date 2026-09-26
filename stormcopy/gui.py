"""Plain Tkinter interface for local and shared Workshop comparisons.

The interface reads an existing search index immediately. Installed Workshop
XML files are indexed in a worker so a large folder never freezes Tk.
"""

import ctypes
import os
from pathlib import Path
import queue
import shutil
import sqlite3
import threading
import time
import tkinter as tk
from tkinter import filedialog
import webbrowser

from .bulk import existing_workshop_folder
from .index import connect, index_directory, scan
from .remote_client import RemoteSearchError, scan_remote
from .search_index import SEARCH_INDEX_VERSION


def _read_only_db(path):
    """Open a separate connection for searches without running schema setup."""
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    return db


def _coverage(db):
    return {
        "indexed_files": db.execute("SELECT COUNT(*) FROM files").fetchone()[0],
        "indexed_items": db.execute(
            "SELECT COUNT(DISTINCT item_id) FROM files").fetchone()[0],
        "known_items": db.execute("SELECT COUNT(*) FROM items").fetchone()[0],
        "search_ready": db.execute(
            "SELECT COUNT(*) FROM search_files WHERE version=?",
            (SEARCH_INDEX_VERSION,)).fetchone()[0],
    }


def _working_set_bytes():
    if os.name != "nt":
        return None
    try:
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        get_memory = psapi.GetProcessMemoryInfo
        get_memory.argtypes = (wintypes.HANDLE,
                               ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD)
        get_memory.restype = wintypes.BOOL
        if get_memory(kernel32.GetCurrentProcess(),
                      ctypes.byref(counters), counters.cb):
            return counters.WorkingSetSize
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    return None


def _mb(value):
    return f"{value / 1048576:,.1f} MB"


def _position(value):
    if value is None:
        return "unknown"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(part) for part in value)
    return str(value)


class DetectorApp:
    def __init__(self, root, db_path="workshop.sqlite", workshop_folder=None):
        self.root = root
        self.db_path = Path(db_path).expanduser().resolve()
        self.initial_workshop_folder = workshop_folder
        self.events = queue.SimpleQueue()
        self.index_active = False
        self.scan_active = False
        self.queued_scan = None
        self.last_result = None
        self.last_scan_path = None
        self.last_scan_source = None
        self.coverage = None
        self.index_progress = "Idle"
        self.index_done = 0
        self.index_warnings = 0
        self.last_progress_scan_done = 0
        self.last_progress_scan_time = 0.0
        self.started = time.monotonic()
        self.previous_cpu = time.process_time()
        self.previous_wall = time.monotonic()
        self.closed = False

        root.title("Stormworks Copy Detector")
        root.geometry("900x750")
        root.minsize(650, 500)
        root.protocol("WM_DELETE_WINDOW", self._close)

        self.xml_var = tk.StringVar()
        self.folder_var = tk.StringVar()
        self.service_var = tk.StringVar(value=os.environ.get("STORMCOPY_SEARCH_URL", ""))
        self.token_var = tk.StringVar(value=os.environ.get("STORMCOPY_SEARCH_TOKEN", ""))
        self.status_var = tk.StringVar(value="Opening index...")
        self.coverage_var = tk.StringVar(value="Index coverage: checking...")
        self.shared_coverage_var = tk.StringVar(value="Shared index: enter a service URL to search it")
        self.progress_var = tk.StringVar(value="Indexing: idle")
        self.resources_var = tk.StringVar(value="Loading resource information...")

        top = tk.Frame(root)
        top.pack(fill="x", padx=8, pady=8)
        top.columnconfigure(1, weight=1)
        tk.Label(top, text="Vehicle XML:").grid(row=0, column=0, sticky="w")
        tk.Entry(top, textvariable=self.xml_var).grid(
            row=0, column=1, sticky="ew", padx=5)
        tk.Button(top, text="Browse", command=self._browse_xml).grid(
            row=0, column=2, padx=(0, 5))
        tk.Button(top, text="Compare locally", command=self.compare).grid(row=0, column=3)

        tk.Label(top, text="Workshop folder:").grid(
            row=1, column=0, sticky="w", pady=(6, 0))
        tk.Entry(top, textvariable=self.folder_var).grid(
            row=1, column=1, sticky="ew", padx=5, pady=(6, 0))
        tk.Button(top, text="Browse", command=self._browse_folder).grid(
            row=1, column=2, padx=(0, 5), pady=(6, 0))
        tk.Button(top, text="Index folder", command=self.index_workshop).grid(
            row=1, column=3, pady=(6, 0))

        tk.Label(top, text="Shared index URL:").grid(
            row=2, column=0, sticky="w", pady=(6, 0))
        tk.Entry(top, textvariable=self.service_var).grid(
            row=2, column=1, sticky="ew", padx=5, pady=(6, 0))
        tk.Button(top, text="Search shared index", command=self.compare_remote).grid(
            row=2, column=3, pady=(6, 0))
        tk.Label(top, text="Access token:").grid(
            row=3, column=0, sticky="w", pady=(6, 0))
        tk.Entry(top, textvariable=self.token_var, show="*").grid(
            row=3, column=1, sticky="ew", padx=5, pady=(6, 0))

        tk.Label(root, textvariable=self.status_var, anchor="w").pack(
            fill="x", padx=8)
        tk.Label(root, textvariable=self.coverage_var, anchor="w").pack(
            fill="x", padx=8)
        tk.Label(root, textvariable=self.shared_coverage_var, anchor="w").pack(
            fill="x", padx=8)
        tk.Label(root, textvariable=self.progress_var, anchor="w").pack(
            fill="x", padx=8, pady=(0, 6))

        tk.Label(root, text="Comparison results", anchor="w").pack(
            fill="x", padx=8)
        results_frame = tk.Frame(root)
        results_frame.pack(fill="both", expand=True, padx=8)
        self.results = tk.Text(results_frame, height=18, wrap="word", state="disabled")
        results_scroll = tk.Scrollbar(results_frame, command=self.results.yview)
        self.results.configure(yscrollcommand=results_scroll.set)
        self.results.pack(side="left", fill="both", expand=True)
        results_scroll.pack(side="right", fill="y")

        self.open_match = tk.Button(root, text="Open best Workshop item",
                                    command=self._open_match, state="disabled")
        self.open_match.pack(anchor="w", padx=8, pady=(5, 8))

        tk.Label(root, text="Debug resources", anchor="w").pack(fill="x", padx=8)
        tk.Label(root, textvariable=self.resources_var, anchor="w",
                 justify="left").pack(fill="x", padx=8)
        debug_frame = tk.Frame(root)
        debug_frame.pack(fill="both", expand=True, padx=8, pady=(5, 8))
        self.debug = tk.Text(debug_frame, height=8, wrap="word", state="disabled")
        debug_scroll = tk.Scrollbar(debug_frame, command=self.debug.yview)
        self.debug.configure(yscrollcommand=debug_scroll.set)
        self.debug.pack(side="left", fill="both", expand=True)
        debug_scroll.pack(side="right", fill="y")

        self._set_text(self.results,
                       "Choose a Stormworks vehicle XML and select Compare.\n"
                       "Compare locally using the indexed files on this computer. "
                       "Installed Workshop files are indexed in the background.\n"
                       "To search an index hosted elsewhere, enter its URL and select "
                       "Search shared index. The XML stays on this computer; structural "
                       "fingerprints are sent to that service.\n"
                       "Only items present in the selected index can be checked.")
        self._log(f"Database: {self.db_path}")
        self._start_worker(self._bootstrap)
        self.root.after(100, self._pump)
        self.root.after(1000, self._refresh_resources)

    @staticmethod
    def _set_text(widget, value):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    def _log(self, message):
        timestamp = time.strftime("%H:%M:%S")
        self.debug.configure(state="normal")
        self.debug.insert("end", f"{timestamp}  {message}\n")
        if int(self.debug.index("end-1c").split(".")[0]) > 600:
            self.debug.delete("1.0", "101.0")
        self.debug.see("end")
        self.debug.configure(state="disabled")

    def _emit(self, kind, **values):
        self.events.put((kind, values))

    @staticmethod
    def _start_worker(function, *args):
        threading.Thread(target=function, args=args, daemon=True).start()

    def _bootstrap(self):
        try:
            db = connect(self.db_path)
            try:
                coverage = _coverage(db)
                try:
                    folder = existing_workshop_folder(
                        db, self.initial_workshop_folder)
                except ValueError as exc:
                    folder = None
                    if self.initial_workshop_folder is not None:
                        self._emit("log", message=str(exc))
            finally:
                db.close()
            self._emit("bootstrap_done", coverage=coverage,
                       folder=str(folder) if folder else None)
        except (OSError, sqlite3.Error, ValueError) as exc:
            self._emit("error", operation="Opening index", message=str(exc))

    def _browse_xml(self):
        value = filedialog.askopenfilename(
            title="Choose Stormworks vehicle XML",
            filetypes=[("XML files", "*.xml"), ("All files", "*")])
        if value:
            self.xml_var.set(value)

    def _browse_folder(self):
        value = filedialog.askdirectory(title="Choose Workshop XML folder")
        if value:
            self.folder_var.set(value)

    def index_workshop(self):
        folder = Path(self.folder_var.get().strip().strip('"')).expanduser()
        if not folder.is_dir():
            self.status_var.set("Choose an existing Workshop folder first.")
            return
        if self.index_active:
            self.status_var.set("Workshop indexing is already running.")
            return
        self.index_active = True
        self.index_done = 0
        self.index_warnings = 0
        self.last_progress_scan_done = 0
        self.last_progress_scan_time = time.monotonic()
        self.progress_var.set("Indexing: finding local XML files...")
        self._log(f"Started indexing local files in {folder.resolve()}")
        self._start_worker(self._index_worker, folder)

    def _index_worker(self, folder):
        started = time.monotonic()
        try:
            db = connect(self.db_path)
            try:
                result = index_directory(
                    db, folder,
                    progress=lambda event: self._emit("index_progress", event=event))
                coverage = _coverage(db)
            finally:
                db.close()
            self._emit("index_done", result=result, coverage=coverage,
                       seconds=time.monotonic() - started)
        except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
            self._emit("error", operation="Indexing Workshop", message=str(exc))

    def compare(self):
        text = self.xml_var.get().strip().strip('"')
        path = Path(text).expanduser() if text else None
        if path is None or not path.is_file():
            self.status_var.set("Choose an existing vehicle XML file first.")
            return
        self._request_scan(path, "local")

    def compare_remote(self):
        text = self.xml_var.get().strip().strip('"')
        path = Path(text).expanduser() if text else None
        if path is None or not path.is_file():
            self.status_var.set("Choose an existing vehicle XML file first.")
            return
        endpoint = self.service_var.get().strip().rstrip("/")
        if not endpoint:
            self.status_var.set("Enter the shared search service URL first.")
            return
        self._request_scan(path, "shared", endpoint, self.token_var.get().strip())

    def _request_scan(self, path, source="local", endpoint=None, token=None,
                      automatic=False):
        if self.scan_active:
            self.queued_scan = (path, source, endpoint, token)
            if not automatic:
                self.status_var.set("Comparison queued after the current scan.")
            return
        self.scan_active = True
        self.last_scan_path = path
        self.last_scan_source = source
        scope = "shared index" if source == "shared" else "current local index"
        self.status_var.set(f"Comparing with the {scope}...")
        self._log(f"Comparing {path.name} with the {scope}" +
                  (f" at {endpoint}" if endpoint else ""))
        self._start_worker(self._scan_worker, path, source, endpoint, token)

    def _scan_worker(self, path, source, endpoint, token):
        started = time.monotonic()
        try:
            if source == "shared":
                result = scan_remote(path, endpoint, token=token)
            else:
                db = _read_only_db(self.db_path)
                try:
                    result = scan(db, path, limit=5)
                finally:
                    db.close()
            self._emit("scan_done", path=str(path), result=result,
                       seconds=time.monotonic() - started,
                       source=source, endpoint=endpoint)
        except (OSError, sqlite3.Error, ValueError, RuntimeError,
                RemoteSearchError) as exc:
            self._emit("error", operation="Comparing XML", message=str(exc),
                       source=source)

    def _show_coverage(self, counts):
        self.coverage = counts
        self.coverage_var.set(
            f"Indexed locally: {counts['indexed_items']:,} Workshop items, "
            f"{counts['indexed_files']:,} vehicle XML files; "
            f"{counts['search_ready']:,} search-ready; "
            f"{counts['known_items']:,} known item IDs")

    def _show_result(self, path, result, seconds, source="local", endpoint=None):
        self.last_result = result
        coverage = result.get("coverage") or {}
        if coverage:
            counts = {
                "indexed_items": coverage.get("indexed_items", 0),
                "indexed_files": coverage.get("indexed_files", 0),
                "search_ready": coverage.get("modern_search_files", 0),
                "known_items": coverage.get("known_items", 0),
            }
            if source == "local":
                self._show_coverage(counts)
            else:
                self.shared_coverage_var.set(
                    f"Shared index: {counts['indexed_items']:,} Workshop items, "
                    f"{counts['indexed_files']:,} vehicle XML files, "
                    f"{counts['search_ready']:,} search-ready; "
                    f"{counts['known_items']:,} known item IDs")
        scope = "shared" if source == "shared" else "local"
        lines = [f"File: {path}",
                 f"Search index: {endpoint if source == 'shared' else 'this computer'}",
                 f"Result: {result.get('status', 'unknown')}",
                 f"Suspicion: {result.get('suspicion_level', 'unknown')}",
                 f"Compared with {coverage.get('indexed_files', 0):,} {scope} indexed "
                 "vehicle XML files.", ""]
        if result.get("status") == "no index":
            lines.append("No searchable Workshop vehicles are indexed yet. "
                         ("The shared service needs indexed Workshop files."
                          if source == "shared" else
                          "Index the local Workshop folder; the comparison will update "
                          "when it finishes."))
        else:
            best = result.get("best_match")
            if best:
                lines.extend([
                    f"Best Workshop item: {best.get('title') or 'Untitled'} "
                    f"(ID {best['item_id']})",
                    f"Vehicle structure covered: {best['similarity_percent']:.1f}%",
                    f"Workshop item covered: {best['workshop_coverage_percent']:.1f}%",
                    f"Multi-signal overlap: {best['combined_similarity_percent']:.1f}%",
                    f"Confidence: {best['confidence']} (heuristic)",
                    f"Shared structural neighborhoods: "
                    f"{best['shared_neighborhoods']:,}; "
                    f"rare: {best['rare_shared_neighborhoods']:,}",
                ])
                estimate = best.get("minhash_jaccard_estimate_percent")
                if estimate is not None:
                    lines.append(f"MinHash estimate: {estimate:.1f}%")
                if best.get("partial_copy_evidence"):
                    lines.append("Nearby rare structures indicate a matching section.")
                if best.get("semantic_copy_evidence"):
                    lines.append("Controller or logic content also matches.")
                if best.get("url"):
                    lines.append(f"Workshop page: {best['url']}")
                evidence = best.get("evidence") or []
                if evidence:
                    lines.extend(["", "Evidence (vehicle position -> Workshop position):"])
                    for item in evidence[:7]:
                        lines.append(
                            f"  {item.get('channel', 'geometry')}: "
                            f"{_position(item.get('query_position'))} -> "
                            f"{_position(item.get('workshop_position'))}; "
                            f"{item.get('matching_components', 0)} matching")
                others = result.get("matches") or []
                if len(others) > 1:
                    lines.extend(["", "Other matches:"])
                    for item in others[1:5]:
                        lines.append(
                            f"  {item.get('title') or item['item_id']}: "
                            f"{item['similarity_percent']:.1f}% structure; "
                            f"{item['confidence']} confidence")
            else:
                lines.append(f"No significant match was found in the {scope} index.")
            lines.extend(["", "This is a comparison with the selected index's "
                          "Workshop files. Missing or private Workshop items were not "
                          "checked. Similarity is evidence, not proof of copying."])
        self._set_text(self.results, "\n".join(lines))
        self.open_match.configure(
            state="normal" if result.get("best_match", {}) and
            result["best_match"].get("url") else "disabled")
        self.status_var.set(
            f"Comparison finished in {seconds:.1f}s against "
            f"{coverage.get('indexed_files', 0):,} indexed XML files."
            + (" Background indexing continues." if self.index_active else ""))
        self._log(
            f"Comparison finished in {seconds:.1f}s; "
            f"{coverage.get('indexed_files', 0):,} indexed XML files; "
            f"{len(result.get('matches') or []):,} reported matches")

    def _open_match(self):
        best = (self.last_result or {}).get("best_match") or {}
        if best.get("url"):
            webbrowser.open(best["url"])

    def _pump(self):
        if self.closed:
            return
        for _ in range(100):
            try:
                kind, data = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._log(data["message"])
            elif kind == "bootstrap_done":
                self._show_coverage(data["coverage"])
                self.status_var.set("Ready to compare with the current local index.")
                if data["folder"]:
                    self.folder_var.set(data["folder"])
                    self._log(f"Workshop folder found: {data['folder']}")
                    self.index_workshop()
                else:
                    self._log("No Steam Workshop folder found automatically; choose one to index")
            elif kind == "index_progress":
                event = data["event"]
                if event.get("warning"):
                    self.index_warnings += 1
                    if self.index_warnings <= 5 or self.index_warnings % 100 == 0:
                        self._log(f"Index warning {self.index_warnings}: "
                                  f"{event['warning']}")
                    elif self.index_warnings == 6:
                        self._log("Additional index warnings are summarized every 100 items")
                phase = event.get("phase", "Index XML")
                done = event.get("done", 0)
                total = event.get("total")
                detail = event.get("detail") or ""
                count = f"{done:,}/{total:,}" if total is not None else f"{done:,}"
                self.index_progress = f"{phase}: {count} {detail}".strip()
                self.progress_var.set("Indexing: " + self.index_progress)
                if phase == "Index XML":
                    self.index_done = done
                    now = time.monotonic()
                    if (self.last_scan_path is not None and
                            done - self.last_progress_scan_done >= 500 and
                            now - self.last_progress_scan_time >= 30):
                        self.last_progress_scan_done = done
                        self.last_progress_scan_time = now
                        self._request_scan(self.last_scan_path, automatic=True)
            elif kind == "index_done":
                self.index_active = False
                previous_files = ((self.coverage or {}).get("indexed_files"))
                self._show_coverage(data["coverage"])
                report = data["result"]
                self.progress_var.set(
                    f"Indexing complete: {report['examined']:,} XML files checked; "
                    f"{report['indexed_or_updated']:,} new or updated; "
                    f"{report['skipped_errors']:,} errors")
                self._log(
                    f"Index finished in {data['seconds']:.1f}s: "
                    f"{report['indexed_or_updated']:,} new or updated, "
                    f"{report['already_current']:,} already current, "
                    f"{self.index_warnings:,} warnings")
                changed_index = (report["indexed_or_updated"] > 0 or
                                 previous_files != data["coverage"]["indexed_files"])
                if self.last_scan_path is not None and changed_index:
                    self._request_scan(self.last_scan_path, automatic=True)
                elif self.last_scan_path is not None and not self.scan_active:
                    self.status_var.set("Workshop index is current; the comparison is up to date.")
                elif self.last_scan_path is None:
                    self.status_var.set("Index ready. Choose a vehicle XML to compare.")
            elif kind == "scan_done":
                self.scan_active = False
                path = Path(data["path"])
                if self.last_scan_path == path:
                    self._show_result(path, data["result"], data["seconds"])
                if self.queued_scan is not None:
                    queued, self.queued_scan = self.queued_scan, None
                    self._request_scan(queued, automatic=True)
            elif kind == "error":
                self._log(f"{data['operation']}: {data['message']}")
                self.status_var.set(f"{data['operation']} failed: {data['message']}")
                if data["operation"] == "Indexing Workshop":
                    self.index_active = False
                    self.progress_var.set("Indexing: stopped after an error")
                elif data["operation"] == "Comparing XML":
                    self.scan_active = False
                    if self.queued_scan is not None:
                        queued, self.queued_scan = self.queued_scan, None
                        self._request_scan(queued, automatic=True)
        self.root.after(100, self._pump)

    def _refresh_resources(self):
        if self.closed:
            return
        wall = time.monotonic()
        cpu = time.process_time()
        elapsed = max(wall - self.previous_wall, 0.001)
        cpu_percent = 100 * max(cpu - self.previous_cpu, 0) / elapsed
        self.previous_wall, self.previous_cpu = wall, cpu
        memory = _working_set_bytes()
        database_bytes = 0
        for path in (self.db_path, Path(str(self.db_path) + "-wal")):
            try:
                database_bytes += path.stat().st_size
            except OSError:
                pass
        try:
            free_bytes = shutil.disk_usage(self.db_path.parent).free
        except OSError:
            free_bytes = None
        lines = [
            f"Process CPU: {cpu_percent:.0f}% (one core = 100%)  |  "
            f"Memory: {_mb(memory) if memory is not None else 'unavailable'}  |  "
            f"Threads: {threading.active_count()}",
            f"Database + journal: {_mb(database_bytes)}  |  "
            f"Free disk: {_mb(free_bytes) if free_bytes is not None else 'unavailable'}  |  "
            f"Uptime: {wall - self.started:.0f}s",
            f"Index worker: {'running' if self.index_active else 'idle'}  |  "
            f"Comparison worker: {'running' if self.scan_active else 'idle'}  |  "
            f"Index XML examined: {self.index_done:,}",
        ]
        self.resources_var.set("\n".join(lines))
        self.root.after(1000, self._refresh_resources)

    def _close(self):
        self.closed = True
        self.root.destroy()


def launch(db_path="workshop.sqlite", workshop_folder=None):
    """Start the desktop UI; call from the CLI's main thread."""
    root = tk.Tk()
    DetectorApp(root, db_path=db_path, workshop_folder=workshop_folder)
    root.mainloop()
