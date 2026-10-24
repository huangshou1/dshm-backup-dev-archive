# dshm-backup — DSHM 备份与迁移工具（平板端交付）

> ⚠️ 本目录是**平板端开发交付**：只读侦查 + 适配 DSHM 的源码 + 合成测试 + 手机验收交接。
> **尚未在手机金丝雀机做真实安装/备份/恢复验收**；在手机验收完成、缺陷修复并经用户明确授权前，
> 不要把它安装到平板主力实例，也不要在平板上执行真实恢复。

DSHM（HarmonyOS 端 DeepSeek Harness）备份与迁移工具（插件 **v0.1.3**）：纯宿主 DSHM 插件 +
注入式 Web UI，零第三方依赖、零静态 `inject`。2026-10-24 外部源码复核与手机第一阶段 D1 修复后重新生成，
修复项与复验状态见 [FIX_REPORT.md](FIX_REPORT.md)；在本轮修复包通过再次源码审查/手机验收前，
不要把它当作正式可用版本。功能：

- **主动备份**：用户点“立即备份”，生成单个 ZIP（会话日志、工作区注册表、工作区产出文件、附件、
  插件名单/版本），写后重新读取逐文件 SHA-256 校验。
- **健康基线备份**：启动完成并延迟后自动做一次基线备份（有冷却/单实例锁/轮换；手动备份永不删除；
  新基线校验成功后才轮换旧基线）。
- **异常抢备**：Host 仍可运行、但检测到插件软失败（Host 侧 loader 条目失败/无 fiber，
  或浏览器侧插件加载失败页）时，自动/半自动做一次保命备份；Host 级不可达时无法备份（由基线兜底）。
- **恢复迁移**：选择备份 → 只读校验/预览 → 明确确认 → 解压到 staging 逐文件校验 → 写回
  sessions/workspace.json/工作区文件/附件 → 复验 → 提示**冷重启**；失败自动回滚。
- 首版只承诺**源端与目标端实际 DSHM 核心版本完全一致**（本次实测 `0.2.1-rc.2+dshm.73`）的迁移；
  版本不一致会明确警告并要求二次确认，格式版本不一致直接拒绝。

## 目录结构

```
dshm-backup-dev/                 # 项目根
├── README.md                    # 本文件（项目总览）
├── FEASIBILITY.md               # 只读侦查证据、DSHM 适配依据、能力边界、待手机验收项
├── TEST_REPORT.md               # 平板端已执行测试/静态检查/未执行项
├── HANDOFF_PHONE.md             # 手机金丝雀验收步骤（人工）
├── FIX_REPORT.md                # 外部源码复核退回修复报告（本轮）
├── PHONE_ACCEPTANCE_PROMPT.md   # 可直接粘贴到手机 DSHM 的验收提示词
├── dshm-backup/                 # 插件源码（安装/审查对象）
│   ├── package.json
│   ├── cordis.patch.yml
│   ├── lib/index.js             # 宿主入口：路由/注入/任务调度/安全校验/基线与抢备
│   ├── lib/backup.js            # 备份引擎（快照、清单、发布与校验）
│   ├── lib/restore.js           # 恢复引擎（预览、映射、写入、复验、回滚）
│   ├── lib/zip.js               # 零依赖 ZIP 读写（data descriptor + deflateRaw + 自写 CRC32）
│   ├── lib/format.js            # 纯规则：格式常量/清单校验/路径映射/workspace.json 改写
│   ├── lib/util.js              # 宿主工具：路径、稳定快照、zstd 帧校验、SHA-256、锁、遍历
│   ├── lib/client.js            # tapIndex 注入的单文件 ES5 UI
│   └── test/zip.test.mjs        # node:test（可选，本机无 node CLI 未执行）
├── test/run_tablet_tests.py     # 平板端合成测试 + 静态检查运行器（内嵌 CPython）
└── recon/                       # 平板只读侦查原始记录（证据来源）
```

## DSHM 端安装（手机验收时执行；平板不得安装本插件）

