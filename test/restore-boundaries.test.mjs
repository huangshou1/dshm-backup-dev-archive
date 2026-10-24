// dshm-backup v0.1.2 真实 JS 回归：回滚边界
//
// 运行（需真实 Node，外部审查环境执行；平板无 node CLI，未在本机执行）：
//   node --test test/restore-boundaries.test.mjs
//
// 覆盖本轮复审阻断问题 1：回滚不得递归删除恢复前已存在的 sessionDir；
// 既有 sessionDir 只删除本次新写日志；未跟踪文件必须保留；无法确认时不得报告完全回滚。

import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, rm, writeFile, access } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { rollbackRestore } from "../dshm-backup/lib/restore.js";

async function exists(path) {
  try {
    await access(path);
    return true;
  } catch (error) {
    return false;
  }
}

async function makeRoot() {
  const root = await mkdtemp(join(tmpdir(), "dshm-backup-rollback-"));
  const stateDir = join(root, "state");
  await mkdir(stateDir, { recursive: true });
  return {
    root,
    runtime: {
      paths: {
        home: join(root, "home"),
        stateDir,
        stagingRoot: join(stateDir, "staging"),
        workspaceRegistryPath: join(root, "workspace.json"),
      },
      logger: null,
    },
  };
}

function makeJournal(stagingDir, extra = {}) {
  var journal = {
    stagingDir,
    createdFiles: [],
    createdDirs: [],
    createdSessionDirs: [],
    registryExisted: false,
    registryText: null,
    registrySnapshotPath: null,
  };
  return Object.assign(journal, extra || {});
}

test("既有 sessionDir 中的无关文件在回滚后仍存在（不得递归删除）", async () => {
  const { root, runtime } = await makeRoot();
  try {
    const sessionDir = join(root, "sessions", "project", "session-existing");
    await mkdir(sessionDir, { recursive: true });
    const unrelated = join(sessionDir, "unrelated-user-file.txt");
    await writeFile(unrelated, "must survive rollback", "utf8");
    const logPath = join(sessionDir, "session.v4.jsonl.zstd");
    await writeFile(logPath, Buffer.from("restored-log-bytes"));
    const stagingDir = join(runtime.paths.stagingRoot, "job-1");
    await mkdir(stagingDir, { recursive: true });
    const journal = makeJournal(stagingDir, {
      createdFiles: [logPath],
      // 关键：既有目录不得进入 createdSessionDirs；只能删除本次写入的日志文件
      createdSessionDirs: [],
    });
    const result = await rollbackRestore(runtime, journal, []);
    assert.equal(result.rolledBack, true, JSON.stringify(result.failures));
    assert.equal(await exists(unrelated), true, "既有无关文件必须保留");
    assert.equal(await exists(logPath), false, "本次写入的日志文件应被删除");
    assert.equal(result.failures.length, 0);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("本次新建的 sessionDir 回滚时被非递归清理", async () => {
  const { root, runtime } = await makeRoot();
  try {
    const sessionDir = join(root, "sessions", "project", "session-new");
    await mkdir(sessionDir, { recursive: true });
    const logPath = join(sessionDir, "session.v4.jsonl.zstd");
    await writeFile(logPath, Buffer.from("new-log"));
    const stagingDir = join(runtime.paths.stagingRoot, "job-2");
    await mkdir(stagingDir, { recursive: true });
    const journal = makeJournal(stagingDir, {
      createdFiles: [logPath],
      createdSessionDirs: [sessionDir],
      createdDirs: [sessionDir],
    });
    const result = await rollbackRestore(runtime, journal, []);
    assert.equal(result.rolledBack, true, JSON.stringify(result.failures));
    assert.equal(await exists(sessionDir), false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("会话目录出现未跟踪文件时 rmdir 失败：保留数据且 rolledBack=false", async () => {
  const { root, runtime } = await makeRoot();
  try {
    const sessionDir = join(root, "sessions", "project", "session-untracked");
    await mkdir(sessionDir, { recursive: true });
    const logPath = join(sessionDir, "session.v4.jsonl.zstd");
    await writeFile(logPath, Buffer.from("new-log"));
    const untracked = join(sessionDir, "untracked.bin");
    await writeFile(untracked, Buffer.from("keep"));
    const stagingDir = join(runtime.paths.stagingRoot, "job-3");
    await mkdir(stagingDir, { recursive: true });
    const journal = makeJournal(stagingDir, {
      createdFiles: [logPath],
      createdSessionDirs: [sessionDir],
    });
    const result = await rollbackRestore(runtime, journal, []);
    assert.equal(result.rolledBack, false, "无法确认清理成功时不得报告完全回滚");
    assert.ok(result.failures.length > 0);
    assert.equal(await exists(untracked), true, "未跟踪文件必须保留");
    assert.equal(await exists(sessionDir), true);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("既有 sessionDir 场景下 workspace.json 恢复后重新读取比对成功", async () => {
  const { root, runtime } = await makeRoot();
  try {
    const original = JSON.stringify({ unit: { name: "workspace", version: 2 }, global: {}, tables: {} });
    await writeFile(runtime.paths.workspaceRegistryPath, "{broken", "utf8");
    const stagingDir = join(runtime.paths.stagingRoot, "job-4");
    await mkdir(stagingDir, { recursive: true });
    const journal = makeJournal(stagingDir, {
      registryExisted: true,
      registryText: original,
    });
    const result = await rollbackRestore(runtime, journal, []);
    assert.equal(result.rolledBack, true, JSON.stringify(result.failures));
    assert.equal(await readFile(runtime.paths.workspaceRegistryPath, "utf8"), original);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
