#!/usr/bin/env python3
"""
Forge Card Downloader
Downloads MTG card images from Scryfall, organized for the Forge game client.
Pure Tkinter GUI — no extra GUI dependencies.
"""

import os
import re
import time
import json
import threading
import logging
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests

from forge_edition_parser import ForgeEditionParser
from set_code_mapper import SetCodeMapper

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("ForgeDownloader")

# ─── Constants ───────────────────────────────────────────────────────────────
SCRYFALL_API       = "https://api.scryfall.com"
SCRYFALL_SEARCH    = f"{SCRYFALL_API}/cards/search"
SCRYFALL_SETS      = f"{SCRYFALL_API}/sets"
SCRYFALL_NAMED     = f"{SCRYFALL_API}/cards/named"
REQUEST_DELAY      = 0.12          # 120 ms — comfortable margin over Scryfall's 100 ms minimum
IMAGE_FORMAT       = "large"
USER_AGENT         = "ForgeCardDownloader/2.0 (github.com/forge-card-downloader)"
MAX_WORKERS        = 6
MAX_RETRIES        = 4
RETRY_BASE_DELAY   = 2.0          # exponential backoff base in seconds

DEFAULT_FORGE_PICS = Path.home() / "AppData" / "Local" / "Forge" / "Cache" / "pics" / "cards"
DEFAULT_FORGE_RES  = Path.home() / "AppData" / "Roaming" / "Forge" / "res" / "editions"

# ─── Dark theme palette ──────────────────────────────────────────────────────
BG        = "#1e1e1e"
BG_ALT    = "#252526"
BG_INPUT  = "#2d2d30"
BG_HOVER  = "#333337"
FG        = "#cccccc"
FG_DIM    = "#808080"
FG_HEAD   = "#ffffff"
ACCENT    = "#569cd6"
ACCENT2   = "#4ec9b0"
RED       = "#f44747"
GREEN     = "#6a9955"
BORDER    = "#3c3c3c"


# ═════════════════════════════════════════════════════════════════════════════
#  SCRYFALL CLIENT  — rate limited, with retry + exponential backoff
# ═════════════════════════════════════════════════════════════════════════════

class ScryfallClient:
    def __init__(self):
        self._lock = threading.Lock()
        self._last_request = 0.0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    def _wait(self):
        with self._lock:
            elapsed = time.time() - self._last_request
            if elapsed < REQUEST_DELAY:
                time.sleep(REQUEST_DELAY - elapsed)
            self._last_request = time.time()

    # ── generic GET with retry ────────────────────────────────────────────
    def get(self, url: str, params: dict = None, retries: int = MAX_RETRIES) -> dict:
        for attempt in range(retries):
            self._wait()
            try:
                r = self.session.get(url, params=params, timeout=30)
                if r.status_code == 429:           # Too Many Requests
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    log.warning(f"429 Too Many Requests — backing off {delay:.1f}s")
                    time.sleep(delay)
                    continue
                r.raise_for_status()
                return r.json()
            except (requests.ConnectionError, requests.Timeout) as e:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                log.warning(f"Request failed (attempt {attempt+1}/{retries}): {e} — retry in {delay:.1f}s")
                time.sleep(delay)
        raise RuntimeError(f"Failed after {retries} retries: {url}")

    # ── image download with retry ─────────────────────────────────────────
    def download_image(self, url: str, retries: int = MAX_RETRIES) -> bytes:
        for attempt in range(retries):
            self._wait()
            try:
                r = self.session.get(url, timeout=60)
                if r.status_code == 429:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    log.warning(f"429 on image — backing off {delay:.1f}s")
                    time.sleep(delay)
                    continue
                r.raise_for_status()
                return r.content
            except (requests.ConnectionError, requests.Timeout) as e:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                log.warning(f"Image download failed (attempt {attempt+1}/{retries}): {e}")
                time.sleep(delay)
        raise RuntimeError(f"Image download failed after {retries} retries")

    # ── convenience ───────────────────────────────────────────────────────
    def get_all_sets(self) -> list[dict]:
        return self.get(SCRYFALL_SETS).get("data", [])

    def search_set(self, set_code: str) -> list[dict]:
        cards, url, params = [], SCRYFALL_SEARCH, {"q": f"set:{set_code}", "unique": "prints", "order": "set"}
        while url:
            data = self.get(url, params=params)
            cards.extend(data.get("data", []))
            url = data.get("next_page") if data.get("has_more") else None
            params = None
        return cards

    def search_card_name(self, name: str) -> dict:
        """Return the card via fuzzy search (single result)."""
        return self.get(SCRYFALL_NAMED, {"fuzzy": name})

    def search_all_prints(self, name: str) -> list[dict]:
        """Return ALL printings of a card across every set."""
        cards, url = [], SCRYFALL_SEARCH
        params = {"q": f'!"{name}" unique:prints', "order": "released"}
        while url:
            data = self.get(url, params=params)
            cards.extend(data.get("data", []))
            url = data.get("next_page") if data.get("has_more") else None
            params = None
        return cards


