import asyncio
import copy
import json
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import pytest
import yaml
from aiohttp import web
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError
from selectolax.lexbor import LexborHTMLParser

from backend.ingestion.configurable import (
    ConfigurableScraper,
    FieldRule,
    ScraperConfig,
    extract_field,
    load_config,
    parse_date_de,
)
from backend.ingestion.orgs.karlsruhe import KarlsruheScraper
from backend.ingestion.scrape import main, parse_args
from backend.ingestion.snapshots import check_page, save_scrape, scrape_diff
from backend.ingestion.staging import StagedMembership, StagedProduction, staging_schema, validate_records
from backend.models import Person, Production

KARLSRUHE_URL = "https://www.staatstheater-karlsruhe.de/programm/info/123/"
STUTTGART_URL = "https://www.schauspiel-stuttgart.de/spielplan/premieren/example/"

# Synthetic HTML follows observed DOM structure, not copied website content.
KARLSRUHE_HTML = """
<base href="https://www.staatstheater-karlsruhe.de/">
<main><div class="col-md-12 col-lg-8">
<h1 class="h2">Example Play</h1><h2 class="h4">Schauspiel nach Example Author</h2>
<p>Premiere: 3.10.2026</p><p>Dauer: 1h 30, keine Pause</p><p>Ab 15 Jahren</p>
<p>This synthetic description is deliberately long enough to be considered real production content.</p>
<hr><p>This long paragraph is venue logistics and must never appear in the production description.</p>
<h3 id="besetzung">Besetzung</h3><div><table>
<tr><th>Mit</th><td><a href="ensemble/id/1/">First Actor</a></td></tr>
<tr><th></th><td><a href="ensemble/id/2/">Second Actor</a></td></tr>
<tr><th>Regie</th><td><a href="ensemble/id/3/">Example Director</a></td></tr>
<tr><th>Bühne</th><td>Example Designer</td></tr>
<tr><td>Kostüme</td><td>Example Costumer</td></tr>
<tr><th>Licht</th><td>Example Lighting</td></tr>
<tr><th>Dramaturgie</th><td><a href="ensemble/id/3/">Example Director</a></td></tr>
<tr><th>Musik</th><td><a href="ensemble/id/4/">Composer One</a>,
<a href="ensemble/id/5/">Composer Two</a></td></tr>
</table></div></div></main>
"""

STUTTGART_HTML = """
<h1 class="productionheader__title">Example Play</h1>
<div class="productionheader__author">von <span>Example Author</span></div>
<div class="productionheader__premiereinfo">Premiere Fr &ndash; 16. Okt 26</div>
<div class="productionheader__duration">Dauer &ndash; ca. 2:50 Std., Pause nach ca. 1:30 Std.</div>
<div class="productionheader__agerecommendation">ab Klasse 10</div>
<div class="richtext richtext--twocol-and-firstletter">
This synthetic production description is long enough to represent the content area.</div>
<div class="cast__item"><a class="cast__link" href="/people/1/">
<div class="cast__name">Example Actor*</div><div class="cast__role">Example Character</div></a></div>
<div class="cast__item"><div class="cast__name">Extra One, Extra Two</div>
<div class="cast__role">Statisterie</div></div>
<div class="crew__item"><div class="crew__role">Inszenierung</div>
<div class="crew__name"><a href="/people/2/">Example Director</a></div></div>
<div class="crew__item"><div class="crew__role">Bühne</div>
<div class="crew__name"><a href="/people/3/">Designer One</a>,
<a href="/people/4/">Designer Two</a></div></div>
"""


@pytest.fixture
def record():
    return {
        "title": "Example Play",
        "url": KARLSRUHE_URL,
        "duration": 90,
        "memberships": [
            {"name": "First Actor", "role": "Hamlet", "url": "https://www.staatstheater-karlsruhe.de/people/1"}
        ],
    }


