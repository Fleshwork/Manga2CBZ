"""Tests for manga2cbz. Run with:  python -m pytest"""
from __future__ import annotations

import sys
import threading
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import manga2cbz as m  # noqa: E402

# Minimal valid file headers, enough for format detection.
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 60
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 60
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 60


def make_series(root: Path, chapters: dict[str, int | dict[str, bytes]]) -> Path:
    """chapters: folder name -> page count (jpgs) or {filename: bytes}."""
    for name, pages in chapters.items():
        d = root / name
        d.mkdir(parents=True)
        if isinstance(pages, int):
            pages = {f"{i}.jpg": JPG + f"{name}|{i}".encode() for i in range(1, pages + 1)}
        for fname, data in pages.items():
            (d / fname).write_bytes(data)
    return root


def run(series: Path, **kw) -> list[Path]:
    return m.convert(series, log=lambda _msg: None, **kw)


def page_tags(cbz: Path) -> list[str]:
    """The 'folder|page' tag embedded in each page, in archive order."""
    with zipfile.ZipFile(cbz) as zf:
        return [zf.read(n)[64:].decode() for n in zf.namelist() if n != "ComicInfo.xml"]


def chapter_order(cbz: Path) -> list[str]:
    out: list[str] = []
    for tag in page_tags(cbz):
        ch = tag.split("|")[0]
        if not out or out[-1] != ch:
            out.append(ch)
    return out


# ------------------------------------------------------------------ parsing
@pytest.mark.parametrize("name, expected", [
    ("Ch.001", (1, None, None, "")),
    ("Ch.010.5", (10, "5", None, "")),
    ("Vol.01 Ch.001 - Title Example", (1, None, 1.0, "Title Example")),
    ("Chapter 12", (12, None, None, "")),
    ("[Group] Vol.3 Ch.25 - Some Title", (25, None, 3.0, "Some Title")),
    ("vol 2 ch 7: The Rain", (7, None, 2.0, "The Rain")),
    ("Volume 04 Chapter 031 - A-B Test", (31, None, 4.0, "A-B Test")),
    ("[Chibi Scans] Ch.5", (5, None, None, "")),
])
def test_parse_chapter_names(name, expected):
    assert m.parse_chapter_name(name) == expected


@pytest.mark.parametrize("name", ["Covers", "Extras", "Characters", "Volume 1 artbook"])
def test_non_chapter_folders_are_ignored(name):
    assert m.parse_chapter_name(name) is None


# ------------------------------------------------------------------ ordering
def test_pages_sort_numerically(tmp_path):
    s = make_series(tmp_path / "S", {"Ch.001": 12})
    [cbz] = run(s)
    assert page_tags(cbz) == [f"Ch.001|{i}" for i in range(1, 13)]


def test_mixed_naming_sorted_by_chapter_number(tmp_path):
    s = make_series(tmp_path / "S", {
        "Vol.01 Ch.001 - Title Example": 2, "Ch.002": 2,
        "Vol.01 Ch.003 - Third": 2, "Ch.004": 2,
    })
    [cbz] = run(s)
    assert chapter_order(cbz) == [
        "Vol.01 Ch.001 - Title Example", "Ch.002", "Vol.01 Ch.003 - Third", "Ch.004"]


# ------------------------------------------------------------------ decimals
def test_decimal_parts_skipped_when_whole_chapter_exists(tmp_path):
    s = make_series(tmp_path / "S", {"Ch.003": 1, "Ch.003.1": 1, "Ch.003.2": 1})
    [cbz] = run(s)
    assert chapter_order(cbz) == ["Ch.003"]


def test_decimal_parts_used_when_whole_chapter_missing(tmp_path):
    s = make_series(tmp_path / "S", {"Ch.004.2": 1, "Ch.004.1": 1, "Ch.005": 1})
    [cbz] = run(s)
    assert chapter_order(cbz) == ["Ch.004.1", "Ch.004.2", "Ch.005"]


def test_side_stories_always_kept_in_place(tmp_path):
    s = make_series(tmp_path / "S", {"Ch.001": 1, "Ch.001.5": 1, "Ch.002": 1})
    [cbz] = run(s)
    assert chapter_order(cbz) == ["Ch.001", "Ch.001.5", "Ch.002"]


def test_keep_all_decimals(tmp_path):
    s = make_series(tmp_path / "S", {"Ch.003": 1, "Ch.003.1": 1})
    [cbz] = run(s, keep_all_decimals=True)
    assert chapter_order(cbz) == ["Ch.003", "Ch.003.1"]


# ------------------------------------------------------------------ grouping
def test_blocks_of_ten_by_number(tmp_path):
    s = make_series(tmp_path / "S", {f"Ch.{i:03d}": 1 for i in range(1, 24)})
    names = sorted(p.name for p in run(s))
    assert names == ["S - Ch.001-010.cbz", "S - Ch.011-020.cbz", "S - Ch.021-030.cbz"]


def test_real_volumes_when_every_chapter_tagged(tmp_path):
    s = make_series(tmp_path / "S", {
        "Vol.01 Ch.001": 1, "Vol.01 Ch.002": 1, "Vol.02 Ch.003": 1})
    assert sorted(p.name for p in run(s)) == ["S - Vol.01.cbz", "S - Vol.02.cbz"]


