# CLAUDE.md — 项目工作守则与状态总览

> **这是本项目的唯一入口。任何会话（新开的、续上的、或换人接手）在动手之前，必须先完整读一遍本文件。**
> 读完先用一句话复述「当前阶段 + 下一步」，再开始任何操作。本文件与代码冲突时，以代码为准并立即修正本文件。

- 最近更新：**2026-11-06**
- 最新提交：`git log -1 --oneline`（本文件不写死自己的哈希，否则每次提交都会自我矛盾）
- 代码版本：`rb-sync 0.7.1`
- 测试基线：`88` 个离线单测全过

---

## 0. 三十秒速览

- **是什么**：把网易云 / QQ音乐 / 酷狗的歌单，同步成 Rekordbox 里的同名 playlist —— 一个**编排器**。
- **不是什么**：不是下载器、不是 NCM 解密工具、不是一站式一键工具。
- **到哪了**：**读 + 取的一半已通**（`plan` / `fetch` / `doctor` / `relink` + W1 只读 WebUI），**写库（`write`）一行没写**。
- **唯一硬阻塞**：Rekordbox 启动时打开的是 `F:` 那份 1,231 首的库，还是 `%APPDATA%` 那份 8 首的旧库？这个不确认，写库可能写进一份 RB 根本不看的文件。
- **下一步**：`relink --apply` → `write --dry-run` → `write --apply`。

---

## 1. 开始工作前必须做的事（Definition of Ready）

1. **完整读本文件**（不是扫一眼）。
2. `git log --oneline -5` + `git status --short`，确认工作区有没有别人没提交的改动。
3. 跑基线测试：`python -m unittest discover -s tests` → 应为 `88 tests OK`。
4. 涉及库的操作前，额外确认三件事：
   - 移动硬盘已挂载，**盘符不一定是 E: 或 F:**（会漂移，用 `Get-CimInstance Win32_LogicalDisk` 查）；
   - `F:\PIONEER\Master\master.db`（或实际路径）可读；
   - Rekordbox 进程未运行（写库时才强制，读库不强制）。
5. 用一句话向用户复述「当前阶段 + 本轮要做什么」，得到确认再动手。

---

## 2. 项目定位

**Rekordbox 离线曲库同步编排器**：以用户的歌单为唯一事实来源，把它翻译成 Rekordbox 里同名 playlist 的成员关系，并在 Rekordbox 关闭时安全、幂等、可回滚地写入 `master.db`。

目标用户：业余 DJ，曲库放移动硬盘，曲源是网易云（也有 QQ/酷狗），痛点是「下载 → 转格式 → 手动分类 → 拖进 playlist」全是手工活。

**明确不做**：

- 不内置任何平台的下载能力（只做外部工具适配位）；
- 不内置 NCM 解密（只提示，不调用、不托管、不进依赖树）；
- 不重写 Rekordbox 写库层（用 pyrekordbox）；
- 不把 Dify / MCP / WebUI 设为硬依赖 —— **CLI 永远是完整功能**。

---

## 3. 用户环境（2026-10 实机核实）

