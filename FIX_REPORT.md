# FIX_REPORT.md — 外部源码复核退回修复报告（插件 v0.1.1）

- 修复对象：`dshm-backup-dev/`（插件 `dshm-backup` 0.1.0 → **0.1.1**）。
- 背景：外部源码复核对全部 JS 文件做了真实语法检查（通过），并用真实 node 执行
  `dshm-backup/test/zip.test.mjs`：**修复前 13/18 通过、5 项失败**（集中于 deflate 条目的
  声明大小/实际解压大小不一致、`hashZipEntries` 失败、`extractZip` 失败、5 MiB 文件读取失败）。
  根因定位到 `lib/zip.js` 的 `writeEntryData()` 管道顺序：`source → deflate → crcTransform`
  使 CRC/`rawSize` 统计的是压缩后数据。
- 本报告记录本轮逐项修改、已执行的检查、只能静态检查项、以及仍需外部真实 JS 测试/手机 DSHM
  验收项。**平板本机无 node CLI，本轮所有 JS 逻辑仍未在平板执行**；外部临时审查副本的
  18/18 结果不冒充“平板已执行 JS 测试”。

---

## 1. ZIP 阻断缺陷（writeEntryData 管道顺序）

**修改文件**：`dshm-backup/lib/zip.js`

- 管道改为：`sourceStream → crcTransform →（method===8 时）createDeflateRaw → sink`。
- 保证 `state.rawSize`/CRC 统计的是**原始数据**；`compressedSize` 由 sink 写入的压缩字节数计算；
  local header（data descriptor 模式）与中央目录分别使用同一组 `crc32Value / compressedSize /
  uncompressedSize`。
- 增加注释说明该顺序是 ZIP 语义要求，禁止改回。

**已执行的检查**：`test/run_tablet_tests.py` 新增「ZIP 写管线顺序」静态回归：
`streams=[sourceStream, crcTransform]`、deflate 只在其后追加、writeEntryData 返回字段、
data descriptor/中央目录字段字节序与变量存在性。结果：PASS。

**仍需**：真实 node 环境执行 `node --test dshm-backup/test/zip.test.mjs` 复验（外部复核已用临时
副本验证 18/18；**正式修复包仍需外部真实测试与手机真实备份复验**，不得由本报告代替）。

## 2. 全新实例误判（系统默认空白工作区）

**修改文件**：`lib/format.js`（`assessFreshTarget` 重写）、`lib/restore.js`（`readTargetState`）。

- `readTargetState` 收集：`storedSessionCount`、`archivedSessionIds`、`pinnedSessionIds`、
  `defaultWorkspaceId`、`workspaceIds`、每个工作区的 `path/sessionIds/title/createdAt/updatedAt`、
  `hasPendingMutation`、Host 当前默认工作区路径（`host-ready.json.workspace` / `paths.workspaceRoot`）。
- `assessFreshTarget` 仅允许严格可识别的「系统自动创建的空白默认工作区」：
  无 stored session、无归档、无置顶、无 pendingMutation；恰好 1 条记录且等于
  `global.defaultWorkspaceId`；`sessionIds` 为空；`path` 与 Host 当前默认工作区一致；
  记录数与 `workspaceIds` 一致；无无法解释的额外记录。
- 其余情况（用户创建的空白工作区、含会话工作区、多个工作区、结构无法确认、Host 默认路径缺失等）
  继续拒绝恢复并给出原因。支持 `workspace.json` 不存在/未初始化且无数据的严格空实例。
- 新增 `targetFingerprint`（registry SHA、会话 id 集合、workspaceIds/default/archived/pinned 等），
  供执行恢复前判断目标是否变化。

**已执行的检查**：新增「全新实例判定矩阵（参考实现）」12 个用例（仅系统默认空白=True，其余均拒绝）、
「全新实例判定规则（代码契约）」。结果：PASS。

**仍需**：手机真实全新实例（系统自动默认工作区）恢复验收；结构差异可能因 DSHM 版本/壳行为不同，
若无法确认则保持拒绝（见 §8）。

## 3. 回滚虚假成功

**修改文件**：`lib/restore.js`（`rollbackRestore` 重写）、`lib/util.js`（新增
`removeFileStrict` / `removeTreeStrict`）、`lib/backup.js`（失败清理、轮换严格判定）。

- 回滚不再使用 `removeFileQuietly` / `removeTreeQuietly` 作为成功依据：
  - 文件/会话目录使用 strict 删除，并在删除后再次 `pathExists` 确认不存在；
  - 本次创建的空目录用 **非递归 `rmdir`**：非空即失败并保留（避免误删未跟踪数据）；
  - `workspace.json` 恢复后重新读取并与快照原文比对；原本不存在时删除后确认不存在；
  - 只有全部创建项与 registry 均确认恢复/清理成功才 `rolledBack=true`；否则 `rolledBack=false`
    且返回 `failures[]` 路径与原因；
  - 保留恢复前 registry 快照（`stateDir/rollback/pre-restore-*.json`）作为证据，不因回滚删除。
