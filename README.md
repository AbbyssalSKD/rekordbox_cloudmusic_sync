# rekordbox_cloudmusic_sync

> **开工前先读 [CLAUDE.md](CLAUDE.md)** —— 需求、技术路线、当前进度、硬约束与已知坑都在那里。
> 任何新会话（人或 AI）在动手之前都应该先读一遍。

把**任意平台的歌单**同步进 **Rekordbox** 离线曲库的编排器。

一句话定位：以歌单为唯一事实来源，把它翻译成 Rekordbox 里同名 playlist 的成员关系，
并在 Rekordbox 关闭时安全、幂等、可回滚地写入 `master.db`。

> 本项目是**编排器**，不是下载器，也不是解密工具。不内置任何平台的下载能力、
> 不内置 NCM 解密、不随仓库分发任何解密工具。

## 支持的数据源

| 源 | `--source` | 歌单 ID | 是否需要登录 | 备注 |
|---|---|---|---|---|
| 网易云音乐 | `netease` | 数字 | 公开歌单不需要 | 一次给全 `trackIds` |
| QQ 音乐 | `qq` | 数字 | 公开歌单不需要 | 一次返回全量（实测 1286 首） |
| 酷狗音乐 | `kugou` | 数字 | 公开歌单不需要 | 需分页；歌单名从分享页 `<title>` 取 |
| 本地文件 | `local` | 自动生成 | 不需要 | CSV / JSON，任何平台都能导出的兜底格式 |

三个平台的 ID 都是纯数字，**互相之间有歧义**，所以：

- 粘贴**歌单链接** → 按域名自动识别平台（推荐）；
- 只给**纯数字 ID** → 必须加 `--source netease|qq|kugou`。

## 当前进度

| 阶段 | 状态 |
|---|---|
| `plan` 多平台读取歌单 → 选择配置 + 快照 + 变更检测 | ✅ 已实现 |
| `doctor` 库体检（只读，永远操作副本） | ✅ 已实现 |
| `relink` 失效路径重映射（只算不写） | ✅ 已实现 |
| `fetch` 算缺口、生成清单、调用外部下载命令 | ✅ 已实现 |
| `serve` 本机 WebUI（W1 只读：设置 / 歌单 / 变更 / 体检 / 重定位预览） | ✅ 已实现 |
| `fetch` 按配置拉取缺失曲目（外部下载工具适配） | ⬜ 未实现 |
| `convert` 调用外部转换/解密工具 | ⬜ 未实现 |
| `write` 生成映射并写入 Rekordbox playlist | ⬜ 未实现 |

完整业务流程图（十步，含安全闸门与维护支线）见 [docs/FLOW.md](docs/FLOW.md)。

## 快速开始

要求 Python ≥ 3.11，无第三方依赖。

```powershell
# 一个命令混着贴三个平台的链接，各自识别
python -m rb_sync plan `
  --playlist "https://music.163.com/playlist?id=3778678" `
  --playlist "https://y.qq.com/n/ryqq/playlist/7707261125" `
  --playlist "https://www.kugou.com/yy/special/single/1063852.html"

# 本地 CSV / JSON 也可以
python -m rb_sync plan --playlist .\my-set.csv

# 纯数字 ID 必须指定平台
python -m rb_sync plan --source kugou --playlist 1063852

# 安装成命令行（可选）
pip install -e .
rb-sync plan --playlist 1063852 --source kugou
```

### 图形界面

```powershell
python -m rb_sync serve          # 默认 http://127.0.0.1:8765，自动开浏览器
python -m rb_sync serve --no-browser --port 8766
```

W1 阶段只读：设置库路径与曲库目录、读歌单、看变更、库体检、重定位预览。
写库要等 W2。界面只监听本机，所有 API 都要一次性令牌（防止别的网页偷偷调用）。

输出：

```
  源          歌单                           曲目  变更
  ─────────────────────────────────────────────────────────────
  网易云音乐  热歌榜                         200   无变化
  QQ音乐      甜度爆表 | 旋律说唱狙击少女心  66    +2 -1
  酷狗音乐    Alan Walker|教主蹦迪精选歌单   86    首次读取（86 首）
