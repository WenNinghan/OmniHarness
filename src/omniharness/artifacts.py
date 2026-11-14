"""Immutable local evidence. Unreferenced files after DB rollback are harmless orphans."""
from __future__ import annotations

import codecs
import hashlib
import os
import stat
from pathlib import Path
from uuid import uuid4

from .m4_services import artifact_output
from .models import ArtifactVersion, Run
from .services import ServiceError, require


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def put(self, db, *, run_id, kind, name, content, operation_id=None, workspace_id=None, metadata=None):
        run = require(db, Run, run_id)
        if workspace_id is not None and workspace_id != run.workspace_id:
            raise ServiceError("ARTIFACT_SCOPE", "工件不属于当前工作区。", 403)
        data = content.encode("utf-8") if isinstance(content, str) else content
        if not isinstance(data, bytes) or len(data) > 100 * 1024 * 1024:
            raise ServiceError("ARTIFACT_SIZE", "工件超过存储上限。", 413)
        identity = str(uuid4())
        path = self.root / identity
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        row = ArtifactVersion(id=identity, run_id=run_id, operation_id=operation_id,
            workspace_id=workspace_id, kind=kind, name=name[:200], sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data), storage_path=identity, metadata_json=metadata or {})
        db.add(row)
        db.flush()
        return artifact_output(row)

    def read(self, row, *, run_id=None, offset=0, limit=65536):
        if run_id is not None and row.run_id != run_id:
            raise ServiceError("ARTIFACT_SCOPE", "不能读取其他任务的工件。", 403)
        if row.storage_path != row.id or not 0 <= offset <= row.size_bytes or not 1 <= limit <= 1024 * 1024:
            raise ServiceError("ARTIFACT_RANGE", "工件范围无效。", 422)
        path = self.root / row.id
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ServiceError("ARTIFACT_INVALID", "工件存储身份不符合约定。", 409)
        with path.open("rb") as stream:
            # Evidence integrity is verified before handing any slice to a model/UI.
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != row.sha256 or info.st_size != row.size_bytes:
                raise ServiceError("ARTIFACT_CHANGED", "已保存的工件内容发生变化。", 409)
            stream.seek(offset)
            data = stream.read(limit)
        return data

    def read_text_page(self, row, *, run_id=None, offset=0, limit=65536):
        # Byte cursors remain stable, but a page never splits a UTF-8 character.
        data = self.read(row, run_id=run_id, offset=offset, limit=min(1024 * 1024, limit + 3))
        try:
            decoder = codecs.getincrementaldecoder('utf-8')('strict')
            selected = data[:limit]
            content = decoder.decode(selected, final=offset + len(selected) == row.size_bytes)
            consumed = len(selected) - len(decoder.getstate()[0])
            for byte in data[limit:]:
                if consumed or not data:
                    break
                selected += bytes([byte])
                content += decoder.decode(bytes([byte]), final=offset + len(selected) == row.size_bytes)
                consumed = len(selected) - len(decoder.getstate()[0])
            return content, offset + consumed
        except UnicodeDecodeError:
            raise ServiceError('ARTIFACT_NOT_TEXT', '工件不是完整的 UTF-8 文本，或分页起点不在字符边界。', 422) from None
