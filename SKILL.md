---
name: bili-video
description: >
  Download a B站/bilibili video and strip its embedded logo/watermark, end to end.
  MUST USE when the user says 下载这个B站视频 / 帮我把这个视频下下来 / BV号 / bilibili 链接 /
  去水印 / 去台标 / 抠掉水印 / 壁纸视频 / 搬运去重 / download this bilibili video /
  remove watermark from video / dewatermark / 视频去水印 / 无水印版本.
  Also use for 随便给我下个 B站视频, 这些视频批量下, 分P下载, or when a bilibili URL is
  pasted with any download / cleanup intent.
  含三个脚本：WBI 签名下载（绕开 yt-dlp 的 412）、浏览器 cookie 提取（上 1080P+）、
  静态水印字形级 inpaint 去除（不留矩形补丁痕迹）。
---

# bili-video — B站下载 + 内嵌水印去除

目标：一条命令拿到成片，不留"糊了一块"的痕迹。

## 目录

```
scripts/bili_fetch.py        # WBI 签名下载（durl 720P / dash 1080P+，多分P，音轨保留）
scripts/bili_cookie_grab.py  # 从本机 Chrome/Edge 扒 SESSDATA（上 1080P+ 用）
scripts/wm_dewatermark.py    # 自动定位 + 字形级 inpaint 去水印
scripts/contact_sheet.py     # 全片缩略图网格：一眼判断有没有水印、水印是否固定
```

依赖：`requests`、`opencv-python`、`numpy`、`pycryptodome`、`imageio-ffmpeg`（自带 ffmpeg 二进制，
找不到 `ffmpeg` 时自动用它）。无第三方 B站 SDK。

## 30 秒上手

```bash
S=.agents/skills/bili-video/scripts       # 或 ~/.agents/skills/bili-video/scripts

python $S/bili_fetch.py BV1Trs363ExF -o ./out        # 下载（匿名=720P）
python $S/wm_dewatermark.py "./out/xxx_720P.mp4"     # 去水印 -> xxx_720P_nowm.mp4
```

先 `--preview` 看检测结果，再跑全片：

```bash
python $S/wm_dewatermark.py IN.mp4 --preview          # 打印 auto bbox + 导出 mask/裁剪图
python $S/wm_dewatermark.py IN.mp4 --rect 15,15,217,59 # 自动不准时手动给框
```

## 流程

1. **解析目标**：URL / BV / av / ep 号都吃；`?p=2` 自动取第 2P。
2. **判断能否上高画质** → 见下表。
3. **下载**：`bili_fetch.py` 自动选路：
   - 匿名 → `playurl` 带 `platform=html5&high_quality=1&fnval=1` → 拿到**已合流 mp4（durl，720P）**，
     比自己拼 dash 省事。
   - 登录（`--sessdata`）→ `fnval=4048&fourk=1` → dash，视频/音频分别下，ffmpeg `-c copy` 合流，
     画质名映射见脚本内 `QN_NAME`。codec 优先 avc（播放器/剪辑软件兼容性最好）。
4. **先判断到底有没有水印**：`python scripts/contact_sheet.py IN.mp4 sheet.jpg` 出一张全片缩略图
   网格（一张图看完 2 分钟片子）。B站投稿原片**通常没有**平台水印；画面里出现 logo 才说明是 UP 主
   压进去的（BV1Trs363ExF 的 `Cyjay08 + bilibili` 属于这种）。
   **`--preview` 报 auto-detection failed 时，先怀疑本来就没水印**——BV1z2iPBPEkE 全片干净，
   直接交付原片即可，不要硬塞 `--rect` 去糊一块本来干净的画面。
5. **去水印** → 见下节。6. **验收** → 见验收清单。

### 画质决策表

| 情况 | 最高可得 | 做法 |
|---|---|---|
| 未登录 | 720P（1280×720） | 直接用；durl 单文件 |
| 有 SESSDATA，普通号 | 1080P | `--sessdata "..."` |
| 大会员号 | 1080P+ / 1080P60 / 4K（看视频 `accept_quality`） | `--qn 127`（默认）|
| 需要原片 1440P/4K 但 B站只存到 1080P+ | — | 看简介里的网盘原图/原片 |

拿 SESSDATA：
```bash
python $S/bili_cookie_grab.py            # 自动扒 Chrome/Edge，打印 SESSDATA
# 或浏览器 F12 -> Application -> Cookies -> https://www.bilibili.com -> SESSDATA
python $S/bili_fetch.py BVxxx --sessdata "<SESSDATA>" -o ./out
# 也可以 export BILI_SESSDATA=... 后直接跑
```

## 去水印原理（为什么不留方块）

矩形 mask + delogo/inpaint = 一块明显发糊的方块 + 硬边。正确做法是只 mask **笔画**：