def test_published_schema_matches_models_and_current_domain_fields(record):
    schema_file = Path(__file__).resolve().parents[1] / "backend/ingestion/staging.schema.json"
    schema = staging_schema()
    assert json.loads(schema_file.read_text()) == schema
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate([record])
    assert set(StagedProduction.model_fields) - {"title", "author", "memberships"} <= set(Production.model_fields)
    assert set(StagedMembership.model_fields) - {"url", "role", "character"} <= set(Person.model_fields)
    for field in ("description",):
        assert StagedProduction.model_fields[field].annotation == Production.model_fields[field].annotation
    for field in ("description", "born", "died", "nationality"):
        assert StagedMembership.model_fields[field].annotation == Person.model_fields[field].annotation


@pytest.mark.parametrize("value", [[], {}, [{"url": KARLSRUHE_URL}], [{"title": "  ", "url": KARLSRUHE_URL}]])
def test_reject_invalid_snapshot_shape(value):
    with pytest.raises(ValidationError):
        validate_records(value)
    assert list(Draft202012Validator(staging_schema()).iter_errors(value))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("url", "/relative"),
        ("duration", -1),
        ("duration", True),
        ("duration", "90"),
        ("age_rating", -1),
        ("premiere", "2026-02-31"),
        ("premiere", "2026-10-16T19:00:00"),
        ("premiere", "123"),
        ("premiere", 123),
        ("typo", "unknown"),
    ],
)
def test_schema_and_runtime_reject_invalid_fields(record, field, value):
    record[field] = value
    with pytest.raises(ValidationError):
        validate_records([record])
    assert list(Draft202012Validator(staging_schema(), format_checker=FormatChecker()).iter_errors([record]))


def test_legacy_and_current_person_fields(record):
    record["premiere"] = "2026-10-03"
    member = record["memberships"][0]
    member.update(type=["actor", "director"], born="1980-01-02", nationality="Example", urls=[KARLSRUHE_URL])
    result = validate_records([record])[0]
    assert result["premiere"] == "2026-10-03"
    assert result["memberships"][0]["born"] == "1980-01-02"
    assert result["memberships"][0]["type"] == ["actor", "director"]
    member["url"] = "javascript:alert(1)"
    with pytest.raises(ValidationError):
        validate_records([record])


def test_datetime_timezone_and_person_dates_match_schema(record):
    record["premiere"] = "2026-10-16T19:00:00+02:00"
    validated = validate_records([record])
    Draft202012Validator(staging_schema(), format_checker=FormatChecker()).validate(validated)
    for born in (123, "19800102", "1980-02-31"):
        record["memberships"][0]["born"] = born
        with pytest.raises(ValidationError):
            validate_records([record])


def test_yaml_and_json_configs_are_equivalent(tmp_path):
    config = load_config("karlsruhe")
    path = tmp_path / "venue.json"
    path.write_text(json.dumps(config.model_dump(mode="json")))
    assert load_config(path) == config
    path = tmp_path / "venue.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    assert load_config(path) == config


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data.update(typo=True),
        lambda data: data["production"]["title"].update(required=False),
        lambda data: data["production"].update(unknown={"selector": "p"}),
        lambda data: data["production"]["title"].update(extract="["),
        lambda data: data["production"]["title"].update(selector="[["),
        lambda data: data.update(source_id="../unsafe"),
    ],
)
def test_config_errors_fail_before_network(mutation):
    data = load_config("karlsruhe").model_dump(mode="json")
    mutation(data)
    with pytest.raises(ValueError):
        ScraperConfig.model_validate(data)


def test_karlsruhe_selectors_and_continuation_roles():
    scraper = ConfigurableScraper(load_config("karlsruhe"))
    record = scraper.parse_page(KARLSRUHE_HTML, KARLSRUHE_URL, "production")
    assert record["title"] == "Example Play"
    assert record["premiere"] == "2026-10-03"
    assert record["duration"] == 90
    assert record["age_rating"] == 15
    assert record["author"] == "Example Author"
    assert "venue logistics" not in record["description"]
    members = record["memberships"]
    assert len(members) == 9
    assert not members[0]["role"] and not members[1]["role"]
    assert members[2]["type"] == "director"
    assert members[3]["type"] == "set_designer"
    assert members[4]["type"] == "costume_designer"
    assert members[5]["type"] == "light_designer"
    assert members[2]["url"] == members[6]["url"]
    assert members[6]["type"] == "dramaturg"
    assert members[7]["name"] == "Composer One"
    assert members[8]["url"].endswith("/ensemble/id/5/")


