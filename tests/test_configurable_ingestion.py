import asyncio
import copy
import json
from collections.abc import AsyncGenerator, Callable
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
from backend.ingestion.scrape import main, parse_args
from backend.ingestion.snapshots import check_page, save_scrape, scrape_diff, without_null_fields, write_json
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
def record() -> dict:
    return {
        "title": "Example Play",
        "url": KARLSRUHE_URL,
        "duration": 90,
        "memberships": [
            {"name": "First Actor", "role": "Hamlet", "url": "https://www.staatstheater-karlsruhe.de/people/1"}
        ],
    }


def test_published_schema_matches_models_and_current_domain_fields(record: dict) -> None:
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
def test_reject_invalid_snapshot_shape(value: object) -> None:
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
        ("premiere_year", 0),
        ("premiere_year", 10000),
        ("premiere_year", True),
        ("premiere_year", "2026"),
        ("typo", "unknown"),
    ],
)
def test_schema_and_runtime_reject_invalid_fields(record: dict, field: str, value: object) -> None:
    record[field] = value
    with pytest.raises(ValidationError):
        validate_records([record])
    assert list(Draft202012Validator(staging_schema(), format_checker=FormatChecker()).iter_errors([record]))


def test_legacy_and_current_person_fields(record: dict) -> None:
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


def test_datetime_timezone_and_person_dates_match_schema(record: dict) -> None:
    record["premiere"] = "2026-10-16T19:00:00+02:00"
    validated = validate_records([record])
    Draft202012Validator(staging_schema(), format_checker=FormatChecker()).validate(validated)
    for born in (123, "19800102", "1980-02-31"):
        record["memberships"][0]["born"] = born
        with pytest.raises(ValidationError):
            validate_records([record])


def test_yaml_and_json_configs_are_equivalent(tmp_path: Path) -> None:
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
def test_config_errors_fail_before_network(mutation: Callable[[dict], None]) -> None:
    data = load_config("karlsruhe").model_dump(mode="json")
    mutation(data)
    with pytest.raises(ValueError):
        ScraperConfig.model_validate(data)


def test_karlsruhe_selectors_and_continuation_roles() -> None:
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


def test_bundled_config_names_are_case_insensitive() -> None:
    assert load_config("Karlsruhe") == load_config("karlsruhe")
    assert load_config("STUTTGART") == load_config("stuttgart")


@pytest.mark.parametrize("role", ["Vermittlung", "VERMITTLUNG", "Theaterpädagogik"])
def test_educator_credit_uses_shared_mapping_without_venue_overrides(role: str) -> None:
    config = load_config("karlsruhe").model_copy(update={"role_types": {}})
    scraper = ConfigurableScraper(config)
    html = KARLSRUHE_HTML.replace("<th>Regie</th>", f"<th>{role}</th>")
    member = scraper.parse_page(html, KARLSRUHE_URL, "production")["memberships"][2]
    assert member["role"] == role
    assert member["type"] == "theatre_educator"


def test_venue_role_overrides_still_take_precedence() -> None:
    config = load_config("karlsruhe").model_copy(update={"role_types": {"Vermittlung": "custom_educator"}})
    html = KARLSRUHE_HTML.replace("<th>Regie</th>", "<th>Vermittlung</th>")
    member = ConfigurableScraper(config).parse_page(html, KARLSRUHE_URL, "production")["memberships"][2]
    assert member["type"] == "custom_educator"


@pytest.mark.parametrize(
    "spacer",
    [
        '<tr><th scope="row"><b></b></th><td>&nbsp;</td></tr>',
        "<tr><td> \n </td><td><br></td></tr>",
    ],
)
def test_blank_credit_spacers_preserve_continuation_roles(spacer: str) -> None:
    scraper = ConfigurableScraper(load_config("karlsruhe"))
    html = KARLSRUHE_HTML.replace(
        '<tr><th></th><td><a href="ensemble/id/2/">Second Actor</a></td></tr>',
        spacer + '<tr><th></th><td><a href="ensemble/id/2/">Second Actor</a></td></tr>',
    )
    expected = scraper.parse_page(KARLSRUHE_HTML, KARLSRUHE_URL, "production")
    assert scraper.parse_page(html, KARLSRUHE_URL, "production") == expected
    html = KARLSRUHE_HTML.replace(
        "<tr><th>Bühne</th><td>Example Designer</td></tr>",
        spacer + "<tr><th></th><td>Example Designer</td></tr>",
    )
    members = scraper.parse_page(html, KARLSRUHE_URL, "production")["memberships"]
    assert members[3]["role"] == "Regie"
    assert members[3]["type"] == "director"


