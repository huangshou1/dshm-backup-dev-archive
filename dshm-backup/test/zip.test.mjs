/**
 * DSHM 备份插件 ZIP 模块测试（node:test + node:assert/strict）
 *
 * 运行方式：node --test test/zip.test.mjs（需要 Node >= 20；本插件宿主为 Node v24）
 *
 * 重要声明：开发机是 HarmonyOS 平板 DSHM 宿主（Node v24.2.0 jitless、无 WASM、
 *   没有 node CLI），本测试文件与 lib/zip.js 均未在本机执行过；
 *   交付后由 lead 做静态检查，测试留待手机 / 有 node 的环境验收。
 *
 * 覆盖点：
 *   - 中文 / 空格 / 嵌套目录名、空文件、无扩展名、store + deflate 混合
 *   - CRC32 标准测试向量
 *   - CRC 篡改检测（extractZip / readZipEntry / hashZipEntries）
 *   - 路径穿越拒绝（../、绝对路径、反斜杠、盘符、重复名）——写出端 + 读取端
 *   - 目标已存在拒绝（不覆盖已有备份）与临时文件清理
 *   - 解压后逐文件 SHA-256 一致、expectedHashes 校验
 *   - listZip 字段正确、readZipEntry 内容正确
 *   - hashZipEntries 与 writeZip 产物逐文件对照
 *   - ZIP_LIMIT 上限、destDir 约束、空 ZIP
 *   - 大文件流式（5 MiB 随机数据）
 */

import test, { after, before } from 'node:test';
import assert from 'node:assert/strict';
import { createHash, randomBytes } from 'node:crypto';
import {
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  rm,
  stat,
  writeFile,
} from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import {
  ZIP_LIMIT,
  crc32,
  extractZip,
  hashZipEntries,
  listZip,
  readZipEntry,
  writeZip,
} from '../lib/zip.js';

const EMPTY_SHA256 = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855';
const sha256 = (buffer) => createHash('sha256').update(buffer).digest('hex');

let root;
let dirCounter = 0;

before(async () => {
  root = await mkdtemp(join(tmpdir(), 'dshm-zip-test-'));
});

after(async () => {
  if (root) await rm(root, { recursive: true, force: true });
});

/** 每个用例一个独立子目录，避免互相干扰。 */
async function freshDir(label) {
  dirCounter += 1;
  const dir = join(root, `${String(dirCounter).padStart(2, '0')}-${label}`);
  await mkdir(dir, { recursive: true });
  return dir;
}

/** 断言 promise 以指定 code 的 ZIP 错误拒绝。 */
async function expectZipError(promise, code) {
  await assert.rejects(promise, (err) => {
    assert.equal(
      err && err.code,
      code,
      `期望错误码 ${code}，实际 ${err && err.code}（${err && err.message}）`,
    );
    return true;
  });
}

/** 路径是否存在。 */
async function exists(target) {
  try {
    await stat(target);
    return true;
  } catch (err) {
    if (err && err.code === 'ENOENT') return false;
    throw err;
  }
}

/**
 * 手工构造最小 ZIP（store 条目），用于向读取端注入不安全 / 不支持的字段
 * （writeZip 会拒绝这些名字，所以必须绕过写端直接造字节）。
 *
 * @param {Array<{
 *   name: string,
 *   data?: Buffer,
 *   method?: number,
 *   flags?: number,
 *   externalAttrs?: number,
 *   crc32?: number
 * }>} entries
 * @returns {Buffer}
 */
