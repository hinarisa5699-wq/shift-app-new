import os
import secrets
import shutil
from pathlib import Path
from typing import Optional, Union

BASE_DIR = Path(__file__).resolve().parent


def resolve_database_path(base_dir: Optional[Union[str, os.PathLike]] = None) -> str:
    """更新時に上書きされにくい親ディレクトリ側のDBを優先する。"""
    explicit_path = os.environ.get("SHIFT_APP_DB_PATH")
    if explicit_path:
        db_path = Path(explicit_path).expanduser()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return str(db_path)

    resolved_base_dir = Path(base_dir).resolve() if base_dir is not None else BASE_DIR
    preferred_path = resolved_base_dir.parent / "shift.db"
    legacy_path = resolved_base_dir / "shift.db"

    if preferred_path.exists():
        return str(preferred_path)

    if legacy_path.exists():
        try:
            if preferred_path != legacy_path:
                shutil.copy2(legacy_path, preferred_path)
                return str(preferred_path)
        except OSError:
            return str(legacy_path)

    return str(preferred_path)


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{resolve_database_path()}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
