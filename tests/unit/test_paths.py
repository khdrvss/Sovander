from sovander import paths


def test_folders_stay_inside_override(tmp_path, monkeypatch):
    monkeypatch.setenv("SOVANDER_HOME", str(tmp_path))
    assert paths.data_dir().is_relative_to(tmp_path)
    assert paths.cache_dir().is_relative_to(tmp_path)
    assert paths.data_dir() != paths.cache_dir()


def test_xdg_folders_used_without_override(tmp_path, monkeypatch):
    monkeypatch.delenv("SOVANDER_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert paths.data_dir() == tmp_path / "data" / "sovander"
    assert paths.cache_dir() == tmp_path / "cache" / "sovander"
