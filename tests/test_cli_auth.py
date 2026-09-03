"""管理员初始化命令测试。"""

import importlib


def test_init_admin_creates_account(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'auth.db'}")
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    import app.config as cfg
    import app.db as db_mod
    import app.repository as repo_mod
    importlib.reload(cfg); importlib.reload(db_mod); importlib.reload(repo_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)
    import app.cli as cli_mod
    monkeypatch.setattr(cli_mod, "_ensure_schema", lambda: None)
    monkeypatch.setattr(cli_mod, "SessionLocal", db_mod.SessionLocal)
    from app.cli import cmd_init_admin
    result = cmd_init_admin(["--login", "owner", "--display-name", "店主", "--password", "strong-pass-123", "--tenant-key", "shop-test"])
    assert result == 0
    with db_mod.SessionLocal() as db:
        user = db.query(db_mod.UserAccount).filter_by(login="owner").one()
        assert user.password_hash != "strong-pass-123"
        assert db.query(db_mod.WorkspaceMembership).filter_by(user_id=user.id, role="admin").count() == 1
    assert "strong-pass-123" not in capsys.readouterr().out


def test_init_admin_rejects_duplicate(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'auth.db'}")
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    import app.config as cfg
    import app.db as db_mod
    import app.repository as repo_mod
    importlib.reload(cfg); importlib.reload(db_mod); importlib.reload(repo_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)
    import app.cli as cli_mod
    monkeypatch.setattr(cli_mod, "_ensure_schema", lambda: None)
    monkeypatch.setattr(cli_mod, "SessionLocal", db_mod.SessionLocal)
    from app.cli import cmd_init_admin
    args = ["--login", "owner", "--display-name", "店主", "--password", "strong-pass-123", "--tenant-key", "shop-test"]
    assert cmd_init_admin(args) == 0
    assert cmd_init_admin(args) == 1
