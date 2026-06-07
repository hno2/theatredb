import datetime as dt
import uuid
from dataclasses import dataclass
from typing import cast, get_args, get_origin

import pandas as pd
import streamlit as st
from pydantic import ValidationError
from sqlalchemy.orm import InstrumentedAttribute
from sqlmodel import SQLModel
from streamlit import session_state, user

from backend.entity_management import create_entity, delete_entity, list_entities, list_entities_where, update_entity
from backend.models import Person, TextContribution

FormValue = str | int | float | bool | dt.datetime | list[str] | dict | None


@dataclass(slots=True)
class _FieldUI:
    """Data class for field UI representation."""

    name: str
    label: str
    kind: str
    optional: bool
    options: list[str] | None = None
    fk_model: type[SQLModel] | None = None
    fk_display: str = "name"
    new_payload_field: str = "name"
    related_people_role: str | None = None
    related_people_label: str = "People"


@dataclass
class _FormErrors:
    fields: dict[str, str]
    global_error: str | None

    @classmethod
    def load(cls, form_key: str) -> _FormErrors:
        return cls(
            fields=session_state.get(f"_errors_{form_key}", {}),
            global_error=session_state.get(f"_errors_global_{form_key}"),
        )

    def save(self, form_key: str) -> None:
        session_state[f"_errors_{form_key}"] = self.fields
        session_state[f"_errors_global_{form_key}"] = self.global_error

    def clear(self, form_key: str) -> None:
        session_state.pop(f"_errors_{form_key}", None)
        session_state.pop(f"_errors_global_{form_key}", None)


def _id_of(entity: SQLModel) -> str:
    return str(entity.model_dump(mode="python")["id"])


def _field_error(errors: dict[str, str], field_name: str) -> str | None:
    return errors.get(field_name)


def _extract_validation_errors(exc: Exception) -> tuple[dict[str, str], str | None]:
    if isinstance(exc, ValidationError):
        field_errors: dict[str, str] = {}
        for err in exc.errors():
            loc = err.get("loc") or []
            field = str(loc[0]) if loc else ""
            msg = str(err.get("msg", "Invalid value"))
            if field:
                field_errors[field] = msg
        return field_errors, None if field_errors else str(exc)
    return {}, str(exc)


def _clear_form_widget_state(form_key: str, fields: list[_FieldUI]) -> None:
    for field in fields:
        session_state.pop(f"{form_key}_field_{field.name}", None)
        session_state.pop(f"{form_key}_fk_pick_{field.name}", None)
        session_state.pop(f"{form_key}_fk_people_{field.name}", None)


def _order_expr(model_cls: type[SQLModel], field_name: str) -> InstrumentedAttribute | None:
    return getattr(model_cls, field_name) if hasattr(model_cls, field_name) else None


def _actor_id() -> str | None:
    """Get the actor ID for auditing purposes, based on the current user or mock user."""
    src = user if getattr(user, "is_logged_in", False) else session_state.get("mock_user")
    return src and (getattr(src, "email", None) or getattr(src, "sub", None))


def _unwrap_optional(annotation: object) -> tuple[object, bool]:
    """Unwrap Optional type annotations to get the base type and whether it's optional."""
    args = get_args(annotation)
    non_none = [a for a in args if a is not type(None)]
    return (non_none[0], True) if len(non_none) == 1 < len(args) else (annotation, False)


_KIND_MAP: dict[object, str] = {
    bool: "bool",
    int: "int",
    float: "float",
    dt.date: "datetime",
    dt.datetime: "datetime",
    uuid.UUID: "uuid",
}


def _guess_kind(t: object) -> str:
    if get_origin(t) is list and get_args(t) == (str,):
        return "list_text"
    return _KIND_MAP.get(t, "text")  # type: ignore[arg-type]


