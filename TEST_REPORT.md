# TEST_REPORT.md — 平板端测试、静态检查与外部复核修复记录（插件 v0.1.4）

- 设备：华为 MatePad Edge 平板（HarmonyOS 7.0.0.109SP6），DSHM `com.dshm.dshclient`，
  平板实测核心 `0.2.1-rc.2+dshm.73`。
- 测试环境：DSHM 内嵌 CPython 3.12；真实 JS 解析器 esprima 4.0.1；内嵌 libzstd（ctypes）
  用于构造/解析合成会话 fixture。**本机无 node CLI，JS 逻辑不在平板执行。**
- 入口：`test/run_tablet_tests.py`；结果：`test/tablet-test-results.json`（随项目 ZIP 交付）。
- 本轮性质：**外部源码复核退回修复后的自测**；修复清单与逐项修改见 `FIX_REPORT.md`。

## 0. 必须先看的结论层级

| 层级 | 本机状态 | 说明 |
|---|---|---|
| 未检查 | 部分项 | 无 JS 运行时，JS 逻辑执行未检查 |
| 静态语法检查 | ✅ 已执行 | esprima 真实 `parseModule`/`parseScript` + 词法平衡检查（`node --check` 未执行：无 node CLI） |
| 合成测试 | ✅ 已执行（Python 参考实现 + 合成 fixture） | 验证数据格式/清单契约/路径映射/安全规则/发布/回滚/节流等；**不是**执行插件 JS |
| DSHM 真实加载 | ❌ 未执行 | 平板禁止安装本插件 |
| 真实安装/备份/恢复/卸载保留 | ❌ 未执行 | 留待手机金丝雀验收 |

**不得把本报告的静态/合成结论写成“插件 JS 已在 DSHM 中运行通过”。**

## 1. 外部真实 ZIP 测试结果（本轮退回的起点）

- 外部源码复核对全部 JS 文件执行**真实语法检查**：通过。
- 外部用**真实 node** 执行 `dshm-backup/test/zip.test.mjs`（修复前 0.1.0）：
  - 总计 18 项，**13 项通过、5 项失败**；
  - 失败集中在 deflate 条目：声明大小与实际解压大小不一致、`hashZipEntries` 失败、
    `extractZip` 失败、5 MiB 文件读取失败；
  - 根因：`lib/zip.js` 的 `writeEntryData()` 把 `createDeflateRaw` 放在 `crcTransform` 之前，
    使 CRC / `rawSize` 统计的是压缩后数据，而 ZIP 中央目录需要原始数据 CRC/大小。
- 修复：管道改为 `source → crcTransform → (deflate) → sink`。
- 外部临时审查副本仅调整该顺序后复测：**18/18 通过**。该结果用于确认根因，
  **不属于平板执行结果**；正式修复包仍需外部真实 JS 测试与手机真实备份复验。

## 2. 本轮已实际执行并通过的检查（以 `tablet-test-results.json` 为准）

本轮检查覆盖原有 50 项 + v0.1.1 回归 25 项 + v0.1.2 复审边界 5 项 + v0.1.3 D1 回归 4 项 + v0.1.4 D2 回归 3 项，最终运行 **PASS=87 FAIL=0 SKIP=0**。

### 2.1 真实 JS 解析与语言约束
- `lib/index.js`、`backup.js`、`restore.js`、`util.js`、`format.js`、`zip.js` 全部通过
  esprima `parseModule`；`lib/client.js` 通过 `parseScript`。
- `client.js` 注入式/ES5 约束：无 import/箭头/模板/let/const/class/async/require/setInterval；
  含幂等哨兵与同源 API 前缀。
- 词法平衡检查：7 个文件全部平衡。

### 2.2 ZIP 修复回归
- **ZIP 写管线顺序**：`streams = [sourceStream, crcTransform]`，deflate 只在其后追加；
  `writeEntryData` 返回 `crc32/uncompressedSize/compressedSize`；data descriptor 与中央目录
  使用原始 CRC/大小。PASS。
- 原有参考 ZIP 布局/篡改/路径安全/发布协议检查继续 PASS。

