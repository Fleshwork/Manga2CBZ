#!/usr/bin/env python3
"""
manga2cbz - pack chapter folders of numbered images into CBZ files.

Chapter folder names it understands (case-insensitive, mixed freely):
    Ch.001
    Ch.001.5                       decimal chapters
    Vol.01 Ch.001 - Title Example
    Chapter 12
    [Group] Vol.3 Ch.25 - Some Title

Ordering is always by chapter number, whatever the naming style.

Decimal chapters:
    Ch.001.1, Ch.001.2 ...  treated as PARTS of chapter 1. If a whole "Ch.001"
                            folder exists the parts are skipped; if not, the
                            parts are used, in order.
    Ch.001.5                treated as a SIDE STORY and always included, after
                            chapter 1 and before chapter 2.
    --keep-all-decimals     disables the skipping and includes everything.

Grouping (--mode):
    auto  (default) if EVERY chapter has a Vol tag, one CBZ per real volume.
          Otherwise (no tags, or a mix) blocks of --size chapters by number:
          Ch.001-010, Ch.011-020, ...
    chunk always blocks of --size chapters, ignoring Vol tags.

Re-running is safe: CBZ files whose contents haven't changed are skipped,
and files whose chapters changed (e.g. new chapters arrived for the last
block) are rebuilt automatically.

Usage:
    python manga2cbz.py "D:/Manga/Some Title" --dry-run
    python manga2cbz.py "D:/Manga/Some Title"
    python manga2cbz.py "D:/Manga/Some Title" --mode chunk --size 10 -o "D:/CBZ"

Only the Python standard library is used. Original files are never modified.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from xml.sax.saxutils import escape

__version__ = "1.0.0"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
SIDE_STORY_PART = "5"           # Ch.N.5 is always kept

CHAPTER_RE = re.compile(
    r"""
    (?:(?<![a-z])vol(?:ume)?[\s._-]*(?P<vol>\d+(?:\.\d+)?)[\s._-]*)?
    (?<![a-z])ch(?:apter|ap)?[\s._-]*(?P<num>\d+)(?:\.(?P<part>\d+))?
    (?P<rest>.*)$
    """,
    re.IGNORECASE | re.VERBOSE,
)
NOTES_RE = re.compile(r"<Notes>manga2cbz: (.*?)</Notes>")

ProgressFn = Callable[[int, int, str], None]


class Cancelled(Exception):
    """Raised when a run is stopped through the `cancel` event."""


# ----------------------------------------------------------------- data types
@dataclass
class Chapter:
    path: Path
    num: int                         # whole chapter number
    part: Optional[str] = None       # decimal digits as written: "5", "1", "10"
    vol: Optional[float] = None
    title: str = ""
    pages: list[Path] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)

    @property
    def sort_key(self):
        return (self.num, -1 if self.part is None else int(self.part))

    @property
    def label(self) -> str:
        return f"{self.num:03d}" + (f".{self.part}" if self.part else "")

    @property
    def is_side_story(self) -> bool:
        return self.part is not None and self.part.lstrip("0") == SIDE_STORY_PART


@dataclass
class Group:
    label: str                       # used in the filename, e.g. "Vol.01"
    chapters: list[Chapter]
    volume: Optional[float] = None

    @property
    def page_count(self) -> int:
        return sum(len(c.pages) for c in self.chapters)

    @property
    def fingerprint(self) -> str:
        """Chapter list + page counts, stored in the CBZ to detect changes."""
        return ",".join(f"{c.label}:{len(c.pages)}" for c in self.chapters)


@dataclass
class ScanResult:
    series: str
    chapters: list[Chapter]
    ignored_folders: list[str]
    skipped_parts: list[str]
    warnings: list[str]
    info: list[str] = field(default_factory=list)


# ----------------------------------------------------------------- helpers
def fmt_vol(n: float, width: int = 2) -> str:
    if n == int(n):
        return f"{int(n):0{width}d}"
    whole, frac = str(n).split(".")
    return f"{int(whole):0{width}d}.{frac}"


def sniff_ext(path: Path) -> Optional[str]:
    """The image's REAL type, read from its first bytes (not its filename)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return None
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[:4] in (b"GIF8",):
        return ".gif"
    if head[4:8] == b"ftyp" and head[8:12] in (b"avif", b"avis"):
        return ".avif"
    return None


