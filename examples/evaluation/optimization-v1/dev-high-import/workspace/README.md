# dev-high-import

修复分批导入apply_batch(path,rows,offset)。持久JSON形状records映射、offset整数，空文件不存在时初始{}与0。每行仅id非空str/value严格int。offset为非负int且等于已处理行数，前向gap拒绝；完全落在已处理范围内的重复批次验证格式后返回原状态，不写盘；部分重叠拒绝。同id相同value可重复，不同value冲突拒绝。整批先验证，冲突/非法不改变原文件；成功原子写入并offset增加行数。CLI state offset JSON_ROWS成功输出状态JSON和0，否则INVALID_BATCH与2。

公开检查：`python -B smoke.py`。公开检查只覆盖基础用例。
只修改：cli.py, importer.py, state.py, validate.py。不要修改 smoke/README，不安装依赖或联网。
