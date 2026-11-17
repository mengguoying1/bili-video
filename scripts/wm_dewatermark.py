#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""wm_dewatermark.py — remove a static (fixed-position) logo/watermark from video.

The watermark is almost never a clean rectangle: it is white glyphs sitting on a
moving background. Painting a filled rectangle with delogo/inpaint leaves a
visible soft block with a hard edge. This script instead:

  1. samples N frames and computes a white top-hat response per frame
     ("brighter than its local neighborhood"), intersects the responses across
     frames -> only details that are white and in the *same place* every frame
     survive = the watermark glyphs,
  2. morphologically closes the glyph mask (fills the hollow parts of a logo)
     and dilates it a few pixels to swallow anti-aliased edges,
  3. inpaints each frame with that mask only -> surrounding real pixels do the
     repair, so there is no rectangular seam,
  4. injects a little grain inside the mask so the repaired patch does not look
     suspiciously smooth,
  5. pipes raw frames straight into libx264 (one generation loss only) and keeps
     the original audio track untouched.

Usage
  python wm_dewatermark.py IN.mp4                       # auto-detect, auto out name
  python wm_dewatermark.py IN.mp4 --rect 6,2,250,82     # explicit watermark box
  python wm_dewatermark.py IN.mp4 --preview             # only write before/after PNG

Tuning
  --grow     dilate radius in px        (default 7; raise if any halo survives)
  --radius   inpaint radius             (default 10; raise for bigger smears)
  --noise    grain sigma 0..5           (default 2.5; 0 = off)
  --crf      x264 quality               (default 16; 18-20 = smaller file)
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time

import cv2
import numpy as np


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def imwrite_u(path: str, img) -> bool:
    """cv2.imwrite that also works for non-ASCII (Chinese) paths on Windows."""
    ext = os.path.splitext(path)[1] or ".png"
    ok, buf = cv2.imencode(ext, img)
    if not ok:
        return False
    with open(path, "wb") as f:
        f.write(buf.tobytes())
    return True


def find_ffmpeg() -> str:
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        raise SystemExit("ffmpeg not found: install it or `pip install imageio-ffmpeg`")


def find_ffprobe() -> str | None:
    p = shutil.which("ffprobe")
    if p:
        return p
    ff = find_ffmpeg()
    cand = os.path.join(os.path.dirname(ff), "ffprobe" + (".exe" if os.name == "nt" else ""))
    return cand if os.path.exists(cand) else None


def probe(ff: str, src: str) -> tuple[int, int, float]:
    """Return (width, height, fps) using ffmpeg's stderr banner."""
    p = subprocess.run([ff, "-hide_banner", "-i", src], capture_output=True, text=True,
                       errors="replace")
    txt = p.stderr
    import re
    w = h = 0
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", txt)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
    fps = 30.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*fps", txt)
    if m:
        fps = float(m.group(1))
    else:
        m = re.search(r"(\d+(?:\.\d+)?)\s*tbr", txt)
        if m:
            fps = float(m.group(1))
    if not (w and h):
        raise SystemExit("could not read video size from ffmpeg output")
    return w, h, fps


