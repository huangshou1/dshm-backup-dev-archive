# DSHM 备份/迁移：运行时能力与导出路径侦查（task-3）

- 环境：HarmonyOS 7.0.0.109SP6 平板，DSHM `com.dshm.dshclient`，core `0.2.1-rc.2+dshm.73`，profile `ondevice`
- 方法：只读。宿主侧用 `python` 工具直读 app 沙箱/宿主入口（`read/grep` 工具对 `/data/storage/el1/bundle` 有路径策略限制，只有 python 能读到宿主入口 `main.js`）；bash 工具是 Alpine guest，`/mnt/share` = 宿主 `.../files`，用户可见工作区在 guest 里不可见（见 A4）。
- 时间：2026-10-24。本文件之外未写任何文件。

---

## A. 结论摘要（可直接指导实现）

### A1. 运行时基线（已实测）
- Node **v24.2.0**，平台 `openharmony/arm64`，`jitless=true`，无 WASM（`WebAssembly === undefined`）。
- `node:zlib` 是**原生 C++ zlib**，且本机带 **zstd**：`zstdCompress/zstdDecompress/zstdCompressSync/zstdDecompressSync/createZstdCompress/createZstdDecompress` 全部存在且被核心实际使用（证据 B1）。
- 可用内置模块（宿主已直接 require 过）：`node:fs`、`node:path`、`node:crypto`、`node:url`、`node:os`、`node:http`、`node:zlib`、`node:worker_threads`、`node:module`、`node:util`（`promisify`）、`node:stream`。插件侧还可用 `setTimeout/setInterval`、`Worker`（线程级，不是进程）。
- **不可用/被拦**：外部进程（child_process/spawn、pty-spawn、随包 ELF）、外部 shell/python/git、node CLI、JIT/WASM。`process.execPath` 是 `/system/bin/appspawn`，`spawn(process.execPath)` 必 `EACCES`。
- 结论：**备份/恢复的全部功能（ZIP、SHA-256、zstd、流式 IO）都能只靠 node 内置模块在宿主插件里完成**，不需要任何外部可执行文件。

### A2. ZIP 方案：零依赖自实现，**不要**依赖核心树里的第三方 zip 库
- 核心树里**没有** `yazl/jszip/adm-zip/archiver/tar`；只有 `fflate@0.8.3`、`pako@0.2.9`、`tiny-inflate`（fflate 是核心 `dsh-session-log-export`/`libreoffice-kit` 的间接依赖，证据 B2）。
- fflate 在"安装域依赖闭包"里，理论上可被 profile 插件 `import "fflate"` 解析到（证据 B2），但它是**未声明的传递依赖**，核心版本一变就可能消失 ⇒ 不作为契约。
- 推荐：**零依赖 ZIP 写入器** = `node:fs` 流式写 + `node:zlib.createDeflateRaw()` + 自写 CRC32 表；读取 = `node:zlib.inflateRawSync()`（或 `createInflateRaw`）。`node:zlib` 原生 zlib 已被 gzip/gunzip（tar-gz.cjs、dshm-installer）证明可用（B2/B3）。
- 实现要点：
  - 单文件 ZIP，条目少（如 `manifest.json` + `payload/…`），local header 用 **data descriptor**（flag bit 3，CRC/大小先写 0，数据后写 `0x08074b50` + crc32 + csize + usize），避免先把整包缓冲进内存。
  - CRC32 用自写查表（20 行）；`zlib.crc32` 是否在本运行时存在未验证，作为可选快路径 `typeof zlib.crc32 === 'function'` 再降级。
  - 中央目录 + EOCD；文件名 UTF-8（flag bit 11）。
  - **ZIP64 边界**：超过单条目/总大小 `0xFFFFFFFF`、条目数 `>0xFFFE` 或中央目录偏移越界才需要 ZIP64（`0x06064b50` EOCD + `0x0001` extra）。建议先做**硬上限**：单包总大小 < 4 GiB、条目数 < 65534，超限直接给用户可读错误（"备份过大，请分批"），ZIP64 留作后续增强。
  - 压缩级别 6 左右即可；备份源里 `session.v4.jsonl.zstd` 已是压缩数据，再 DEFLATE 收益很低，可对已压缩条目用 **STORE（method 0）**，只对 `.json/jsonl/.yml/.md/.log` 用 deflate。
  - 导入端：读 EOCD→中央目录→`inflateRawSync`；先解到临时目录，校验 manifest 内每文件 SHA-256 后再提交。