- 备份失败路径的 staging/临时文件清理改为严格删除 + 失败记录，不再默认“已清理”。
- `rotateBackups` 使用严格删除并复核；删除失败/未确认写入 `failed[]`，不谎称删除成功。

**已执行的检查**：静态检查（回滚函数无 Quietly、含 strict/rmdir/stillExists/rolledBack 条件、
保留快照）+ 参考实现（非空目录删除失败 → `rolledBack=false`）+ 原有「导入失败回滚」检查。
结果：PASS。

**仍需**：外部真实 JS 测试（模拟删除失败）与手机真实恢复失败/回滚演练。

## 4. 恢复执行前必须重新检查目标

**修改文件**：`lib/restore.js`（`inspectBackup`/`executeRestore`）、`lib/index.js`（`/api/restore`）。

- `inspectBackup` 把 `conflicts`、`canRestore`、`targetFingerprint`、`targetFresh` 写入 `internal`。
- `executeRestore` 在**任何 staging 解压、目录创建或目标写入之前**：
  重新 `readTargetState` → 按新规则重判全新；比较 `targetFingerprint`（变化即
  `RESTORE_PREVIEW_STALE`）；重新计算恢复映射并与预览映射比较；重新执行
  `collectRestoreConflicts`（会话冲突/文件冲突/映射/越界）。任一不符即拒绝并要求重新预览。
- `/api/restore` 服务端强制检查 `internal.canRestore === true` 且 `conflicts` 为空，
  不只依赖客户端禁用按钮。
- 新增 `RESTORE_PREVIEW_STALE` / `RESTORE_PREVIEW_INVALID` 错误码与中文说明。

**已执行的检查**：「预览后目标变化回归（参考实现）：预览后新建会话 → 不再全新、指纹变化」+
代码契约（重读/指纹/映射/冲突检查都发生在 `ensureDir(stagingRoot)` 之前；服务端有
canRestore/conflicts 检查）。结果：PASS。

**仍需**：外部真实 JS 测试与手机「预览后新建会话再确认恢复必须拒绝」的实机验收。

## 5. 异常抢备节流与轮换

**修改文件**：`lib/index.js`（`finalizeJobState` / `triggerEmergencyBackup`）。

- 只有 `kind === "emergency"` 且备份成功后写入 `state.emergencyBootId`；
  manual/baseline 不再改变本次启动的异常抢备状态。
- 紧急备份成功后调用 `rotateBackups(runtime, "emergency")`（在 `createBackup` 成功之后），
  `keepEmergency` 生效；轮换仍只处理 emergency，手动备份永不删除；轮换异常不推翻已成功备份。
- 触发逻辑仍检查 `emergencyBootId === bootId` 与时间冷却。

**已执行的检查**：静态检查（唯一 emergency 赋值、触发前检查、createBackup 先于 rotate）+
参考节流逻辑（manual/baseline 后仍可触发；同启动第二次 emergency 被节流）。结果：PASS。

**仍需**：手机失败页/真实异常信号下的抢备与轮换验收。

## 6. 工作区文件误排除

**修改文件**：`lib/util.js`（`DEFAULT_SKIP_DIR_NAMES`）、`lib/backup.js`（继续使用
`skipAbsPrefixes`）。

- 从全局按名称排除列表移除 `"dshm-backup"` 与 `".dshm-backup-tmp"`。
- 真实 `backupDir` / `stateDir` / `stagingRoot` 仍通过绝对路径前缀精确排除，不会递归打包自身。
- 用户工作区里同名的 `dshm-backup/` 项目目录（例如本项目源码）会作为正常产出文件被备份。

**已执行的检查**：代码契约 + 参考遍历（`dshm-backup/src/index.js` 被收录，`node_modules` 与
真实 backups 输出目录被排除）。结果：PASS。

**仍需**：手机真实备份后检查 ZIP 内是否包含工作区同名目录。

## 7. 插件清单严格缩减为「名称＋版本」

**修改文件**：`lib/util.js`（`readPluginInventory`）、`lib/format.js`（`validateManifest` +
新增 `packageNameOf`）、`lib/backup.js`（`data/plugin-inventory.json` 去掉 profileBundles）、
文档/手机材料。

- `contents.plugins.user` / `.core` 每项**只有** `{name, version}`；版本取不到写 `"unknown"`。
- 删除 spec/kind/id/bundle/disabled/profileBundles 及插件目录信息。
- `validateManifest` 强制 name/version 白名单，额外字段报 `plugin-extra-fields`。
- 核心 scoped 包版本解析：`@scope/pkg[/subpath]` 取前两段包名后定位
  `node_modules/@scope/pkg/package.json`；普通 `pkg/subpath` 取第一段。
- README/FEASIBILITY/HANDOFF_PHONE/PHONE_ACCEPTANCE_PROMPT 同步为「仅名称+版本」。

**已执行的检查**：字段白名单静态检查、参考实现（额外字段被拒）、scoped 包名解析参考用例。
结果：PASS。

