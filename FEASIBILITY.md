[Uploading FEASIBILITY.md…]()
# FEASIBILITY.md — DSHM 备份与迁移工具：只读侦查、适配依据与能力边界

- 设备：华为 MatePad Edge 平板（主力机），HarmonyOS 7.0.0.109SP6。
- 应用：DSHM `com.dshm.dshclient`（应用 versionName 1.1.4 / versionCode 1010004）。
- 工作方式：**只读侦查**（未安装/启用/停用任何插件，未修改 DSHM 配置，未改写真实会话/工作区，未输出真实聊天正文与任何凭据）。
- 侦查时间：2026-10-24（Asia/Shanghai）。证据路径约定：
  - `CORE=/data/storage/el2/base/haps/entry/files/dsh/cores/0.2.1-rc.2+dshm.73`
  - `AI=$CORE/node_modules/@deepseek-ai`
  - `HOME=/data/storage/el2/base/haps/entry/files/dsh/home`（DSH_HOME）
  - `PROFILE=$HOME/profiles/ondevice`
  - 用户可见工作区：`/storage/Users/currentUser/Download/com.dshm.dshclient`
- bash 工具运行在 Alpine guest 中，`/mnt/share` = 宿主 `.../entry/files`（9p 只读使用）；guest 的 `/storage/...` cwd **不是** host 工作区；写项目文件均用 write/python 工具的 host 路径完成。

---

## 0. 结论摘要

1. **可行，且已具备明确的 DSHM 适配路径**：本工具实现为 DSHM 的**纯宿主 cordis 插件 + `webServer.tapIndex` 注入单文件 UI 脚本**。该形态与已安装且真机跑通的 `dshm-boot-rescue`（纯宿主插件、零静态 inject、prefix 路由 + tapIndex 注入）完全一致；所有 UI 与 API 走 DSHM 自己的 `127.0.0.1:3120` Web 前端，不依赖桌面浏览器或 PC。
2. **数据面已查明**：会话日志、工作区注册表、工作区产出文件、附件、插件名单/版本的存储位置与格式均有源码/实测证据；备份可据此生成单个 ZIP，导入恢复只需写回文件并在 Host 停止后生效。
3. **关键限制（已纳入设计）**：`workspace.json` 是“内存权威 + 整文件原子替换”，**Host 运行期间外部改写可能被运行实例覆盖** ⇒ 恢复流程在写回后要求用户**立即冷重启**（划掉后台重开），重启前不得新建会话；恢复前保存目标原 `workspace.json` 以便回滚。
4. **平台能力边界**：宿主插件内可用 `node:fs / zlib（含 zstd）/ crypto / stream`，可零依赖自实现 ZIP 与 SHA-256；**没有 node CLI、不能创建外部进程、不能触发系统文件选择/分享对话框** ⇒ 采用“用户可见工作区目录 + 界面显示绝对路径 + 备份目录列表选择”的导出/导入方案。
5. **首版迁移承诺**：只承诺 **源端与目标端实际 DSHM 核心版本一致**（含内部构建号，本次实测为 `0.2.1-rc.2+dshm.73`）的迁移；版本不一致时明确警告、要求用户二次确认，不静默按同版本处理。
6. **没有发现会使核心功能无法设计/实现的阻塞**；但“真实安装/加载/备份/恢复/卸载保留”等结论必须留待手机金丝雀验收（见 §8）。
7. **2026-10-24 外部源码复核退回修复（v0.1.1；v0.1.2 第二轮复审、v0.1.3 手机第一阶段 D1 修复见 FIX_REPORT.md）**：ZIP 写管线（CRC/原始大小必须在 deflate 之前）、全新实例判定、
   回滚严格判定、恢复前重检、异常抢备节流/轮换、工作区同名目录误排除、插件清单仅 name/version、
   核心目录权威判定、verified 语义、恢复进度等已修复；插件版本升至 0.1.1。详见 FIX_REPORT.md。
8. **提示词历史基线已过期（按用户规则记录）**：提示词所写 `core 0.2.1-alpha.1` 在当前环境不可见；实测 DSHM 核心构建为 `0.2.1-rc.2+dshm.73`、上游 `@deepseek-ai/dsh` 为 `0.2.1-rc.2`。本工具以实测为准，并把各版本维度分开记录，不合并成一个字符串。

