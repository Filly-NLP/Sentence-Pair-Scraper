import socket
import threading
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.storage.models import Base
from src.sources.registry import SourceConfig, RSSFeedConfig, SitemapConfig


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch):
    """Fail loudly if a test attempts a real socket connection.

    HTTP tests must inject an ``httpx`` client/transport or patch the existing
    ``httpx.get`` seams.  ``httpx.MockTransport`` does not use these socket
    methods, so injected offline clients remain fully usable.
    """

    original_connect = socket.socket.connect
    original_socketpair = socket.socketpair
    socketpair_state = threading.local()

    def is_socketpair_fallback_connect(_sock, address):
        """Allow only the loopback connect used by socketpair's fallback."""
        if not getattr(socketpair_state, "allow_connect", False):
            return False
        if _sock.type != socket.SOCK_STREAM or _sock.proto != 0:
            return False
        if _sock.getblocking():
            return False

        if _sock.family == socket.AF_INET:
            return (
                isinstance(address, tuple)
                and len(address) == 2
                and address[0] == "127.0.0.1"
                and isinstance(address[1], int)
                and 0 < address[1] <= 65535
            )
        if hasattr(socket, "AF_INET6") and _sock.family == socket.AF_INET6:
            return (
                isinstance(address, tuple)
                and len(address) in (2, 4)
                and address[0] == "::1"
                and isinstance(address[1], int)
                and 0 < address[1] <= 65535
                and (len(address) == 2 or (address[2] == 0 and address[3] == 0))
            )
        return False

    def fail_connect(_sock, address):
        if is_socketpair_fallback_connect(_sock, address):
            return original_connect(_sock, address)
        raise AssertionError(
            "Offline test tripwire: attempted a real socket connection to "
            f"{address!r}; inject an offline HTTP transport/client or patch "
            "the existing httpx.get seam."
        )

    def fail_connect_ex(_sock, address):
        raise AssertionError(
            "Offline test tripwire: attempted a real socket connection to "
            f"{address!r}; inject an offline HTTP transport/client or patch "
            "the existing httpx.get seam."
        )

    def fail_create_connection(address, *args, **kwargs):
        raise AssertionError(
            "Offline test tripwire: attempted a real socket connection to "
            f"{address!r}; inject an offline HTTP transport/client or patch "
            "the existing httpx.get seam."
        )

    def fail_udp_send(_sock, *args, **kwargs):
        raise AssertionError(
            "Offline test tripwire: attempted outbound UDP traffic; inject an "
            "offline HTTP transport/client or patch the existing httpx.get seam."
        )

    @contextmanager
    def allow_socketpair_connect():
        previous = getattr(socketpair_state, "allow_connect", False)
        socketpair_state.allow_connect = True
        try:
            yield
        finally:
            socketpair_state.allow_connect = previous

    def socketpair_without_network(*args, **kwargs):
        """Allow asyncio's private loopback pipe to initialize on Windows."""
        # Python 3.14 uses a connect-based socketpair fallback on Windows.
        # This is an in-process event-loop primitive, not an external network
        # escape.  Keep the exception narrowly scoped to socketpair creation.
        with allow_socketpair_connect():
            return original_socketpair(*args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", fail_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", fail_connect_ex)
    monkeypatch.setattr(socket, "create_connection", fail_create_connection)
    monkeypatch.setattr(socket, "socketpair", socketpair_without_network)
    monkeypatch.setattr(socket.socket, "sendto", fail_udp_send)
    if hasattr(socket.socket, "sendmsg"):
        monkeypatch.setattr(socket.socket, "sendmsg", fail_udp_send)

@pytest.fixture
def db_session():
    """Create a fresh in-memory SQLite database for each test session."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()

@pytest.fixture
def sample_source():
    """Return a sample Bandera source configuration."""
    return SourceConfig(
        id="bandera",
        name="Bandera",
        domain="bandera.inquirer.net",
        enabled=True,
        language="filipino",
        crawl_delay_seconds=10,
        max_concurrent=1,
        rss=[RSSFeedConfig(url="https://bandera.inquirer.net/feed", category="general")],
        sitemap=[SitemapConfig(url="https://bandera.inquirer.net/sitemap.xml")],
        extraction=None
    )
