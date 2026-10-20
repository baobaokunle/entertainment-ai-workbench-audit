// Text-only offline preview. Caller reads an explicit file; this module never does.
import { createHash } from 'node:crypto';
import { adaptExportRows } from './export_format_adapter.mjs';

export const FILE_FORMATS = Object.freeze(['db-json', 'db-jsonl', 'chatlab-json', 'chatlab-jsonl']);
export const FILE_LIMITS = Object.freeze({ max_bytes: 8388608, max_lines: 4096,
  max_line_bytes: 1048576, max_rows: 2000, max_depth: 32, max_tokens: 200000,
  max_field_chars: 256000, max_output_bytes: 33554432 });
const bytes = value => Buffer.byteLength(value, 'utf8');
const digest = value => createHash('sha256').update(value, 'utf8').digest('hex');
const fail = code => { throw new Error(code); };
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);

// Validate syntax/depth/duplicate keys and unsafe numeric lexemes BEFORE JSON.parse.
// No numeric ID repair: even safe numeric IDs are rejected by the shape validators.
export function checkedJson(input, budget) {
  let index = 0;
  const number = /-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/y;
  const whitespace = () => { while (index < input.length && /[ \t\r\n]/.test(input[index])) index++; };
  const quote = key => {
    const start = index++;
    while (index < input.length) {
      const char = input[index++];
      if (char === '\\') index++;
      else if (char === '"') {
        if (key) { try { return JSON.parse(input.slice(start, index)); } catch { fail('invalid_json'); } }
        return;
      }
    }
    fail('invalid_json');
  };
  const value = depth => {
    if (depth > FILE_LIMITS.max_depth) fail('json_depth_exceeded');
    if (++budget.tokens > FILE_LIMITS.max_tokens) fail('json_token_budget_exceeded');
    whitespace();
    const char = input[index];
    if (char === '"') { quote(false); return; }
    if (char === '{' || char === '[') {
      const close = char === '{' ? '}' : ']';
      const keys = new Set(); index++; whitespace();
      if (input[index] === close) { index++; return; }
      while (true) {
        whitespace();
        if (char === '{') {
          if (input[index] !== '"') fail('invalid_json');
          const key = quote(true);
          if (keys.has(key)) fail('duplicate_json_key');
          keys.add(key); whitespace();
          if (input[index++] !== ':') fail('invalid_json');
        }
        value(depth + 1); whitespace();
        if (input[index] === close) { index++; return; }
        if (input[index++] !== ',') fail('invalid_json');
      }
    }
    for (const literal of ['true', 'false', 'null']) {
      if (input.startsWith(literal, index)) { index += literal.length; return; }
    }
    number.lastIndex = index;
    const match = number.exec(input);
    if (!match) fail('invalid_json');
    const token = match[0]; index = number.lastIndex;
    if (token.length > 128) fail('unsafe_numeric_token');
    if (/^-?\d+$/.test(token)) {
      const integer = BigInt(token);
      if (integer > 9007199254740991n || integer < -9007199254740991n) fail('unsafe_numeric_token');
    } else {
      const numeric = Number(token);
      const significant = token.split(/[eE]/)[0].replace(/[-.]/g, '').replace(/^0+/, '');
      if (!Number.isFinite(numeric) || Math.abs(numeric) > Number.MAX_SAFE_INTEGER ||
        significant.length > 15 || (numeric === 0 && /[1-9]/.test(significant))) fail('unsafe_numeric_token');
    }
  };
  value(0); whitespace();
  if (index !== input.length) fail('invalid_json');
  try { return JSON.parse(input); } catch { fail('invalid_json'); }
}

function nullableText(value, name) {
  if (value === undefined || value === null) return null;
  if (typeof value !== 'string') fail(`invalid_string:${name}`);
  if (value.length > FILE_LIMITS.max_field_chars) fail(`field_budget_exceeded:${name}`);
  return value;
}
function identifier(value, name, optional = false) {
  if (optional && (value === undefined || value === null || value === '')) return null;
  if (typeof value !== 'string' || !value.trim() || value.length > 512 || /[\x00-\x1f]/.test(value)) {
    fail(`string_id_required:${name}`);
  }
  return value;
}
function sourceTime(raw) {
  const number = typeof raw === 'number' || (typeof raw === 'string' && /^\d+(?:\.\d+)?$/.test(raw)) ? Number(raw) : NaN;
  return { state: Number.isFinite(number) && number > 0 && number <= 253402300799 ? 'parsed' : 'unknown',
    value: Number.isFinite(number) && number > 0 && number <= 253402300799 ? number : null,
    unit: 'unix_seconds', verified: false };
}
function rejectNul(body) { if (body?.includes('\0')) fail('body_contains_nul'); }

