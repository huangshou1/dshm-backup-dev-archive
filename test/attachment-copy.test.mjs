// dshm-backup v0.1.3 真实 JS 回归：0o400 附件复制（D1 修复）
//
// 运行（需真实 Node，外部审查环境执行；平板无 node CLI，未在本机执行）：
//   node --test test/attachment-copy.test.mjs
//
// 背景：DSHM 附件被 dsh-attachment-local 有意 chmod 0o400（只读）。旧实现 copyFileAtomic
// 直接 copyFile 后 fsyncFile(tmp, "r+")，tmp 继承 0400 导致 EACCES，手机第一阶段备份 100% 失败。
// 本测试直接使用产品 copyFileAtomic 与 createBackup，覆盖：
//   1) 0400 源文件被复制成功，源 mode 不变，目标可写；
//   2) 实例内含 0400 附件时 manual 备份成功、重新校验通过、manifest 含附件且 SHA-256 正确。
// 会话 fixture 使用产品同样支持的明文 v4 会话，不依赖 zlib.zstdCompressSync 是否可用。

import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, rm, stat, writeFile, chmod } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import * as util from "../dshm-backup/lib/util.js";
import * as fmt from "../dshm-backup/lib/format.js";
import { createBackup, verifyBackupFile } from "../dshm-backup/lib/backup.js";
import { listZip, readZipEntry } from "../dshm-backup/lib/zip.js";

async function makeHome() {
  const root = await mkdtemp(join(tmpdir(), "dshm-backup-attachment-"));
  const home = join(root, "home");
  const workspace = join(root, "workspace");
  await mkdir(join(home, "sessions"), { recursive: true });
  await mkdir(join(home, "storages"), { recursive: true });
  await mkdir(workspace, { recursive: true });
  return { root, home, workspace };
}

function makeRuntime(home, workspace) {
  const paths = {
    home,
    profileName: "ondevice",
    profileDir: join(home, "profiles", "ondevice"),
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

async function writePlainSession(home, cwd, id) {
  const dir = join(home, "sessions", fmt.projectKey(cwd), fmt.encodeSegment(id));
  await mkdir(dir, { recursive: true });
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
  const bytes = Buffer.from(JSON.stringify(header) + "\n" + JSON.stringify(event) + "\n", "utf8");
  await writeFile(join(dir, "session.v4.jsonl"), bytes);
  return { dir, bytes };
}

test("copyFileAtomic 复制 0o400 源文件成功且不继承只读 mode", async () => {
  const root = await mkdtemp(join(tmpdir(), "dshm-backup-copy0400-"));
  try {
    const source = join(root, "attachment-0400.bin");
    const destination = join(root, "copied.bin");
    const payload = Buffer.from("attachment-bytes-synthetic");
    await writeFile(source, payload);
    await chmod(source, 0o400);
    await util.copyFileAtomic(source, destination);
    assert.deepEqual(await readFile(destination), payload);
    const srcStat = await stat(source);
    const dstStat = await stat(destination);
    assert.equal(srcStat.mode & 0o777, 0o400, "源文件 mode 不得被改变");
    assert.equal(dstStat.mode & 0o777, 0o600, "目标临时文件应显式 chmod 为可写 mode");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("实例内含 0o400 附件时 manual 备份成功并重新校验通过", async () => {
  const { root, home, workspace } = await makeHome();
  try {
    const sessionId = "session-dddddddd-dddd-4ddd-8ddd-dddddddddddd";
    await writePlainSession(home, workspace, sessionId);
    const attachmentsDir = join(home, "attachments", "v1", "files");
    await mkdir(attachmentsDir, { recursive: true });
    const attachmentName = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
    const attachmentPayload = Buffer.from("DSHM attachment content (synthetic)");
    const attachmentPath = join(attachmentsDir, attachmentName);
    await writeFile(attachmentPath, attachmentPayload);
    // 模拟 dsh-attachment-local 的只读附件
    await chmod(attachmentPath, 0o400);
    const runtime = makeRuntime(home, workspace);
    const report = await createBackup(runtime, "manual", {});
    assert.equal(report.ok, true, "含 0400 附件的实例备份应成功");
    const verify = await verifyBackupFile(runtime.paths, report.file);
    assert.equal(verify.ok, true, "备份重新校验应通过：" + verify.errors.join(","));
    const manifest = JSON.parse((await readZipEntry(report.file, "manifest.json")).toString("utf8"));
    assert.equal(manifest.contents.attachments.length, 1, "附件应进入备份清单");
    const attachment = manifest.contents.attachments[0];
    assert.equal(attachment.sha256, util.sha256Buffer(attachmentPayload));
    const listed = await listZip(report.file);
    assert.ok(listed.entries.some((entry) => entry.name === attachment.entry), "附件应进入 ZIP");
    assert.equal(manifest.source.platform === null || manifest.source.platform === undefined, true);
    assert.equal(manifest.source.coreVersion, "unknown");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