| 项 | 事实 |
|---|---|
| Rekordbox | **7.2.14**，安装在 `D:\rekordbox 7.2.14\`（注册表 `InstallLocation` 为空，pyrekordbox 必须显式指路） |
| 真正的库 | `F:\PIONEER\Master\master.db`，**10.5 MB / 1,231 首 / 47 个歌单**，`integrity_check: ok`，usn 46498 |
| APPDATA 那份库 | `%APPDATA%\Pioneer\rekordbox\master.db`，1.57 MB / **8 首**（全是自带采样），2026-06-30 之后没再动过 |
| 移动硬盘 | WD Elements 2620，931 GB，USB。**盘符会漂移**：曾作为 `E:`，后来变成 `F:` |
| 音乐文件 | **只用 `F:\Music`**（1,081 个音频，2026-11-06 起）。`E:\Music` 那份用户已明确弃用，**不要再把它加进曲库目录或搜索路径** |
| 目录结构 | **由用户自己维护**。`F:\Music` 下的 `Cloud Music` / `Neon step` / `rekordbox` / `djay` / `Cyanolights` / 各种专辑目录**一律不要合并、不要搬运**（2026-11-06 用户明确说过「这几个文件夹都不需要你来修改」）。程序只负责把新下载的按 `fetch.out_dir` 放好 |
| 未解密 | 24 个 `.ncm` 在 `E:\Music\Cloud Music\VipSongsDownload`（3.88 GB）—— 随 E 盘一起弃用；其中 12 首在 `F:\Music` 已有可播放版本，另 12 首只存在于 E |
| Python | 系统 3.14.7（无 pyrekordbox）；项目 `.venv` 内装了 pyrekordbox 0.5.0 |
| C 盘 | 只剩约 5.8 GB，空间紧张 —— 别往 C 盘写大文件 |

---

## 4. 核心需求

| # | 需求 | 状态 |
|---|---|---|
| **R1** | 多平台歌单只读读取（网易云 / QQ / 酷狗 / 本地 CSV-JSON），链接自动识别平台 | ✅ |
| **R2** | 多歌单归属映射：一首歌可属多个歌单，持久化 `track → [playlist…]`，支持增量 | 部分 ✅（`fetch` 已产出「曲目 → 歌单」映射；写库侧未接） |
| **R3** | 曲目身份对齐：磁盘文件 ↔ Rekordbox collection 的 `DjmdContent`（文件 ≠ 曲目） | ⬜ |
| **R4** | 安全写入：进程检测 → 备份 → dry-run → 幂等写入 → 完整性校验 → 失败回滚 | ⬜ |
| **R5** | 手动指定库路径与曲库目录；本机 WebUI 可视化操作 | W1 ✅ / W2 ⬜ |
| **R6** | 增量与幂等：只处理变化，重复执行结果一致 | 部分 ✅（歌单侧已有 content_hash 增量） |

---

## 5. 硬约束（违反任何一条都会造成真实损失）

| # | 约束 | 依据 |
|---|---|---|
| **C1** | 写 `master.db` 时 **Rekordbox 必须完全关闭** | SQLCipher 独占；用户在 2026-06-14、06-30 各坏过一次库（`Corrupt/` 下有隔离副本） |
| **C2** | **文件 ≠ 曲目**：playlist 成员是 `DjmdContent` 外键，磁盘上有文件不等于能进 playlist | 必须先把文件与 collection 对齐 |
| **C3** | **绝不内置**下载器或 NCM 解密 | 合规：网易 2021 年向 GitHub 发过 NCM 转换仓库的 DMCA；GitHub 会连带清理 fork |
| **C4** | 映射/写库**不用 LLM** | 幂等性与可复现性；写库错一首要到演出才发现 |
| **C5** | **禁止写死盘符与库路径** | 用户的盘符已从 E: 漂到 F:，导致 332 首路径失效 |
| **C6** | 只读诊断**必须操作副本** | 把 `master.db` 复制到 `state/.cache/` 再打开，用完即删 |
| **C7** | 依赖策略：`plan` 零第三方依赖；`pyrekordbox` 是**可选**依赖 | 保证「装个 Python 就能读歌单」 |

---

## 6. 技术路线

### 6.1 分层

```
CLI (cli/main.py)          WebUI (web/: server/api/jobs/static)
        └──────────┬──────────────┘
              services.py              ← 唯一业务逻辑，两边共用
                    │
     ┌──────────────┼───────────────┐
 sources/        mapping/        rekordbox/
 网易云 QQ 酷狗   diff match      library relink (write 待做)
 本地 CSV/JSON      │                 │
                    └────── state/ ───┘   (selection / 快照 / 计划 / 备份)
```

### 6.2 数据流（十步，详见 [docs/FLOW.md](docs/FLOW.md)）

```
歌单链接 → 选择配置 → 曲目解析 → 下载(外) → 转换(外) → 归属映射
        → collection 对齐 → 写入计划(dry-run) → 安全写入 → 对账