def test_nonempty_credit_row_without_person_still_fails() -> None:
    scraper = ConfigurableScraper(load_config("karlsruhe"))
    html = KARLSRUHE_HTML.replace(
        '<tr><th>Regie</th><td><a href="ensemble/id/3/">Example Director</a></td></tr>',
        "<tr><th>Regie</th><td>&nbsp;</td></tr>",
    )
    with pytest.raises(ValueError, match="Required selector"):
        scraper.parse_page(html, KARLSRUHE_URL, "production")


def test_stuttgart_selectors_short_dates_durations_and_multiple_people() -> None:
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


@pytest.mark.parametrize("color", ["primary", "green", "yellow", "purple"])
def test_badische_landesbuehne_production_and_listing(color: str) -> None:
    scraper = ConfigurableScraper(load_config("badische-landesbuehne"))
    base = scraper.config.base_url
    url = base + "produktionen/example.html?m=51"
    listing = f"""
    <base href="{base}"><main>
    <a href="./produktionen/example.html?m=51">Premiere</a>
    <a href="./produktionen/example.html?m=51">Production</a>
    <a href="personen/actor.html">Person</a><a href="seiten/programm-2627.html">Programme</a>
    </main><footer><a href="produktionen/unrelated.html">Unrelated</a></footer>
    """
    assert scraper.parse_list(listing, scraper.config.listing.urls[0]) == [url]
    html = f"""
    <base href="{base}"><main><h1>Example Play</h1>
    <p class="supraline"><span>Schauspiel</span> nach der Novelle von Example Author</p>
    <div class="row"><div class="col col-md-8">
    <div class="mb-5"><p>Short summary, not the full description.</p></div>
    <p>A synthetic <strong>description</strong> of this production.</p>
    <p>Second paragraph.</p><hr><p>Unrelated adaptation notes.</p></div>
    <div class="col col-md-4"><p>Dauer<br><strong>1 Stunden 05 Minuten</strong></p>
    <p>Altersempfehlung<br><strong>ab 13 Jahren</strong></p></div></div>
    <div id="naechste"><div class="termin"><div class="row">
    <div class="tabnum"><strong>20.11.2026</strong></div></div></div></div>
    <div id="vergangene"><div class="termin"><div class="row">
    <div class="tabnum"><strong>18.09.2026</strong></div>
    <div><img alt="Premiere"></div></div></div></div>
    <div class="bg-{color} text-white"><div class="container"><div class="row">
    <div><h2>Besetzung</h2></div>
    <div><p>Inszenierung: <span class="fw-semibold">Example Director</span></p>
    <p>Bühne &amp; Kostüm: <span class="fw-semibold">Example Designer</span></p></div>
    <div><p>Spieler 1: Die Regisseurin: <span class="fw-semibold">
    <a href="personen/actor.html">Example Actor</a></span></p>
    <p>Mit: <span class="fw-semibold">Unlinked Actor</span></p></div>
    </div></div></div></main>
    """
    record = scraper.parse_page(html, url, "production")
    assert record["title"] == "Example Play"
    assert record["author"] == "Example Author"
    assert record["premiere"] == "2026-09-18"
    assert record["duration"] == 65
    assert record["age_rating"] == 13
    assert record["description"] == ("A synthetic **description** of this production.\n\nSecond paragraph.")
    members = record["memberships"]
    assert [member["name"] for member in members] == [
        "Example Director",
        "Example Designer",
        "Example Actor",
        "Unlinked Actor",
    ]
    assert [member["type"] for member in members] == ["director", "set_designer", "actor", "actor"]
    assert members[1]["role"] == "Bühne & Kostüm"
    assert members[2]["role"] == "Spieler 1: Die Regisseurin"
    assert members[2]["url"] == base + "personen/actor.html"
    assert members[3]["url"] is None
    assert not members[3]["role"]
    assert scraper.parse_page(html.replace('alt="Premiere"', 'alt="Einführung"'), url, "production")["premiere"] is None


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
        (
            "badische-landesbuehne",
            '<main><div class="col-md-8"><h1>Person</h1><div class="col-xl-10"><p>'
            + "Biography. " * 12
            + "</p></div><h2>Produktionen von und mit</h2><p>Unrelated production.</p></div></main>",
            "https://www.badische-landesbuehne.de/de/personen/example.html",
        ),
    ],
)
def test_person_rules_do_not_use_modal_or_footer_text(site: str, html: str, url: str) -> None:
    html = "<div class='modal-body'><p>" + "Unrelated text. " * 20 + "</p></div>" + html
    person = ConfigurableScraper(load_config(site)).parse_page(html, url, "person")
    assert person["name"] == "Person"
    assert "Biography" in person["description"]
    assert "Unrelated" not in person["description"]


