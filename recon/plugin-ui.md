[Uploading plugin-ui.md…]()
# DSHM 插件机制 / UI 注入 / 生命周期侦查报告

> 范围：只读侦查 DSHM core `0.2.1-rc.2+dshm.73`（HarmonyOS 平板）。侦查时间受限，**未确认项已在 D 节末尾单列**；所有结论都对应本机真实文件:行号。
> 路径简写（下同）：
> - `CORE` = `/data/storage/el2/base/haps/entry/files/dsh/cores/0.2.1-rc.2+dshm.73`
> - `AI` = `CORE/node_modules/@deepseek-ai`
> - `HOME` = `/data/storage/el2/base/haps/entry/files/dsh/home`
> - `PROFILE` = `HOME/profiles/ondevice`
> - `RESCUE` = `PROFILE/node_modules/dshm-boot-rescue`（已装成功范式，纯宿主插件 + tapIndex 注入）

---

## A. 结论摘要（可直接指导备份/迁移工具实现）

1. **备份工具选型：纯宿主插件 + `webServer.tapIndex` 注入单文件客户端 IIFE 是唯一已验证可行的"不依赖客户端插件树"的 UI 通道**。UI 页由 Host 端插件在 index.html 渲染时字符串注入，`<script>` 在浏览器解析页面时执行；即使所有 cordis client 半身都 pending/失败，脚本仍会执行（失败页救援插件 dshm-boot-rescue 即靠此成立；其宿主代码 `RESCUE/lib/index.js:9,1720-1735`，客户端 `RESCUE/lib/rescue-client.js`）。
2. **插件必须"零静态 inject"**：`export const inject` 会因缺服务让整个 profile 进入 pending（救援插件注释 `RESCUE/lib/index.js:17-19`，核心 loader 行为见 `AI/cordis-plugin-loader/lib/index.js:631` 与 `AI/dsh-app-boot/lib/index.js:4425,4438`）。正确做法：`ctx.get("webServer")/ctx.get("loader")` 判空 + `ctx.on("internal/service", ...)` 等 webServer 就绪后重试（`RESCUE/lib/index.js:1789,1820-1841`；真实启动日志 `HOME/dshm-boot-rescue.log` 显示 loader:ok 后约 1.4s 才 "webServer：已获取"，随后 tapIndex 在 index 渲染时才注入）。
3. **可靠的"启动完成"时机（Host 侧）**：`AI/dsh-web-app/lib/index.js:171-176` 在 `get("loader").await()` 之后再做 `auditStartupEntries` 并确认 `webServer`/`connection` 存在，才宣告 ready（打印带 token 的 URL）。备份工具可用：`loader.await()`（`AI/cordis-plugin-loader/lib/index.js:154-160`）+ 监听 `internal/service` 拿到 webServer（`AI/cordis/lib/index.js:849` 发出 `("internal/service", name, impl)`）；**不存在**（未发现）名为 `ready`/`dispose` 的公开 cordis 生命周期事件，拆卸走 disposer/`ctx.effect`。
4. **插件包契约（最小可加载）**：package.json 需 `"type":"module"`、`"main"`/`"exports"` 指向 ESM 入口、`"dsh":{"bundle":{"patch":"./cordis.patch.yml"}}`；包内 bundle patch 用 `- insert: [{id, name}]` 注册自己（行 id 与包名一致，便于 `.dshm-plugin-rows.yml` 启停）；profile 的 `package.json` 把它写进 `dependencies`，`dsh.profile.bundles` 列出它（安装器会自动把新 bundle 依赖激活：`AI/dsh-app-boot/lib/index.js:1203-1226`）。核心侧契约解析：`AI/dsh-app-boot/lib/index.js:490-530,1024-1027`。
5. **UI 不选官方 client 半身（`dsh.client`）**：官方机制存在（`AI/dsh-client-modules/lib/index.js:40-69` 解析 package.json `dsh.client`；客户端用同源 `<script src>` 加载 bundle `AI/dsh-client-modules/lib/client.js:580-594`），但它是"client 插件树"的一部分，树 pending/失败时不会执行、失败页上也无法自救；`tapIndex` 是 Host 对每次 index 响应的 HTML 变换（`AI/dsh-host-webserver/lib/index.js:369-375,533-535`；`AI/dsh-host-frontend-static/lib/index.js:85`），在 client boot 之前执行，故选择 tapIndex。**结构化替代**是官方 `webserver/index-inject` 行（`AI/dsh-host-webserver/lib/index.js:29-57,522-526`，`kind:"script-src"` 等），比裸 HTML 更"官方"，但同样要求 Host 端插件先激活；两者可并存，建议优先 tapIndex（已验证）。
6. **异常/抢备信号**：失败页文案在客户端 Web bundle 中生成：`CORE/node_modules/@deepseek-ai/dsh-web-frontend/dist/assets/index-CN7NIWNw.js`（单行压缩文件，行号按 grep 为 :190）：字符偏移 ~914447 处抛 `web boot: N entries did not activate … <name>: pending (waiting for service: …)`；~915600 处渲染标题 `Failed to load plugins`。核心 CLI 侧同类信号：`AI/dsh-app-boot/lib/index.js:4425`（`warning: N entries did not activate`）、`:4438`（`startup failed: N required plugins did not activate`）、`:4479 auditStartupEntries()`。**能区分"Host 活着但插件软失败"**：失败页仍由 webServer + frontend-static 正常 HTTP 200 提供 ⇒ Host 进程/webServer 正常，只是 client 激活失败；Host 级不可达时 webServer 不响应，**任何插件都跑不了**（插件与 Host 同进程、同 loader）。运行期 Host 侧还可直接读 `ctx.loader.entries()` 的 `fiber.state`/`fiberPhase`（`AI/dsh-host-plugin-inventory/lib/index.js:112-157`，状态枚举 `:51-67`）。
7. **已安装插件名单/版本**：权威来源 = `PROFILE/package.json`（`dependencies` 里第三方包，含 `github:owner/repo[#ref]` 形式；`dsh.profile.bundles` 是激活顺序）+ 各包 `node_modules/<pkg>/package.json` 的 `version`（如 `RESCUE/package.json:3` = 1.0.0）。核心 bundle 与第三方区分：核心在 `CORE/dshm-core.json` 的 `plugins[]`（`id/name/bundle/disabled/nativeKind`）与 `CORE/node_modules/@deepseek-ai/*`；第三方在 profile `dependencies` + `PROFILE/node_modules/*`。`pluginInventory.list()` 只给 `entryId/moduleName/enabled/fiberPhase/meta`，**不含 version 字段**（`AI/dsh-host-plugin-inventory/lib/index.js:128-134`），版本要自己读 package.json；缺版本标 `unknown`。
8. **安全边界**：webServer 只接受具体 IP、拒绝 0.0.0.0 通配（`AI/dsh-host-webserver/lib/index.js:251-257`），端侧实际 `127.0.0.1:3120`（`HOME/host-ready.json` 的 `baseUrl`/`runtime.listenAddress`）；但 webserver 层**不做鉴权**，鉴权在 `connection.authorizeIndex`（`AI/dsh-host-frontend-static/lib/index.js:95`，connection 实现未定位）。写接口必须自己校验：仅回环 Host、`content-type: application/json`、`sec-fetch-site` 同源、`Origin` 与 Host 一致、body 上限（可照抄救援插件 `RESCUE/lib/index.js:1140-1166`、`MAX_BODY_BYTES` `:39`、读体上限 `:1212`）。插件是**同进程可信代码**，`node:fs` 无能力沙箱；落地路径限定 `HOME` 内 + 用户可见工作区 `/storage/Users/currentUser/Download/<包名>`（`HOME/host-ready.json` 的 `workspace` 字段即当前工作区）。