def test_stuttgart_selectors_short_dates_durations_and_multiple_people():
    scraper = ConfigurableScraper(load_config("stuttgart"))
    record = scraper.parse_page(STUTTGART_HTML, STUTTGART_URL, "production")
    assert record["premiere"] == "2026-10-16"
    assert record["duration"] == 170
    assert record["age_rating"] is None  # School grade is not an age.
    assert record["author"] == "Example Author"
    members = record["memberships"]
    assert [member["name"] for member in members] == [
        "Example Actor",
        "Extra One",
        "Extra Two",
        "Example Director",
        "Designer One",
        "Designer Two",
    ]
    assert members[0]["role"] == "Example Character"
    assert members[0]["type"] == "actor"
    assert members[1]["url"] is None
    assert members[3]["type"] == "director"
    assert members[4]["type"] == members[5]["type"] == "set_designer"
    assert members[4]["url"] != members[5]["url"]
    record = scraper.parse_page(
        STUTTGART_HTML.replace("Example Character", "Regisseur"),
        STUTTGART_URL,
        "production",
    )
    assert record["memberships"][0]["type"] == "actor"


@pytest.mark.parametrize(
    ("site", "html", "url"),
    [
        (
            "karlsruhe",
            '<main><h1>Person</h1><div class="col-sm-12 col-md-7"><p>' + "Biography. " * 12 + "</p></div></main>",
            "https://www.staatstheater-karlsruhe.de/ensemble/id/1/",
        ),
        (
            "stuttgart",
            '<main><h1>Person*</h1><div class="richtext">' + "Biography. " * 12 + "</div></main>",
            "https://www.schauspiel-stuttgart.de/people/1/",
        ),
    ],
)
def test_person_rules_do_not_use_modal_or_footer_text(site, html, url):
    html = "<div class='modal-body'><p>" + "Unrelated text. " * 20 + "</p></div>" + html
    person = ConfigurableScraper(load_config(site)).parse_page(html, url, "person")
    assert person["name"] == "Person"
    assert "Biography" in person["description"]
    assert "Unrelated" not in person["description"]


def test_listing_filters_overviews_profiles_and_duplicates():
    scraper = ConfigurableScraper(load_config("stuttgart"))
    html = """
    <a href="/spielplan/premieren/">Overview</a>
    <a href="/spielplan/premieren/spielplananalyse-26-27/">Analysis</a>
    <a href="/spielplan/premieren/example/">Production</a>
    <a href="/spielplan/premieren/example/#cast">Duplicate</a>
    <a href="/spielplan/premieren/example/person/">Person</a>
    """
    assert scraper.parse_list(html, scraper.config.listing.urls[0]) == [STUTTGART_URL]
    scraper = ConfigurableScraper(load_config("karlsruhe"))
    assert scraper.parse_list(
        '<base href="https://www.staatstheater-karlsruhe.de/"><h3 class="recife">'
        '<a href="programm/info/123/">Title</a></h3>',
        scraper.config.listing.urls[0],
    ) == [KARLSRUHE_URL]


@pytest.mark.parametrize("html", ["<html></html>", "<h1>Access denied</h1>"])
def test_missing_required_production_selector_is_an_error(html):
    with pytest.raises(ValueError, match="Required selector"):
        ConfigurableScraper(load_config("karlsruhe")).parse_page(html, KARLSRUHE_URL, "production")


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/",
        "http://www.staatstheater-karlsruhe.de/",
        "file:///etc/passwd",
        "https://user:password@www.staatstheater-karlsruhe.de/",
    ],
)
def test_scraper_rejects_outside_origins(url):
    with pytest.raises(ValueError, match="outside configured origin"):
        ConfigurableScraper(load_config("karlsruhe")).check_url(url)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Premiere Fr \u2013 16. Okt 26", "2026-10-16"),
        ("1. März 2026", "2026-03-01"),
        ("3.10.2026", "2026-10-03"),
        ("no announced premiere", None),
    ],
)
def test_german_dates(text, expected):
    assert parse_date_de(text) == expected


