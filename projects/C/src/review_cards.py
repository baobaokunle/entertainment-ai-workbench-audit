"""Render local cards without discarding human decisions or media evidence."""
import re


def escape_note(value):
    text = str(value).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    return re.sub(r'([\\`*_{}\[\]()#!|])', r'\\\1', text)


def merge_evidence(first, second, label):
    if first and second and first != second:
        raise ValueError(label + '_conflict')
    return first or second


def review_state(card, review=None):
    review = review or {}
    if review and review.get('case_id') != card['case_id']:
        raise ValueError('review_case_mismatch')
    previous = card.get('review_status')
    decision = review.get('decision', previous or 'pending')
    if previous and previous != decision:
        raise ValueError('review_decision_conflict')
    labels = {'pending': '待审核', 'excluded': '原回复排除',
              'approved_candidate': '用户认可候选（不等于逐句全部认可）'}
    if decision not in labels:
        raise ValueError('unsupported_review_decision')
    nested = card.get('user_review', {})
    conditions = merge_evidence(review.get('conditions', ''), nested.get('conditions', ''), 'review_conditions')
    usage_scope = merge_evidence(review.get('usage_scope', ''), card.get('usage_scope', ''), 'review_scope')
    if usage_scope == 'object_specific' and not conditions.strip():
        raise ValueError('object_specific_conditions_required')
    confirmed_roles = {'user_confirmed_anchor_account_A': 'A', 'user_confirmed_anchor_account_B': 'B'}
    card_side = confirmed_roles.get(card.get('account_role'))
    review_side = review.get('account_reply_side')
    if review_side not in (None, '', 'pending', 'A', 'B'):
        raise ValueError('unsupported_account_side')
    if review_side not in ('A', 'B'):
        review_side = None
    confirmed_side = merge_evidence(card_side, review_side, 'account_role')
    if review_side and not card_side and review.get('account_side_evidence') != 'user_confirmed':
        raise ValueError('account_side_evidence_missing')
    highlighted = merge_evidence(review.get('reply_line_numbers', []),
                                 nested.get('reply_line_numbers', card.get('reply_line_numbers', [])),
                                 'reply_line_numbers')
    if (not isinstance(highlighted, list) or len(highlighted) != len(set(highlighted)) or
            any(type(i) is not int or not 1 <= i <= len(card['messages']) for i in highlighted)):
        raise ValueError('invalid_reply_line_numbers')
    return {'decision': decision, 'label': labels[decision], 'conditions': conditions,
            'usage_scope': usage_scope, 'confirmed_side': confirmed_side, 'reply_line_numbers': highlighted,
            'feedback': merge_evidence(review.get('user_feedback', ''), nested.get('feedback', ''), 'feedback'),
            'guidance': merge_evidence(review.get('revision_guidance', ''), nested.get('revision_guidance', ''), 'guidance'),
            'context_extension': merge_evidence(review.get('user_context_extension', ''),
                                                card.get('user_context_extension', ''), 'context_extension')}


def media_annotations(card, review=None):
    annotations = {}
    for index, message in enumerate(card['messages'], 1):
        if message.get('user_annotation'):
            item = message['user_annotation']
            if item.get('line') != index:
                raise ValueError('media_line_mismatch')
            annotations[index] = dict(item)
    for item in (review or {}).get('message_annotations', []):
        index = item.get('line')
        if index in annotations and annotations[index] != item:
            raise ValueError('media_annotation_conflict')
        annotations[index] = dict(item)
    for index, item in annotations.items():
        if type(index) is not int or not 1 <= index <= len(card['messages']):
            raise ValueError('media_line_out_of_range')
        message = card['messages'][index - 1]
        if (item.get('speaker') != message['speaker'] or item.get('time') != message['time']):
            raise ValueError('media_message_mismatch')
        if not (item.get('evidence') == 'user_confirmed' and
                item.get('message_kind') == 'sticker' and
                item.get('text_is_typed_reply') is False):
            raise ValueError('unsupported_media_annotation')
    return annotations


def render_card(card, review=None):
    state = review_state(card, review)
    annotations = media_annotations(card, review)
    side = state['confirmed_side']
    other = 'B' if side == 'A' else 'A'
    role = (f'{side}为用户确认的主播账号；{other}为本会话另一说话方。' if side else
            'A为跨会话重复出现的账号候选；B为本会话另一说话方。账号归属待确认。')
    lines = [f"## {escape_note(card['case_id'])} · {escape_note(card['conversation_alias'])}", '',
             f"时间：{escape_note(card['start'])} 至 {escape_note(card['end'])}（北京时间）｜{len(card['messages'])}条", '',
             '角色：' + role + '不区分具体工作人员。', '',
             '审核：' + state['label'], '',
             '会话性质和平台状态仍未可靠核实。认可仅供后续候选审核，不自动批准每句话。', '',
             '媒介：未单独标注的消息仍未核实，导出文本字段不能证明实际为打字；用户确认的表情包单独显示。', '',
             '规则线索：' + escape_note('、'.join(card['scene_tags'])) + '。仅关键词提示。', '',
             '上下文边界：' + escape_note(card['boundary_note']), '']
    if state['usage_scope'] == 'object_specific':
        lines.extend(['**使用范围：必须分对象使用，不能作为统一角色。**', ''])
    if state['reply_line_numbers']:
        lines.extend(['用户标记的回复行号：' + '、'.join(map(str, state['reply_line_numbers'])) +
                      '。此标记不代表仅认可这些句子，仍结合整卡结论理解。', ''])
    for label, value in (('适用条件', state['conditions']), ('用户评价', state['feedback']),
                         ('改进方向', state['guidance'])):
        if value:
            lines.extend([label + '：' + escape_note(value), ''])
    for index, message in enumerate(card['messages'], 1):
        suffix = ' · 表情包（用户确认）' if index in annotations else ''
        lines.extend([f"**{index:02d} · {escape_note(message['speaker'])} · {escape_note(message['time'])}{suffix}**"])
        if index in annotations:
            lines.extend(['', '此条显示文字仅作表情包内容线索，不是打字回复；未加载原媒体。', ''])
        # masked_text is already escaped by the bounded local sampler.
        lines.extend(['> ' + message['masked_text'].replace('\n', '\n> '), ''])
    if state['context_extension']:
        lines.extend(['用户补充（未读取额外消息核验）：' + escape_note(state['context_extension']), ''])
    if state['decision'] == 'pending':
        lines.extend(['- 审核：待审核（认可 / 修改 / 排除）',
                      '- 账号回复方：' + (side + '（用户已确认）' if side else '待确认'),
                      '- 可保留的回复行号：', '- 修改或适用条件：', ''])
    return '\n'.join(lines)