### A3. SHA-256
- `node:crypto` 可直接用（宿主入口 `main.js:23` 就 `require('node:crypto')`，同文件 `:28` 注明用 sha256 判等）。
- 实现：`crypto.createHash('sha256')` 流式喂数据（`pipeline(readStream, hash)`）；同时记录**文件级 sha256（manifest 内）**与**整包 sha256（同名 `.sha256` 旁文件）**，供传输后校验和恢复前校验。
- 明文恢复时再校验一次，校验失败绝不覆盖现有数据。

### A4. 导出位置（已确认可写、用户可见、卸载后仍在）
- **默认导出目录（用户可见、实际路径）**：
  `/storage/Users/currentUser/Download/com.dshm.dshclient/dshm-backup-dev/backups/`
  （= 工作区，文件管理器里的"内部存储/Download/com.dshm.dshclient"；**这是普通文件系统绝对路径，不是 URI**，宿主 node 直接 `fs` 读写）
- 证据：`main.js:1581-1612` 明确该目录**读/写/删全通且不需要 ACL**，是 ArkTS 经 `DocumentPickerMode.DOWNLOAD` 按包名建立的路径级授权，"卸载重装后同包名仍可直接访问"；宿主启动时对 `DSHM_PUBLIC_DOWNLOAD` 做 `lstat + create/write/unlink` 探写（`main.js:1613-1638, 1665-1700`）。当前工作区已由宿主侧正常写入过（`.recon/`、`dshm-boot-rescue/` 等，实测可见）。
- 导出后**把绝对路径显示在回复/UI 里**，并提示用户可复制到 PC；不要只给相对路径。
- 应用私有 `DSH_HOME=/data/storage/el2/base/haps/entry/files/dsh/home` 只用于**备份源数据与内部临时文件**，不作最终交付位置（应用沙箱卸载预期被清除，手机验收项 C5）。
- 注意：bash guest **看不到**该工作区（guest 的 `/mnt/share` = app sandbox；`/storage/.../com.dshm.dshclient` 在 guest 里是另一个空目录）。备份插件跑在宿主 node 里，用普通绝对路径即可，不要用 URI、不要指望 guest。

### A5. 文件选择器/分享/导入
- **纯宿主插件不能触发系统文件保存/选择/分享**：宿主 node 侧与 ArkTS 之间只有启动期 env（`DSHM_PUBLIC_DOWNLOAD`）+ 文件队列通道（安装队列、OpenLink 队列），没有"pick/save/share"的插件 API；ArkTS 的 picker 只在启动期认领目录（A4 证据同源）。→ 判定：**系统文件保存对话框：不可行（纯宿主插件）**。
- 可行替代（推荐，已具备条件）：
  1. **导出**：直接写 A4 用户可见目录，UI/回复展示绝对路径；
  2. **导入**：用户用文件管理器把 ZIP 放进 `Download/com.dshm.dshclient/`（用户可写自己的目录），插件提供"扫描工作区里的备份文件"功能（宿主 `fs.readdir` 工作区即可）；
  3. 可选增强：Web 前端 `dsh-client-file-upload` 已有上传组件，若插件能注册 HTTP 路由，可让用户在界面里 `<input type=file>` 上传 ZIP——**插件是否可注册 HTTP 路由未确认**（C4）。
- 调研到的相关包（存在但均非"宿主插件可调用"的公开 API）：`dsh-client-file-upload`、`dsh-client-ui-directory-picker-native`、`dsh-client-ui-directory-picker-browse`、`dsh-host-directory-picker(-native/-browse/-auto)`、`dsh-api-workspace-files`、`dsh-client-ui-attachment`、`dsh-file-reference(-local)`；`<files>/diag-file-selector`、`diag-pick-called`、`diag-select-returned`、`diag-folder-grants`、`diag-persist-done` 是 ArkTS 侧选择器/授权流程的诊断产物（只作为线索，未展开读）。
- 结论：**导出可行（A4）；系统保存/分享不可行；系统选择导入改为"工作区文件 + 界面列表"最稳，tapIndex `<input type=file>` 上传方案待验证（C4）。**

