from pathlib import Path

from src.sources.registry import SourceConfig, SourceRegistry


def test_source_scope_requires_domain_and_path_boundary():
    source = SourceConfig(
        id="star", name="Star", domain="www.philstar.com", enabled=True,
        language="filipino", url_prefix="/pilipino-star-ngayon",
    )
    assert source.accept_url("https://www.philstar.com/pilipino-star-ngayon/bansa/story")
    assert not source.accept_url("https://www.philstar.com/pilipino-star-ngayon")
    assert not source.accept_url("https://www.philstar.com/pilipino-star-ngayon-extra/story")
    assert not source.accept_url("https://other.example/pilipino-star-ngayon/story")


def test_observed_publisher_article_shapes_are_scoped():
    registry = SourceRegistry(Path("config/sources.yaml"))
    abante = registry.get_source("abante")
    psn = registry.get_source("pilipino_star_ngayon")
    pang = registry.get_source("pang_masa")
    gma = registry.get_source("gma_filipino")
    bandera = registry.get_source("bandera")

    abante_url = "https://www.abante.com.ph/2026/07/24/abante-front-page-balita-ngayong-hulyo-25-2026/"
    psn_url = "https://www.philstar.com/bansa/2026/08/11/2548446/55-percent-pinoy-gusto-makita-ebidensiya"
    psn_prefixed_url = "https://www.philstar.com/pilipino-star-ngayon/showbiz/2026/09/01/2553206/princess-hours-actor-pumanaw-sa-edad-na-44-covid-sa-korea-tumataas-na-ulit"
    pang_url = "https://www.philstar.com/punto-mo/2026/08/11/2548505/magandang-epekto-ng-halik"
    pang_prefixed_url = "https://www.philstar.com/pang-masa/pang-movies/2026/08/02/2546368/atasha-gusting-mag-explore-sa-pelikula"
    gma_url = "https://www.gmanetwork.com/news/serbisyopubliko/transportation/997824/list-diverted-flights/story/"
    bandera_url = "https://bandera.inquirer.net/452344/heart-evangelista-ipinamigay-sa-charity"

    assert abante and abante.accept_url(abante_url)
    assert psn and psn.accept_url(psn_url)
    assert psn.accept_url(psn_prefixed_url)
    assert pang and pang.accept_url(pang_url)
    assert pang.accept_url(pang_prefixed_url)
    assert gma and gma.accept_url(gma_url)
    assert bandera and bandera.accept_url(bandera_url)

    # Abante's homepage and date/category roots are not articles.
    assert not abante.accept_url("https://www.abante.com.ph/")
    assert not abante.accept_url("https://www.abante.com.ph/2026")
    assert not abante.accept_url("https://www.abante.com.ph/2026/07/24/")

    # Philstar section roots, homepage, and English sections are not articles.
    assert not psn.accept_url("https://www.philstar.com/")
    assert not psn.accept_url("https://www.philstar.com/bansa")
    assert not psn.accept_url("https://www.philstar.com/headlines/2026/08/11/2548446/english-story")
    # Shared Philstar sitemap URLs must not be attributed to the other brand.
    assert not psn.accept_url(pang_url)
    assert not psn.accept_url(pang_prefixed_url)
    assert not pang.accept_url(psn_url)
    assert not pang.accept_url(psn_prefixed_url)
    assert not gma.accept_url(psn_url)