---

## B. 证据详情

### B1. 第三方 host 插件包契约

- `AI/dsh-app-boot/lib/index.js:490-495`：profile = manifest `dsh.profile.bundles`（有序）+ 用户 patch 层；bundle 用 `"dsh": { "bundle": { "patch": "./cordis.patch.yml" } }` 声明一个文件或有序文件列表，按 bundles 顺序叠加到空 entry 列表，最后应用 profile 的 `cordis.patch.yml`。
- 同文件 `:517-530`：`bundlePatchFiles()` 只接受 string 或 string[]，错类型抛 `dsh.bundle.patch must be a file path or a list of file paths`；`bundlePatchPaths()` = `join(packageDir, file)`。
- 同文件 `:1020-1027`：profile 每个 bundle 名先解析目录，再要求 `bundleManifest.dsh?.bundle` 存在，否则抛 `profile bundle ... declares no dsh.bundle in its package.json`；随后 `bundlePatchPaths(packageDir, bundle)`。
- 同文件 `:984-1000`：bundle 目录"先安装树、后 profile 目录"两锚点解析；解析失败抛 `cannot resolve profile bundle ...`。
- 同文件 `:3054-3060`（patch 语义 schema 描述）：`insert` 追加 entries（可指定 group id）；其他 patch 替换所给字段，**config 整块替换不做深合并**；`name` 为真值时只断言既有插件名而非改名；未知 target / 无 id 的非 insert patch 告警并跳过。
- 同文件 `:1203-1226`：包管理器操作成功后 `reconcileProfileBundles` 把新 bundle 依赖自动加进 `dsh.profile.bundles`（"New bundle dependencies activate automatically"）。
- 实机范例 package.json `RESCUE/package.json`：`:6 "type":"module"`、`:7 "main":"lib/index.js"`、`:10 "dependencies":{}`、`:21-26 "files"`（必须含入口与 patch；npm 打包用）、`:27-36 "exports"`（`.`→index.js，`./client`→rescue-client.js，`./cordis.patch.yml`、`./package.json`）、`:37-41 "dsh":{"bundle":{"patch":"./cordis.patch.yml"}}`、`:2 "name"`、`:3 "version"`。
- 实机范例 bundle patch `RESCUE/cordis.patch.yml:5-7`：
  ```yaml
  - insert:
      - id: dshm-boot-rescue
        name: 'dshm-boot-rescue'
  ```
  行 id 与包名一致；`.dshm-plugin-rows.yml` 用该 id 表达启停（`RESCUE/cordis.patch.yml:3-4` 注释）。