### 2.3 全新实例判定
- 全新实例判定矩阵（参考实现）12 个用例：**只有**「系统自动默认空白工作区」判为全新；
  用户创建的空白工作区、path 不一致、含会话 ID、多个工作区、归档、置顶、pendingMutation、
  已有 stored session、额外工作区记录、Host 默认路径缺失、未初始化但有记录均拒绝。PASS。
- 代码契约：`readTargetState` 收集默认工作区字段；`assessFreshTarget` 严格条件。PASS。

### 2.4 恢复前重检 / 预览后变化
- 「预览后新建会话」参考回归：fresh 由 true 变 false、targetFingerprint 变化 → 必须拒绝。PASS。
- 代码契约：`executeRestore` 在任何 staging 解压/目录创建/写目标之前重读目标、比较指纹、
  重新计算映射、复查冲突；`/api/restore` 服务端强制检查 `canRestore`/`conflicts`。PASS。

### 2.5 回滚严格判定
- 代码契约：回滚不再使用 Quietly 删除；strict 删除 + 再次存在性检查；空目录用非递归 rmdir；
  registry 恢复后重读比对；失败 `rolledBack=false`；保留 registry 快照。PASS。
- 参考实现：非空目录删除失败 → `rolledBack=false`，不假报成功。PASS。

### 2.6 异常抢备节流/轮换
- 只有 `kind === "emergency"` 成功后才写 `emergencyBootId`；manual/baseline 不改变本次启动状态。PASS。
- 紧急备份 `createBackup` 成功后才 `rotateBackups(emergency)`。PASS。
- 参考节流：manual/baseline 后仍可触发；同一启动第二次 emergency 被节流。PASS。

### 2.7 工作区同名目录
- 全局按名称排除列表已移除 `dshm-backup` / `.dshm-backup-tmp`；真实 backup/state/staging 仍按
  绝对路径排除。PASS。
- 参考遍历：工作区 `dshm-backup/src/index.js` 被收录；`node_modules` 与真实 `backups/`
  输出目录被排除。PASS。

### 2.8 插件清单严格 name/version
- user/core 字段白名单只有 `name/version`；无 profileBundles/spec/kind/id/bundle/disabled。PASS。
- scoped 包名解析：`@scope/pkg/sub` → `@scope/pkg`；`pkg/sub` → `pkg`。PASS。
- `validateManifest` 拒绝插件额外字段（`plugin-extra-fields`）；参考实现同样拒绝。PASS。

### 2.9 核心目录/unknown 版本
- 不再按 cores 字典序/唯一候选猜核心；权威来源 `DSHM_CORE_DIR`、`DSH_CORE_DIR`、
  `process.argv` 实际加载路径；取不到标 `unconfirmed`、coreVersion `unknown`。PASS。
- unknown 版本不参与同版本判定（unknown≠exact）；版本比较矩阵参考实现 PASS。

### 2.10 「已校验」状态
- 列表扫描只报告 `hasSidecar/sidecarSha`，`verified` 固定 false；只有真实复算并核对成功的缓存
  才显示 true；界面显示“未校验/仅有旁文件（未校验）”。PASS。
- 参考缓存判定（SHA/大小匹配才 true）PASS。

### 2.11 恢复进度
- `startJob` 的 progress 已传入 `executeRestore`；`executeRestore` 优先 `settings.onProgress`；
  阶段进度点 ≥5 且含完成。PASS。

### 2.12 原有检查（50 项）
中文/多轮/空会话/torn/明文 fixture、中文/空格/嵌套/空文件/无扩展名、排除规则、凭据不泄漏、
参考 ZIP 布局/篡改、路径映射、workspace.json 改写、重复备份不覆盖、发布协议、轮转、
零 inject、禁用 API、预览零写入（AST）、回滚分支（AST）、DSHM 适配链路等继续 PASS
（详见结果 JSON）。

## 3. 已执行但失败的项目

- 本轮自测未发现新的产品级失败；但**外部真实 ZIP 测试的失败是真实存在的**：
  修复前 13/18（5 项失败），根因与修复已记录在 §1 与 FIX_REPORT.md；
  外部临时副本复测 18/18 只说明修复方向正确，**正式修复包仍需外部复测与手机验收**。