# ═════════════════════════════════════════════════════════════════════════════
#  DOWNLOAD LOGIC
# ═════════════════════════════════════════════════════════════════════════════

class DownloadTask:
    __slots__ = ("card_name", "set_code", "collector_number", "image_url",
                 "save_path", "status", "error_msg")

    def __init__(self, card_name, set_code, collector_number, image_url, save_path):
        self.card_name       = card_name
        self.set_code        = set_code
        self.collector_number = collector_number
        self.image_url       = image_url
        self.save_path       = Path(save_path)
        self.status          = "pending"
        self.error_msg       = ""


class DownloadManager:
    def __init__(self, client: ScryfallClient, max_workers: int = MAX_WORKERS):
        self.client      = client
        self.max_workers  = max_workers
        self._cancel      = threading.Event()
        self._running     = False

    @property
    def running(self): return self._running

    def cancel(self): self._cancel.set()

    def _do(self, task: DownloadTask) -> DownloadTask:
        if self._cancel.is_set():
            task.status = "cancelled"; return task
        if task.save_path.exists():
            task.status = "skipped"; return task
        try:
            task.status = "downloading"
            task.save_path.parent.mkdir(parents=True, exist_ok=True)
            data = self.client.download_image(task.image_url)
            task.save_path.write_bytes(data)
            task.status = "done"
        except Exception as e:
            task.status = "error"
            task.error_msg = str(e)
        return task

    def run(self, tasks, on_progress=None, on_log=None, on_done=None):
        self._cancel.clear()
        self._running = True

        def _work():
            done = skip = err = 0
            total = len(tasks)
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                futs = {pool.submit(self._do, t): t for t in tasks}
                for i, f in enumerate(as_completed(futs), 1):
                    if self._cancel.is_set(): break
                    t = f.result()
                    if t.status == "done":    done += 1
                    elif t.status == "skipped": skip += 1
                    elif t.status == "error": err += 1
                    if on_progress: on_progress(i, total, t)
                    if on_log and t.status == "error":
                        on_log(f"ERROR: {t.card_name} — {t.error_msg}")
            self._running = False
            if on_log:
                if self._cancel.is_set():
                    on_log(f"Cancelled. {done} downloaded, {skip} skipped, {err} errors.")
                else:
                    on_log(f"Done! {done} downloaded, {skip} skipped, {err} errors.")
            if on_done: on_done()

        threading.Thread(target=_work, daemon=True).start()


# ═════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═════════════════════════════════════════════════════════════════════════════

# Layouts where both face names are concatenated (single physical card)
_CONCAT_LAYOUTS = {"split", "fuse", "aftermath", "adventure", "flip"}
# Layouts where only the front face name is used (two physical faces / separate images)
_FRONT_ONLY_LAYOUTS = {"transform", "modal_dfc", "reversible_card", "double_faced_token", "art_series"}


def _clean_card_name(card_name: str, layout: str = "") -> str:
    """Convert Scryfall card name to Forge's filename convention.

    Split / Fuse / Aftermath / Adventure cards (single physical card):
        "Dusk // Dawn"        →  "DuskDawn"
        "Fire // Ice"         →  "FireIce"
        "Bonecrusher Giant // Stomp"  →  "Bonecrusher GiantStomp"

    Transform / MDFC cards (two physical faces):
        "Delver of Secrets // Insectile Aberration"  →  "Delver of Secrets"
        Uses only the front face name.

    If layout is unknown/empty, defaults to concatenation (safer — won't lose data).
    """
    if " // " in card_name:
        left, right = card_name.split(" // ", 1)
        if layout.lower() in _FRONT_ONLY_LAYOUTS:
            name = left
        else:
            # split, fuse, aftermath, adventure, flip, or unknown → concatenate
            name = left + right
    else:
        name = card_name
    return re.sub(r'[<>:"/\\|?*]', '', name)


def forge_filename(card_name: str, layout: str = "", art_index: int = None) -> str:
    """Build the filename Forge expects.
    First copy:  Card Name.fullborder.jpg
    Subsequent:  Card Name2.fullborder.jpg, Card Name3.fullborder.jpg, ...
    """
    name = _clean_card_name(card_name, layout)
    if art_index and art_index > 1:
        return f"{name}{art_index}.fullborder.jpg"
    return f"{name}.fullborder.jpg"