- profile 侧：`PROFILE/package.json` 的 `dependencies` 含 `dshm-boot-rescue: github:huangshou1/dshm-boot-rescue` 等第三方包（github: spec 形式已见两个：另有 `github:TonyKingWJX/dsh-thinking-bar#v1.0.5`，`#ref` 形式有实据；`&path:/子目录` 形式**本机未发现实例**，见 D 节未确认）。`dsh.profile.bundles` 末项即 `dshm-boot-rescue`，与核心 bundle（`@deepseek-ai/dsh-base`、`@deepseek-ai/dsh-web-app` 等）同列。
- 启停持久化：`PROFILE/.dshm-plugin-rows.yml` 实文件内容为
  ```yaml
  # [dshm-boot-rescue] disabled guard
  - id: guard
    disabled: true
  ```
  行数据即 loader entry 的 `id/disabled` 覆盖；`AI/cordis-plugin-loader/lib/index.js:336-344`（entry 或其父 disabled 即禁用）、`:373-375`（`update()` 时 disabled 直接 `fiber?.dispose()`）。**注意：该文件由 DSHM 应用壳（HarmonyOS 侧）在启动时重拼进 `cordis.patch.yml`，core 树内未找到 `dshm-plugin-rows` 实现**（grep 全量 `AI/**/*.js` 仅命中 `AI/dsh-client-ui-plugin-manager/lib/client.js:2364` 的 `data-plugin-rows` 组件属性，属巧合）——此点列 D 节未确认。

**最小可加载插件清单（伪代码）**：
```text
my-backup/
  package.json          # {name,version,type:"module",main:"lib/index.js",
                        #  exports:{".":{"default":"./lib/index.js"}},
                        #  files:[...], dsh:{bundle:{patch:"./cordis.patch.yml"}}}
  cordis.patch.yml      # - insert: [{id: my-backup, name: my-backup}]
  lib/index.js          # ESM: export const name/version; export function apply(ctx){...}
  lib/client.js         # 单文件 IIFE，仅 var/function，无 import/require
```
安装：`PROFILE/package.json` 的 `dependencies` 增加 `my-backup: github:owner/repo[#ref]`（或 semver），`dsh.profile.bundles` 加 `my-backup`（若安装器不自动加，见 `AI/dsh-app-boot/lib/index.js:1203-1226`）；**必须冷重启 DSHM 进程**（插件树只在启动时拼装，见 B2/B3）。

### B2. 加载与生命周期（core 源码）

- 导入：`AI/cordis-plugin-loader/lib/index.js:214-228` `Loader.import()`：`cordis:` 前缀走 builtins；有内部 Node loader 时 `internal.import(name, ctx.baseUrl, {})`；相对路径按 `ctx.baseUrl` 解析；否则动态 `import(name)`。第三方包名从 profile 目录锚点解析。
- 应用：`AI/cordis-plugin-loader/lib/index.js:437-465` `Entry._init()`：`await tree.import(name)` → `loader.unwrapExports(ns)`（`:666-672` 取 default/__esModule）→ `_patchContext` → `ctx.registry.plugin(plugin, config, stack)` 真正执行 `apply()`；import 失败只 `ctx.logger.error(error)` 且 entry 无 fiber（:453-456），**不抛给 Host**。
- 启停与 await：`:154-160` `EntryTree.await()` 等所有 entry 的 init/inertia 结束（boot ready 用）；`:590-650` `Loader` 服务，`:606` `ctx.reflect.provide("loader", this, ...)` ⇒ `ctx.get("loader")` 可用；`:647-650` `[Service.check]` 对 pending 任务返回 false（服务 pending 语义）。
- 生命周期事件（真实存在，来自 cordis 源码）：
  - `AI/cordis/lib/index.js:849`：`this.ctx.events.emit(self, "internal/service", name, this._getImpl(name,false)?.value)` —— 服务提供时广播，参数 `(serviceName, impl)`；救援插件据此等 webServer（`RESCUE/lib/index.js:1827-1838`）。
  - `AI/cordis/lib/index.js:970` 与 `:1093`：`internal/plugin`（plugin/fiber 生命周期内部事件）。`AI/cordis-plugin-loader/lib/index.js:325,522`：`loader/entry-init`；`:76,386,571`：`loader/partial-dispose`；`:356,526`：`loader/patch-context`；`:422`：`loader/volatile-update`。
  - webServer 专属：`AI/dsh-host-webserver/lib/index.js:522-526` `collectIndexInjections()` 每次 `renderIndex` 时 `ctx.emit("webserver/index-inject", table)`。
  - **未发现**名为 `ready` 或 `dispose` 的公开 cordis 事件（grep `AI/cordis/lib/index.js` 无 `'ready'`/`"ready"` 命中）；拆卸模型 = `ctx.effect(disposer)`/返回 disposer（`AI/cordis/lib/index.js:333,364-394`）。=> "启动完成"最可靠的可编程时机见 A3。
- 安装/更新前钩子：**未发现**（core 中无 beforeInstall/beforeUpdate 类钩子证据；`AI/dsh-plugin-manager/lib/index.js` 内 grep `github:|installQueue|beforeInstall` 均无命中）→ 列 D 节"不存在/未确认"。
- 进程内升级/重载：core 有 HMR/recompose 体系（`AI/dsh-app-boot/lib/index.js:3783-3885` 重放 patch 列表、`:3940-3943` 重启未激活 entries；`AI/dsh-hmr`），但 DSHM 端侧启停走"改 rows + 重开应用"，**不保证**页面内热重载会重拼 profile（救援 README:9-10,30-33 明确"只在进程启动时拼装"；本次未在 core 找到 DSHM 重拼代码，见 D 节）。

