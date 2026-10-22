import os

from mediapane.watch import Watcher


def test_reports_new_files_once_they_are_stable(tmp_path):
    (tmp_path / "old.png").write_bytes(b"x")
    w = Watcher([tmp_path])
    assert w.poll() == []  # existing files are skipped by default
    new = tmp_path / "new.png"
    new.write_bytes(b"abc")
    assert w.poll() == []  # first sighting: wait for it to settle
    assert w.poll() == [new]
    assert w.poll() == []


def test_changed_and_ignored_files(tmp_path):
    w = Watcher([tmp_path])
    (tmp_path / "notes.txt").write_text("hi")
    (tmp_path / ".hidden.png").write_bytes(b"x")
    f = tmp_path / "fig.png"
    f.write_bytes(b"1")
    w.poll()
    assert w.poll() == [f]
    f.write_bytes(b"22")
    os.utime(f, (1, 1))
    w.poll()
    assert w.poll() == [f]


def test_existing_flag(tmp_path):
    f = tmp_path / "a.gif"
    f.write_bytes(b"x")
    w = Watcher([tmp_path], existing=True)
    w.poll()
    assert w.poll() == [f]
