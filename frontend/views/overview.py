import streamlit as st
from backend.entity_management import list_entities, count_entities
from backend.models import Organization, Person, Production, Text, Award, Source
import pandas as pd

for model_cls in [Organization, Person, Production, Text, Award, Source]:
    st.write(f"**{model_cls.__name__}**: {count_entities(model_cls)} entries")

st.write("## Wo sind die Theater?")
data = pd.DataFrame(
    [(orga.name, orga.lat, orga.lon) for orga in list_entities(Organization, order_by="name") if orga.lat and orga.lon],
    columns=["name", "lat", "lon"],
)
st.map(data)