### B3. UI 注入链路（webServer / frontend-static / tapIndex）

- 服务定义与路由：`AI/dsh-host-webserver/lib/index.js:249` `class WebServer extends Service`；`:326-333` `register({kind:"exact"|"prefix", path, handler})`，重复 key 抛错；`:340-346` `registerUpgrade`；`:355-361` `registerFallback`（唯一）；`:369-375` `tapIndex(transform)` 纯 html→html 变换，注册顺序执行，返回 disposer；`:377-493` `[Service.init]` 创建 http(s) server 并在 `:466-476` `listen(config.port, config.host)`（失败 = FAILED fiber）；`:511-515` `applyIndexTaps`；`:522-526` 结构化注入表；`:533-535` `renderIndex(html) = applyIndexTaps(renderIndexInjections(html, collectIndexInjections()))`。
- HTML 注入渲染：`:29-57` `renderRow`（`global`/`script`/`script-src`/`script-preload`/`style`/`html` 行类型与 head/body placement）；`:59-61` splice；`:63-70` `READY_MARKUP` 尾脚本 settle `__DSH_BOOT_READY__`（客户端 boot entry 会 await 它）；`:79-98` `renderIndexInjections()`：head 行插 `<head>` 后，body 行插 `<body>` 后，最后追加 READY_MARKUP。
- dist 服务：`AI/dsh-host-frontend-static/lib/index.js:21` `inject=["webServer","connection"]`；`:81-97 apply`：`registerFallback`（`:87`）服务整个 SPA；index 响应 `renderIndex(await readFile(distIndex))` 并注入 `<base href="./">`（`:85`）；`:95` 先 `ctx.connection.authorizeIndex(req,res)` 再出 index；非 index 静态资源直接公开；路径穿越 403、非 GET/HEAD 405（`:51-55,88-92`）。
- 依赖装配：`AI/dsh-web-app/lib/index.js:29` `inject=["webServer"]`；`:139-148` apply 取 `ctx.webServer.host`、`ctx.provide("webApp")`、`ctx.plugin(FrontendStatic,{distIndex: internals.resolveDistIndex()})`；`:156-177` `ctx.inject(["connection"], ...)` 内 `const settled = connectionCtx.get("loader")?.await()` → `auditStartupEntries` → 确认 webServer+connection 后 `announceReady()`（`printUrl` 打印带认证 token 的 URL，`:160-162`）。DSHM 的 `host-ready.json`（`HOME/host-ready.json`：`url/baseUrl/port/profile/pid/workspace/runtime.listenAddress`）由应用壳写，非 core。
- 注入脚本加载时序（实机日志 `HOME/dshm-boot-rescue.log`）：`loader：ok`（apply 早期）→ `webServer：暂不可用（等待 internal/service 事件并重试）` → 约 1.4s 后 `webServer：已获取` → `路由注册：ok` → `tapIndex：注册成功` → 约 4s 后首次 `tapIndex：已向 index.html 注入救援脚本`（即第一次页面请求时）。⇒ **tapIndex 是请求时渲染，不是启动时一次性注入；插件可在启动完成后再注册也不会丢首屏**（前提是注册发生在首个 index 请求前；实测首请求前已完成）。
- 同源 fetch：注入脚本用相对路径 `/dshm-boot-rescue/*`（`RESCUE/lib/rescue-client.js:44-47` API 常量、`RESCUE/lib/index.js:1706-1710` 注册 `kind:"prefix"` 路由），与页面同源；index 鉴权由 `connection.authorizeIndex` 负责（实现未定位，见 D 节）；静态/路由是否要求 token 本次未实测（救援插件 README/`/health` 描述为可直接 GET，属插件作者声明）。**未发现需要 CSP nonce 的机制**；tapIndex 是纯字符串插入，测试页可自带 CSP。
- 官方 client 半身 vs tapIndex：官方机制 = package.json `dsh.client` 声明被 host 半身扫描（`AI/dsh-client-modules/lib/index.js:40-69`：`platform` 必填、`inject`/`external` 为字符串数组、`immediately` 布尔；`:136` bundle 缺失报错、`:150-152` 组合失败汇总），客户端 loader 用同源 classic `<script src>` 加载 bundle（`AI/dsh-client-modules/lib/client.js:580-594`），失败抛 `client-modules: bundle script <url> failed to load`（`:591`）或 `client-modules: <id> import failed (see console)`（`:503,552`），并把 failures 发布到 client 状态（`:511-514,566-569`）。**结论**：client 半身适合"正常 UI 扩展"（正式页面、设置页），但依赖 client 插件树成功激活；备份/救援工具需要"树坏了也能用"，故用 tapIndex。结构化注入（`webserver/index-inject` 的 `script-src` 行）与 tapIndex 等价安全，但注册 API 更窄（仅行类型），且仍需 Host 插件先激活；tapIndex 已在真机跑通，优先。
- 冷重启要求：插件启停 = profile patch 重拼（进程启动时），**必须划掉后台重开应用**；页面刷新/切前台无效（救援 README:9-10,30-33；`AI/dsh-app-boot/lib/index.js:4522 boot()` 只在进程启动调用；DSHM 壳未在 core 内）。