def test_extract_list_fields():
    tree = LexborHTMLParser('<a href="/first">Tag One</a><a href="/second">Tag Two</a>')
    assert extract_field(tree, FieldRule(selector="a", many=True), KARLSRUHE_URL) == ["Tag One", "Tag Two"]
    assert extract_field(
        tree,
        FieldRule(selector="a", many=True, attribute="href", format="url"),
        "https://example.test/",
    ) == ["https://example.test/first", "https://example.test/second"]


def test_scrape_diff_fields_people_and_order_independence(record):
    before = validate_records([record])
    after = copy.deepcopy(before)
    after[0]["duration"] = 100
    after[0]["memberships"][0]["description"] = "Updated biography"
    report = scrape_diff(before, after)
    assert "~1 changed" in report
    assert "duration:" in report and "- 90" in report and "+ 100" in report
    assert "First Actor (Hamlet) > description" in report
    after = copy.deepcopy(before)
    after[0]["memberships"].append({"name": "Another", "role": "Other", "url": None})
    normalized = validate_records(after)
    assert "~0 changed" in scrape_diff(
        normalized, [{**normalized[0], "memberships": normalized[0]["memberships"][::-1]}]
    )
    assert "+ memberships: Another (Other)" in scrape_diff(before, after)
    assert "+1 added" in scrape_diff([], before)
    assert "-1 removed" in scrape_diff(before, [])
    with pytest.raises(ValueError, match="Duplicate production URL"):
        scrape_diff(before, [*before, *before])


def test_snapshots_never_overwrite_and_scopes_do_not_mix(record, tmp_path):
    config = load_config("karlsruhe")
    first, _ = save_scrape([record], config, tmp_path, limit=1, enrich_people=False)
    second, report = save_scrape([record], config, tmp_path, limit=1, enrich_people=False)
    assert first != second and first.exists() and second.exists()
    assert "=1 unchanged" in report
    for limit, people in [(2, False), (None, False), (1, True)]:
        _, report = save_scrape([record], config, tmp_path, limit=limit, enrich_people=people)
        assert "No previous scrape" in report
    config.request_delay = 2
    _, report = save_scrape([record], config, tmp_path, limit=1, enrich_people=False)
    assert "No previous scrape" in report


def test_page_check_persists_and_only_accept_advances_baseline(record, tmp_path):
    config = load_config("karlsruhe")
    code, report = check_page("old", record, KARLSRUHE_URL, "production", config, tmp_path)
    assert code == 0 and "Baseline created" in report
    path = next(tmp_path.rglob("*.json"))
    baseline = path.read_bytes()
    assert check_page("old", record, KARLSRUHE_URL, "production", config, tmp_path)[0] == 0
    code, report = check_page("new html", record, KARLSRUHE_URL, "production", config, tmp_path)
    assert code == 1 and "HTML: changed; extracted data: unchanged" in report
    assert path.read_bytes() == baseline
    assert check_page("new html", record, KARLSRUHE_URL, "production", config, tmp_path, data_only=True)[0] == 0
    updated = {**record, "duration": 100}
    code, report = check_page("new html", updated, KARLSRUHE_URL, "production", config, tmp_path, accept=True)
    assert code == 1 and "duration:" in report and "accepted" in report
    assert path.read_bytes() != baseline
    assert check_page("new html", updated, KARLSRUHE_URL, "production", config, tmp_path)[0] == 0