def _build_fields(model_cls: type[SQLModel], *, field_overrides: dict | None = None) -> list[_FieldUI]:
    """Build field UI representations from a SQLModel class.

    Args:
        model_cls: The SQLModel class to build fields for.
        field_overrides: Optional dict of field overrides, keyed by field name. Each override can specify:
            - kind: Optional explicit kind (e.g. "multiselect", "datetime", "list_text", etc.)
            - label: Optional explicit label for the field
            - options: For select fields, the list of options to choose from
            - exclude: If true, the field will be excluded from the UI
    Returns:
        A list of _FieldUI instances representing the fields to render in the UI.
    """
    field_overrides = field_overrides or {}
    fields = []
    for name, field in model_cls.model_fields.items():
        ov: dict = field_overrides.get(name, {})
        if ov.get("exclude"):
            continue
        base_type, optional = _unwrap_optional(field.annotation)
        kind = str(ov.get("kind") or _guess_kind(base_type))
        if kind == "uuid" and name == "id":
            continue
        fields.append(
            _FieldUI(
                name=name,
                label=str(ov.get("label", name.replace("_", " ").title())),
                kind=kind,
                optional=optional,
                options=list(ov["options"]) if "options" in ov else None,
                fk_model=ov.get("fk_model"),
                fk_display=str(ov.get("fk_display", "name")),
                new_payload_field=str(ov.get("new_payload_field", "name")),
                related_people_role=ov.get("related_people_role"),
                related_people_label=str(ov.get("related_people_label", "People")),
            )
        )
    return fields


def _render_fk_select(field: _FieldUI, d: FormValue, *, form_key: str) -> FormValue:
    if field.fk_model is None:
        v = st.text_input(field.label, value="" if d is None else str(d))
        return v.strip() or None
    entities = list_entities(field.fk_model, order_by=_order_expr(field.fk_model, field.fk_display))
    options: list[str] = [str(getattr(e, field.fk_display, str(e))) for e in entities]
    id_by_label = {str(getattr(e, field.fk_display, str(e))): _id_of(e) for e in entities}
    default_choice: list[str] = []
    if d:
        d_str = str(d)
        for lbl, oid in id_by_label.items():
            if oid == d_str:
                default_choice = [lbl]
                break

    choice = st.multiselect(
        field.label,
        options=options,
        default=default_choice,
        max_selections=1,
        accept_new_options=True,
        key=f"{form_key}_fk_pick_{field.name}",
    )

    selected = choice[0].strip() if choice else ""
    if not selected:
        return None

    is_existing = selected in id_by_label
    people: list[str] = []
    if field.related_people_role and not is_existing:
        person_names = [
            str(p.model_dump(mode="python").get("name", "")) for p in list_entities(Person, order_by=Person.name)
        ]
        people = st.multiselect(
            f"↳ {field.related_people_label}",
            options=person_names,
            default=[],
            accept_new_options=True,
            key=f"{form_key}_fk_people_{field.name}",
        )

    if not is_existing:
        return {
            "__new__": field.fk_model,
            "name": selected,
            "payload_field": field.new_payload_field,
            "people": people,
            "people_role": field.related_people_role,
        }
    result = id_by_label[selected]
    return result


def _field_key(form_key: str, name: str) -> str:
    return f"{form_key}_field_{name}"


def _show_error(err: str | None) -> None:
    if err:
        st.caption(f":red[{err}]")


def _render_bool(field: _FieldUI, d: bool | None, key: str) -> FormValue:
    return st.checkbox(field.label, value=bool(d), key=key)


def _render_numeric(field: _FieldUI, d: float | str | None, key: str) -> FormValue:
    raw = st.text_input(field.label, value="" if d is None else str(d), key=key).strip()
    if not raw:
        return None
    try:
        return (int if field.kind == "int" else float)(raw)
    except ValueError:
        return raw


def _render_datetime(field: _FieldUI, d: dt.datetime | dt.date | None) -> FormValue:
    if isinstance(d, dt.datetime):
        date = d.date()
    elif isinstance(d, dt.date):
        date = d
    else:
        date = None
    picked = st.date_input(field.label, value=date, format="DD.MM.YYYY", min_value=dt.date(100, 1, 1))
    return picked or None


def _render_multiselect(field: _FieldUI, d: str, key: str, accept_new: bool = True) -> FormValue:
    return st.multiselect(
        field.label,
        options=field.options or [],
        default=d if isinstance(d, list) else [],
        key=key,
        accept_new_options=accept_new,
    )


def _render_list_text(field: _FieldUI, d: str, key: str) -> FormValue:
    text = st.text_area(
        field.label,
        value="\n".join(d if isinstance(d, list) else []),
        key=key,
    )
    return [line.strip() for line in text.splitlines() if line.strip()]


