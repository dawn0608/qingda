-- 仅显式升级执行。执行前停止旧进程并保护已恢复验证的原始备份。
-- 不使用 CASCADE；意外业务依赖必须使整份迁移回滚。
DROP TABLE IF EXISTS bridge_portal_actions, bridge_portal_items,
    bridge_portal_verifications, bridge_thread_states, bridge_actions,
    bridge_artifacts, bridge_messages, bridge_items, bridge_files,
    bridge_computer_pairings, bridge_computer_credentials, bridge_clients,
    bridge_downloads, bridge_mobile_pairings, bridge_mobile_sessions,
    bridge_launches, login_attempts;
DROP FUNCTION IF EXISTS register_credential_route();
DROP TABLE IF EXISTS credential_routes;

DO $$
DECLARE
    uid bigint;
    message record;
    new_content_id bigint;
    previous_scope text := current_setting('app.user_id', true);
BEGIN
    FOR uid IN SELECT id FROM users LOOP
        PERFORM set_config('app.user_id', uid::text, true);
        -- 实际已发送的微信正文不随专属任务删除，转存为普通历史内容。
        FOR message IN
            SELECT m.id, t.content FROM message_records m
            JOIN tasks t ON t.user_id=m.user_id AND t.id=m.outbound_task_id
            WHERE t.kind='bridge' AND m.content_id IS NULL
        LOOP
            SELECT coalesce(max(id), 0)+1 INTO new_content_id FROM message_contents;
            INSERT INTO message_contents(id,text) VALUES(new_content_id,message.content);
            UPDATE message_records SET content_id=new_content_id WHERE id=message.id;
        END LOOP;
        UPDATE message_records m SET outbound_task_id=NULL
            FROM tasks t WHERE t.user_id=m.user_id AND t.id=m.outbound_task_id AND t.kind='bridge';
        DELETE FROM tasks WHERE kind='bridge';
        DELETE FROM settings WHERE key='bridge' OR key LIKE 'bridge\_%' ESCAPE '\';
        DELETE FROM history_dedup WHERE scope='bridge' OR scope LIKE 'bridge\_%' ESCAPE '\';
        DELETE FROM audit WHERE action='bridge' OR action LIKE 'bridge\_%' ESCAPE '\';
    END LOOP;
    PERFORM set_config('app.user_id', coalesce(previous_scope, ''), true);
END $$;
DELETE FROM auth_events WHERE kind='bridge' OR kind LIKE 'bridge\_%' ESCAPE '\';
INSERT INTO schema_versions VALUES(58);