def grab(ff: str, src: str, ts: float) -> np.ndarray | None:
    p = subprocess.run([ff, "-v", "error", "-ss", "%.3f" % ts, "-i", src, "-frames:v", "1",
                        "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True)
    if not p.stdout:
        return None
    return cv2.imdecode(np.frombuffer(p.stdout, np.uint8), cv2.IMREAD_COLOR)


# ---------------------------------------------------------------- detection
def _white_detail(roi: np.ndarray, thresh: int = 20) -> np.ndarray:
    """Pixels that are white-ish AND brighter than their local neighborhood.

    A pure-global threshold fails on bright frames (white sky merges with the
    watermark), so the watermark test is *local* contrast, not absolute value.
    """
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    local = cv2.blur(gray, (25, 25))
    diff = cv2.subtract(gray, local)  # gray - localmean, clamped at 0
    sat = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)[:, :, 1]
    return (gray > 130) & (sat < 85) & (diff > thresh) & (local < 230)


def detect_glyph_mask(frames: list[np.ndarray], box: tuple[int, int, int, int] | None,
                      thresh: int = 20, vote_frac: float = 0.5) -> np.ndarray:
    """Majority vote of per-frame white detail responses inside `box`.

    All-frames-AND is too brittle: one bright frame (white fog / sky) kills the
    whole glyph. A >=50% vote keeps the watermark (it is present in *every*
    frame) while random background detail does not survive the vote.
    """
    h, w = frames[0].shape[:2]
    x, y, bw, bh = box if box else (0, 0, w, h)
    acc = np.zeros((bh, bw), np.uint16)
    for f in frames:
        acc += _white_detail(f[y:y + bh, x:x + bw], thresh).astype(np.uint16)
    need = max(1, int(round(len(frames) * vote_frac)))
    mask = ((acc >= need).astype(np.uint8)) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    return mask


def auto_bbox(frames: list[np.ndarray]) -> tuple[int, int, int, int] | None:
    """Locate a static bright emblem near a frame edge."""
    h, w = frames[0].shape[:2]
    m = detect_glyph_mask(frames, None, vote_frac=0.55)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(
        cv2.dilate(m, np.ones((7, 7), np.uint8)), connectivity=8)
    boxes = []
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if area < 250 or bw < 10 or bh < 6:
            continue
        if bw > w * 0.85 or bh > h * 0.85:
            continue
        # frame-edge prior: watermarks live near an edge
        near_edge = (x < w * 0.4 or y < h * 0.4 or x + bw > w * 0.6 or y + bh > h * 0.6)
        if not near_edge:
            continue
        boxes.append((area, x, y, bw, bh))
    if not boxes:
        return None
    boxes.sort(reverse=True)
    _, x0, y0, w0, h0 = boxes[0]
    X0, Y0, X1, Y1 = x0, y0, x0 + w0, y0 + h0
    for area, x, y, bw, bh in boxes[1:8]:
        if x <= X1 + 60 and x + bw >= X0 - 60 and y <= Y1 + 30 and y + bh >= Y0 - 30:
            X0, Y0 = min(X0, x), min(Y0, y)
            X1, Y1 = max(X1, x + bw), max(Y1, y + bh)
    bw_total, bh_total = X1 - X0, Y1 - Y0
    if bw_total * bh_total > w * h * 0.25:
        log("[mask] auto bbox too large (%dx%d), refusing" % (bw_total, bh_total))
        return None
    x0 = max(0, X0 - 6); y0 = max(0, Y0 - 6)
    return (x0, y0, min(w - x0, bw_total + 12), min(h - y0, bh_total + 12))


# ---------------------------------------------------------------- processing
def build_mask(ff, src, w, h, fps, box, grow, n_samples=12, duration=None):
    if duration is None:
        p = subprocess.run([ff, "-hide_banner", "-i", src], capture_output=True, text=True,
                           errors="replace")
        import re
        m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", p.stderr)
        duration = (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))) if m else 30.0
    ts_list = [duration * (i + 0.5) / n_samples for i in range(n_samples)]
    frames = [f for f in (grab(ff, src, t) for t in ts_list) if f is not None]
    if not frames:
        raise SystemExit("could not sample frames")
    log("[mask] sampled %d frames, duration=%.1fs" % (len(frames), duration))

    if box is None:
        box = auto_bbox(frames)
        if box is None:
            raise SystemExit("auto-detection failed: pass --rect x,y,w,h")
        log("[mask] auto bbox = %s" % (box,))
    x, y, bw, bh = box
    glyph = detect_glyph_mask(frames, (x, y, bw, bh))
    log("[mask] glyph px=%d inside box" % int((glyph > 0).sum()))

    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (grow * 2 + 1, grow * 2 + 1))
    grown = cv2.dilate(glyph, k)
    full = np.zeros((h, w), np.uint8)
    full[y:y + bh, x:x + bw] = grown
    log("[mask] final px=%d (%.2f%% of frame)" % (int((full > 0).sum()),
                                                  100.0 * (full > 0).sum() / (w * h)))
    return full, (x, y, bw, bh)