1. 均匀抽 12 帧，逐帧算"比局部邻域更亮的白细节"（`gray - blur(25)` 且 `gray>130` 且 `sat<85`
   且局部均值 `<230`，最后一条是为了不把白雾/白天空当水印）。
2. 跨帧**多数投票**（≥50%）取交集式结果：水印每帧都在同一位置 → 存活；背景细节会漂移 → 掉票。
   *注意：全帧 AND 会死，只要有一帧背景纯白（雾、天空）水印就被全清零——这是踩过的坑。*
3. `close(9×9)` 填掉 logo 的空心结构，`dilate(grow=7)` 吞掉抗锯齿边缘，但**不填满整个矩形**。
4. 逐帧 `cv2.inpaint(TELEA, radius=10)`，修复靠周围真实像素完成，所以没有矩形接缝。
5. mask 内注入 σ≈2.5 的颗粒噪声，避免"过于干净的一块"。
6. 原始帧 `rawvideo` 管道直进 libx264（**只压一次**），音轨 `-c:a copy` 原样保留。

### 参数调优

| 症状 | 调 |
|---|---|
| 边缘还有白色描边/鬼影 | `--grow 9` 或 `--grow 11` |
| 补丁太大、糊得明显 | `--grow 5`，`--radius 6` |
| 补丁区域过分光滑可疑 | `--noise 3.5` |
| 画面纹理复杂、糊了很假 | `--radius 4`（小半径多次不如一次大半径） |
| 自动框不准 | 手填 `--rect x,y,w,h`（先用 `--preview` 看导出图量） |
| 文件太大 | `--crf 20 --preset medium` |

### 已知边界

- 只处理**固定位置**水印。移动/漂移水印需要逐帧检测，现脚本不覆盖。
- **不是所有视频都有水印**：`--preview` 报 `auto-detection failed` 时先看 contact sheet 缩略图，
  确认真的干净就跳过处理，别为了"走了流程"去糊一块干净的画面。
- 水印落在极亮背景（纯白雾/天空）时，检测依赖相对亮度，偶尔会漏 1–2 px 抗锯齿边，加大 `--grow`。
- 只出现在片头的浮动水印：`ffmpeg -ss` 切段分别处理再 `concat`。
- 720P 源去水印后仍然是 720P；inpaint 不产生细节，不要期望"变清晰"。

## 坑位清单（都实测过）

1. **yt-dlp 读 B站会 412**（风控），别浪费时间，走 WBI API。
2. `playurl` 必须 WBI 签名（`w_rid` = md5(排序后 query + mixinKey)，mixinKey 由 `img_key+sub_key`
   按 64 位置换表取值），否则 412/-403。
3. 匿名 `fnval=4048` 的 dash 只给 480P，换 `platform=html5&high_quality=1&fnval=1` 才有 720P 合流 mp4。
4. 浏览器 cookie 库：运行中的 Edge 会**独占锁**（`ERROR_SHARING_VIOLATION`，连共享读都拒），
   Chrome 127+ 的 `v20` cookie 是 app-bound 加密、外部解不开 → 直接从 devtools 复制 SESSDATA 最快。
5. Windows 中文/空格路径下 `cv2.imwrite/imread` 会失败，必须走 `imencode + tofile`（脚本内 `imwrite_u`）。
6. PowerShell 不支持 heredoc（`python - <<'PY'`），把脚本写文件再跑。
7. `ffmpeg -i` 只看信息时会返回 exit 1，不是错误。
8. 输出名里的中文标点、双空格会烦人，`safe_name()` 已处理非法字符，但仍建议 `-o` 用干净目录。

## 验收清单

- [ ] `ffmpeg -i OUT.mp4` 时长与原片一致，`Stream #0:1` 音轨还在
- [ ] 抽 3–4 帧（开头/中间/结尾）目视：水印区域无残留、无矩形硬边
- [ ] 分辨率/帧率与原片相同（去水印不应降采样）
- [ ] 保留原始带水印文件做对照，`--preview` 生成的裁剪图可作前后对照

## 输出约定

```
out/
  <title>_720P.mp4            # 下载原片（带水印，留档）
  <title>_720P_nowm.mp4       # 去水印成片
  <title>.meta.json           # aid/cid/owner/dimension/画质/时间戳
  <title>_part01_wm_box.png   # 检测框特写（--preview）
```

## 实战样例（BV1Trs363ExF，2026-11）

- 源 2560×1440 37s；匿名只给 720P（durl 4.52 MB）；WBI 一次过。
- 水印：左上 `Cyjay08 + bilibili`，约 214×60 @ (18,18)，全片固定 → 内嵌（非播放器叠加）。
- 自动检测输出 bbox `(15,15,217,59)`，字形 mask 3507 px，扩后 8060 px（0.87% 画面）。
- 结果：1094 帧、`crf 16 preset slow`、音轨保留，水印区干净无接缝。