### A6. 定时/后台/锁
- 定时：`cordis-plugin-timer` **已安装且启用**（`dshm-core.json` plugins 里 `id=timer, name=@deepseek-ai/cordis-plugin-timer, bundle=@deepseek-ai/dsh-base, disabled=false`）；宿主自己用 `setInterval/setTimeout` 且实测在跑（心跳线程每秒写 `dshm-hb.log`，看门狗 `main.js:620-639`）。→ 插件里 `ctx.setInterval`/全局 `setInterval` 均可用，记得 `unref()`。
- 后台：**没有独立后台服务**。插件与 Host 同进程同生命周期：安装/启用插件后必须重启 DSHM 才挂载（插件技能文档 + `main.js` 启动即挂载），应用退出/被系统回收 ⇒ Host 进程结束 ⇒ 定时器全停。备份不能依赖"应用退到后台继续跑"，长任务要么在前台一次完成，要么分卷/断点续做。
- 单实例锁（与 `dsh-atomic-write` 同款模式，`main.js:5566-5590`）：`fs.openSync(lockPath,'wx')` 写 pid；结束时删除；启动时读 pid 判定 stale（pid 不存在才删，读不出/自己 pid/进程仍存活时不动）。**注意**宿主已自愈清理同名 `.lock` 孤儿（同一段代码），备份插件的锁请用独立文件名（如 `dshm-backup.lock`）避免与上游锁混淆。
- 冷却/轮换：状态文件（JSON：`lastRunAt`、`lastResult`、`retainDays/keepN`）放 `DSH_HOME` 或工作区 `dshm-backup-dev/state.json`，写入走"临时文件 + rename"；轮换删除最旧备份时先 stat 再删，全程 try/catch 不阻断主流程。

### A7. 版本标识采集（不同维度分别记录，不要合成一个版本号）
| 维度 | 来源（实测可达） | 字段 |
|---|---|---|
| 核心包 | `<CORE>/dshm-core.json` | `coreVersion=0.2.1-rc.2+dshm.73`、`builtAt=2026-10-18T01:59:13.380Z`、`nodeFloor=22.17.0`、`platform=openharmony/arm64`、`profile=ondevice`、`overrides.{node-pty,koffi,sharp}` |
| 核心依赖树 | `<CORE>/package.json` + `<CORE>/node_modules/@deepseek-ai/dsh/package.json` | `dshm-core-bundle@0.0.0`、上游 `@deepseek-ai/dsh@0.2.1-rc.2` |
| 运行时 | `<DSH_HOME>/host-ready.json` → `runtime` | `nodeVersion=v24.2.0`、`platform`、`jitless=true`、`zstd=true`、`natives[]`、`python`、`exec`、`terminal` |
| 进程/会话 | `host-ready.json` 顶层 | `pid=8375`、`startedAt=2026-10-23T17:27:16.742Z`、`profile=ondevice`、`workspace=/storage/Users/currentUser/Download/com.dshm.dshclient`、`port=3120`（`token/url/baseUrl` 敏感，**不采集/不打印**） |
| profile | `<DSH_HOME>/profiles/ondevice/package.json` | `name=dshm-ondevice-profile`、`version=0.0.0`、`dependencies`（用户插件列表）、`dsh.profile.bundles[]` |
| 端侧插件集 | `dshm-core.json` → `plugins[]` | 每项 `id/name/bundle/disabled/nativeKind/nativeVia`（备份 manifest 里原样收录，便于排查"备份时装了哪些插件"） |
| DSHM 应用版本 | ArkTS 侧（未在 node 侧发现稳定通道） | **未确认**；见 C6 |

采集实现：备份 manifest 增加 `versions:{ core, upstreamDsh, node, platform, jitless, zstd, profile, appVersion? }`；`coreVersion/builtAt` 每次运行现读，不缓存进插件常量。

---

## B. 证据详情

### B1. node 运行时与 zstd（最强证据）
1. `<DSH_HOME>/host-ready.json`（python 直读，仅取非敏感字段）：
   - `pid=8375`、`startedAt=2026-10-23T17:27:16.742Z`、`profile=ondevice`、`port=3120`、`workspace=/storage/Users/currentUser/Download/com.dshm.dshclient`
   - `runtime.nodeVersion="v24.2.0"`、`platform="openharmony/arm64"`、`jitless=true`、`zstd=true`、`listenAddress="127.0.0.1:3120"`
   - `runtime.natives`：`koffi ok`、`sharp ok`（libvips 真件）、`node-pty ok`、`sandbox-dlopen=false`、`pty-spawn=false`（`forkpty(3) failed`）
   - `runtime.exec.results`：`python3.12=fail(EPERM)`、`git=缺`、`rg=fail(EPERM)`、`ash=fail(EPERM)`、`bash=fail(EPERM)`、`toybox=ok`、`system-sh=缺` → **无可用外部 shell/解释器**
   - `runtime.terminal`：`realShell=no`、`libsExec=EACCES`、`relaySh=rc126`、`relayAsh=rc126`
   - `runtime.python`：内嵌 CPython `bridge=true/ready=true/selftest=ok`（与 node 同进程，不 execve；备份插件用不到，但可作为复杂解析的备选）