1. 先由用户把 `dshm-backup/`（或整个仓库）托管到可访问的 GitHub 仓库；**本任务未上传、未创建
   Token**（遵守用户约束）。
2. 在 DSHM 设置 → 插件 → 安装插件，输入以下形式之一（`github:` 语法已验证；`&path:` 子目录形式
   在现有 profile 中无实例，属“兼容性未确认”，见 FEASIBILITY §8）：
   - 仓库根即插件：`github:<owner>/<repo>#<branch>`
   - 插件在子目录：`github:<owner>/<repo>#<branch>&path:/dshm-backup`
3. 安装成功后**必须冷重启**：划掉后台，重新打开 DSHM 应用。页面刷新无效。
4. 重启后在页面右下角应出现「备份」悬浮按钮（正常页与插件加载失败页都会注入）。

> 平板端禁止：安装/启用/停用本插件（包括现在这版）、修改 `cordis.patch.yml` /
> `.dshm-plugin-rows.yml` / profile dependencies、执行真实恢复。

## 备份位置（用户可见 + 实际路径）

- **默认导出目录**：`<当前 DSHM 默认工作区>/dshm-backup/backups/`
  - 本机实测默认工作区：`/storage/Users/currentUser/Download/com.dshm.dshclient`
  - 因此本机为：`/storage/Users/currentUser/Download/com.dshm.dshclient/dshm-backup/backups/`
  - 这是普通文件系统绝对路径（不是 URI）；文件管理器“内部存储 → Download →
    com.dshm.dshclient → dshm-backup → backups”可见。
- 备份文件名：`dshm-backup-<manual|baseline|emergency|pre-restore>-<UTC时间>-<随机>.zip`
  + 同名 `.sha256` 旁文件；不覆盖旧文件。
- 若当前默认工作区不可用/回退到应用沙箱，插件会显示只读或降级原因（备份目录可见性在 UI 标注）。
- **卸载后是否保留**：该目录属于用户个人盘 `/storage/Users/currentUser/**`，不是应用沙箱
  `/data/storage/el2/base/**`；按 DSHM 代码注释，重装同包名可再获授权。但 HarmonyOS 真实卸载行为
  必须由手机金丝雀验收（见 HANDOFF_PHONE.md），平板不能替手机下结论。

## 备份内容与排除

包含：`sessions/**` 会话日志（v4，含对话记录/标题事件/工具记录；正在写入的会话会标记“不完整”）、
`storages/workspace.json`（工作区路径/标题/顺序/归档/置顶/会话归属）、工作区产出文件
（`/storage/Users/currentUser/Download/...` 下的工作区目录，排除依赖/版本库/缓存/备份自身）、
`attachments/**`（排除 tmp）、插件名单+版本（用户插件与核心插件，**仅名称+版本**；版本取不到写 unknown）、
manifest（格式版本、时间、来源核心版本各维度、逐文件 SHA-256、完整性状态、排除项、警告）。

默认排除：`.credentials.yaml`、`host-ready.json`（含 token）、`openai-gateway/api-key`、
`.anonymous-user-id`、缓存/临时/日志、插件目录/安装包/node_modules、会话投影缓存、锁文件。

> ⚠️ 备份 ZIP 内包含**明文聊天记录与工作区文件**（这是迁移目标本身）。请像对待敏感数据一样保管
> 备份包；不要上传到公共位置。工具不会把真实聊天正文写入日志/报告。

## 使用（手机验收时）

1. **立即备份**：右下角「备份」→ 面板 →「立即备份」。任务进度轮询显示；完成后显示监听位置、
   大小、包含内容、排除项、校验结果。文件写入后工具会重新读取并逐文件 SHA-256 校验，失败会删除
   不完整文件并报错。
2. **查看最近备份/基线**：面板显示“最近健康基线”的时间/位置/校验状态与备份列表（手动/基线/
   异常抢备/恢复前快照）。