```

三条顺序不能换：
1. **归属信息必须在下载之前捕获**（否则 track→playlist 多对多关系丢失）；
2. **collection 对齐必须夹在映射和写入之间**（否则一首也加不进去）；
3. **写入必须最后、且由人确认**（唯一会改库的动作）。

### 6.3 中间产物

```
state/selection.json              选择配置（下游唯一事实来源）
state/playlists/<source>_<id>.json  歌单快照（成员 + content_hash）
state/relink_plan.json            路径重映射计划（306 条）
state/write_plan.json             写入计划（未实现）
backup/master_<时间戳>.db          写入前备份（未实现）
```

### 6.4 选型理由（都试过或算过代价）

| 选择 | 理由 |
|---|---|
| Python | pyrekordbox 是 Python 生态；`plan` 可做到零依赖 |
| 不导出 XML 让用户导入 | 用户明确要走「直接写库」；XML 多一步手动操作 |
| WebUI 用 `http.server` + 原生 JS | 引 npm 构建链会让「装完就能用」变成「还要装 Node」 |
| 不做 Electron | 浏览器够用 |
| 跨平台曲目对齐用「曲名+主艺人+时长容差」 | 三个平台 ID 不通；混音/现场版刻意**不合并** |

---

## 7. 当前进度

### 7.1 已实现（`plan` / `fetch` / `doctor` / `relink` / `serve`）

| 命令 | 作用 | 是否碰库 |
|---|---|---|
| `rb-sync plan` | 读歌单 → 选择配置 + 快照 + 变更检测 | 否 |
| `rb-sync fetch` | 算缺口（曲目→歌单映射）、生成清单、调用**用户配置的**外部下载命令；加密容器单独成档 | 否 |
| `rb-sync fetch --run-playlists` | **整歌单模式**：下载 → 解密（配了 post_command 时）→ 只挑真缺的入库 → 清空暂存区（0.7.0/0.7.1） | 写曲库目录 |
| `rb-sync doctor` | 库体检：曲目/歌单树/缺失分布/完整性 | 只读副本 |
| `rb-sync relink` | 失效路径重映射（**只算不写**） | 只读副本 |
| `rb-sync serve` | 本机 WebUI（设置/歌单/变更/下载清单/体检/重定位预览） | 只读副本 |

验证过的数字（2026-10-28，库在 F: 时）：

```
doctor    → 1,231 首 / 47 个歌单 / integrity ok / 843 在 / 338 缺失
relink    → 338 失效：284 可映射（root_remap 252 + basename 13 + basename_ext 19），54 未匹配
            （只搜 `F:\Music`。早先搜 E: 时是 306，其中 29 条指向 E:，随 E 弃用作废）
fetch     → 热歌榜 200 首：本地已有 0 / 缺失 200 → 清单 200 行（handoff 模式）
            自建 24 首 .ncm 歌单：本地已有 0 / 缺失 0 / **待解密 24**（不再误判为已有）
            Prog House 31 首：配本人 VIP cookie 后 yun 31/31 全下 → 挑出真缺的 3 首 → **present 31 / missing 0**
web 冒烟  → 18/18 通过（含 403 鉴权、job 轮询）
单测      → 88 个全过（venv 与裸系统 Python 各一遍）
整歌单模式 → 0.7.0：CLI 端到端（假下载器）与 WebUI `/api/fetch/run-playlists` 均验证通过
解密环节  → 0.7.1：**用真实 .ncm 验证通过**（134 MB → 133.9 MB FLAC，头部 `fLaC` 正确）；
            整歌单模式下现在会自动「下载 → 解密 → 挑缺的入库 → 清空」