### B4. 异常/失败信号（core 中真实存在的位置）

| 信号 | 产生位置 | 可用于"抢备"吗 |
|---|---|---|
| `web boot: N entries did not activate` + 每项 `<name>: pending (waiting for service: …)` / `<name>: import failed …` | `AI/dsh-web-frontend/dist/assets/index-CN7NIWNw.js` 行 190，字符偏移 ≈914447（客户端 boot 激活检查抛错） | ✅ 页面文本可检测；说明 Host（webServer+frontend-static）活着，client 树软失败 |
| `Failed to load plugins`（失败页标题） | 同文件行 190，偏移 ≈915600（`Kn.failedTitle` 渲染失败卡） | ✅ 失败页文案识别 |
| `warning: N entries did not activate` / `startup failed: N required plugins did not activate` | `AI/dsh-app-boot/lib/index.js:4425` / `:4438`（`auditStartupEntries`，`:4479`） | ⚠️ 这是 CLI stdout 诊断，DSHM Host 的 stderr 未必看得到；逻辑同源 |
| `client-modules: bundle script <url> failed to load` | `AI/dsh-client-modules/lib/client.js:591` | ✅ 浏览器 console/失败列表 |
| `client-modules: <id> import failed (see console)` / `is waiting for activation` | `AI/dsh-client-modules/lib/client.js:503,552,556` | ✅ client 失败清单（`failures`，`:511-514,566-569`） |
| Host 侧 loader entry 状态（pending/loading/active/failed） | `AI/dsh-host-plugin-inventory/lib/index.js:112-157`（`readPluginInventory` 读 `ctx.loader.entries()`；状态枚举 `:51-67`：PENDING=0/LOADING=1/ACTIVE=2/FAILED=3） | ✅ 插件自身运行期可直接查 `ctx.loader.entries()`/`entry.fiber.state`，是最干净的"软失败"信号 |
| Host 不可达 | 无 HTTP 响应/端口关闭（`host-ready.json` 失效） | 边界：插件与 Host 同进程，Host 起不来则**没有任何插件能运行**（救援 README:301-302 亦声明此限制） |
| `.dshm-boot-failed` + rows 隔离 | `HOME/.dshm-boot-failed`（README:303-310 描述；本次未在 core 找到实现） | ❓ 未确认（DSHM 壳行为） |

补充：失败页是**客户端 React 错误态**（`Failed to load plugins` 卡在 `dsh-web-frontend` bundle 内），Host 不感知；因此"抢备"应同时用"页面文案侧"（注入客户端脚本）+ "Host 侧 loader 状态"两个面。

### B5. 已安装插件名单/版本来源

- 已装第三方（`PROFILE/package.json` `dependencies`）：`dsh-thinking-bar`（github:…#v1.0.5）、`dsh-whale-widget`、`dshmarket`、`cc-safety-net`、`dsh-cache-billing`（github:…）、`dsh-date-wrapper`（0.4.2）、`dsh-plugin-save-token`（2.4.1）、`dshm-boot-rescue`（github:huangshou1/dshm-boot-rescue）。激活顺序 = 同文件 `dsh.profile.bundles`（8 个 `@deepseek-ai/*` 核心 bundle + 8 个第三方）。
- 单包版本：`PROFILE/node_modules/<pkg>/package.json` 的 `version`（例：`RESCUE/package.json:3` = `1.0.0`；插件自身也读它，`RESCUE/lib/index.js:48-49` 导出 `version`）。
- 核心 bundle/插件清单：`CORE/dshm-core.json` 顶层 `coreVersion/platform/profile/builtAt/nodeFloor`，`plugins[]` 每项 `{id,name,bundle,disabled,nativeKind,nativeVia}`（已读前若干项；`DISABLED`/bundle 来源字段可用于区分核心内部行）。核心包物理位置 `CORE/node_modules/@deepseek-ai/*`（315 个目录/包）。
- 运行期 inventory：`AI/dsh-host-plugin-inventory/lib/index.js:112-157` → `{entryId, moduleName, enabled, fiberPhase, meta?}`；`meta` 来自 `ctx.get("pluginPackages")?.metaOf(name, base)`（`:123,127`），**无 version 字段**；服务名 `pluginInventory`（`:97`），`static inject=["loader"]`（`:95`）。web-app bundle patch 里挂载行 id `plugin-inventory`，注释"Read-only projection of current Loader entries for trusted client RPCs"。
- 设置页插件清单（UI）：`AI/dsh-client-ui-settings-plugin-inventory/lib/index.js:2-3` 仅注释（host 半身无逻辑），列表数据由 `lib/client.js` 组装（本次未细读 → D 节未确认；其数据源大概率是上面的 pluginInventory RPC + package meta）。
- 可靠获取"名称+版本"的推荐算法：① 读 `PROFILE/package.json` 的 `dependencies` → 得到已装插件名+spec；② 对每个名字解析 `PROFILE/node_modules/<name>/package.json`（注意 pnpm 符号链接/实际目录，见 `AI/dsh-app-boot/lib/index.js:647-661` 链接投影说明）取 `version`；③ 若包不在 profile 而由安装树提供（核心），到 `CORE/node_modules/@deepseek-ai/...` 取，并用 `CORE/dshm-core.json.plugins[]` 判定为"核心"；④ 目录/包缺失或解析失败 → `version:"unknown"`、`kind:"unknown"`；⑤ 区分核心 vs 第三方：名字前缀 `@deepseek-ai/` 或出现在 `dshm-core.json.plugins[]` ⇒ 核心；出现在 profile `dependencies` 且不在核心清单 ⇒ 第三方（github/tarball/semver spec 均可保留展示）。
- 启停态来源：`.dshm-plugin-rows.yml`（持久，行 id 非包名，需映射；实文件见 B1）+ `pluginInventory.enabled`（运行期）。

