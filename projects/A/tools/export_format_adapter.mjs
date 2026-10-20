// Independent offline candidate adapter. No business/API registration or file I/O.
import { createHash } from 'node:crypto';
import {
  extractServerMsgIds, getContentJson, tryParseJson, getRefMsg, getRefContent,
  isSystemMsg, renderSystemMsg, shouldShow,
} from '../vendor/douyin-chat-export/frontend/src/lib/douyinMessage.js';

export const FORMAT = 'douyin-chat-export.message_rows.v1';
export const MAX_ROWS = 2000;
const MAX_TEXT = 256000;
const MAX_ID = 512;
const MAX_TEMPLATES = 64;

// Input is ordinary JSON data, not live DOM/page objects or arbitrary Proxies.
// Read only finite own data fields; reject accessors instead of invoking them.
function field(object, key) {
  const descriptor = Object.getOwnPropertyDescriptor(object, key);
  if (!descriptor) return undefined;
  if (!Object.hasOwn(descriptor, 'value')) throw new TypeError(`accessor_not_allowed:${key}`);
  return descriptor.value;
}

function plain(object, label) {
  if (!object || typeof object !== 'object' || Array.isArray(object)) {
    throw new TypeError(`${label}_must_be_plain_object`);
  }
  const prototype = Object.getPrototypeOf(object);
  if (prototype !== Object.prototype && prototype !== null) {
    throw new TypeError(`${label}_must_be_plain_object`);
  }
}

function id(value, key, optional = false) {
  if (optional && (value === null || value === undefined || value === '')) return null;
  if (typeof value !== 'string' || !value.trim() || value.length > MAX_ID) {
    throw new TypeError(`platform_id_must_be_string:${key}`);
  }
  return value;
}

function text(value, key) {
  if (value === null || value === undefined) return null;
  if (typeof value !== 'string') throw new TypeError(`text_must_be_string:${key}`);
  if (value.length > MAX_TEXT) throw new RangeError(`text_too_long:${key}`);
  return value;
}

function timestamp(raw) {
  // This row format uses Unix seconds. Missing, zero, invalid or ambiguous
  // values remain unknown; never manufacture epoch zero or the current time.
  const acceptable = typeof raw === 'number' ||
    (typeof raw === 'string' && /^\d+(?:\.\d+)?$/.test(raw));
  const value = acceptable ? Number(raw) : NaN;
  if (!Number.isFinite(value) || value <= 0 || value > 253402300799) {
    return { state: 'unknown', value: null, unit: 'unix_seconds' };
  }
  return { state: 'known', value, unit: 'unix_seconds' };
}

function primitive(value, key) {
  if (value === undefined || value === null) return null;
  if (typeof value === 'string' || typeof value === 'boolean' ||
      (typeof value === 'number' && Number.isFinite(value))) return value;
  throw new TypeError(`primitive_required:${key}`);
}

function safeTemplateDisplay(parserRow) {
  const source = getContentJson(parserRow) || tryParseJson(parserRow.content);
  if (!source?.tips) return true;
  if (typeof source.tips !== 'string' || source.tips.length > MAX_TEXT) return false;
  if (!Array.isArray(source.template)) return true;
  if (source.template.length > MAX_TEMPLATES) return false;
  let preview = source.tips;
  for (const entry of source.template) {
    if (!entry || entry.key === undefined) continue;
    if (!['string', 'number'].includes(typeof entry.key)) return false;
    const replacement = entry.name || '';
    if (typeof replacement !== 'string') return false;
    // Conservatively refuse braces, including partial delimiters that could
    // join adjacent input into a fresh placeholder on a later replacement.
    if (/[{}]/.test(replacement)) return false;
    const token = `{{${entry.key}}}`;
    let occurrences = 0;
    for (let position = preview.indexOf(token); position !== -1;
      position = preview.indexOf(token, position + token.length)) occurrences++;
    const nextLength = preview.length + occurrences * (replacement.length - token.length);
    if (nextLength > MAX_TEXT) return false;
    // Allocate only after checking the exact length for this replacement.
    preview = preview.replaceAll(token, () => replacement);
  }
  return true;
}

function unknownDisplay(original) {
  return {
    display_body: original, display_visibility_hint: null,
    reference_display_body: null, platform_reference_ids: [],
    display_parse_state: 'unknown',
  };
}