```

### 7.2 W1 已提交（2026-11-06）

服务层抽取、`rb_sync/web/`、结构化配置、54 个单测、本文件，都在 2026-11-06 的 W1 提交里。
开工时 `git status` 应当干净；如果有未提交改动，先看清是谁的、改了什么，再继续。

`config.local.json`（已 gitignore）当前：
`db_path = F:\PIONEER\Master\master.db`、`music_roots = ["F:\Music"]`、
`previous_roots = ["E:/Music"]`。

> `previous_roots` 里的 `E:/Music` **不是"还要用 E 盘"**，而是库里记录着的旧路径前缀 ——
> 重定位要靠它才能把 338 条失效路径映射到 `F:/Music`。删掉它 relink 就没有"旧根"可用了。

### 7.3 未实现

| 阶段 | 内容 | 依赖 |
|---|---|---|
| W2-1 | `relink --apply`：把 306 条新路径写回库（含备份/校验/回滚） | 确认库位置 |
| W2-2 | `write --dry-run`：算 playlist 增删计划 | collection 对齐 |
| W2-3 | `write --apply`：建 playlist + 插成员 | W2-1/2 |
| MVP-2 | 磁盘扫描 → collection 匹配 → `track→[playlist]` 归属表 | — |
| 后续 | `convert` 外部工具适配（转码/解密提示） | — |

---

## 8. 关键数据快照（2026-10-26 的真库）

| 指标 | 数值 |
|---|---|
| 曲目 | 1,231（自带素材 50） |
| 歌单 | 47 个（`Lib` 文件夹 32 个 + `Playlist` 文件夹 14 个 + 根级 1 个） |
| `Attribute` 语义 | **1 = 文件夹，0 = 歌单**（实证，写库建文件夹必须用 1） |
| 文件在位 | 843 |
| 路径失效 | 338 → **284 可自动重映射**（只搜 `F:\Music`）→ 54 未匹配，需人工或重新下载 |
| 换扩展名的映射 | 19 首（`.flac → .wav`），**默认不勾选**，音质可能不同 |

---

## 9. 常用命令

```powershell
# 环境（含 pyrekordbox）
.\.venv\Scripts\python.exe -m unittest discover -s tests

# 读歌单（零依赖，系统 python 也行）
python -m rb_sync plan --playlist "https://music.163.com/playlist?id=xxx"

# 算缺口 / 执行外部下载命令（项目不下载，只调用你配置的命令）
python -m rb_sync fetch
python -m rb_sync fetch --run --command netease='yun "{url}" -o "{out}"'

# 库体检 / 重定位（需要 pyrekordbox）
python -m rb_sync doctor  --db "F:\PIONEER\Master\master.db"
python -m rb_sync relink  --db "F:\PIONEER\Master\master.db" `
        --from "E:/Music" --to "F:/Music" --search "F:\Music" --search "E:\Music"

# 图形界面（仅本机 127.0.0.1）
python -m rb_sync serve

# 或者双击仓库根目录的「启动WebUI.bat」—— 等价于上面那条，会自动开浏览器；
# 已在跑时不重复启动，只把浏览器打开

# 编码体检（防止文件被写坏）
.\.venv\Scripts\python.exe .venv\scan_encoding.py
```

> 说明：`.venv/` 里现在只留 4 个开发期脚本：`scan_encoding.py`（编码体检，**建议保留**）、
> `smoke_web.py`、`smoke_fetch_api.py`、`e2e_real.py`（冒烟）。它们不进仓库。
> 一次性的 probe / demo / debug 脚本用完即删，别攒着（2026-11-06 清理过一次，删了 27 个）。

---

## 10. 文件地图