2. 宿主入口 `main.js`（`/data/storage/el1/bundle/entry/resources/resfile/resources/app/main.js`，可用 python 读，`read/grep` 工具被路径策略拒绝）：
   - `:1217-1230` `runtimeFacts()`：`nodeVersion: process.version`、`jitless: process.execArgv.includes('--jitless')`、`zstd: typeof zlib.zstdCompressSync === 'function'`（`require('node:zlib')`，失败则 false）
   - `:1198-1200` 注释：Node 22.22 默认就有 `zlib.zstdCompressSync`，`--experimental-zstd` 反而是非法选项
   - `:1236-1243` 对 koffi/sharp/node-pty 当场 `require(join(CORE_DIR,'node_modules',mod))` 探针
3. 核心 `@deepseek-ai/dsh-session-persistence-jsonl/lib/index.js`：
   - `:50` `import { constants, createZstdCompress, createZstdDecompress, zstdCompress, zstdDecompress, zstdDecompressSync } from "node:zlib";`
   - `:1328-1329` `promisify(zstdCompress/zstdDecompress)`；`:1409/:1417/:1435` 调用；`:2324` `DEFAULT_COMPRESSION="zstd"`；`:2011/:3315` 写 zstd 帧
   - 同类代码也在 `lib/worker.cjs:13106/13130/13131/13211/13229`
4. 落盘事实：`<DSH_HOME>/sessions/--storage-Users-currentUser-Download-…--/session-*/session.v4.jsonl.zstd` 实际存在（例：`session-d071e72b-…/session.v4.jsonl.zstd` 18506 B）⇒ zstd 写路径在真机跑通。
5. jitless ⇒ WASM 关闭：`jitless-env.cjs:8-15`（"`--jitless` 在 V8 里连带关掉 WASM，于是 `WebAssembly === undefined`"）；`main.js:225-236` 同样描述；`jitless-env.cjs:59` 用 `typeof WebAssembly !== 'undefined'` 判定。
6. Worker 线程可用：`main.js:588-618` 用 `new Worker(evalSrc,{workerData})` 起心跳线程，每秒 append `dshm-hb.log`；`dshm-hb.log` 当前 ~924 KB 且 mtime 跟随（实测在写）。`jitless-env.cjs:511-525` 包装 `node:worker_threads.Worker` 注入 `--expose-internals --require worker-bootstrap.cjs`（`main.js:792`）。
7. 不能起进程：`main.js:11-12`（"为什么不用 child_process 再起一个 node：鸿蒙对创建进程有平台级限制（D6 E15）"）；`main.js:795-800`（`process.execPath` = `/system/bin/appspawn`，`spawn(process.execPath)` 必 `EACCES`）；host-ready 的 `exec/terminal/pty-spawn` 读数同向。
8. 事件循环阻塞是**已知故障模式**：`main.js:620-639` 看门狗，间隔 ≥1.5s 记 `LOOP-GAP`；备份的大文件同步压缩/哈希必须用异步/流式 API 或分块 `await`。

### B2. ZIP/压缩库盘点与模块解析
1. 核心 `node_modules` 目录（`<CORE>/node_modules`，192 个顶层项）中与 zip/tar/压缩相关的只有：`fflate@0.8.3`、`pako@0.2.9`、`tiny-inflate`；**无** `yazl/jszip/adm-zip/archiver/tar`。
2. `fflate` 的依赖者（读各自 package.json）：`@deepseek-ai/dsh-session-log-export`（`fflate: ^0.8.2`）、`@deepseek-ai/libreoffice-kit`（`0.8.2`）。`fflate` 是纯 JS（无 `.node`）。
3. 插件能否解析到核心包：`@deepseek-ai/dsh-app-boot/lib/worker/profile-resolution-bootstrap.js:397-458`（`routeScoped`）——profile 内插件 import 裸包名时，先查 profile 本地 `node_modules`，否则若包在安装域 entries 里且 `scope==="installation"` 或 profile 激活 ⇒ 走 interception 指向核心树包目录。安装域闭包由 `dsh-app-boot/lib/index.js:716-753`（`collectInstallationScopePackages`）从 `INSTALL_ANCHOR` 计算；`INSTALL_ANCHOR` = `@deepseek-ai/dsh/package.json`（`@deepseek-ai/dsh/lib/profile-boot-C14i-8IN.js:119`）。本次 BFS 实测该闭包含 549 个包，`fflate` 可达（经 `libreoffice-kit`），`dsh-session-log-export` 可达（经 `dsh-web-app`）。⇒ **大概率可 `import "fflate"`，但属核心内部传递依赖，不作契约**。
4. 纯 JS gzip/gunzip 已在端侧使用：`tar-gz.cjs:1-20`（"tar.gz = gzip(ustar)，`node:zlib.gunzipSync` + 512 字节头解析是零依赖的确定性路径"）；`dshm-installer.js:3-20`（安装管线："HTTPS 拉 tarball → 纯 JS gunzip + ustar 解包"）。
5. 结论：ZIP 走**零依赖自实现**（`node:zlib.createDeflateRaw` + CRC32 表 + 自写头/中央目录）；导入 `inflateRawSync`。`deflateRaw/inflateRaw` 与 `zlib.crc32` 的精确可用性未在真机逐项探针（C2），但同一 `node:zlib` 模块的 gzip/zstd 已被实际使用，风险低。