- 测试脚本迭代中修复的问题（不计入产品结论）：esprima 4 的 ES2017 语法上限（`import.meta`/
  `for await`/可选 catch 绑定做解析归一化）、libzstd 解压单帧、文档检查误判/缺文件等。

## 4. 只能静态检查 / 未执行（本机）

| 项目 | 状态 | 原因/替代 |
|---|---|---|
| `node --input-type=module --check`（要求 7.12） | **未在平板执行** | 本机无 node CLI；替代=esprima 真实解析+词法检查，**不等价** |
| `node --test dshm-backup/test/zip.test.mjs` | **未在平板执行** | 同上；外部真实 node 测试结果见 §1 |
| ZIP 二进制真实写/读/CRC/流式 | 未在平板执行 | 仅静态/参考检查；外部临时副本曾真实执行 |
| HTTP 路由/鉴权/body 上限 | 未执行 | 需插件在 Host 运行；手机验收 |
| node:fs/zlib/crypto 插件运行期行为 | 部分证据 | 核心包使用证据见 FEASIBILITY；插件自身未运行 |
| 真实 DSHM 加载/备份/恢复/卸载保留 | 未执行 | 平板禁止；手机验收 |
| ES2018/2020 语法语义 | 未完整校验 | 解析归一化绕过，需真实运行验证 |

## 5. 需求「七、平板端允许的测试」逐条对照（更新）

| # | 要求 | 状态 |
|---|---|---|
| 1 | 中文标题/多轮/空会话 fixture | ✅ 合成参考测试 |
| 2 | 中文文件名/空格/嵌套/空文件/无扩展名 | ✅ 合成参考测试 |
| 3 | 插件清单只名称+版本 | ✅ 静态/参考（v0.1.1 修复后严格 name/version） |
| 4 | 模拟凭据不进入备份/报告/日志 | ✅ 合成参考 |
| 5 | 备份包篡改后拒绝导入 | ✅ 参考实现 + JS 校验逻辑静态检查 |
| 6 | 重复备份不覆盖旧文件 | ✅ 代码契约 + 参考 |
| 7 | 备份中途失败不留正式文件 | ✅ 发布协议参考 + 严格清理代码契约 |
| 8 | 导入预览零写入 | ✅ AST |
| 9 | 路径映射正确不越界 | ✅ 参考 + 代码 |
| 10 | 导入失败回滚或标未完成 | ✅ 参考 + 严格回滚代码契约 |
| 11 | 自动轮换不删手动/上一有效基线 | ✅ 代码契约 + 严格删除/失败报告 |
| 12 | ESM 用 `node --input-type=module --check` | ❌ **未在平板执行**（无 node）；替代=esprima；外部真实测试待复测 |
| 13 | 无 PC 专用步骤 | ✅ 文档/依赖检查 |

## 6. 仍需外部真实 JS 测试或手机 DSHM 验收

1. 外部用真实 node 执行**正式修复包**的 `zip.test.mjs`（目标 18/18）与 JS 单测；
2. 手机分阶段验收：
   - 阶段 A：安装/冷重启/UI 注入/路由；合成会话与文件；主动备份；“仍在写入”的长生成/工具写入
     过程备份（无法制造则标未覆盖）；导出到 `Download/com.dshm.dshclient` **之外**；完整性校验；
   - 阶段 B（用户另行批准）：卸载/重装后备份保留；全新实例恢复；会话真实出现、工作区文件/
     路径映射、插件清单仅 name/version、未安装任何插件；失败回滚；异常测试（另行批准）。
3. 手机版本维度与平板对照；若核心版本/`coreDirSource` 不一致，按 unknown/警告处理。

## 7. 证据边界

1. JS 逻辑未在平板执行：证据来自真实解析、AST 静态检查与 Python 参考实现；
2. ZIP 二进制行为未在平板执行；外部真实测试修复前 13/18，临时副本 18/18；
3. HTTP/权限/卸载等系统行为未执行；
4. 合成 fixture ≠ 真实档案（规模/并发/边界可能不同）；
5. `test/run_tablet_tests.py` 是开发期验证工具，不是“设备端插件运行”的证明。