// Shared by the bounded preview and the document-preserving paged host reader.
export function normalizeChatlabRecord(row, index, namespace, metadata, header, sourceKind) {
  if (!object(row)) fail('message_object_required');
  if (!Object.hasOwn(row, 'content')) fail('message_content_required');
  const original = nullableText(row.content, 'content'); rejectNul(original);
  const messageId = identifier(row.platformMessageId, 'platformMessageId', true);
  const senderId = identifier(row.sender, 'sender', true);
  identifier(row.replyToMessageId, 'replyToMessageId', true);
  if (row.replyTo !== undefined && !object(row.replyTo)) fail('invalid_reply_block');
  if (row.replyTo) identifier(row.replyTo.replyTo, 'replyTo.replyTo', true);
  nullableText(row.accountName, 'accountName');
  if (row.type !== undefined && row.type !== null && typeof row.type !== 'number' && typeof row.type !== 'string') fail('invalid_message_type');
  if (row.timestamp !== undefined && row.timestamp !== null && typeof row.timestamp !== 'number' && typeof row.timestamp !== 'string') fail('invalid_timestamp');
  const keyOrigin = messageId ? 'platform_claim' : 'local_record';
  const version = { local_record_id: `record:${index + 1}`, original_body: original,
    display_body: original, original_message: row, original_timestamp: row.timestamp ?? null,
    timestamp: sourceTime(row.timestamp), original_type: row.type ?? null,
    type_state: [0, 1, 5, 24, 26, 27, 99].includes(row.type) ? 'export_declared' : 'unknown',
    platform_sender_id: senderId, direction: 'unknown',
    identity: { state: 'unverified', person_key: null },
    delivery: { state: 'unknown', send_success: null, manually_sent: null, operator_key: null },
    candidate_revision: digest(JSON.stringify(row)) };
  return { source_key: JSON.stringify([namespace, 'chatlab-document', metadata.text_sha256,
    keyOrigin, messageId ?? index + 1]), account_namespace: namespace, key_origin: keyOrigin,
    platform_message_id: messageId, platform_conversation_id: header.meta.groupId ?? null,
    conversation_binding: 'unverified_export_document', source_kind: sourceKind,
    revision_state: 'single_candidate', observations: 1, versions: [version] };
}