---

## 1. 版本基线（不同维度分别记录，不相互覆盖）

| 维度 | 实测值 | 证据 |
|---|---|---|
| DSHM 应用版本 | versionName **1.1.4** / versionCode 1010004 / targetAPIVersion 260000026 | `/data/storage/el1/bundle/entry.hap!module.json`（Python `zipfile` 读取） |
| DSH 核心构建/补丁版本（权威） | **0.2.1-rc.2+dshm.73**，platform `openharmony/arm64`，profile `ondevice`，builtAt 2026-10-18T01:59:13.380Z，nodeFloor 22.17.0 | `CORE/dshm-core.json` |
| 上游 `@deepseek-ai/dsh` 版本 | **0.2.1-rc.2** | `CORE/node_modules/@deepseek-ai/dsh/package.json` |
| 核心树目录名 | `0.2.1-rc.2+dshm.73` | `CORE` 路径 |
| profile 版本 | `dshm-ondevice-profile@0.0.0` | `PROFILE/package.json` |
| Host 运行时 | node **v24.2.0**；openharmony/arm64；`jitless=true`；`zstd=true` | `HOME/host-ready.json` → `runtime`（token/url 字段不读取、不记录） |
| 用户可见核心版本 | **0.2.1-rc.2**（设置页“当前版本”文案字面量） | `AI/dsh-client-ui-settings-general/lib/client.js:663` |
| 会话格式版本 | **v4**（`session.v4.jsonl.zstd`） | `AI/dsh-session-format-catalog/lib/index.js:41`；文件名规则 `AI/dsh-session-format/lib/index.js:472-479` |
| 工作区注册表格式 | unit `workspace` version **2** | `AI/dsh-workspace/lib/index.js:255-268` |
| 历史基线 `0.2.1-alpha.1` | 当前环境未发现该标识；**提示词基线已过期** | 上述实测；`dshm-core.json`、host-ready、profile、核心包均无 alpha.1 |

> 结论：第一版“同版本迁移”按 **实际核心构建版本字符串完全相等** 判定（`0.2.1-rc.2+dshm.73` == `0.2.1-rc.2+dshm.73`），而不是只看 `0.2.1` 前缀。

---

## 2. DSHM 插件机制与适配依据

### 2.1 最小可加载插件契约（本工具实际采用）

- 包：`dshm-backup@0.1.0`，`"type":"module"`、`"main":"lib/index.js"`、`exports` 指向 ESM 入口与 `./client`、`./cordis.patch.yml`、`./package.json`；`"dsh":{"bundle":{"patch":"./cordis.patch.yml"}}`。
  - 证据：profile 中已安装插件 `dshm-boot-rescue` 的 `package.json`（`PROFILE/node_modules/dshm-boot-rescue/package.json`，字段 `type/main/exports/files/dsh.bundle.patch` 均实测存在）；bundle patch 解析 `AI/dsh-app-boot/lib/index.js:490-530,1024-1027`。
- 注册行：`cordis.patch.yml` 用 `- insert: [{id: dshm-backup, name: 'dshm-backup'}]`，**行 id 与包名一致**（DSHM `.dshm-plugin-rows.yml` 用该 id 表达启停）。
  - 证据：`dshm-boot-rescue/cordis.patch.yml:5-7`；行 id 语义见 `AI/cordis-plugin-loader/lib/index.js:336-344`。
- 入口：ESM `export function apply(ctx)` + `export const name`；不使用 default 导出；无 client 半身。
  - 证据：`AI/cordis-plugin-loader/lib/index.js:437-465`（import → unwrapExports → registry.plugin → apply）；`dshm-boot-rescue/lib/index.js:1739`（实测 apply 形态）。
- **零静态 `export const inject`**：静态 inject 缺服务会让整个 profile 进入 pending（失败页），因此本插件只用 `ctx.get("webServer")`/`ctx.get("loader")` 判空 + `ctx.on("internal/service")` + 定时重试兜底。
  - 证据：`AI/cordis-plugin-loader/lib/index.js:631`；`AI/dsh-app-boot/lib/index.js:4425,4438`；`dshm-boot-rescue/lib/index.js:1789,1820-1841`。