@pytest.mark.parametrize("biography", ["", " \n ", "<p>&nbsp;</p>"])
def test_badische_landesbuehne_profiles_without_biographies(biography: str) -> None:
    scraper = ConfigurableScraper(load_config("badische-landesbuehne"))
    url = scraper.config.base_url + "personen/example.html"
    html = (
        '<main><div class="col-md-8"><h1>Example Person</h1>'
        f'<div class="col-xl-10">{biography}</div>'
        '<h2>Produktionen von und mit</h2><div class="production">Unrelated play</div>'
        "</div></main>"
    )
    person = scraper.parse_page(html, url, "person")
    assert person["name"] == "Example Person"
    assert person["url"] == url
    assert person["description"] is None
    with pytest.raises(ValueError, match="Required selector 'main h1'"):
        scraper.parse_page(html.replace("<h1>Example Person</h1>", ""), url, "person")


@pytest.mark.parametrize(
    "placeholder", ["", "<p>Mehr Informationen folgen.</p>", "<p><strong>Mehr Informationen folgen.</strong></p>"]
)
@pytest.mark.parametrize(
    "heading",
    [
        "<p>Auf der Bühne in der Spielzeit 24|25:</p>",
        "<h2>Auf der Bühne in der Spielzeit 26/27:</h2>",
        "<p><strong>Auf der Bühne in der Spielzeit 24|25:</strong></p>",
        "<p><em>Auf der Bühne in der Spielzeit 24|25:</em></p>",
        "<p>Auf der Bühne:</p>",
    ],
)
def test_badische_landesbuehne_biographies_stop_before_season_gallery(heading: str, placeholder: str) -> None:
    scraper = ConfigurableScraper(load_config("badische-landesbuehne"))
    url = scraper.config.base_url + "personen/example.html"
    biography = "<p>Example <strong>biography</strong>.</p><p>Second paragraph.</p>"
    gallery = '<ul><li><a href="/production.html"><img src="/photo.jpg" alt="Scene"></a></li></ul>'
    html = (
        '<main><div class="col-md-8"><h1>Example Person</h1>'
        f'<div class="col-xl-10">{biography}{placeholder}{heading}{gallery}</div></div></main>'
    )
    person = scraper.parse_page(html, url, "person")
    assert person["description"] == "Example **biography**.\n\nSecond paragraph."
    person = scraper.parse_page(html.replace(biography, ""), url, "person")
    assert person["description"] is None
    person = scraper.parse_page(html.replace(heading + gallery, ""), url, "person")
    assert person["description"] == "Example **biography**.\n\nSecond paragraph."