def image_url(card: dict) -> Optional[str]:
    for src in [card.get("image_uris"),
                (card.get("card_faces") or [{}])[0].get("image_uris")]:
        if src:
            return src.get(IMAGE_FORMAT) or src.get("large") or src.get("normal")
    return None


def build_tasks_for_set(cards: list[dict], set_code: str,
                        folder_path: Path) -> list[DownloadTask]:
    """Build download tasks for a list of cards, correctly numbering
    duplicate card names within the same set.

    Cards are sorted by collector_number so the numbering matches
    what Forge expects (lowest CN = no suffix, next = 2, etc.).
    """
    from collections import defaultdict

    # Group cards by cleaned name within this set
    groups: dict[str, list[dict]] = defaultdict(list)
    for c in cards:
        url = image_url(c)
        if not url:
            continue
        clean = _clean_card_name(c["name"], c.get("layout", ""))
        groups[clean].append(c)

    # Sort each group by collector_number (numeric when possible)
    def _cn_sort_key(card):
        cn = card.get("collector_number", "0")
        # Strip non-digit suffixes like "★" or "a"/"b"
        digits = re.sub(r"[^\d]", "", cn)
        return int(digits) if digits else 0

    tasks = []
    for clean_name, group in groups.items():
        group.sort(key=_cn_sort_key)
        for i, c in enumerate(group):
            url = image_url(c)
            art_index = (i + 1) if len(group) > 1 else None
            fname = forge_filename(c["name"], c.get("layout", ""), art_index=art_index)
            tasks.append(DownloadTask(
                card_name=c["name"],
                set_code=set_code,
                collector_number=c.get("collector_number", ""),
                image_url=url,
                save_path=folder_path / fname,
            ))
    return tasks


def build_tasks_multi_set(cards: list[dict],
                          folder_resolver) -> list[DownloadTask]:
    """Build tasks for cards spanning multiple sets (e.g. all prints of one card).
    folder_resolver(set_code) -> Path to the set's image folder.
    """
    from collections import defaultdict

    # Group by (set, cleaned_name)
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for c in cards:
        if not image_url(c):
            continue
        key = (c["set"], _clean_card_name(c["name"], c.get("layout", "")))
        groups[key].append(c)

    def _cn_sort_key(card):
        cn = card.get("collector_number", "0")
        digits = re.sub(r"[^\d]", "", cn)
        return int(digits) if digits else 0

    tasks = []
    for (sc, clean_name), group in groups.items():
        group.sort(key=_cn_sort_key)
        folder = folder_resolver(sc)
        for i, c in enumerate(group):
            url = image_url(c)
            art_index = (i + 1) if len(group) > 1 else None
            fname = forge_filename(c["name"], c.get("layout", ""), art_index=art_index)
            tasks.append(DownloadTask(
                card_name=c["name"],
                set_code=sc,
                collector_number=c.get("collector_number", ""),
                image_url=url,
                save_path=folder / fname,
            ))
    return tasks