- 依赖：`dependencies` 为空，只用 `node:fs / node:fs/promises / node:path / node:crypto / node:zlib / node:url`；禁止 child_process/Worker/WASM 依赖。
  - 证据：`dshm-boot-rescue/package.json` `dependencies:{}`；DSHM 平台实测 host-ready exec 探针（随包 ELF/子进程被拒）。
- 冷重启：插件条目在进程启动时组装；修改/安装后必须“划掉后台重新打开应用”，页面刷新无效。
  - 证据：`AI/dsh-app-boot/lib/index.js:4522`（boot 仅在进程启动调用）；profile 的 `cordis.patch.yml` 每次启动重拼（`.recon/SPEC.md` 已核实、本轮复核 dshm 壳行为不在 core）。

### 2.2 UI 注入链路（本工具实际采用）

- 宿主插件在拿到 `webServer` 后：
  1. `webServer.register({kind:"prefix", path:"/dshm-backup", handler})` 注册 API；
  2. `webServer.tapIndex(fn)` 在每次 `index.html` 渲染时注入
     `<script defer src="/dshm-backup/client.js"></script>`。
- 证据：`AI/dsh-host-webserver/lib/index.js:326-333`（register）、`:369-375`（tapIndex）、`:533-535`（renderIndex = applyIndexTaps + renderIndexInjections）；`AI/dsh-host-frontend-static/lib/index.js:85`（index 响应走 `renderIndex`）；实例 `dshm-boot-rescue/lib/index.js:1691-1735` 真机跑通。
- 失败页场景：客户端 Web bundle 在 client 树激活失败时渲染 `Failed to load plugins` / `web boot: N entries did not activate`，但 Host/webServer 仍正常返回 HTTP 200 ⇒ 注入脚本仍执行，可在失败页上提供备份入口/触发抢备。
  - 证据：`AI/dsh-web-frontend/dist/assets/index-CN7NIWNw.js:190`（失败页文案与失败卡）；`dshm-boot-rescue/lib/rescue-client.js` 已验证失败页检测范式。
- 为什么不使用官方 `dsh.client` 半身：它依赖 client 插件树成功激活，失败页场景不可用；本工具的核心价值恰在“Host 活着但插件树异常时仍能备份”。证据：`AI/dsh-client-modules/lib/index.js:40-69`、`lib/client.js:580-594`。
- 同源与安全：写接口只接受本机回环 `Host/Origin`、`sec-fetch-site` same-origin/none、`content-type: application/json`、body ≤64 KiB；由插件自行校验（webserver 层不做鉴权）。证据：`AI/dsh-host-webserver/lib/index.js:251-257`；`dshm-boot-rescue/lib/index.js:1140-1166` 的真实校验范式；`HOME/host-ready.json` 的 `baseUrl=http://127.0.0.1:3120`。

### 2.3 安装方式（交付给手机验收时使用，平板不执行）

- 设置 → 插件 → 安装插件输入：`github:<owner>/<repo>#<branch>&path:/<subdir>`（已验证语法；`#ref` 在现有 profile 有实据，`&path:` 子目录形式本机无实例、标“未确认”）。
- 或端侧 `pnpm add <spec>`（队列假壳，Host 进程内安装器接单）。安装成功后插件行写入 profile，但**必须冷重启**才挂载。
- 本工具不做自动安装、不在运行时修改 profile/rows 配置。

---

## 3. 数据层：会话、工作区、产出文件、插件清单

### 3.1 DSH_HOME 结构与会话存储

- `HOME` 顶层实测：`sessions/`、`storages/`、`profiles/`、`attachments/`、`skills/`、`dsh-market/`、`install-queue/`、`open-link/`、`whale-*/`、`channel-pack/`、`openai-gateway/` 等；根文件含 `.credentials.yaml`（未读取）、`host-ready.json`（含 token，只读取非敏感字段）、运行态统计文件。
- 会话：`HOME/sessions/--<projectKey>--/<encodeSegment(id)>/session.v4.jsonl.zstd` + `session.lock`。
  - 路径编码规则已在 `lib/format.js` 中按 core 源码实现（`encodeSegment`/`projectKey`），证据：`AI/dsh-session-persistence-jsonl/lib/index.js:881-978`。