```

### 常用参数

| 参数 | 作用 |
|---|---|
| `--source` | 强制指定平台（纯数字 ID 或链接无法识别时） |
| `--append` | 追加到已有选择配置（默认是**整体替换**，替换时会提示丢掉了几个歌单） |
| `--name` | 覆盖歌单名，仅单个 `--playlist` 可用（想让 Rekordbox playlist 叫别的名字时用） |
| `--refresh` | 忽略快照缓存，强制重取全部元数据 |
| `--json` | 机器可读的变更报告 |
| `--cookie` / `<SOURCE>_COOKIE` / `config.local.json` | 私密歌单才需要 |

### 私密歌单

公开歌单三个平台都匿名可读。私密歌单 / 「我喜欢的音乐」需要登录 Cookie：

```powershell
$env:NETEASE_COOKIE = "MUSIC_U=xxxxx"
$env:QQ_COOKIE      = "..."
$env:KUGOU_COOKIE   = "..."

# 或写进 config.local.json（已被 .gitignore 忽略）
'{"netease_cookie": "MUSIC_U=xxxxx", "qq_cookie": "..."}' |
  Set-Content config.local.json -Encoding UTF8
```

## 变更跟踪是怎么做到的（跨平台统一）

歌单链接是**活引用**而不是快照，每次运行都重新抓取真实数据。三个平台的接口差异很大
（网易云给 `trackUpdateTime`、QQ 给 `mtime`、酷狗什么都不给），所以做到一半靠平台、
一半靠自建：

1. 先只取「有序成员 ID 列表」——这一步对三个平台都是一次/几次轻量请求；
2. 对 ID 列表算 `content_hash`（SHA-1 前 16 位）。和上次快照一致 → **直接复用元数据，
   零元数据请求**；
3. 不一致 → diff 出「新增 / 移除 / 顺序变动 / 曲名变动 / 变灰」；
4. 只为**新增**曲目拉元数据，其余走本地缓存。

这就是「你在 App 里改歌单，下次运行程序自动跟上」的实现方式 —— 没有推送、没有后台常驻。

## 取回缺失曲目（fetch）

```powershell
# 只算缺口 + 生成清单，不下载任何东西
python -m rb_sync fetch

# 指定曲库目录（默认用配置里的 rekordbox.music_roots）
python -m rb_sync fetch --music-root "F:\Music" --music-root "E:\Music"

