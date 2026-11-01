import shutil
import uuid
from pathlib import Path

from app.config import get_settings


class LocalStorage:
    """Files on disk under random names.

    The original filename only ever lives in the database, so nothing a user
    uploads can choose its own path. Swapping in S3 means replacing this class.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        resolved = (self.root / key).resolve()
        if not resolved.is_relative_to(self.root.resolve()):
            raise ValueError("Storage key escapes the storage directory")
        return resolved

    def new_key(self, suffix: str) -> str:
        name = uuid.uuid4().hex
        return f"{name[:2]}/{name}{suffix}"

    def put_bytes(self, data: bytes, suffix: str) -> str:
        key = self.new_key(suffix)
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def put_file(self, source: Path, suffix: str) -> str:
        key = self.new_key(suffix)
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, path)
        return key

    def preview_dir(self, key: str) -> Path:
        return self.path(f"previews/{Path(key).stem}")


def get_storage() -> LocalStorage:
    return LocalStorage(get_settings().storage_dir)