- 会话格式：多 zstd 帧拼接（第一帧恰一行 v4 header；每批事件一帧），append+fsync，无整文件重写；崩溃可恢复末尾残缺帧。
  - 证据：`AI/dsh-session-persistence-jsonl/lib/index.js:50`（node:zlib zstd）、`:780-880`（header 校验 `isHeaderLine`）、`:1321-1402`（帧扫描/压缩）、`:3302-3369`（append + truncate 回滚）。
- 一致性读取：core 自己的 `readStableSnapshot` 用 stat identity（dev:ino:size:mtimeNs:ctimeNs）前后比对，变化重试一次，仍变化则返回“第一次 stat 的 size 前缀”。
  - 证据：`AI/dsh-session-persistence-jsonl/lib/index.js:1666-1706`。本工具 `util.readStableFile` 按同一策略实现，并在 manifest 标记 `stable`/`torn`/`complete`。
- 写锁：`session.lock` 是 POSIX `flock(2)` 非阻塞独占锁，无租约/TTL，进程死亡释放；**读者不碰锁**，不能用锁内容判活跃。
  - 证据：`AI/dsh-session-persistence-jsonl/lib/index.js:651-770`。

### 3.2 工作区注册表 workspace.json

- 位置：`HOME/storages/workspace.json`（storage-json 单文件布局 `<root>/<name>.json`，证据 `AI/dsh-storage-json/lib/index.js:159-176`）。
- 结构：`{unit:{name:"workspace",version:2}, global:{initialized,defaultWorkspaceId?,workspaceIds,archivedSessionIds,pinnedSessionIds,pendingMutation?}, tables:{workspaces:{<id>:{path,title,sessionIds,createdAt,updatedAt}}}}`。
  - 证据：`AI/dsh-workspace/lib/index.js:204-268`；序列化/原子写 `AI/dsh-storage-json/lib/index.js:6-83`。
- 语义：写路径“先落盘成功再改内存”，整文件临时文件 + fsync + rename 原子替换，单写者 last-write-wins ⇒ **Host 运行期间外部改写可能被内存态覆盖**（这是恢复必须冷重启的原因）。
- 启动引导：`initialized=false` 时会从所有 stored session header 的 `cwd` 重建工作区（标题=目录名，按最新会话时间排序），但会丢失用户改过的标题、顺序、归档/置顶；`initialized=true` 时按记录与 header 索引过滤/成员校验。
  - 证据：`AI/dsh-workspace/lib/index.js:379-396,758-832,875-915`。
- 目标“全新实例”判定（本工具采用）：无 stored 会话日志、无归档会话、workspace.json 不存在或 initialized=false 或工作区记录均为空。证据同上；工具实现 `format.assessFreshTarget`。

### 3.3 工作区产出文件与路径关联

- 会话 header 的绝对 `cwd` 即工作区路径，与 workspace 记录的 `path`（realpath 规范化）必须相等；工作区目录本身是用户可见目录（本机 `/storage/Users/currentUser/Download/<dir>`）。
- 证据：`AI/dsh-session-persistence-jsonl/lib/index.js:878-880`（cwd 必须绝对）；`AI/dsh-workspace/lib/index.js:875-915`（header.cwd 与 workspace.path 校验）；`AI/dsh-workspace/lib/index.js:111-128`（attach 要求 realpath 相等且目录存在）。
- **恢复路径重映射的必需动作**（已实现）：目标路径不同时，① 重写会话 header 第一帧的 `cwd`（其余 zstd 帧字节不变）；② 按新 cwd 计算 `projectKey` 目录；③ 同步改写 workspace.json 的 `path`。若不重写，会话会被工作区成员校验过滤或找不到日志目录。

### 3.4 附件与其它资源