function buildRawZip(entries) {
  const localParts = [];
  const centralParts = [];
  let offset = 0;

  for (const entry of entries) {
    const nameBytes = Buffer.from(entry.name, 'utf8');
    const data = entry.data ? Buffer.from(entry.data) : Buffer.alloc(0);
    const method = entry.method === undefined ? 0 : entry.method;
    const flags = entry.flags === undefined ? 0x0800 : entry.flags;
    const crcValue = entry.crc32 === undefined ? crc32(data) : entry.crc32;
    const externalAttrs =
      entry.externalAttrs === undefined ? (0o100644 << 16) >>> 0 : entry.externalAttrs >>> 0;
    const dosTime = 0;
    const dosDate = 0x21; // 1980-01-01

    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(flags, 6);
    local.writeUInt16LE(method, 8);
    local.writeUInt16LE(dosTime, 10);
    local.writeUInt16LE(dosDate, 12);
    local.writeUInt32LE(crcValue >>> 0, 14);
    local.writeUInt32LE(data.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(nameBytes.length, 26);
    local.writeUInt16LE(0, 28);
    localParts.push(local, nameBytes, data);

    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE(0x0314, 4);
    central.writeUInt16LE(20, 6);
    central.writeUInt16LE(flags, 8);
    central.writeUInt16LE(method, 10);
    central.writeUInt16LE(dosTime, 12);
    central.writeUInt16LE(dosDate, 14);
    central.writeUInt32LE(crcValue >>> 0, 16);
    central.writeUInt32LE(data.length, 20);
    central.writeUInt32LE(data.length, 24);
    central.writeUInt16LE(nameBytes.length, 28);
    central.writeUInt16LE(0, 30);
    central.writeUInt16LE(0, 32);
    central.writeUInt16LE(0, 34);
    central.writeUInt16LE(0, 36);
    central.writeUInt32LE(externalAttrs, 38);
    central.writeUInt32LE(offset, 42);
    centralParts.push(central, nameBytes);

    offset += 30 + nameBytes.length + data.length;
  }

  const centralBuffer = Buffer.concat(centralParts);
  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0);
  eocd.writeUInt16LE(entries.length, 8);
  eocd.writeUInt16LE(entries.length, 10);
  eocd.writeUInt32LE(centralBuffer.length, 12);
  eocd.writeUInt32LE(offset, 16);

  return Buffer.concat([...localParts, centralBuffer, eocd]);
}

/** 把手工构造的 ZIP 落盘。 */
async function writeRawZip(dir, fileName, entries) {
  const zipPath = join(dir, fileName);
  await writeFile(zipPath, buildRawZip(entries));
  return zipPath;
}

// ---------------------------------------------------------------------------
// 基础
// ---------------------------------------------------------------------------

test('ZIP_LIMIT 与 API 契约一致', () => {
  assert.deepEqual(ZIP_LIMIT, {
    maxEntries: 65534,
    maxTotalBytes: 0xffffffff - 1024,
    maxSingleEntryBytes: 0xffffffff - 1024,
  });
  assert.equal(Object.isFrozen(ZIP_LIMIT), true);
});

test('crc32 标准测试向量', () => {
  assert.equal(crc32(Buffer.from('123456789')), 0xcbf43926);
  assert.equal(crc32(Buffer.from('The quick brown fox jumps over the lazy dog')), 0x414fa339);
  assert.equal(crc32(Buffer.alloc(0)), 0);
});

// ---------------------------------------------------------------------------
// 端到端：写 → 列 → 读单条 → 解压
// ---------------------------------------------------------------------------

