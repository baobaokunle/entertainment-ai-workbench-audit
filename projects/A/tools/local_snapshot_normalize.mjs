// Internal local pipe only. The caller captures private output in memory.
import {adaptExportRows} from './export_format_adapter.mjs';
try {
  const parts=[]; let size=0;
  for await (const part of process.stdin) {
    size+=part.length;
    if(size>8*1024*1024) throw new Error('budget');
    parts.push(part);
  }
  const input=JSON.parse(Buffer.concat(parts).toString('utf8'));
  if(Object.keys(input).sort().join(',')!=='namespace,rows'||!Array.isArray(input.rows)||input.rows.length<1||input.rows.length>100) throw new Error('shape');
  const result=adaptExportRows(input.rows,{account_namespace:input.namespace,source_kind:'unverified_export'});
  const output=Buffer.from(JSON.stringify(result));
  if(output.length>32*1024*1024) throw new Error('budget');
  process.stdout.write(output);
} catch {
  process.stderr.write('LOCAL_SNAPSHOT_NORMALIZATION_REFUSED\n');
  process.exitCode=2;
}
