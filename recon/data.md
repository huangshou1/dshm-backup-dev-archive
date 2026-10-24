# DSHM 数据层侦查报告（备份/迁移用）

- 侦查者：recon-data；只读，未改动任何 DSHM 数据。
- 目标环境：HarmonyOS 平板 DSHM（com.dshm.dshclient），core `0.2.1-rc.2+dshm.73`。
- 路径约定：
  - `CORE=/data/storage/el2/base/haps/entry/files/dsh/cores/0.2.1-rc.2+dshm.73`（core 树，权威源码）
  - `HOME=/data/storage/el2/base/haps/entry/files/dsh/home`（DSH_HOME；该 files 目录经 `ls -la /data/storage/el2/base/haps/entry/files` 确认是 `-> /mnt/share` 的软链；bash guest 用 `/mnt/share/dsh/...` 只读访问）
  - 下文 `CORE/node_modules/@deepseek-ai/<pkg>/lib/index.js` 统一简写为 `<pkg>/lib/index.js`。
- 会话文件的读取纪律：仅用 Python+libzstd 打印 JSON 键名/类型/行数；未打印任何消息正文或标题值。

---

## A. 结论摘要（可直接指导备份/恢复设计）

### A1. 必须备份（权威数据）
1. **会话日志树** `HOME/sessions/--<projectKey>--/<sessionDir>/session.v4.jsonl.zstd`：会话正文、标题事件、时间戳、token 用量等都在此。恢复后无需任何额外索引即可出现在会话列表（`session-persistence-jsonl root = dshHomePath('sessions')`，`dsh-base/cordis.patch.yml:138-141`）。
2. **工作区清单** `HOME/storages/workspace.json`：工作区路径/标题/会话归属/顺序/归档/置顶。**可选但强烈建议**：不备份也能由启动引导（bootstrap）从会话 header 重建工作区，但会丢失用户改过的标题、工作区顺序、归档/置顶状态（`dsh-workspace/lib/index.js:758-832`）。
3. **工作区目录本身**（用户可见目录，如 `/storage/Users/currentUser/Download/<dir>`）：会话工具读写的产出文件，不在 DSH_HOME 内。
4. **附件**（可选，保历史完整）：`HOME/attachments/v1/files` + `file-objects`（消息里的图片/文件以内容寻址引用指向这里；`dsh-base/cordis.patch.yml:143-145`）。

### A2. 可以重建/排除
- `HOME/storages/session_projcache/**`：仅投影缓存（write-behind checkpoint，`dsh-base/cordis.patch.yml:192-200`），删掉会自动重建。
- 会话搜索索引：本机 SQLite 是 `:memory:` 且 `openAt: never`（`dsh-base/cordis.patch.yml:157-161`）、session-search 覆盖层亦为 `:memory:` + `first-search`（`dsh-experimental-session-search/cordis.patch.yml`），**无持久索引需迁移**。
- `session.lock`：0 字节 flock 锁文件，恢复时无需复制（写入打开时自动创建；`jsonl/lib/index.js:705-711`）。
- 第三方插件状态/缓存：`.dshw-*`、`card-state.json`、`card-usage.jsonl`、`whale-*`、`dsh-market/`、`install-queue/`、`channel-pack/`、`open-link/`、`speech-to-text/`、`profiles/.../node_modules`、`.dsh-market`、`.plugin-manager`。
- 运行时/凭据：`host-ready.json`（含 token）、`.credentials.yaml`、`openai-gateway/api-key`、`.anonymous-user-id`、`app-lifecycle.json`、`dshm-boot-rescue.log`（详见 E 节）。