test('端到端：中文/空格/嵌套目录/空文件/无扩展名/store+deflate/目录条目', async () => {
  const dir = await freshDir('roundtrip');
  const zipPath = join(dir, 'backup.zip');
  const manifest = Buffer.from(
    JSON.stringify({ version: 1, entries: ['中文 目录/文件 名.txt', 'noext'] }),
    'utf8',
  );
  const chinese = Buffer.from('你好，DSHM 备份！', 'utf8');
  const payload = Buffer.from('abc', 'utf8');
  const entries = [
    { name: 'manifest.json', source: { type: 'buffer', data: manifest }, compress: 'deflate' },
    { name: '中文 目录/文件 名.txt', source: { type: 'buffer', data: chinese }, compress: 'store' },
    {
      name: 'nested/empty.bin',
      source: { type: 'buffer', data: Buffer.alloc(0) },
      compress: 'deflate',
    },
    { name: 'nested/' },
    { name: 'noext', source: { type: 'buffer', data: payload }, compress: 'store' },
  ];

  const result = await writeZip(zipPath, entries);
  assert.equal(result.entries, entries.length);
  const zipBytes = await readFile(zipPath);
  assert.equal(result.size, zipBytes.length);
  assert.equal((await stat(zipPath)).size, zipBytes.length);
  assert.match(result.sha256, /^[0-9a-f]{64}$/);
  assert.equal(result.sha256, sha256(zipBytes));
  assert.equal(
    (await readdir(dir)).filter((name) => name.includes('.tmp-')).length,
    0,
    '成功后不应残留临时文件',
  );

  const listed = await listZip(zipPath);
  const listedByName = new Map(listed.entries.map((item) => [item.name, item]));
  assert.deepEqual(
    [...listedByName.keys()].sort(),
    entries.map((entry) => entry.name).sort(),
  );
  const manifestEntry = listedByName.get('manifest.json');
  assert.equal(manifestEntry.size, manifest.length);
  assert.equal(manifestEntry.method, 8);
  assert.equal(manifestEntry.compressedSize > 0, true);
  assert.equal(manifestEntry.crc32, crc32(manifest));
  assert.equal(manifestEntry.isDirectory, false);
  assert.equal(manifestEntry.isSymlink, false);
  const chineseEntry = listedByName.get('中文 目录/文件 名.txt');
  assert.equal(chineseEntry.method, 0);
  assert.equal(chineseEntry.size, chinese.length);
  assert.equal(chineseEntry.compressedSize, chinese.length);
  assert.equal(listedByName.get('nested/empty.bin').size, 0);
  assert.equal(listedByName.get('nested/').isDirectory, true);
  assert.equal(listedByName.get('noext').method, 0);
  assert.equal(listed.totalSize, manifest.length + chinese.length + payload.length);

  assert.deepEqual(await readZipEntry(zipPath, 'manifest.json'), manifest);
  assert.deepEqual(await readZipEntry(zipPath, '中文 目录/文件 名.txt'), chinese);
  assert.deepEqual(await readZipEntry(zipPath, 'nested/empty.bin'), Buffer.alloc(0));
  await expectZipError(readZipEntry(zipPath, 'missing.json'), 'ZIP_NOT_FOUND');

  // destDir 不存在 → 自动创建
  const destDir = join(dir, 'restore');
  const extracted = await extractZip(zipPath, destDir);
  assert.equal(extracted.totalSize, listed.totalSize);
  assert.equal(extracted.files.length, 4);
  for (const fileEntry of extracted.files) {
    assert.match(fileEntry.sha256, /^[0-9a-f]{64}$/);
    assert.equal(fileEntry.crc32Ok, true);
  }
  const extractedByName = new Map(extracted.files.map((item) => [item.name, item]));
  assert.equal(extractedByName.get('manifest.json').size, manifest.length);
  assert.equal(extractedByName.get('manifest.json').sha256, sha256(manifest));
  assert.equal((await readFile(join(destDir, 'manifest.json'))).equals(manifest), true);
  assert.equal(
    (await readFile(join(destDir, '中文 目录', '文件 名.txt'))).equals(chinese),
    true,
  );
  assert.equal((await stat(join(destDir, 'nested'))).isDirectory(), true);
  assert.equal((await stat(join(destDir, 'nested', 'empty.bin'))).size, 0);
  assert.equal((await readFile(join(destDir, 'noext'))).equals(payload), true);
  assert.equal((await readdir(destDir)).includes('中文 目录'), true);
});

// ---------------------------------------------------------------------------
// hashZipEntries
// ---------------------------------------------------------------------------

test('hashZipEntries 与 writeZip 产物逐文件对照', async () => {
  const dir = await freshDir('hashes');
  const zipPath = join(dir, 'hash.zip');
  const files = [
    ['a.txt', Buffer.from('hello')],
    ['中文/空格 名.txt', Buffer.from('你好', 'utf8')],
    ['empty.dat', Buffer.alloc(0)],
  ];
  await writeZip(
    zipPath,
    files.map(([name, data]) => ({ name, source: { type: 'buffer', data } })),
  );

  const hashed = await hashZipEntries(zipPath);
  assert.equal(hashed.entries.length, files.length);
  const byName = new Map(hashed.entries.map((item) => [item.name, item]));
  for (const [name, data] of files) {
    const item = byName.get(name);
    assert.ok(item, `缺少条目 ${name}`);
    assert.equal(item.size, data.length);
    assert.equal(item.sha256, sha256(data));
  }
  assert.equal(byName.get('empty.dat').sha256, EMPTY_SHA256);
  assert.equal(
    hashed.totalSize,
    files.reduce((sum, [, data]) => sum + data.length, 0),
  );

  // 目录条目不出现在 hashZipEntries 结果中（其 SHA-256 无意义）
  const dirZip = join(dir, 'with-dir.zip');
  await writeZip(dirZip, [
    { name: 'dir/' },
    { name: 'dir/x.txt', source: { type: 'buffer', data: Buffer.from('x') } },
  ]);
  const dirHashed = await hashZipEntries(dirZip);
  assert.deepEqual(
    dirHashed.entries.map((item) => item.name),
    ['dir/x.txt'],
  );
});

