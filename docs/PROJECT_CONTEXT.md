# 项目上下文（源自 `docs/网易云歌单自动化读取 - DeepSeek.pdf`）

> 这是从既有对话 PDF 中提取并经结构化整理的项目背景，作为本仓库的起始上下文。
> 目标对象是**网易云音乐（NetEase Cloud Music）**；仓库/目录名为
> `rekordbox_cloudmusic_sync`（拼写正确，PDF 中出现的 `couldmusic` 是笔误）。
> 代码落地的当前进度见根目录 `README.md`。

## 1. 背景与痛点

- 业余 DJ，使用 Rekordbox，曲库存于移动硬盘。
- 曲源为网易云音乐：下载 → 手动转格式 → 手动分类 → 手动放进 Rekordbox playlist。
- 网易云与主流 DJ 平台无合作，无法关联流媒体账号，只能手动更新曲库 → 低效、易遗漏。
- 目标：自动化读取网易云账号歌单，并同步更新离线曲库与 Rekordbox playlist。

## 2. 已确认的硬事实

| # | 结论 |
|---|---|
| 1 | 没有任何单一开源项目覆盖全流程，必须「现成工具 + 自建编排层」。 |
| 2 | 网易云官方 API 面向合作方（appId + RSA_SHA256 签名，`openapi.music.163.com`），个人不可直接用；社区方案基于官方 API 扫码登录并本地存 Cookie。 |
| 3 | **pyrekordbox 及基于它的 MCP 在 Rekordbox 运行时无法提交数据库变更**，写库必须在 Rekordbox 完全关闭时执行。这不是缺陷，要写进流程设计。 |
| 4 | 可直接复用的 Rekordbox MCP：`bangersss-mcp`（含 Dry Mode 覆盖层预览）、`rekordbox-mcp`（多个维护版本，30+ 工具，支持按调性/BPM 搜索、My Tag 读写）。使用前必须备份曲库。 |
| 5 | Dify 只支持 HTTP 传输的 MCP，而上述 MCP 多为 stdio 本地服务 → 需自建轻量 HTTP 桥接，或让 Dify 只做决策层。 |

## 3. 目标工作流

1. 程序读取账号歌单列表
2. 手动选择要同步的歌单
3. 脚本下载缺失歌曲到移动硬盘指定目录
4. 若为 NCM → 交给外部解密工具（仓库不内置）
5. 按歌曲所属歌单自动分配进 Rekordbox playlist
   - 一首歌可属于多个歌单 → 同时进入多个 playlist
   - playlist 不存在则自动创建（名称大小写不敏感匹配）

## 4. 关键设计决策

### 4.1 阶段拆分（重新定义"全自动"）

- 前段（读取 → 下载 → 解密 → 转格式）：可无人值守。
- 后段（写 Rekordbox）：仅能在 Rekordbox 关闭后执行，不能后台静默同步。
- 因此「全自动」= 前段无人值守 + 关闭 Rekordbox 后执行写入阶段。

### 4.2 选择与执行解耦

歌单选择结果落成配置 JSON，下载/写入脚本读配置，禁止硬编码。

### 4.3 多歌单归属必须显式保存

若按歌单逐个下载，track → playlist 映射会丢失。必须在下载阶段保留映射：
每首歌旁 `.playlists.json`，或总目录维护 `track → [playlist_names]` 表。

### 4.4 写库流程

1. 前段完成后暂存 `{playlist_name: [track_paths]}` 写入计划（JSON）
2. 用户关闭 Rekordbox → 运行独立写入脚本（存在则取 ID，不存在则创建）
3. 重新打开 Rekordbox 查看
4. 首次跑通开 Dry Mode 验证映射

### 4.5 解密/转换的两条路径

