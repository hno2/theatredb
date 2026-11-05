from itertools import starmap
from typing import cast
from urllib.parse import urlencode

import pandas as pd
import pydeck as pdk
import streamlit as st

from backend.entity_management import count_entities, list_entities
from backend.models import Award, Organization, Person, Production, Source, Text
from frontend.views.helpers import has_coordinates


def organization_data(organizations: list[Organization]) -> pd.DataFrame:
    """Prepare directory records, including organizations without coordinates."""
    return pd.DataFrame(
        [
            {
                "id": str(org.id),
                "name": org.name,
                "city": (org.city or "").strip(),
                "country": (org.country or "").strip(),
                "lat": org.lat,
                "lon": org.lon,
                "detail": f"detail?{urlencode({'kind': 'organization', 'id': str(org.id)})}",
            }
            for org in organizations
        ],
        columns=["id", "name", "city", "country", "lat", "lon", "detail"],
    )


def theater_map(data: pd.DataFrame) -> pdk.Deck:
    """Build a selectable map with a stable layer ID for Streamlit selections."""
    view = pdk.data_utils.compute_view(data[["lon", "lat"]])
    view.zoom = min(cast(float, view.zoom) - 1, 11)
    return pdk.Deck(
        map_style=None,
        initial_view_state=view,
        layers=[
            pdk.Layer(
                "ScatterplotLayer",
                id="theaters",
                data=data.to_dict("records"),
                get_position="[lon, lat]",
                get_fill_color=[139, 58, 50, 200],
                get_line_color=[255, 255, 255],
                get_radius=650,
                radius_min_pixels=5,
                radius_max_pixels=14,
                line_width_min_pixels=1,
                stroked=True,
                pickable=True,
                auto_highlight=True,
            ),
        ],
        tooltip={"text": "{name}\n{city}\nAnklicken, um den Detail-Link anzuzeigen."},
    )


def render_location_links(data: pd.DataFrame, entity_id: str) -> None:
    """Show every theatre at the selected point, ignoring stale selections."""
    selected_data = data.loc[data["id"] == entity_id, :]
    if not selected_data.empty:
        position = selected_data.iloc[0]
        at_location = data.loc[(data["lat"] == position["lat"]) & (data["lon"] == position["lon"]), :]
        if len(at_location) > 1:
            st.caption(f"{len(at_location)} Einträge an diesem Standort")
        for theater in at_location.to_dict("records"):
            with st.container(border=True):
                st.text(theater["name"])
                st.caption(" · ".join(part for part in (theater["city"], theater["country"]) if part))
                st.page_link(
                    "views/detail.py",
                    label="Theater ansehen",
                    icon=":material/arrow_forward:",
                    query_params={"kind": "organization", "id": theater["id"]},
                )


