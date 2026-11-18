# bili-video

B站视频下载 + 内嵌水印去除，一套可直接调用的脚本，也是一份 agent skill。

两件事它做得比较干净：

1. **下载**不走 `yt-dlp`。B站风控对 `yt-dlp` 直接回 `412`（换版本、挂代理、带 cookie 都一样），
   本仓库自己实现 WBI 签名直连 web API，匿名拿 720P 合流 mp4，登录可上 1080P / 1080P+ / 4K。
2. **去水印**不画矩形。矩形 + `delogo` / `inpaint` 会留下发糊的方块和硬边；这里只遮水印笔画，
   修复用的是周围真实像素，不产生矩形接缝。

## 特性

- 支持 `BV id` / `av` / `ep` / 完整 URL，自动识别 `?p=N` 分P，支持整部多分P下载
- 匿名 720P（durl 已合流，单文件）；带 `SESSDATA` 自动切 DASH 走最高画质，ffmpeg `-c copy` 合流
- codec 优先 avc，兼容播放器和剪辑软件
- 去水印：自动定位水印（多数投票 + 局部白细节），自动生成字形 mask，逐帧 inpaint，
  音轨原样保留，只压一次编码
- 附带浏览器 cookie 提取（DPAPI + AES-GCM，无需 pywin32），用于上 1080P+

## 安装

```bash
git clone https://github.com/mengguoying1/bili-video.git
cd bili-video
pip install -r requirements.txt
```

`ffmpeg` 在 PATH 里最好；没有的话脚本会自动用 `imageio-ffmpeg` 自带的二进制。

## 用法

### 下载

```bash
python scripts/bili_fetch.py BV1xxxxxxxxx -o ./out
python scripts/bili_fetch.py "https://www.bilibili.com/video/BV1xxxxxxxxx/?p=2"
python scripts/bili_fetch.py BV1xxxxxxxxx -p all -o ./out           # 整部多分P
python scripts/bili_fetch.py BV1xxxxxxxxx --sessdata "<SESSDATA>"   # 上 1080P+
```

| 情况 | 最高可得 |
|---|---|
| 未登录 | 720P |
| 普通账号 | 1080P |
| 大会员 | 1080P+ / 1080P60 / 4K（取决于视频 `accept_quality`） |

拿 `SESSDATA`：

```bash
python scripts/bili_cookie_grab.py     # 自动读本机 Chrome/Edge
```

或者浏览器 `F12 → Application → Cookies → https://www.bilibili.com → SESSDATA`。
（运行中的浏览器可能独占锁 cookie 库，Chrome 127+ 的 `v20` cookie 是 app-bound 加密、
外部解不开，这时直接从 devtools 复制最快。）

### 去水印

```bash
python scripts/contact_sheet.py in.mp4 sheet.jpg         # 先一眼看全片：到底有没有水印
python scripts/wm_dewatermark.py in.mp4 --preview        # 再确认检测对不对
python scripts/wm_dewatermark.py in.mp4                  # -> in_nowm.mp4
python scripts/wm_dewatermark.py in.mp4 --rect 15,15,217,59   # 自动不准时手动给框
```

`--preview` 会打印自动检测到的 bbox，并导出 mask 图和检测框特写，先确认再跑全片。

`--preview` 报 `auto-detection failed` 时，先怀疑**这片子本来就没有水印**（B站投稿原片绝大多数是干净的），
用 contact sheet 确认后直接交付原片，不要硬给 `--rect` 把干净画面糊掉。

## 去水印原理

1. 均匀抽 12 帧，逐帧算「比局部邻域更亮的白细节」（`gray - blur(25)`，并排除局部均值 > 230
   的高亮背景，避免白雾/白天空被当成水印）。
2. 跨帧**多数投票**（≥50%）：水印每帧都在同一位置 → 存活；背景细节会漂移 → 掉票。
   *全帧 AND 会崩——只要有一帧背景纯白，整个字形就被清零，这是踩过的坑。*