| 路径 | 流程 | 代价 |
|---|---|---|
| A 保留原构思 | 下载工具 → 外部解密 CLI → 转 Rekordbox 格式 | 多一步解密，格式转换需另处理 |
| B 并入 W4DJ-RKB | 下载工具只拿文件 → W4DJ-RKB 解密 + 转换 + 导出 .m3u8 | 依赖其兼容性 |

W4DJ-RKB 不含下载能力，下载仍需前置工具。

## 5. 项目定位与边界

- 定位：**Rekordbox 离线曲库同步编排器**，不是"网易云歌单下载器"。
- 候选名：`rekordbox-library-sync` / `rb-offline-sync` / `playlist-bridge`。
- **不做**：内置网易云下载、内置 NCM 解密；重复实现 Rekordbox 写库；把 Dify 设为硬依赖。

### 目标目录结构

```
sources/      歌单源适配器（网易云、本地 CSV、其他）
downloader/   外部下载工具适配器（不实现侵权下载）
convert/      调用外部转换/解密工具
mapping/      多歌单归属表、增量状态库
rekordbox/    备份、dry-run、进程检测、playlist 增删
cli/          plan / sync / write --dry-run / write
docs/         配置示例、风险声明、故障排查
```

### 目标 CLI

```bash
rb-sync plan --source netease --select "electro house,future house"
rb-sync fetch
rb-sync convert
rb-sync write --dry-run
rb-sync write
```

## 6. 合规结论（重要）

- 网易 2021-10 曾向 GitHub 发 DMCA 要求下架所有 NCM 格式转换仓库；GitHub 处理同类投诉时可能波及整个 fork 网络（如 Ubisoft 案移除 131 个仓库）。
- Unlock Music 自身 README 已声明"存在法律风险及滥用风险，不再提供 Demo 服务"。
- 因此：**不提交解密代码、不列强制依赖、不提供"一键下载并解密"封装**。
  检测到 NCM 只输出提示/钩子；工具路径由用户自行配置；文档可纯文字提及社区工具，但不进依赖树、不在代码中调用。
- 法律定性：项目是"曲库同步编排器"，不是 DRM 规避工具。

## 7. MVP 范围

1. 读取歌单列表 → 生成选择配置
2. 由本地已有文件生成 track → playlist 映射表
3. Rekordbox 关闭时，dry-run 验证后创建 playlist 并添加曲目
4. 下载/解密仅作"外部工具调用示例"，不进入核心承诺

## 8. 下一步工作项

1. 定名与定位（CLI 优先，Dify 可选）—— 已定：`rb-sync`，编排器，CLI 唯一必需入口
2. 画数据流：歌单源 → 选择配置 → 下载 → 转换/解密（外部）→ 映射 → 关 Rekordbox → dry-run → 写入
3. 定义中间产物格式：选择配置 JSON / 映射表 JSON / 写入计划 JSON —— 选择配置与快照已落地
4. 实现 MVP 1、2 步（不碰下载与解密）—— 第 1 步已完成；第 2 步需先补 collection 匹配
5. 用 bangersss-mcp 的 Dry Mode 验证 playlist 创建/添加
6. README 风险声明（不托管解密工具、不内置下载、遵守版权与平台条款）—— 已完成
7. Dify 桥接作为后续可选模块，不阻塞 MVP

## 9. 实现后补充的两条关键认知

- **文件 ≠ 曲目**：Rekordbox 的 playlist 成员是 `DjmdContent` 的外键，磁盘上有文件不等于能被
  加进 playlist。写库前必须先做「磁盘文件 → collection 曲目」匹配，未匹配的要先导入 collection。
- **歌单链接是活引用**：`/api/v6/playlist/detail` 返回完整有序 `trackIds` 与
  `trackUpdateTime`/`trackCount`，每次运行重新抓取并 diff 本地快照，即可实现「App 里改、
  下次运行自动跟上」，无需推送或常驻进程。

## 10. 多平台扩展（2026-10 追加需求）

原上下文只假设网易云。实际需求是「输入酷狗、QQ 音乐的歌单同样有效」，因此源层改为
适配器注册表（`rb_sync/sources/`），并实测确认三者都能匿名只读：