def render_overview() -> None:
    """Render the German-language entry point to the theatre database."""
    st.caption("THEATERDB / THEATER IM DEUTSCHSPRACHIGEN RAUM")
    st.title("Theaterlandschaft entdecken")
    st.markdown(
        "Welche Menschen, Stücke und Produktionen verbinden Theater miteinander? "
        "TheaterDB sammelt Informationen über Theater im deutschsprachigen Raum. "
        "Ziel ist es, **Muster und Verbindungen zwischen den Häusern sichtbar zu machen** "
        "und die Theaterlandschaft gemeinsam zu erkunden."
    )

    data = organization_data(list_entities(Organization, order_by="name"))
    mapped = data.loc[list(starmap(has_coordinates, data[["lat", "lon"]].itertuples(index=False, name=None))), :]
    cities = data.loc[data["city"] != "", ["city", "country"]].drop_duplicates()
    counts = {model: count_entities(model) for model in (Production, Person, Text, Award, Source)}

    st.space("small")
    metrics = [
        ("Theater & Organisationen", len(data), "Alle erfassten Häuser und Organisationen."),
        ("Orte", len(cities), "Erfasste Kombinationen aus Ort und Land; fehlende Ortsangaben zählen nicht."),
        ("Produktionen", counts[Production], "Alle in der Datenbank erfassten Produktionen."),
        ("Personen", counts[Person], "Alle in der Datenbank erfassten Personen."),
    ]
    for column, (label, value, help_text) in zip(st.columns(4), metrics, strict=True):
        column.metric(label, f"{value:,}".replace(",", "."), help=help_text, border=True)
    st.caption(
        f"Erfasster Datenbestand · {counts[Text]:,} Texte · {counts[Award]:,} Auszeichnungen · "
        f"{counts[Source]:,} Quellen. Keine vollständige Bestandsaufnahme der Theaterlandschaft.".replace(",", ".")
    )
    st.divider()

    st.subheader("Häuser finden")
    search = st.text_input(
        "Theater, Ort oder Land suchen",
        placeholder="Zum Beispiel: Burgtheater, Berlin oder Österreich",
    ).strip()
    filtered = data
    if search:
        searchable = data["name"] + " " + data["city"] + " " + data["country"]
        filtered = data.loc[searchable.str.contains(search, case=False, regex=False), :]
    visible = mapped.loc[mapped.index.intersection(filtered.index), :]

    st.caption(
        f"{len(visible)} von {len(filtered)} Treffern auf der Karte. "
        "Punkt anklicken und über den Link das Theater öffnen."
    )
    if not visible.empty:
        event = st.pydeck_chart(
            theater_map(visible),
            key=f"theater_map_{search.casefold()}",
            on_select="rerun",
            selection_mode="single-object",
            height=480,
        )
        selected = event["selection"].get("objects", {}).get("theaters", [])
        if selected:
            render_location_links(visible, selected[0]["id"])
    elif filtered.empty:
        st.info("Keine Theater gefunden. Versuche einen anderen Suchbegriff.")
    else:
        st.info("Für diese Treffer sind noch keine gültigen Koordinaten hinterlegt. Nutze das Verzeichnis unten.")

    coverage_column, cities_column = st.columns(2, gap="large")
    with coverage_column:
        st.markdown("#### Was die Karte zeigt")
        st.write(
            "Jeder Punkt steht für eine erfasste Organisation mit gültigen Koordinaten. "
            "Mehrere Häuser können am selben Standort liegen."
        )
        coverage = len(mapped) / len(data) if len(data) else 0
        st.progress(coverage, text=f"{coverage:.0%} der Einträge mit Kartenposition")
        st.caption(
            f"{len(data) - len(mapped)} Einträge ohne gültige Koordinaten. Sie bleiben im Verzeichnis auffindbar."
        )
    with cities_column:
        st.markdown("#### Orte mit vielen Einträgen")
        locations = data.loc[data["city"] != "", ["city", "country"]].copy()
        if not locations.empty:
            locations["Ort"] = locations.apply(
                lambda row: " · ".join(part for part in (row["city"], row["country"]) if part),
                axis=1,
            )
            top_cities = locations["Ort"].value_counts().head(5).rename("Einträge").reset_index()
            st.bar_chart(
                top_cities,
                x="Ort",
                y="Einträge",
                horizontal=True,
                color="#8b3a32",
                height=220,
                sort="-Einträge",
            )
            st.caption("Gesamter Datenbestand, unabhängig von der Suche. Keine Rangliste nach Größe oder Bedeutung.")
        else:
            st.caption("Noch keine Ortsangaben vorhanden.")

    st.subheader("Theaterverzeichnis")
    st.caption("Alle Suchtreffer, auch ohne Kartenposition. Über „Ansehen“ öffnest du den jeweiligen Eintrag.")
    if not filtered.empty:
        st.dataframe(
            filtered[["name", "city", "country", "detail"]],
            hide_index=True,
            column_config={
                "name": st.column_config.TextColumn("Theater / Organisation", width="large"),
                "city": "Ort",
                "country": "Land",
                "detail": st.column_config.LinkColumn("Eintrag", display_text="Ansehen"),
            },
        )
    elif data.empty:
        st.info("Noch keine Theater erfasst. Sobald Einträge vorliegen, erscheinen sie hier und auf der Karte.")
    st.divider()
    st.caption(
        "Die Datenbasis wächst. Fehlende Angaben bedeuten nicht, dass es keine Verbindung gibt. "
        "Die Detailseiten zeigen den bisherigen Stand zu Häusern, Produktionen und beteiligten Personen."
    )


if __name__ == "__main__":
    render_overview()
