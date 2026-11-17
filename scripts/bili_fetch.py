#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""bili_fetch.py — download a bilibili video (single part or whole multi-P) reliably.

Why not yt-dlp: bilibili's risk control answers yt-dlp with HTTP 412 on most
networks. This script talks to the same web APIs the browser uses, with its own
WBI signature implementation, so it keeps working.

Two quality paths:
  * anonymous  -> x/player/wbi/playurl with platform=html5 & high_quality=1
                  returns a merged mp4 (durl) up to 720P. No login needed.
  * logged in  -> x/player/wbi/playurl with fnval=4048 returns DASH
                  (1080P/1080P+/4K depending on the account), video+audio are
                  downloaded separately and muxed with ffmpeg.

Usage
  python bili_fetch.py <BV/av/ep/URL> [-p N|all] [-o OUTDIR]
                       [--sessdata SESSDATA | --env BILI_SESSDATA]
                       [--qn 127] [--cookie-file cookies.txt] [--force-dash]

Examples
  python bili_fetch.py BV1Trs363ExF
  python bili_fetch.py "https://www.bilibili.com/video/BV1Trs363ExF/?p=2"
  python bili_fetch.py BV1Trs363ExF --sessdata "xxxx%2Cyyyy" -o D:\videos
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
API = "https://api.bilibili.com"

# WBI mixin key permutation table (bilibili front-end shipped constant)
MIXIN_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
             33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
             26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
             20, 34, 44, 52]

QN_NAME = {127: "8K", 126: "Dolby", 125: "HDR", 120: "4K", 116: "1080P60", 112: "1080P+",
           80: "1080P", 74: "720P60", 64: "720P", 32: "480P", 16: "360P", 6: "240P"}


# ---------------------------------------------------------------- helpers
def log(*a):
    print(*a, file=sys.stderr, flush=True)


def find_ffmpeg() -> str | None:
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def parse_target(s: str) -> tuple[str, int | None]:
    """Accept BV1xxx / av123 / ep123 / full URL. Returns (id_string, page)."""
    s = s.strip()
    page = None
    m = re.search(r"[?&]p=(\d+)", s)
    if m:
        page = int(m.group(1))
    for pat, kind in ((r"(BV[0-9A-Za-z]{10})", "bvid"),
                      (r"av(\d+)", "aid"),
                      (r"ep(\d+)", "epid"),
                      (r"ss(\d+)", "season")):
        m = re.search(pat, s)
        if m:
            val = m.group(1) if kind == "bvid" else m.group(1)
            return (val if kind == "bvid" else "%s:%s" % (kind, val)), page
    raise SystemExit("cannot parse a video id from: %r" % s)