/** Explicit format and caller partition; returns only candidates, never a commit. */
export function adaptExportText(text, options) {
  if (typeof text !== 'string') fail('text_input_required');
  if (!object(options) || ![Object.prototype, null].includes(Object.getPrototypeOf(options))) fail('explicit_options_required');
  const option = key => {
    const descriptor = Object.getOwnPropertyDescriptor(options, key);
    if (descriptor && !Object.hasOwn(descriptor, 'value')) fail('option_accessor_not_allowed');
    return descriptor?.value;
  };
  const format = option('format'), namespace = option('account_namespace');
  const sourceKind = option('source_kind') ?? 'unverified_export';
  if (!FILE_FORMATS.includes(format)) fail('unsupported_format');
  if (typeof namespace !== 'string' || !namespace.trim() || namespace !== namespace.trim() ||
    namespace.length > 256 || /[\x00-\x1f]/.test(namespace)) fail('external_account_namespace_required');
  if (!['fictional', 'unverified_export'].includes(sourceKind)) fail('unsupported_source_kind');
  const size = bytes(text);
  if (size > FILE_LIMITS.max_bytes) fail('file_byte_budget_exceeded');
  if (!text.trim()) fail('empty_input');
  const lines = text.split('\n');
  if (lines.at(-1) === '') lines.pop();
  if (lines.length > FILE_LIMITS.max_lines) fail('file_line_budget_exceeded');
  for (const line of lines) if (bytes(line) > FILE_LIMITS.max_line_bytes) fail('line_byte_budget_exceeded');
  const metadata = { text_sha256: digest(text), size_bytes: size, line_count: lines.length };
  const base = { format: format.startsWith('db-') ? 'douyin-chat-export.message_rows.v1' : 'chatlab.export_records.v0.0.2',
    input_format: format, status: 'offline_preview', persisted: false, account_namespace: namespace,
    account_binding: 'external_unverified', source_kind: sourceKind,
    file_metadata: metadata, coverage: { state: 'unknown' },
    capabilities: { source: 'unverified', adapter: false, send: false } };
  const isJsonl = format.endsWith('jsonl');
  const tokenBudget = { tokens: 0 };
  const parsed = isJsonl ? lines.map((line, index) => {
    if (!line.trim()) fail(`empty_jsonl_line:${index + 1}`);
    try { return checkedJson(line, tokenBudget); } catch (error) { fail(`${error.message}:line=${index + 1}`); }
  }) : checkedJson(text, tokenBudget);
  let rows, header = null, members = null;
  if (format.startsWith('db-')) {
    if (!Array.isArray(parsed)) fail('db_rows_array_required');
    rows = parsed;
  } else if (isJsonl) {
    if (!object(parsed[0]) || parsed[0]._type !== 'header') fail('chatlab_first_header_required');
    header = parsed[0]; members = []; rows = []; let sawMessage = false;
    for (const record of parsed.slice(1)) {
      if (!object(record)) fail('invalid_chatlab_record');
      if (record._type === 'member' && !sawMessage) members.push(record);
      else if (record._type === 'message') { sawMessage = true; rows.push(record); }
      else fail('invalid_chatlab_record_order_or_type');
    }
  } else {
    if (!object(parsed) || !Array.isArray(parsed.members) || !Array.isArray(parsed.messages)) fail('invalid_chatlab_document');
    header = { chatlab: parsed.chatlab, meta: parsed.meta }; members = parsed.members; rows = parsed.messages;
  }
  if (rows.length > FILE_LIMITS.max_rows || (members && members.length > FILE_LIMITS.max_rows)) fail('record_budget_exceeded');
  if (header) {
    if (!object(header.chatlab) || header.chatlab.version !== '0.0.2') fail('unsupported_chatlab_version');
    if (header.chatlab.generator !== 'douyin-chat-export') fail('unsupported_chatlab_generator');
    if (!object(header.meta) || header.meta.platform !== 'douyin' || !['private', 'group'].includes(header.meta.type)) fail('invalid_chatlab_meta');
    identifier(header.meta.ownerId, 'ownerId', true);
    identifier(header.meta.groupId, 'groupId', true);
    for (const member of members) {
      if (!object(member)) fail('invalid_chatlab_member');
      identifier(member.platformId, 'platformId'); nullableText(member.accountName, 'member.accountName');
    }
    base.header = header; base.members = members;
    base.body_provenance = 'export_content_only_not_reconstructed_platform_original';
  }
  // Charge each bounded candidate BEFORE retaining it, never serialize the full
  // already-amplified output to discover the budget only after allocation.
  let outputBytes = bytes(JSON.stringify(base));
  const charge = candidate => {
    outputBytes += bytes(JSON.stringify(candidate)) + 64;
    if (outputBytes > FILE_LIMITS.max_output_bytes) fail('output_byte_budget_exceeded');
  };
  if (outputBytes > FILE_LIMITS.max_output_bytes) fail('output_byte_budget_exceeded');
  const groups = new Map();
  for (let index = 0; index < rows.length; index++) {
    const row = rows[index];
    if (!object(row)) fail('message_object_required');
    let record;
    if (!header) {
      rejectNul(nullableText(row.content, 'content'));
      identifier(row.msg_id, 'msg_id'); identifier(row.conv_id, 'conv_id');
      identifier(row.sender_uid, 'sender_uid', true);
      record = adaptExportRows([row], { account_namespace: namespace, source_kind: sourceKind }).records[0];
      record.key_origin = 'platform_claim';
      for (const candidate of record.versions) {
        candidate.timestamp.state = candidate.timestamp.state === 'known' ? 'parsed' : 'unknown';
        candidate.timestamp.verified = false;
      }
    } else {
      record = normalizeChatlabRecord(row, index, namespace, metadata, header, sourceKind);
    }
    charge(record);
    const existing = groups.get(record.source_key);
    if (!existing) groups.set(record.source_key, record);
    else {
      existing.observations += record.observations;
      const revisionKey = header ? 'candidate_revision' : 'body_revision';
      for (const version of record.versions) {
        if (!existing.versions.some(old => old[revisionKey] === version[revisionKey])) existing.versions.push(version);
      }
      if (existing.versions.length > 1) existing.revision_state = 'conflicting_versions';
    }
  }
  return { ...base, records: [...groups.values()], output_budget: {
    conservative_charged_bytes: outputBytes, limit_bytes: FILE_LIMITS.max_output_bytes } };
}