- 附件：`HOME/attachments/v1/{files,file-objects,tmp}`，消息以内容寻址引用；`tmp/` 排除。证据：profile patch 行配置（`AI/dsh-base/cordis.patch.yml:143-145` 一线）与实测目录结构。
- 可重建/排除：`storages/session_projcache/**`（投影缓存）、内存 SQLite 搜索索引（`:memory:`）、第三方组件状态（`.dshw-*`、`card-*`、`whale-*` 等）、插件目录/node_modules、插件安装包、缓存/临时/日志。
- 插件名单来源（只名称+版本）：
  - 用户插件：`PROFILE/package.json` 的 `dependencies`（spec）+ `PROFILE/node_modules/<pkg>/package.json` 的 `version`；
  - 核心插件：`CORE/dshm-core.json` 的 `plugins[]`（id/name/bundle/disabled）+ `CORE/node_modules/<pkg>/package.json` 的 version；
  - 运行期 inventory（`pluginInventory.list()`）只有 entryId/moduleName/enabled/fiberPhase/meta，**无 version 字段**，故版本必须读 package.json；缺版本标 `unknown`。
  - 证据：`AI/dsh-host-plugin-inventory/lib/index.js:112-157`；实例 `PROFILE/node_modules/dshm-boot-rescue/package.json` = 1.0.0。

---

## 4. 运行时能力与导出/导入边界

| 能力 | 结论 | 证据 |
|---|---|---|
| node 运行时 | v24.2.0，openharmony/arm64，jitless=true，zstd=true | `HOME/host-ready.json` runtime |
| `node:zlib` | 原生且含 zstd API（核心实际用于会话文件） | `AI/dsh-session-persistence-jsonl/lib/index.js:50,1328-1436` |
| `node:crypto` / `node:fs` / `node:stream` | 可用（宿主自身使用） | 宿主入口 main.js（resfile app/main.js）与核心包实际 import |
| ZIP 库 | 核心树无 yazl/jszip/adm-zip/archiver；fflate/pako 只是传递依赖，不作契约 ⇒ **零依赖自实现 ZIP** | `CORE/package-lock.json` 关键词检索；`AI/dsh-session-log-export` 使用 fflate |
| node CLI | **不可用**（随包 ELF/子进程被平台拒绝；无 node 可执行） | host-ready `runtime.exec`/`terminal` 探针；`.recon/SPEC.md` |
| 创建进程/外部 shell | 不可用（EACCES/E15） | host-ready `runtime.exec.results`、`pty-spawn=false` |
| 系统文件选择/保存/分享 | **纯宿主插件不可行**（宿主↔ArkTS 只有启动期 env 与文件队列，无 pick/save RPC） | host-ready、`main.js` 工作区认领逻辑、`AI/dsh-host-directory-picker*` 等包均非插件可调用公开 API |
| 用户可见可写目录 | `/storage/Users/currentUser/Download/<包名>/`（宿主可读写删；卸载后属用户目录，重装同包名可再获授权） | 宿主 main.js 工作区认领与探写（`workspaceProbeWritable`）；本机 host-ready.workspace 实测为该路径 |
| 定时器 | `setTimeout/setInterval` 可用；插件随 Host 进程生命周期，无后台服务 | host-ready；`cordis-plugin-timer` 已启用（`CORE/dshm-core.json` plugins：id=timer disabled=false） |
| 单实例锁 | `fs.open(...,"wx")` + pid stale 判定可用 | `AI/atomic-write` 同款协议；宿主 main.js 锁实现（withFileLock） |
| 磁盘空间 | `fs.statfs` API 存在，OHOS 实际行为**未实测**（工具 try/catch 降级） | 标“未确认/待手机” |

**运行核心版本判定（v0.1.1 修复）**：优先使用 Host 注入的权威环境变量 `DSHM_CORE_DIR`
（宿主 main.js 注释明确为核心树根目录），其次 `DSH_CORE_DIR`/`DSH_CORE_PATH`、`process.argv` 中实际
加载的核心路径；取不到时 `coreDir=null`、`coreVersion="unknown"`，不再按 cores 目录名/字典序猜测。
unknown 不参与“同版本”判定，恢复需要二次确认。残余风险：若未来 DSHM 不再注入该变量且 argv 无核心
路径，则版本保持 unknown。