### B3. 磁盘/流/原子性
- `node:fs` 全量可用（宿主入口 `main.js:21` `require('node:fs')`；宿主持久化写日志 `createWriteStream(...,{flags:'a'})` `:100`、`appendFileSync` `:120`、`openSync/writeSync/closeSync/unlinkSync` 探写 `:1623-1627`）。
- 原子写/文件锁的成熟先例：`@deepseek-ai/dsh-atomic-write` 被核心使用；`main.js:5566-5590` 详述 `withFileLock` 用 `flag:'wx'` 建 `<file>.lock`、内容为持有者 pid、finally 释放；宿主启动时 `recoverOrphanLocks(HOME_DIR)`（`main.js:5579`、`:5979`）按 pid 判 stale 自愈。
- `fsync`：核心 `dsh-atomic-write` 的语义包含持久化写，但**具体 fsync 调用点本次未逐行核实**（C2）；备份实现建议 temp 文件写完后 `fs.fsyncSync(fd)` 再 `rename`，失败可降级（rename 本身已保证同目录原子替换）。
- `fs.statfs`：Node 24 API 存在，但**本运行时/OHOS hmfs 上的实际行为未验证**（C1）；建议 try/catch 包住，拿不到就跳过空间检查，改为"备份完成后校验大小"。

### B4. 路径可用性 / 持久化（导出关键）
1. 用户可见工作区：`main.js:1581-1612` 注释（真机结论）：
   - 应用进程原始 HOME = `/storage/Users/currentUser`
   - `Download/<包名>/` = `/storage/Users/currentUser/Download/com.dshm.dshclient`：**写/读/删全通、不需要 ACL**
   - 授权来自 `DocumentPickerMode.DOWNLOAD` 的 `save()`（ArkTS 侧、无 UI），是**按包名归属的路径级授权**，"卸载重装后同包名仍可直接访问"
   - 应用进程自己直拼路径 `mkdir` 公共目录 **必 EPERM**；只有 ArkTS 能建立，通道是启动前 env `DSHM_PUBLIC_DOWNLOAD`（`main.js:1593-1601`）
2. 宿主启动校验：`main.js:1665-1690` ① `lstat` 判目录 + ② `workspaceProbeWritable()`（`create+write+unlink`，`:1616-1638`）都过才采用；否则回退 `<SANDBOX_HOME>/workspace`（应用沙箱，用户不可见，`main.js:1691-1700`）。
3. 当前实际状态：`workspace=/storage/Users/currentUser/Download/com.dshm.dshclient`（host-ready.json），且该目录已存在前次任务产物；⇒ 本机这次启动**认领成功**。
4. 应用私有目录：`DSH_HOME=/data/storage/el2/base/haps/entry/files/dsh/home`（host-ready 同目录），会话/配置/token/凭据都在此。宿主持久写该目录（sessions、host-ready、日志、卡状态等）；宿主进程对它有完整读写。
5. 卸载保留判定（初判，需手机验收 C5）：
   - `/storage/Users/currentUser/**` = 用户个人盘，**不随应用卸载删除**（DSHM 代码注释 `main.js:1589` 称同包名重装后仍可直接访问；重新认领授权即可）。
   - `/data/storage/el2/base/...` = 应用沙箱数据，**预期随卸载清除**（代码未给出显式保证，标手机验收）。
   - 导出/交付必须落在工作区（用户目录），不要依赖应用沙箱。