**仍需**：外部真实 JS 测试读取核心清单；手机验收核对预览/报告中插件条目只有 name/version。

## 8. 核心版本目录选择

**修改文件**：`lib/util.js`（`pickCoreDir` → `detectCoreDir` + `coreDirSource`）、
`lib/format.js`（`isKnownVersion` / unknown 分支）、`lib/backup.js`（manifest 记录
`coreDirSource`）、`lib/index.js`（状态暴露 coreDirSource）。

- 优先使用 Host 注入的权威环境变量 **`DSHM_CORE_DIR`**（宿主 main.js 注释明确为核心树根目录），
  其次 `DSH_CORE_DIR`/`DSH_CORE_PATH`、`process.argv` 中实际加载的 `cores/<版本>/` 路径、
  `host-ready.json runtime` 显式字段；**取不到即 `coreDir=null`、`coreVersion="unknown"`**。
- 删除「cores 下字典序最后一个/唯一候选」猜测逻辑。
- `describeVersionComparison` 中 unknown 不再与任何版本（包括 unknown）视为相同：level=unknown，
  恢复必须二次确认。
- 残余风险已写入 FEASIBILITY.md：若未来 DSHM 不再注入 `DSHM_CORE_DIR` 且 argv 无核心路径，
  版本会标 unknown，恢复会走“版本不一致需确认”路径而非静默同版本。

**已执行的检查**：静态检查（无字典序猜测、含权威来源与 unconfirmed、默认 coreVersion unknown）
+ 版本比较矩阵参考实现（unknown≠exact、相同完整版本=exact、不同=core-mismatch）。结果：PASS。

**仍需**：手机真实插件运行时核对 `coreDirSource` 实际取值；若与手机运行核心不一致需外部复核。

## 9. 备份「已校验」状态

**修改文件**：`lib/util.js`（`listBackupEntries`）、`lib/index.js`（`buildBackupsSnapshot`、
`finalizeJobState`、`/api/verify`）、`lib/client.js`（界面文案）。

- 列表扫描只报告 `hasSidecar` / `sidecarSha`，`verified` 固定为 false；不再“有旁文件=已验证”。
- `state.verified` 缓存记录**实际重新计算并核对成功**的备份/校验（备份引擎发布前后校验、
  `/api/verify` 完整校验），键为文件路径，记录 sha256/size/verifiedAt/source。
- 状态快照只有缓存 sha 与旁文件 sha 一致、大小一致时才显示 `verified=true` 与 verifiedAt；
  否则显示“未校验”或“仅有旁文件（未校验）”。
- 不新增轮询时的大文件哈希；只有备份成功或用户点击“校验选中备份”后才更新缓存。

**已执行的检查**：静态契约 + 参考缓存判定（匹配才 true）。结果：PASS。
**仍需**：手机界面核对“有旁文件但未校验”与“已校验”两种显示。

## 10. 接通恢复进度

**修改文件**：`lib/index.js`（`/api/restore` → `startJob` 的 progress 传入
`executeRestore`）、`lib/restore.js`（优先 `settings.onProgress`）。

- `executeRestore` 现在优先使用调用方传入的 `onProgress`，其次 runtime.onProgress；
  各阶段更新 phase/percent/message，任务结束后由 job 状态收尾，不再长期停在“排队”。

**已执行的检查**：代码契约（index 传入 onProgress；executeRestore 优先 settings.onProgress；
阶段进度点 ≥5、含 done）。结果：PASS。
**仍需**：手机真实恢复时观察进度条与结束后状态。

## 11. 文档与手机验收材料同步

**修改文件**：`README.md`、`FEASIBILITY.md`、`TEST_REPORT.md`、`HANDOFF_PHONE.md`、
`PHONE_ACCEPTANCE_PROMPT.md`、`dshm-backup/README.md`、`dshm-backup/package.json`、本文件。

- 插件版本 **0.1.1**（与已判定有缺陷的 0.1.0 区分）。
- 文档更新：插件清单仅 name/version；ZIP 修复与外部 13/18→18/18 记录；回滚严格判定；
  恢复前重检；fresh 规则；版本目录权威来源；verified 语义；恢复进度。
- 手机验收改为分阶段：
  1) 先完成**安装/加载 + 合成备份 + 完整性校验**；
  2) 通过后由用户**另行批准**卸载/清数据与全新实例恢复；
  3) “保持会话窗口”不再当作“正在写入”：要求在可丢弃会话中启动足够长的生成/工具写入过程，
     并在仍有事件写入时执行备份；无法稳定制造该条件则标记“未覆盖”，不得宣称通过；
  4) 卸载前必须把测试备份复制到 `Download/com.dshm.dshclient` **之外**或回传平板，
     不能只复制到同一应用目录就称独立外部副本；
  5) 所有结果在重置前回传平板。
- 项目 ZIP 重新生成，并**包含** `test/tablet-test-results.json`；提供新的 SHA-256。

**已执行的检查**：文档存在性/关键内容检查（`test_docs_fix_round`）、版本 0.1.1 检查。
**仍需**：外部源码复审；手机按更新后的 HANDOFF/PHONE_PROMPT 分阶段验收。