### B6. 宿主文件访问/安全

- webServer 绑定：`AI/dsh-host-webserver/lib/index.js:251-257` host 必须是具体 IPv4/IPv6 字面量，通配地址直接抛错（注释明确"binding every interface would expose remote code execution"）。端侧运行值：`HOME/host-ready.json` 的 `baseUrl = http://127.0.0.1:3120`、`runtime.listenAddress = 127.0.0.1:3120`（token 字段存在——**本报告不转录任何 token**）。
- 鉴权层次：webserver 本身不鉴权；index 响应前由 `connection.authorizeIndex`（`AI/dsh-host-frontend-static/lib/index.js:95`）认证；带 token URL 由 `dsh-web-app` 在 ready 后 `connection.authenticatedUrl(webUrl)` 打印（`AI/dsh-web-app/lib/index.js:156-162`）。connection 服务/包实现本次未定位（D 节）。
- 写接口校验范式（救援插件，实测跑通）：`RESCUE/lib/index.js:1140-1166` —— Host 头必须回环字面量（防 DNS rebinding，`:1143-1145`）、`content-type` 必须以 `application/json` 开头（`:1146-1149`）、`sec-fetch-site` 只能 `same-origin`/`none`（`:1150-1153`）、`Origin` 必须与 Host 一致（`:1154-1165`）；请求体上限 `MAX_BODY_BYTES = 32*1024`（`:39`）在 `:1212` 检查；整批 fail-closed、id 白名单/长度校验另在 `:1168+`。
- 插件 fs 权限：cordis loader 同进程加载第三方 ESM，**没有 node:fs 能力沙箱**（loader 代码无 fs 拦截；`AI/cordis-plugin-loader/lib/index.js:437-465` 直接 import+apply）。因此安全边界靠插件自律：救援插件把读写限定 `DSH_HOME`/`DSHM_HOME`（`RESCUE/lib/index.js:17-20` 注释、`:598` 环境变量定位、`:630` "任何读取都必须留在 DSH_HOME 内"、`:795` 依赖名禁 `..`/绝对路径）。备份工具应沿用：只写 `HOME/**`（如 `HOME/backups/…`）与用户可见工作区 `/storage/Users/currentUser/Download/<包名>/`（当前工作区实值见 `host-ready.json.workspace`）。
- `HOME` 内可写实测证据：救援诊断日志 `HOME/dshm-boot-rescue.log` 真实存在（22KB，含多轮启动记录），说明插件可在 `HOME` 写文件；profile 亦在其下。

### B7. 版本敏感点（我们依赖的内部面）

| 依赖面 | 证据 | 稳定性 | 替代成本 |
|---|---|---|---|
| `webServer.register({kind:"prefix",path,handler})` | `AI/dsh-host-webserver/lib/index.js:326-333` | 中（0.2.1-rc.2 内部 API，生命周期/签名可能变；真机已跑通） | 高：无别的 HTTP 路由入口（registerFallback 唯一且被 frontend-static 占） |
| `webServer.tapIndex(fn)` | 同文件 `:369-375` | 中 | 中：可退 `webserver/index-inject` 行（`:29-57`）或让客户端自行 fetch 资源；但失败页自愈能力会丢 |
| `renderIndex`/READY_MARKUP `__DSH_BOOT_READY__` | 同文件 `:63-98,533-535` | 中低（客户端 boot 协议） | 低：备份 UI 注入 script 自带 defer/ready 逻辑即可 |
| `ctx.get("webServer"|"loader")` + `internal/service` 事件 | `AI/cordis/lib/index.js:849`；loader `:606`；救援 `:1820-1841` | 中（cordis 4.0.5 契约） | 中：若事件改名，需轮询 `ctx.get`（救援已内置定时重试兜底 `:1789` 区域） |
| `loader.entries()`/`entry.fiber.state`/`fiberPhase` | loader `:142-148`；inventory `AI/dsh-host-plugin-inventory/lib/index.js:112-157,51-67` | 中 | 低：仅用于诊断/异常检测 |
| profile 路径约定（`DSH_HOME/profiles/<profile>/package.json`、`cordis.patch.yml`、`.dshm-plugin-rows.yml`） | `PROFILE/*` 实文件；loader/profile 逻辑 `AI/dsh-app-boot/lib/index.js:490-530,984-1027`；rows 语义见 B1 | 中（rows 由 DSHM 壳维护，未在 core） | 中：读名单/版本可只依赖 package.json + node_modules，不写 rows |
| bundle patch 契约 `dsh.bundle.patch`/`insert` 行 | `AI/dsh-app-boot/lib/index.js:517-530,3054-3060` | 高（核心装载契约） | 高 |
| 失败页文案（`Failed to load plugins`/`web boot: … did not activate`） | `AI/dsh-web-frontend/dist/assets/index-CN7NIWNw.js:190`（偏移 ≈915600/≈914447） | 低（文案/前端 bundle hash 会变；文件名为 hash） | 低：客户端脚本按正则多文案兜底（救援 `rescue-client.js:...` 已做），不硬编码单一 hash |
| `dsh.client` 官方 client 半身 | `AI/dsh-client-modules/lib/index.js:40-69`；client `:580-594` | 中低（尚未验证第三方端侧构建链） | 高（需构建产物+host 扫描+树激活），且失败页场景不可用 |