def test_partial_vol_tags_fall_back_to_blocks(tmp_path):
    s = make_series(tmp_path / "S", {"Vol.01 Ch.001": 1, "Ch.002": 1})
    assert [p.name for p in run(s)] == ["S - Ch.001-010.cbz"]


def test_chunk_mode_ignores_volumes(tmp_path):
    s = make_series(tmp_path / "S", {"Vol.01 Ch.001": 1, "Vol.02 Ch.002": 1})
    assert [p.name for p in run(s, mode="chunk", size=5)] == ["S - Ch.001-005.cbz"]


# ------------------------------------------------------------------ formats
def test_mixed_formats_and_true_extensions(tmp_path):
    s = make_series(tmp_path / "S", {"Ch.001": {
        "1.jpg": JPG, "2.webp": WEBP, "3.jpg": WEBP,   # 3 is a mislabeled webp
        "4.webp": JPG, "5.JPG": PNG, "6.jpeg": JPG,
    }})
    [cbz] = run(s)
    with zipfile.ZipFile(cbz) as zf:
        names = [n for n in zf.namelist() if n != "ComicInfo.xml"]
    assert names == ["001_001.jpg", "001_002.webp", "001_003.webp",
                     "001_004.jpg", "001_005.png", "001_006.jpg"]


def test_mislabeled_and_duplicate_pages_are_warned(tmp_path):
    s = make_series(tmp_path / "S", {"Ch.001": {"1.jpg": WEBP, "2.jpg": JPG, "2.webp": WEBP}})
    warnings = m.scan(s).warnings
    assert any("wrong extension" in w for w in warnings)
    assert any("same page number" in w for w in warnings)


def test_junk_files_ignored(tmp_path):
    s = make_series(tmp_path / "S", {"Ch.001": {
        "1.jpg": JPG, "._1.jpg": b"junk", "Thumbs.db": b"x", "info.txt": b"x"}})
    [cbz] = run(s)
    with zipfile.ZipFile(cbz) as zf:
        assert [n for n in zf.namelist() if n != "ComicInfo.xml"] == ["001_001.jpg"]


def test_over_999_pages_still_sort(tmp_path):
    s = make_series(tmp_path / "S", {"Ch.001": 1001})
    [cbz] = run(s)
    with zipfile.ZipFile(cbz) as zf:
        names = [n for n in zf.namelist() if n != "ComicInfo.xml"]
    assert names == sorted(names) and names[-1] == "001_1001.jpg"


# ------------------------------------------------------------------ archive
def test_cbz_is_uncompressed_and_has_comicinfo(tmp_path):
    s = make_series(tmp_path / "S", {"Vol.02 Ch.007 - Rain": 2})
    [cbz] = run(s)
    with zipfile.ZipFile(cbz) as zf:
        assert {i.compress_type for i in zf.infolist()} == {zipfile.ZIP_STORED}
        info = zf.read("ComicInfo.xml").decode()
    assert "<Series>S</Series>" in info and "<Volume>2</Volume>" in info
    assert "<Title>Rain</Title>" in info and "<PageCount>2</PageCount>" in info


def test_originals_untouched(tmp_path):
    s = make_series(tmp_path / "S", {"Ch.001": 3})
    before = {p: p.read_bytes() for p in s.rglob("*") if p.is_file()}
    run(s)
    assert {p: p.read_bytes() for p in s.rglob("*") if p.is_file()} == before


# ------------------------------------------------------------------ re-runs
def test_rerun_skips_unchanged_and_updates_changed(tmp_path):
    s = make_series(tmp_path / "S", {"Ch.001": 1, "Ch.011": 1})
    assert len(run(s)) == 2
    assert run(s) == []                                   # nothing changed
    make_series(s, {"Ch.012": 1})                         # new chapter arrives
    assert [p.name for p in run(s)] == ["S - Ch.011-020.cbz"]
    assert len(run(s, overwrite=True)) == 2


def test_dry_run_writes_nothing(tmp_path):
    s = make_series(tmp_path / "S", {"Ch.001": 1})
    run(s, dry_run=True)
    assert not (tmp_path / "S (CBZ)").exists()


def test_custom_output_folder(tmp_path):
    s = make_series(tmp_path / "S", {"Ch.001": 1})
    [cbz] = run(s, out_dir=tmp_path / "out")
    assert cbz.parent == tmp_path / "out"


# ------------------------------------------------------------------ cancel
def test_cancel_leaves_no_partial_files(tmp_path):
    s = make_series(tmp_path / "S", {f"Ch.{i:03d}": 5 for i in range(1, 21)})
    stop = threading.Event()

    def progress(done, total, name):
        if done == 60:              # block 1 = 50 pages, so this is inside block 2
            stop.set()

    with pytest.raises(m.Cancelled):
        run(s, progress=progress, cancel=stop)
    out = sorted(p.name for p in (tmp_path / "S (CBZ)").iterdir())
    assert out == ["S - Ch.001-010.cbz"]                  # finished one kept, no .part


def test_errors(tmp_path):
    with pytest.raises(NotADirectoryError):
        run(tmp_path / "missing")
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError):
        run(tmp_path / "empty")