### A3. 一致性快照（正在写入的会话能否直接复制？）
- **能复制，但必须做前后比对**：日志是 **append-only + 多 zstd 帧**，每帧独立可解压（`jsonl/lib/index.js:1321-1325, 1395-1401, 3302-3316`）。core 自己读文件就用“stat → readFile → stat，两次 identity 相同才认”的方式（`jsonl/lib/index.js:1675-1706`），备份可照抄。
- 复制后 stat 身份 `dev:ino:size:mtimeNs:ctimeNs`：前后一致 → 快照一致；不一致 → 重试一次；仍不一致 → 退回“复制前 size 的字节前缀”（core 的同一策略，`jsonl/lib/index.js:1700-1703`），并标记该会话“备份时仍在写入”。
- 复制到的文件若末尾是**不完整帧**（torn tail）：core 读取时能恢复前缀并在下次写入前截断修复（`jsonl/lib/index.js:3014-3080, 2967-2976`），因此“帧边界不完美”的副本通常仍可打开；**建议**在备份端自行把末尾裁剪到 `scanZstdFrames` 报告的最后完整帧 end 之后再做校验（只丢最后一批未完成事件）。
- 会话写入是 **append + fsync**，无整文件重写（`jsonl/lib/index.js:3328-3369`）；一次写入失败会 truncate 回旧 size（同上），所以“复制中的文件”只会表现为多/少一个末尾帧。
- 不要尝试靠 `session.lock` 内容判断活跃：它 0 字节、flock 语义不落在文件内容里（`jsonl/lib/index.js:651-770`）。活跃判定以 stat 变化为准。
- **不要边运行边改 `workspace.json`**：JSON 存储是“内存权威 + 整文件原子替换、单写者 last-write-wins”（`storage-json/lib/index.js:6-45`），外部改动会被进程内下一次写覆盖。恢复 workspace.json 必须在 Host 停止时进行。

### A4. 恢复路径/字段（关键）
- 会话文件必须落在 **按 header.cwd 计算出的 projectKey 目录** 下：`logPath = sessions/projectKey(cwd)/encodeSegment(id)/session.v4.jsonl.zstd`（`jsonl/lib/index.js:941-978`；projectKey/encodeSegment 规则见 `jsonl/lib/index.js:881-933`）。目录名与会话 id 不匹配的副本不会被当作该会话（路径由 id+cwd 计算）。
- **cwd 是绝对路径**：`isHeaderLine` 要求 `cwd` 为 absolute string（`jsonl/lib/index.js:878-880`）。目标机用户目录不同 → 必须重写 header 的 `cwd`（第一帧明文只有 header 一行，重压缩第一帧即可，后续帧不受影响）并同步重命名 projectKey 目录；同时改 workspace.json 的 `path`。否则该会话会被工作区成员校验过滤（`dsh-workspace/lib/index.js:875-895`）。
- 工作区唯一性/归属校验：同一 `path` 不能属于两个 workspace；同一 session 不能被两个 workspace 记账；`initialized=true` 时 order 数必须等于表数（`dsh-workspace/lib/index.js:833-857`）。恢复前目标 registry 必须为空/全新，否则整份覆盖会破坏目标已有数据。
- 重复 session id：持久层 `create()` 检测到已存在会抛 `SessionAlreadyExistsError`（`jsonl/lib/index.js:2499-2513`）；恢复策略只能是“跳过已存在 / 报冲突”，不能覆盖。
- 路径已存在：`create` 对同一 canonical path 直接复用已有 workspace 且不改标题（`dsh-workspace/lib/index.js:397-411`）；但采用“复制 workspace.json”恢复时应要求目标清空 registry，避免合并语义未定义。
- 全新实例判定：`workspace.json` 不存在，或 `global.initialized=false`，或 `workspaceIds=[]` 且 sessions 目录为空；随后 `initializeDefault` 只在“registry 空 + 无归档 + 无 live session + 无任何 stored header”时自动建默认工作区（`dsh-workspace/lib/index.js:425-438`）。

### A5. 版本维度（区分，不合并）
| 维度 | 值/来源 | 证据 |
|---|---|---|
| 核心版本（权威） | `0.2.1-rc.2+dshm.73`，`platform=openharmony/arm64`，`profile=ondevice`，`builtAt=2026-10-18T01:59:13.380Z`，`nodeFloor=22.17.0` | `CORE/dshm-core.json`（coreVersion/platform/profile/builtAt/nodeFloor/producer/plugins[]） |
| core 树目录名 | `0.2.1-rc.2+dshm.73` | `CORE` 路径本身 |
| core bundle package.json | `dshm-core-bundle@0.0.0`，依赖 `@deepseek-ai/dsh: 0.2.1-rc.2+dshm.73` | `CORE/package.json` |
| 会话格式版本 | v4（文件名 `session.v4.jsonl.zstd`） | `dsh-session-format-catalog/lib/index.js:41`；`dsh-session-format/lib/index.js:472-479`；`jsonl/lib/index.js:786-791` |
| DSHM profile 版本 | `dshm-ondevice-profile@0.0.0`（不含 core 版本） | `HOME/profiles/ondevice/package.json` |
| Host 运行时 | `host-ready.json.runtime.nodeVersion/platform/zstd` 等；**无 coreVersion 字段** | `HOME/host-ready.json`（键结构实测） |
| UI 设置页核心版本来源 | **未确认**：JS 层 grep 无 `coreVersion` 字符串，疑由原生 ArkUI 读 `dshm-core.json` 显示 | 全 node_modules `grep coreVersion` 0 命中 |

