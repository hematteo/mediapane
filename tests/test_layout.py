from mediapane.layout import Line, clean, clip, fit, gallery_lines, parse_keys, width


def test_fit_keeps_aspect_and_bounds():
    # 2:1 image, square 10x20 px cells: width-limited
    c, r = fit(2000, 1000, 100, 100, 10, 20)
    assert (c, r) == (100, 25)
    # height-limited
    c, r = fit(1000, 1000, 100, 10, 10, 20)
    assert r == 10 and c == 20


def test_fit_caps_upscaling():
    c, r = fit(100, 50, 200, 200, 10, 20, upscale=2.0)
    assert (c, r) == (20, 5)  # 200 x 100 px at most


def test_fit_degenerate():
    assert fit(0, 10, 10, 10, 10, 20) == (0, 0)
    assert fit(10, 10, 0, 10, 10, 20) == (0, 0)


def test_gallery_lines_and_starts():
    lines, starts = gallery_lines([(10, 2), (0, 0), (5, 1)])
    assert starts == [0, 4, 6]
    assert lines[0] == Line(0, "gap") and lines[1] == Line(0, "caption")
    assert lines[2:4] == [Line(0, "image", 0), Line(0, "image", 1)]
    assert lines[4:6] == [Line(1, "gap"), Line(1, "caption")]  # no preview: caption only
    assert lines[-1] == Line(2, "image", 0)


def test_parse_keys():
    keys, rest = parse_keys("jk\x1b[A\x1b[B\x1b[5~\x1b[6~\x1bOH\r \x1b[Z\x1bq")
    assert keys == ["j", "k", "up", "down", "pgup", "pgdn", "home", "enter", " ", "btab", "esc",
                    "q"]  # fmt: skip
    assert rest == ""


def test_parse_mouse():
    keys, _ = parse_keys("\x1b[<65;10;5M\x1b[<0;3;7m")
    assert keys == [("mouse", 65, 9, 4, True), ("mouse", 0, 2, 6, False)]


def test_split_sequences_are_kept_for_the_next_read():
    keys, rest = parse_keys("j\x1b[<65;1")
    assert keys == ["j"] and rest == "\x1b[<65;1"
    keys, rest = parse_keys(rest + "0;5M")
    assert keys == [("mouse", 65, 9, 4, True)] and rest == ""
    assert parse_keys("\x1b") == ([], "\x1b")  # maybe Esc, maybe the start of a sequence


def test_focus_and_paste_markers():
    keys, _ = parse_keys("\x1b[I\x1b[O\x1b[200~xq\x1b[201~")
    assert keys == ["focus_in", "focus_out", "paste_start", "x", "q", "paste_end"]


def test_clean_strips_control_and_surrogates():
    assert clean("a\x1b]0;pwn\x07b") == "a\ufffd]0;pwn\ufffdb"
    assert clean("\udcff.png") == "?.png"
    assert clean("\x9b31m") == "\ufffd31m"  # C1 CSI too


def test_width_and_clip():
    assert width("abc") == 3 and width("日本") == 4 and width("\x1b[1mab\x1b[0m") == 2
    assert clip("日本語", 5) == "日本\x1b[0m"
    assert clip("\x1b[1mabcdef", 3) == "\x1b[1mabc\x1b[0m"
