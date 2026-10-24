// dshm-backup v0.1.2 真实 JS 回归：备份边界
//
// 运行（需真实 Node，外部审查环境执行；平板无 node CLI，未在本机执行）：
//   node --test test/backup-boundaries.test.mjs
//
// 覆盖本轮复审阻断问题：
//   2) coreDir 未确认（platform=null/nodeVersion=null）时 manual/baseline/emergency 仍能生成并校验备份；
//   3) header 损坏的会话以 complete=false 进入 ZIP/SHA-256，不阻断整份备份；
//   5) 不完整会话 ID 可从 registry 引用中清除，而真正空会话保留。
//
// 可移植性：若当前 Node 无 zlib.zstdCompressSync，自动使用产品同样支持的明文 v4 会话 fixture
// （session.v4.jsonl），并在运行日志中标注“plaintext fixture (no zstd API)”。

import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import zlib from "node:zlib";
import * as util from "../dshm-backup/lib/util.js";
import * as fmt from "../dshm-backup/lib/format.js";
import { createBackup, verifyBackupFile } from "../dshm-backup/lib/backup.js";
import { listZip, readZipEntry } from "../dshm-backup/lib/zip.js";

async function makeHome() {
  const root = await mkdtemp(join(tmpdir(), "dshm-backup-boundary-"));
  const home = join(root, "home");
  const workspace = join(root, "workspace");
  await mkdir(join(home, "sessions"), { recursive: true });
  await mkdir(join(home, "storages"), { recursive: true });
  await mkdir(join(home, "profiles", "ondevice"), { recursive: true });
  await mkdir(workspace, { recursive: true });
  await writeFile(join(workspace, "产出 文件.txt"), "workspace output", "utf8");
  return { root, home, workspace };
}

function makeRuntime(home, workspace) {
  const paths = {
    home,
    profileName: "ondevice",
    profileDir: join(home, "profiles", "ondevice"),
    // 关键：模拟 coreDir 无法确认（DSHM_CORE_DIR/argv 都取不到）
    coreDir: null,
    coreDirSource: "unconfirmed",
    sessionsDir: join(home, "sessions"),
    storagesDir: join(home, "storages"),
    workspaceRegistryPath: join(home, "storages", "workspace.json"),
    attachmentsDir: join(home, "attachments"),
    stateDir: join(home, "dshm-backup"),
    configPath: join(home, "dshm-backup", "config.json"),
    statePath: join(home, "dshm-backup", "state.json"),
    lockPath: join(home, "dshm-backup", "dshm-backup.lock"),
    logPath: join(home, "dshm-backup.log"),
    stagingRoot: join(home, "dshm-backup", "staging"),
    backupDir: join(home, "backups"),
    backupDirVisible: false,
    workspaceRoot: workspace,
    restoreFallbackBase: join(home, "restored-workspaces"),
    hostReady: { workspace },
  };
  const versions = util.collectVersionInfo(paths);
  return { paths, config: {}, versions, logger: util.createLogger(paths.logPath) };
}

// 测试可移植性：Node 20 等环境可能没有 zlib.zstdCompressSync。
// 产品同时支持明文 v4 会话（compression=none，文件名 session.v4.jsonl），
// 因此无 zstd API 时自动改用明文 fixture，并在测试名/日志中标注替代路径。
const HAS_ZSTD = typeof zlib.zstdCompressSync === "function";

async function writeSession(home, cwd, id, options = {}) {
  const dir = join(home, "sessions", fmt.projectKey(cwd), fmt.encodeSegment(id));
  await mkdir(dir, { recursive: true });
  let bytes;
  let fileName;
  if (options.corruptHeader) {
    if (HAS_ZSTD) {
      bytes = zlib.zstdCompressSync(Buffer.from("this-is-not-a-json-header\n", "utf8"));
      fileName = "session.v4.jsonl.zstd";
    } else {
      // 明文损坏 header：同样会被 product 识别为 complete=false
      bytes = Buffer.from("this-is-not-a-json-header\n", "utf8");
      fileName = "session.v4.jsonl";
    }
  } else {
    const header = {
      type: "session",
      version: 4,
      id,
      createdAt: Date.now(),
      isSeeded: false,
      delegationDepth: 0,
      cwd,
    };
    const event = { seq: 0, time: Date.now(), type: "message/user", data: { text: "synthetic" } };
    if (HAS_ZSTD) {
      bytes = Buffer.concat([
        zlib.zstdCompressSync(Buffer.from(JSON.stringify(header) + "\n", "utf8")),
        zlib.zstdCompressSync(Buffer.from(JSON.stringify(event) + "\n", "utf8")),
      ]);
      fileName = "session.v4.jsonl.zstd";
    } else {
      bytes = Buffer.from(JSON.stringify(header) + "\n" + JSON.stringify(event) + "\n", "utf8");
      fileName = "session.v4.jsonl";
    }
  }
  await writeFile(join(dir, fileName), bytes);
  return { dir, bytes, fileName };
}