| 平台 | 接口 | 返回 | 变更戳 | 分页 |
|---|---|---|---|---|
| 网易云 | `api/v6/playlist/detail` + `api/v3/song/detail` | `trackIds` 全量有序；元数据另取 | `trackUpdateTime` | 无需 |
| QQ 音乐 | `c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg` | `songlist` 全量（含元数据） | `mtime` | 无需（实测 1286 首） |
| 酷狗 | `mobilecdn.kugou.com/api/v3/special/song` | `data.info`（`hash` + `filename`） | 无 | 必须（`pagesize≤300`） |

平台差异带来的三个设计约束：

1. **曲目 ID 一律字符串**：网易云是数字、QQ 是 `songmid`、酷狗是 32 位 hash，混用类型会让
   下游到处做判断。快照 schema 升到 v2。
2. **变更检测不能依赖平台时间戳**：改为对「有序成员 ID 列表」算 `content_hash`，
   三个平台统一走同一套逻辑。
3. **裸数字 ID 有歧义**（三个平台都是数字）：链接按域名自动识别；裸数字必须 `--source`。

另补 `local` 源（CSV / JSON），既是平台接口失效时的兜底，也方便手工整理歌单。

跨平台同一首歌用 `mapping/match.py` 的 `track_key`（曲名 + 主艺人归一化）对齐，
`same_track` 再用时长容差兜底 —— 混音版/现场版刻意**不**合并，避免写错歌。

## 11. 当前状态与已定决策（2026-10-28）

### 目标库（唯一）

```
F:\PIONEER\Master\master.db     11,038,720 B   最后写入 2026-10-26 23:32:48
```

- 移动硬盘是 **WD Elements，当前盘符 `F:`**（不是 E: —— 内置 SSD 占了 E:，这个盘符漂移
  正是库里 332 首路径失效的成因）。盘符可能再变，程序不能写死。
- 该库实测：`integrity_check: ok`，usn 46498，**1,231 首**，两个文件夹 `Lib` / `Playlist`
  下共 **46 个歌单**。
- `Attribute=1` = 文件夹，`Attribute=0` = 歌单（实证，写库时建文件夹必须用 1）。
- 另有三份**不再关注**的库：`%APPDATA%\Pioneer\rekordbox\master.db`（8 首，6-30 的空库）、
  `Corrupt\20260614221325`（460 首，6-11）、`Corrupt\20260630230752`（9 首，6-16）。
  它们只是"崩过两次"的证据，不作为同步目标。

### 已定决策

1. **写库方式**：直接写 `master.db`（走 pyrekordbox），不做导出 XML 那条线。
2. **不需要 LLM**：playlist 归属来自歌单本身，是「字典查找 + 集合运算 + 主键插入」，
   用模型会破坏幂等性与可复现性。
3. **缺失文件暂缓处理**：338 首缺失里，306 首已算出重定位映射（见 `state/relink_plan.json`：
   root_remap 252 / basename 32 / basename_ext 22），剩 32 首（全部 Maozon 专辑）全盘确实不存在。
   **本轮不追这 32 首，也不执行 `--apply`。**

### 未解开的疑问（恢复工作时的第一件事）

Rekordbox 启动时打开的到底是 `%APPDATA%` 那份（8 首）还是 `F:` 这份（1,231 首）？
这台机器上找不到 APPDATA 重定向的证据，而 `F:` 那份在 10-26 被写过。
**判断方法：打开 Rekordbox 看曲目数。** 这决定 `write` 该写哪个文件。

### 安全约束（写库前必须满足）

- Rekordbox 进程必须未运行（`library.is_rekordbox_running()` 已实现探测）。
- 写前自动备份，写后 `PRAGMA integrity_check` 复核。
- 目前 `doctor` / `relink` 全部只读：复制到 `state/.cache/` 再打开，用完即删。