---

## 12. 本轮测试实际执行情况

### 12.1 已实际执行（平板内嵌 CPython + esprima + libzstd）

- 真实 JS 解析：6 个宿主 ESM 模块 `parseModule` + `client.js` `parseScript`；
- AST/静态检查：零 inject、禁用 API、依赖、预览零写入、回滚分支、接口一致性、
  ZIP 管道顺序、全新实例规则、恢复前重检、回滚严格判定、异常节流、工作区排除、插件字段、
  核心目录/unknown 版本、verified 语义、恢复进度、文档一致性；
- 合成 fixture + Python 参考实现：会话（中文/多轮/torn/明文/空会话）、文件（中文/空格/嵌套/空/
  无扩展名）、排除规则、凭据不泄漏、参考 ZIP 布局/篡改、路径映射、workspace 改写、fresh 矩阵、
  预览后变化、回滚失败、节流逻辑、工作区目录遍历等。
- 结果：见 `TEST_REPORT.md` 与 `test/tablet-test-results.json`（本轮最终 PASS/FAIL/SKIP 数以该文件为准）。

### 12.2 只能静态检查（本轮未执行 JS）

- `node --input-type=module --check`、`node --test dshm-backup/test/zip.test.mjs`：本机无 node CLI，
  **未在平板执行**；
- ZIP 二进制真实写入/读取/CRC/流式行为：未在平板执行（仅外部临时审查副本曾用真实 node 验证）；
- HTTP 路由/鉴权/body 上限、真实文件系统权限、真实 DSHM 插件加载：未在平板执行；
- `import.meta`/`for await`/可选 catch 绑定在 esprima 4 中的解析归一化：语义正确性需真实运行验证。

### 12.3 仍需外部真实 JS 测试或手机 DSHM 验收

1. 外部真实 node 执行正式修复包的 `zip.test.mjs`（目标 18/18）与全部 JS 单测；
2. 手机安装/冷重启/UI 注入与路由可用性；
3. 手机主动备份（含“仍在写入”的长过程）、导出到应用外、完整性校验；
4. 手机全新实例恢复（系统默认空白工作区识别、会话真实出现、工作区文件/路径映射、插件清单）；
5. 手机卸载/重装后备份保留；
6. 恢复失败回滚、异常抢备、版本不一致警告等破坏性/异常场景（需用户另行批准）。

### 12.4 未解决的兼容性/安全问题

- `workspace.json` 运行期写回的覆盖窗口仍需“立即冷重启”规避（残余风险，见 FEASIBILITY）。
- `coreDir` 依赖 `DSHM_CORE_DIR` 注入；拿不到时按 unknown 处理（不猜测，但会使恢复需二次确认）。
- 系统文件选择/分享仍不可用；ZIP 仍不支持 ZIP64（<4 GiB）；`&path:` 子目录安装形式仍未实测。
- 真实 JS 执行与真机行为必须由外部/手机验证，本报告不将其写成已通过。

---

# 第二轮复审退回修复（v0.1.2，2026-10-24）

本轮针对 v0.1.1 复审阻断问题修复；插件版本升至 **0.1.2**。真实 JS 回归文件新增
`test/backup-boundaries.test.mjs`、`test/restore-boundaries.test.mjs`（需外部真实 node 执行；
平板本机无 node CLI，**未在平板执行**，不得写成已通过）。

## 13. 阻断 1：回滚误递归删除既有会话目录

**修改文件**：`lib/restore.js`。

- 写入会话前记录 `sessionDirExistedBefore`；只有**本次新建**的 sessionDir 才进入
  `journal.createdSessionDirs`；
- 本次写入的日志文件始终单独进入 `journal.createdFiles`；
- 回滚时先 strict 删除日志文件，再对本次新建 sessionDir 使用**非递归 rmdir**；
  既有 sessionDir 不参与目录清理；
- 目录非空/未跟踪文件时 rmdir 失败，保留目录并 `rolledBack=false`，不再使用
  `removeTreeStrict(journal.createdSessionDirs)`；
- 导出 `rollbackRestore` 供真实 JS 回归直接验证。

**真实 JS 回归**：`test/restore-boundaries.test.mjs`
- 既有 sessionDir + 既有无关文件：回滚后无关文件仍在、仅日志被删；
- 本次新建目录：回滚后目录被非递归清理；
- 新建目录中出现未跟踪文件：保留数据且 `rolledBack=false`；
- workspace.json 恢复后重新读取比对。
（平板未执行；外部真实 node 复跑为准。）

## 14. 阻断 2：coreDir 未确认时 platform=null 导致整份备份失败

**修改文件**：`lib/format.js`。

- `source.platform`、`source.nodeVersion`、`coreBuiltAt`、`coreDirSource`、`appVersion`
  允许缺失或 `null`（展示性元数据缺失不得阻断备份）；
- `coreVersion/upstreamDshVersion = "unknown"` 仍保留，恢复时按未知版本二次确认；
- `validateManifest` 不再因 `bad-source-platform` 拒绝合法 manifest。

