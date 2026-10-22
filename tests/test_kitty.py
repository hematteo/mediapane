import base64
import re

from mediapane.kitty import (
    CHUNK,
    DIACRITICS,
    MAX_CELLS,
    PLACEHOLDER,
    Graphics,
    make_id,
    placeholder_row,
    tmux_wrap,
)


def test_diacritics_table_is_complete():
    assert MAX_CELLS == 297
    assert len(set(DIACRITICS)) == 297
    assert DIACRITICS[0] == "̅" and DIACRITICS[-1] == "\U0001d244"


def test_ids_fit_256_colour_and_high_byte():
    assert make_id(0) == 1
    assert make_id(254) == 255
    assert make_id(255) == (1 << 24) | 1  # low byte never 0, top byte rolls over
    ids = {make_id(n) for n in range(2000)}
    assert len(ids) == 2000


def test_placeholder_row_encodes_id_row_and_columns():
    row = placeholder_row(make_id(4), 2, 3)
    assert row.startswith("\x1b[38;5;5m") and row.endswith("\x1b[39m")
    cells = row.removeprefix("\x1b[38;5;5m").removesuffix("\x1b[39m")
    assert cells == "".join(PLACEHOLDER + DIACRITICS[2] + DIACRITICS[c] for c in range(3))


def test_placeholder_row_high_byte_uses_third_diacritic():
    image_id = make_id(255 * 3 + 7)  # high byte 3
    row = placeholder_row(image_id, 0, 1)
    assert row == f"\x1b[38;5;8m{PLACEHOLDER}{DIACRITICS[0]}{DIACRITICS[0]}{DIACRITICS[3]}\x1b[39m"


def test_file_transfer_sends_the_path():
    g = Graphics(direct=False, tmux=False)
    seq = g.place(7, 10, 4, path="/tmp/x.png")
    m = re.fullmatch(r"\x1b_G(.*);(.*)\x1b\\", seq)
    assert m
    assert "t=f" in m.group(1) and "U=1" in m.group(1) and "i=7,c=10,r=4" in m.group(1)
    assert base64.b64decode(m.group(2)) == b"/tmp/x.png"


def test_direct_transfer_chunks_and_reassembles():
    g = Graphics(direct=True, tmux=False)
    payload = bytes(range(256)) * 50  # > one chunk once base64-encoded
    seq = g.place(9, 5, 5, png=payload)
    parts = re.findall(r"\x1b_G([^;]*);([^\x1b]*)\x1b\\", seq)
    assert len(parts) > 1
    assert "t=d" in parts[0][0] and "m=1" in parts[0][0]
    assert all(ctl == "q=2,m=1" for ctl, _ in parts[1:-1])
    assert parts[-1][0] == "q=2,m=0"
    assert all(len(data) <= CHUNK for _, data in parts)
    assert base64.b64decode("".join(data for _, data in parts)) == payload


def test_tmux_wrap_doubles_escapes():
    assert tmux_wrap("\x1b_Ga=d\x1b\\") == "\x1bPtmux;\x1b\x1b_Ga=d\x1b\x1b\\\x1b\\"
    g = Graphics(direct=False, tmux=True)
    assert g.delete_all().startswith("\x1bPtmux;")
