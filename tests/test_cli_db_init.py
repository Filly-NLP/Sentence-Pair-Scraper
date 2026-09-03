from contextlib import contextmanager

from src.cli.main import crawl_cmd, export_cmd, stats_cmd


class _Config:
    def get(self, key, default=None):
        values = {
            "storage.database_url": "sqlite:///:memory:",
            "http.user_agent": None,
        }
        return values.get(key, default)


class _Query:
    def filter(self, *args, **kwargs):
        return self

    def count(self):
        return 1

    def group_by(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return []

    def first(self):
        return None


class _Session:
    def query(self, *args, **kwargs):
        return _Query()


class _DatabaseManager:
    instances = []

    def __init__(self, url):
        self.initialized = False
        self.read_only = False
        self.session_opened = False
        self.url = url
        self.__class__.instances.append(self)

    def init_db(self):
        self.initialized = True

    @classmethod
    def read_only(cls, url):
        manager = cls(url)
        manager.read_only = True
        return manager

    @contextmanager
    def get_session(self):
        assert self.initialized or self.read_only, "CLI must initialize or open read-only before a session"
        self.session_opened = True
        yield _Session()


class _Pipeline:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def crawl_queued_urls(self, **kwargs):
        self.__class__.calls.append(kwargs)
        return None


class _Exporter:
    def __init__(self, session):
        pass

    def export_to_jsonl(self, *args, **kwargs):
        return 0


def test_mutating_and_read_only_cli_commands_open_the_expected_database_mode(monkeypatch, tmp_path):
    monkeypatch.setattr("src.cli.main.DatabaseManager", _DatabaseManager)
    monkeypatch.setattr("src.cli.main.CrawlPipeline", _Pipeline)
    monkeypatch.setattr("src.storage.exporter.Exporter", _Exporter)

    obj = {"config": _Config()}
    _Pipeline.calls.clear()
    crawl_cmd.callback.__wrapped__(
        obj,
        source="test-source",
        limit=2,
        discovery_method="default",
    )
    export_cmd.callback.__wrapped__(
        obj,
        output=str(tmp_path / "corpus.jsonl"),
        format="jsonl",
        source=None,
        from_date=None,
        to_date=None,
        append=False,
        auto_version=False,
    )
    stats_cmd.callback.__wrapped__(obj)

    assert len(_DatabaseManager.instances) == 3
    assert _DatabaseManager.instances[0].initialized is True
    assert _DatabaseManager.instances[0].read_only is False
    assert _DatabaseManager.instances[1].read_only is True
    assert _DatabaseManager.instances[2].read_only is True
    assert len(_Pipeline.calls) == 1
    assert _Pipeline.calls[0]["source_id"] == "test-source"
    assert _Pipeline.calls[0]["limit"] == 2
    assert _Pipeline.calls[0]["discovery_method"] == "default"