function display(parserRow, original) {
  try {
    // Check before any upstream display helper: renderSystemMsg can otherwise
    // amplify a small template payload exponentially through replaceAll.
    if (!safeTemplateDisplay(parserRow)) return unknownDisplay(original);
    // Upstream display heuristics may substitute templates or hide system tips.
    // Keep the original separately and never delete an import candidate.
    const system = isSystemMsg(parserRow);
    const body = system ? renderSystemMsg(parserRow) : original;
    const ref = getRefMsg(parserRow);
    return {
      display_body: typeof body === 'string' ? body : original,
      display_visibility_hint: shouldShow(parserRow),
      reference_display_body: ref ? getRefContent(ref) : null,
      platform_reference_ids: extractServerMsgIds(parserRow),
      display_parse_state: 'parsed_unverified',
    };
  } catch {
    return unknownDisplay(original);
  }
}

/**
 * Preview finite DB-shaped message rows already supplied by the caller.
 * Not a generic ChatLab file reader, collector, identity resolver or ingestion API.
 * account_namespace is an external partition label, never an authenticated account.
 */
export function adaptExportRows(rows, options) {
  plain(options, 'options');
  const namespace = field(options, 'account_namespace');
  if (typeof namespace !== 'string' || !namespace.trim() ||
      namespace !== namespace.trim() || namespace.length > 256) {
    throw new TypeError('external_account_namespace_required');
  }
  const sourceKind = field(options, 'source_kind') ?? 'unverified_export';
  if (!['fictional', 'unverified_export'].includes(sourceKind)) {
    throw new TypeError('unsupported_source_kind');
  }
  const coverageClaim = field(options, 'source_coverage') ?? 'unknown';
  if (!['unknown', 'declared_partial', 'declared_complete'].includes(coverageClaim)) {
    throw new TypeError('unsupported_coverage_claim');
  }
  if (!Array.isArray(rows)) throw new TypeError('rows_must_be_array');
  const length = field(rows, 'length');
  if (length > MAX_ROWS) throw new RangeError('too_many_rows');
  const groups = new Map();
  for (let index = 0; index < length; index++) {
    const row = field(rows, String(index));
    plain(row, 'row');
    const messageId = id(field(row, 'msg_id'), 'msg_id');
    const conversationId = id(field(row, 'conv_id'), 'conv_id');
    const senderId = id(field(row, 'sender_uid'), 'sender_uid', true);
    const original = text(field(row, 'content'), 'content');
    const rawData = text(field(row, 'raw_data'), 'raw_data');
    const refMsg = text(field(row, 'ref_msg'), 'ref_msg');
    const rawTime = primitive(field(row, 'timestamp'), 'timestamp');
    const msgType = primitive(field(row, 'msg_type'), 'msg_type');
    const sourceDirection = field(row, 'direction');
    const direction = ['incoming', 'outgoing'].includes(sourceDirection) ? sourceDirection : 'unknown';
    const parserRow = {
      msg_id: messageId, conv_id: conversationId, sender_uid: senderId,
      content: original, raw_data: rawData, ref_msg: refMsg, msg_type: msgType,
      timestamp: rawTime,
    };
    const sourceKey = JSON.stringify([namespace, conversationId, messageId]);
    const candidate = {
      original_body: original, original_raw_data: rawData,
      original_ref_msg: refMsg, original_timestamp: rawTime,
      timestamp: timestamp(rawTime), platform_sender_id: senderId,
      msg_type: msgType, direction,
      ...display(parserRow, original),
      identity: { state: 'unverified', person_key: null },
      delivery: { state: 'unknown', send_success: null, manually_sent: null,
        operator_key: null, attribution: 'unknown' },
    };
    const signature = JSON.stringify([
      original, rawData, refMsg, rawTime, senderId, msgType, direction,
    ]);
    candidate.body_revision = createHash('sha256').update(signature).digest('hex');
    let group = groups.get(sourceKey);
    if (!group) {
      group = {
        source_key: sourceKey, account_namespace: namespace,
        platform_message_id: messageId, platform_conversation_id: conversationId,
        source_kind: sourceKind, revision_state: 'single_candidate',
        observations: 0, versions: [],
      };
      groups.set(sourceKey, group);
    }
    group.observations++;
    if (!group.versions.some(version => version.body_revision === candidate.body_revision)) {
      group.versions.push(candidate);
    }
    if (group.versions.length > 1) group.revision_state = 'conflicting_versions';
  }
  return {
    format: FORMAT, status: 'offline_preview', persisted: false,
    account_namespace: namespace, account_binding: 'external_unverified',
    source_kind: sourceKind, coverage: { state: 'unknown', external_claim: coverageClaim },
    capabilities: { source: 'unverified', adapter: false, send: false },
    records: [...groups.values()],
  };
}