**真实 JS 回归**：`test/backup-boundaries.test.mjs`
- 构造 `coreDir=null / coreDirSource=unconfirmed / platform=null` 的 runtime；
- manual、baseline、emergency 三种备份均实际生成并通过 `verifyBackupFile`；
- 断言 manifest 中 platform 允许缺失/null、coreVersion 为 unknown。

## 15. 阻断 3：header 损坏会话阻断整份保命备份

**修改文件**：`lib/format.js`、`lib/backup.js`（不完整条目的写入路径已存在）。

- `complete=true` 仍严格要求有效 id/cwd/createdAt/SHA/file/size；
- `complete=false` 允许 `cwd=null`、`createdAt=null`，但必须保留 id/file/sha/size/incompleteReason；
- 不完整会话仍进入 ZIP 与 SHA-256 校验范围，恢复时跳过；
- 修复后单个 header 损坏会话不会使 manual/emergency 整包失败。

**真实 JS 回归**：`test/backup-boundaries.test.mjs`
- 一个有效会话 + 一个 header 损坏会话；
- manual 与 emergency 均成功并通过重新校验；
- manifest 中有效会话 `complete=true`、损坏会话 `complete=false`（cwd/createdAt=null）；
- 损坏会话原始文件进入 ZIP，恢复跳过由 restore 逻辑保证。

## 16. 恢复跳过不完整会话后 registry 保留悬空 ID

**修改文件**：`lib/format.js`（`rewriteWorkspaceRegistry` options + `pruneSkippedSessionReferences`）、
`lib/restore.js`（计算 `skippedSessionIds` 并清理）。

- 仅移除本次明确跳过的 complete=false 会话 ID；
- 同时从 `workspaces[*].sessionIds`、`archivedSessionIds`、`pinnedSessionIds` 清除这些 ID；
- **真正空会话**（只在 registry 中、无日志文件）不在 skippedSessionIds 中，必须保留；
- 恢复报告继续列出 skipped 明细与 skippedSessionIds。

**真实 JS 回归**：`test/backup-boundaries.test.mjs` 的 registry 清理用例：
- 不完整会话被移除；真正空会话保留；归档/置顶中的对应引用同步清理。

## 17. 次要修复

1. **发布后复验失败清理文案**（`lib/backup.js`）：收集三处清理结果；只有全部确认清理成功才写
   “已确认删除不完整备份”，否则在错误中列出可能残留路径与原因，不再无依据声称已删除。
2. **HANDOFF_PHONE.md**：
   - §5 改为“插件清单只包含名称+版本（不含 spec/id/bundle/disabled/插件目录）”；
   - §7 改为“恢复过程不得主动改变插件；其他插件由用户根据名单自行处理，不承诺自动恢复插件名单
     或启停状态”，不再要求“插件数量/启停状态与恢复前一致”。

## 18. v0.1.2 测试与外部执行边界

- **平板已执行**：esprima 解析全部 JS（含新增测试文件）、AST/静态检查、合成 fixture 与 Python
  参考回归；结果见 `TEST_REPORT.md` 与 `test/tablet-test-results.json`。
- **平板未执行 JS**：本机无 node CLI；以下真实 JS 测试**未在平板执行**，需外部审查环境真实运行：
  - `node --test dshm-backup/test/zip.test.mjs`
  - `node --test test/backup-boundaries.test.mjs`
  - `node --test test/restore-boundaries.test.mjs`
- **仍需外部/手机验收**：v0.1.2 正式包真实 JS 测试（ZIP 18/18 + 两份边界回归）、手机分阶段
  安装/加载/合成备份/完整性校验/外部导出；卸载与全新恢复必须另行批准。
- 残余风险：`workspace.json` 运行期写回需冷重启；`DSHM_CORE_DIR` 缺失时版本按 unknown；
  系统文件选择/分享不可用；ZIP64 <4 GiB 限制；`&path:` 安装形式未实测。

---

# v0.1.3 — 手机第一阶段 D1 修复（2026-10-24）

手机第一阶段验收（v0.1.2）结论：安装、冷重启、插件加载、按钮/面板/路由、版本字段检查通过；
**主动备份与自动基线备份在含附件的实例上 100% 失败**（42%、“复制附件”阶段 EACCES），
因此第一阶段阻塞。本版修复 D1 及两项上轮非阻断项；仍未进行卸载/清数据/全新恢复/异常测试。

## 19. D1（阻断）：0o400 附件导致 copyFileAtomic 后 fsync 失败

**现象**：附件存在时 manual/baseline 均约 0.2s 失败在 42%，错误指向
`.../dshm-backup/staging/backup-*/payload/data/attachments/.../*.tmp` 的 `EACCES`。

**根因链（手机实测 + 代码）**：
1. DSHM `dsh-attachment-local` 把附件对象有意 chmod 为 `0o400`（只读）；
2. `dshm-backup/lib/util.js` 的 `copyFileAtomic` 使用 `copyFile(source, tmp)`，Node/libuv
   会让 tmp 继承源文件 mode（0400）；