**导出方案（已实现）**：
- 备份包写到用户可见目录 `<当前默认工作区>/dshm-backup/backups/`（本机实测工作区为 `/storage/Users/currentUser/Download/com.dshm.dshclient`）；界面显示“用户可见路径”（实际绝对路径）与校验值；该目录若在工作区内，备份时自动排除，避免递归打包。
- “卸载后仍保留”依据：路径属于 `/storage/Users/currentUser` 用户个人盘，不属应用沙箱；但 HarmonyOS 卸载行为必须以手机真机验收为准（§8）。

**导入方案（已实现）**：
- 用户用文件管理器把 ZIP 放进上述 `backups/` 目录；插件在 UI 列表里展示并可“预览/校验/恢复”。不依赖系统文件选择器，也不要求 PC。可选 `<input type=file>` 上传因无法确认 ArkWeb 文件选择与插件路由容量，首版不做（标未确认）。

---

## 5. 备份包设计（与实现一致）

- 单个 ZIP（零依赖写入：`node:zlib.createDeflateRaw` + 自写 CRC32 + data descriptor；读取用 `inflateRaw`；不支持 ZIP64，单包硬上限 <4 GiB）。
- 结构：
  - `manifest.json`：格式版本、备份类型（manual/baseline/emergency/pre-restore）、创建时间、来源版本（各维度分开）、会话/工作区/文件/附件清单及逐个 SHA-256、跳过/不完整原因、排除项、警告、统计。
  - `data/workspace.json`：工作区注册表原始快照 + sha256。
  - `data/sessions/<encodeSegment(id)>/session.v4.jsonl.zstd`：会话日志快照（不完整会话标记 `complete:false`，恢复时默认跳过并报告）。
  - `data/workspace-files/ws-NNNN/<rel>`：工作区产出文件。
  - `data/attachments/<rel>`：附件（排除 tmp）。
  - `data/plugin-inventory.json`：插件名单+版本（仅名单/版本，不含插件目录/安装包）。
- 唯一文件名 `dshm-backup-<kind>-<UTC时间戳>-<随机>.zip`；同目录临时文件写完 → 重新读取逐文件 SHA-256 校验 → 原子 rename → 写 `.sha256` 旁文件 → 发布后再从最终路径复核；失败删除不完整文件，不覆盖旧备份。
- 自动轮换只处理 baseline/emergency，手动备份永不删除；只在**新备份校验成功后**轮换。

---

## 6. 恢复设计（与实现一致）

流程：选择备份 → **只读检查**（列出 ZIP 中央目录、读 manifest、逐文件 SHA-256、旁文件校验；不写目标）→ 预览（目标是否全新、版本比较、逐工作区路径映射、冲突/警告、跳过的不完整会话）→ 用户明确确认 → 解压到 staging 并逐文件校验 → 应用（工作区目录 → 会话日志（必要时重写 header.cwd）→ 附件 → 工作区文件 → workspace.json）→ 复验 → 报告；失败则回滚（删除本次创建项、恢复目标原 workspace.json、清理 staging）。

安全防线（全部实现）：
- Zip Slip：条目名拒绝绝对路径/盘符/反斜杠/`..`/控制字符/超长/重复；解压后 realpath 必须在目标目录内；拒绝符号链接条目。
- 只允许 method 0/8；拒绝加密、ZIP64、超大/压缩比异常的 zip bomb。
- 目标不是全新实例：**默认拒绝**（首版不提供覆盖模式），避免破坏真实实例。
- 会话重名/文件冲突：预览阶段只读检测并拒绝执行。
- 版本不一致：核心版本或会话/工作区格式不一致时明确警告；格式不兼容拒绝执行；核心版本不一致需 `allowVersionMismatch` 二次确认。
- 恢复成功后必须冷重启；UI 与报告都明确提示“重启前不要新建会话”。
- 恢复前保存目标 `workspace.json` 原文；失败时写回并删除本次创建的文件/会话目录。
- 绝不安装/启用/停用任何插件；插件清单仅供人工核对。