---

## B. 证据详情

### B1. DSH_HOME 目录结构（实测）
`HOME` 实测顶层（Python `os.scandir`）：`attachments/`、`channel-pack/`、`dsh-market/`、`install-queue/`、`open-link/`、`openai-gateway/`、`our-free-model/`、`profiles/`、`sessions/`、`skills/`、`speech-to-text/`、`storages/`、`whale-audio/`、`whale-bubble-imgs/`、`whale-roles/`，以及根文件 `.anonymous-user-id`(37B)、`.credentials.yaml`(478B)、`.dshw-size.json`、`.dshw-turn.json`、`.dshw-usage.json`、`app-lifecycle.json`、`card-state.json`、`card-usage.jsonl`、`dshm-boot-rescue.log`、`host-ready.json`。
- `sessions/` 下现有 project 目录：`--storage-Users-currentUser-Download-brickpack--`、`--…-com.dshm.dshclient--`、`--…-dsh--`、`--…-dshm-testing--`（每目录多个 session 子目录）。
- `storages/` 只有 `workspace.json`(2665B) 与 `session_projcache/sessions/`。
- 关系：profile = `HOME/profiles/ondevice`（package.json 定义 bundles → 决定装载哪些工具/插件）；sessions 根 = `HOME/sessions`；storages 根 = `HOME/storages`（`dsh-base/cordis.patch.yml:138-141,182-190`）。