```
rb_sync/
├── model.py       领域模型 = 中间产物 schema（TrackRef / PlaylistSnapshot / Selection），曲目 ID 一律字符串
├── state.py       本地状态库（快照 + 选择配置），文件名 <source>_<id>.json
├── config.py      结构化配置：库路径 / 曲库目录 / 旧根 / Cookie；支持部分更新
├── services.py    业务逻辑，CLI 与 Web 共用（plan / inspect / relink / probe）
├── sources/       平台适配器 + 注册表 + 自动识别
│   ├── base.py    HttpSource / content_hash / assemble（变更检测与元数据缓存）
│   ├── netease.py qq.py kugou.py local.py
│   └── __init__.py detect()：链接按域名识别，裸数字必须显式 --source
├── mapping/
│   ├── diff.py    快照差异：新增/移除/重排/改名/变灰
│   └── match.py   跨平台对齐：normalize / track_key / same_track（不合并 Remix/Live）
├── downloader/    fetch 阶段：缺口计算、清单、外部命令适配、增量状态
│   ├── plan.py    曲目→歌单映射、三层匹配（文件名校验艺人）、缺失清单
│   ├── pipeline.py 整歌单模式：按歌单跑命令 → 挑缺的入库（带重试）→ 清空暂存区
│   ├── runner.py  命令模板渲染 + subprocess（只调用用户命令，不下载）
│   └── state.py   fetch_state.json：present / downloaded / failed
├── rekordbox/
│   ├── library.py 只读打开库（永远用副本）+ 进程检测 + integrity_check
│   └── relink.py  路径重映射纯计算：root_remap / basename / basename_ext / 歧义
├── cli/main.py    命令入口（薄壳，只负责打印）
└── web/           W1 只读界面：server(路由/鉴权) api jobs static
docs/
├── PROJECT_CONTEXT.md  背景与全部设计决策（长文，含历史）
├── FLOW.md             十步业务流程图（Mermaid）
└── WEBUI.md            WebUI 设计与 API 契约
```

---

## 11. 决策记录

| 日期 | 决定 | 理由 |
|---|---|---|
| 2026-10-28 | 写库直接走 pyrekordbox，不做「导出 XML 再导入」 | 用户明确选择；XML 多一步手动操作 |
| 2026-10-28 | 映射与写库**不用 LLM** | 纯确定性；LLM 破坏幂等与可复现 |
| 2026-10-28 | 曲目 ID 统一为字符串，快照 schema v2 | 三平台 ID 类型不一致 |
| 2026-10-28 | 变更检测用 `content_hash` 而非平台时间戳 | 酷狗不提供任何时间戳 |
| 2026-10-28 | 加 `local` 源（CSV/JSON） | 平台接口失效时的兜底 |
| 2026-10-28 | WebUI 仅本机 + token + Host 校验 | 界面上有能写库的按钮，不能暴露到局域网 |
| 2026-10-28 | 先做服务层抽取，再做 UI | 避免 CLI/Web 两份实现漂移 |

---

## 12. 已知坑与事故（别再踩）