@pytest.mark.parametrize(
    ("site", "html", "url"),
    [("karlsruhe", KARLSRUHE_HTML, KARLSRUHE_URL), ("stuttgart", STUTTGART_HTML, STUTTGART_URL)],
)
@pytest.mark.parametrize("noise", ["\u00ad", "&shy;", "&#173;", "\u200b", "\ufeff"])
def test_text_cleanup_covers_all_production_and_credit_fields(site: str, html: str, url: str, noise: str) -> None:
    scraper = ConfigurableScraper(load_config(site))
    expected = scraper.parse_page(html, url, "production")
    noisy_html = html.replace("Example", f"Exam{noise}ple").replace("synthetic", f"syn{noise}thetic")
    assert scraper.parse_page(noisy_html, url, "production") == expected


@pytest.mark.parametrize("site", ["karlsruhe", "stuttgart"])
def test_biographies_are_clean_markdown_without_headings(site: str) -> None:
    content = (
        "<strong>Example</strong> <em>biography</em> with enough words to pass minimum length "
        'and <a href="/people/other/">another person</a>. Second&nbsp;sentence with soft&shy;hyphens.'
    )
    body = (
        f'<div class="col-sm-12 col-md-7"><p>{content}</p></div>'
        if site == "karlsruhe"
        else f'<div class="richtext"><h2>Biography heading</h2><p>{content}</p></div>'
    )
    scraper = ConfigurableScraper(load_config(site))
    url = scraper.config.base_url + "people/example/"
    person = scraper.parse_page(f"<main><h1>Exam&shy;ple Person*</h1>{body}</main>", url, "person")
    assert person["name"] == ("Example Person*" if site == "karlsruhe" else "Example Person")
    assert "**Example** *biography*" in person["description"]
    assert f"[another person]({scraper.config.base_url}people/other/)" in person["description"]
    assert "Second sentence with softhyphens." in person["description"]
    if site == "stuttgart":
        assert person["description"].startswith("Biography heading\n\n")
    assert "<" not in person["description"]
    assert "#" not in person["description"]


def test_stuttgart_production_description_retains_markdown_structure() -> None:
    html = STUTTGART_HTML.replace(
        "This synthetic production description is long enough to represent the content area.",
        "<h1>Main heading</h1><h2>Section heading</h2>"
        "<p>A soft&shy;hyphenated <strong>bold <em>and italic</em></strong> description."
        '<br>Next line with <a href="/info/#details" title="More info">a link</a>.</p>'
        "<ul><li>First item</li><li>Second item</li></ul>"
        "<script>not content</script><style>not content</style>"
        "<noscript>not content</noscript><template>not content</template><!-- not content -->",
    )
    record = ConfigurableScraper(load_config("stuttgart")).parse_page(html, STUTTGART_URL, "production")
    assert record["description"] == (
        "Main heading\n\nSection heading\n\n"
        "A softhyphenated **bold *and italic*** description.  \n"
        'Next line with [a link](https://www.schauspiel-stuttgart.de/info/#details "More info").\n\n'
        "- First item\n- Second item"
    )


def test_markdown_links_honor_html_base_and_preserve_inline_spacing() -> None:
    html = STUTTGART_HTML.replace(
        "This synthetic production description is long enough to represent the content area.",
        "<p>Long description with hy&shy;phenated and <em>empha</em>sized words, "
        '<a href="info/">details</a> and <a href="mailto:info@example.test">email</a>.</p>',
    )
    html = '<base href="https://www.schauspiel-stuttgart.de/other/">' + html
    record = ConfigurableScraper(load_config("stuttgart")).parse_page(html, STUTTGART_URL, "production")
    assert record["description"] == (
        "Long description with hyphenated and *empha*sized words, "
        "[details](https://www.schauspiel-stuttgart.de/other/info/) and [email](mailto:info@example.test)."
    )