def encode_clean(ff, src, out, mask, w, h, fps, radius, noise, crf, preset, duration):
    mask_bool = mask > 0
    dec = subprocess.Popen([ff, "-v", "error", "-i", src, "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                           stdout=subprocess.PIPE, bufsize=w * h * 3 * 2)
    enc = subprocess.Popen([ff, "-v", "error", "-y",
                            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", "%dx%d" % (w, h),
                            "-r", ("%g" % fps), "-i", "-", "-i", src,
                            "-map", "0:v:0", "-map", "1:a:0?", "-shortest",
                            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                            "-pix_fmt", "yuv420p", "-profile:v", "high",
                            "-color_primaries", "bt709", "-color_trc", "bt709",
                            "-colorspace", "bt709",
                            "-c:a", "copy", "-movflags", "+faststart", out],
                           stdin=subprocess.PIPE, bufsize=w * h * 3 * 2)
    rng = np.random.default_rng(1234)
    grain = rng.normal(0, noise, (h, w, 3)).astype(np.int16) if noise > 0 else None
    nbytes = w * h * 3
    i = 0
    t0 = time.time()
    while True:
        buf = dec.stdout.read(nbytes)
        if len(buf) < nbytes:
            break
        img = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
        out_f = cv2.inpaint(img, mask, radius, cv2.INPAINT_TELEA)
        if grain is not None:
            tmp = out_f.astype(np.int16)
            tmp[mask_bool] = np.clip(tmp[mask_bool] + grain[mask_bool], 0, 255)
            out_f = tmp.astype(np.uint8)
        enc.stdin.write(out_f.tobytes())
        i += 1
        if i % 300 == 0:
            log("   %d frames  %.1fs" % (i, time.time() - t0))
    enc.stdin.close()
    dec.stdout.close()
    dec.wait()
    enc.wait()
    log("[encode] %d frames -> %s (%.1fs)" % (i, out, time.time() - t0))
    return i


def write_preview(ff, src, out_png, box, at=1.0):
    img = grab(ff, src, at)
    if img is None:
        return
    x, y, w, h = box
    crop = img[max(0, y - 10):y + h + 10, max(0, x - 10):x + w + 10]
    if crop.size:
        scale = max(1, min(4, 1200 // max(crop.shape[1], 1)))
        big = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
        imwrite_u(out_png, big)
        log("[preview]", out_png)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Remove a static watermark from video")
    ap.add_argument("input")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--rect", default=None, help="watermark box: x,y,w,h")
    ap.add_argument("--grow", type=int, default=7)
    ap.add_argument("--radius", type=int, default=10)
    ap.add_argument("--noise", type=float, default=2.5)
    ap.add_argument("--crf", type=int, default=16)
    ap.add_argument("--preset", default="slow")
    ap.add_argument("--samples", type=int, default=12, help="frames used for detection")
    ap.add_argument("--preview", action="store_true", help="only dump mask/crop, no encode")
    ap.add_argument("--mask-out", default=None, help="save the mask as .npy + .png")
    args = ap.parse_args(argv)

    src = args.input
    if not os.path.exists(src):
        raise SystemExit("no such file: " + src)
    ff = find_ffmpeg()
    w, h, fps = probe(ff, src)
    log("[probe] %dx%d @ %.3g fps" % (w, h, fps))

    box = tuple(int(v) for v in args.rect.split(",")) if args.rect else None
    mask, used_box = build_mask(ff, src, w, h, fps, box, args.grow, args.samples)
    if args.mask_out:
        base = os.path.splitext(args.mask_out)[0]
        np.save(base + ".npy", mask)
        imwrite_u(base + ".png", mask)
        log("[mask] saved", base + ".npy/.png")
    if args.preview:
        write_preview(ff, src, os.path.splitext(src)[0] + "_wm_box.png", used_box)
        return 0

    out = args.out or (os.path.splitext(src)[0] + "_nowm.mp4")
    encode_clean(ff, src, out, mask, w, h, fps, args.radius, args.noise, args.crf,
                 args.preset, None)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