# ═════════════════════════════════════════════════════════════════════════════
#  APPLICATION
# ═════════════════════════════════════════════════════════════════════════════

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Forge Card Downloader")
        self.geometry("1050x700")
        self.minsize(850, 550)
        self.configure(bg=BG)
        self.option_add("*Font", ("Segoe UI", 10))

        self.client   = ScryfallClient()
        self.dlm      = DownloadManager(self.client)
        self.mapper   = SetCodeMapper()
        self.all_sets  = []
        self.filtered  = []
        self._missing_tasks = []
        self._current_prints = []

        self.forge_pics_var = tk.StringVar(value=str(DEFAULT_FORGE_PICS))
        self.forge_res_var  = tk.StringVar(value=str(DEFAULT_FORGE_RES))
        self.workers_var    = tk.IntVar(value=MAX_WORKERS)

        self._apply_theme()
        self._build_ui()
        self._try_load_editions()
        self.after(300, self._fetch_sets)

    # ── Theme ─────────────────────────────────────────────────────────────
    def _apply_theme(self):
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure(".", background=BG, foreground=FG, fieldbackground=BG_INPUT,
                         borderwidth=0, relief="flat")
        style.configure("TFrame",      background=BG)
        style.configure("Alt.TFrame",  background=BG_ALT)
        style.configure("TLabel",      background=BG, foreground=FG)
        style.configure("Head.TLabel", background=BG, foreground=FG_HEAD,
                         font=("Segoe UI", 14, "bold"))
        style.configure("Dim.TLabel",  background=BG, foreground=FG_DIM)
        style.configure("TButton",     background=BG_INPUT, foreground=FG, padding=(12, 6))
        style.map("TButton",
                  background=[("active", BG_HOVER)],
                  foreground=[("active", FG_HEAD)])
        style.configure("Accent.TButton", background=ACCENT, foreground="#fff")
        style.map("Accent.TButton", background=[("active", "#4a88b8")])
        style.configure("Green.TButton", background=GREEN, foreground="#fff")
        style.map("Green.TButton", background=[("active", "#5a8845")])
        style.configure("Red.TButton", background=RED, foreground="#fff")
        style.map("Red.TButton", background=[("active", "#d63b3b")])
        style.configure("TEntry",      fieldbackground=BG_INPUT, foreground=FG,
                         insertcolor=FG)
        style.configure("TNotebook",   background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=BG_ALT, foreground=FG_DIM,
                         padding=(16, 8))
        style.map("TNotebook.Tab",
                  background=[("selected", BG)],
                  foreground=[("selected", ACCENT)])
        style.configure("Horizontal.TProgressbar",
                         troughcolor=BG_INPUT, background=ACCENT, thickness=10)
        style.configure("Treeview", background=BG_ALT, foreground=FG,
                         fieldbackground=BG_ALT, rowheight=24, borderwidth=0)
        style.configure("Treeview.Heading", background=BG_INPUT, foreground=FG_HEAD,
                         font=("Segoe UI", 9, "bold"))
        style.map("Treeview",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", "#fff")])
        style.configure("TCheckbutton", background=BG, foreground=FG)
        style.configure("TScale", background=BG, troughcolor=BG_INPUT)
        style.configure("TCombobox", fieldbackground=BG_INPUT, foreground=FG)

    # ── Build UI ──────────────────────────────────────────────────────────
    def _build_ui(self):
        # Tabs
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(8, 0))

        self._build_sets_tab()
        self._build_single_tab()
        self._build_scan_tab()
        self._build_settings_tab()

        # Bottom bar (always visible)
        self._build_bottom_bar()

    # ── BOTTOM BAR (progress) ─────────────────────────────────────────────
    def _build_bottom_bar(self):
        bar = ttk.Frame(self, style="Alt.TFrame")
        bar.pack(fill="x", side="bottom", padx=0, pady=0)

        # Row 0: current item label
        self.prog_item = ttk.Label(bar, text="Ready", style="Dim.TLabel")
        self.prog_item.pack(fill="x", padx=12, pady=(6, 0))

        # Row 1: progress bar + percentage + cancel
        row = ttk.Frame(bar, style="Alt.TFrame")
        row.pack(fill="x", padx=12, pady=4)

        self.prog_bar = ttk.Progressbar(row, mode="determinate", maximum=100)
        self.prog_bar.pack(side="left", fill="x", expand=True, pady=2)

        self.prog_pct = ttk.Label(row, text=" 0%", width=5, style="Dim.TLabel")
        self.prog_pct.pack(side="left", padx=(8, 4))

        self.cancel_btn = ttk.Button(row, text="Cancel", style="Red.TButton",
                                      command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=(4, 0))

        # Row 2: log
        self.log_text = tk.Text(bar, height=3, bg=BG_INPUT, fg=FG_DIM,
                                 font=("Consolas", 9), bd=0, highlightthickness=0,
                                 insertbackground=FG)
        self.log_text.pack(fill="x", padx=12, pady=(0, 8))

    # ── SETS TAB ──────────────────────────────────────────────────────────
    def _build_sets_tab(self):
        f = ttk.Frame(self.nb)
        self.nb.add(f, text="  Sets  ")

        # toolbar
        tb = ttk.Frame(f)
        tb.pack(fill="x", pady=(8, 4), padx=8)

        ttk.Label(tb, text="Download Full Sets", style="Head.TLabel").pack(side="left")

        self.set_search_var = tk.StringVar()
        self.set_search_var.trace_add("write", lambda *_: self._filter_sets())
        e = ttk.Entry(tb, textvariable=self.set_search_var, width=28)
        e.pack(side="right", padx=4)
        ttk.Label(tb, text="Search:").pack(side="right")

        # filter row
        fr = ttk.Frame(f)
        fr.pack(fill="x", padx=8, pady=2)

        self.set_type_var = tk.StringVar(value="All")
        types = ["All", "expansion", "core", "masters", "draft_innovation",
                 "commander", "funny", "starter", "promo"]
        cb = ttk.Combobox(fr, textvariable=self.set_type_var, values=types,
                           state="readonly", width=18)
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda _: self._filter_sets())

        ttk.Button(fr, text="Download Selected", style="Accent.TButton",
                    command=self._download_sets).pack(side="right")

        self.sel_all_var = tk.BooleanVar()
        ttk.Checkbutton(fr, text="Select all visible", variable=self.sel_all_var,
                         command=self._toggle_all).pack(side="right", padx=12)

        # treeview
        cols = ("code", "name", "cards", "released", "type")
        self.set_tree = ttk.Treeview(f, columns=cols, show="tree headings",
                                      selectmode="none")
        self.set_tree.heading("#0",       text="", anchor="w")
        self.set_tree.heading("code",     text="Code")
        self.set_tree.heading("name",     text="Name")
        self.set_tree.heading("cards",    text="Cards")
        self.set_tree.heading("released", text="Released")
        self.set_tree.heading("type",     text="Type")
        self.set_tree.column("#0",        width=40, stretch=False)
        self.set_tree.column("code",      width=60,  stretch=False)
        self.set_tree.column("name",      width=320)
        self.set_tree.column("cards",     width=60,  stretch=False)
        self.set_tree.column("released",  width=100, stretch=False)
        self.set_tree.column("type",      width=130, stretch=False)

        sb = ttk.Scrollbar(f, orient="vertical", command=self.set_tree.yview)
        self.set_tree.configure(yscrollcommand=sb.set)
        self.set_tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=4)
        sb.pack(side="right", fill="y", pady=4, padx=(0, 8))

        # checkbox toggle on click
        self.set_tree.bind("<ButtonRelease-1>", self._on_tree_click)
        self._checked_sets = set()

        self.sets_status = ttk.Label(f, text="Loading sets...", style="Dim.TLabel")
        self.sets_status.pack(padx=8, pady=2, anchor="w")

    def _on_tree_click(self, event):
        item = self.set_tree.identify_row(event.y)
        if not item:
            return
        code = self.set_tree.set(item, "code")
        if code in self._checked_sets:
            self._checked_sets.discard(code)
            self.set_tree.item(item, text="")
        else:
            self._checked_sets.add(code)
            self.set_tree.item(item, text="  ✓")

    def _toggle_all(self):
        check = self.sel_all_var.get()
        for item in self.set_tree.get_children():
            code = self.set_tree.set(item, "code")
            if check:
                self._checked_sets.add(code)
                self.set_tree.item(item, text="  ✓")
            else:
                self._checked_sets.discard(code)
                self.set_tree.item(item, text="")

    def _populate_set_tree(self):
        self.set_tree.delete(*self.set_tree.get_children())
        for s in self.filtered:
            code = s["code"]
            chk = "  ✓" if code in self._checked_sets else ""
            self.set_tree.insert("", "end", text=chk,
                                  values=(code.upper(), s["name"],
                                          s.get("card_count", "?"),
                                          s.get("released_at", "?"),
                                          s.get("set_type", "?")))
        self.sets_status.config(text=f"{len(self.filtered)} sets")

    # ── SINGLE CARD TAB ──────────────────────────────────────────────────
    def _build_single_tab(self):
        f = ttk.Frame(self.nb)
        self.nb.add(f, text="  Search Card  ")

        ttk.Label(f, text="Search Card (All Prints)", style="Head.TLabel").pack(
            padx=8, pady=(8, 4), anchor="w")

        tb = ttk.Frame(f); tb.pack(fill="x", padx=8, pady=4)
        ttk.Label(tb, text="Name:").pack(side="left")
        self.card_name_var = tk.StringVar()
        ttk.Entry(tb, textvariable=self.card_name_var, width=35).pack(side="left", padx=6)
        ttk.Button(tb, text="Search", style="Accent.TButton",
                    command=self._search_card).pack(side="left", padx=4)
        ttk.Button(tb, text="Download Selected", style="Green.TButton",
                    command=self._download_selected_prints).pack(side="left", padx=4)
        ttk.Button(tb, text="Download All Prints", style="TButton",
                    command=self._download_all_prints).pack(side="left", padx=4)

        # Treeview for prints
        cols2 = ("set_code", "set_name", "number", "rarity", "artist", "status")
        self.print_tree = ttk.Treeview(f, columns=cols2, show="headings",
                                        selectmode="extended")
        self.print_tree.heading("set_code",  text="Set")
        self.print_tree.heading("set_name",  text="Set Name")
        self.print_tree.heading("number",    text="#")
        self.print_tree.heading("rarity",    text="Rarity")
        self.print_tree.heading("artist",    text="Artist")
        self.print_tree.heading("status",    text="Status")
        self.print_tree.column("set_code",   width=55,  stretch=False)
        self.print_tree.column("set_name",   width=250)
        self.print_tree.column("number",     width=50,  stretch=False)
        self.print_tree.column("rarity",     width=80,  stretch=False)
        self.print_tree.column("artist",     width=180)
        self.print_tree.column("status",     width=90,  stretch=False)

        sb2 = ttk.Scrollbar(f, orient="vertical", command=self.print_tree.yview)
        self.print_tree.configure(yscrollcommand=sb2.set)
        self.print_tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=4)
        sb2.pack(side="right", fill="y", pady=4, padx=(0, 8))

    # ── SCAN TAB ─────────────────────────────────────────────────────────
    def _build_scan_tab(self):
        f = ttk.Frame(self.nb)
        self.nb.add(f, text="  Scan Missing  ")

        ttk.Label(f, text="Scan for Missing Cards", style="Head.TLabel").pack(
            padx=8, pady=(8, 4), anchor="w")

        ttk.Label(f, text="Scans your Forge card folder vs. Scryfall to find missing images.",
                   style="Dim.TLabel").pack(padx=8, anchor="w")

        tb = ttk.Frame(f); tb.pack(fill="x", padx=8, pady=8)
        ttk.Label(tb, text="Set code (blank = all folders):").pack(side="left")
        self.scan_set_var = tk.StringVar()
        ttk.Entry(tb, textvariable=self.scan_set_var, width=12).pack(side="left", padx=6)
        ttk.Button(tb, text="Scan", style="Accent.TButton",
                    command=self._scan_missing).pack(side="left", padx=4)
        self.scan_dl_btn = ttk.Button(tb, text="Download Missing", style="Green.TButton",
                                       command=self._download_missing, state="disabled")
        self.scan_dl_btn.pack(side="left", padx=4)

        self.scan_text = tk.Text(f, bg=BG_INPUT, fg=FG, font=("Consolas", 10),
                                  bd=0, highlightthickness=0)
        self.scan_text.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    # ── SETTINGS TAB ─────────────────────────────────────────────────────
    def _build_settings_tab(self):
        f = ttk.Frame(self.nb)
        self.nb.add(f, text="  Settings  ")

        ttk.Label(f, text="Settings", style="Head.TLabel").pack(
            padx=12, pady=(12, 8), anchor="w")

        # Forge pics path
        g1 = ttk.LabelFrame(f, text="Forge image folder (Cache/pics/cards)")
        g1.pack(fill="x", padx=12, pady=6)
        r1 = ttk.Frame(g1); r1.pack(fill="x", padx=8, pady=8)
        ttk.Entry(r1, textvariable=self.forge_pics_var, width=80).pack(side="left", fill="x", expand=True)
        ttk.Button(r1, text="Browse", command=lambda: self._browse(self.forge_pics_var)).pack(side="left", padx=6)

        # Forge res/editions path
        g2 = ttk.LabelFrame(f, text="Forge editions folder (res/editions)")
        g2.pack(fill="x", padx=12, pady=6)
        r2 = ttk.Frame(g2); r2.pack(fill="x", padx=8, pady=8)
        ttk.Entry(r2, textvariable=self.forge_res_var, width=80).pack(side="left", fill="x", expand=True)
        ttk.Button(r2, text="Browse", command=lambda: self._browse(self.forge_res_var)).pack(side="left", padx=6)
        ttk.Button(g2, text="Reload Edition Mappings",
                    command=self._try_load_editions).pack(padx=8, pady=(0, 8), anchor="w")

        # Workers
        g3 = ttk.LabelFrame(f, text="Download Threads")
        g3.pack(fill="x", padx=12, pady=6)
        r3 = ttk.Frame(g3); r3.pack(fill="x", padx=8, pady=8)
        ttk.Label(r3, text="1").pack(side="left")
        self.worker_scale = ttk.Scale(r3, from_=1, to=16, variable=self.workers_var,
                                       orient="horizontal")
        self.worker_scale.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Label(r3, text="16").pack(side="left")
        self.worker_lbl = ttk.Label(r3, text=str(MAX_WORKERS), width=3)
        self.worker_lbl.pack(side="left", padx=6)
        self.workers_var.trace_add("write", lambda *_: self.worker_lbl.config(
            text=str(self.workers_var.get())))

        # Image quality
        g4 = ttk.LabelFrame(f, text="Image Quality")
        g4.pack(fill="x", padx=12, pady=6)
        self.quality_var = tk.StringVar(value=IMAGE_FORMAT)
        ttk.Combobox(g4, textvariable=self.quality_var,
                      values=["small", "normal", "large", "png", "border_crop"],
                      state="readonly", width=20).pack(padx=8, pady=8, anchor="w")

        self.map_status = ttk.Label(f, text="", style="Dim.TLabel")
        self.map_status.pack(padx=12, pady=8, anchor="w")

    # ═════════════════════════════════════════════════════════════════════
    #  LOGIC
    # ═════════════════════════════════════════════════════════════════════

    def _browse(self, var):
        p = filedialog.askdirectory()
        if p: var.set(p)

    def _try_load_editions(self):
        p = Path(self.forge_res_var.get())
        if p.exists():
            parser = ForgeEditionParser(p)
            self.mapper.load_forge_mappings(parser)
            msg = f"Loaded {len(parser.editions)} editions from Forge"
            self.map_status.config(text=f"✓ {msg}")
            self._log(msg)
        else:
            self.map_status.config(text="⚠ Editions folder not found — using built-in mappings")

    def _forge_folder(self, scryfall_code: str) -> str:
        return self.mapper.scryfall_to_forge_folder(scryfall_code)

    def _scryfall_code(self, folder: str) -> str:
        return self.mapper.forge_folder_to_scryfall(folder)

    # ── Fetch sets ────────────────────────────────────────────────────────
    def _fetch_sets(self):
        def _f():
            try:
                self.all_sets = self.client.get_all_sets()
                self.filtered = list(self.all_sets)
                self.after(0, self._filter_sets)
                self.after(0, lambda: self._log(f"Loaded {len(self.all_sets)} sets"))
            except Exception as e:
                self.after(0, lambda: self._log(f"Failed to load sets: {e}"))
        threading.Thread(target=_f, daemon=True).start()

    def _filter_sets(self):
        q = self.set_search_var.get().lower().strip()
        t = self.set_type_var.get()
        self.filtered = [s for s in self.all_sets
                         if (t == "All" or s.get("set_type") == t)
                         and (not q or q in s["name"].lower() or q in s["code"].lower())]
        self._populate_set_tree()

    # ── Download sets ─────────────────────────────────────────────────────
    def _download_sets(self):
        codes = list(self._checked_sets)
        if not codes:
            self._log("No sets selected."); return
        if self.dlm.running:
            self._log("Download already in progress."); return
        self._log(f"Preparing {len(codes)} set(s)...")
        self.cancel_btn.config(state="normal")

        def _prep():
            tasks = []
            for code in codes:
                try:
                    self.after(0, lambda c=code: self._log(f"Fetching card list for {c.upper()}..."))
                    cards = self.client.search_set(code)
                    folder = self._forge_folder(code)
                    base = Path(self.forge_pics_var.get()) / folder
                    tasks.extend(build_tasks_for_set(cards, code, base))
                except Exception as e:
                    self.after(0, lambda e=e, c=code: self._log(f"Error fetching {c}: {e}"))
            if tasks:
                self.after(0, lambda: self._log(f"Downloading {len(tasks)} cards..."))
                self.dlm.max_workers = self.workers_var.get()
                self.dlm.run(tasks,
                              on_progress=self._on_prog,
                              on_log=lambda m: self.after(0, lambda m=m: self._log(m)),
                              on_done=lambda: self.after(0, self._dl_done))
            else:
                self.after(0, lambda: self._log("No cards found."))
                self.after(0, self._dl_done)

        threading.Thread(target=_prep, daemon=True).start()

    # ── Search card (all prints) ──────────────────────────────────────────
    def _search_card(self):
        name = self.card_name_var.get().strip()
        if not name: return

        self.print_tree.delete(*self.print_tree.get_children())
        self._current_prints = []
        self._log(f"Searching all prints of '{name}'...")

        def _s():
            try:
                # First resolve fuzzy name
                resolved = self.client.search_card_name(name)
                real_name = resolved.get("name", name)
                self.after(0, lambda: self.card_name_var.set(real_name))

                # Now get all prints
                prints = self.client.search_all_prints(real_name)
                self._current_prints = prints
                base = Path(self.forge_pics_var.get())

                # Pre-compute the correct filename for each print
                # by building tasks (which handle duplicate numbering)
                resolver = lambda sc: base / self._forge_folder(sc)
                preview_tasks = build_tasks_multi_set(prints, resolver)
                # Map (set_code, collector_number) -> save_path for status display
                path_lookup = {(t.set_code, t.collector_number): t.save_path
                               for t in preview_tasks}

                def _fill():
                    for p in prints:
                        key = (p["set"], p.get("collector_number", ""))
                        sp = path_lookup.get(key)
                        exists = sp.exists() if sp else False
                        self.print_tree.insert("", "end", values=(
                            p["set"].upper(),
                            p.get("set_name", "?"),
                            p.get("collector_number", "?"),
                            p.get("rarity", "?").title(),
                            p.get("artist", "?"),
                            "✓ exists" if exists else "—"
                        ))
                    self._log(f"Found {len(prints)} prints of '{real_name}'")

                self.after(0, _fill)
            except Exception as e:
                self.after(0, lambda: self._log(f"Search error: {e}"))

        threading.Thread(target=_s, daemon=True).start()

    def _prints_to_tasks(self, prints: list[dict]) -> list[DownloadTask]:
        base = Path(self.forge_pics_var.get())
        resolver = lambda sc: base / self._forge_folder(sc)
        return build_tasks_multi_set(prints, resolver)

    def _download_selected_prints(self):
        sel = self.print_tree.selection()
        if not sel:
            self._log("No prints selected. Click rows to select."); return
        indices = [self.print_tree.index(s) for s in sel]
        prints = [self._current_prints[i] for i in indices if i < len(self._current_prints)]
        tasks = self._prints_to_tasks(prints)
        if tasks:
            self._run_tasks(tasks)

    def _download_all_prints(self):
        if not self._current_prints:
            self._log("Search for a card first."); return
        tasks = self._prints_to_tasks(self._current_prints)
        if tasks:
            self._run_tasks(tasks)

    # ── Scan ──────────────────────────────────────────────────────────────
    def _scan_missing(self):
        code = self.scan_set_var.get().strip().lower()
        base = Path(self.forge_pics_var.get())
        if not base.exists():
            self._log("Forge card folder not found — check Settings."); return

        self.scan_text.delete("1.0", "end")
        self._missing_tasks = []
        self._scan_insert("Scanning...\n")

        def _do():
            try:
                if code:
                    folders = [code]
                else:
                    folders = [d.name.lower() for d in base.iterdir() if d.is_dir()]

                total_missing = 0
                for folder in folders:
                    try:
                        sc = self._scryfall_code(folder)
                        cards = self.client.search_set(sc)
                        ff = self._forge_folder(sc)
                        set_dir = base / ff
                        set_dir.mkdir(parents=True, exist_ok=True)
                        existing = {f.name.lower() for f in set_dir.iterdir() if f.is_file()}

                        # Build properly-numbered tasks for this set
                        set_tasks = build_tasks_for_set(cards, sc, set_dir)
                        missing = 0
                        for t in set_tasks:
                            if t.save_path.name.lower() not in existing:
                                missing += 1
                                self._missing_tasks.append(t)
                        total_missing += missing
                        self.after(0, lambda f=folder.upper(), m=missing, t=len(cards):
                                   self._scan_insert(f"  {f}: {m} missing / {t} total\n"))
                    except Exception as e:
                        self.after(0, lambda f=folder, e=e:
                                   self._scan_insert(f"  {f}: error — {e}\n"))

                self.after(0, lambda: self._scan_insert(
                    f"\n{'─'*50}\nTotal missing: {total_missing}\n"))
                if total_missing > 0:
                    self.after(0, lambda: self.scan_dl_btn.config(state="normal"))
            except Exception as e:
                self.after(0, lambda: self._scan_insert(f"Scan error: {e}\n"))

        threading.Thread(target=_do, daemon=True).start()

    def _scan_insert(self, txt):
        self.scan_text.insert("end", txt)
        self.scan_text.see("end")

    def _download_missing(self):
        if not self._missing_tasks:
            self._log("No missing cards."); return
        self.scan_dl_btn.config(state="disabled")
        self._run_tasks(self._missing_tasks)

    # ── Shared download runner ────────────────────────────────────────────
    def _run_tasks(self, tasks):
        if self.dlm.running:
            self._log("Download already in progress."); return
        self.cancel_btn.config(state="normal")
        self._log(f"Downloading {len(tasks)} cards...")
        self.dlm.max_workers = self.workers_var.get()
        self.dlm.run(tasks,
                      on_progress=self._on_prog,
                      on_log=lambda m: self.after(0, lambda m=m: self._log(m)),
                      on_done=lambda: self.after(0, self._dl_done))

    def _cancel(self):
        self.dlm.cancel()
        self._log("Cancelling...")
        self.cancel_btn.config(state="disabled")

    def _dl_done(self):
        self.cancel_btn.config(state="disabled")

    # ── Progress (fixed-width bar, label above) ───────────────────────────
    def _on_prog(self, done, total, task):
        def _u():
            pct = done / total * 100 if total else 0
            self.prog_bar["value"] = pct
            self.prog_pct.config(text=f"{pct:.0f}%")
            icon = "✓" if task.status == "done" else ("⏭" if task.status == "skipped" else "✗")
            self.prog_item.config(
                text=f"{icon}  [{done}/{total}]  {task.card_name}  ({task.set_code.upper()})")
        self.after(0, _u)

    # ── Log ───────────────────────────────────────────────────────────────
    def _log(self, msg):
        ts = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{ts}] {msg}\n")
        self.log_text.see("end")
        log.info(msg)


# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app = App()
    app.mainloop()
