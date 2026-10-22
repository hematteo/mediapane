"""File extensions mediapane treats as media (no imports, so it's cheap to use anywhere)."""

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".apng"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS
