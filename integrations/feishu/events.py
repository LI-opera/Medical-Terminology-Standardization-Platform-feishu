"""Validate SDK event dictionaries. Never infer a mention from display names."""

from dataclasses import dataclass
import json
import re

HELP = '发送英文纯文本即可分析医学缩写；群聊请从候选列表真正 @ 本机器人。每条消息独立处理。仅提供缩写扩写与概念映射，不支持文件或连续追问。'


@dataclass(frozen=True)
class Message:
    tenant: str
    message_id: str
    chat_id: str
    user_id: str
    text: str
    notice: str = ''


def unpack(raw, app_id):
    if not isinstance(raw, dict):
        return None
    header, event = raw.get('header'), raw.get('event')
    if not isinstance(header, dict) or not isinstance(event, dict):
        return None
    if header.get('event_type') != 'im.message.receive_v1' or header.get('app_id') != app_id:
        return None
    sender, msg = event.get('sender'), event.get('message')
    if not isinstance(sender, dict) or not isinstance(msg, dict) or sender.get('sender_type') != 'user':
        return None
    sid = sender.get('sender_id') or {}
    if not isinstance(sid, dict):
        return None
    tenant = header.get('tenant_key') or sender.get('tenant_key')
    if sender.get('tenant_key') and sender['tenant_key'] != tenant:
        return None
    user = sid.get('open_id')
    if not all(isinstance(x, str) and x for x in (tenant, user, msg.get('message_id'), msg.get('chat_id'))):
        return None
    return tenant, user, msg


def mentions(msg):
    items = msg.get('mentions') or []
    if not isinstance(items, list):
        return []
    return [x for x in items if isinstance(x, dict) and isinstance(x.get('id'), dict)
            and isinstance(x.get('key'), str) and re.fullmatch(r'@_user_\d+', x['key'])]


def text_content(msg):
    try:
        content = json.loads(msg.get('content', '{}'))
        text = content.get('text') if isinstance(content, dict) else None
        return text if isinstance(text, str) else ''
    except (ValueError, TypeError):
        return ''


def parse(raw, cfg):
    data = unpack(raw, cfg.app_id)
    if not data:
        return None
    tenant, user, msg = data
    if tenant != cfg.tenant or user not in cfg.users or user == cfg.bot_id:
        return None
    kind = msg.get('chat_type')
    text = text_content(msg)
    if kind == 'group':
        if msg['chat_id'] not in cfg.chats:
            return None
        own = [x for x in mentions(msg) if x['id'].get('open_id') == cfg.bot_id]
        if not own:
            return None
        if msg.get('message_type') == 'text' and not any(x['key'] in text for x in own):
            return None
        for item in own:
            text = text.replace(item['key'], '')
    elif kind != 'p2p':
        return None
    text = text.strip()
    notice = ''
    if msg.get('message_type') != 'text':
        notice = '当前仅支持粘贴英文纯文本，请不要上传图片、文件或富文本。'
    elif text.lower() in ('帮助', '/help', 'help'):
        notice = HELP
    elif not text:
        notice = '请在消息中附上英文文本；仅 @ 机器人不会启动分析。'
    elif len(text) > cfg.max_chars:
        notice = f'文本超过 {cfg.max_chars} 字符，请缩短后发送；本次没有调用分析。'
    elif not re.search(r'[A-Za-z]', text):
        notice = '请粘贴英文临床文本，或发送“帮助”。'
    return Message(tenant, msg['message_id'], msg['chat_id'], user, text, notice)


def discovery_record(raw, app_id, token):
    data = unpack(raw, app_id)
    if not data:
        return None
    tenant, user, msg = data
    if msg.get('message_type') != 'text' or msg.get('chat_type') not in ('p2p', 'group'):
        return None
    text = text_content(msg)
    items = mentions(msg)
    for item in items:
        text = text.replace(item['key'], '')
    if text.strip() != f'/register {token}':
        return None
    # Only IDs are printed, never message bodies, names or secrets.
    return {'tenant_key': tenant, 'user_open_id': user, 'chat_id': msg['chat_id'],
            'chat_type': msg['chat_type'],
            'mentioned_open_ids': [x['id'].get('open_id') for x in items]}