def test_corrupt_previous_snapshot_and_check_fail_explicitly(record, tmp_path):
    config = load_config("karlsruhe")
    path, _ = save_scrape([record], config, tmp_path, limit=1, enrich_people=False)
    path.write_text("invalid")
    with pytest.raises(ValueError):
        save_scrape([record], config, tmp_path, limit=1, enrich_people=False)
    check_page("html", record, KARLSRUHE_URL, "production", config, tmp_path)
    baseline = next(tmp_path.rglob("production-*.json"))
    baseline.write_text("{}")
    with pytest.raises(ValidationError):
        check_page("html", record, KARLSRUHE_URL, "production", config, tmp_path)


def test_legacy_karlsruhe_entry_uses_shared_config_and_safe_default():
    scraper = KarlsruheScraper()
    assert scraper.max_productions == 1
    assert scraper.parse_detail(KARLSRUHE_HTML, {"url": KARLSRUHE_URL})["duration"] == 90
    first = {"url": KARLSRUHE_URL, "title": "first"}
    assert scraper.deduplicate([first, {"url": "other"}, {"url": KARLSRUHE_URL}]) == [first]
    with pytest.raises(ValueError):
        KarlsruheScraper(max_productions=0)


def test_cli_safe_defaults_and_offline_commands(tmp_path, record, capsys):
    assert parse_args(["scrape", "--config", "karlsruhe"]).limit == 1
    assert not parse_args(["scrape", "--config", "karlsruhe"]).all
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps([record]))
    assert main(["validate", str(path)]) == 0
    assert main(["diff", str(path), str(path)]) == 0
    assert "unchanged" in capsys.readouterr().out
    assert main(["schema", "--output", str(tmp_path / "schema.json")]) == 0
    path.write_text("[]")
    assert main(["validate", str(path)]) == 2
    assert "Ingestion error" in capsys.readouterr().err
    assert main(["scrape", "--config", "missing-file.yaml"]) == 2


@asynccontextmanager
async def _local_venue():
    requests = []
    production = """
    <h1 class="h2">Synthetic Play</h1><h3 id="besetzung"></h3><div><table>
    <tr><th>Mit</th><td><a href="/ensemble/id/1/">Person</a></td></tr>
    <tr><th>Regie</th><td><a href="/ensemble/id/1/">Person</a></td></tr>
    </table></div>
    """
    listing = '<h3 class="recife"><a href="/programm/info/1/">One</a><a href="/programm/info/2/">Two</a></h3>'
    bodies = {
        "/list": listing,
        "/paged-list": listing + '<a class="next" href="/second-list">Next</a>',
        "/second-list": '<h3 class="recife"><a href="/programm/info/3/">Three</a></h3>',
        "/empty": "<html>No production links</html>",
        "/bad-list": '<h3 class="recife"><a href="/programm/info/404/">Missing profile</a></h3>',
        "/ensemble/id/1/": '<main><h1>Person</h1><div class="col-sm-12 col-md-7"><p>'
        + "Biography. " * 12
        + "</p></div></main>",
    }

    async def handler(request):
        await request.read()
        requests.append(request.path)
        if request.path in {"/redirect", "/external"}:
            raise web.HTTPFound("/programm/info/1/" if request.path == "/redirect" else "https://outside.example/page")
        if request.path == "/json":
            return web.json_response({"not": "html"})
        if request.path.startswith("/programm/info/"):
            body = (
                production.replace("/ensemble/id/1/", "/ensemble/id/404/")
                if request.path.endswith("/404/")
                else production
            )
        elif request.path in bodies:
            body = bodies[request.path]
        else:
            raise web.HTTPNotFound()
        return web.Response(text=body, content_type="text/html")

    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        host, port = runner.addresses[0]
        origin = f"http://{host}:{port}/"
        config = load_config("karlsruhe").model_dump(mode="json")
        config.update(base_url=origin, request_delay=0)
        config["listing"]["urls"] = [origin + "list"]
        yield origin, config, requests
    finally:
        await runner.cleanup()


