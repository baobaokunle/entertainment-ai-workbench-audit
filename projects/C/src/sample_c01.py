"""Bounded local structural sampling. Never emits identities or message text."""
from pathlib import Path
from dataclasses import dataclass
from collections import Counter, defaultdict
import datetime
import hashlib
import json
import re
import secrets
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from local_snapshot import fixed_snapshot, readonly_connection, write_json_new, plain_path
from prepare_c01 import CROOT, EXPECTED, BATCH, INPUT
from review_cards import render_card

RULE_VERSION = 'c01-structural-v1'
MAX_WINDOWS, MAX_MESSAGES, MAX_PER_WINDOW = 24, 720, 30


@dataclass(frozen=True)
class Meta:
    message: str
    conversation: str
    sender: str
    timestamp: int
    sequence: int
    eligible: bool


def segments(rows):
    """Never bridge a rejected record, conversation, or 30-minute gap."""
    run = []
    for row in rows:
        split = run and (row.conversation != run[-1].conversation or
                         row.timestamp < run[-1].timestamp or
                         row.timestamp - run[-1].timestamp > 1800)
        if split or not row.eligible:
            if run:
                yield run
            run = []
        if row.eligible:
            run.append(row)
    if run:
        yield run


def window_candidates(rows):
    result = []
    for run in segments(rows):
        for offset in range(0, len(run), MAX_PER_WINDOW):
            part = run[offset:offset + MAX_PER_WINDOW]
            switches = sum(a.sender != b.sender for a, b in zip(part, part[1:]))
            if len(part) >= 8 and len({r.sender for r in part}) == 2 and switches >= 3:
                result.append(part)
    return result


