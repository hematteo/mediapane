from PIL import Image

from mediapane.media import probe, scaled_png, sniff_name


def test_png_is_used_as_is(png, tmp_path):
    m = probe(png, tmp_path / "w")
    assert m.kind == "image" and (m.width, m.height) == (300, 200) and m.still == png


def test_jpeg_gets_a_png_still(tmp_path):
    p = tmp_path / "x.jpg"
    Image.new("RGB", (40, 30)).save(p)
    m = probe(p, tmp_path / "w")
    assert m.kind == "image" and m.still and m.still.suffix == ".png"


def test_gif_frames(gif, tmp_path):
    m = probe(gif, tmp_path / "w")
    assert m.kind == "anim" and len(m.frames) == 6
    assert all(p.exists() for p, _ in m.frames)
    assert abs(sum(d for _, d in m.frames) - 0.3) < 1e-6
    assert m.label.startswith("▶ 6 frames")


def test_video(mp4, tmp_path):
    m = probe(mp4, tmp_path / "w")
    assert m.kind == "video" and (m.width, m.height) == (320, 180)
    assert m.still and m.still.exists()
    assert m.duration and abs(m.duration - 2.0) < 0.2
    assert m.label == "▶ 0:02"


def test_scaled_png(png):
    with Image.open(__import__("io").BytesIO(scaled_png(png, 150))) as im:
        assert im.size == (150, 100)


def test_sniff():
    assert sniff_name(b"\x89PNG....") == "stdin.png"
    assert sniff_name(b"GIF89a...") == "stdin.gif"
    assert sniff_name(b"\x00\x00\x00\x18ftypmp42") == "stdin.mp4"


def test_truncated_png_is_rejected_up_front(png, tmp_path):
    import pytest

    bad = tmp_path / "cut.png"
    bad.write_bytes(png.read_bytes()[:200])
    with pytest.raises(ValueError, match="damaged image"):
        probe(bad, tmp_path / "w")


def test_exif_rotation_is_applied(tmp_path):
    p = tmp_path / "phone.jpg"
    im = Image.new("RGB", (40, 20))
    exif = im.getexif()
    exif[0x0112] = 6  # rotate 90 degrees
    im.save(p, exif=exif)
    m = probe(p, tmp_path / "w")
    assert (m.width, m.height) == (20, 40)
