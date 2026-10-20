// Whole-document validation and grouping. No IO, authority, or business writes.
import { createHash } from 'node:crypto';
import { adaptExportText, checkedJson, FILE_LIMITS, normalizeChatlabRecord } from './export_file_format_adapter.mjs';
export const DOCUMENT_LIMITS = Object.freeze({ max_bytes: FILE_LIMITS.max_bytes,
  max_messages: 100000, max_normalized_bytes: 64 * 1024 * 1024 });
const bytes = value => Buffer.byteLength(value, 'utf8');
const sha = value => createHash('sha256').update(value, 'utf8').digest('hex');
const fail = code => { throw new Error(code); };

export function prepareChatlabDocument(text, options) {
  if (typeof text !== 'string' || bytes(text) > DOCUMENT_LIMITS.max_bytes) fail('document_byte_budget_exceeded');
  const lines = text.split('\n');
  if (lines.at(-1) === '') lines.pop();
  if (lines.length > DOCUMENT_LIMITS.max_messages + FILE_LIMITS.max_rows + 1) fail('document_line_budget_exceeded');
  const metadata = { text_sha256: sha(text), size_bytes: bytes(text), line_count: lines.length };
  const prefix = [];
  const groups = new Map();
  let base = null, count = 0, charged = 0;
  for (const line of lines) {
    if (!line.trim()) fail('empty_jsonl_line');
    if (bytes(line) > FILE_LIMITS.max_line_bytes) fail('line_byte_budget_exceeded');
    const row = checkedJson(line, {tokens: 0});
    if (!row || typeof row !== 'object' || Array.isArray(row)) fail('invalid_chatlab_record');
    if (row._type !== 'message') {
      if (base || (prefix.length ? row._type !== 'member' : row._type !== 'header')) fail('invalid_chatlab_record_order_or_type');
      prefix.push(line);
      if (prefix.length > FILE_LIMITS.max_rows + 1) fail('record_budget_exceeded');
      continue;
    }
    if (!base) {
      const validated = adaptExportText(prefix.join('\n'), {...options, format:'chatlab-jsonl'});
      const {records, output_budget, ...rest} = validated;
      base = {...rest, file_metadata: metadata};
      charged = bytes(JSON.stringify(base));
    }
    if (++count > DOCUMENT_LIMITS.max_messages) fail('document_record_budget_exceeded');
    const record = normalizeChatlabRecord(row, count - 1, base.account_namespace, metadata, base.header, base.source_kind);
    charged += bytes(JSON.stringify(record)) + 64;
    if (charged > DOCUMENT_LIMITS.max_normalized_bytes) fail('document_output_budget_exceeded');
    const prior = groups.get(record.source_key);
    if (!prior) groups.set(record.source_key, record);
    else {
      prior.observations++;
      for (const version of record.versions) {
        if (!prior.versions.some(old => old.candidate_revision === version.candidate_revision)) prior.versions.push(version);
      }
      if (prior.versions.length > 1) prior.revision_state = 'conflicting_versions';
    }
  }
  if (!base) {
    const {records, output_budget, ...rest} = adaptExportText(text, {...options, format:'chatlab-jsonl'});
    base = rest;
  }
  // Retain the exact old preview for inputs accepted by the existing reader.
  let legacy = null;
  try { legacy = adaptExportText(text, {...options, format:'chatlab-jsonl'}); }
  catch (error) {
    if (!['record_budget_exceeded','file_line_budget_exceeded','json_token_budget_exceeded','output_byte_budget_exceeded'].includes(error.message.split(':')[0])) throw error;
  }
  return {format:'chatlab-validated-document-1', base, records:[...groups.values()],
    source_rows:count, legacy_preview:legacy};
}