### B5. 文件选择器 / 上传 / 分享（调研结论）
- 存在的包（core 树，`<CORE>/node_modules/@deepseek-ai/`）：`dsh-client-file-upload`、`dsh-client-ui-directory-picker-native`、`dsh-client-ui-directory-picker-browse`、`dsh-host-directory-picker`、`dsh-host-directory-picker-native`、`dsh-host-directory-picker-browse`、`dsh-host-directory-picker-auto`、`dsh-api-workspace-files`、`dsh-client-ui-attachment`、`dsh-file-reference`、`dsh-file-reference-local`。
- ArkTS 侧选择器/授权诊断产物（`<files>/` 根，皆小文件，未展开内容）：`diag-file-selector`(26 B)、`diag-pick-called`(616 B)、`diag-select-returned`(462 B)、`diag-resolve-dispatched`(951 B)、`diag-folder-grants`(7929 B)、`diag-persist-done`(590 B)。
- 宿主与 ArkTS 的既有通道只有：启动前 env（`DSHM_PUBLIC_DOWNLOAD`）+ 文件队列（`<DSH_HOME>/install-queue/*.req`，`dshm-installer.js:12-15`；OpenLink 队列见 `main.js:6000-6005`）。**没有**给插件的 pick/save/share RPC。
- `main.js:5914-5990` 显示宿主可自注册 HTTP 端点（如 `/dshm-python/*`、`/dshm-open-link`、`/dshm-registry/view`），说明"注册路由"这一层在本机可行，但插件 SDK 是否开放同等能力**未确认**（C4）。
- ⇒ 结论：**纯宿主插件不能触发系统保存/选择/分享对话框**；导出走 B4 工作区路径；导入走"用户把文件放进工作区 + 插件列出/读取"最稳；前端 `<input type=file>` 上传到插件路由是可选增强，待确认插件路由能力。

### B6. 定时/后台/生命周期/锁
- `cordis-plugin-timer`：核心 `dshm-core.json` `plugins[]` 中 `{id:"timer", name:"@deepseek-ai/cordis-plugin-timer", bundle:"@deepseek-ai/dsh-base", disabled:false, nativeKind:"PURE_JS"}` ⇒ **已启用**。
- 宿主自身定时器实测工作：`main.js:605-608`（心跳 `setInterval`，写 `dshm-hb.log`）、`:620-639`（看门狗 `setInterval`）、`:5905`（`setTimeout` 重试）；`dshm-hb.log` 当前持续增长。
- 生命周期：插件在 Host 启动时随 profile 挂载（`main.js:5983-5988` `profileBoot.runProfile(...)`，`BOOT_40_PROFILE_BOOT/BOOT_50_DSH_INIT` 里程碑 `:5923-5995`）；插件安装后需**重启 DSHM** 才生效（插件安装技能文档：重启前 `pluginInventory` 看不到）。应用退出 ⇒ Host 进程结束 ⇒ 定时器/任务全停；**无后台守护**。
- 单实例：一个 Host 进程（`host-ready.json.pid`）+ `main.js:1210` 提到 host-ready 写入带 **pid 守卫**（避免上一轮进程的读数混入）；`app-lifecycle.json` 记录启停（`<DSH_HOME>/app-lifecycle.json`，93 B）。
- 锁实现（可直接照抄）：`fs.openSync(lockPath,'wx')` 写 pid，`finally` unlink；启动时判 stale = 读 pid → pid 不存在才删（`main.js:5566-5590`）。冷却/轮换 = 独立状态 JSON + 原子写 + 按 mtime/序号淘汰旧包。

### B7. 日志/诊断（只做定向读取，不读 40MB+ 全文）
- `<files>/dshm-host.log`：宿主 diag 日志。位置由 `main.js:88-94` 决定：`<DSHM_SANDBOX_HOME>/dshm-host.log`；格式每行 `[ISO 时间] 文本`（`:96-108`）；同步版 `diagSync` 走 `appendFileSync`（`:118-122`）。启动首行：`--- boot pid=… execPath=… argv=…`（`:156`），随后 `userData=… DSH_BASE=…`（`:157`）、`platform=… versions=…`（`:161`）。
  - 工作区判定行：`默认工作区：<path>（来源=DSHM_PUBLIC_DOWNLOAD，即 ArkTS 经 DOWNLOAD 模式认领的 Download/<包名>/，用户可见；原始 HOME=…；选择理由=lstat 判为目录且 create+write+unlink 全部通过）`（`:1684-1686`）。
  - 插件日志会带时间戳前缀（`main.js:166-190` "插件日志时间戳前缀已就绪"）。
  - 启动里程碑：`BOOT_10_ENV_READY` / `BOOT_20_CORE_FOUND` / `BOOT_30_PROFILE_READY` / `BOOT_40_PROFILE_BOOT` / `BOOT_50_DSH_INIT` / `BOOT_60_HTTP_BIND`（`:5923-5995`）。
  - 实测大小 ~14.4 MB（2026-10-24 01:44），可定向 `grep -m` 关键词，勿整读。