### B2. 会话文件格式（v4 / zstd 容器 / 写入语义）
- 格式版本 = 4：`dsh-session-format-catalog/lib/index.js:41`（`currentVersion: 4`）；文件名规则 `session.v${n}.jsonl`（v0 为 `session.jsonl`），压缩后缀 `.zstd`：`dsh-session-format/lib/index.js:472-479`、`jsonl/lib/index.js:786-791,976-978`。
- **容器 = 多个独立 zstd 帧拼接**：每帧独立可解压且带 checksum；第一帧明文**恰好一行 header**；每个 durable append 批次 = 一帧完整 JSONL 行（`jsonl/lib/index.js:1321-1325,1395-1401,1931-1933,3302-3316`）。`scanZstdFrames` 定位完整帧，末尾不完整帧返回 `tornStart` 用于修复（`jsonl/lib/index.js:1339-1402`）。
- header 行（第一帧）必填键：`type`(="session")、`version`(4)、`id`、`createdAt`(epoch ms int)、`isSeeded`、`delegationDepth`；可选：`cwd`(绝对路径)、`parentSession`、`origin`(subagent)、`agentPreset`（`jsonl/lib/index.js:816-830,847-880`）。
- 事件行（后续帧）：每行一个 JSON，envelope 键 `seq,time,type,data`（源码 `jsonl/lib/index.js:979-995`；实测见下）。
- **实测验证**（只打印键名/类型，不打印值）：用 Python ctypes 加载 `/system/lib64/libzstd.z.so` 解码 `HOME/sessions/--…-com.dshm.dshclient--/<sessionDir>/session.v4.jsonl.zstd`（344 B → 2 帧：帧0 明文 234B 且恰好一行；帧1 明文 269B，3 行事件）。header 键 = `['agentPreset','createdAt','cwd','delegationDepth','id','isSeeded','type','version']`，`type=session`,`version=4`；事件类型 = `permission/preset`、`sandbox/mode`、`approval/policy`，事件键 = `['data','seq','time','type']`。同目录 `session.lock` 实测 0 字节（`ls -la /mnt/share/dsh/home/sessions/--…--/<sessionDir>/`）。
- 写入/原子性：会话**懒物化**——create 时仅内存登记；首次 append/flush 才落盘：建 root/project/session 目录 → 写 `<log>.<rand>.tmp`（`wx`,`0600`）→ fsync → 独占发布 → 目录 fsync → 删 tmp（`jsonl/lib/index.js:2431-2437,3229-3301,3240-3270`）。追加：`open(path,"a")` → stat 旧 size → 写一整帧 → fsync；失败则 truncate 回旧 size 再抛（`jsonl/lib/index.js:3328-3369`）。
- **DSHM 专有补丁**：鸿蒙沙箱禁止 `link(2)`（EACCES），`dshmPublishExclusive` 改为“`access(to)` 存在性检查 + `rename(from,to)`”；rename 同盘原子，已存在则人为抛 EEXIST（`jsonl/lib/index.js:5-34`）。注意这不是真正 no-clobber 原子操作，依赖单写者 + `session.lock`。
- 读一致性：`readStableSnapshot` 用 `dev:ino:size:mtimeNs:ctimeNs` 比对，重试一次，仍变则返回“第一次读的 size 前缀”（`jsonl/lib/index.js:1666-1706`）。
- 崩溃恢复：读到最后不完整帧时，用 `ZSTD_e_flush` 解出可读前缀，记录 `tornTruncateTo`；下次写入前 `truncate` 到该偏移并 fsync；已解出的尾部事件重放为一批（`jsonl/lib/index.js:3014-3080,256-272,2967-2976,3370-3380`）。
- 锁语义：`session.lock` 是 **POSIX `flock(2)` 非阻塞独占锁**（`node-addon-system/flock.tryLockExclusive`），写句柄生命周期内一直持有；竞争 → `SessionAlreadyOwnedError`；**没有租约/续租/TTL**，注释明确“deliberately no expiry”；进程死亡由内核释放；读句柄不碰锁；锁文件永不删除（保留稳定 inode），获取后校验 inode 一致否则重试（最多 3 次）（`jsonl/lib/index.js:651-770,705-751`）。
- 活跃写入节奏：live 事件先缓冲，`setTimeout(..., 200)` 批量落盘（`jsonl/lib/index.js:130-150` 附近 `enqueueLive`），因此“正在对话时复制”最多丢最后 ≤200ms 的已发布事件 + 未 flush 的当前步。

### B3. workspace.json（结构 / 读写 / 引导）
- 位置与后端：storage-json root = `HOME/storages`（`dsh-base/cordis.patch.yml:182-190`），单文件布局 `<root>/<name>.json`（`storage-json/lib/index.js:159-176`）→ `HOME/storages/workspace.json`；workspace 插件挂在 web 层（`dsh-web-app/cordis.patch.yml:91-92`）。
- 文件形状（序列化代码）：`{ unit: { name, version }, global, tables }`，pretty JSON（`storage-json/lib/index.js:66-83`）。workspace domain：`name="workspace", version=2`，`global` schema = `initialized:boolean, defaultWorkspaceId?:string, workspaceIds:string[], archivedSessionIds:string[]=[], pinnedSessionIds:string[]=[], pendingMutation?:{operation:"create"|"delete", workspaceId}`；表 `workspaces`（`dsh-workspace/lib/index.js:240-268`）。
- 表记录字段：`path`(realpath 规范化绝对路径)、`title`、`sessionIds`(有序=显示顺序)、`createdAt`/`updatedAt`(ISO 字符串)（`dsh-workspace/lib/index.js:204-215`）。
- 原子写：同目录临时文件 `.<uuid>.tmp` → fsync → `rename` 覆盖目标 → 父目录 fsync；单写者、last-write-wins（`storage-json/lib/index.js:6-45`）；domain 层每写必先落盘成功再改内存（`storage-domain/lib/index.js:96+` 注释）。
- 变更流程（2 阶段可恢复）：create 先写 global 的 `pendingMutation=create` → 写表记录 → 再写 global（`initialized=true`、`workspaceIds` 头部插入、首用写 `defaultWorkspaceId`）；中途崩溃由启动 `recoverPendingMutation` 删除半成品记录（`dsh-workspace/lib/index.js:641-704,747-757`）；delete 类似（`705-741`）。
- 启动引导 bootstrap（**迁移关键**）：`initialized=false` 时，读取所有 stored session header → 按 `realpath(header.cwd)` 分组 → 每组若有多余未记账会话则自动新建 workspace（title=目录 basename，createdAt=组内最新 header createdAt，顺序按最新时间降序）→ 最后置 `initialized=true`（`dsh-workspace/lib/index.js:379-396,758-832`）。
- 成员校验：`indexHeader` 要求 header.cwd 存在且是目录，否则记录 missing/invalid 并从 workspace.sessionIds 里过滤（`dsh-workspace/lib/index.js:875-895,905-915`）；attach 时要求 `realpath(header.cwd) === workspace.path` 且目录存在（`111-128`）。
- 新建工作区：`realpathNormalize(path)`（必须已存在目录）、同 canonical path 复用已有实体、新记录 id=randomUUID、prepend 到 workspaceIds（`397-411,637-655`）。