**"零 inject"可行性结论**：✅ 可行且已被真机验证。`RESCUE/lib/index.js` 无 `export const inject`（该文件导出仅 name/version/常量/纯函数/`apply`，`apply` 在 `:1739`；grep `export const inject` 无命中），全部服务通过 `ctx.get()` 判空 + `ctx.on("internal/service")` 重试 + 定时器兜底；`ctx.effect()` 注册清理（`:1750-1842`）。核心 pending 机制决定静态 inject 缺服务会进入 pending 并拖垮 web boot（`AI/cordis-plugin-loader/lib/index.js:631`，`AI/dsh-app-boot/lib/index.js:4425,4438`；救援注释 `RESCUE/lib/index.js:17-19`）⇒ **备份工具必须零静态 inject**。

---

## C. 最小插件骨架（伪代码）

```text
dshm-backup/
  package.json
  cordis.patch.yml
  lib/index.js        # host 半身
  lib/client.js       # 单文件 IIFE（var/function，禁 import/require/Worker/WASM；ArkWeb 兼容）
```

**package.json**
```json
{
  "name": "dshm-backup",
  "version": "0.1.0",
  "type": "module",
  "main": "lib/index.js",
  "exports": { ".": { "default": "./lib/index.js" },
               "./client": { "default": "./lib/client.js" },
               "./cordis.patch.yml": "./cordis.patch.yml",
               "./package.json": "./package.json" },
  "files": ["lib/index.js","lib/client.js","cordis.patch.yml","README.md"],
  "dsh": { "bundle": { "patch": "./cordis.patch.yml" } }
}
```

**cordis.patch.yml**
```yaml
- insert:
    - id: dshm-backup        # 行 id = 包名，便于 rows 启停
      name: 'dshm-backup'
```

**lib/index.js**
```js
import { readFile, writeFile, mkdir, readdir, stat } from "node:fs/promises";
import { join, resolve, relative } from "node:path";
export const name = "dshm-backup";
export const version = readVersion();            // 读自身 package.json
// 禁止 export const inject！

export function apply(ctx) {
  try {
    ctx.effect(() => {
      const cleanups = []; let timer = null; let attached = false;
      const tryAttach = () => {
        const ws = ctx.get?.("webServer");
        if (!ws || typeof ws.register !== "function") return false;
        cleanups.push(ws.register({ kind: "prefix", path: "/dshm-backup",
          handler: (req,res) => route(ctx, req, res) }));       // 读写接口（自校验见 B6）
        if (typeof ws.tapIndex === "function")
          cleanups.push(ws.tapIndex(html => inject(html, "/dshm-backup/client.js")));
        attached = true; return true;
      };
      if (!tryAttach()) {
        cleanups.push(ctx.on("internal/service", (n, impl) => {
          if (n === "webServer" && impl) tryAttach();
        }));
        timer = setInterval(() => { if (tryAttach()) clearInterval(timer); }, 1000);
      }
      return () => { for (const d of cleanups) try { d(); } catch {} ; clearInterval(timer); };
    }, "dshm-backup: routes and script");
  } catch (e) { /* 只记日志，绝不影响 Host 启动 */ }
}

function inject(html, src) {         // 仅插一次；失败页也能执行
  if (html.includes('data-dshm-backup')) return html;
  const tag = `<script data-dshm-backup src="${src}"></script>`;
  const i = html.search(/<\/body>/i);
  return i < 0 ? html + tag : html.slice(0,i) + tag + html.slice(i);
}
```
**lib/client.js**：IIFE；`fetch("/dshm-backup/state")` 同源；渲染悬浮面板；所有逻辑 try/catch、不 reload 页面、不轮询外部；只调用本插件的 Host 路由。
（备份动作本身：Host 端用 `node:fs` 打包 `HOME/sessions|storages|profiles` 等到 `HOME/backups/…` 或工作区；进度由 client 轮询；详见同项目其它侦查报告。）

---

## D. 可用钩子/异常信号清单

