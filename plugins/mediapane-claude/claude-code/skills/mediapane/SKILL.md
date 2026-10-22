---
name: mediapane
description: Show images, plots, GIFs and videos to the user in their mediapane viewer (`mp FILE`) instead of reading them, and look at images cheaply (`mediapane peek FILE`).
---

# mediapane

The user has a media pane open beside this terminal. Showing them a file there costs you almost
nothing; reading an image yourself costs about width × height / 750 tokens (~1,800 for a
typical plot).

- **Show the user an image, plot, GIF or video:** `mp path/to/file`. Don't read it first just to
  show it. It prints nothing on success.
- **Related images** (training steps, a sweep, before/after): `mp --tag NAME --step N file`
  makes one entry the user steps through. From Python:
  `mediapane.show(figure_or_array_or_tensor, tag="NAME", step=N)`.
- **Look at an image yourself:** `mediapane peek file.png` prints the path of a copy at most
  768 px on its longest side (a video becomes a 2×2 sheet of frames); read that path instead of
  the original. Add `--max 1024` only when fine detail matters.
- **Compare two images:** `mediapane diff a.png b.png` (if the diff plugin is installed),
  otherwise `mp a.png b.png`.
- Files you write, edit or read are shown automatically when mediapane's hook is installed, and
  so are images that a command you ran created and named; don't `mp` those again.
- If `mp` says no pane is running, tell the user once (`mediapane open` starts one) and carry on.
