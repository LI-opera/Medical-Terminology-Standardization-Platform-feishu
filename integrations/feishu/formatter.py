"""Render only backend facts, in plain text; no extra LLM call."""

LABELS = {'CODED': '已映射到标准概念', 'WITHHELD': '扩写完成，暂不编码',
          'NOT_EXPANDED': '未能可靠扩写', 'ABSTAIN': '放弃判断，需复核',
          'PENDING': '结果尚未完成'}


def safe(value):
    # Feishu's text messages can interpret at tags. Break markup explicitly.
    return str(value if value is not None else '未提供').replace('<', '＜').replace('>', '＞')


def render(result):
    if not isinstance(result, dict) or not isinstance(result.get('expanded_text'), str) or not isinstance(result.get('mapping_states'), list):
        raise ValueError('invalid_response')
    states = result['mapping_states']
    lines = ['医学缩写处理结果（不代表整份病例已完成实体标准化）',
             f'后端请求编号：{safe(result.get("request_id"))}',
             '扩写文本：', safe(result['expanded_text']), '', '逐项结果：']
    if not states:
        lines.append('未识别到可处理的目标缩写；请勿据此判断全文已标准化。')
    for row in states:
        if not isinstance(row, dict):
            raise ValueError('invalid_response')
        status = row.get('status')
        lines.append(f'{safe(row.get("abbreviation"))} → {safe(row.get("expansion"))}')
        lines.append(f'状态：{LABELS.get(status, "状态未识别")} [{safe(status)}]')
        concept = row.get('chosen_concept')
        if status == 'CODED' and isinstance(concept, dict) and concept:
            lines.extend([f'概念名称：{safe(concept.get("concept_name"))}',
                          f'概念代码：{safe(concept.get("concept_code"))}',
                          f'内部概念 ID：{safe(concept.get("concept_id"))}'])
            if concept.get('vocabulary_id'):
                lines.append(f'词汇表：{safe(concept["vocabulary_id"])}')
        elif status == 'CODED':
            lines.append('后端缺少概念详情，请复核；不补造编码。')
        failure = row.get('failure')
        if isinstance(failure, dict):
            lines.append('原因类型：' + safe(failure.get('type', '未提供')))
        lines.append('')
    return '\n'.join(lines)


def chunks(text, job_id, limit=2400):
    """UTF-8 bounded text chunks, plus small numbered envelope (<4KB JSON)."""
    parts, current, size = [], [], 0
    for char in text:
        width = len(char.encode('utf-8'))
        if size + width > limit:
            parts.append(''.join(current))
            current, size = [], 0
        current.append(char)
        size += width
    if current:
        parts.append(''.join(current))
    return [f'任务 {job_id} [{i}/{len(parts)}]\n{part}' for i, part in enumerate(parts, 1)]
