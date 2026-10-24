# Lead 侦查笔记（工作稿，后续并入 FEASIBILITY.md）

时间：2026-10-24（Asia/Shanghai）。环境：HarmonyOS 7.0.0.109SP6 平板，DSHM com.dshm.dshclient。

## 0. 工作区真实性（安全备注）
- 当前会话 host 工作区 = `/storage/Users/currentUser/Download/com.dshm.dshclient`（workspace.json 中 workspace id c6001194-…，仅本会话；createdAt 2026-10-23T17:27:48Z）。
- 该目录并非严格空目录：含前次任务遗留 `.recon/`（boot-rescue 侦查资料）、`dshm-boot-rescue/`（旧插件拷贝）、`dshm-boot-rescue-1.0.0.tar.gz`、`.virtfs_metadata/`。
- 处理：本任务所有写入只放在新建的 `dshm-backup-dev/` 子目录内，不触碰上述遗留文件。此差异需在 FEASIBILITY.md 与最终汇报中说明。

## 1. 版本维度（分别记录，不合并）
| 维度 | 值 | 来源 |
|---|---|---|
| DSHM 应用版本 | versionName 1.1.4 / versionCode 1010004 / targetAPI 260000026 | `/data/storage/el1/bundle/entry.hap!module.json`（Python zipfile 读取） |
| 核心包版本（dsh npm 版本） | 0.2.1-rc.2 | `cores/0.2.1-rc.2+dshm.73/node_modules/@deepseek-ai/dsh/package.json` |
| 核心构建/补丁版本 | 0.2.1-rc.2+dshm.73，builtAt 2026-10-18T01:59:13.380Z，nodeFloor 22.17.0 | `cores/0.2.1-rc.2+dshm.73/dshm-core.json` |
| 用户可见核心版本（设置页） | 0.2.1-rc.2（`general.currentVersion` 文案处字面量） | `node_modules/@deepseek-ai/dsh-client-ui-settings-general/lib/client.js:663` |
| 宿主运行时 | node v24.2.0，platform openharmony/arm64，jitless=true，zstd=true | `$DSH_HOME/host-ready.json` runtime |
| 提示词历史基线 core 0.2.1-alpha.1 | 当前环境未发现该版本标识；按用户规则以实测为准，标注“提示词基线已过期” | 以上证据 |

## 2. 环境与路径
- DSH_HOME（host 真实路径）= `/data/storage/el2/base/haps/entry/files/dsh/home`；profile = `$DSH_HOME/profiles/ondevice`。
- guest bash 的 `/mnt/share` = host 的 `/data/storage/el2/base/haps/entry/files`（9p）。guest bash 的 cwd `/storage/...` 是 guest 本地空目录，**不是** host 工作区；写项目文件必须用 write/python 工具（host 侧），不能靠 guest bash 的 cwd。
- `host-ready.json` workspace 字段 = `/storage/Users/currentUser/Download/com.dshm.dshclient`；含 token，禁止输出。
- 用户可见持久区：`/storage/Users/currentUser/Download/<包名>/`（host node 可读写；卸载是否保留留手机验收）。

## 3. 数据模型（已确认）
- 会话：`$DSH_HOME/sessions/--<cwd 编码>--/<session-id 编码>/session.v4.jsonl.zstd` + `session.lock`。编码函数：`encodeSegment`/`projectKey`（dsh-session-persistence-jsonl/lib/index.js:892-953；`projectKey` 把 `/ \ :` 变 `-`，其他不安全字符 `~XXXX`）。
- 会话格式：v4（`SESSION_FORMAT_VERSION`，dsh-session-format-catalog 校验）；写路径用 node:zlib 的 zstdCompress/zstdDecompress 多帧（dsh-session-persistence-jsonl/lib/index.js:50,1408-1436）。
- 会话写锁：POSIX `flock(2)` on `session.lock`（dsh-session-persistence-jsonl/lib/index.js:655-770）；读者不碰锁。备份一致性方案需自行复制+校验（zstd 帧/JSONL 完整性 + stat 前后比对）。
- 工作区注册表：`$DSH_HOME/storages/workspace.json`，unit/version=2，`global{initialized,workspaceIds,archivedSessionIds,pinnedSessionIds}`，`tables.workspaces[<id>]{path,title,sessionIds,createdAt,updatedAt}`；domain spec 见 dsh-workspace/lib/index.js:255-268（version 2）。
- 工作区服务 `workspaceRegistry`（Cordis Service）负责持久化与 session membership；直接改文件需冷重启后由 boot 重建（待定方案，需写恢复设计）。
- 工作区目录本身 = `/storage/Users/currentUser/Download/<dir>`；会话 cwd 即工作区路径，关联靠 workspace.json 记录 + 会话 header。

## 4. 插件机制（前次任务已验证，同 core 版本）
- 纯宿主插件：package.json（type=module, main=lib/index.js, exports, dsh.bundle.patch=./cordis.patch.yml, files）；cordis.patch.yml `- insert: - id: <包名> name: <包名>`；依赖为空、只用 node: 内置；禁止 child_process/Worker/WASM；禁止静态 inject（用 ctx.get）。
- UI：`ctx.get("webServer")` 后 `webServer.register({kind:"prefix",path,handler})` + `webServer.tapIndex(fn)` 注入 `/<prefix>/client.js`（单文件 IIFE ES5）；前端同源 fetch 调宿主路由。实例：dshm-boot-rescue/lib/index.js:1691-1849。
- 插件启停持久化在 `$DSH_HOME/profiles/ondevice/.dshm-plugin-rows.yml`；cordis.patch.yml 每次启动重拼；改插件后需冷重启（划掉后台重开）。
- 安装：设置→插件→安装插件输入 `github:owner/repo#branch&path:/subdir`；或 pnpm 假壳（install-queue）。

## 5. 关键运行时能力
- node 内置：zlib（deflateRaw/inflateRaw + zstd*）、crypto（sha256）、fs/promises、stream、path、url、timers 均可用（核心包已实际使用 zlib.zstdCompress/zstdDecompress，见上）。
- 无 node CLI（host-ready exec 探针：python3.12/git/rg/ash/bash 均 fail 或缺，toybox ok）；因此 `node --check` 在平板上不可执行 → 需用替代静态检查（Python 词法检查）并在 TEST_REPORT 如实标注。
- 零依赖 ZIP 可自实现（zlib.deflateRaw + crypto + CRC32），导入解压用 zlib.inflateRaw；fflate/pako 只在核心树内，不作为第三方插件依赖。
- tapIndex 注入客户端运行在 ArkWeb（Chrome 124+），可用 fetch/Promise；保持 ES5 风格。
- 定时器：setTimeout 可用（boot-rescue 已用）；周期基线备份可用 setTimeout 链 + 冷却文件；单实例锁可用 fs.open(wx)。
- 插件随 Host 进程生命周期，进程停则插件停；Host 级不可达时插件无法运行（基线备份的必要性依据）。

## 6. 恢复设计待确认点（继续取证）
- workspace.json 由哪个 storage backend 落盘（文件名/原子写/是否另有 SQLite）→ 直接改文件 + 冷重启 vs 调 workspaceRegistry 服务 API。
- v4 会话 header 字段与事件类型（用于 manifest 校验与预览，不解析正文内容）。
- 目标“全新实例”判定标准（install 后 registry 已有默认 workspace/session 的情况）。
- 备份导出目录与卸载后保留（默认 `Download/<包名>/` 下专用子目录，排除在工作区文件备份之外）。
- 异常抢备真实信号：loader entries fiber FAILED（启动后检查）+ 客户端失败页 DOM（POST 触发）；需再核实事件/字段。