def test_markdown_strip_removes_multiline_biography_boilerplate() -> None:
    biography = "This synthetic biography is deliberately long enough to pass the configured minimum length."
    html = (
        f'<main><h1>Person</h1><div class="col-sm-12 col-md-7"><p>{biography}'
        "<br>Möchten Sie sich auf die Warteliste setzen lassen?<br>Unwanted contact information.</p></div></main>"
    )
    person = ConfigurableScraper(load_config("karlsruhe")).parse_page(
        html, "https://www.staatstheater-karlsruhe.de/ensemble/id/1/", "person"
    )
    assert person["description"] == biography


def test_listing_filters_overviews_profiles_and_duplicates() -> None:
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
def test_missing_required_production_selector_is_an_error(html: str) -> None:
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
def test_scraper_rejects_outside_origins(url: str) -> None:
    with pytest.raises(ValueError, match="outside configured origin"):
        ConfigurableScraper(load_config("karlsruhe")).check_url(url)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Premiere Fr \u2013 16. Okt 26", "2026-10-16"),
        ("1. März 2026", "2026-03-01"),
        ("1. Mär. 26", "2026-03-01"),
        ("1. Mar 2026", "2026-03-01"),
        ("1. Oktober 2026", "2026-10-01"),
        ("1. OKT. 26", "2026-10-01"),
        ("29. Februar 2024", "2024-02-29"),
        ("3.10.2026", "2026-10-03"),
        ("no announced premiere", None),
        ("1. Unknown 2026", None),
    ],
)
def test_german_dates(text: str, expected: str | None) -> None:
    assert parse_date_de(text) == expected


@pytest.mark.parametrize(
    ("month", "name"),
    list(
        enumerate(
            (
                "Januar",
                "Februar",
                "März",
                "April",
                "Mai",
                "Juni",
                "Juli",
                "August",
                "September",
                "Oktober",
                "November",
                "Dezember",
            ),
            start=1,
        )
    ),
)
def test_all_german_months(month: int, name: str) -> None:
    expected = f"2026-{month:02d}-01"
    assert parse_date_de(f"Premiere: 1. {name} 2026") == expected
    assert parse_date_de(f"1. {name[:3]}. 26") == expected


@pytest.mark.parametrize("text", ["29. Februar 2026", "31. April 2026", "31.04.2026"])
def test_invalid_german_dates_fail_explicitly(text: str) -> None:
    with pytest.raises(ValueError):
        parse_date_de(text)


def test_extract_list_fields() -> None:
    tree = LexborHTMLParser('<a href="/first">Tag One</a><a href="/second">Tag Two</a>')
    assert extract_field(tree, FieldRule(selector="a", many=True), KARLSRUHE_URL) == ["Tag One", "Tag Two"]
    assert extract_field(
        tree,
        FieldRule(selector="a", many=True, attribute="href", format="url"),
        "https://example.test/",
    ) == ["https://example.test/first", "https://example.test/second"]


def test_scrape_diff_fields_people_and_order_independence(record: dict) -> None:
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


def test_snapshots_never_overwrite_and_scopes_do_not_mix(record: dict, tmp_path: Path) -> None:
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


def test_generated_json_omits_null_and_empty_array_fields(record: dict, tmp_path: Path) -> None:
    record.update(description=None, age_rating=0, author="", tags=[])
    record["memberships"][0].update(description=None, born=None)
    expected = without_null_fields(validate_records([record]))
    path, _ = save_scrape([record], load_config("karlsruhe"), tmp_path, limit=None, enrich_people=False)
    output = json.loads(path.read_text())
    assert output == expected
    assert "description" not in output[0]
    assert "born" not in output[0]["memberships"][0]
    assert output[0]["age_rating"] == 0
    assert "author" in output[0] and not output[0]["author"]
    assert "tags" not in output[0]
    assert "critics" not in output[0]
    assert "urls" not in output[0]["memberships"][0]
    assert validate_records(output) == validate_records([record])
    person = {"name": "Person", "url": None, "description": None, "urls": []}
    path = tmp_path / "person.json"
    write_json(path, person)
    assert json.loads(path.read_text()) == {"name": "Person"}