def select_windows(by_conversation):
    """12 conversations, two non-overlapping windows each; stratify by volume."""
    candidates = {key: window_candidates(rows) for key, rows in by_conversation.items()}
    eligible = [key for key, value in candidates.items() if len(value) >= 2]
    eligible.sort(key=lambda key: (len(by_conversation[key]), hashlib.sha256(key.encode()).hexdigest()))
    selected = []
    for bucket in range(3):
        group = eligible[len(eligible) * bucket // 3:len(eligible) * (bucket + 1) // 3]
        group.sort(key=lambda key: hashlib.sha256((RULE_VERSION + key).encode()).hexdigest())
        selected.extend(group[:4])
    windows = []
    for key in selected:
        choices = candidates[key]
        # Spread time using the earliest and latest qualifying windows.
        windows.extend([choices[0], choices[-1]])
    if len(windows) > MAX_WINDOWS or sum(map(len, windows)) > MAX_MESSAGES:
        raise ValueError('sample_limit_exceeded')
    ids = [row.message for window in windows for row in window]
    if len(ids) != len(set(ids)):
        raise ValueError('overlapping_windows')
    return windows, len(eligible)


def redact(text, known_values):
    """Automatic masking only. Outputs remain restricted, never cloud approved."""
    value = str(text)
    for name in sorted({v for v in known_values if isinstance(v, str) and v}, key=len, reverse=True):
        if len(name) >= 2:
            value = value.replace(name, '[称呼或标识]')
    value = re.sub(r'(?:https?://|www\.)[^\s<>]+', '[链接]', value, flags=re.I)
    value = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[邮箱]', value)
    value = re.sub(r'@[\w\u4e00-\u9fff-]+', '[提及]', value)
    value = re.sub(r'(?<!\w)(?:\+?\d[\d\s-]{5,}\d)(?!\w)', '[号码]', value)
    value = re.sub(r'[A-Za-z0-9_-]{16,}', '[长标识]', value)
    value = re.sub(r'(?:微信|wx|v信|抖音号|手机号|电话|地址|住址|我叫|真名)\s*[:：是为]?\s*[^，。！？\n]{1,70}', '[联系方式或身份描述]', value, flags=re.I)
    # Escape Markdown/HTML so message text cannot inject links, images or widgets.
    value = value.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    value = re.sub(r'([\\`*_{}\[\]()#!|])', r'\\\1', value)
    return value


SCENES = {
    '关系边界线索': ('见面', '约会', '吃醋', '微信', '联系方式'),
    '情绪回应线索': ('难过', '伤心', '焦虑', '委屈', '压力', '累了'),
    '冲突修复线索': ('对不起', '误会', '生气', '道歉', '别气'),
    '直播关联线索': ('直播', '下播', '开播', '礼物'),
    '日常接话线索': ('早安', '晚安', '吃饭', '在吗', '干嘛'),
}


def scene_tags(texts):
    joined = '\n'.join(texts)
    found = [label for label, terms in SCENES.items() if any(term in joined for term in terms)]
    return found or ['一般对话核验']


def main():
    destination = plain_path(BATCH / 'restricted_samples')
    artifacts = ['selected_contexts.json', 'case_cards.json', 'review_batch_01.md',
                 'review_batch_02.md', 'review_register.json', 'sample_complete.json']
    if any((destination / name).exists() for name in artifacts):
        raise FileExistsError('no_sample_overwrite')
    with fixed_snapshot(INPUT, EXPECTED):
        connection = readonly_connection(INPUT)
        try:
            # No body text is returned by this all-record metadata pass.
            payload = "CASE WHEN json_valid(json_extract(m.raw_data,'$.content_json')) THEN json_extract(m.raw_data,'$.content_json') ELSE '{}' END"
            sql = f"""SELECT m.msg_id,m.conv_id,m.sender_uid,m.timestamp,m.seq,
                CASE WHEN m.msg_type=1 AND json_extract(m.raw_data,'$.awe_type')=700
                  AND json_type(m.raw_data,'$.is_recalled')='integer' AND json_extract(m.raw_data,'$.is_recalled')=0
                  AND json_type(m.raw_data,'$.visible')='integer' AND json_extract(m.raw_data,'$.visible')=0
                  AND (m.ref_msg IS NULL OR m.ref_msg='')
                  AND (m.media_url IS NULL OR m.media_url='') AND (m.media_local_path IS NULL OR m.media_local_path='')
                  AND json_type({payload},'$.text')='text' AND json_extract({payload},'$.text')=m.content
                  AND COALESCE(json_extract({payload},'$.is_recalled'),0)=0
                  AND json_type({payload},'$.related_share_video') IS NULL
                  AND json_type({payload},'$.resource_url') IS NULL
                  AND json_type(m.raw_data,'$.forwarded_bodies') IS NULL
                  AND length(m.content) BETWEEN 1 AND 800 THEN 1 ELSE 0 END
                FROM messages m JOIN (SELECT conv_id FROM messages GROUP BY conv_id
                  HAVING COUNT(DISTINCT sender_uid)=2) c ON m.conv_id=c.conv_id
                ORDER BY m.conv_id,m.timestamp,m.seq,m.msg_id"""
            grouped = defaultdict(list)
            appearances = defaultdict(set)
            for row in connection.execute(sql):
                item = Meta(*row)
                grouped[item.conversation].append(item)
                appearances[item.sender].add(item.conversation)
            # This is a labelled hypothesis for human review, never an identity assignment.
            ranked = sorted(appearances, key=lambda sender: (-len(appearances[sender]), sender))
            shared_candidate = ranked[0] if ranked else None
            shared_count = len(appearances[shared_candidate]) if shared_candidate else 0
            second_count = len(appearances[ranked[1]]) if len(ranked) > 1 else 0
            if shared_count == second_count:
                raise ValueError('ambiguous_shared_candidate')
            eligible_grouped = {key: value for key, value in grouped.items()
                                if any(row.sender == shared_candidate for row in value)}
            windows, qualified = select_windows(eligible_grouped)
            # Metadata suitable for masking; never printed or stored in public outputs.
            names = set()
            for row in connection.execute('SELECT uid,nickname,unique_id FROM users'):
                names.update(value for value in row if isinstance(value, str) and value)
            cards, contexts, registry = [], [], []
            aliases = {}
            content_read_count = 0
            for index, window in enumerate(windows, 1):
                conv = window[0].conversation
                aliases.setdefault(conv, '会话-' + secrets.token_hex(3).upper())
                case_id = f'C01-{index:03d}'
                local_rows, card_rows = [], []
                for meta in window:
                    body, sec_uid = connection.execute("SELECT content,json_extract(raw_data,'$.sender_sec_uid') FROM messages WHERE msg_id=? AND conv_id=? AND sender_uid=?", (meta.message,conv,meta.sender)).fetchone()
                    content_read_count += 1
                    if content_read_count > MAX_MESSAGES:
                        raise ValueError('body_read_limit')
                    local_rows.append({'message_id':meta.message,'sender_uid':meta.sender,
                                       'timestamp':meta.timestamp,'seq':meta.sequence,'content':body})
                    stamp = datetime.datetime.fromtimestamp(meta.timestamp,datetime.timezone(datetime.timedelta(hours=8)))
                    card_rows.append({'speaker':'A' if meta.sender == shared_candidate else 'B',
                                      'time':stamp.strftime('%H:%M:%S'),
                                      'masked_text':redact(body,names | {sec_uid,meta.sender})})
                contexts.append({'case_id':case_id,'source_sha256':EXPECTED,'conversation_id':conv,
                                 'candidate_account_uid':shared_candidate,'account_role':'unverified',
                                 'operator_policy':'not_distinguished_user_requested','messages':local_rows})
                tz=datetime.timezone(datetime.timedelta(hours=8))
                card={'case_id':case_id,'conversation_alias':aliases[conv],
                      'start':datetime.datetime.fromtimestamp(window[0].timestamp,tz).isoformat(),
                      'end':datetime.datetime.fromtimestamp(window[-1].timestamp,tz).isoformat(),
                      'operator_policy':'not_distinguished_user_requested','account_role':'unverified',
                      'conversation_scope':'unknown','platform_status':'public_reference_only_unverified',
                      'deidentification':'automatic_only_local_review_required',
                      'boundary_note':'连续片段；遇30分钟间隔、不合格记录或30条上限切段。片段前后可能还有上下文，未跨缺口拼接。',
                      'scene_tags':scene_tags([r['content'] for r in local_rows]),'messages':card_rows}
                cards.append(card)
                registry.append({'case_id':case_id,'decision':'pending','account_reply_side':'pending',
                                 'reply_line_numbers':[],'revision':'','conditions':'','reviewer':'','reviewed_at':''})
            write_json_new(destination/'selected_contexts.json',contexts)
            write_json_new(destination/'case_cards.json',cards)
            write_json_new(destination/'review_register.json',registry)
            for batch_index in range(2):
                subset=cards[batch_index*12:(batch_index+1)*12]
                head=f'# C-01本机审核｜第{batch_index+1}批\n\n本批{len(subset)}张；未审核、未用于训练。正文经过自动遮蔽但仍属受限资料，请勿上传。\n\n无需判断是谁打字。先确认A/B哪方是账号回复，再按编号选择“认可 / 修改 / 排除”，填写可保留的回复行号。可在聊天中只回复编号和结论，不必粘贴正文。\n\n'
                with (destination/f'review_batch_{batch_index+1:02d}.md').open('x',encoding='utf-8') as stream:
                    stream.write(head+'\n\n'.join(render_card(card) for card in subset))
            summary={'rule_version':RULE_VERSION,'source_sha256':EXPECTED,'input':str(INPUT),
                     'two_sender_conversations':len(grouped),'shared_candidate_conversation_count':shared_count,
                     'next_candidate_conversation_count':second_count,'qualified_conversations':qualified,
                     'selected_conversations':len(aliases),'cards':len(cards),'body_messages_read':content_read_count,
                     'batch_sizes':[len(cards[:12]),len(cards[12:])],
                     'operator_policy':'not_distinguished_user_requested','account_role':'unverified',
                     'conversation_scope':'unknown','platform_status':'public_reference_only_unverified',
                     'scene_counts':dict(Counter(tag for card in cards for tag in card['scene_tags'])),
                     'at':datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
                     'metadata_selection_sql':sql,'model_calls':0,'platform_calls':0}
        finally:
            connection.close()
    write_json_new(destination/'sample_complete.json',summary)
    write_json_new(CROOT/'docs/evidence/C01_sample_summary.json',summary)
    print(json.dumps({key:summary[key] for key in ('cards','selected_conversations','body_messages_read','batch_sizes','qualified_conversations','shared_candidate_conversation_count','next_candidate_conversation_count','scene_counts')},ensure_ascii=True))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'failed':type(error).__name__}))
        raise SystemExit(1)