3. `close(9×9)` 填掉 logo 的空心结构，`dilate(grow=7)` 吞掉抗锯齿边缘，但**不填满整个矩形**。
4. 逐帧 `cv2.inpaint(TELEA, radius=10)`，用周围真实像素修复。
5. mask 内注入 σ≈2.5 颗粒，避免补丁"过于干净"。
6. 原始帧走 `rawvideo` 管道直进 libx264（只压一次），音轨 `-c:a copy`。

### 调参

| 症状 | 参数 |
|---|---|
| 边缘还有白色描边/鬼影 | `--grow 9` 或 `--grow 11` |
| 补丁大、糊得明显 | `--grow 5 --radius 6` |
| 补丁过分光滑可疑 | `--noise 3.5` |
| 自动框不准 | `--rect x,y,w,h` |
| 文件太大 | `--crf 20 --preset medium` |

### 边界

- 只处理**固定位置**水印；移动/漂移水印不覆盖。
- 水印压在极亮背景上时可能漏 1–2 px 抗锯齿边，加大 `--grow`。
- 水印只在片头出现时：`ffmpeg -ss` 切段分别处理再 `concat`。
- 源是 720P 就是 720P，inpaint 不产生细节。

## YouTube

B站不能用 `yt-dlp`，YouTube 反过来只能用 `yt-dlp`。两个坑：

**bot 检测** —— 直接下会报 `Sign in to confirm you're not a bot`，换 player client 即可：

```bash
# tv_embedded：全量 1080p60，最全
yt-dlp --extractor-args "youtube:player_client=tv_embedded" \
  -f "299+bestaudio[ext=m4a]/299+bestaudio/18" \
  --merge-output-format mp4 -o "%(title).80B__%(id)s.%(ext)s" "URL"
```

| client | 结果 |
|---|---|
| `tv_embedded` | 全量 1080p60 + 多语言原始音轨 |
| `android_vr` / `android` | 只给 format 18（360p 合流），保底 |
| `ios` / `mweb` / `web_embedded` | `Requested format is not available` |

**ffmpeg 不在 PATH** —— `imageio-ffmpeg` 的二进制叫 `ffmpeg-win-x86_64-v7.1.exe`，`yt-dlp` 只认
`ffmpeg(.exe)`。复制成 `ffmpeg.exe` 到包装目录，用 `--ffmpeg-location` 指过去：

```bash
FFBIN="$LOCALAPPDATA/Temp/ffbin"; mkdir -p "$FFBIN"
cp "$(python -c 'import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())')" "$FFBIN/ffmpeg.exe"
yt-dlp --ffmpeg-location "$FFBIN" ...
```

`%(title).80B` 按字节截断，中文/葡语标题会切在半截，下完记得改名。
YouTube 平台不加片头水印，只有 UP 主自己压的 logo 才需要跑去水印。

## 作为 agent skill 使用

把本仓库放进 agent 的技能目录即可被自动发现（Claude Code / DSH / 任何读 `SKILL.md` 的宿主）：

```bash
git clone https://github.com/mengguoying1/bili-video.git ~/.agents/skills/bili-video
```

`SKILL.md` 里是给 agent 的执行流程、决策表、参数对照和坑位清单；`scripts/` 是可直接调用的实现。

## 踩坑记录

1. `yt-dlp` 读 B站必 412，别浪费时间。
2. `playurl` 必须做 WBI 签名（`w_rid`），否则 412 / -403。
3. 匿名 `fnval=4048` 的 dash 只给 480P；`platform=html5&high_quality=1&fnval=1` 才有 720P 合流 mp4。
4. 运行中的 Edge 会独占锁 cookie 库（`ERROR_SHARING_VIOLATION`，共享读也会被拒）。
5. Windows 中文/空格路径下 `cv2.imwrite/imread` 会静默失败，必须 `imencode + tofile`。
6. PowerShell 不支持 heredoc（`python - <<'PY'`），脚本写文件再跑。
7. `ffmpeg -i` 只看信息时返回 exit 1，不是错误。

## 免责

仅用于个人学习、离线备份与自有素材处理。下载和二次分发他人作品前，请遵守平台协议并取得权利人许可。

## License

未声明许可证，保留所有权利。需要其它授权方式请开 issue。