# 真正执行外部下载命令
python -m rb_sync fetch --run
```

判定「本地已有」是三层匹配，越往后越松：

1. 文件名能拆成 `艺人 - 曲名`，且**曲名与主艺人都对上**；
2. 曲名作为独立词出现在文件名里（能容忍 `Song (Extended Mix).flac`），但**艺人必须也在文件名里**；
3. 同一首有多份候选 → 记「有歧义」，不猜。

> 第 2 条那个「艺人必须也在」是踩出来的：只比曲名会把 `颜人中 - My love` 错配到
> `MK (JPN) - My Love.flac`。

**本项目不下载任何内容。** `fetch --run` 调用的是你自己配置的外部命令：

```json
{
  "fetch": {
    "commands": {
      "netease": "yun \"{url}\" -q 320 -o \"{out}\""
    },
    "out_dir": "F:\\Music\\Cloud Music\\Sync",
    "timeout": 600
  }
}
```

可用占位符：`{url} {id} {name} {artists} {album} {source} {uid} {playlists} {out}`。

没配置命令时是 **handoff 模式**：只把清单写到 `<state-dir>/fetch/<平台>.txt`，交给你自己的工具。

### 整歌单模式（多数下载器只能按歌单下）

逐曲下载要求「给一个歌曲链接就能下一首」，但很多工具（比如 yun-playlist-downloader）
只接受**歌单 / 专辑 / 电台**的链接。这时候用整歌单模式：一条命令跑完
**下载 → 挑出真缺的入库 → 清空暂存区**。

```json
{
  "fetch": {
    "playlist_command": "yun \"{playlist_url}\" -s -q 999",
    "inbox_dir": "F:\\yun_inbox",
    "out_dir": "F:\\Music\\Sync"
  }
}
```

```powershell
python -m rb_sync fetch --run-playlists
```

```powershell
# 图形界面「下载」页也有「整歌单下载」按钮（会先要你确认）
```

它做的事：

1. 只对**还有缺口**的歌单执行命令（没有缺口的歌单不下，省流量）；
2. 命令**在 `inbox_dir` 里执行**（多数下载器没有输出目录参数，只会写当前目录）；
3. 扫 `inbox_dir`，只把**确实缺的**文件搬进 `out_dir`，同名的已在库不覆盖；
4. 清空 `inbox_dir`，只保留 cookie 之类的凭据文件（名字含 `cookie` 的 `.txt`）。

占位符：`{playlist_url} {name} {source} {id} {out}`。

> 模板里的相对路径按 `inbox_dir` 解析，不是按你敲命令的目录。要引用脚本之类，
> 请写绝对路径。

增量靠 `state/fetch_state.json`：`present`（磁盘确认）与 `downloaded`（命令返回 0 但未核实）
都会跳过；想强制重跑加 `--force`。`fetch --verify` 只重扫磁盘、不执行命令。

### 加密文件（`.ncm` 等）

下载到的是加密容器时，它**不会被当成「已有」** —— Rekordbox 放不了 `.ncm`。
fetch 把它单独标成「待解密」：既不重复下载，也不会假装完成。

配一条通用后处理命令（**命令由你自己写，放在仓库外**）：

```json
"fetch": {
  "post_command": "C:\\tools\\你的解密包装.cmd \"{file}\" \"{out}\""
}
```

占位符：`{file} {name} {artists} {album} {source} {uid} {playlists} {out}`。

配好之后，**整歌单模式会自动带上解密**：

```
下载 → 解密 → 挑出真缺的入库 → 清空暂存区
```

- 解出来的文件先落在暂存区，再由管线挑进曲库（保证「只有缺的才进库」）；
- 解密成功后容器会被删掉（内容已经变成可播放文件）；
- **解密失败、或者根本没配 `post_command` 时，容器一律保留**，绝不会被清理掉；
- 也可以自己处理完再 `rb-sync fetch --verify`。

> 仓库里不内置、不调用、不托管任何解密实现 —— 原因见 [CLAUDE.md](CLAUDE.md) 的 C3。

## 产物（全是磁盘上的 JSON，可读、可 diff、可重跑）

```
state/
├── selection.json                 # 选择配置：下游阶段唯一的事实来源
└── playlists/
    ├── netease_3778678.json       # <source>_<playlist_id>.json
    ├── qq_7707261125.json         # 不同平台同号 ID 不会互相覆盖
    └── kugou_1063852.json
```

`state/`、音频文件、`.tmp/`、`config.local.json` 都在 `.gitignore` 里。

## 加一个新平台要改哪里

适配层的契约只有三个方法（见 [rb_sync/sources/base.py](rb_sync/sources/base.py)）：

1. 继承 `HttpSource`，填 `name` / `display_name` / `host_patterns` / `url_template`；
2. 实现 `resolve_id(text)` —— 从链接里取出 ID；
3. 实现 `fetch(playlist_id, previous)` —— 拿到有序 ID 列表后调 `self.assemble(...)`，
   变更检测与元数据缓存自动生效；
4. 在 [rb_sync/sources/__init__.py](rb_sync/sources/__init__.py) 的 `HTTP_SOURCES` 里登记一行。

`assemble` 已经处理了「content_hash 命中就走缓存」「只补新增曲目元数据」两件事，
新平台不需要重复实现。

## 库体检与路径重定位（doctor / relink）

这两个命令要读 `master.db`，需要可选依赖：

```powershell
pip install -e .[rekordbox]
```

```powershell
# 体检：库路径、完整性、曲目/歌单结构、失效文件分布
python -m rb_sync doctor --db "F:\PIONEER\Master\master.db"

