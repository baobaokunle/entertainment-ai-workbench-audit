// Private input/output is a local parent-process pipe, never a user log.
import { prepareChatlabDocument, DOCUMENT_LIMITS } from './chatlab_paged_adapter.mjs';
let input = '', size = 0;
process.stdin.setEncoding('utf8');
try {
  for await (const chunk of process.stdin) {
    size += Buffer.byteLength(chunk);
    if (size > DOCUMENT_LIMITS.max_bytes * 6 + 4096) throw new Error('worker_input_budget_exceeded');
    input += chunk;
  }
  const request = JSON.parse(input);
  const document = prepareChatlabDocument(request.text, {account_namespace:request.namespace, source_kind:'unverified_export'});
  process.stdout.write(JSON.stringify({ok:true, document}));
} catch (error) {
  const code = String(error.message).split(':')[0];
  process.stdout.write(JSON.stringify({ok:false, code:/^[a-z_]{1,80}$/.test(code) ? code : 'document_validation_failed'}));
  process.exitCode = 1;
}