class Bili:
    def __init__(self, sessdata: str | None = None):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        self.logged_in = False
        self._bootstrap(sessdata)

    def _bootstrap(self, sessdata):
        try:
            fp = self.s.get(API + "/x/frontend/finger/spi", timeout=20).json()["data"]
            self.s.cookies.set("buvid3", fp["b_3"], domain=".bilibili.com")
            self.s.cookies.set("buvid4", fp["b_4"], domain=".bilibili.com")
        except Exception as e:
            log("[warn] fingerprint failed:", e)
        if sessdata:
            self.s.cookies.set("SESSDATA", urllib.parse.unquote(sessdata), domain=".bilibili.com")
        self.s.get("https://www.bilibili.com/", timeout=20)
        nav = self.s.get(API + "/x/web-interface/nav", timeout=20).json()
        d = nav.get("data") or {}
        self.logged_in = bool(d.get("isLogin"))
        wbi = d.get("wbi_img") or {}
        self.img_key = wbi.get("img_url", "").rsplit("/", 1)[-1].split(".")[0]
        self.sub_key = wbi.get("sub_url", "").rsplit("/", 1)[-1].split(".")[0]
        log("[auth] isLogin=%s uname=%s" % (self.logged_in, d.get("uname")))

    def wbi_sign(self, params: dict) -> dict:
        mixin = "".join((self.img_key + self.sub_key)[i] for i in MIXIN_TAB)[:32]
        p = dict(params)
        p["wts"] = int(time.time())
        p = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in p.items()}
        q = urllib.parse.urlencode(sorted(p.items()))
        p["w_rid"] = hashlib.md5((q + mixin).encode()).hexdigest()
        return p

    def get(self, path, **params):
        r = self.s.get(API + path, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def resolve(self, target: str) -> dict:
        """Return {aid, bvid, title, owner, desc, pic, pages:[{cid,page,part,duration}]}"""
        if target.startswith("bvid:") or target.startswith("BV"):
            bv = target.split(":", 1)[-1]
            v = self.get("/x/web-interface/view", bvid=bv)
            d = v["data"]
        elif target.startswith("aid:"):
            d = self.get("/x/web-interface/view", aid=int(target.split(":", 1)[1]))["data"]
        elif target.startswith("epid:"):
            # bangumi: ep -> its play url carries cid/avid
            pg = self.get("/pgc/view/web/season", ep_id=int(target.split(":", 1)[1]))
            ep = pg["data"]["episodes"][0]
            d = {"aid": ep["aid"], "bvid": ep["bvid"], "title": ep["share_copy"],
                 "owner": {"name": pg["data"].get("season_title", "")}, "desc": "",
                 "pic": ep.get("cover"), "pages": [{"cid": ep["cid"], "page": 1,
                                                    "part": ep["share_copy"],
                                                    "duration": ep.get("duration", 0) / 1000}],
                 "duration": int(ep.get("duration", 0) / 1000),
                 "dimension": {"width": 0, "height": 0}}
        else:
            raise SystemExit("unsupported target: %s" % target)
        return d

    def playurl(self, aid: int, cid: int, want_dash: bool, qn: int = 127) -> dict:
        if want_dash:
            p = self.wbi_sign({"avid": aid, "cid": cid, "qn": qn, "fnval": 4048, "fnver": 0,
                               "fourk": 1, "otype": "json", "platform": "pc"})
        else:
            p = self.wbi_sign({"avid": aid, "cid": cid, "qn": qn, "fnval": 1, "fnver": 0,
                               "platform": "html5", "high_quality": 1, "otype": "json"})
        r = self.s.get(API + "/x/player/wbi/playurl", params=p, timeout=30).json()
        if r.get("code") != 0:
            log("[playurl] code=%s msg=%s" % (r.get("code"), r.get("message")))
            return {}
        return r.get("data") or {}


# ---------------------------------------------------------------- download
def stream_to_file(url: str, dst: str, session: requests.Session, referer="https://www.bilibili.com/"):
    headers = {"User-Agent": UA, "Referer": referer, "Range": "bytes=0-"}
    with session.get(url, headers=headers, stream=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        t0 = time.time()
        with open(dst, "wb") as f:
            for chunk in r.iter_content(1 << 19):
                f.write(chunk)
                done += len(chunk)
                if total > (1 << 20):
                    pct = done * 100 // total
                    if pct % 10 == 0:
                        log("   %3d%%  %.1f/%.1f MB  %.1f MB/s" % (
                            pct, done / 1e6, total / 1e6, done / 1e6 / max(time.time() - t0, .01)))
    return done


def safe_name(s: str, limit=70) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", s).strip(" .")
    return s[:limit] or "video"


def pick_dash(dash: dict):
    """Highest quality video stream, preferring avc for player compatibility."""
    vids = dash.get("video") or []
    if not vids:
        return None, None
    codec_rank = lambda c: (0 if c.startswith("avc") else 1 if c.startswith("hev") else 2)
    best = sorted(vids, key=lambda v: (-v.get("id", 0), codec_rank(v.get("codecs", "")),
                                       -v.get("bandwidth", 0)))[0]
    auds = dash.get("audio") or []
    audio = sorted(auds, key=lambda a: -a.get("bandwidth", 0))[0] if auds else None
    return best, audio


def main(argv=None):
    ap = argparse.ArgumentParser(description="Download bilibili video (no yt-dlp, WBI-signed)")
    ap.add_argument("target", help="BV id / av id / ep id / full URL")
    ap.add_argument("-o", "--out", default=os.getcwd(), help="output directory")
    ap.add_argument("-p", "--page", default=None,
                    help="1-based part number, or 'all' (default: the part in the URL, else all)")
    ap.add_argument("--sessdata", default=None, help="SESSDATA cookie for >=1080P")
    ap.add_argument("--env", default="BILI_SESSDATA", help="env var holding SESSDATA")
    ap.add_argument("--qn", type=int, default=127, help="requested qn for DASH (default 127 = max)")
    ap.add_argument("--force-dash", action="store_true", help="try DASH even when anonymous")
    ap.add_argument("--keep-parts", action="store_true", help="keep raw video/audio streams")
    args = ap.parse_args(argv)

    sessdata = args.sessdata or os.environ.get(args.env)
    b = Bili(sessdata)
    target, url_page = parse_target(args.target)
    info = b.resolve(target)
    aid = info["aid"]
    pages = info.get("pages") or [{"cid": info["cid"], "page": 1,
                                   "part": info.get("title", ""), "duration": info.get("duration", 0)}]
    log("[meta] %s | aid=%s | parts=%d | owner=%s" % (
        info.get("title"), aid, len(pages), (info.get("owner") or {}).get("name")))

    want = args.page if args.page is not None else (str(url_page) if url_page else
                                                    ("all" if len(pages) > 1 else "1"))
    if want == "all":
        sel = pages
    else:
        n = int(want)
        sel = [p for p in pages if p["page"] == n] or pages[:1]

    os.makedirs(args.out, exist_ok=True)
    ff = find_ffmpeg()
    results = []
    for pg in sel:
        cid = pg["cid"]
        base = safe_name(info.get("title", "video"))
        if len(pages) > 1:
            base = "%s_p%02d_%s" % (base, pg["page"], safe_name(pg.get("part", ""), 40))

        data = {}
        if b.logged_in or args.force_dash:
            data = b.playurl(aid, cid, True, args.qn)
        if not data.get("dash"):
            data = b.playurl(aid, cid, False, 112)  # anonymous 720P merged mp4
        if not data:
            log("[fail] no playurl for cid", cid)
            continue

        if data.get("dash"):
            v, a = pick_dash(data["dash"])
            if not v:
                log("[fail] empty dash")
                continue
            qn = v.get("id")
            log("[dash] cid=%s quality=%s(%s) %sx%s codec=%s" % (
                cid, qn, QN_NAME.get(qn, "?"), v.get("width"), v.get("height"), v.get("codecs")))
            vpath = os.path.join(args.out, base + ".%s.video.m4s" % qn)
            apath = os.path.join(args.out, base + ".%s.audio.m4s" % qn) if a else None
            stream_to_file(v["baseUrl"], vpath, b.s)
            if a:
                stream_to_file(a["baseUrl"], apath, b.s)
            out = os.path.join(args.out, "%s_%s.mp4" % (base, QN_NAME.get(qn, str(qn))))
            if ff:
                cmd = [ff, "-v", "error", "-y", "-i", vpath]
                if apath:
                    cmd += ["-i", apath]
                cmd += ["-c", "copy", "-movflags", "+faststart", out]
                subprocess.run(cmd, check=True)
                log("[mux] ->", out)
            else:
                log("[warn] ffmpeg not found: kept raw streams, merge them yourself")
                out = vpath
            if not args.keep_parts:
                for f in (vpath, apath):
                    if f and os.path.exists(f):
                        os.remove(f)
            results.append({"file": out, "quality": QN_NAME.get(qn, str(qn)), "cid": cid})
        else:
            durls = data.get("durl") or []
            qn = data.get("quality")
            log("[durl] cid=%s quality=%s(%s) segments=%d" % (
                cid, qn, QN_NAME.get(qn, "?"), len(durls)))
            parts = []
            for i, seg in enumerate(durls, 1):
                fp = os.path.join(args.out, "%s.part%02d.mp4" % (base, i))
                stream_to_file(seg["url"], fp, b.s)
                parts.append(fp)
            if len(parts) == 1:
                out = os.path.join(args.out, "%s_%s.mp4" % (base, QN_NAME.get(qn, str(qn))))
                os.replace(parts[0], out)
            else:
                out = os.path.join(args.out, "%s_%s.mp4" % (base, QN_NAME.get(qn, str(qn))))
                lst = os.path.join(args.out, base + ".concat.txt")
                with open(lst, "w", encoding="utf-8") as f:
                    for p in parts:
                        f.write("file '%s'\n" % p.replace("\\", "/"))
                subprocess.run([ff, "-v", "error", "-y", "-f", "concat", "-safe", "0",
                                "-i", lst, "-c", "copy", out], check=True)
                os.remove(lst)
                for p in parts:
                    os.remove(p)
            log("[done] ->", out)
            results.append({"file": out, "quality": QN_NAME.get(qn, str(qn)), "cid": cid})

    meta = {"aid": aid, "title": info.get("title"),
            "owner": (info.get("owner") or {}).get("name"),
            "desc": info.get("desc"), "cover": info.get("pic"),
            "duration": info.get("duration"), "dimension": info.get("dimension"),
            "source_url": "https://www.bilibili.com/video/%s" % info.get("bvid", ""),
            "logged_in": b.logged_in, "files": results,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    mp = os.path.join(args.out, safe_name(info.get("title", "video")) + ".meta.json")
    with open(mp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    log("[meta] saved", mp)
    print(json.dumps(results, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
