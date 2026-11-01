"""引用只信任同账号同会话的持久消息，不采用回调内嵌的引用正文作为真值。"""


def assistant_reply(conn, aid, cid, reference_id, at):
    if not reference_id:
        return None
    row = conn.execute(
        "SELECT t.* FROM tasks t LEFT JOIN message_views v ON v.key='t:'||t.id WHERE t.account_id=%s AND t.conversation_id=%s AND t.kind='auto' AND t.state='accepted' AND t.completed_at<=%s AND CAST((t.response_json #>> '{data,newMsgId}') AS TEXT)=%s AND coalesce(v.visible,1)=1 AND coalesce(v.revoked,0)=0 AND NOT EXISTS(SELECT 1 FROM messages m JOIN message_views mv ON mv.key=('m:' || lpad((m.id)::text,20,'0')) WHERE m.account_id=t.account_id AND m.conversation_id=t.conversation_id AND m.platform_id=%s AND (mv.revoked=1 OR mv.visible=0)) ORDER BY t.completed_at DESC LIMIT 1",
        (aid, cid, at, reference_id, reference_id),
    ).fetchone()
    return dict(row) if row else None