### B4. 会话元数据分布
- 会话文件内（随日志迁移）：`id`、`createdAt`、`cwd`、`parentSession`、`origin`、`isSeeded`、`delegationDepth`、`agentPreset`（header，B2）；标题 = **日志内 `session/title` 事件**，投影取最新值；无标题时由首条用户消息派生 fallback（不落盘）（`dsh-session-title/lib/index.js:170-188`，`89-99`）。
- token 用量：助手消息事件的 `data.usage`（格式校验代码直接断言 `event.data.usage`，`jsonl/lib/index.js:1886`）；会话级累计在 `card-usage.jsonl`/`.dshw-usage.json` 仅为第三方组件统计，非权威。
- 外部/展示态（非必需）：`card-state.json`（sessionId/title/state/turn/step、balance、usage 汇总）、`.dshw-usage.json`（date/lastBalance/todayUsage/history/accounting/events）、`.dshw-turn.json`（seq/updatedAt）、`.dshw-size.json`（UI 尺寸/音量等）——实测键结构，均由 whale/卡片插件写。
- **模型字段：未确认**。header 里只有 `agentPreset`；单条消息/事件是否记录实际模型 id 未在本轮验证（未打印事件值），待手机验收。

### B5. 其它索引 / 缓存 / 存储
- `storages/session_projcache/sessions/`：per-record 布局（每会话一个版本戳 checkpoint 文档），writeEveryEvents=200 / writeIntervalMs=5000，纯投影缓存（`dsh-base/cordis.patch.yml:192-200`；`storage-json/lib/index.js:55-63` per-record 说明）。排除、可重建。
- session search：无持久索引（见 A2 证据）。
- 无 `deliverables/`、`schedules/`、`goals/` 目录出现在 HOME 实测清单中 → 未发现独立存储；schedules 服务注释称“owns the task store”（`dsh-web-app/cordis.patch.yml:135-138`），具体存储位置未确认；goals 相关包存在（`dsh-tool-goal`）但本轮未定位持久化位置，待验收。
- `our-free-model/`（availability/catalog/feed/session-map/settings/stats）+ `channel-pack/`、`dsh-market/`、`install-queue/`、`open-link/`、`speech-to-text/`、`whale-*-` 均为插件/运行态数据，非会话恢复必需。

### B6. 工作区产出文件与路径
- 绑定方式：会话 header 的绝对 `cwd`（= workspace 记录的 `path`，realpath 规范化后必须相等）。工作区文件属于用户目录（本机 `/storage/Users/currentUser/Download/<dir>`），不在 DSH_HOME。
- 工具调用记录里的文件路径：由 fs 工具在 cwd 下解析；本轮未验证记录值是绝对还是相对（未打印事件值）→ 见 D。恢复影响面判断：工作区内的产出文件靠“复制用户目录 + 保持/重写 cwd”即可；日志内的路径字符串最多影响展示，core 不依赖其解析（未验证）。
- 附件：`HOME/attachments/v1/{files,file-objects,tmp}`（实测目录）；消息保留内容寻址引用，图片/文件字节在日志之外（`dsh-base/cordis.patch.yml:143-145`）。`tmp/` 排除。
- deliverables：`dsh-tool-present`/`dsh-client-ui-deliverables` 的产物是工作区文件；HOME 无独立 deliverable 存储（实测）。