def _render_text(field: _FieldUI, d: str, key: str) -> FormValue:
    return st.text_input(field.label, value="" if d is None else str(d), key=key).strip() or None


def _render_field(field: _FieldUI, defaults: dict, *, form_key: str, errors: dict[str, str]) -> FormValue:
    d = defaults.get(field.name)
    err = _field_error(errors, field.name)
    key = _field_key(form_key, field.name)

    match field.kind:
        case "bool":
            val = _render_bool(field, d, key)
        case "int" | "float":
            val = _render_numeric(field, d, key)
        case "datetime":
            val = _render_datetime(field, d)
        case "multiselect":
            val = _render_multiselect(field, d, key)
        case "list_text":
            val = _render_list_text(field, d, key)
        case "fk_select":
            val = _render_fk_select(field, d, form_key=form_key)
        case _:
            val = _render_text(field, d, key)

    _show_error(err)
    return val


def _link_people_to_text(entity: SQLModel, people: list[str], role: str, actor_id: str | None) -> None:
    """Create Person entities and TextContribution links."""
    text_id = _id_of(entity)
    for person_name in (p.strip() for p in people if p.strip()):
        existing = list_entities_where(Person, filter_field="name", filter_value=person_name, order_by=Person.name)
        if existing:
            person = existing[0]
            pdata = person.model_dump(mode="python")
            ptypes: list[str] = list(pdata.get("type") or [])
            if role not in ptypes:
                update_entity(Person, entity_id=str(pdata["id"]), payload={"type": [*ptypes, role]}, actor_id=actor_id)
        else:
            person = create_entity(
                Person,
                payload={"name": person_name, "type": [role]},
                actor_id=actor_id,
                get_if_exists={"name": person_name},
            )

        person_id = _id_of(person)
        already_linked = any(
            _id_of(c)
            and str(c.model_dump(mode="python").get("text_id")) == text_id
            and str(c.model_dump(mode="python").get("person_id")) == person_id
            and c.model_dump(mode="python").get("role") == role
            for c in list_entities(TextContribution, order_by="id")
        )
        if not already_linked:
            create_entity(
                TextContribution, payload={"text_id": text_id, "person_id": person_id, "role": role}, actor_id=actor_id
            )


def _resolve_new_entity(key: str, v: dict, actor_id: str | None) -> str | None:
    """Resolve a potentially new entity specified in the payload, creating it if necessary."""
    model_cls = v["__new__"]
    name = (v.get("name") or "").strip()
    if not name:
        return None
    payload_field = str(v.get("payload_field") or "name")
    entity = create_entity(
        model_cls, payload={payload_field: name}, actor_id=actor_id, get_if_exists={payload_field: name}
    )
    people = cast(list[str], v.get("people") or [])
    role = cast(str | None, v.get("people_role"))
    if people and role and model_cls.__name__ == "Text":
        _link_people_to_text(entity, people, role, actor_id)
    return _id_of(entity)


def _resolve_new_entities(vals: dict, actor_id: str | None) -> dict:
    """Resolve all potentially new entities in the given dict, creating them if necessary."""
    return {
        k: (_resolve_new_entity(k, v, actor_id) if isinstance(v, dict) and "__new__" in v else v)
        for k, v in vals.items()
    }


def _render_member_rows(
    members: list,
    fields: list[_FieldUI],
    fk_lookups: dict[str, dict[str, str]],
    child_model: type[SQLModel],
    child_name: str,
    actor: str | None,
) -> None:
    header_cols = st.columns([*[3] * len(fields), 1])
    for col, f in zip(header_cols[:-1], fields, strict=False):
        col.markdown(f"**{f.label}**")
    for member in members:
        m_dict = member.model_dump(mode="python")
        m_id = str(m_dict["id"])
        row_cols = st.columns([*[3] * len(fields), 1])
        for col, f in zip(row_cols[:-1], fields, strict=False):
            val = m_dict.get(f.name)
            if f.kind == "fk_select" and f.name in fk_lookups:
                val = fk_lookups[f.name].get(str(val) if val is not None else "", str(val) or "—")
            col.write(str(val) if val is not None else "—")
        if row_cols[-1].button("✕", key=f"del_{child_name}_{m_id}", help="Remove"):
            delete_entity(child_model, entity_id=m_id, actor_id=actor)
            st.rerun()