1. **PowerShell 5.1 会毁掉无 BOM 的 UTF-8 文件**。`Get-Content | Set-Content` 把中文读成 GBK 再写回 UTF-8，文件立刻乱码且 `py_compile` 报语法错（2026-10-28 真实发生过，三个文件被毁）。**对策：改源码只用编辑工具或 Python 脚本，不要用 PowerShell 文本管道。** 改完跑 `scan_encoding.py`。
2. **库坏过两次**：`Corrupt\20260614221325`（460 首）与 `Corrupt\20260630230752`（9 首）。现用库是第三次重建的。所以「备份 + 校验 + 独占」是硬要求。
3. **盘符漂移**：`E:` 被内置 SSD 占用后，移动硬盘变成 `F:`，库里 332 首路径全部失效。任何地方都不要写死盘符。
4. **`.cache` 副本残留**：SQLAlchemy 会短暂占用文件句柄，`Library.close()` 里已加 `gc.collect()`，并在下次打开时清理残留。
5. **快捷方式失效**：桌面 `rekordbox 7.lnk` 指向 `C:\Program Files (x86)\rekordbox 7.2.14`（不存在），实际是 `D:\rekordbox 7.2.14`。
6. **本机只有 C/D/E 三块固定盘 + 一块可插拔 WD**；盘是否挂载随缘，涉及库的验证先查 `F:\PIONEER\Master\master.db` 是否存在。
7. **外部命令输出不能假定编码**。中文 Windows 默认 cp936，但工具常写 UTF-8；`subprocess.run(text=True)` 不指定编码会在读取线程里抛 `UnicodeDecodeError`（2026-11-06 踩过）。`downloader/runner.py` 已固定 `encoding="utf-8", errors="replace"`。**自己写临时脚本时同样要加。**
8. **只比曲名会错配**。`颜人中 - My love` 曾匹配到 `MK (JPN) - My Love.flac`。现在的规则：第二层匹配要求**艺人也在文件名里**，且曲名按词边界比（`Love` 不命中 `Lovely`）。`tests/test_fetch.py` 里有回归用例。
9. **PowerShell 传含引号的参数给 Python 会掉引号**。用 `--command 'netease=tool "{url}"'` 这类模板做端到端测试时，改用 Python 脚本直接调 `services.fetch_run(commands=...)`，别在 PowerShell 里拼。
10. **`.ncm` 曾被误判成"已有"**。`.ncm` 一度在"音频后缀"清单里，于是加密文件让 fetch 认为这首已就绪，Rekordbox 却放不了。现在 `model.py` 分成 `PLAYABLE_SUFFIXES` / `ENCRYPTED_SUFFIXES`，加密容器单独一档 `encrypted`，状态记为 `encrypted`（**不会被跳过**，配了后处理就会重试）。可播放文件优先于加密文件。
11. **后处理钩子只是"跑一条你给的命令"**。`fetch.post_command` 对「只有加密容器」的曲目逐条执行，占位符里有 `{file}`。仓库不内置任何解密实现，也不在文档里给可直接复制的解密命令 —— 包装脚本放仓库外。
12. **手改 `config.local.json` 少个逗号 = 一切看起来都坏了**。以前 `load_settings()` 解析失败会**静默回退默认值**（库路径空、曲目目录空），界面表现就是"什么都没配、功能全废"（2026-11-06 真实发生）。现在：`config_error()` 会带回具体行列；`probe` 第一条就报红；Web 顶栏显示「配置文件有误（已回退默认值）」；`update_settings` 拒绝往坏配置上写。**改配置后先看顶栏有没有红标。**
13. **`previous_roots` ≠ 扫描目录**。它存的是**库里记录的旧路径前缀**（现在是 `E:/Music`）。用户说"忘掉 E 盘"指的是不再扫 `E:\Music`，但旧根必须留着，否则 relink 没有"从哪映射"的依据。
14. **用户说"不要"就是不要，别自行抢救**。2026-11-06 用户说 E 盘的 `.ncm` 不要了，我仍把 3.88 GB 拷到 `F:\ncm_inbox`，被要求删掉；还替他做了个启动 bat，也被说多此一举（本地 `index.html` 双击即可）。**先按字面执行；想多做一步先问。**
15. **Unlock Music 的两个 GitHub 仓库都已被 DMCA 下架**（2022-11-04 的 QQ 音乐通知）：`unlock-music/cli` 与 `ix64/unlock-music` 均返回 *Repository unavailable due to DMCA takedown*，`git ls-remote` 直接 403。→ **不要再去 GitHub 找 CLI 版**；也不去镜像站代取。用户本地 `D:\Unlock_Music` 那份网页版是磁盘文件，不受影响。
16. **`.cmd` / `.bat` 必须写成纯 ASCII**。cmd.exe 按 ANSI(cp936) 读批处理，UTF-8 的中文注释会被解析成乱码命令，整个脚本语法崩掉（2026-11-06 踩过）。要么全英文，要么用 GBK 编码保存。
17. **当前解密工具链（全部在仓库之外）**：
    - `D:\ncmpp\`：`git clone` 的 [Majjcom/ncmpp](https://github.com/Majjcom/ncmpp)（C++，121★）
    - `D:\ncmpp\bin\ncmpp.exe`：v1.1.0 release 预编译（478 KB）
    - `D:\ncmpp\decrypt_one.cmd`：我写的单文件包装（见下）
    - `config.local.json` 的 `fetch.post_command` 已指向它：`"D:\ncmpp\decrypt_one.cmd" "{file}" "{out}"`
    - **2026-11-06 用真实 `.ncm` 验证通过**：`DR.VEGAS LABORATORY - CHROMATIC.ncm`（134 MB）
      → `DR.VEGAS LABORATORY - CHROMATIC.flac`（133.9 MB，头部 `fLaC`）
    - 包装器要点：在**源文件旁边**建临时目录（不拷到 `%TEMP%`，省一次大盘拷贝）；
      结尾用 `echo.\|` 喂掉 ncmpp 的 `system("pause")`；解出的文件由管线搬走
18. **yun-playlist-downloader 实测结论（路线 A）**：
    - 只支持**歌单 / 专辑 / 电台** URL；单曲 URL 直接 `unsupported type`（源码 `getType` 是 `url.includes(type)`）；
    - 裸数字 id 会被当成**歌单 id**；无输出目录参数，永远下到 CWD；
    - 文件名默认 `:name/:singer - :songName.:ext`（歌单名子目录 + 歌手 - 歌名），**正好符合我们的匹配规则**；
    - **没 VIP cookie 时 31 首只有 3 首能下**，其余报「版权受限」；
    - **用本人 VIP 账号的 cookie 后 31/31 全部可下**（2026-11-06 实测，54 秒 / 1.01 GB，多为 FLAC）；
      文件放 `yun.cookie.txt`，内容 `MUSIC_U=...; __csrf=...` 一行即可
    - 它只按"同路径同名同大小"跳过，**会重复下载库里换个文件名已有的歌**（明明 Happy face 已存在仍下了一遍）
      → 结论：**下到暂存目录（如 `F:\yun_inbox`）再挑，别直接下进 `F:\Music`**
    - cookie 文件：纯文本，空行和 `//` 开头的行被忽略，其余行直接拼接成 Cookie 头，
      所以内容就是 `MUSIC_U=...; __csrf=...` 这种；默认路径是 CWD 下的 `yun.cookie.txt`