def test_output_cleanup_preserves_false_zero_and_array_structure() -> None:
    data = {"nested": [{"empty": [], "null": None, "zero": 0, "false": False, "text": "", "object": {}}]}
    assert without_null_fields(data) == {"nested": [{"zero": 0, "false": False, "text": "", "object": {}}]}
    assert without_null_fields([]) == []
    assert without_null_fields([[], {"empty": []}]) == [[], {}]


def test_page_check_persists_and_only_accept_advances_baseline(record: dict, tmp_path: Path) -> None:
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


def test_corrupt_previous_snapshot_and_check_fail_explicitly(record: dict, tmp_path: Path) -> None:
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


def test_cli_defaults_and_offline_commands(tmp_path: Path, record: dict, capsys: pytest.CaptureFixture) -> None:
    assert parse_args(["scrape", "--config", "karlsruhe"]).limit is None
    assert not parse_args(["scrape", "--config", "karlsruhe"]).all
    assert parse_args(["scrape", "--config", "karlsruhe", "--limit", "3"]).limit == 3
    assert parse_args(["scrape", "--config", "karlsruhe", "--all"]).all
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
async def _local_venue() -> AsyncGenerator[tuple[str, dict, list[str]]]:
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

    async def handler(request: web.Request) -> web.Response:
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


def test_http_pipeline_is_bounded_enriches_once_and_reports_errors(capsys: pytest.CaptureFixture) -> None:
    async def exercise() -> None:
        async with _local_venue() as (origin, config, requests):
            scraper = ConfigurableScraper(ScraperConfig.model_validate(config))
            records = await scraper.scrape(limit=1)
            assert requests == ["/list", "/programm/info/1/", "/ensemble/id/1/"]
            assert all("Biography" in m["description"] for m in records[0]["memberships"])
            progress = capsys.readouterr().err
            assert all(label in progress for label in ("Listing pages", "Productions", "People"))
            assert "100%" in progress and "1/1" in progress
            requests.clear()
            await scraper.scrape(limit=1, enrich_people=False)
            assert requests == ["/list", "/programm/info/1/"]
            assert "People" not in capsys.readouterr().err
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


def test_cli_page_and_check_fetch_exactly_one_page(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    async def exercise() -> None:
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
            output = json.loads(output_path.read_text())
            assert "description" not in output[0]
            assert "born" not in output[0]["memberships"][0]
            capsys.readouterr()
            assert (
                await asyncio.to_thread(
                    main,
                    [
                        "page",
                        "--config",
                        str(config_path),
                        "--url",
                        origin + "ensemble/id/1/",
                        "--kind",
                        "person",
                    ],
                )
                == 0
            )
            person = json.loads(capsys.readouterr().out)
            assert "born" not in person and "role" not in person
            assert "Biography" in person["description"]
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


def test_cli_scrapes_all_by_default_on_local_fixture(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    async def exercise() -> None:
        async with _local_venue() as (_origin, config, requests):
            config_path = tmp_path / "venue.json"
            config_path.write_text(json.dumps(config))
            assert (
                await asyncio.to_thread(
                    main,
                    [
                        "scrape",
                        "--config",
                        str(config_path),
                        "--output-dir",
                        str(tmp_path / "output"),
                    ],
                )
                == 0
            )
            assert requests[0] == "/list"
            assert sorted(requests[1:3]) == ["/programm/info/1/", "/programm/info/2/"]
            assert requests[3:] == ["/ensemble/id/1/"]
            snapshot = next((tmp_path / "output").rglob("*productions.json"))
            records = json.loads(snapshot.read_text())
            assert len(records) == 2
            assert "premiere" not in records[0]
            assert "born" not in records[0]["memberships"][0]
            output = capsys.readouterr()
            assert "Productions" in output.err and "2/2" in output.err
            assert "People" in output.err and "1/1" in output.err
            assert "Saved 2 productions" in output.out

    asyncio.run(exercise())


def test_pagination_is_bounded_and_failures_never_save_partial_scrapes(tmp_path: Path) -> None:
    async def exercise() -> None:
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
            assert requests[:2] == ["/paged-list", "/second-list"]
            assert sorted(requests[2:]) == [
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