def _render_inline_relation_table_editor(  # noqa: C901
    *,
    parent_id: str,
    relation: dict,
    child_model: type[SQLModel],
    child_name: str,
    relation_key: str,
    fk_field: str,
    fields: list[_FieldUI],
    fk_lookups: dict[str, dict[str, str]],
    members: list,
    actor: str | None,
) -> None:
    """Render editable table for child relation and persist full table on save."""
    rows: list[dict] = []
    for member in members:
        m_dict = member.model_dump(mode="python")
        row = {"__id": str(m_dict["id"])}
        for f in fields:
            val = m_dict.get(f.name)
            if f.kind == "fk_select" and f.name in fk_lookups:
                val = fk_lookups[f.name].get(str(val) if val is not None else "", "")
            row[f.name] = val
        rows.append(row)
    if not rows:
        rows.append({"__id": "", **{f.name: None for f in fields}})

    column_config: dict = {
        "__id": None,
    }
    for f in fields:
        if f.kind in {"int", "float"}:
            column_config[f.name] = st.column_config.NumberColumn(f.label)
        else:
            column_config[f.name] = st.column_config.TextColumn(f.label)

    edited = st.data_editor(
        pd.DataFrame(rows),
        num_rows="dynamic",
        hide_index=True,
        key=f"table_editor_{relation_key}_{parent_id}",
        column_config=column_config,
    )

    if st.button(f"Save {relation.get('title', child_name)}", key=f"save_{relation_key}_{parent_id}"):
        existing_by_id = {_id_of(m): m for m in members}
        keep_ids: set[str] = set()
        fk_autocreate = cast(dict, relation.get("fk_autocreate", {}))

        for _, row in edited.iterrows():
            payload: dict = {}
            for f in fields:
                raw = row.get(f.name)
                if f.kind == "fk_select":
                    label = "" if raw is None else str(raw).strip()
                    if not label:
                        payload[f.name] = None
                    else:
                        reverse_lookup = {v: k for k, v in fk_lookups.get(f.name, {}).items()}
                        if label in reverse_lookup:
                            payload[f.name] = reverse_lookup[label]
                        else:
                            auto_cfg = cast(dict | None, fk_autocreate.get(f.name))
                            if auto_cfg and f.fk_model is not None:
                                payload_field = cast(str, auto_cfg.get("payload_field", "name"))
                                new_payload: dict[str, object] = {payload_field: label}
                                infer_type_from = cast(str | None, auto_cfg.get("infer_type_from"))
                                if infer_type_from:
                                    inferred = row.get(infer_type_from)
                                    if inferred is not None and str(inferred).strip():
                                        new_payload["type"] = [str(inferred).strip()]
                                created = create_entity(
                                    f.fk_model,
                                    payload=new_payload,
                                    actor_id=actor,
                                    get_if_exists={payload_field: label},
                                )
                                created_id = _id_of(created)
                                payload[f.name] = created_id
                                fk_lookups.setdefault(f.name, {})[created_id] = label
                            else:
                                payload[f.name] = None
                elif f.kind == "int":
                    payload[f.name] = int(raw) if raw not in (None, "") else None
                elif f.kind == "float":
                    payload[f.name] = float(raw) if raw not in (None, "") else None
                else:
                    payload[f.name] = raw

            if all(v in (None, "", []) for v in payload.values()):
                continue

            payload[fk_field] = parent_id
            row_id = "" if row.get("__id") is None else str(row.get("__id")).strip()
            if row_id and row_id in existing_by_id:
                update_entity(
                    child_model,
                    entity_id=row_id,
                    payload=payload,
                    actor_id=actor,
                )
                keep_ids.add(row_id)
            else:
                created_member = create_entity(
                    child_model,
                    payload=payload,
                    actor_id=actor,
                )
                keep_ids.add(_id_of(created_member))

        for existing_id in existing_by_id:
            if existing_id not in keep_ids:
                delete_entity(
                    child_model,
                    entity_id=existing_id,
                    actor_id=actor,
                )

        st.session_state["_flash"] = f"{relation.get('title', child_name)} saved."
        st.rerun()