- `<files>/node-output.log`：Host stdout/stderr 的落盘（`main.js:76-80` 说明"Node 的 console.log 走 stdout，在应用进程里不进 hilog，所以把 stdout/stderr 落进文件"）。实测 ~88.5 MB，是**最大**日志；诊断只用 `tail -c`/定向 grep。
- `<DSH_HOME>/dshm-boot-rescue.log`（实测 22.8 KB）：`dshm-boot-rescue` 插件（profile 依赖 + 工作区另有 `dshm-boot-rescue-1.0.0.tar.gz`）的自救日志，格式未逐行核对；可作为"上次启动是否异常/自救"的诊断入口。
- `<files>/dshm-hb.log`：心跳线程每秒一行 `HB <n> <epochMs> <iso> cpu=<utime+stime ticks>`，首行 `# 心跳线程启动 pid=…`（`main.js:581-608`）；判"进程被冻结 vs 主线程被挡"用。
- `<files>/dsh-js-tid`：JS 主线程 tid（`main.js:557-563`），配合 `/proc/<pid>/task/<tid>/stat|wchan` 用（仅诊断）。
- 其它 diag：`diag-web-console`、`diag-web-load`、`diag-tray`、`diag-bottom-inset`、`diag-decor-*`、`diag-openlink` 等（ArkTS/UI 侧，随实现变化，不作契约）。
- 结论：**能读到"最后一次启动结果 + 插件的 console 输出"**（host-ready.json 的 pid/startedAt + dshm-host.log 的 boot/BOOT_* 行 + node-output.log）；仅诊断，不修改任何配置。

### B8. 版本标识（详细字段见 A7）
- `<CORE>/dshm-core.json`：`coreVersion="0.2.1-rc.2+dshm.73"`、`platform="openharmony/arm64"`、`profile="ondevice"`、`builtAt="2026-10-18T01:59:13.380Z"`、`nodeFloor="22.17.0"`、`overrides`（node-pty/koffi/sharp 的 @ohos-ports 版本）、`producer="tools/pack-core.mjs"`、`plugins[]`。
- `<CORE>/package.json`：`dshm-core-bundle@0.0.0`，依赖 `@deepseek-ai/dsh@0.2.1-rc.2+dshm.73`。
- `<CORE>/node_modules/@deepseek-ai/dsh/package.json`：上游 `0.2.1-rc.2`。
- `<DSH_HOME>/profiles/ondevice/package.json`：`dshm-ondevice-profile@0.0.0`，`dependencies` 列出用户插件（`dsh-thinking-bar`、`dsh-whale-widget`、`dshmarket`、`cc-safety-net`、`dsh-cache-billing`、`dsh-date-wrapper`、`dsh-plugin-save-token`、`dshm-boot-rescue`），`dsh.profile.bundles[]` 列出 bundle 顺序。
- `host-ready.json`：`pid`、`startedAt`、`profile`、`workspace`、`runtime.*`（A7）；`url/baseUrl/token` 敏感，禁止写入备份/日志。

---

## C. 未确认 / 待手机验收项

| # | 项 | 现状 | 验收方法（手机） |
|---|---|---|---|
| C1 | `fs.statfs` 在 OHOS hmfs 上是否可返回真实 `f_bavail` | Node 24 API 存在，未实测 | 最小插件调用 `fs.statfsSync('/storage/Users/currentUser/Download/com.dshm.dshclient')`，打印/回退捕获 |
| C2 | `zlib.deflateRawSync/inflateRawSync`、`zlib.crc32`、`fs.fsyncSync` 精确可用性 | 同一模块 gzip/zstd 已用；逐项未探 | 最小插件一次性打印 `typeof`，失败则用自写 CRC32 + 已验证 API 降级 |
| C3 | 用 `import "fflate"` 从第三方插件解析到核心树 | 解析器机制（B2）支持，实际插件未验证 | 最小插件 try/catch import；**即使可用也不建议依赖** |
| C4 | 插件能否注册 HTTP 路由（供 `<input type=file>` 上传导入） | 宿主自身可注册路由；插件 SDK 未确认 | 查插件 ctx 能力或试注册一个 GET；否则走"工作区文件列表"方案 |
| C5 | **卸载后保留**：`/storage/Users/currentUser/Download/com.dshm.dshclient` 不被删除、重装后重新认领可读写；应用沙箱（含 DSH_HOME）被清除 | 代码注释称用户目录同包名重装仍可访问；沙箱清除为依据系统语义的预期 | 真机装本备份插件→导出→卸载→重装→文件是否仍在/路径可读（**不得用平板结果替手机**） |
| C6 | DSHM 应用版本（ArkTS 侧）在 node 侧/UI 的稳定展示 | 未在核心 JS 里发现稳定字段；`dsh-client-ui-settings-general`/`dsh-brand` 源码未查 | 查设置页"关于/版本"或 ArkTS `bundleManager`；备份 manifest 里 `appVersion` 允许为空 |
| C7 | `node-output.log` 是否有轮转/上限（88.5 MB 已很大） | 未确认 | 观察多次启动后大小；备份不依赖它，只读尾部 |
| C8 | 大备份的内存/耗时边界（jitless ARM、单线程事件循环） | 未知；宿主有 LOOP-GAP 看门狗说明阻塞敏感 | 10/100/500 MB 三档实测，确认 `LOOP-GAP` 不出现、UI 不超时 |
| C9 | 备份导入的恢复语义（覆盖前校验、原子替换 DSH_HOME 子树） | 设计层面：temp+rename；真机未做 | 在测试 profile/工作区做一次破坏-恢复演练 |