### B7. 全新实例 / 冲突判定（实测 + 源码）
- 全新：`HOME/storages/workspace.json` 不存在 或 `global.initialized=false`；`workspaceIds` 为空；`HOME/sessions/` 无对应 project 目录/会话。此时恢复会话 + 不恢复 workspace.json → 启动 bootstrap 自动分组建工作区（B3）。
- 目标已有数据时：**禁止整份覆盖 workspace.json**（会与目标现有 registry 冲突/覆盖）。会话 id 冲突 → `SessionAlreadyExistsError`。路径冲突 → validation 拒绝同 path 两记录。建议本工具只在全新实例上执行“整机恢复”，否则走“新建 workspace + 复制会话 + 由 bootstrap 或 registry API 记账”的合并模式（合并模式未验证）。
- `initializeDefault` 自动建默认工作区只在 registry 与历史全空时发生（`dsh-workspace/lib/index.js:425-438`），因此“先恢复 sessions、再启动”的时序要保证 workspace 目录也已存在。

### B8. 版本标识证据补充
- `CORE/dshm-core.json`（64893B）字段：`coreVersion/platform/profile/builtAt/nodeFloor/overrides/producer/plugins[]`；plugins 每项含 `id/name/bundle/disabled/nativeKind/nativeVia`（例如 `session-persistence-jsonl` 行）。这是原生侧“当前 core 版本”的权威来源。
- `HOME/host-ready.json` 键：`url/baseUrl/token/port/profile/pid/startedAt/workspace/runtime`；`runtime` 含 `nodeVersion/platform/jitless/zstd/listenAddress/natives[]/python{...}/exec{...}/terminal{...}`。含 token，敏感；且**无 coreVersion**。
- `STORAGE`/会话文件名可交叉验证格式版本（`session.v4.*`）。
- UI 设置页显示来源未确认（无 JS coreVersion 字符串）。

---

## C. 恢复字段 / 流程建议（设计可直接采用）

1. **前置校验（备份端与恢复端都做）**：记录 coreVersion、会话格式 v4、profile 名、设备平台；恢复端若不满足则拒绝（格式 v4 以外的 header 会被 `SessionFormatUnsupportedError` 拒绝，`jsonl/lib/index.js:997-1007`）。
2. **备份**（Host 可运行，逐会话快照）：
   - 枚举 `HOME/sessions/*/*/session.v4.jsonl.zstd`；
   - 每个文件：stat → 读取字节 → stat，identity 相同才接受；不同则重试 1 次；仍不同则接受“首次 size 前缀”并打 `incomplete/active` 标记；
   - 校验 zstd 帧：若末尾不完整，记录 `torn=true`，可选裁剪到最后完整帧 end；
   - 记录 sha256 + size + mtime + 来源 header 的 `id/cwd/createdAt`（只写元数据，不落聊天正文）；
   - `workspace.json`、`attachments`（排除 tmp）同样 stat 校验后复制；
   - 排除清单见 E。
3. **恢复**（要求 Host 停止；目标为全新实例）：
   - 先把“工作区目录”恢复到目标机；若目标绝对路径与源不同：重写会话 header `cwd`（只解/重压第一帧）+ 按新 cwd 计算 projectKey 重命名目录 + 同步改 `workspace.json` 的 `path`；否则路径原样即可。
   - 复制 `sessions/` 树（`session.lock` 可省略/建空文件）；确保 `HOME/sessions` 权限目录存在。
   - 恢复 `workspace.json`（若采用）：`unit.version=2`、`global.initialized=true`、`workspaceIds` 保序、`archivedSessionIds/pinnedSessionIds` 保序、表记录 path 为恢复后 canonical 绝对路径、sessionIds 保序；**确保没有指向不存在 path 的记录**（否则该工作区会话被过滤但不致命，`875-915`）。
   - 不恢复 workspace.json 时：不要创建 initialized=true 的空文件；让 `initialized=false` 触发 bootstrap（标题=目录名、顺序按会话时间），接受 UI 元数据损失。
   - 附件：复制 `attachments/v1/files` 与 `file-objects`（保持内容寻址目录结构），不要复制 `tmp`。
