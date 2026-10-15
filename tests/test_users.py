from pathlib import Path

import pytest

from potok.users import DEFAULT_NAME, DEFAULT_PASSWORD, UserError, UserStore


def test_first_user_is_potok_with_the_same_password(tmp_path: Path) -> None:
    store = UserStore(tmp_path / "users.db")
    assert store.list_users() == (DEFAULT_NAME,)
    assert store.authenticate(DEFAULT_NAME, DEFAULT_PASSWORD)
    assert not store.authenticate(DEFAULT_NAME, "other")
    again = UserStore(tmp_path / "users.db")
    assert again.list_users() == (DEFAULT_NAME,)


def test_add_change_password_and_keep_last_user(tmp_path: Path) -> None:
    store = UserStore(tmp_path / "users.db")
    store.add_user("anna", "secret1")
    assert store.list_users() == ("anna", "potok")
    store.set_password("potok", "new-pass")
    assert store.authenticate("potok", "new-pass")
    assert not store.authenticate("potok", DEFAULT_PASSWORD)
    store.remove_user("anna")
    with pytest.raises(UserError, match="последнего"):
        store.remove_user("potok")
    with pytest.raises(UserError, match="уже есть"):
        store.add_user("potok", "secret1")