test('hashZipEntries 拒绝损坏条目', async () => {
  const dir = await freshDir('hash-tamper');
  const zipPath = join(dir, 'store.zip');
  const marker = Buffer.from('HASH-TAMPER-PAYLOAD-0123456789');
  await writeZip(zipPath, [
    { name: 'payload.bin', source: { type: 'buffer', data: marker }, compress: 'store' },
  ]);
  const bytes = await readFile(zipPath);
  const index = bytes.indexOf(marker);
  assert.ok(index >= 0, '未在 zip 中找到 store 模式的明文载荷');
  bytes[index] ^= 0xff;
  const tampered = join(dir, 'tampered.zip');
  await writeFile(tampered, bytes);
  await expectZipError(hashZipEntries(tampered), 'ZIP_CRC_MISMATCH');
});

// ---------------------------------------------------------------------------
// 写出端安全校验
// ---------------------------------------------------------------------------

test('writeZip 拒绝非法条目名（路径穿越/绝对路径/反斜杠/盘符/空段/超长）', async () => {
  const dir = await freshDir('badnames-write');
  const zipPath = join(dir, 'bad.zip');
  const bufferSource = () => ({ type: 'buffer', data: Buffer.from('x') });
  const badNames = [
    '../evil.txt',
    '/abs.txt',
    'a\\b.txt',
    'C:evil.txt',
    'a//b.txt',
    './x.txt',
    'a/../b.txt',
    'dir/./x.txt',
    '',
  ];
  for (const name of badNames) {
    // eslint-disable-next-line no-await-in-loop
    await expectZipError(writeZip(zipPath, [{ name, source: bufferSource() }]), 'ZIP_PATH_UNSAFE');
  }

  const longSegment = 'a'.repeat(256);
  await expectZipError(
    writeZip(zipPath, [{ name: longSegment, source: bufferSource() }]),
    'ZIP_PATH_UNSAFE',
  );
  const longName = Array.from({ length: 5 }, () => 'b'.repeat(250)).join('/');
  await expectZipError(
    writeZip(zipPath, [{ name: longName, source: bufferSource() }]),
    'ZIP_PATH_UNSAFE',
  );

  assert.equal(await exists(zipPath), false);
  assert.equal((await readdir(dir)).length, 0, '校验失败不应留下临时文件');
});

test('writeZip 拒绝重复名与文件/目录路径冲突', async () => {
  const dir = await freshDir('dupes-write');
  const zipPath = join(dir, 'dup.zip');
  const bufferSource = () => ({ type: 'buffer', data: Buffer.from('x') });

  await expectZipError(
    writeZip(zipPath, [
      { name: 'same.txt', source: bufferSource() },
      { name: 'same.txt', source: bufferSource() },
    ]),
    'ZIP_DUPLICATE',
  );
  await expectZipError(
    writeZip(zipPath, [
      { name: 'a', source: bufferSource() },
      { name: 'a/b.txt', source: bufferSource() },
    ]),
    'ZIP_PATH_UNSAFE',
  );
  await expectZipError(
    writeZip(zipPath, [{ name: 'a/' }, { name: 'a', source: bufferSource() }]),
    'ZIP_DUPLICATE',
  );
  assert.equal(await exists(zipPath), false);
});