async function writeRegistry(home, workspacePath, sessionIds) {
  const registry = {
    unit: { name: "workspace", version: 2 },
    global: { initialized: true, workspaceIds: ["w1"], archivedSessionIds: [], pinnedSessionIds: [] },
    tables: {
      workspaces: {
        w1: {
          path: workspacePath,
          title: "合成工作区",
          sessionIds: sessionIds.slice(),
          createdAt: "2026-01-01T00:00:00.000Z",
          updatedAt: "2026-01-01T00:00:00.000Z",
        },
      },
    },
  };
  await writeFile(join(home, "storages", "workspace.json"), JSON.stringify(registry, null, 2) + "\n", "utf8");
  return registry;
}

async function readManifest(zipPath) {
  const bytes = await readZipEntry(zipPath, "manifest.json");
  return JSON.parse(bytes.toString("utf8"));
}

test("coreDir 未确认时 manual/baseline/emergency 均能生成并重新校验备份", async () => {
  const { root, home, workspace } = await makeHome();
  try {
    const sessionId = "session-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    await writeSession(home, workspace, sessionId);
    await writeRegistry(home, workspace, [sessionId]);
    const runtime = makeRuntime(home, workspace);
    assert.equal(runtime.versions.coreVersion, "unknown");
    assert.ok(runtime.versions.platform === null || runtime.versions.platform === undefined);
    for (const kind of ["manual", "baseline", "emergency"]) {
      const report = await createBackup(runtime, kind, {});
      assert.equal(report.ok, true, kind + " 备份应成功");
      const verify = await verifyBackupFile(runtime.paths, report.file);
      assert.equal(verify.ok, true, kind + " 备份重新校验应通过：" + verify.errors.join(","));
      const manifest = await readManifest(report.file);
      const declaredPlatform = manifest.source.platform;
      assert.ok(declaredPlatform === null || declaredPlatform === undefined, "platform 允许缺失/null");
      assert.equal(manifest.source.coreVersion, "unknown");
      assert.ok(!manifest.source.coreVersion || manifest.source.coreVersion === "unknown");
    }
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("header 损坏会话以 complete=false 保留，不阻断 manual/emergency 备份", async () => {
  const { root, home, workspace } = await makeHome();
  try {
    const validId = "session-bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
    const brokenId = "session-cccccccc-cccc-4ccc-8ccc-cccccccccccc";
    await writeSession(home, workspace, validId);
    const broken = await writeSession(home, workspace, brokenId, { corruptHeader: true });
    await writeRegistry(home, workspace, [validId, brokenId]);
    const runtime = makeRuntime(home, workspace);
    for (const kind of ["manual", "emergency"]) {
      const report = await createBackup(runtime, kind, {});
      assert.equal(report.ok, true, kind + " 备份不应被损坏 header 阻断");
      const verify = await verifyBackupFile(runtime.paths, report.file);
      assert.equal(verify.ok, true, kind + " 备份重新校验应通过：" + verify.errors.join(","));
      const manifest = await readManifest(report.file);
      const sessions = manifest.contents.sessions;
      const valid = sessions.find((item) => item.id === validId);
      const brokenItem = sessions.find((item) => item.id === brokenId);
      assert.ok(valid, "有效会话应在清单中");
      assert.equal(valid.complete, true);
      assert.ok(brokenItem, "损坏 header 会话应作为不完整条目保留");
      assert.equal(brokenItem.complete, false);
      assert.equal(brokenItem.cwd, null);
      assert.equal(brokenItem.createdAt, null);
      assert.ok(typeof brokenItem.incompleteReason === "string" && brokenItem.incompleteReason.length > 0);
      // 原始字节进入 ZIP 与 SHA-256 校验范围
      const listed = await listZip(report.file);
      assert.ok(listed.entries.some((entry) => entry.name === brokenItem.file), "损坏会话文件应进入 ZIP");
      const shas = manifest.contents.sessions.map((item) => item.sha256);
      assert.equal(shas.length, 2);
    }
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("恢复跳过不完整会话时清除其 registry 引用，但保留真正空会话", async () => {
  const workspace = "/storage/Users/currentUser/Download/synthetic-workspace";
  const registry = {
    unit: { name: "workspace", version: 2 },
    global: {
      initialized: true,
      workspaceIds: ["w1"],
      archivedSessionIds: ["incomplete-id", "valid-id"],
      pinnedSessionIds: ["incomplete-id", "empty-id"],
    },
    tables: {
      workspaces: {
        w1: {
          path: workspace,
          title: "w",
          sessionIds: ["valid-id", "incomplete-id", "empty-id"],
          createdAt: "2026-01-01T00:00:00.000Z",
          updatedAt: "2026-01-01T00:00:00.000Z",
        },
      },
    },
  };
  const mapping = [{ from: workspace, to: workspace }];
  const rewritten = fmt.rewriteWorkspaceRegistry(registry, mapping, { skippedSessionIds: ["incomplete-id"] });
  assert.equal(rewritten.ok, true, rewritten.errors.join(","));
  assert.deepEqual(rewritten.registry.tables.workspaces.w1.sessionIds, ["valid-id", "empty-id"]);
  assert.deepEqual(rewritten.registry.global.archivedSessionIds, ["valid-id"]);
  assert.deepEqual(rewritten.registry.global.pinnedSessionIds, ["empty-id"]);
});
