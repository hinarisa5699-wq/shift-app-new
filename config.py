import logging
import os
import secrets
import shutil
from pathlib import Path
from typing import Optional, Union

BASE_DIR = Path(__file__).resolve().parent

logger = logging.getLogger(__name__)


def _ensure_writable_dir(directory: Path) -> bool:
    """ディレクトリを作成し、実際に書き込めるか検証する。
    作成不可・書き込み不可（権限不足／永続ディスク未マウント等）なら False。"""
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    probe = directory / ".write_test"
    try:
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def resolve_database_path(base_dir: Optional[Union[str, os.PathLike]] = None) -> str:
    """更新時に上書きされにくい親ディレクトリ側のDBを優先する。"""
    resolved_base_dir = Path(base_dir).resolve() if base_dir is not None else BASE_DIR
    # 書き込み可能なローカルのフォールバック先（永続ディスク未マウント時の一時保存先）
    fallback_path = resolved_base_dir / "instance" / "shift.db"

    explicit_path = os.environ.get("SHIFT_APP_DB_PATH")
    if explicit_path:
        db_path = Path(explicit_path).expanduser()
        if _ensure_writable_dir(db_path.parent):
            return str(db_path)
        # /var/data 等が未マウント／権限不足で書き込めない → クラッシュさせず一時パスへ
        logger.warning(
            "SHIFT_APP_DB_PATH=%s の親フォルダを作成・書き込みできません"
            "（永続ディスク未マウントの可能性）。一時パス %s を使用します。"
            "再起動・再デプロイでこの一時データは失われます。",
            explicit_path,
            fallback_path,
        )
        _ensure_writable_dir(fallback_path.parent)
        return str(fallback_path)

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