test('writeZip 拒绝覆盖已存在目标并清理临时文件', async () => {
  const dir = await freshDir('target-exists');
  const zipPath = join(dir, 'backup.zip');
  const first = await writeZip(zipPath, [
    { name: 'a.txt', source: { type: 'buffer', data: Buffer.from('v1') } },
  ]);
  const firstBytes = await readFile(zipPath);

  await expectZipError(
    writeZip(zipPath, [{ name: 'a.txt', source: { type: 'buffer', data: Buffer.from('v2') } }]),
    'ZIP_TARGET_EXISTS',
  );

  const secondBytes = await readFile(zipPath);
  assert.equal(secondBytes.equals(firstBytes), true, '已存在的备份不能被覆盖');
  assert.equal(sha256(secondBytes), first.sha256);
  assert.equal(
    (await readdir(dir)).filter((name) => name.includes('.tmp-')).length,
    0,
    '失败后应清理临时文件',
  );
});

test('writeZip 拒绝不支持的压缩方式/来源类型/参数', async () => {
  const dir = await freshDir('bad-compress');
  const zipPath = join(dir, 'bad.zip');
  await expectZipError(
    writeZip(zipPath, [
      {
        name: 'a.txt',
        source: { type: 'buffer', data: Buffer.from('x') },
        compress: 'bzip2',
      },
    ]),
    'ZIP_UNSUPPORTED',
  );
  await expectZipError(
    writeZip(zipPath, [{ name: 'a.txt', source: { type: 'something-else' } }]),
    'ZIP_UNSUPPORTED',
  );
  await expectZipError(writeZip(zipPath, [{ name: 'a.txt' }]), 'ZIP_INVALID_ARGUMENT');
  await expectZipError(writeZip(zipPath, 'not-an-array'), 'ZIP_INVALID_ARGUMENT');
  assert.equal(await exists(zipPath), false);
});

// ---------------------------------------------------------------------------
// 读取端安全校验（手工构造 ZIP 注入恶意字段）
// ---------------------------------------------------------------------------

test('读取端拒绝路径穿越名（listZip/extractZip）', async () => {
  const dir = await freshDir('traversal-read');
  const destDir = join(dir, 'dest');
  const badNames = [
    '../evil.txt',
    '/abs.txt',
    'a\\b.txt',
    'C:evil.txt',
    'a/../../b.txt',
  ];
  for (let index = 0; index < badNames.length; index += 1) {
    // eslint-disable-next-line no-await-in-loop
    const zipPath = await writeRawZip(dir, `bad-${index}.zip`, [
      { name: badNames[index], data: Buffer.from('evil') },
    ]);
    // eslint-disable-next-line no-await-in-loop
    await expectZipError(listZip(zipPath), 'ZIP_PATH_UNSAFE');
    // eslint-disable-next-line no-await-in-loop
    await expectZipError(extractZip(zipPath, destDir), 'ZIP_PATH_UNSAFE');
  }
  assert.equal(await exists(join(dir, 'evil.txt')), false);
  assert.equal(await exists(join(root, 'evil.txt')), false);
  assert.equal(await exists(destDir), false, '预检失败不应创建 destDir');
});

test('读取端拒绝重复条目名', async () => {
  const dir = await freshDir('dupes-read');
  const zipPath = await writeRawZip(dir, 'dup.zip', [
    { name: 'dup.txt', data: Buffer.from('1') },
    { name: 'dup.txt', data: Buffer.from('2') },
  ]);
  await expectZipError(listZip(zipPath), 'ZIP_DUPLICATE');
  await expectZipError(extractZip(zipPath, join(dir, 'dest')), 'ZIP_DUPLICATE');
  await expectZipError(hashZipEntries(zipPath), 'ZIP_DUPLICATE');
});

test('读取端识别并拒绝符号链接条目', async () => {
  const dir = await freshDir('symlink');
  const symlinkMode = (0o120777 << 16) >>> 0;
  const zipPath = await writeRawZip(dir, 'link.zip', [
    { name: 'link', data: Buffer.from('/etc/passwd'), externalAttrs: symlinkMode },
  ]);
  const listed = await listZip(zipPath);
  assert.equal(listed.entries.length, 1);
  assert.equal(listed.entries[0].isSymlink, true);
  assert.equal(listed.entries[0].isDirectory, false);
  await expectZipError(extractZip(zipPath, join(dir, 'dest')), 'ZIP_UNSUPPORTED');
  await expectZipError(hashZipEntries(zipPath), 'ZIP_UNSUPPORTED');
});