**真实存在、可直接用（有 core 证据）**
1. `ctx.get("loader")` → `loader.entries()` / `entry.fiber.state`（pending/active/failed）——Host 运行期软失败检测（`AI/cordis-plugin-loader/lib/index.js:590-650,142-148`；`AI/dsh-host-plugin-inventory/lib/index.js:51-67,112-157`）。
2. `ctx.get("loader").await()` —— 等所有插件条目初始化结束（`AI/cordis-plugin-loader/lib/index.js:154-160`；`AI/dsh-web-app/lib/index.js:171` 亦用）。
3. `ctx.on("internal/service", (name, impl) => …)` —— webServer 等服务的可用时机（`AI/cordis/lib/index.js:849`；实测救援插件 `RESCUE/lib/index.js:1827-1838`）。
4. `webServer.tapIndex(fn)`、`webServer.register({kind:"prefix"})` —— 已跑通（B3/B6）。
5. `webserver/index-inject` 结构化注入（`script-src` 等行）——官方替代注入面（`AI/dsh-host-webserver/lib/index.js:29-57,522-526`）。
6. `ctx.effect(disposer)` —— 插件拆卸/清理（`AI/cordis/lib/index.js:333,364-394`；救援 `RESCUE/lib/index.js:1750`）。
7. 浏览器侧失败信号文本：`Failed to load plugins` / `web boot: N entries did not activate` / `<name>: pending (waiting for service: …)` / `client-modules: bundle script … failed to load`（位置见 B4）。
8. `HOME/host-ready.json`（`baseUrl/port/profile/pid/workspace/runtime.listenAddress`，实际运行值）——外部观测 Host 是否就绪（写入方为 DSHM 壳，非 core）。

**确认不存在（本机 core 未发现）**
- 静态 `inject` 之外的"安装前/更新前钩子"：未发现（`AI/dsh-plugin-manager/lib/index.js` grep 无 `beforeInstall/github:` 等）。安装通道实现位置未定位。
- 名为 `ready` / `dispose` 的公开 cordis 生命周期事件：未发现（cordis 用 `internal/plugin`、`internal/service`、`internal/update` + disposer/effect）。
- core 内的 `.dshm-plugin-rows.yml` 重拼/隔离（`quarantineAfterBootFailure`、`.dshm-boot-failed`）：**core 树 grep 无命中**；实文件与日志只在 `PROFILE`/`HOME`，属 DSHM 应用壳（HarmonyOS 侧）行为，代码不可见。

**未确认（需要后续补证）**
- 安装标识 `github:owner/repo#branch&path:/子目录` 的 `&path:` 形式（本机 profile 只有 `#ref` 实例；解析代码未定位）。
- 插件安装队列/假壳 pnpm 的 Host 侧实现（`HOME/install-queue/` 目录存在，代码未定位）。
- `connection` 服务实现包（`authorizeIndex`/token 校验/Cookie 细节）——顶层 `AI` 下无 `dsh-connection`，可能在 `dsh-web` 或安装树别名，未展开。
- `dsh-client-ui-settings-plugin-inventory/lib/client.js` 的清单/版本数据源（仅确认 host 半身为空注释，`AI/dsh-client-ui-settings-plugin-inventory/lib/index.js:2-3`）。
- 客户端 `client-modules` failures 是否有 Host 侧可读投影（client 状态 `:511-514,566-569` 仅确认在浏览器侧发布）。
- `PROFILE/package.json`、`AI/dsh-web-app/cordis.patch.yml` 的行号未取（小文件读取工具在收尾时限内超时；文件内容与字段已核对）。
- `.dshm-plugin-rows.yml` 由"插件页重写并丢注释"（救援 README:36-37 声明）未独立验证。

---

## E. 风险与版本敏感点（结论）

1. **最高风险：内部 API 非契约**。`webServer.register/tapIndex/renderIndex`、`__DSH_BOOT_READY__`、`loader.entries/fiber.state`、`internal/service` 均为 `0.2.1-rc.2` developer preview 内部面（package.json 版本统一 0.2.1-rc.2，`AI/dsh-host-webserver/package.json`）。升级 core 后需回归：前缀路由是否仍匹配、tapIndex 是否仍被 frontend-static 的 renderIndex 调用、失败页文案是否变。
2. **失败页检测必须容忍文案/hash 变化**：`dsh-web-frontend/dist/assets/index-CN7NIWNw.js` 文件名带 hash、内容压缩单行；不要硬编码文件名/偏移，应按多正则匹配页面文本（救援客户端做法），并同时依赖 Host 侧 `loader.entries()` 状态做诊断。
3. **零 inject 是硬约束**：任何静态 inject 在缺服务时会让 web boot 整体 pending（失败页），备份工具等于自毁；必须沿用 `ctx.get` 判空 + `internal/service` 重试 + 定时兜底。
4. **冷重启语义**：插件启停/装配只在进程启动时生效；UI 上的"恢复/升级插件"必须提示划掉后台重开 DSHM，不能只刷新页面。
5. **写安全自担**：webServer 层无鉴权，端侧虽只绑 127.0.0.1（且拒绝通配），仍须按 B6 范式自校验 Host/Origin/sec-fetch-site/content-type/body 上限；token 不要写进任何日志/报告。
6. **文件落点**：备份产物优先写用户可见工作区 `/storage/Users/currentUser/Download/<包名>/`（便于用户取出）与 `HOME/backups/`（持久、插件可直接访问）；不要写 profile 的 `cordis.patch.yml`/`.dshm-plugin-rows.yml`（属 DSHM 壳管理，且每次启动重拼，写它不持久且有隔离风险）。
7. **版本敏感依赖清单**：见 B7 表；建议在插件内做 capability probe（注册/tapIndex 是否存在、`loader.entries` 是否可调用、`internal/service` 是否可监听），失败只降级打日志，绝不 throw 到 loader。