3. 随后 `fsyncFile(tmp)` 用 `open(tmp, "r+")`，0400 无写位 → `EACCES`；
4. 整个备份 job 失败，staging 被清理。

**修改**（`dshm-backup/lib/util.js`）：`copyFileAtomic` 在 `copyFile` 之后显式
`chmod(tmp, 0o600)`（支持 options.mode 覆盖）再 `fsyncFile/rename`；源文件 mode 不变。
这样既修复只读附件，也不会让备份 staging 产物保持不可写。

**真实 JS 回归（新增，外部 node 执行；平板未执行）**：
`test/attachment-copy.test.mjs`
- 0400 源文件经 `copyFileAtomic` 复制成功，源 mode 仍为 0400，目标 mode 0600；
- 构造含 0400 附件 + 明文 v4 会话的实例，`createBackup("manual")` 成功、`verifyBackupFile` 通过、
  manifest 附件 SHA-256 正确、附件进入 ZIP。
- 覆盖 coreDir=null/platform=null（同时回归 v0.1.2 阻断 2）。

**仍需**：外部真实 node 执行该测试；手机第一阶段重新验收（安装加载 + 合成备份 + 校验 +
外部导出），通过后才谈后续阶段。**未进行**卸载、清数据、全新恢复或异常测试。

## 20. 上轮非阻断项处理

1. **`test/backup-boundaries.test.mjs` 无 zstd API 环境的可移植性**：
   增加 `HAS_ZSTD` 检测；无 `zlib.zstdCompressSync` 时自动改用产品同样支持的**明文 v4 会话**
   fixture（`session.v4.jsonl`），并在测试文件头标注替代路径；不再在 fixture 构造阶段失败。
2. **`TEST_REPORT.md` 旧概述**：已统一为当前平板检查计数（本轮更新为最新 PASS 数），
   不再保留“75+/75”歧义表述。
3. 非阻断观察（未在本版修复，记录为已知限制）：
   - `state.core.appVersion` 仍为 null：宿主 node 侧无稳定应用版本通道，面板显示 unknown；
   - 端侧安装器只接受 npm/GitHub 标识、不接受本地目录/zip：属部署流程限制；
   - 基线失败提示的醒目程度需手机侧人工确认。

## 21. v0.1.3 交付与边界

- 插件版本 **0.1.3**；项目 ZIP 与 SHA-256 在最终汇报中给出（避免文档自引用导致哈希变化）。
- 平板端可执行部分：esprima 解析全部 JS/测试文件、AST/静态检查、合成 fixture 与 Python 参考回归；
- **平板未执行真实 JS**（无 node CLI）；真实 JS 测试仍需外部环境：
  `node --test dshm-backup/test/zip.test.mjs`、
  `node --test test/backup-boundaries.test.mjs`、
  `node --test test/restore-boundaries.test.mjs`、
  `node --test test/attachment-copy.test.mjs`。
- 手机第一阶段需在外部复跑通过后重新执行；卸载保留、全新实例恢复、真实恢复/回滚与异常测试
  仍须分别获得明确批准；不得在平板主力实例安装/启用/停用本插件。

---

# v0.1.4 — 手机第二阶段 D2 修复（2026-10-24）

## 22. D2（阻断第二阶段）：沙箱→用户可见目录的 copyFile 被 OHOS 拒绝（EPERM）

**手机实测（v0.1.3，第二阶段）**：
- 预览正常：6 会话 / 1 工作区 / 6 文件 / 4 附件；目标全新；源工作区目录被手机侧删除后，
  映射按设计重映射到 `Download/com.dshm.dshclient/dshm-backup-restored/dshm-backup-phone-test`。
- 恢复在 **78%** 失败：
  `EPERM: operation not permitted, copyfile`
  `…/dsh-home/dshm-backup/staging/restore-*/data/workspace-files/ws-0001/README`
  → `…/Download/com.dshm.dshclient/dshm-backup-restored/…/README.<uuid>.tmp`。
- 同一用户目录内此前 `mkdir` 与普通 write（备份 zip）可成功，附件 0400（D1）已修复；
  因此定位为 `copyFile` 系统调用/沙箱策略在“应用沙箱 → 用户可见目录”方向被拒，
  不是目录权限或 mode 问题。手机 UI 提示“恢复未完成，目标数据可能处于未完成状态”。

**修改**（`dshm-backup/lib/util.js`）：
- `copyFileAtomic` 不再调用 `copyFile`；改为
  `createReadStream(source) → createWriteStream(tmp, {flags:"wx", mode:0o600})` 的
  `pipeline` 普通读写复制，随后 `chmod(tmp, targetMode)` / `fsyncFile` / `rename`。
- 保留 D1 的 0o400 源文件读取兼容与目标 0600 可写；`options.mode` 仍可覆盖。
- 备份 staging 复制（用户目录→沙箱）与恢复复制（沙箱→用户目录）统一走流式路径，避免方向差异。