**已知残余风险（如实记录）**：
1. `workspace.json` 在 Host 运行期间写回，存在被运行中实例内存态覆盖的窗口；只能通过“立即冷重启”规避，无法在插件内彻底消除（除非 DSHM 提供官方导入 API/停机导入通道，当前未发现）。
2. 会话日志若在备份瞬间仍在写入，可能丢最后 ≤200ms 已发布事件或末尾不完整帧；manifest 会标 `stable=false`/`torn=true` 并要求恢复时跳过或提示（不静默当完整）。
3. 恢复用脚本重写 header 第一帧（仅在路径映射时）；首版只在手机真机验证后使用。
4. 系统文件选择/分享不可用；“卸载后保留”仍需手机真机验证。

---

## 7. DSHM 适配逐项自检（交付产物 ↔ 实测机制）

| 要求 | 本工具做法 | 证据/位置 |
|---|---|---|
| 插件声明 | package.json `type/main/exports/dsh.bundle.patch`；`files` 含入口、client、patch | `dshm-backup/package.json`；对照 `PROFILE/node_modules/dshm-boot-rescue/package.json` |
| 注册行 | `cordis.patch.yml` `- insert: [{id: dshm-backup, name: dshm-backup}]` | `dshm-backup/cordis.patch.yml` |
| 入口/模块格式 | ESM `export name/version/apply`；无 default、无 client 半身 | `dshm-backup/lib/index.js`；core loader `AI/cordis-plugin-loader/lib/index.js:437-465` |
| 零 inject | 无 `export const inject`；`ctx.get` + `internal/service` + 定时重试；失败只降级 | `lib/index.js` `attachWebServer/tryAttach/apply` |
| 页面注入 | `webServer.tapIndex` 注入 `<script defer src="/dshm-backup/client.js">`；幂等 marker | `lib/index.js` `injectClientScript`；webserver 证据同 §2.2 |
| 路由 | `webServer.register({kind:"prefix", path:"/dshm-backup"})` | `lib/index.js` `attachWebServer` |
| 数据目录 | 只读写 `DSH_HOME` 内与用户可见 `Download/<包名>` 工作区；不写 profile/rows | `lib/util.js` `resolveRuntimePaths`；`lib/backup.js` |
| 安全写入 | 回环 Host/Origin/Sec-Fetch-Site/content-type/body 上限；无遥测 | `lib/index.js` `assertMutationRequest/readJsonBody` |
| 依赖 | `dependencies: {}`，只用 node: 内置 | `dshm-backup/package.json`；`lib/*.js` import 清单 |
| 备份格式 | 单 ZIP + manifest + SHA-256；临时文件→校验→原子发布 | `lib/zip.js`、`lib/backup.js` |
| 恢复语义 | 只读预览/明确确认/路径安全/版本警告/冷重启/回滚 | `lib/restore.js`、`lib/client.js` |
| 版本敏感点 | `webServer.register/tapIndex`、`loader.entries/fiber.state`、`internal/service`、profile 路径约定集中隔离在 `index.js`/`util.js`；失败只降级 | 见 §2/§4 与 `lib/index.js` 诊断日志 |

---

## 8. 未确认项与必须留待手机金丝雀验收的事项