def test_http_pipeline_is_bounded_enriches_once_and_reports_errors():
    async def exercise():
        async with _local_venue() as (origin, config, requests):
            scraper = ConfigurableScraper(ScraperConfig.model_validate(config))
            records = await scraper.scrape(limit=1)
            assert requests == ["/list", "/programm/info/1/", "/ensemble/id/1/"]
            assert all("Biography" in m["description"] for m in records[0]["memberships"])
            requests.clear()
            await scraper.scrape(limit=1, enrich_people=False)
            assert requests == ["/list", "/programm/info/1/"]
            requests.clear()
            async with scraper.session() as session:
                html = await scraper.fetch(session, origin + "redirect")
                scraper.parse_page(html, origin + "redirect", "production")
                assert requests == ["/redirect", "/programm/info/1/"]
                with pytest.raises(aiohttp.ClientResponseError):
                    await scraper.fetch(session, origin + "missing")
                with pytest.raises(ValueError, match="outside configured origin"):
                    await scraper.fetch(session, origin + "external")
                with pytest.raises(ValueError, match="Expected HTML"):
                    await scraper.fetch(session, origin + "json")
            with pytest.raises(ValueError, match="limit must be positive"):
                await scraper.scrape(limit=0)

    asyncio.run(exercise())


def test_cli_page_and_check_fetch_exactly_one_page(tmp_path):
    async def exercise():
        async with _local_venue() as (origin, config, requests):
            config_path = tmp_path / "local.json"
            config_path.write_text(json.dumps(config))
            output_path = tmp_path / "production.json"
            assert (
                await asyncio.to_thread(
                    main,
                    [
                        "page",
                        "--config",
                        str(config_path),
                        "--url",
                        origin + "programm/info/1/",
                        "--output",
                        str(output_path),
                    ],
                )
                == 0
            )
            assert requests == ["/programm/info/1/"]
            assert len(json.loads(output_path.read_text())) == 1
            requests.clear()
            check_args = [
                "check",
                "--config",
                str(config_path),
                "--url",
                origin + "ensemble/id/1/",
                "--kind",
                "person",
                "--state-dir",
                str(tmp_path / "checks"),
            ]
            assert await asyncio.to_thread(main, check_args) == 0
            assert await asyncio.to_thread(main, check_args) == 0
            assert requests == ["/ensemble/id/1/", "/ensemble/id/1/"]

    asyncio.run(exercise())


def test_pagination_is_bounded_and_failures_never_save_partial_scrapes(tmp_path):
    async def exercise():
        async with _local_venue() as (origin, config, requests):
            paged_config = copy.deepcopy(config)
            paged_config["listing"].update(urls=[origin + "paged-list"], next_selector="a.next", max_pages=1)
            paged = ConfigurableScraper(ScraperConfig.model_validate(paged_config))
            with pytest.raises(ValueError, match="max_pages reached"):
                await paged.scrape(limit=None, enrich_people=False)
            assert requests == ["/paged-list"]
            requests.clear()
            paged_config["listing"]["max_pages"] = 2
            paged = ConfigurableScraper(ScraperConfig.model_validate(paged_config))
            assert len(await paged.scrape(limit=None, enrich_people=False)) == 3
            assert requests == [
                "/paged-list",
                "/second-list",
                "/programm/info/1/",
                "/programm/info/2/",
                "/programm/info/3/",
            ]
            empty_config = copy.deepcopy(config)
            empty_config["listing"]["urls"] = [origin + "empty"]
            with pytest.raises(ValueError, match="No production links"):
                await ConfigurableScraper(ScraperConfig.model_validate(empty_config)).scrape()
            failed_config = copy.deepcopy(config)
            failed_config["listing"]["urls"] = [origin + "bad-list"]
            config_path = tmp_path / "local.json"
            config_path.write_text(json.dumps(failed_config))
            requests.clear()
            assert (
                await asyncio.to_thread(
                    main,
                    [
                        "scrape",
                        "--config",
                        str(config_path),
                        "--output-dir",
                        str(tmp_path / "staged"),
                    ],
                )
                == 2
            )
            assert requests == ["/bad-list", "/programm/info/404/", "/ensemble/id/404/"]
            assert not list((tmp_path / "staged").rglob("*.json"))

    asyncio.run(exercise())
