// dshm-backup v0.1.7 真实 JS 回归：copyFileDirect 所有权与回滚边界
//
// 运行（需真实 Node，外部审查环境执行；平板无 node CLI，未在本机执行）：
//   node --test test/direct-write.test.mjs
//
// 覆盖阻断缺陷：目标已存在时不得删除/不得登记；预检后目标被其他过程创建时
// rollbackRestore 必须保留既有文件；源读取失败只清理本次创建的文件；正常直接写入成功。

import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import * as util from "../dshm-backup/lib/util.js";
import { rollbackRestore } from "../dshm-backup/lib/restore.js";

async function makeRoot() {
  const root = await mkdtemp(join(tmpdir(), "dshm-backup-direct-"));
  const stateDir = join(root, "state");
  await mkdir(stateDir, { recursive: true });
  const runtime = {
    paths: {
      home: join(root, "home"),
      stateDir,
      stagingRoot: join(stateDir, "staging"),
      workspaceRegistryPath: join(root, "workspace.json"),
    },
    logger: null,
  };
  return { root, runtime };
}

async function exists(path) {
  try {
    await stat(path);
    return true;
  } catch (error) {
    return false;
  }
}

test("目标已存在：copyFileDirect 抛 EEXIST，既有内容/SHA 不变，onCreated 不触发", async () => {
  const { root } = await makeRoot();
  try {
    const source = join(root, "source.txt");
    const destination = join(root, "target", "existing.txt");
    await mkdir(join(root, "target"), { recursive: true });
    await writeFile(source, "new-content", "utf8");
    await writeFile(destination, "preexisting-content", "utf8");
    const before = await util.sha256File(destination);
    let createdCalls = 0;
    await assert.rejects(
      util.copyFileDirect(source, destination, { onCreated: function () { createdCalls += 1; } }),
      function (error) { return error && error.code === "EEXIST"; }
    );
    assert.equal(createdCalls, 0, "EEXIST 时绝不能登记为本次创建");
    assert.equal(await readFile(destination, "utf8"), "preexisting-content");
    assert.equal(await util.sha256File(destination), before);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("预检后目标被其他过程创建：journal 不登记，rollbackRestore 保留既有文件", async () => {
  const { root, runtime } = await makeRoot();
  try {
    const source = join(root, "source.txt");
    const destination = join(root, "workspace", "late-created.txt");
    await mkdir(join(root, "workspace"), { recursive: true });
    await writeFile(source, "new-content", "utf8");
    // 模拟预览后、恢复写入前被其他过程创建
    await writeFile(destination, "created-by-someone-else", "utf8");
    const before = await util.sha256File(destination);
    const journal = {
      stagingDir: join(runtime.paths.stagingRoot, "job"),
      createdFiles: [],
      createdDirs: [],
      createdSessionDirs: [],
      registryExisted: false,
      registryText: null,
      registrySnapshotPath: null,
    };
    await mkdir(journal.stagingDir, { recursive: true });
    await assert.rejects(
      util.copyFileDirect(source, destination, {
        onCreated: function (createdPath) { journal.createdFiles.push(createdPath); },
      }),
      function (error) { return error && error.code === "EEXIST"; }
    );
    assert.equal(journal.createdFiles.length, 0, "既有文件不得进入回滚 journal");
    const rollback = await rollbackRestore(runtime, journal, []);
    assert.equal(rollback.rolledBack, true, JSON.stringify(rollback.failures));
    assert.equal(await exists(destination), true, "既有文件必须保留");
    assert.equal(await readFile(destination, "utf8"), "created-by-someone-else");
    assert.equal(await util.sha256File(destination), before);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("目标不存在且正常写入：onCreated 一次，内容与源一致", async () => {
  const { root } = await makeRoot();
  try {
    const source = join(root, "source.txt");
    const destination = join(root, "target", "nested", "new.txt");
    await mkdir(join(root, "target"), { recursive: true });
    const payload = Buffer.from("direct-write-normal", "utf8");
    await writeFile(source, payload);
    const created = [];
    await util.copyFileDirect(source, destination, {
      onCreated: function (createdPath) { created.push(createdPath); },
    });
    assert.equal(created.length, 1);
    assert.equal(created[0], destination);
    assert.deepEqual(await readFile(destination), payload);
    const info = await stat(destination);
    assert.equal(info.mode & 0o777, 0o600);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("目标不存在、源读取失败：只清理本次创建的文件，journal 保留跟踪", async () => {
  const { root } = await makeRoot();
  try {
    const missingSource = join(root, "does-not-exist.txt");
    const destination = join(root, "target", "partial.txt");
    await mkdir(join(root, "target"), { recursive: true });
    const tracked = [];
    await assert.rejects(
      util.copyFileDirect(missingSource, destination, {
        onCreated: function (createdPath) { tracked.push(createdPath); },
      }),
      function (error) { return error && (error.code === "ENOENT" || error.code === "EISDIR"); }
    );
    assert.equal(tracked.length, 1, "创建成功后必须先登记");
    assert.equal(await exists(destination), false, "本次创建的部分文件应被清理");
    assert.equal(tracked[0], destination, "journal 跟踪不得因清理成功而丢失（外层可幂等清理）");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("回滚清理失败时保留路径并报告 rolledBack=false", async () => {
  const { root, runtime } = await makeRoot();
  try {
    // 用一个非空目录冒充 journal 中无法删除的“文件”，模拟清理失败
    const cannotDelete = join(root, "cannot-delete");
    await mkdir(cannotDelete, { recursive: true });
    await writeFile(join(cannotDelete, "child.bin"), "keep");
    const journal = {
      stagingDir: join(runtime.paths.stagingRoot, "job2"),
      createdFiles: [cannotDelete],
      createdDirs: [],
      createdSessionDirs: [],
      registryExisted: false,
      registryText: null,
      registrySnapshotPath: null,
    };
    await mkdir(journal.stagingDir, { recursive: true });
    const rollback = await rollbackRestore(runtime, journal, []);
    assert.equal(rollback.rolledBack, false, "清理失败不得报告完全回滚");
    assert.ok(rollback.failures.length > 0);
    assert.equal(await exists(join(cannotDelete, "child.bin")), true);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