| # | 项 | 平板状态 | 手机验收方式 |
|---|---|---|---|
| 1 | 真实安装插件、ESM 模块解析/加载、冷重启后 UI 注入 | 未执行（平板禁止安装本插件） | 按 HANDOFF_PHONE.md 安装 + 重开应用，检查设置→插件可见、页面右下角按钮 |
| 2 | 真实主动备份（含正在写入的会话） | 仅合成测试 | 建立可丢弃合成会话 → 立即备份 → 检查备份列表/大小/SHA-256 显示 |
| 3 | 备份文件导出到应用外、文件管理器可见、卸载重装后保留 | 路径性质已确认；卸载行为未验证 | 备份后到系统文件管理器查看 `Download/com.dshm.dshclient/dshm-backup/backups/`；卸载重装后复查 |
| 4 | 全新实例恢复：对话真实出现在新 DSHM 会话列表 | 仅合成测试 | 卸载重装 → 重装本插件 → 导入备份 → 预览 → 恢复 → 冷重启 → 核对会话/工作区/文件 |
| 5 | 路径重映射（目标路径不可用时）表现 | 逻辑已实现、未真机 | 恢复后检查工作区路径与产出文件可打开；核对会话内文件引用 |
| 6 | workspace.json 运行期写回后的冷重启效果/覆盖风险 | 代码层已防护（立即重启用） | 恢复后按指引冷重启；检查工作区标题/顺序/会话归属；再重启一次确认持久 |
| 7 | 插件清单（只名单+版本、导入不装插件） | 合成测试 | 预览/恢复报告核对清单；确认插件页无新增/停用 |
| 8 | 不完整会话（备份时仍在写入）的标记与恢复跳过 | 合成测试 | 备份时保持对话写入，查看 manifest/报告的 incomplete 标记与恢复跳过项 |
| 9 | 失败页救援模式（插件树异常时仍可备份） | 仅代码路径 | 另行批准后可安装已知会 pending 的测试插件（不得用 brickpack），观察失败页是否出现备份入口并可备份 |
| 10 | `fs.statfs`、`deflateRaw/inflateRaw`、`crc32`、`fsync` 的 OHOS 精确行为 | API 存在性有证据；逐项未实测 | 插件运行日志/备份成功/校验结果验证；失败时按实现内降级路径处理 |
| 11 | 系统文件选择/上传导入（首版未用） | 判定纯宿主插件不可行 | 如后续版本采用 `<input type=file>`，另立验收 |
| 12 | 大备份（内存/耗时/LOOP-GAP） | 未实测 | 10/100/500 MB 档位观察 UI 是否卡顿、任务是否完成 |
| 13 | 跨版本恢复（首版只承诺同版本） | 不在承诺范围 | 版本不一致时核对警告与拒绝/确认行为 |

---

## 9. 安全与敏感数据边界

- 备份默认排除：`.credentials.yaml`、`host-ready.json`（含 token）、`openai-gateway/api-key`、`.anonymous-user-id`、插件目录/node_modules/安装包/插件市场缓存、会话投影缓存、附件 `tmp/`、锁文件、缓存/临时/日志、备份目录自身。
- 备份包含：会话正文（用户自己的对话记录——这是迁移目标本身，不是“凭据”）、工作区产出文件、附件、workspace.json、插件名单+版本。README 必须提示“备份 ZIP 含明文聊天记录，请妥善保管”。
- 报告/日志/界面不写 token、cookie、密码；真实聊天正文不写入侦查报告或测试报告。
- 平板端本次未备份、未打包任何真实档案；合成测试全程使用项目内 mock 数据。

---

## 10. 关于当前工作区（安全备注）

- 当前会话 host 工作区为 `/storage/Users/currentUser/Download/com.dshm.dshclient`，其目录并非严格空目录：含前次任务遗留 `.recon/`（boot-rescue 侦查资料）、`dshm-boot-rescue/`（旧插件拷贝）等。按用户“不得写入真实档案目录”的要求，本任务所有写入都限制在新建的 `dshm-backup-dev/` 子目录内，未触碰任何遗留文件；本文件把该差异如实记录。若用户要求更严格隔离，可把 `dshm-backup-dev/` 整体迁移到用户新建的空工作区。
- 本任务未在平板安装/启用/停用任何插件，未修改 `cordis.patch.yml` / `.dshm-plugin-rows.yml` / profile dependencies。

---

## 11. 结论

- **DSHM 适配可行**：插件形态、入口、注册行、UI 注入、数据路径、运行时能力均有实测/源码证据，产物按上述机制实现。
- **无阻断性阻塞**：所需数据均可取得，导出到卸载后仍可能保留的位置已具备条件，恢复所需索引（workspace.json + 会话 header）可完整备份与重建。
- **必须留待手机确认的结论**：真实安装/加载、真实备份、真实全新实例恢复、卸载重装保留、失败页救援与路径重映射在真机上的表现（§8）。
- 在手机金丝雀验收完成、缺陷修复并经用户明确授权前，**不得**建议把本插件安装到平板主力实例，也不得指导在平板上执行真实恢复。
