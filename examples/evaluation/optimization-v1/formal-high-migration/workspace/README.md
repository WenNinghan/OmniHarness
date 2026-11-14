# formal-high-migration

修复v1→v2 SQLite迁移及兼容API。v1 items(id INTEGER PRIMARY KEY,name TEXT,status TEXT)，user_version=1；v2 items(id,title TEXT NOT NULL,done INTEGER NOT NULL CHECK0/1)，user_version=2。open→0/done→1，保留id和Unicode名称；非法status或NULL name拒绝迁移ValueError且schema/版本/所有数据完整回滚。空库version0创建v2；重复迁移无变化；未知版本拒绝。storage.connect返回可用连接；list_items按id排序输出id/title/done(bool)；add_item拒绝空/非字符串并返回新id。compat.list_legacy保留id/name/status旧字典格式，add_legacy可用。CLI db list / db add TITLE，成功exit0输出JSON或id；错误exit2并stderr INVALID_STORE。legacy.py为公开fixture辅助也须保留seed行为。

公开检查：`python -B smoke.py`。公开检查只覆盖基础用例。
只修改：cli.py, compat.py, legacy.py, migration.py, schema.py, storage.py。不要修改 smoke/README，不安装依赖或联网。
