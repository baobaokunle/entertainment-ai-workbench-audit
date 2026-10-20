export const uuid = () => crypto.randomUUID().replaceAll("-", "");
export const ADAPTER = "b-fixed-sources/0.1.6";
export const badKey = /cookie|authorization|password|access.?token|refresh.?token|csrf|signature|a_bogus|msToken|headers|credentials/i;
export class LosslessNumber { constructor(token){this.token=token;} }

// JSON numeric lexemes never pass through IEEE-754. Used also inside generated MAIN adapter.
export function parseLossless(text) {
  let p = 0, depth = 0;
  const ws = () => { while (/\s/.test(text[p] || "x")) p++; };
  function str() {
    const start = p++;
    while (p < text.length) { if (text[p] === "\\") { p += 2; continue; } if (text[p++] === '"') return JSON.parse(text.slice(start, p)); }
    throw Error("SOURCE_JSON");
  }
  function value() {
    ws(); if (++depth > 64) throw Error("SOURCE_DEPTH");
    let out, c = text[p];
    if (c === '"') out = str();
    else if (c === "{" || c === "[") {
      const obj = c === "{"; p++; out = obj ? Object.create(null) : []; ws();
      const end = obj ? "}" : "]";
      if (text[p] !== end) for (;;) {
        ws(); let key;
        if (obj) { if (text[p] !== '"') throw Error("SOURCE_JSON"); key = str(); ws(); if (text[p++] !== ":" || Object.hasOwn(out,key)) throw Error("SOURCE_JSON"); }
        const item = value(); if (obj) out[key] = item; else out.push(item);
        ws(); if (text[p] !== ",") break; p++;
      }
      if (text[p++] !== end) throw Error("SOURCE_JSON");
    } else {
      const match = /^(?:true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)/.exec(text.slice(p));
      if (!match) throw Error("SOURCE_JSON"); p += match[0].length;
      out = /^[tfn]/.test(match[0]) ? JSON.parse(match[0]) : new LosslessNumber(match[0]);
    }
    depth--; return out;
  }
  const out = value(); ws(); if (p !== text.length) throw Error("SOURCE_JSON"); return out;
}

export function scalar(value) {
  if (value === null) return {type:"null",value:null,lossless:true};
  if (value instanceof LosslessNumber)
    return {type:"number_token",value:value.token,lossless:true};
  if (typeof value === "string" && value.length <= 32768) return {type:"string",value,lossless:true};
  if (typeof value === "boolean") return {type:"boolean",value,lossless:true};
  if (typeof value === "number" && Number.isFinite(value) && Math.abs(value) <= Number.MAX_SAFE_INTEGER)
    return {type:"number",value,lossless:true};
  return null;
}
export const raw = value => value && typeof value === "object" && "value" in value ? value.value : null;
export function observation(domain, source, fields, context={}, coverage={}, issues=[]) {
  return {contract:"b-source-observation/1",domain,
    source:{origin:source.origin,path:source.path,adapter:ADAPTER,transport:source.transport || "site_get",record_ref:source.ref || "/",raw_response_saved:false},
    observed_at:source.observed_at || new Date().toISOString(), fields,
    context:{owner:null,app_scope:null,session:null,conversation:null,period_label:null,period_start:null,period_end:null,source_timezone:null,source_cutoff:null,...context},
    coverage:{status:"partial",basis:"bounded_source_projection",received:null,limit:null,terminal:"unknown",...coverage},issues};
}
export function capability(domain, code, source, context={}) {
  return observation("capability",source,{domain:scalar(domain),status:scalar(code)},context,
    {status:"not_available",basis:"adapter_result"},[code]);
}

export function localDate(d=new Date()) {
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
}
export function dateDays(first,last) {
  const date = new Date(`${first}T00:00:00Z`), end = new Date(`${last}T00:00:00Z`); const out=[];
  if (!/^\d{4}-\d\d-\d\d$/.test(first)||!/^\d{4}-\d\d-\d\d$/.test(last)||!Number.isFinite(+date) || !Number.isFinite(+end) || date>end || date.toISOString().slice(0,10)!==first||end.toISOString().slice(0,10)!==last||(end-date)/86400000>365) throw Error("DATE_RANGE_MAX_366");
  for (;date<=end;date.setUTCDate(date.getUTCDate()+1)) out.push(date.toISOString().slice(0,10)); return out;
}
// Date labels without known timezone stay labels; this arithmetic must NOT create UTC evidence.
export function segments(start,end,step) {
  if (!/^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$/.test(start||"") || !/^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$/.test(end||"")) return [];
  const a=Date.parse(start.replace(" ","T")+"Z"), b=Date.parse(end.replace(" ","T")+"Z");
  if (!Number.isFinite(a+b) || b<a || b-a>7*86400000 || !Number.isInteger(step)||step<1000) return [];
  const fmt=t=>new Date(t).toISOString().slice(0,19).replace("T"," ");
  if(fmt(a)!==start||fmt(b)!==end)return [];
  const out=[]; for(let t=a;t<=b;t+=step) out.push([fmt(t),fmt(Math.min(b,t+step-1000))]); return out;
}

// User filter, not a platform classification. Unknown values never become zero.
export function sessionSkipReasons(start,end,viewers) {
  const reasons=[];
  if(segments(start,end,86400000).length) {
    const elapsed=Date.parse(end.replace(' ','T')+'Z')-Date.parse(start.replace(' ','T')+'Z');
    if(elapsed<=600000)reasons.push('SKIP_DURATION_LE_10_MINUTES');
  }
  let count=null;
  if(viewers?.lossless===true) {
    if(viewers.type==='number'&&Number.isSafeInteger(viewers.value)&&viewers.value>=0)count=BigInt(viewers.value);
    else if(['string','number_token'].includes(viewers.type)&&typeof viewers.value==='string'&&/^\d+$/.test(viewers.value))count=BigInt(viewers.value);
  }
  if(count!==null&&count<=5n)reasons.push('SKIP_VIEWERS_LE_5');
  return reasons;
}

export function legacyRecord(parsed,fileKey,line,includeBody) {
  if (!parsed || typeof parsed!=="object" || Array.isArray(parsed)) throw Error("LEGACY_ROW_FORMAT");
  const allowed=["_type","type","recordType","platformId","platformMessageId","accountName","secUid","douyinId","conversationId","conversation_id","shortId","messageId","message_id","senderId","sender","timestamp","time","direction","memberCount","isGroup","name","nickname","displayName","role","chatType","exportedAt"];
  if(includeBody) allowed.push("text","content","body");
  const fields={}; for(const k of allowed) if(Object.hasOwn(parsed,k)) { const v=scalar(parsed[k]); if(v) fields[k]=v; }
  for(const [group,keys] of [['chatlab',['version','exportedAt','generator']],['meta',['name','platform','type','ownerId']]])for(const k of keys){const v=scalar(parsed[group]?.[k]);if(v)fields[`${group}.${k}`]=v;}
  fields.file_key=scalar(fileKey); fields.line=scalar(line);
  // A member ID or export private label never establishes conversation ownership/type.
  const native=fields.conversationId || fields.conversation_id || null;
  return observation("legacy_chat",{origin:"local:user-selected-jsonl",path:"/selected-jsonl",transport:"selected_jsonl",ref:`/files/${fileKey}/lines/${line}`},fields,
    {conversation:native},{basis:"selected_file_rows"},["HISTORICAL_ATTRIBUTION_UNVERIFIED","EXPORT_TYPE_NOT_NATIVE_EVIDENCE"]);
}
