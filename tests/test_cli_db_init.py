from contextlib import contextmanager

from src.cli.main import crawl_cmd, export_cmd, stats_cmd


class _Config:
    def get(self, key):
        values = {
            "storage.database_url": "sqlite:///:memory:",
            "http.user_agent": None,
        }
        assert key in values
        return values[key]


class _Query:
    def filter(self, *args, **kwargs):
        return self

    def count(self):
        return 0

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
        self.session_opened = False
        self.url = url
        self.__class__.instances.append(self)

    def init_db(self):
        self.initialized = True

    @contextmanager
    def get_session(self):
        assert self.initialized, "CLI must migrate/init before opening a session"
        self.session_opened = True
        yield _Session()


class _Pipeline:
    def __init__(self, *args, **kwargs):
        pass

    async def crawl_queued_urls(self, **kwargs):
        return None


class _Exporter:
    def __init__(self, session):
        pass

    def export_to_jsonl(self, *args, **kwargs):
        return 0


def test_crawl_export_and_stats_initialize_database_before_session(monkeypatch, tmp_path):
    monkeypatch.setattr("src.cli.main.DatabaseManager", _DatabaseManager)
    monkeypatch.setattr("src.cli.main.CrawlPipeline", _Pipeline)
    monkeypatch.setattr("src.storage.exporter.Exporter", _Exporter)

    obj = {"config": _Config()}
    crawl_cmd.callback.__wrapped__(obj, source=None)
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
    assert all(manager.initialized and manager.session_opened for manager in _DatabaseManager.instances)
