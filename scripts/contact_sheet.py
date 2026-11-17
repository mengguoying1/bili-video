#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""contact_sheet.py — dump N evenly spaced frames into one annotated grid image.

First thing to run on an unknown video: it answers "is there a watermark at all,
and is it fixed or does it move?" in a single glance, before spending minutes on
inpainting.

Usage
  python contact_sheet.py IN.mp4 sheet.jpg            # 12 frames
  python contact_sheet.py IN.mp4 sheet.jpg 24         # denser sampling
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

import cv2
import numpy as np


def find_ffmpeg() -> str:
    import shutil
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        raise SystemExit("ffmpeg not found")


def duration_of(ff: str, src: str) -> float:
    p = subprocess.run([ff, "-hide_banner", "-i", src], capture_output=True, text=True,
                       errors="replace")
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", p.stderr)
    if not m:
        return 60.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        raise SystemExit(__doc__)
    src = argv[0]
    out = argv[1] if len(argv) > 1 else os.path.splitext(src)[0] + "_sheet.jpg"
    n = int(argv[2]) if len(argv) > 2 else 12
    ff = find_ffmpeg()
    dur = duration_of(ff, src)

    frames = []
    for i in range(n):
        ts = dur * (i + 0.5) / n
        q = subprocess.run([ff, "-v", "error", "-ss", "%.2f" % ts, "-i", src, "-frames:v", "1",
                            "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True)
        img = cv2.imdecode(np.frombuffer(q.stdout, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            continue
        img = cv2.resize(img, (426, 240), interpolation=cv2.INTER_AREA)
        cv2.putText(img, "%.1fs" % ts, (6, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (0, 0, 255), 2, cv2.LINE_AA)
        frames.append(img)

    cols = 4
    rows = (len(frames) + cols - 1) // cols
    blank = np.zeros((240, 426, 3), np.uint8)
    frames += [blank] * (rows * cols - len(frames))
    grid = np.vstack([np.hstack(frames[r * cols:(r + 1) * cols]) for r in range(rows)])
    ok, buf = cv2.imencode(".jpg", grid, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    with open(out, "wb") as f:
        f.write(buf.tobytes())
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
