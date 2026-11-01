"""测试库地址由服务器隔离夹具提供，路径只作隔离资源名称。"""

resolve = None


def database_url(key):
    if isinstance(key, str) and key.startswith(("postgresql://", "postgres://")):
        return key
    if resolve is None:
        raise RuntimeError("需要服务器隔离 PostgreSQL 测试夹具")
    return resolve(key)