def page_ext(path: Path) -> str:
    """Extension to use inside the CBZ: the real type if known."""
    return sniff_ext(path) or path.suffix.lower()


def natural_key(name: str):
    """'10.jpg' sorts after '2.jpg'."""
    parts = re.split(r"(\d+)", name.lower())
    return [int(t) if t.isdigit() else t for t in parts]


def parse_chapter_name(name: str):
    """-> (num, part, vol, title) or None."""
    m = CHAPTER_RE.search(name)
    if not m:
        return None
    vol = float(m.group("vol")) if m.group("vol") else None
    return (int(m.group("num")), m.group("part"), vol,
            m.group("rest").strip(" -_:."))


def safe_filename(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", s).strip(" .")


# ----------------------------------------------------------------- scanning
def apply_decimal_rule(chapters: list[Chapter]):
    """Drop Ch.N.x parts when a whole Ch.N exists. Side stories (.5) stay."""
    wholes = {c.num for c in chapters if c.part is None}
    kept, dropped = [], []
    for c in chapters:
        if c.part is not None and not c.is_side_story and c.num in wholes:
            dropped.append(c)
        else:
            kept.append(c)
    return kept, dropped


def scan(manga_dir: Path, keep_all_decimals: bool = False) -> ScanResult:
    if not manga_dir.is_dir():
        raise NotADirectoryError(f"Not a folder: {manga_dir}")

    chapters, ignored, warnings = [], [], []
    for sub in sorted(manga_dir.iterdir(), key=lambda p: natural_key(p.name)):
        if not sub.is_dir():
            continue
        parsed = parse_chapter_name(sub.name)
        if parsed is None:
            ignored.append(sub.name)
            continue
        num, part, vol, title = parsed
        chap = Chapter(path=sub, num=num, part=part, vol=vol, title=title)
        for f in sorted(sub.iterdir(), key=lambda p: natural_key(p.name)):
            if f.name.startswith("."):
                continue                  # hidden files, macOS "._1.jpg" junk
            if f.is_file() and f.suffix.lower() in IMAGE_EXTS:
                chap.pages.append(f)
            elif f.is_file():
                chap.skipped_files.append(f.name)
        if not chap.pages:
            warnings.append(f"'{sub.name}' has no images - skipped")
            continue
        if chap.skipped_files:
            warnings.append(
                f"'{sub.name}': ignored non-image files {chap.skipped_files}")
        mislabeled = []
        for f in chap.pages:
            real = sniff_ext(f)
            named = ".jpg" if f.suffix.lower() == ".jpeg" else f.suffix.lower()
            if real and real != named:
                mislabeled.append(f"{f.name} is really {real[1:].upper()}")
        if mislabeled:
            shown = ", ".join(mislabeled[:5]) + (
                f" (+{len(mislabeled) - 5} more)" if len(mislabeled) > 5 else "")
            warnings.append(
                f"'{sub.name}': {len(mislabeled)} file(s) have the wrong extension "
                f"- {shown}. They get the correct extension inside the CBZ.")
        stems: dict[str, list[str]] = {}
        for f in chap.pages:
            stems.setdefault(f.stem.lower(), []).append(f.name)
        dupes = [names for names in stems.values() if len(names) > 1]
        if dupes:
            warnings.append(
                f"'{sub.name}': same page number in more than one file "
                f"{dupes} - all included, so that page will appear twice")
        chapters.append(chap)

    chapters.sort(key=lambda c: (c.sort_key, natural_key(c.path.name)))

    dropped: list[Chapter] = []
    if not keep_all_decimals:
        chapters, dropped = apply_decimal_rule(chapters)
    skipped_parts = [f"'{c.path.name}' (whole Ch.{c.num:03d} exists)" for c in dropped]

    seen: dict[str, str] = {}
    for c in chapters:
        if c.label in seen:
            warnings.append(
                f"Chapter {c.label} appears twice: '{seen[c.label]}' and "
                f"'{c.path.name}' - both included")
        seen[c.label] = c.path.name

    return ScanResult(manga_dir.name, chapters, ignored, skipped_parts, warnings)


# ----------------------------------------------------------------- grouping
def _blocks_by_number(chapters: list[Chapter], size: int) -> list[Group]:
    """Ch.001-010, Ch.011-020 ... by chapter NUMBER, so a block's filename
    never changes as new chapters arrive. Decimals ride with their whole
    number (Ch.010.5 goes in the 001-010 block)."""
    blocks: dict[int, list[Chapter]] = {}
    for c in chapters:
        blocks.setdefault(max(c.num - 1, 0) // size, []).append(c)
    groups = []
    for b in sorted(blocks):
        start = min(b * size + 1, blocks[b][0].num)
        end = (b + 1) * size
        groups.append(Group(f"Ch.{start:03d}-{end:03d}", blocks[b]))
    return groups


def group_chapters(chapters: list[Chapter], mode: str = "auto",
                   size: int = 10) -> tuple[list[Group], str]:
    """-> (groups, explanation of the grouping used)."""
    tagged = sum(c.vol is not None for c in chapters)
    if mode == "auto" and tagged == len(chapters):
        by_vol: dict[float, list[Chapter]] = {}
        for c in chapters:
            by_vol.setdefault(c.vol, []).append(c)
        groups = [Group(f"Vol.{fmt_vol(v)}", by_vol[v], volume=v)
                  for v in sorted(by_vol)]
        return groups, "every chapter has a Vol tag - one CBZ per volume"

    if mode == "chunk":
        why = f"blocks of {size} chapters (chunk mode)"
    elif tagged:
        why = (f"Vol tags on only {tagged} of {len(chapters)} chapters - "
               f"ignoring them, blocks of {size} chapters by number")
    else:
        why = f"no Vol tags - blocks of {size} chapters by number"
    return _blocks_by_number(chapters, size), why


# ----------------------------------------------------------------- writing
def comic_info_xml(series: str, group: Group) -> str:
    titles = [c.title for c in group.chapters if c.title]
    lines = ['<?xml version="1.0" encoding="utf-8"?>',
             '<ComicInfo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
             f"  <Series>{escape(series)}</Series>"]
    if group.volume is not None:
        lines.append(f"  <Volume>{fmt_vol(group.volume, 1)}</Volume>")
    if len(group.chapters) == 1:
        lines.append(f"  <Number>{group.chapters[0].label.lstrip('0') or '0'}</Number>")
        if titles:
            lines.append(f"  <Title>{escape(titles[0])}</Title>")
    elif titles:
        lines.append(f"  <Summary>{escape(' / '.join(titles))}</Summary>")
    lines += [f"  <PageCount>{group.page_count}</PageCount>",
              f"  <Notes>manga2cbz: {escape(group.fingerprint)}</Notes>",
              "  <Manga>YesAndRightToLeft</Manga>",
              "</ComicInfo>"]
    return "\n".join(lines) + "\n"


def existing_fingerprint(path: Path) -> Optional[str]:
    try:
        with zipfile.ZipFile(path) as zf:
            m = NOTES_RE.search(zf.read("ComicInfo.xml").decode("utf-8", "replace"))
        return m.group(1) if m else None
    except (OSError, KeyError, zipfile.BadZipFile):
        return None


def write_cbz(series: str, group: Group, out_path: Path,
              on_page: Optional[Callable[[], None]] = None) -> None:
    """Writes to a .part file first, so an interrupted run never leaves a
    half-written .cbz behind."""
    tmp = out_path.with_name(out_path.name + ".part")
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.writestr("ComicInfo.xml", comic_info_xml(series, group))
            cw = max(3, len(str(len(group.chapters))))
            pw = max(3, len(str(max(len(c.pages) for c in group.chapters))))
            for ci, chap in enumerate(group.chapters, start=1):
                for pi, page in enumerate(chap.pages, start=1):
                    # chapter-position_page keeps order in every reader
                    zf.write(page, f"{ci:0{cw}d}_{pi:0{pw}d}{page_ext(page)}")
                    if on_page:
                        on_page()
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, out_path)


# ----------------------------------------------------------------- driver
def convert(manga_dir: Path, out_dir: Optional[Path] = None, mode: str = "auto",
            size: int = 10, overwrite: bool = False, dry_run: bool = False,
            keep_all_decimals: bool = False,
            progress: Optional[ProgressFn] = None,
            cancel=None,
            log: Callable[[str], None] = print) -> list[Path]:
    """Main entry point; the GUI calls this with its own callbacks.
    `cancel` is an optional threading.Event; setting it stops the run after
    the current page and raises Cancelled (finished CBZ files are kept)."""
    manga_dir = Path(manga_dir).expanduser().resolve()
    out_dir = Path(out_dir) if out_dir else manga_dir.parent / f"{manga_dir.name} (CBZ)"

    result = scan(manga_dir, keep_all_decimals)
    if not result.chapters:
        raise ValueError(f"No chapter folders with images found in {manga_dir}")

    groups, why = group_chapters(result.chapters, mode, size)
    series = safe_filename(result.series)

    log(f"Series:   {result.series}")
    log(f"Chapters: {len(result.chapters)}   ->   {len(groups)} CBZ file(s)")
    log(f"Grouping: {why}")
    log(f"Output:   {out_dir}")
    for name in result.ignored_folders:
        log(f"  ! ignored folder (no chapter number found): '{name}'")
    for s in result.skipped_parts:
        log(f"  - skipped part {s}")
    for w in result.warnings:
        log(f"  ! {w}")
    log("")

    plan = []
    for g in groups:
        out_path = out_dir / f"{series} - {g.label}.cbz"
        if not out_path.exists():
            status = "new"
        elif overwrite:
            status = "overwrite"
        elif existing_fingerprint(out_path) == g.fingerprint:
            status = "up to date"
        else:
            status = "update"
        chs = ", ".join(c.label for c in g.chapters)
        log(f"  {out_path.name:<40} {g.page_count:>5} pages  [{status}]  ch {chs}")
        if status != "up to date":
            plan.append((g, out_path))

    if dry_run:
        log("\nDry run - nothing written.")
        return []
    if not plan:
        log("\nEverything is up to date.")
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    total = sum(g.page_count for g, _ in plan)
    done = 0
    written = []
    for g, out_path in plan:
        def tick(name=out_path.name):
            nonlocal done
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            done += 1
            if progress:
                progress(done, total, name)
        write_cbz(result.series, g, out_path, on_page=tick)
        written.append(out_path)

    log(f"\nDone: {len(written)} file(s), {total} pages.")
    return written


_last_pct = -1


def _cli_progress(done: int, total: int, name: str) -> None:
    global _last_pct
    pct = done * 100 // total
    if pct == _last_pct and done != total:
        return
    _last_pct = pct
    width = 30
    filled = width * done // total
    sys.stdout.write(f"\r[{'#' * filled}{'.' * (width - filled)}] {pct:3d}%  "
                     f"{name[:40]:<40}")
    if done == total:
        sys.stdout.write("\n")
    sys.stdout.flush()


def main() -> int:
    ap = argparse.ArgumentParser(description="Pack manga chapter folders into CBZ files.")
    ap.add_argument("manga_dir", help="folder containing the chapter folders")
    ap.add_argument("-o", "--out", help="output folder (default: '<manga> (CBZ)' next to it)")
    ap.add_argument("--mode", choices=["auto", "chunk"], default="auto",
                    help="auto: per real volume only if every chapter is tagged; "
                         "chunk: always blocks of --size")
    ap.add_argument("--size", type=int, default=10, help="chapters per block")
    ap.add_argument("--keep-all-decimals", action="store_true",
                    help="include Ch.N.x parts even when a whole Ch.N exists")
    ap.add_argument("--overwrite", action="store_true", help="rebuild every CBZ")
    ap.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    ap.add_argument("--version", action="version", version=f"manga2cbz {__version__}")
    a = ap.parse_args()
    try:
        convert(Path(a.manga_dir), Path(a.out) if a.out else None, a.mode, a.size,
                a.overwrite, a.dry_run, a.keep_all_decimals, progress=_cli_progress)
    except (NotADirectoryError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped. Finished CBZ files were kept.", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
