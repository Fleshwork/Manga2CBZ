#!/usr/bin/env python3
"""
manga2cbz GUI - a small CustomTkinter front end for manga2cbz.py.

Keep this file in the same folder as manga2cbz.py, then run:
    pip install customtkinter
    python manga2cbz_gui.py
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from tkinter import filedialog, messagebox

try:
    import customtkinter as ctk
except ImportError:
    print("CustomTkinter is not installed. Run:  pip install customtkinter")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manga2cbz as backend  # noqa: E402

SETTINGS_FILE = Path.home() / ".manga2cbz_gui.json"
# Files bundled by PyInstaller live in sys._MEIPASS; otherwise next to this script.
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
ICON_FILE = RESOURCE_DIR / "manga2cbz.ico"
MONO = ("Consolas" if sys.platform == "win32"
        else "Menlo" if sys.platform == "darwin" else "DejaVu Sans Mono", 12)


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    try:
        SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def open_in_file_manager(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class App(ctk.CTk):
    CANCEL_ACTIVE = ("#c0392b", "#a93226")
    CANCEL_IDLE = ("gray75", "gray30")
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("system")
        self.title(f"Manga → CBZ  v{backend.__version__}")
        if sys.platform == "win32" and ICON_FILE.exists():
            # CustomTkinter sets its own icon shortly after start; set ours after it.
            self.after(250, lambda: self.iconbitmap(str(ICON_FILE)))
        self.geometry("920x640")
        self.minsize(720, 480)

        self.settings = load_settings()
        self.events: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.last_out_dir: Path | None = None

        self.manga_var = ctk.StringVar(value=self.settings.get("manga_dir", ""))
        self.out_var = ctk.StringVar(value=self.settings.get("out_dir", ""))
        self.mode_var = ctk.StringVar(value=self.settings.get("mode", "Auto"))
        self.size_var = ctk.StringVar(value=str(self.settings.get("size", 10)))
        self.decimals_var = ctk.BooleanVar(value=False)
        self.overwrite_var = ctk.BooleanVar(value=False)

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(50, self._poll)

    # ------------------------------------------------------------- layout
    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)
        pad = {"padx": 16}

        # folders
        folders = ctk.CTkFrame(self, fg_color="transparent")
        folders.grid(row=0, column=0, sticky="ew", pady=(16, 8), **pad)
        folders.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(folders, text="Manga folder").grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.manga_entry = ctk.CTkEntry(
            folders, textvariable=self.manga_var,
            placeholder_text="The folder that contains Ch.001, Ch.002 …")
        self.manga_entry.grid(row=0, column=1, sticky="ew")
        self.manga_btn = ctk.CTkButton(folders, text="Browse…", width=90,
                                       command=self._browse_manga)
        self.manga_btn.grid(row=0, column=2, padx=(8, 0))

        ctk.CTkLabel(folders, text="Output folder").grid(row=1, column=0, sticky="w",
                                                         padx=(0, 12), pady=(8, 0))
        self.out_entry = ctk.CTkEntry(
            folders, textvariable=self.out_var,
            placeholder_text="Leave empty for '<manga> (CBZ)' next to the manga folder")
        self.out_entry.grid(row=1, column=1, sticky="ew", pady=(8, 0))
        self.out_btn = ctk.CTkButton(folders, text="Browse…", width=90,
                                     command=self._browse_out)
        self.out_btn.grid(row=1, column=2, padx=(8, 0), pady=(8, 0))

        # options
        opts = ctk.CTkFrame(self, fg_color="transparent")
        opts.grid(row=1, column=0, sticky="ew", pady=(0, 8), **pad)

        ctk.CTkLabel(opts, text="Grouping").pack(side="left", padx=(0, 8))
        self.mode_seg = ctk.CTkSegmentedButton(opts, values=["Auto", "Blocks"],
                                               variable=self.mode_var)
        self.mode_seg.pack(side="left")
        ctk.CTkLabel(opts, text="Chapters per block").pack(side="left", padx=(20, 8))
        self.size_entry = ctk.CTkEntry(opts, textvariable=self.size_var, width=50,
                                       justify="center")
        self.size_entry.pack(side="left")
        self.decimals_chk = ctk.CTkCheckBox(opts, text="Keep all decimal chapters",
                                            variable=self.decimals_var)
        self.decimals_chk.pack(side="left", padx=(20, 0))
        self.overwrite_chk = ctk.CTkCheckBox(opts, text="Rebuild all",
                                             variable=self.overwrite_var)
        self.overwrite_chk.pack(side="left", padx=(16, 0))

        # actions
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=2, column=0, sticky="ew", pady=(4, 8), **pad)
        self.preview_btn = ctk.CTkButton(actions, text="Preview", width=110,
                                         fg_color="transparent", border_width=1,
                                         text_color=("gray10", "gray90"),
                                         command=lambda: self._start(dry_run=True))
        self.preview_btn.pack(side="left")
        self.convert_btn = ctk.CTkButton(actions, text="Convert", width=110,
                                         command=lambda: self._start(dry_run=False))
        self.convert_btn.pack(side="left", padx=(8, 0))
        self.cancel_btn = ctk.CTkButton(actions, text="Cancel", width=90,
                                        fg_color=self.CANCEL_IDLE,
                                        hover_color=("#a93226", "#922b21"),
                                        text_color_disabled=("gray45", "gray60"),
                                        state="disabled", command=self._cancel)
        self.cancel_btn.pack(side="left", padx=(8, 0))
        self.open_btn = ctk.CTkButton(actions, text="Open output folder", width=150,
                                      fg_color="transparent", border_width=1,
                                      text_color=("gray10", "gray90"),
                                      state="disabled", command=self._open_output)
        self.open_btn.pack(side="right")

        # progress
        prog = ctk.CTkFrame(self, fg_color="transparent")
        prog.grid(row=3, column=0, sticky="ew", pady=(0, 8), **pad)
        prog.grid_columnconfigure(0, weight=1)
        self.progress = ctk.CTkProgressBar(prog)
        self.progress.set(0)
        self.progress.grid(row=0, column=0, sticky="ew")
        self.status = ctk.CTkLabel(prog, text="Pick a manga folder, then Preview.",
                                   anchor="w")
        self.status.grid(row=1, column=0, sticky="ew", pady=(4, 0))

        # log
        self.log_box = ctk.CTkTextbox(self, font=MONO, wrap="none")
        self.log_box.grid(row=4, column=0, sticky="nsew", pady=(0, 16), **pad)
        self.log_box.configure(state="disabled")

        self.inputs = [self.manga_entry, self.manga_btn, self.out_entry, self.out_btn,
                       self.mode_seg, self.size_entry, self.decimals_chk,
                       self.overwrite_chk, self.preview_btn, self.convert_btn]

    # ------------------------------------------------------------- actions
    def _browse_manga(self):
        start = self.manga_var.get() or self.settings.get("manga_dir", "")
        if start:
            start = str(Path(start).parent)
        path = filedialog.askdirectory(title="Choose a manga folder",
                                       initialdir=start or None)
        if path:
            self.manga_var.set(path)
            self._start(dry_run=True)       # preview right away

    def _browse_out(self):
        path = filedialog.askdirectory(title="Choose an output folder",
                                       initialdir=self.out_var.get() or None)
        if path:
            self.out_var.set(path)

    def _start(self, dry_run: bool):
        if self.worker and self.worker.is_alive():
            return
        manga = self.manga_var.get().strip()
        if not manga or not Path(manga).is_dir():
            messagebox.showwarning("Manga → CBZ", "Please choose a manga folder first.")
            return
        try:
            size = int(self.size_var.get())
            if size < 1:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Manga → CBZ", "Chapters per block must be a whole number above 0.")
            return

        out = self.out_var.get().strip()
        self.settings.update(manga_dir=manga, out_dir=out,
                             mode=self.mode_var.get(), size=size)
        save_settings(self.settings)

        manga_path = Path(manga)
        self.last_out_dir = Path(out) if out else \
            manga_path.resolve().parent / f"{manga_path.name} (CBZ)"

        self._clear_log()
        self.progress.set(0)
        self.status.configure(text="Previewing…" if dry_run else "Converting…")
        self._set_running(True)
        self.cancel_event.clear()

        kwargs = dict(
            manga_dir=manga_path,
            out_dir=Path(out) if out else None,
            mode="chunk" if self.mode_var.get() == "Blocks" else "auto",
            size=size,
            overwrite=self.overwrite_var.get(),
            dry_run=dry_run,
            keep_all_decimals=self.decimals_var.get(),
        )
        self.worker = threading.Thread(target=self._run, args=(kwargs, dry_run),
                                       daemon=True)
        self.worker.start()

    def _run(self, kwargs: dict, dry_run: bool):
        """Background thread: never touches widgets, only posts events."""
        post = self.events.put
        try:
            written = backend.convert(
                **kwargs,
                log=lambda msg: post(("log", msg)),
                progress=lambda d, t, n: post(("progress", d, t, n)),
                cancel=self.cancel_event,
            )
            post(("done", dry_run, len(written)))
        except backend.Cancelled:
            post(("cancelled",))
        except Exception as e:  # show any failure instead of dying silently
            post(("error", f"{type(e).__name__}: {e}"))

    def _cancel(self):
        if not (self.worker and self.worker.is_alive()):
            return
        self.cancel_event.set()
        self.cancel_btn.configure(state="disabled")
        self.status.configure(text="Cancelling after the current page…")

    def _open_output(self):
        if self.last_out_dir and self.last_out_dir.is_dir():
            open_in_file_manager(self.last_out_dir)

    # ------------------------------------------------------------- event pump
    def _poll(self):
        latest_progress = None
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "log":
                    self._append_log(ev[1])
                elif kind == "progress":
                    latest_progress = ev        # only draw the newest one
                elif kind == "done":
                    _, dry_run, count = ev
                    if latest_progress:
                        self._show_progress(latest_progress)
                        latest_progress = None
                    if dry_run:
                        self.status.configure(text="Preview ready. Press Convert to write the files.")
                    else:
                        self.progress.set(1)
                        self.status.configure(
                            text=f"Done – {count} file(s) written." if count
                            else "Everything was already up to date.")
                    self._set_running(False)
                elif kind == "cancelled":
                    self._append_log("\nCancelled. Files finished before cancelling were kept.")
                    self.status.configure(text="Cancelled.")
                    self._set_running(False)
                    latest_progress = None
                elif kind == "error":
                    self._append_log(f"\nERROR  {ev[1]}")
                    self.status.configure(text="Something went wrong – see the log.")
                    self._set_running(False)
                    latest_progress = None
        except queue.Empty:
            pass
        if latest_progress:
            self._show_progress(latest_progress)
        self.after(50, self._poll)

    def _show_progress(self, ev):
        _, done, total, name = ev
        self.progress.set(done / total if total else 0)
        self.status.configure(text=f"{done} / {total} pages   ·   {name}")

    # ------------------------------------------------------------- helpers
    def _set_running(self, running: bool):
        state = "disabled" if running else "normal"
        for w in self.inputs:
            w.configure(state=state)
        self.cancel_btn.configure(
            state="normal" if running else "disabled",
            fg_color=self.CANCEL_ACTIVE if running else self.CANCEL_IDLE)
        can_open = (not running and self.last_out_dir is not None
                    and self.last_out_dir.is_dir())
        self.open_btn.configure(state="normal" if can_open else "disabled")

    def _append_log(self, text: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Manga → CBZ",
                                       "A conversion is running. Cancel it and quit?"):
                return
            self.cancel_event.set()
            self.worker.join(timeout=5)
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