3. **导入与恢复**：
   - 把 ZIP 放进上述 `backups/` 目录（文件管理器操作，不需要 PC/ADB）。
   - 面板 →「刷新」→ 选择备份 →「预览」：只读检查 ZIP 结构、manifest、逐文件 SHA-256、版本、
     目标是否全新、路径映射、冲突/警告、不完整会话（**预览阶段不写目标数据**）。
   - 「确认恢复」需二次点击；执行后显示恢复报告与醒目提示：**请划掉后台重新打开 DSHM 应用以加载
     恢复的数据；重启前不要新建会话**。
   - 目标不是全新实例时**默认拒绝覆盖**；恢复失败自动回滚（删除本次创建项、恢复目标原
     workspace.json），并如实报告回滚是否完全成功。
4. **校验选中备份**：对任意备份重新读取 manifest/逐文件 SHA-256/旁文件，显示是否通过。

## 已知限制（如实声明）

1. **真实端到端未在平板验证**：本机无 node CLI，无法执行插件 JS；已做真实 JS 解析（esprima）、
   AST 安全/适配检查、合成 fixture 参考测试（详见 TEST_REPORT.md）。外部复核曾用真实 node 执行
   ZIP 测试：修复前 13/18 失败 5 项，根因（CRC 在 deflate 之后）已修复；外部临时副本复测 18/18。
   正式修复包仍需外部真实 JS 测试与手机验收，平板不冒充已执行 JS。
2. **workspace.json 运行期写回风险**：workspace.json 是“内存权威 + 整文件原子替换”；恢复写回后
   必须立即冷重启，重启前新建会话可能被运行实例覆盖。插件用“恢复后提示 + 恢复前保存原文件 +
   失败回滚”降低风险，但无法彻底消除（除非 DSHM 未来提供官方导入 API）。
3. **不完整的活动会话**：备份时若会话仍在写入，最多可能丢最后约 200ms 已发布事件或末尾不完整帧；
   manifest 标 `complete:false`，恢复时默认跳过并在报告中列出（不会静默当完整）。
4. **系统文件选择/分享不可用**：纯宿主插件不能调系统 picker；导出/导入采用“用户可见目录 + 界面
   列表”。不依赖 PC、ADB、桌面浏览器。
5. **无后台服务**：备份/恢复必须前台完成；应用退出即停止，不存在“关掉应用继续跑”。
6. **单包 < 4 GiB**：零依赖 ZIP 未实现 ZIP64，超限会明确失败并提示，不会静默截断。
7. **首版只承诺同版本迁移**：跨核心版本只做兼容性检查与警告，不保证可用。
8. **插件只导出名单/版本**：导入恢复**不会**安装/启用/停用/卸载任何插件；恢复后如需插件需要用户
   自行重新安装。

## 开发与测试

- 平板端已执行：`test/run_tablet_tests.py`（DSHM 内嵌 CPython；esprima 真实 ESM/ES5 解析 +
  AST 检查 + 合成 fixture 参考测试 + 交付物检查）。结果与限制见 `TEST_REPORT.md`。
- 可选（非交付必要条件）：在任何有 Node 的环境运行 `node --test dshm-backup/test/zip.test.mjs`
  验证 ZIP 模块；这不是安装/备份/恢复的必要条件，真实行为以手机验收为准。
- 本机无 node CLI：**没有执行** `node --input-type=module --check` / `node --test`；也没有把 PC
  作为安装、运行、测试或恢复的必要条件。

## 验收状态

| 层级 | 状态 |
|---|---|
| 只读侦查（环境/数据/插件机制/运行时） | 已执行（平板，证据见 FEASIBILITY.md） |
| DSHM 适配的源码实现 | 已完成（纯宿主插件 + tapIndex 注入 UI，零依赖） |
| 静态语法/解析检查 | 已执行（esprima 真实解析 + AST 检查；`node --check` 未执行，无 node CLI） |
| 合成测试 | 已执行（Python 参考实现 + 合成 fixture；JS 逻辑未在平板执行） |
| 真实安装/加载/主动备份/恢复/卸载保留 | **待手机金丝雀验收** |