test('读取端拒绝不支持的 method / 加密标志 / ZIP64', async () => {
  const dir = await freshDir('unsupported');

  const badMethod = await writeRawZip(dir, 'method.zip', [
    { name: 'a.txt', data: Buffer.from('x'), method: 12 },
  ]);
  await expectZipError(listZip(badMethod), 'ZIP_UNSUPPORTED');
  await expectZipError(extractZip(badMethod, join(dir, 'dest-method')), 'ZIP_UNSUPPORTED');

  const encrypted = await writeRawZip(dir, 'encrypted.zip', [
    { name: 'a.txt', data: Buffer.from('x'), flags: 0x0001 },
  ]);
  await expectZipError(listZip(encrypted), 'ZIP_UNSUPPORTED');

  // 正常 zip，把 EOCD 的条目数改成 0xFFFF → 触发 ZIP64 标记
  const okDir = await freshDir('zip64');
  const okPath = join(okDir, 'ok.zip');
  await writeZip(okPath, [
    { name: 'a.txt', source: { type: 'buffer', data: Buffer.from('x') } },
  ]);
  const bytes = await readFile(okPath);
  const eocdOffset = bytes.length - 22;
  assert.equal(bytes.readUInt32LE(eocdOffset), 0x06054b50);
  bytes.writeUInt16LE(0xffff, eocdOffset + 8);
  bytes.writeUInt16LE(0xffff, eocdOffset + 10);
  const zip64Path = join(okDir, 'zip64.zip');
  await writeFile(zip64Path, bytes);
  await expectZipError(listZip(zip64Path), 'ZIP_UNSUPPORTED');
});

// ---------------------------------------------------------------------------
// 损坏 / 校验
// ---------------------------------------------------------------------------

test('CRC 篡改检测（extractZip / readZipEntry / hashZipEntries）', async () => {
  const dir = await freshDir('crc-tamper');
  const zipPath = join(dir, 'store.zip');
  const marker = Buffer.from('CRC-TAMPER-PAYLOAD-0123456789');
  await writeZip(zipPath, [
    { name: 'payload.bin', source: { type: 'buffer', data: marker }, compress: 'store' },
  ]);

  const bytes = await readFile(zipPath);
  const index = bytes.indexOf(marker);
  assert.ok(index >= 0, '未在 zip 中找到 store 模式的明文载荷');
  bytes[index] ^= 0xff;
  const tampered = join(dir, 'tampered.zip');
  await writeFile(tampered, bytes);

  const destDir = join(dir, 'dest');
  await expectZipError(extractZip(tampered, destDir), 'ZIP_CRC_MISMATCH');
  assert.equal(await exists(destDir), true, 'CRC 失败前 destDir 已被创建');
  assert.equal((await readdir(destDir)).length, 0, '失败条目的临时文件应被清理');

  await expectZipError(readZipEntry(tampered, 'payload.bin'), 'ZIP_CRC_MISMATCH');
  await expectZipError(hashZipEntries(tampered), 'ZIP_CRC_MISMATCH');
});

test('extractZip 用 expectedHashes 逐文件校验 SHA-256', async () => {
  const dir = await freshDir('expected-hashes');
  const zipPath = join(dir, 'hashes.zip');
  const dataA = Buffer.from('aaa');
  const dataB = Buffer.from('bbb');
  await writeZip(zipPath, [
    { name: 'a.txt', source: { type: 'buffer', data: dataA } },
    { name: 'b.txt', source: { type: 'buffer', data: dataB } },
  ]);

  const good = await extractZip(zipPath, join(dir, 'good'), {
    expectedHashes: { 'a.txt': sha256(dataA), 'b.txt': sha256(dataB) },
  });
  assert.equal(good.files.length, 2);

  // 大小写不敏感：大写十六进制也应接受
  const upper = await extractZip(zipPath, join(dir, 'upper'), {
    expectedHashes: { 'a.txt': sha256(dataA).toUpperCase(), 'b.txt': sha256(dataB) },
  });
  assert.equal(upper.files.length, 2);

  await expectZipError(
    extractZip(zipPath, join(dir, 'bad'), {
      expectedHashes: { 'a.txt': '0'.repeat(64), 'b.txt': sha256(dataB) },
    }),
    'ZIP_HASH_MISMATCH',
  );
});

