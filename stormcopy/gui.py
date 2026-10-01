"""Tkinter interface for local and shared Workshop comparisons.

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
from tkinter import filedialog, ttk
import webbrowser

from .bulk import existing_workshop_folder
from .catalog import CatalogRefresher, known_id_page
from .index import connect, index_directory, scan
from .remote_client import RemoteSearchError, scan_remote
from .result_tables import build_result_tables
from .search_index import SEARCH_INDEX_VERSION
from .workshop_metadata import get_workshop_description
from .visualizers import VehicleVisualizerPanel


_BG = "#FFFFFF"
_TEXT = "#1C1C1C"
_CERTAINTY_COLORS = {
    "high": "#B42318",
    "medium": "#A34D00",
    "holds some matched microcontrollers": "#8A6A00",
    "low": "#267239",
    "uncertain": "#8A6A00",
    "no match found": "#267239",
}
_CERTAINTY_ROW_COLORS = {
    "high": "#FDE8E7",
    "medium": "#FFF0DF",
    "holds some matched microcontrollers": "#FFF7D1",
    "low": "#E7F5EA",
    "uncertain": "#FFF7D1",
    "no match found": "#E7F5EA",
}


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
        self.scan_sequence = 0
        self.last_view_context = None
        self.coverage = None
        self.catalog_refresher = None
        self.catalog_start_job = None
        self.known_page_cursors = [""]
        self.known_page_index = 0
        self.known_request_id = 0
        self.known_has_more = False
        self.known_last_id = ""
        self.catalog_refresh_note = "Automatic Steam refresh starting..."
        self.preview_files = None
        self.preview_loaded = None
        self.preview_options = []
        self.preview_input_path = None
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
        root.geometry("1120x820")
        root.minsize(760, 540)
        root.configure(bg=_BG)
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
        self.known_catalog_var = tk.StringVar(value="Known Workshop IDs: opening index...")
        self.known_list_var = tk.StringVar(value="Loading saved IDs...")
        self.known_search_var = tk.StringVar()
        self.preview_match_var = tk.StringVar(value="No match selected")

        top = tk.Frame(root, bg=_BG)
        top.pack(fill="x", padx=8, pady=8)
        top.columnconfigure(1, weight=1)
        tk.Label(top, text="Vehicle XML:", bg=_BG).grid(row=0, column=0, sticky="w")
        tk.Entry(top, textvariable=self.xml_var).grid(
            row=0, column=1, sticky="ew", padx=5)
        tk.Button(top, text="Browse", command=self._browse_xml).grid(
            row=0, column=2, padx=(0, 5))
        tk.Button(top, text="Compare locally", command=self.compare).grid(row=0, column=3)

        tk.Label(top, text="Workshop folder:", bg=_BG).grid(
            row=1, column=0, sticky="w", pady=(6, 0))
        tk.Entry(top, textvariable=self.folder_var).grid(
            row=1, column=1, sticky="ew", padx=5, pady=(6, 0))
        tk.Button(top, text="Browse", command=self._browse_folder).grid(
            row=1, column=2, padx=(0, 5), pady=(6, 0))
        tk.Button(top, text="Index folder", command=self.index_workshop).grid(
            row=1, column=3, pady=(6, 0))

        tk.Label(top, text="Shared index URL:", bg=_BG).grid(
            row=2, column=0, sticky="w", pady=(6, 0))
        tk.Entry(top, textvariable=self.service_var).grid(
            row=2, column=1, sticky="ew", padx=5, pady=(6, 0))
        tk.Button(top, text="Search shared index", command=self.compare_remote).grid(
            row=2, column=3, pady=(6, 0))
        tk.Label(top, text="Access token:", bg=_BG).grid(
            row=3, column=0, sticky="w", pady=(6, 0))
        tk.Entry(top, textvariable=self.token_var, show="*").grid(
            row=3, column=1, sticky="ew", padx=5, pady=(6, 0))

        self.status_banner = tk.Label(root, textvariable=self.status_var,
                                      anchor="w", bg=_BG, fg=_TEXT,
                                      font=("Segoe UI", 10), padx=10, pady=6)
        self.status_banner.pack(fill="x", padx=8, pady=(0, 5))
        tk.Label(root, textvariable=self.coverage_var, anchor="w", bg=_BG).pack(
            fill="x", padx=8)
        tk.Label(root, textvariable=self.shared_coverage_var, anchor="w", bg=_BG).pack(
            fill="x", padx=8)
        tk.Label(root, textvariable=self.progress_var, anchor="w", bg=_BG).pack(
            fill="x", padx=8, pady=(0, 6))

        self._configure_result_style()
        notebook = ttk.Notebook(root)
        self.notebook = notebook
        notebook.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        results_tab = tk.Frame(notebook, bg=_BG)
        preview_tab = tk.Frame(notebook, bg=_BG)
        known_tab = tk.Frame(notebook, bg=_BG)
        debug_tab = tk.Frame(notebook, bg=_BG)
        notebook.add(results_tab, text="Comparison results")
        notebook.add(preview_tab, text="3D previews")
        notebook.add(known_tab, text="Known Workshop IDs")
        notebook.add(debug_tab, text="Debug resources")
        self.preview_tab = preview_tab
        self.known_tab = known_tab
        preview_controls = tk.Frame(preview_tab, bg=_BG)
        preview_controls.pack(fill="x", padx=10, pady=(7, 0))
        tk.Label(preview_controls, text="Workshop match:", bg=_BG).pack(side="left")
        self.preview_selector = ttk.Combobox(
            preview_controls, textvariable=self.preview_match_var,
            state="disabled", width=55)
        self.preview_selector.pack(side="left", fill="x", expand=True,
                                   padx=(7, 0))
        self.preview_selector.bind("<<ComboboxSelected>>", self._select_preview_match)
        self.visualizers = VehicleVisualizerPanel(preview_tab)
        self.visualizers.pack(fill="both", expand=True)
        self._make_known_tab(known_tab)
        notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        toolbar = tk.Frame(results_tab, bg=_BG)
        toolbar.pack(fill="x", padx=10, pady=(8, 4))
        self.certainty_banner = tk.Label(
            toolbar, text="Ready to compare", bg=_BG, fg=_TEXT,
            anchor="w", font=("Segoe UI", 10, "bold"), pady=5)
        self.certainty_banner.pack(side="left", fill="x", expand=True)
        self.open_match = tk.Button(
            toolbar, text="Open matched Workshop vehicle",
            command=self._open_match, state="disabled")
        self.open_match.pack(side="right", padx=(8, 0))

        self.result_frame = tk.Frame(results_tab, bg=_BG)
        self.results_canvas = tk.Canvas(self.result_frame, bg=_BG, highlightthickness=0)
        result_scroll = tk.Scrollbar(self.result_frame, command=self.results_canvas.yview)
        self.results_canvas.configure(yscrollcommand=result_scroll.set)
        self.results_canvas.pack(side="left", fill="both", expand=True)
        result_scroll.pack(side="right", fill="y")
        self.results_body = tk.Frame(self.results_canvas, bg=_BG)
        self.results_window = self.results_canvas.create_window(
            (0, 0), window=self.results_body, anchor="nw")
        self.results_body.bind("<Configure>", self._resize_results_scroll)
        self.results_canvas.bind("<Configure>", self._resize_results_width)
        self.results_canvas.bind("<MouseWheel>", self._scroll_results)

        self.summary_table = self._make_table(
            "Main match", ("Field", "Value"), (220, 760), height=1)
        self.candidates_table = self._make_table(
            "Possible matches", ("Rank", "Vehicle", "Certainty", "Input covered",
                                  "Matched data"),
            (70, 300, 190, 145, 320), height=1)
        self.candidates_table.bind("<Double-1>", self._open_candidate)
        self.candidate_links = {}
        self.description_box = self._make_description_box()
        self.channels_table = self._make_table(
            "Matched data by type", ("Data", "Shared", "Input covered"),
            (220, 120, 180), height=1)
        self.evidence_table = self._make_table(
            "Matching positions", ("Data", "Matching units", "Input position",
                                   "Workshop position", "Rarity"),
            (180, 125, 180, 190, 120), height=1)
        self.program_table = self._make_table(
            "Program and index information", ("Field", "Value"),
            (220, 760), height=1)

        tk.Label(debug_tab, textvariable=self.resources_var, anchor="w", bg=_BG,
                 justify="left").pack(fill="x", padx=8, pady=8)
        debug_frame = tk.Frame(debug_tab, bg=_BG)
        debug_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.debug = tk.Text(debug_frame, height=12, wrap="word", state="disabled")
        debug_scroll = tk.Scrollbar(debug_frame, command=self.debug.yview)
        self.debug.configure(yscrollcommand=debug_scroll.set)
        self.debug.pack(side="left", fill="both", expand=True)
        debug_scroll.pack(side="right", fill="y")
        self._log(f"Database: {self.db_path}")
        self._start_worker(self._bootstrap)
        self.root.after(100, self._pump)
        self.root.after(1000, self._refresh_resources)

    def _configure_result_style(self):
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Result.Treeview", background=_BG,
                        fieldbackground=_BG, foreground=_TEXT,
                        rowheight=25, borderwidth=1,
                        font=("Segoe UI", 10))
        style.configure("Result.Treeview.Heading", background=_BG,
                        foreground=_TEXT, relief="solid",
                        font=("Segoe UI", 10, "bold"))
        style.map("Result.Treeview",
                  background=[("selected", "#FFF7D1")],
                  foreground=[("selected", _TEXT)])
        style.map("Result.Treeview.Heading",
                  background=[("active", _BG)])
        style.configure("TNotebook.Tab", padding=(13, 7))

    def _make_known_tab(self, parent):
        tk.Label(parent, textvariable=self.known_catalog_var, bg=_BG,
                 anchor="w").pack(fill="x", padx=10, pady=(10, 3))
        controls = tk.Frame(parent, bg=_BG)
        controls.pack(fill="x", padx=10, pady=(0, 5))
        tk.Label(controls, text="Workshop ID:", bg=_BG).pack(side="left")
        entry = tk.Entry(controls, textvariable=self.known_search_var, width=24)
        entry.pack(side="left", padx=(6, 5))
        entry.bind("<Return>", lambda _event: self._find_known_id())
        tk.Button(controls, text="Find ID", command=self._find_known_id).pack(side="left")
        tk.Button(controls, text="Show list", command=self._reset_known_pages).pack(
            side="left", padx=(5, 0))
        self.find_ids_button = tk.Button(
            controls, text="Find IDs", command=self._toggle_find_ids)
        self.find_ids_button.pack(side="right")

        holder = tk.Frame(parent, bg=_BG)
        holder.pack(fill="both", expand=True, padx=10)
        columns = ("Workshop ID", "Title", "Indexed")
        self.known_table = ttk.Treeview(holder, columns=columns, show="headings",
                                        style="Result.Treeview", selectmode="browse")
        for column in columns:
            self.known_table.heading(column, text=column)
        self.known_table.column("Workshop ID", width=170, minwidth=130, stretch=False)
        self.known_table.column("Title", width=650, minwidth=180, stretch=True)
        self.known_table.column("Indexed", width=100, minwidth=80, stretch=False)
        scroll = tk.Scrollbar(holder, command=self.known_table.yview)
        self.known_table.configure(yscrollcommand=scroll.set)
        self.known_table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.known_table.bind("<Double-1>", self._open_known_id)

        footer = tk.Frame(parent, bg=_BG)
        footer.pack(fill="x", padx=10, pady=(5, 10))
        self.known_previous = tk.Button(footer, text="Previous", state="disabled",
                                        command=self._previous_known_page)
        self.known_previous.pack(side="left")
        self.known_next = tk.Button(footer, text="Next", state="disabled",
                                    command=self._next_known_page)
        self.known_next.pack(side="left", padx=(5, 10))
        tk.Label(footer, textvariable=self.known_list_var, bg=_BG,
                 anchor="w").pack(side="left", fill="x", expand=True)

    def _on_tab_changed(self, _event=None):
        selected = self.notebook.select()
        if selected == str(self.preview_tab):
            self._show_visualizers_if_needed()
        elif selected == str(self.known_tab) and self.known_request_id == 0:
            self._load_known_ids()

    def _start_catalog_refresher(self, find_all=False):
        self.catalog_start_job = None
        if self.closed:
            return
        if self.catalog_refresher is None:
            self.catalog_refresher = CatalogRefresher(
                self.db_path,
                on_update=lambda event: self._emit("catalog_update", event=event))
            if find_all:
                self.catalog_refresher.find_all()
            self.catalog_refresher.start()
        elif find_all:
            self.catalog_refresher.find_all()
            self.catalog_refresher.start()

    def _toggle_find_ids(self):
        if self.catalog_refresher is not None and self.catalog_refresher.is_finding:
            self.catalog_refresher.cancel_find()
            self.find_ids_button.configure(text="Find IDs")
            self.catalog_refresh_note = "ID search stopped. Its saved position can be resumed."
        else:
            if self.catalog_start_job is not None:
                self.root.after_cancel(self.catalog_start_job)
                self.catalog_start_job = None
            self._start_catalog_refresher(find_all=True)
            self.find_ids_button.configure(text="Stop finding IDs")
            self.catalog_refresh_note = "Finding public Workshop IDs; total pages unknown."
        if self.coverage is not None:
            self._show_coverage(self.coverage)
        else:
            self.known_catalog_var.set(self.catalog_refresh_note)

    def _show_visualizers_if_needed(self):
        if (self.preview_files is not None and
                self.preview_loaded != self.preview_files):
            self.visualizers.show_files(*self.preview_files)
            self.preview_loaded = self.preview_files

    def _set_preview_matches(self, input_path, result, source):
        self.preview_input_path = input_path
        self.preview_options = []
        seen = set()
        best = result.get("best_match") or {}
        for match in (best, *(result.get("matches") or [])):
            if not match:
                continue
            item_id = str(match.get("item_id") or "")
            if item_id in seen:
                continue
            seen.add(item_id)
            label = f"{match.get('title') or 'Workshop item'} (ID {item_id})"
            local_file = match.get("file") if source == "local" else None
            self.preview_options.append((label, local_file,
                                         match.get("evidence") or ()))
            if len(self.preview_options) == 4:
                break
        self.preview_selector.configure(
            values=[option[0] for option in self.preview_options],
            state="readonly" if self.preview_options else "disabled")
        if self.preview_options:
            self.preview_selector.current(0)
        else:
            self.preview_match_var.set("No matched vehicle")
        self._select_preview_match()

    def _select_preview_match(self, _event=None):
        selected = self.preview_selector.current()
        selected_match = (self.preview_options[selected]
                          if 0 <= selected < len(self.preview_options) else None)
        local_file = selected_match[1] if selected_match else None
        evidence = selected_match[2] if selected_match else ()
        self.preview_files = (self.preview_input_path, local_file, evidence)
        self.preview_loaded = None
        if self.notebook.select() == str(self.preview_tab):
            self._show_visualizers_if_needed()

    def _load_known_ids(self, exact_id=None):
        if self.closed:
            return
        self.known_request_id += 1
        request_id = self.known_request_id
        after_id = self.known_page_cursors[self.known_page_index]
        self.known_list_var.set("Loading saved Workshop IDs...")
        self._start_worker(self._known_ids_worker, request_id, after_id, exact_id)

    def _known_ids_worker(self, request_id, after_id, exact_id):
        try:
            db = _read_only_db(self.db_path)
            try:
                if exact_id is None:
                    page = known_id_page(db, after_id=after_id, limit=101)
                    has_more = len(page) > 100
                    page = page[:100]
                else:
                    row = db.execute(
                        "SELECT id,title,indexed FROM items WHERE id=?",
                        (exact_id,)).fetchone()
                    page = ([{"id": row[0], "title": row[1],
                              "indexed": bool(row[2])}] if row else [])
                    has_more = False
            finally:
                db.close()
            self._emit("known_ids_loaded", request_id=request_id, rows=page,
                       has_more=has_more, exact_id=exact_id)
        except (OSError, sqlite3.Error, ValueError) as exc:
            self._emit("known_ids_error", request_id=request_id, message=str(exc))

    def _find_known_id(self):
        item_id = self.known_search_var.get().strip()
        if not item_id.isdigit():
            self.known_list_var.set("Enter a numeric Workshop ID.")
            return
        self._load_known_ids(exact_id=item_id)

    def _reset_known_pages(self):
        self.known_search_var.set("")
        self.known_page_cursors = [""]
        self.known_page_index = 0
        self._load_known_ids()

    def _next_known_page(self):
        if not self.known_has_more or not self.known_last_id:
            return
        self.known_page_cursors = self.known_page_cursors[:self.known_page_index + 1]
        self.known_page_cursors.append(self.known_last_id)
        self.known_page_index += 1
        self._load_known_ids()

    def _previous_known_page(self):
        if self.known_page_index > 0:
            self.known_page_index -= 1
            self._load_known_ids()

    def _open_known_id(self, _event):
        selected = self.known_table.focus()
        if selected:
            item_id = self.known_table.item(selected, "values")[0]
            if item_id.isdigit():
                webbrowser.open(
                    f"https://steamcommunity.com/sharedfiles/filedetails/?id={item_id}")

    def _make_table(self, title, columns, widths, height):
        section = tk.Frame(self.results_body, bg=_BG, padx=8, pady=7)
        section.pack(fill="x", padx=10, pady=(4, 5))
        tk.Label(section, text=title, bg=_BG, fg=_TEXT,
                 font=("Segoe UI", 11, "bold"), anchor="w").pack(fill="x", pady=(0, 5))
        holder = tk.Frame(section, bg=_BG)
        holder.pack(fill="x")
        tree = ttk.Treeview(holder, columns=columns, show="headings",
                            height=height, style="Result.Treeview",
                            selectmode="browse")
        for column, width in zip(columns, widths):
            tree.heading(column, text=column)
            tree.column(column, width=width, minwidth=min(90, width),
                        stretch=column == columns[-1], anchor="w")
        for name, color in _CERTAINTY_ROW_COLORS.items():
            tree.tag_configure(name, background=color, foreground=_TEXT)
        tree.pack(side="left", fill="x", expand=True)
        if len(columns) > 3:
            horizontal = tk.Scrollbar(section, orient="horizontal", command=tree.xview)
            tree.configure(xscrollcommand=horizontal.set)
            horizontal.pack(fill="x")
        return tree

    def _make_description_box(self):
        section = tk.Frame(self.results_body, bg=_BG, padx=8, pady=7)
        section.pack(fill="x", padx=10, pady=(4, 5))
        tk.Label(section, text="Matched vehicle description", bg=_BG,
                 fg=_TEXT, font=("Segoe UI", 11, "bold"),
                 anchor="w").pack(fill="x", pady=(0, 5))
        holder = tk.Frame(section, bg=_BG)
        holder.pack(fill="x")
        box = tk.Text(holder, height=3, wrap="word", bg=_BG,
                      fg=_TEXT, insertbackground=_TEXT, relief="solid",
                      borderwidth=1, selectbackground="#FFF7D1",
                      font=("Segoe UI", 10), state="disabled")
        scroll = tk.Scrollbar(holder, command=box.yview)
        box.configure(yscrollcommand=scroll.set)
        box.pack(side="left", fill="x", expand=True)
        scroll.pack(side="right", fill="y")
        return box

    @staticmethod
    def _replace_rows(tree, rows, tags=None):
        tree.delete(*tree.get_children())
        rows = list(rows)
        tree.configure(height=max(1, len(rows)))
        tags = tags or ()
        for index, values in enumerate(rows):
            row_tag = tags[index] if index < len(tags) else None
            tree.insert("", "end", values=values,
                        tags=(row_tag,) if row_tag else ())

    def _resize_results_scroll(self, _event):
        self.results_canvas.configure(scrollregion=self.results_canvas.bbox("all"))

    def _resize_results_width(self, event):
        self.results_canvas.itemconfigure(self.results_window, width=event.width)

    def _scroll_results(self, event):
        if event.delta:
            self.results_canvas.yview_scroll(-int(event.delta / 120), "units")
        return "break"

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
        self._request_scan(path, "shared", endpoint,
                           self.token_var.get().strip() or None)

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
        self.scan_sequence += 1
        scan_id = self.scan_sequence
        scope = "shared index" if source == "shared" else "current local index"
        self.status_var.set(f"Comparing with the {scope}...")
        self._log(f"Comparing {path.name} with the {scope}")
        self._start_worker(self._scan_worker, path, source, endpoint, token, scan_id)

    def _scan_worker(self, path, source, endpoint, token, scan_id):
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
                       source=source, endpoint=endpoint,
                       memory_bytes=_working_set_bytes(), scan_id=scan_id)
            best = result.get("best_match") or {}
            if str(best.get("item_id", "")).isdigit():
                description = get_workshop_description(best["item_id"])
                self._emit("description_done", scan_id=scan_id,
                           description=description)
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
        self.known_catalog_var.set(
            f"{counts['known_items']:,} saved Workshop IDs; "
            f"{counts['indexed_items']:,} have indexed vehicles. "
            f"{self.catalog_refresh_note}")

    def _show_result(self, path, result, seconds, source="local", endpoint=None,
                     memory_bytes=None, scan_id=None):
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
        best = result.get("best_match") or {}
        pending_description = bool(str(best.get("item_id", "")).isdigit())
        self.last_view_context = {
            "path": path, "source": source, "endpoint": endpoint,
            "seconds": seconds, "memory_bytes": memory_bytes,
            "scan_id": scan_id,
            "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "app_uptime_seconds": time.monotonic() - self.started,
            "description": ("Loading public Workshop description..."
                            if pending_description else None),
        }
        self._render_report(reset_scroll=True)
        self._set_preview_matches(path, result, source)
        searched = coverage.get("searched_files", coverage.get("indexed_files", 0))
        scope = "shared" if source == "shared" else "local"
        elapsed_text = f"{seconds:.2f}" if seconds < 1 else f"{seconds:.1f}"
        self.status_var.set(
            f"Comparison finished in {elapsed_text}s against "
            f"{searched:,} {scope} indexed XML files."
            + (" Background indexing continues."
               if source == "local" and self.index_active else ""))
        self._log(
            f"Comparison finished in {seconds:.1f}s; "
            f"{searched:,} {scope} indexed XML files; "
            f"{len(result.get('matches') or []):,} reported matches")

    def _render_report(self, reset_scroll=False):
        result = self.last_result
        context = self.last_view_context
        if result is None or context is None:
            return
        report = build_result_tables(
            result, xml_path=context["path"], seconds=context["seconds"],
            memory_bytes=context["memory_bytes"], source=context["source"],
            endpoint=context["endpoint"], description=context["description"],
            scanned_at=context["scanned_at"],
            app_uptime_seconds=context["app_uptime_seconds"])
        certainty = report["certainty"]
        best = result.get("best_match") or {}
        color = _CERTAINTY_COLORS.get(certainty.casefold(), _TEXT)
        self.certainty_banner.configure(
            text=f"Certainty: {certainty.upper()}",
            fg=color)
        if not self.result_frame.winfo_manager():
            self.result_frame.pack(fill="both", expand=True)

        summary_rows = []
        summary_tags = []
        for field, value in report["summary"]:
            if field == "Result status":
                continue
            if field == "Matched vehicle description" and len(value) > 180:
                value = value[:177].rstrip() + "..."
            summary_rows.append((field, value))
            summary_tags.append(
                certainty.casefold() if field == "Certainty level" else None)
            if field == "Matched vehicle" and "listed_in_known_ids" in best:
                summary_rows.append(
                    ("ID in searched catalog",
                     "Yes" if best["listed_in_known_ids"] else "No"))
                summary_tags.append(None)
        self._replace_rows(self.summary_table, summary_rows, summary_tags)

        description = context["description"]
        if description is None:
            description = ("No public Workshop description is available for this match."
                           if result.get("best_match") else
                           "No matched vehicle to describe.")
        self._set_text(self.description_box, description)

        self._replace_rows(
            self.channels_table,
            [(row["channel"], row["shared"], row["input_coverage"])
             for row in report["channels"]])
        self._replace_rows(
            self.evidence_table,
            [(row["channel"], row["matches"], row["input_position"],
              row["workshop_position"], row["rarity"])
             for row in report["evidence"]])

        self.candidates_table.delete(*self.candidates_table.get_children())
        self.candidate_links = {}
        for row in report["candidates"]:
            label = f"{row['title']} (ID {row['item_id']})"
            item = self.candidates_table.insert(
                "", "end",
                values=(row["rank"], label, row["certainty"],
                        row["percent"], row["matched_data"]),
                tags=(row["certainty"].casefold(),))
            if row["url"].startswith("https://steamcommunity.com/sharedfiles/"):
                self.candidate_links[item] = row["url"]
        for rank in range(len(report["candidates"]) + 2, 5):
            self.candidates_table.insert(
                "", "end",
                values=(rank, "No further indexed match", "—", "—", "—"))
        self.candidates_table.configure(
            height=max(1, len(self.candidates_table.get_children())))
        self._replace_rows(self.program_table, report["program"])
        self.open_match.configure(
            state="normal" if best.get("url") else "disabled")
        if reset_scroll:
            self.results_canvas.yview_moveto(0)

    def _open_candidate(self, _event):
        item = self.candidates_table.focus()
        url = self.candidate_links.get(item)
        if url:
            webbrowser.open(url)

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
                self._reset_known_pages()
                if self.catalog_start_job is None and self.catalog_refresher is None:
                    self.catalog_start_job = self.root.after(
                        15000, self._start_catalog_refresher)
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
                    if (self.last_scan_source == "local" and
                            self.last_scan_path is not None and
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
                if (self.last_scan_source == "local" and
                        self.last_scan_path is not None and changed_index):
                    self._request_scan(self.last_scan_path, automatic=True)
                elif (self.last_scan_source == "local" and
                      self.last_scan_path is not None and not self.scan_active):
                    self.status_var.set("Workshop index is current; the comparison is up to date.")
                elif self.last_scan_path is None:
                    self.status_var.set("Index ready. Choose a vehicle XML to compare.")
            elif kind == "scan_done":
                self.scan_active = False
                path = Path(data["path"])
                if (self.last_scan_path == path and
                        self.last_scan_source == data["source"]):
                    self._show_result(path, data["result"], data["seconds"],
                                      source=data["source"], endpoint=data["endpoint"],
                                      memory_bytes=data["memory_bytes"],
                                      scan_id=data["scan_id"])
                if self.queued_scan is not None:
                    queued, self.queued_scan = self.queued_scan, None
                    self._request_scan(*queued, automatic=True)
            elif kind == "description_done":
                if (self.last_view_context is not None and
                        self.last_view_context["scan_id"] == data["scan_id"] and
                        self.scan_sequence == data["scan_id"]):
                    self.last_view_context["description"] = (
                        data["description"] or "Description unavailable from Steam.")
                    self._render_report()
            elif kind == "catalog_update":
                event = data["event"]
                state = event["state"]
                manual = event.get("manual", False)
                if event.get("manual_cancelled"):
                    if state == "updated" and self.coverage is not None:
                        previous = self.coverage["known_items"]
                        counts = dict(self.coverage)
                        counts["known_items"] = event["known_items"]
                        self._show_coverage(counts)
                        if (event["known_items"] != previous and
                                self.notebook.select() == str(self.known_tab)):
                            self._load_known_ids()
                    continue
                if manual:
                    self.find_ids_button.configure(
                        text="Find IDs" if event["manual_complete"] or
                        state != "updated" else "Stop finding IDs")
                if state == "updated":
                    interval = event["next_check_seconds"]
                    delay = (f"{interval // 3600} h" if interval >= 3600 else
                             f"{interval // 60} min" if interval >= 60 else
                             f"{interval} s")
                    if manual:
                        pages = event["manual_pages"]
                        checked = event.get("manual_items_seen", 0)
                        if event["manual_complete"]:
                            self.catalog_refresh_note = (
                                f"ID search finished after {pages:,} pages "
                                f"and {checked:,} items checked; "
                                f"{event['known_items']:,} IDs saved.")
                        else:
                            self.catalog_refresh_note = (
                                f"Finding IDs: {pages:,} pages, "
                                f"{checked:,} items checked; "
                                f"{event['known_items']:,} IDs saved. "
                                "You can stop and resume.")
                    elif (self.catalog_refresher is None or
                          not self.catalog_refresher.is_finding):
                        self.catalog_refresh_note = f"Next Steam check in {delay}."
                    if self.coverage is not None:
                        previous = self.coverage["known_items"]
                        counts = dict(self.coverage)
                        counts["known_items"] = event["known_items"]
                        self._show_coverage(counts)
                        if (event["known_items"] != previous and
                                self.notebook.select() == str(self.known_tab)):
                            self._load_known_ids()
                elif state == "no_key":
                    self.catalog_refresh_note = (
                        "Save a Steam API key, then click Find IDs."
                        if manual else "Save a Steam API key for automatic updates.")
                    if self.coverage is not None:
                        self._show_coverage(self.coverage)
                else:
                    self.catalog_refresh_note = (
                        "ID search paused after a Steam error; click Find IDs to retry."
                        if manual else "Steam update paused; retrying later.")
                    if self.coverage is not None:
                        self._show_coverage(self.coverage)
                    self._log("Known Workshop ID refresh paused: " +
                              event.get("message", "unknown error"))
            elif kind == "known_ids_loaded":
                if data["request_id"] != self.known_request_id:
                    continue
                rows = data["rows"]
                self.known_table.delete(*self.known_table.get_children())
                for row in rows:
                    self.known_table.insert(
                        "", "end",
                        values=(row["id"], row["title"] or "Untitled",
                                "Yes" if row["indexed"] else "No"))
                self.known_has_more = data["has_more"]
                self.known_last_id = rows[-1]["id"] if rows else ""
                exact_id = data["exact_id"]
                self.known_previous.configure(
                    state="normal" if exact_id is None and self.known_page_index > 0
                    else "disabled")
                self.known_next.configure(
                    state="normal" if exact_id is None and self.known_has_more
                    else "disabled")
                self.known_list_var.set(
                    (f"ID {exact_id} is not in the saved catalog." if not rows else
                     f"ID {exact_id} is in the saved catalog." if exact_id else
                     f"Page {self.known_page_index + 1}; {len(rows)} IDs shown in ID order."))
            elif kind == "known_ids_error":
                if data["request_id"] == self.known_request_id:
                    self.known_list_var.set(
                        "Could not read the saved ID list: " + data["message"])
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
                        self._request_scan(*queued, automatic=True)
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
        if self.catalog_start_job is not None:
            self.root.after_cancel(self.catalog_start_job)
            self.catalog_start_job = None
        if self.catalog_refresher is not None:
            self.catalog_refresher.stop()
        self.visualizers.close()
        self.root.destroy()


def launch(db_path="workshop.sqlite", workshop_folder=None):
    """Start the desktop UI; call from the CLI's main thread."""
    root = tk.Tk()
    DetectorApp(root, db_path=db_path, workshop_folder=workshop_folder)
    root.mainloop()
