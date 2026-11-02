# 2026-11-02 项目身份一次性切换

本目录是迁移资料，不是应用或日常发布入口。正常运行只接受 `qingda`；这里的旧名称仅用于明确的资源改名和失败恢复，不提供模块别名、旧布局开关或自动回退。

当前来源为 `wechat_manager` 数据库、四个同前缀角色和项目专用评测库，目标为 `qingda`。同名目标冲突、对象 OID/属性漂移、MD5 角色密码或未归属资源均停止。SCRAM 密码不变，角色成员关系、对象身份、业务 schema 59及历史数据保持；回调和网页 URL 不变。登录只识别 `qingda_session`，不转接旧 Cookie、不清空服务器会话。

## 冻结与执行

先核对生产源码、配置及网关基线，以已提交且 CI 通过的代码生成带 `project_identity: "qingda"` 的发布包。完整旧源码、配置、服务、密钥、依赖恢复材料和本目录工具须保存在项目备份根的独立保护目录。

```sh
# 只准备备份、哈希、资源清单和独立运行环境，不切换业务。
/opt/qingda/.venv/bin/python <已上传工具目录>/identity_cutover.py \
  --state /var/backups/qingda/identity-<提交> --prepare \
  --bundle <发布包> --sha256 <发布包SHA256>

# 必须从冻结副本及独立解释器执行，不能依赖将被替换的应用环境。
/var/lib/qingda-identity/identity-<提交>/venv/bin/python \
  /var/backups/qingda/identity-<提交>/tools/identity_cutover.py \
  --state /var/backups/qingda/identity-<提交> --apply
```

持有发布锁后暂停定时器、冻结管理台写入、复制回调哈希白名单，再将原回调路由切至独立缓冲。独立缓冲环境、白名单和文件位于 `/var/lib/qingda-identity/identity-<提交>`，由保护目录记录其引用，备份根维持 root:0700。缓冲以 `qingda` 系统身份运行，单请求16 MiB，总量512 MiB，保留1 GiB空闲；文件和父目录持久化成功才返回200，满额或写失败返回503。

回调记录包含稳定 ID、用户 ID、接收时间、原始正文和摘要。恢复使用 `qingda_migration_audit.callback_imports` 唯一收据，与业务接收处于同一事务并获取相同业务接收锁；数据库已提交而进度尚未记录时，重复执行也不会再次导入。原始缓冲文件保留于受恢复清单保护的独立缓冲目录，不当作日志公开。

停止业务后禁止目标库连接，按 OID 完成库和角色原地改名；部署新包、重建最终路径的运行环境并更新连接配置。新 Web 就绪后切回网关，等待旧 Nginx Worker及缓冲在途请求退出，再进行最终回放，最后恢复业务 Worker。原停站状态和定时器启用状态继续保持。

## 中断与恢复

```sh
/var/lib/qingda-identity/identity-<提交>/venv/bin/python \
  /var/backups/qingda/identity-<提交>/tools/identity_cutover.py \
  --state /var/backups/qingda/identity-<提交> --rollback
```

只能使用同一冻结目录。OID 日志判断每项真实完成状态，不依赖阶段标签猜测；恢复原数据库名、角色名及原连接属性，再启动匹配的旧源码和环境。保留最新业务库、导入收据及回调，禁止用迁移前备份覆盖新数据。旧迁移工具、配置和源码随恢复包冻结，不依赖活动目录中已删除的旧入口。

旧备份保留原文件、校验值和原保留元数据，退出新格式自动轮转。所有失败、未验收、被引用材料保持保护。只在实际生产验收完成、无进程引用后回收本次测试/上传资源。

## 验证入口

本目录 `test_*.py` 必须在服务器隔离环境执行；改名演练使用独立 PostgreSQL 集群，`MIGRATION_TEST_ADMIN_URL` 显式指向测试端口15432。业务回归使用名称含 `test` 的 `TEST_DATABASE_URL`。覆盖角色/OID、连接属性、冲突、中断恢复、双进程互斥、缓冲满额、写入失败、已提交回放和退出屏障。

生产验收只读核对账号、聊天、凭据、媒体、版本与哈希；完成新格式日备及隔离恢复。用户重新登录和微信实收未触发时，不记为通过。历史来源、原工具及既有验收记录不改写为此次结果。