// ---------------------------------------------------------------------------
// 上限 / destDir / 空 ZIP / 大文件
// ---------------------------------------------------------------------------

test('extractZip 大小上限与 destDir 约束', async () => {
  const dir = await freshDir('limits');
  const zipPath = join(dir, 'limits.zip');
  const data = Buffer.alloc(1024, 7);
  await writeZip(zipPath, [{ name: 'big.bin', source: { type: 'buffer', data } }]);

  await expectZipError(
    extractZip(zipPath, join(dir, 'limit-file'), { maxFileBytes: 100 }),
    'ZIP_LIMIT',
  );
  await expectZipError(
    extractZip(zipPath, join(dir, 'limit-total'), { maxTotalBytes: 100 }),
    'ZIP_LIMIT',
  );
  await expectZipError(
    extractZip(zipPath, join(dir, 'bad-option'), { maxFileBytes: 0 }),
    'ZIP_INVALID_ARGUMENT',
  );

  const nonEmpty = join(dir, 'non-empty');
  await mkdir(nonEmpty, { recursive: true });
  await writeFile(join(nonEmpty, 'keep.txt'), 'keep');
  await expectZipError(extractZip(zipPath, nonEmpty), 'ZIP_DEST_NOT_EMPTY');
  assert.equal((await readdir(nonEmpty)).length, 1, '非空 destDir 不应被写入');

  const notDir = join(dir, 'not-dir');
  await writeFile(notDir, 'file');
  await expectZipError(extractZip(zipPath, notDir), 'ZIP_DEST_INVALID');
});

test('空 ZIP：write/list/read/hash/extract', async () => {
  const dir = await freshDir('empty-zip');
  const zipPath = join(dir, 'empty.zip');
  const result = await writeZip(zipPath, []);
  assert.equal(result.entries, 0);
  assert.equal(result.size, 22);
  assert.equal(result.sha256, sha256(await readFile(zipPath)));

  assert.deepEqual(await listZip(zipPath), { entries: [], totalSize: 0 });
  assert.deepEqual(await hashZipEntries(zipPath), { entries: [], totalSize: 0 });

  const destDir = join(dir, 'out');
  const extracted = await extractZip(zipPath, destDir);
  assert.deepEqual(extracted, { files: [], totalSize: 0 });
  assert.equal((await stat(destDir)).isDirectory(), true);
  assert.deepEqual(await readdir(destDir), []);
});

test('大文件流式：5 MiB 随机数据 deflate 写入 → 哈希校验 → 解压一致', async () => {
  const dir = await freshDir('large');
  const input = randomBytes(5 * 1024 * 1024);
  const inputPath = join(dir, 'input.bin');
  await writeFile(inputPath, input);

  const zipPath = join(dir, 'big.zip');
  const result = await writeZip(zipPath, [
    { name: 'input.bin', source: { type: 'file', path: inputPath }, compress: 'deflate' },
  ]);
  assert.equal(result.entries, 1);
  const zipBytes = await readFile(zipPath);
  assert.equal(result.sha256, sha256(zipBytes));

  const hashed = await hashZipEntries(zipPath);
  assert.equal(hashed.entries.length, 1);
  assert.equal(hashed.entries[0].name, 'input.bin');
  assert.equal(hashed.entries[0].size, input.length);
  assert.equal(hashed.entries[0].sha256, sha256(input));
  assert.equal(hashed.totalSize, input.length);

  const destDir = join(dir, 'out');
  const extracted = await extractZip(zipPath, destDir);
  assert.equal(extracted.totalSize, input.length);
  assert.equal(extracted.files.length, 1);
  assert.equal(extracted.files[0].sha256, sha256(input));
  assert.equal((await readFile(join(destDir, 'input.bin'))).equals(input), true);
});