# 重定位：把库里失效的旧路径映射到文件新位置（只生成计划，不写库）
python -m rb_sync relink --db "F:\PIONEER\Master\master.db" `
  --from E:/Music --to F:/Music `
  --search F:\Music --search E:\Music
```

真实跑出来的结果（1,231 首的库，移动硬盘从 E: 变成 F: 后）：

```
失效曲目 : 338
可映射   : 306
    basename            32
    basename_ext        22   （换了扩展名，音质可能不同）
    root_remap         252
歧义     :  0
未匹配   :  32
```

**只读保证**：`doctor` / `relink` 会把 `master.db` 复制到 `state/.cache/` 再打开，
真库一个字节都不动。写库（`write`）尚未实现，且实现后会强制「Rekordbox 未运行 + 先备份」。

匹配是分层的、每层都可解释：根路径重映射 → 同名文件（可忽略扩展名）→ 剩下的标记未匹配。
同名文件多处命中时**不猜**，单独列进「歧义」。

## 为什么写入 Rekordbox 必须单独一步

`master.db` 是 SQLCipher 加密库，pyrekordbox 需要独占访问，**Rekordbox 必须完全关闭**才能
写入。所以「全自动」在本项目里的定义是：

> 下载/转换阶段无人值守 + **关闭 Rekordbox 后**执行写入阶段。

另外，playlist 成员指向的是 collection 里的 `DjmdContent`，**文件在硬盘上并不等于能进
playlist**。写库阶段需要先做「磁盘文件 → collection 曲目」的匹配，未匹配的曲目要先导入
collection。

## 目录结构

```
rb_sync/
├── model.py             # 领域模型 = 中间产物 schema（TrackRef / PlaylistSnapshot / Selection）
├── state.py             # 本地状态库（快照 + 选择配置）
├── config.py            # 配置：Cookie / 库路径 / 曲库目录
├── services.py          # 业务逻辑，CLI 与 Web 共用
├── sources/
│   ├── base.py          # Source 协议 + HttpSource + content_hash + assemble
│   ├── netease.py       # 网易云
│   ├── qq.py            # QQ 音乐
│   ├── kugou.py         # 酷狗（含 filename 拆分）
│   ├── local.py         # 本地 CSV / JSON
│   └── __init__.py      # 注册表 + 平台自动识别
├── mapping/
│   ├── diff.py          # 快照差异（新增/移除/顺序/改名/变灰）
│   └── match.py         # 跨平台曲目对齐（曲名+主艺人归一化、时长容差）
├── rekordbox/
│   ├── library.py       # 只读打开库（永远操作副本）+ Rekordbox 进程检测
│   └── relink.py        # 失效路径重映射（纯计算，可单测）
├── web/                 # 本机 WebUI：server / api / jobs / static（W1 只读）
└── cli/
    └── main.py          # rb-sync plan / doctor / relink ...
tests/                   # 离线单元测试
docs/PROJECT_CONTEXT.md  # 项目背景与设计决策
```

## 测试

```bash
python -m unittest discover -s tests
```

## 合规声明

- 本项目只处理用户**自己合法拥有**的内容，不提供、不代管、不下载任何受版权保护的内容。
- 仓库**不包含、不调用、不依赖**任何 NCM 解密实现。若下载得到 `.ncm` 文件，程序只会提示，
  解密工具由用户自行获取并配置。
- 各平台的接口可能随时变更或收紧；本项目只做只读读取，请自行遵守各平台服务条款与当地法律。

## Roadmap

1. ✅ MVP-1：多平台歌单读取 → 选择配置 → 快照 + 变更检测
2. ⬜ MVP-2：磁盘扫描 → Rekordbox collection 匹配 → `track → [playlist]` 归属表（含跨平台去重）
3. ⬜ MVP-3：备份 + `write --dry-run` + 幂等写入 playlist
4. ⬜ 之后：外部下载/转换工具适配层（`fetch` / `convert`）

详细背景与设计决策见 [docs/PROJECT_CONTEXT.md](docs/PROJECT_CONTEXT.md)。