**真实 JS 回归**（`test/attachment-copy.test.mjs` 新增，外部 node 执行；平板未执行）：
- “沙箱→用户可见目录嵌套目标”用例：0400 源文件复制到
  `Download/com.dshm.dshclient/dshm-backup-restored/.../README`，断言内容与目标 0600。
- 静态契约：`util.js` 不得再出现 `copyFile(`，必须使用 pipeline/流式复制。

**第二阶段失败证据归档**：
- `reports/phone-stage2-failure-v0.1.3-20261024.md`；
- 截图 `reports/stage2-failure-v0.1.3/`（预览计数/映射、警告与 manifest、78% EPERM、操作卡）。

**后续要求**：
- v0.1.4 需外部真实 node 测试与手机第二阶段重跑；重跑前必须确认/清理上次失败留下的
  部分目标数据（DShM 数据与 `dshm-backup-restored` 目录），并由用户再次明确批准清洗/重装；
- 卸载保留与全新恢复尚未通过；异常测试/回滚演练仍另行批准。

---

# v0.1.5 — 手机第二阶段 D3 修复（2026-10-24）

## 23. D3（阻断第二阶段）：目标用户目录禁止 chmod（EPERM）

**手机实测（v0.1.4，第二阶段，18:01）**：
- 恢复仍到 78% 失败，但操作名从 v0.1.3 的 `copyfile` 变为 **`chmod`**：
  `EPERM: operation not permitted, chmod`
  → `Download/com.dshm.dshclient/dshm-backup-restored/dshm-backup-phone-test/README.<uuid>.tmp`。
- 说明 v0.1.4 的流式复制已经成功（临时文件已创建并写入），失败在复制后的
  `chmod(tmp, 0o600)`；OHOS 允许普通 read/write，但禁止对用户可见 Download 下的文件 chmod。
- 证据：`reports/phone-stage2-failure-v0.1.4-20261024.md` 与
  `reports/stage2-failure-v0.1.4/` 截图（sha256 `57cac1d761aa347e…`）。

**修改**（`dshm-backup/lib/util.js`）：
- `copyFileAtomic` **移除目标 chmod**；目标权限仅由
  `createWriteStream(tmp, { flags:"wx", mode: targetMode })` 的 mode 决定（默认 0o600）。
- `fsyncFile` 由 `open(filePath, "r+")` 改为 `open(filePath, "r")`，只读 fd 也可 fsync，
  避免系统收紧权限后写打开失败。
- D1 的 0400 源文件、D2 的流式复制均保留；不产生新的 chmod 依赖。

**回归**：
- 静态契约：`util.js` 不得出现 `chmod(`；`fsyncFile` 不得使用 `"r+"`；
  `copyFileAtomic` 必须使用 createWriteStream + mode。
- 真实 JS `test/attachment-copy.test.mjs` 保留 0400 源复制与沙箱→用户目录嵌套目标用例；
  平板未执行（无 node CLI）。
- v0.1.5 仍需外部真实 node 测试与手机第二阶段重跑；重跑前必须清理上次失败残留并由用户
  再次确认清洗/重装，不得使用异常抢备。

## 24. 手机第二阶段当前结论（截至 v0.1.5 交付）

- v0.1.3：预览正常，恢复 78% `copyfile` EPERM（D2）。
- v0.1.4：预览/流式写入通过到同一阶段，`chmod` EPERM（D3）。
- v0.1.5：修复 D3，待手机重跑；第二阶段尚未通过。
- 卸载保留、全新恢复、真实回滚/异常测试均未通过；不得宣称通过。

---

# v0.1.6 — v0.1.5 复查与恢复写入加固（2026-10-24）

## 25. 复查 v0.1.5：chmod 已移除，但仍有用户目录元数据操作风险

复查 `copyFileAtomic` 在 v0.1.5 的完整步骤：
`createReadStream → createWriteStream(tmp) → fsyncFile(tmp, "r") → rename(tmp, target) → fsyncDir`。

- chmod 已移除（D3 修复正确）；
- 但 `fsync/rename/fsyncDir` 仍是针对**用户可见 Download** 的元数据/目录操作。手机只证明了
  “普通 write 成功”，没有证明这些操作在该目录一定可用；若再次被 OHOS 拒绝，恢复会第三次失败。
- 恢复流程中用户目录写入点只有“工作区文件”一处（会话/附件/registry 都写 DSH_HOME）；
  因此针对该处做最小化加固即可，不需要改备份/校验路径。

**加固**（v0.1.6）：
- 新增 `util.copyFileDirect(source, destination, options)`：只做
  `createWriteStream(destination, { flags:"wx", mode:0o600 })` 的普通流式写入最终路径；
  **不创建临时文件、不 chmod、不 fsync、不 rename**；失败时删除部分目标并抛出。
- `restore.js` 的工作区文件复制改用 `copyFileDirect`，并在写入**之前**把目标路径加入
  `journal.createdFiles`，失败时由 rollback 清理部分文件；
