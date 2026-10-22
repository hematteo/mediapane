# mediapane-diff

A [mediapane](https://github.com/hematteo/mediapane) plugin: compare two images in the pane.

```sh
mediapane diff before.png after.png            # side by side, flicker, difference heatmap
mediapane diff a.png b.png --mode heat         # just one of: side, flicker, heat
mediapane diff <(git show HEAD~1:fig.png) fig.png --name fig.png
```

The views arrive as one series (one gallery entry, stepped with ←/→):

* **side by side**, labelled A and B
* **flicker**: A and B alternating at 2 fps, which makes small shifts jump out
* **difference**: per-pixel absolute difference (the largest over channels) in heat colours over
  a dimmed A. The scale is stretched to the largest difference, so even a single changed level
  shows.

It prints a one-line summary (`fig.png: 1.33% of pixels differ (max 215/255, mean 2.87)`, or
`identical`). Animations are compared by their first frame; if the sizes differ, B is scaled to
A's size. Exit status: 0 on success, 1 if no pane is reachable, 2 if an input can't be read.

## As a git difftool

```sh
git config --global difftool.mediapane.cmd 'mediapane diff "$LOCAL" "$REMOTE" --name "$MERGED"'
git difftool -y -t mediapane -- fig.png
git difftool -y -t mediapane HEAD~3 -- '*.png'
```

For a file that was added or deleted, git passes `/dev/null` for the missing side, and only the
side that exists is shown.

## Install

```sh
uv tool install mediapane --with mediapane-diff
```

Development: `uv sync && uv run pytest` in this directory.