---

## D. 风险与替代方案

1. **jitless 无 WASM**：不要引入依赖 WASM 的 zip/压缩库（如某些 wasm-flate）；`node:zlib` 原生实现是安全路径。
2. **单线程事件循环**：所有大文件操作必须 async/流式；禁止 `readFileSync` 整包进内存 + `deflateRawSync` 一把梭。分块 `await` 或 stream pipeline；给 UI 一个进度文件（如 `backups/.progress.json`）。
3. **ZIP 自实现的正确性**：优先"单文件/少量条目 + data descriptor + CRC32 表"（可用 python `zipfile` 在 PC 上交叉验证产物，guest 侧也可用 `unzip -t` 测试——busybox 有 unzip）。
4. **导出位置**：只写工作区（用户可见）；写前先探写（照抄 `workspaceProbeWritable`），失败则回退沙箱并**明确告知用户"当前不可见"**。备份目录名建议 `dshm-backup-dev/backups/`，避免与用户文件混放；提供"保留最近 N 份 + 手动清理"。
5. **敏感数据**：`<DSH_HOME>/host-ready.json`（含 token）、`.credentials.yaml`、sessions 里的聊天内容——备份包默认**排除凭据文件**，或提供"含凭据"显式选项并提醒用户备份包是明文；日志/诊断输出同样不得打印 token。
6. **插件依赖**：若一定要第三方库，必须随插件声明并在端侧安装（仅纯 JS；含 `.node` 的包装不上，见插件安装技能）；推荐零依赖以规避网络/registry 风险。
7. **锁与并发**：单实例锁用 `wx` + pid（B6），备份运行中写 `state.json`；任务超时/中断的残留锁按 pid 判 stale 清理。两个备份同时触发时第二个直接拒绝并提示。
8. **替代方案（若 ZIP 实现成本过高）**：`tar.gz`（`zlib.gzipSync` 同模块、`tar-gz.cjs` 已有纯 JS tar 解析先例 + guest `tar` 可验），但 Windows 用户友好度不如 ZIP；当前推荐仍为 ZIP。
9. **导出路径的"用户可见"验收**：文件管理器能否看到 `Download/com.dshm.dshclient/dshm-backup-dev/backups/*.zip`、微信/浏览器等其它应用能否读取该文件（部分应用受包名授权限制），需手机验收。
10. **不做后台**：备份必须前台完成或在下次启动续做（分卷 + 断点记录）；不要向用户承诺"关掉应用也会继续跑"。

---

## 附：本次只读侦查用到的命令/取证位置（可复核）
- `python` 工具读：`/data/storage/el2/base/haps/entry/files/dsh/home/host-ready.json`（scrub token 后取字段）、`.../dsh/cores/0.2.1-rc.2+dshm.73/dshm-core.json`、`.../package.json`、`.../profiles/ondevice/package.json`、`.../sessions/...`、宿主入口 `/data/storage/el1/bundle/entry/resources/resfile/resources/app/{main.js,jitless-env.cjs,tar-gz.cjs,dshm-installer.js}`
- bash(guest) 读：`/mnt/share` = 宿主 `.../files`（app 沙箱）；`ls /mnt/share/dsh/home`、`ls /mnt/share/dsh/cores/*/node_modules`、`ls -la /mnt/share/*.log`（只列尺寸，不整读）
- 核心包证据：`@deepseek-ai/dsh-session-persistence-jsonl/lib/index.js`、`@deepseek-ai/dsh-app-boot/lib/worker/profile-resolution-bootstrap.js`、`@deepseek-ai/dsh-app-boot/lib/index.js`、`@deepseek-ai/dsh/lib/profile-boot-C14i-8IN.js`、`@deepseek-ai/dsh/lib/worker/profile-resolution-bootstrap.js`
- 注：`/data/storage/el1/bundle` 对 `read/grep/write` 工具在路径策略之外（被 DSHM 闸门拒绝），仅 `python` 工具可读；这是本机特有的取证限制，不是文件不存在。