4. **恢复后自检**：打开每个会话（只读）→ 帧扫描完整、header id 与目录名一致、事件 seq 连续（`validateStoredEvents`/`assertContiguous` 的源码位于 `dsh-session-persistence/lib/index.js`）；UI 中会话出现且工作区归属正确；损坏会话标记为“恢复失败/不完整”，不要静默跳过。
5. **绝不**：覆盖目标已有 registry/session 同 id 文件；在 Host 运行时写 workspace.json；复制 `.credentials.yaml`/`host-ready.json`；把真实档案复制进项目目录。

---

## D. 未确认项 / 待手机验收

1. **模型字段**：会话内是否记录每轮 actual model（header 只有 agentPreset）——需在设备上只读导出事件键名确认。
2. **工具调用记录中的文件路径形态**：绝对 vs 相对、是否需要重写（影响展示与非关键引用）。
3. **deliverables / schedules / goals 的持久化位置**：HOME 实测无对应目录；schedule “task store”位置未定位；目标机有 schedule 后需复查。
4. **UI 设置页核心版本来源**：JS 无 coreVersion；疑原生读取 `dshm-core.json`，需 UI 实测对照。
5. **重复 session id 在“不同 projectKey 目录”下的解析行为**：`findLog` 遍历细节未读，未验证跨目录同 id 的冲突语义。
6. **非全新实例的合并恢复**：未验证；建议首版工具只支持全新实例。
7. **torn-tail 副本的端上实际接受度**：源码层可恢复（B2），需真机导入一次验证。
8. **session_projcache 重建行为**：排除后 UI 列表/投影是否即时正常，需真机验证。
9. **附件引用缺失时的 UI 表现**（是否只是图片裂图）未验证。
10. **HarmonyOS 上 flock 实际可用性**：profile 注释称 session-persistence-jsonl 必须保留（经 koffi），本轮未做并发写验证。
11. **our-free-model/session-map.json 语义**（是否会话→模型映射，迁移是否需要）未验证。

---

## E. 敏感与排除清单

### E1. 凭据/令牌（必须排除，绝不打包）
- `HOME/.credentials.yaml`（478B，实测存在；托管凭据文档，`dsh-base/cordis.patch.yml:113-116` 注释）——未读取内容。
- `HOME/host-ready.json`：含 `token` 等鉴权字段（实测键结构，未打印值）；且是运行时握手文件，恢复时由新实例重建。
- `HOME/openai-gateway/api-key`（43B，实测存在）——凭据文件。
- `HOME/.anonymous-user-id`（37B）——遥测用户标识，不应迁移（删除即重置身份）。
- `HOME/dshm-boot-rescue.log`（22795B）——启动救援日志，可能含路径/敏感输出；排除。
- `HOME/our-free-model/settings.json`、`profiles/ondevice/cordis.patch.yml` 等配置中可能含用户自定义 endpoint/参数；默认排除或需人工审阅（本轮未读内容）。

### E2. 缓存/临时/可重建（排除）
- `HOME/storages/session_projcache/**`（投影缓存）
- `HOME/attachments/v1/tmp/**`（附件临时区）
- `HOME/profiles/ondevice/node_modules/**`、`.dsh-market/`、`.plugin-manager/`（插件安装物）
- `HOME/dsh-market/`、`channel-pack/`、`install-queue/`、`open-link/`、`speech-to-text/`、`whale-audio/`、`whale-bubble-imgs/`、`whale-roles/`
- `.dshw-size.json`、`.dshw-turn.json`、`.dshw-usage.json`、`card-state.json`、`card-usage.jsonl`（第三方组件状态；`card-usage.jsonl` 实测 808 行、键 `t/in/out`）
- `app-lifecycle.json`（pid/foreground 运行态）
- `sessions/*/*/session.lock`（0 字节锁文件，恢复时自动重建；可选复制空文件）

### E3. 不该进入项目的
- 真实会话日志/真实档案不得复制到本项目目录；备份产物应为用户指定目录（工作区外或交付目录），并默认排除 E1/E2。
- 本报告不包含任何真实聊天正文、标题或凭据值。