def _render_inline_relations(
    parent_id: str,
    relations: list[dict],
    actor: str | None,
) -> None:
    """Render inline child-relation tables (e.g. cast/crew for a production)."""
    for relation in relations:
        child_model: type[SQLModel] = relation["model"]
        fk_field: str = relation["fk_field"]
        title: str = relation.get("title", child_model.__name__)
        child_name = child_model.__name__
        order_by: str = relation.get("order_by", "id")

        # build fields: exclude id (done in _build_fields) + fk_field
        merged_overrides = {fk_field: {"exclude": True}, **relation.get("field_overrides", {})}
        fields = _build_fields(child_model, field_overrides=merged_overrides)

        # build FK display lookup dicts
        fk_lookups: dict[str, dict[str, str]] = {}
        for f in fields:
            if f.kind == "fk_select" and f.fk_model is not None:
                fk_lookups[f.name] = {
                    _id_of(e): getattr(e, f.fk_display, "?")
                    for e in list_entities(f.fk_model, order_by=_order_expr(f.fk_model, f.fk_display))
                }

        st.subheader(f":material/group: {title}")

        members = list_entities_where(child_model, filter_field=fk_field, filter_value=parent_id, order_by=order_by)

        if relation.get("editable_table", False):
            relation_key = f"{child_name}_{fk_field}"
            _render_inline_relation_table_editor(
                parent_id=parent_id,
                relation=relation,
                child_model=child_model,
                child_name=child_name,
                relation_key=relation_key,
                fk_field=fk_field,
                fields=fields,
                fk_lookups=fk_lookups,
                members=members,
                actor=actor,
            )
            continue

        if members:
            _render_member_rows(members, fields, fk_lookups, child_model, child_name, actor)
        else:
            st.caption("None yet.")

        add_form_key = f"add_{child_name}_{parent_id}"
        add_errors = _FormErrors.load(add_form_key)
        with st.form(add_form_key, clear_on_submit=False):
            new_vals = {f.name: _render_field(f, {}, form_key=add_form_key, errors=add_errors.fields) for f in fields}
            if add_errors.global_error:
                st.error(add_errors.global_error)
            if st.form_submit_button(f"Add to {title}", icon=":material/add_circle:", use_container_width=False):
                try:
                    new_vals[fk_field] = parent_id
                    resolved = _resolve_new_entities(new_vals, actor)
                    create_entity(child_model, payload=resolved, actor_id=actor)
                    add_errors.clear(add_form_key)
                    _clear_form_widget_state(add_form_key, fields)
                except Exception as exc:  # noqa: BLE001
                    _FormErrors(*_extract_validation_errors(exc)).save(add_form_key)
                st.rerun()


def _entity_to_row(e: SQLModel) -> dict:
    row = e.model_dump(mode="json")
    if "id" in row:
        row["id"] = str(row["id"])
    return row


def _load_df(model_cls: type[SQLModel]) -> pd.DataFrame:
    rows = [_entity_to_row(e) for e in list_entities(model_cls, order_by=None)]
    df = pd.DataFrame(rows)
    if not df.empty and "id" in df.columns:
        df = df.set_index("id", drop=False)
    return df


def _render_table(df: pd.DataFrame, fields: list[_FieldUI], name: str) -> dict | None:
    df_view = df.drop(columns=["id"], errors="ignore").copy()
    for f in fields:
        if f.kind != "fk_select" or f.fk_model is None or f.name not in df_view.columns:
            continue
        lookup = {
            str(e.model_dump(mode="python").get("id")): str(getattr(e, f.fk_display, ""))
            for e in list_entities(f.fk_model, order_by=_order_expr(f.fk_model, f.fk_display))
        }
        display_col = f.name.removesuffix("_id")
        df_view[display_col] = [lookup.get(str(v), "") if pd.notna(v) and str(v) else "" for v in df_view[f.name]]
        df_view = df_view.drop(columns=[f.name], errors="ignore")

    sel = st.dataframe(
        df_view, hide_index=True, key=f"table_{name.lower()}", on_select="rerun", selection_mode="single-row"
    )
    sel_rows = sel.get("selection", {}).get("rows", [])
    return df.iloc[sel_rows[0]].to_dict() if len(sel_rows) == 1 and not df.empty else None


