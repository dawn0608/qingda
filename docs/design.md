# 架构

项目为模块化单体。Web提供验证码认证、账号与助手配置、逐用户回调；Worker处理收件箱、联系人核实、模型与持久发送队列。唯一业务数据库为PostgreSQL，结构显式升级，业务启动只核验版本。

## 模块职责

- `auth`、`contact_bindings`和`tenancy`维护用户、联系方式、会话及RLS范围。
- `callbacks`、`protocol`和`history_identity`维护回调解析、撤回、去重和ID高水位。
- `conversation_options`、`contact_profiles`和关系核实模块提供当前账号的身份及关系证据。
- `private_replies`、`dialogue_windows`、`reply_queue`组织收集、输入修订、成员范围、任务及发送间隔。
- `conversation_history`、`history_tool`和`reply_decisions`维护真实问答、原文回查及严格输出决策；处理消息关联统一保存在`tasks.handled_message_ids`。
- `incoming_media` 保存授权静态图片像素和语音转写；阿里百炼短语音 HTTP 适配器只承担 ASR，密钥和业务来源均按用户隔离，固定的 DeepSeek Flash 原生处理文本和图片并生成回复。

## 状态与隔离

任务从queued到generating、ready或preparing，再进入sending和accepted、failed或unknown。输入变化、人工接管或权限变化可取消未提交任务；进入发送边界或结果未知后不能自动重发。后台跨会话准备，同一会话互斥，同账号按答案就绪顺序串行发送，前次发送结束至少间隔1秒。

网络等待不占数据库连接；每个并发任务使用独立短事务。领取、续期和提交检查所有权，窗口、预算、容量与时间水位均持久化。

Cookie、回调密钥、逐电脑Bearer和手机会话先确定可信用户范围，RLS缺省拒绝，事务局部范围避免连接池串用。客户端声明的用户或电脑不能扩大授权。模型密钥按用户加密读取，网页与检索内容只作数据。

配置见[运维](operations.md)，数据库见[存储](postgresql-storage.md)，恢复边界见[验收](verification.md)。