---

## 9. v0.1.2 第二轮复审退回修复（本轮）

### 9.1 本轮修复对应检查（平板实际执行）

- **回滚边界**：代码契约 + 参考检查：
  - 仅本次新建 sessionDir 进入目录清理；既有 sessionDir 只删除本次日志；
  - 非递归 rmdir；未跟踪文件存在时 `rolledBack=false`；
  - `rollbackRestore` 已导出供真实 JS 回归。
- **coreDir 未确认**：`format.js` 允许 platform/nodeVersion 等展示性字段缺失或 null；
  `coreVersion/upstreamDshVersion` 仍为 unknown；备份不会因 `bad-source-platform` 失败。
- **损坏 header 会话**：complete=true 严格；complete=false 允许 cwd/createdAt=null，
  仍保留 id/file/sha/size/incompleteReason 并进入 ZIP/SHA 校验。
- **registry 跳过清理**：`pruneSkippedSessionReferences` 只移除明确跳过的 ID，
  同步清理 workspace sessionIds/archived/pinned；真正空会话保留。
- **发布后复验失败清理文案**：清理结果未确认时错误文案列出残留路径，不声称已删除。
- **HANDOFF 文档**：插件清单仅名称+版本；不承诺恢复插件启停状态。
- **新增真实 JS 回归文件**：`test/backup-boundaries.test.mjs`、`test/restore-boundaries.test.mjs`；
  本机仅做 esprima 解析与存在性/覆盖静态检查，**未在平板执行**。

### 9.2 真实 JS 测试（新增，需外部真实 node 执行）

| 文件 | 覆盖 | 平板状态 |
|---|---|---|
| `test/backup-boundaries.test.mjs` | coreDir=null/platform=null 三类备份；损坏 header；registry 跳过清理 | **未在平板执行**（无 node CLI） |
| `test/restore-boundaries.test.mjs` | 既有 sessionDir+无关文件；新建目录清理；未跟踪文件 rolledBack=false；registry 恢复 | **未在平板执行**（无 node CLI） |
| `test/zip.test.mjs` | ZIP 读写/CRC/压缩/路径安全（v0.1.1 外部 18/18） | 外部已执行；本平板未执行 |

> 平板端 75+/75 PASS 仅代表 esprima 解析 + AST/静态检查 + 合成参考测试；
> **不代表上述真实 JS 测试已在平板通过**。外部复审需真实执行两份新增边界回归。

### 9.3 仍未解决/待验收

- 外部真实 node 执行 v0.1.2 正式包的三份 JS 测试（ZIP 18/18 + 备份/回滚边界）；
- 手机分阶段：安装加载、合成备份、完整性校验、外部导出；卸载/全新恢复另行批准；
- 残余风险：workspace.json 冷重启窗口、`DSHM_CORE_DIR` 缺失时 unknown、无系统文件选择/分享、
  ZIP64 <4 GiB、`&path:` 安装形式未实测。

---

## 10. v0.1.3：手机第一阶段 D1 修复后的平板自测（本轮）

### 10.1 手机第一阶段实际结果（来自手机回传，非平板测试）

- 安装/冷重启/插件加载/按钮/面板/路由/版本字段：通过；
- 主动备份与自动基线备份：**失败**（42%，附件复制阶段 `EACCES`），第一阶段阻塞；
- 根因：DSHM 附件为 0o400，`copyFileAtomic` 的 tmp 继承 0400，随后 `fsyncFile` 以 `r+` 打开失败；
- 本版修复：`copyFileAtomic` copy 后显式 chmod 0o600 再 fsync；源 mode 不变；
- 真实 JS 回归新增：`test/attachment-copy.test.mjs`（0400 源复制 + 含 0400 附件实例备份）。

### 10.2 本轮平板端实际执行

- esprima 解析全部 JS 与全部 JS 测试文件（含新增 `attachment-copy.test.mjs`）；
- AST/静态检查：`copyFileAtomic` 的 chmod 位于 fsync 之前；无继承 0400 的旧路径；
- 合成 fixture/Python 参考回归：原有 80 项 + 本轮 D1 修复回归；
- 结果以 `test/tablet-test-results.json` 为准；其中仍**不包含**真实 JS 执行。