- 其他复制（备份 staging、附件恢复等 DSH_HOME 内部）继续用 `copyFileAtomic`，保留原子性。
- 真实 JS 回归 `test/attachment-copy.test.mjs` 新增 `copyFileDirect` 用户目录嵌套目标用例；
  静态契约要求 restore 使用 `copyFileDirect` 且先记录回滚。

## 26. v0.1.6 的边界

- 仍未在手机验证；第二阶段未通过。需要外部真实 node 测试 + 手机重跑（重跑前再次清理残留并
  由用户明确确认第二次清洗/重装）。
- 直接写入不提供原子性；但目标已确认不存在，失败由回滚清理，属于为受限文件策略做的取舍，
  已在恢复报告中保留失败路径处理。

---

# v0.1.7 — 修复 copyFileDirect 误删既有文件（2026-10-24）

## 27. 阻断缺陷：EEXIST 时无条件删除既有目标，且 restore 过早登记 journal

**外部复审实测（v0.1.6）**：
- `copyFileDirect` 使用 `createWriteStream(destination, {flags:"wx"})`；目标已存在时写入以
  `EEXIST` 失败；catch 却无条件 `rm(destination, {force:true})`，导致**既有目标文件被删除**。
- 复现结果：`{"errorCode":"EEXIST","preexistingFileSurvived":false,"content":null}`。
- `restore.js` 还在调用前无条件 `journal.createdFiles.push(targetFile)`；若预检后目标被其他进程
  创建，`copyFileDirect` EEXIST 后外层 rollback 会按 journal 删除该既有文件。
- 现有 91/91 测试未覆盖“目标已存在”失败路径，因此不能排除该缺陷。

**修复（v0.1.7）**：
1. `copyFileDirect` 改为先 `open(destination, "wx", targetMode)` **排他创建**；
   - 只有 open 成功才视为本次调用创建的文件；
   - EEXIST/open 失败：不删除 destination、不调用 onCreated、不登记 journal；
2. 创建成功后、开始流式复制前，调用 `options.onCreated(destination)` 完成 journal 登记；
3. `restore.js` 不再在调用前 push；只在 onCreated 回调中登记；
4. 复制失败时只清理由本次创建的文件；若清理失败，onCreated 已登记的路径保留，外层
   `rollbackRestore` 继续严格清理并如实报告 `rolledBack=false`；
5. 文件句柄/流在成功、读失败、写失败路径关闭；保持 `"wx"` 拒绝覆盖语义；
6. `copyFileAtomic`、备份与附件恢复路径未改动。

**真实 JS 回归（新增 `test/direct-write.test.mjs`，外部 node 执行；平板未执行）**：
- 目标已存在：抛 EEXIST；既有内容与 SHA-256 不变；onCreated 不触发；
- 模拟预检后目标被其他过程创建并进入 `rollbackRestore`：journal 为空，既有文件保留；
- 目标不存在、源读取失败：登记后只清理本次创建的文件，journal 保留跟踪；
- 正常直接写入：onCreated 一次、内容一致、mode 0600；
- 清理失败：`rollbackRestore` 保留路径并报告 `rolledBack=false`（既有 restore-boundaries 用例）。
- 原 0400/D2/ZIP/备份边界测试保持通过（外部真实执行）。

## 28. 交付与边界

- 版本 0.1.7；需外部真实 JS 测试（ZIP、backup-boundaries、restore-boundaries、
  attachment-copy、direct-write）与手机第二阶段重跑。
- 手机第二阶段重跑前仍需清理上次残留并由用户明确确认；异常测试仍未批准。

---

# v0.1.8 — 悬浮备份按钮支持长按拖动（2026-10-24）

## 29. 需求：备份按钮易误触，可调整位置

**需求**：右下角「备份」按钮容易误触；改成类似桌面图标长按拖动，可移动到页面其他位置。

**实现**（`lib/client.js`，ES5 IIFE，无新增依赖）：
- 长按约 500ms 进入拖动；移动阈值 10px，超过则取消长按视为误触/滑动；
- 拖动中限制在视口内（四边 4px 边距），松手保存到 `localStorage` 键
  `dshm-backup.fab-pos.v1`；页面挂载/窗口 resize 时恢复并重新夹取；
- 普通轻点仍打开面板；长按/拖动后抑制紧随的 click，避免误开面板；
- 长按触发前若移动超过阈值，取消长按并抑制 click；
- 触摸使用 `touchstart/touchmove/touchend`，桌面使用 `mousedown/mousemove/mouseup`；
  拖动时 `touch-action:none`、禁用 `-webkit-touch-callout`，并阻止 contextmenu；
- 失败页救援按钮同样可拖动；按钮 title/aria 与面板提示文案注明“长按可拖动位置”；
- 不影响备份/恢复/API；不读取 Cookie，localStorage 仅保存按钮坐标。

**测试**：
- 平板 esprima 解析 ES5 与静态契约检查（事件/阈值/持久化键/拖动类名）；
- 真实手机 UI 拖动需后续手机验收（本轮未安装/未操作手机）；
- 真实 JS 测试（Node）不覆盖 DOM 拖动，需额外浏览器/手机验证。