19. **刚下载完的大文件可能被瞬间锁住**。把 yun 下的文件搬进曲库时，`shutil.move` 报
    `PermissionError: [WinError 5]`，重试同样的拷贝就成功了（杀软/索引器在扫新文件）。
    **对策：搬运脚本要带重试，别把一次性失败当成永久错误。** `pipeline.harvest` 已内置 3 次重试。
20. **整歌单命令是在暂存目录里执行的**（`cwd=inbox_dir`）。模板里的相对路径按**暂存目录**解析，
    不是按你敲命令的地方解析 —— 要引用脚本必须写绝对路径（2026-11-06 端到端演练时踩到过）。
21. **cmd 里 `"目录\"` 这种写法会吃掉引号**。`copy "%SRC%" "%WORK%\"` 中反斜杠紧贴引号，
    cmd 把它当成转义，整条命令就废了（`[ncmpp] copy to temp failed`）。**目标路径要写全文件名**：
    `copy /y "%SRC%" "%WORK%\%~nx1"`。`move ... "%DEST%\"` 同理，改成 `for %%F in (...) do move`。
22. **在本项目的 agent 会话里，Python 进程只能写工作区**。写 `F:\`、`D:\ncmpp`、`%TEMP%`
    都会 `PermissionError`（**不是杀软，也不是 ACL**）。所以：
    - 跨盘的真实写入要**由用户在自己的终端里跑**，agent 只能在工作区（`.tmp/`）里做端到端验证；
    - 早先那次"搬到曲库报 WinError 5"其实也混了这条，不一定全是文件锁。

---

## 13. 未决问题

1. **Rekordbox 打开的是哪份库？**（唯一硬阻塞）判断方法：打开 Rekordbox 看曲目数 —— 1,231 → `F:` 那份；8 → `%APPDATA%` 那份。台面上找不到把 APPDATA 指向 `F:` 的配置（不是 junction、无环境变量）。
2. **32 首确实不存在的曲目**（Maozon 专辑）：已确认全盘搜不到，需要重新下载或放弃。用户当时说「先不用管」。
3. **24 个 `.ncm` 已随 E 盘弃用**，其中 12 首在 `F:\Music` 有可播放版本、12 首只在 E。
   用户若还想要这 12 首，只能在 E 盘还在时处理（解密/另存）或以后重新下载。
4. **19 首换扩展名的映射**（`.flac → .wav`）：需要用户确认音质可接受。

---

## 14. 下一步（按优先级）

1. 让用户确认 **§13.1**（RB 打开哪份库）—— 在此之前不要写库。
2. **W2-1 `relink --apply`**：写回 306 条路径。要求：进程检测 → 备份 `master.db` + `masterPlaylists6.xml` → 分批更新 `FolderPath` → `integrity_check` → 失败从备份还原。
3. **W2-2 `write --dry-run`**：读副本，算出「要建哪些 playlist / 加哪些歌 / 跳过哪些」，输出 `write_plan.json` + 人可读预览。**不写一个字节。**
4. **W2-3 `write --apply`**：落实 §5 的 C1/C2/C4。
5. W3：SSE 进度、任务历史、报告导出。

---

## 15. 每次工作结束前（Definition of Done）

1. 跑一遍测试，确认基线没退化。
2. **更新本文件的 §7 进度、§13 未决问题、§16 变更日志。**（这是硬性要求，不是可选项）
3. 代码改动要么提交，要么在 §7.2 明确列出「未提交」。
4. 涉及库的操作，确认 `master.db` 的 mtime 没有被意外改动。

---

## 16. 变更日志

| 日期 | 提交 | 内容 |
|---|---|---|
| 2026-10-28 | `62b8992` | 多平台歌单读取（网易云/QQ/酷狗/本地）+ 变更检测 + doctor/relink（只读）+ 40 单测 |
| 2026-10-28 | `3070323` | 补 `docs/FLOW.md` 十步业务流程图 |
| 2026-10-28 | W1 提交 | W1 只读 WebUI（`services.py` 抽取、`web/`、结构化配置、54 单测、18 项 HTTP 冒烟） |
| 2026-11-06 | W1 提交 | 建立 `CLAUDE.md`：需求、技术路线、进度、硬约束、已知坑、DoR/DoD |
| 2026-11-06 | 本次提交 | `fetch`：缺口计算（含多歌单归属）、三层匹配、清单、外部命令适配、增量状态、WebUI 下载页；73 单测 |
| 2026-11-06 | 本次提交 | 加密容器单独成档（不再误判为已有）+ 通用后处理钩子 `fetch.post_command`；78 单测 |
| 2026-11-06 | 本次提交 | 弃用 E 盘（只扫 `F:\Music`）：relink 重算 338→284；配置解析失败不再静默回退；WebUI 版本/配置红标；80 单测 |
| 2026-11-06 | 文档 | 实测确认 `unlock-music/cli` 与 `ix64/unlock-music` 均被 DMCA 下架 |
| 2026-11-06 | 未提交（仅配置） | 接入 `Majjcom/ncmpp`（D:\ncmpp + 包装脚本 decrypt_one.cmd），`fetch.post_command` 已配置；仓库代码未改动 |
| 2026-11-06 | 本次提交 | **0.7.0 整歌单模式**：`fetch --run-playlists` + WebUI「整歌单下载」按钮；下载→挑缺的入库→清空暂存区（保留 cookie）；`downloader/pipeline.py`；85 单测 |
| 2026-11-06 | 本次提交 | **0.7.1 解密接入管线**：整歌单模式自动「下载→解密→挑缺的入库→清空」；解不开/没配命令的容器**永不删除**；真实 `.ncm` 验证通过（134 MB → 133.9 MB FLAC）；88 单测 |
| 2026-11-06 | 本次提交 | `启动WebUI.bat`：双击即起本机 WebUI（已运行时只开浏览器）；文件内纯 ASCII 无 BOM |