### 10.3 真实 JS 测试（新增/保留，均需外部 node 执行）

| 文件 | 覆盖 | 平板状态 |
|---|---|---|
| `dshm-backup/test/zip.test.mjs` | ZIP 读写/CRC/压缩/路径安全 | 外部 v0.1.2 已 18/18；v0.1.3 待复跑 |
| `test/backup-boundaries.test.mjs` | coreDir=null/platform=null、损坏 header、registry 跳过；新增无 zstd 时明文 fallback | **未在平板执行** |
| `test/restore-boundaries.test.mjs` | 既有 sessionDir+无关文件、新建目录、未跟踪文件 rolledBack=false | **未在平板执行** |
| `test/attachment-copy.test.mjs` | 0o400 源复制、含 0400 附件实例备份/校验 | **未在平板执行**（本版新增，外部待跑） |

### 10.4 未解决/待验收

- 外部真实 node 执行 v0.1.3 四份 JS 测试；
- 手机第一阶段重新验收（安装加载、合成备份、校验、外部导出）；
- 卸载保留/全新恢复/回滚/异常测试仍须分别批准；
- 已知限制：appVersion 无法取得（unknown）、端侧安装器仅支持 npm/GitHub 标识、
  workspace.json 冷重启窗口、`DSHM_CORE_DIR` 缺失时 unknown、无系统文件选择/分享、ZIP64<4GiB、
  `&path:` 安装形式未由平板实测。

---

## 11. v0.1.4：手机第二阶段 D2（copyFile EPERM）修复后的平板自测

### 11.1 手机第二阶段实际结果（来自手机截图/记录，非平板测试）

- v0.1.3 预览正常（6/1/6/4、目标全新、重映射），恢复在 78% 因
  `EPERM copyfile`（沙箱 staging → 用户 Download `dshm-backup-restored`）失败；
- 失败后目标可能部分写入；第二阶段未通过；卸载保留/恢复结论暂不能给出；
- 证据归档：`reports/phone-stage2-failure-v0.1.3-20261024.md` 与截图目录；
- 本版修复：`copyFileAtomic` 改为流式 read/write（pipeline），不再调用 `copyFile`。

### 11.2 平板已执行

- esprima 解析全部 JS 与 JS 测试文件（含新增 D2 用例）；
- 静态检查：`util.js` 无 `copyFile(`；`copyFileAtomic` 使用 createReadStream/createWriteStream/pipeline，
  保留 chmod 0600 与 fsync；
- 原 D1/第二阶段阻断回归、静态/合成检查继续执行；最终计数以 `test/tablet-test-results.json` 为准。
- **平板未执行真实 JS**（无 node CLI）。

### 11.3 真实 JS 测试（需外部 node 执行）

| 文件 | 覆盖 | 平板状态 |
|---|---|---|
| `dshm-backup/test/zip.test.mjs` | ZIP 18/18 基线 | 外部 v0.1.2 已通过；v0.1.4 待复跑 |
| `test/backup-boundaries.test.mjs` | coreDir/platform、损坏 header、registry | 未在平板执行 |
| `test/restore-boundaries.test.mjs` | 回滚既有目录/未跟踪文件 | 未在平板执行 |
| `test/attachment-copy.test.mjs` | D1 0400 附件 + D2 沙箱→用户目录嵌套目标 | 未在平板执行（v0.1.4 新增 D2 用例） |

### 11.4 未解决/待验收

- 外部真实 node 执行 v0.1.4 四份 JS 测试；
- 手机第二阶段重跑（需先清理上次失败的部分目标数据，用户再次批准清洗/重装）：
  卸载保留 + 全新实例恢复；
- 异常测试/回滚演练另行批准；
- 已知限制：appVersion null、安装通道仅 npm/GitHub、workspace.json 冷重启窗口、
  coreDir unknown 兜底、无系统文件选择/分享、ZIP64<4GiB、`&path:` 安装形式未平板实测。