def _render_edit_form(model_cls: type[SQLModel], fields: list[_FieldUI], sel_row: dict, actor: str) -> None:
    st.subheader(":material/edit: Edit")
    edit_form_key = f"edit_{model_cls.__name__.lower()}"
    edit_errors: _FormErrors = _FormErrors.load(edit_form_key)
    with st.form(edit_form_key, clear_on_submit=False):
        vals = {f.name: _render_field(f, sel_row, form_key=edit_form_key, errors=edit_errors.fields) for f in fields}
        save, delete = st.columns(2)
        if edit_errors.global_error:
            st.error(edit_errors.global_error)
        if save.form_submit_button("Save", icon=":material/save:", width="stretch", type="primary"):
            try:
                resolved = _resolve_new_entities(vals, actor)
                update_entity(model_cls, entity_id=sel_row.get("id"), payload=resolved, actor_id=actor)
                st.session_state["_flash"] = "Saved."
                edit_errors.clear(edit_form_key)
            except Exception as exc:  # noqa: BLE001
                _FormErrors(*_extract_validation_errors(exc)).save(edit_form_key)
            st.rerun()
        if delete.form_submit_button("Delete", icon=":material/delete:", width="stretch", type="secondary"):
            delete_entity(model_cls, entity_id=sel_row.get("id"), actor_id=actor)
            st.session_state["_flash"] = "Deleted."
            st.rerun()


def _render_add_form(model_cls: type[SQLModel], fields: list[_FieldUI], actor: str) -> None:
    st.subheader(":material/add_circle: Add")
    add_form_key = f"add_{model_cls.__name__.lower()}"
    add_errors = _FormErrors.load(add_form_key)
    with st.form(add_form_key, clear_on_submit=False):
        vals = {f.name: _render_field(f, {}, form_key=add_form_key, errors=add_errors.fields) for f in fields}
        if add_errors.global_error:
            st.error(add_errors.global_error)
        if st.form_submit_button("Add", icon=":material/add_circle:", use_container_width=True, type="primary"):
            try:
                resolved = _resolve_new_entities(vals, actor)
                create_entity(model_cls, payload=resolved, actor_id=actor)
                st.session_state["_flash"] = "Added."
                add_errors.clear(add_form_key)
                _clear_form_widget_state(add_form_key, fields)
            except Exception as exc:  # noqa: BLE001
                _FormErrors(*_extract_validation_errors(exc)).save(add_form_key)
            st.rerun()


def render_sqlmodel_crud(
    *,
    model_cls: type[SQLModel],
    title: str | None = None,
    field_overrides: dict | None = None,
    inline_relations: list[dict] | None = None,
) -> None:
    """Render a CRUD UI for a given SQLModel class.

    Args:
        model_cls: The SQLModel class to render the CRUD UI for.
        title: Optional title for the page. Defaults to the model class name.
        field_overrides: Optional dict of field overrides, keyed by field name. Each override can specify:
            - kind: Optional explicit kind (e.g. "multiselect", "datetime", "list_text", etc.)
            - label: Optional explicit label for the field
            - options: For select fields, the list of options to choose from
            - exclude: If true, the field will be excluded from the UI
        inline_relations: Optional list of child relation configs. Each config dict:
            - model: child SQLModel class
            - fk_field: field name on child pointing to parent id
            - title: display title for the section
            - order_by: field to sort children by (default "id")
            - field_overrides: per-field overrides for child fields (same format as above)
    """
    fields = _build_fields(model_cls, field_overrides=field_overrides)
    actor = _actor_id()
    df = _load_df(model_cls)
    st.title(title or f"{model_cls.__name__}s Management")
    sel_row = _render_table(df, fields, model_cls.__name__)
    if sel_row:
        _render_edit_form(model_cls, fields, sel_row, actor)
        if inline_relations:
            _render_inline_relations(str(sel_row.get("id")), inline_relations, actor)
    else:
        st.caption(f"Select row to edit {model_cls.__name__}.")
        _render_add_form(model_cls, fields, actor)
