"""Local description embeddings: chunking, indexing, hybrid search, tag suggestions and cluster discovery.

Only Production and Text descriptions are embedded. Vectors live in PostgreSQL (pgvector); the encoder runs
locally via the optional `embeddings` extra, so no description text leaves the machine.
"""

import argparse
import hashlib
import os
import re
import sys
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from functools import cache
from itertools import batched, groupby
from operator import attrgetter, itemgetter
from typing import Any, Literal, cast

from sqlalchemy import Float, delete, exists, func
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import Session, col, select

from backend.models import (
    EMBEDDING_DIMENSIONS,
    DescriptionChunk,
    Production,
    ProductionTag,
    Tag,
    TagLayer,
    TagOrigin,
    TagStatus,
    Text,
)
from backend.taxonomy import _audit

MODEL_ID = os.environ.get("THEATERDB_EMBEDDING_MODEL", "microsoft/harrier-oss-v1-270m")
MODEL_REVISION = os.environ.get("THEATERDB_EMBEDDING_REVISION")  # pin a commit hash in production
CHUNKING = "chunk-v1"
CHUNK_CHARS = 1200
SEARCH_INSTRUCTION = "Given a search query about theatre, retrieve production or play descriptions that match it"
TAG_INSTRUCTION = "Given a theme of a theatre production, retrieve production descriptions about this theme"

Kind = Literal["production", "text"]
# texts, optional query instruction -> L2-normalized vectors
Encoder = Callable[[Sequence[str], str | None], list[list[float]]]


def pipeline_key() -> str:
    """Identify model, revision and chunking; vectors from other pipelines are stale."""
    return f"{MODEL_ID}@{MODEL_REVISION or 'main'}/{CHUNKING}"


@cache
def local_encoder() -> Encoder:
    """Load the sentence-transformers model once per process."""
    try:
        from sentence_transformers import SentenceTransformer  # ruff: ignore[import-outside-top-level]
    except ImportError as exc:
        raise RuntimeError("Semantic search needs the embeddings extra: uv sync --extra embeddings") from exc
    model = SentenceTransformer(MODEL_ID, revision=MODEL_REVISION, model_kwargs={"dtype": "auto"})

    def encode(texts: Sequence[str], instruction: str | None) -> list[list[float]]:
        prompt = f"Instruct: {instruction}\nQuery: " if instruction else None
        vectors = model.encode(list(texts), prompt=prompt, normalize_embeddings=True, batch_size=16)
        return vectors.tolist()

    return encode


def _encode(encoder: Encoder, texts: Sequence[str], instruction: str | None = None) -> list[list[float]]:
    vectors = encoder(texts, instruction)
    if any(len(vector) != EMBEDDING_DIMENSIONS for vector in vectors):
        raise ValueError(f"{MODEL_ID} does not produce {EMBEDDING_DIMENSIONS}-dimensional vectors")
    return vectors


def _owner(kind: Kind) -> tuple[type[Production | Text], Any, Any]:
    if kind == "production":
        return Production, col(DescriptionChunk.production_id), col(Production.name)
    return Text, col(DescriptionChunk.text_id), col(Text.title)


def description_hash(title: str, description: str) -> str:
    """Hash what is embedded, so title or description edits trigger re-indexing."""
    return hashlib.sha256(f"{title}\n\n{description}".encode()).hexdigest()


def chunk_description(description: str, size: int = CHUNK_CHARS) -> list[str]:
    """Merge paragraphs up to `size` characters; split longer paragraphs at sentence ends."""
    pieces = [
        sentence
        for paragraph in re.split(r"\n\s*\n", description.strip())
        for sentence in ([paragraph] if len(paragraph) <= size else re.split(r"(?<=[.!?…])\s+", paragraph))
        if sentence.strip()
    ]
    chunks: list[str] = []
    for piece in pieces:
        if chunks and len(chunks[-1]) + len(piece) + 2 <= size:
            chunks[-1] = f"{chunks[-1]}\n\n{piece}"
        else:
            chunks.append(piece.strip())
    return chunks


def _embed_batch(session: Session, kind: Kind, rows: Sequence[tuple], encoder: Encoder, pipeline: str) -> None:
    _, owner, _ = _owner(kind)
    session.exec(delete(DescriptionChunk).where(owner.in_([row[0] for row in rows])))
    pieces = [
        (owner_id, number, title, chunk, digest)
        for owner_id, title, description, digest in rows
        for number, chunk in enumerate(chunk_description(description))
    ]
    vectors = _encode(encoder, [f"{title}\n\n{chunk}" for _, _, title, chunk, _ in pieces])
    for (owner_id, number, _, chunk, digest), vector in zip(pieces, vectors, strict=True):
        session.add(
            DescriptionChunk(
                **{f"{kind}_id": owner_id},
                chunk_no=number,
                content=chunk,
                description_hash=digest,
                pipeline=pipeline,
                embedding=vector,
            )
        )


def _stale_rows(session: Session, kind: Kind, pipeline: str) -> list[tuple]:
    model, owner, name = _owner(kind)
    indexed: dict[uuid.UUID, set[tuple[str, str]]] = defaultdict(set)
    for owner_id, digest, row_pipeline in session.exec(
        select(owner, DescriptionChunk.description_hash, DescriptionChunk.pipeline).where(owner.is_not(None))
    ):
        indexed[owner_id].add((digest, row_pipeline))
    description = col(model.description)
    rows = session.exec(select(model.id, name, description).where(func.length(description) > 0).order_by(col(model.id)))
    return [
        (owner_id, title, text, digest)
        for owner_id, title, text in rows
        if indexed.get(owner_id) != {(digest := description_hash(title, text or ""), pipeline)}
    ]


def index_descriptions(
    session: Session,
    *,
    kinds: Sequence[Kind] = ("production", "text"),
    limit: int | None = None,
    encoder: Encoder | None = None,
    batch_size: int = 32,
) -> int:
    """Embed new or changed descriptions (at most `limit` entities); unchanged ones are skipped."""
    pipeline, done = pipeline_key(), 0
    for kind in kinds:
        model, owner, _ = _owner(kind)
        described = select(model.id).where(func.length(col(model.description)) > 0)
        session.exec(delete(DescriptionChunk).where(owner.is_not(None), owner.not_in(described)))
        session.commit()
        stale = _stale_rows(session, kind, pipeline)
        if limit is not None:
            stale = stale[: limit - done]
        for rows in batched(stale, batch_size, strict=False):
            encoder = encoder or local_encoder()
            _embed_batch(session, kind, rows, encoder, pipeline)
            session.commit()
        done += len(stale)
    return done


def reciprocal_rank_fusion(*rankings: Sequence[uuid.UUID], k: int = 60) -> list[uuid.UUID]:
    """Fuse ranked id lists without comparing their incompatible scores."""
    scores: dict[uuid.UUID, float] = defaultdict(float)
    for ranking in rankings:
        for rank, key in enumerate(ranking, 1):
            scores[key] += 1 / (k + rank)
    return sorted(scores, key=lambda key: scores[key], reverse=True)


def search_descriptions(
    session: Session,
    query: str,
    *,
    kind: Kind,
    filters: Sequence[ColumnElement[bool]] = (),
    encoder: Encoder | None = None,
    limit: int = 20,
    candidates: int = 100,
) -> list[dict[str, Any]]:
    """Hybrid PostgreSQL search: nearest description chunks plus German full text, fused by RRF."""
    model, owner, name = _owner(kind)
    vector = _encode(encoder or local_encoder(), [query], SEARCH_INSTRUCTION)[0]
    distance = col(DescriptionChunk.embedding).op("<=>", return_type=Float)(vector).label("distance")  # cosine
    # shortcut: exact scan over all chunks; add an HNSW index once chunk counts reach the millions.
    nearest = (
        select(owner.label("id"), col(DescriptionChunk.content).label("content"), distance)
        .where(owner.is_not(None), DescriptionChunk.pipeline == pipeline_key())
        .ext(distinct_on(owner))
        .order_by(owner, distance)
        .subquery()
    )
    dense = dict(
        session.exec(select(nearest.c.id, nearest.c.content).order_by(nearest.c.distance).limit(candidates)).all()
    )
    document = func.to_tsvector("german", col(model.description))
    tsquery = func.websearch_to_tsquery("german", query)
    lexical = session.exec(
        select(model.id)
        .where(document.op("@@")(tsquery))
        .order_by(func.ts_rank_cd(document, tsquery).desc())
        .limit(candidates)
    ).all()
    ranked = reciprocal_rank_fusion(list(dense), lexical)
    rows = {
        row[0]: row
        for row in session.exec(
            select(model.id, name, col(model.description)).where(col(model.id).in_(ranked), *filters)
        )
    }
    hits = [
        {"id": key, "title": rows[key][1], "snippet": dense.get(key) or rows[key][2] or "", "lexical": key in lexical}
        for key in ranked
        if key in rows
    ]
    return hits[:limit]


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _suggest(session: Session, chunk: DescriptionChunk, tag: Tag, score: float) -> bool:
    production_id = chunk.production_id
    if production_id is None:
        return False
    row = session.exec(
        select(ProductionTag).where(ProductionTag.production_id == production_id, ProductionTag.tag_code == tag.code)
    ).one_or_none()
    # Manual, source, published and rejected rows are decisions; the model never overrides them.
    if row is not None and (row.origin != TagOrigin.MODEL or row.status != TagStatus.SUGGESTED):
        return False
    operation = "update" if row else "insert"
    row = row or ProductionTag(
        production_id=production_id, tag_code=tag.code, status=TagStatus.SUGGESTED, origin=TagOrigin.MODEL
    )
    row.score, row.evidence = round(score, 4), chunk.content[:500]
    row.description_hash, row.pipeline, row.decided_by = chunk.description_hash, chunk.pipeline, "model"
    session.add(row)
    _audit(session, row, operation, None)
    return True


def _unsuggested_productions(session: Session, pipeline: str, limit: int | None) -> list[uuid.UUID]:
    done = select(ProductionTag.id).where(
        ProductionTag.production_id == DescriptionChunk.production_id,
        ProductionTag.origin == TagOrigin.MODEL,
        ProductionTag.pipeline == pipeline,
        ProductionTag.description_hash == DescriptionChunk.description_hash,
    )
    statement = (
        select(DescriptionChunk.production_id)
        .where(col(DescriptionChunk.production_id).is_not(None), DescriptionChunk.pipeline == pipeline, ~exists(done))
        .distinct()
        .limit(limit)
    )
    return [owner_id for owner_id in session.exec(statement) if owner_id is not None]


def suggest_tags(
    session: Session,
    *,
    limit: int | None = None,
    margin: float = 0.05,
    per_production: int = 3,
    encoder: Encoder | None = None,
) -> int:
    """Store standout Inhalt tags as reviewable suggestions; score = similarity margin over the mean, not a probability."""
    tags = session.exec(
        select(Tag).where(col(Tag.active), Tag.layer == TagLayer.CONTENT, col(Tag.definition).is_not(None))
    ).all()
    pipeline = pipeline_key()
    production_ids = _unsuggested_productions(session, pipeline, limit)
    if not tags or not production_ids:
        return 0
    tag_vectors = _encode(
        encoder or local_encoder(), [f"{tag.label}: {tag.definition}" for tag in tags], TAG_INSTRUCTION
    )
    chunks = session.exec(
        select(DescriptionChunk)
        .where(col(DescriptionChunk.production_id).in_(production_ids), DescriptionChunk.pipeline == pipeline)
        .order_by(col(DescriptionChunk.production_id), col(DescriptionChunk.chunk_no))
    ).all()
    created = 0
    for _, members in groupby(chunks, key=attrgetter("production_id")):
        group = list(members)
        best = [
            (*max(((_dot(tag_vector, chunk.embedding), chunk) for chunk in group), key=itemgetter(0)), tag)
            for tag, tag_vector in zip(tags, tag_vectors, strict=True)
        ]
        # Raw similarities sit in a narrow band; the margin over this production's mean tag score separates better.
        mean = sum(score for score, _, _ in best) / len(best)
        scored = [(score - mean, tag, chunk) for score, chunk, tag in best if score - mean >= margin]
        for score, tag, chunk in sorted(scored, key=itemgetter(0), reverse=True)[:per_production]:
            created += _suggest(session, chunk, tag, score)
        session.commit()
    return created


# shortcut: short list of 4+ letter German function words; swap for a full list if cluster terms stay noisy.
GERMAN_STOP_WORDS = """
    aber alle allem allen aller alles also andere anderen auch auf beim bereits bevor dabei damit dann darf
    darum dass davon dazu deine dem den denen denn der deren des dessen dich dies diese diesem diesen dieser
    dieses doch dort durch eine einem einen einer eines einige etwas euch gegen geht hast hatte hatten heute
    hier ihnen ihre ihrem ihren ihrer ihres immer jede jedem jeden jeder jedes jetzt kann kein keine keinem
    keinen können könnte mehr mein meine mich mit muss nach nicht nichts noch nun nur oder ohne schon sehr seine
    seinem seinen seiner seit sich sie sind soll sondern sowie unter viel viele vom von vor wann warum was weil
    welche wenn werden wieder will wird wurde während würde zum zur zwei zwischen über
""".split()  # ruff: ignore[split-static-string]


def _class_terms(documents: list[str], labels: Sequence[int], top: int) -> dict[int, list[str]]:
    """Class-based TF-IDF (BERTopic's c-TF-IDF): terms frequent in one cluster but rare across clusters."""
    import numpy as np  # ruff: ignore[import-outside-top-level]
    from sklearn.feature_extraction.text import CountVectorizer  # ruff: ignore[import-outside-top-level]

    clusters = sorted(set(labels) - {-1})
    joined = [" ".join(doc for doc, label in zip(documents, labels, strict=True) if label == c) for c in clusters]
    words = r"(?u)\b[^\W\d_]{4,}\b"
    vectorizer = CountVectorizer(
        token_pattern=words, stop_words=GERMAN_STOP_WORDS, max_df=0.6 if len(clusters) > 2 else 1.0
    )
    counts = cast(Any, vectorizer.fit_transform(joined)).toarray()  # old bundled stubs miss sparse types
    terms = vectorizer.get_feature_names_out()
    frequency = counts / counts.sum(axis=1, keepdims=True).clip(min=1)
    weight = frequency * np.log(1 + counts.sum() / len(clusters) / counts.sum(axis=0).clip(min=1))
    return {c: [str(terms[i]) for i in row.argsort()[::-1][:top]] for c, row in zip(clusters, weight, strict=True)}


def _production_vectors(session: Session, sample: int | None) -> list[tuple[uuid.UUID, str, str, list[float]]]:
    """Mean chunk vector per production (re-normalized), with title and description for labelling."""
    pipeline = pipeline_key()
    ids = select(DescriptionChunk.production_id).where(
        col(DescriptionChunk.production_id).is_not(None), DescriptionChunk.pipeline == pipeline
    )
    rows = session.exec(
        select(Production.id, Production.name, col(Production.description), DescriptionChunk.embedding)
        .join(DescriptionChunk, col(DescriptionChunk.production_id) == Production.id)
        .where(col(Production.id).in_(ids.distinct().limit(sample)), DescriptionChunk.pipeline == pipeline)
        .order_by(col(Production.id))
    ).all()
    result = []
    for owner_id, members in groupby(rows, key=itemgetter(0)):
        group = list(members)
        mean = [sum(values) / len(group) for values in zip(*(row[3] for row in group), strict=True)]
        norm = sum(value * value for value in mean) ** 0.5 or 1.0
        result.append((owner_id, group[0][1], group[0][2] or "", [value / norm for value in mean]))
    return result


def cluster_productions(
    session: Session, *, min_cluster_size: int = 5, sample: int | None = None, top_terms: int = 8
) -> list[dict[str, Any]]:
    """Discover candidate Inhalt tags offline: PCA + HDBSCAN on description vectors, labelled by c-TF-IDF.

    Output is for editors to name and define new vocabulary entries; nothing is written to the database.
    """
    # pyrefly: ignore[missing-module-attribute]  # bundled sklearn stubs predate HDBSCAN (sklearn>=1.3)
    from sklearn.cluster import HDBSCAN  # ruff: ignore[import-outside-top-level]
    from sklearn.decomposition import PCA  # ruff: ignore[import-outside-top-level]

    items = _production_vectors(session, sample)
    if len(items) < 2 * min_cluster_size:
        return []
    # shortcut: PCA stands in for BERTopic's UMAP; switch to umap-learn if clusters stay mostly noise.
    reduced = PCA(n_components=min(5, len(items) - 1), random_state=0).fit_transform([item[3] for item in items])
    labels = [int(label) for label in HDBSCAN(min_cluster_size=min_cluster_size, copy=True).fit_predict(reduced)]
    if not set(labels) - {-1}:
        return []
    terms = _class_terms([f"{item[1]} {item[2]}" for item in items], labels, top_terms)
    published = defaultdict(list)
    for production_id, label in session.exec(
        select(ProductionTag.production_id, Tag.label)
        .join(Tag, col(Tag.code) == ProductionTag.tag_code)
        .where(
            col(ProductionTag.production_id).in_([item[0] for item in items]),
            ProductionTag.status == TagStatus.PUBLISHED,
        )
    ):
        published[production_id].append(label)
    report = []
    for cluster, words in terms.items():
        members = [item for item, label in zip(items, labels, strict=True) if label == cluster]
        tags = Counter(tag for item in members for tag in published[item[0]])
        report.append(
            {
                "cluster": cluster,
                "size": len(members),
                "terms": words,
                "examples": [item[1] for item in members[:5]],
                "tags": [f"{tag} ({count})" for tag, count in tags.most_common(3)],
            }
        )
    return sorted(report, key=itemgetter("size"), reverse=True)


def main(argv: list[str] | None = None) -> int:
    """Index descriptions, search them, suggest Inhalt tags, or report clusters."""
    from backend.user_management import get_session, init_db  # ruff: ignore[import-outside-top-level]

    parser = argparse.ArgumentParser(description=main.__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    index = commands.add_parser("index", help="Embed new or changed descriptions")
    index.add_argument("--kind", choices=["production", "text"], action="append")
    index.add_argument("--limit", type=int)
    search = commands.add_parser("search", help="Hybrid description search")
    search.add_argument("query")
    search.add_argument("--kind", choices=["production", "text"], default="production")
    suggest = commands.add_parser("suggest", help="Suggest Inhalt tags from indexed descriptions")
    suggest.add_argument("--limit", type=int)
    suggest.add_argument("--margin", type=float, default=0.05)
    cluster = commands.add_parser("cluster", help="Print description clusters as tag candidates")
    cluster.add_argument("--min-size", type=int, default=5)
    cluster.add_argument("--sample", type=int)
    args = parser.parse_args(argv)
    init_db()
    with get_session() as session:
        if args.command == "index":
            print(
                f"Indexed {index_descriptions(session, kinds=args.kind or ('production', 'text'), limit=args.limit)}."
            )
        elif args.command == "search":
            for hit in search_descriptions(session, args.query, kind=args.kind):
                print(f"{hit['title']}\n    {hit['snippet'][:160]!r}")
        elif args.command == "suggest":
            print(f"Stored {suggest_tags(session, limit=args.limit, margin=args.margin)} suggestions.")
        else:
            for entry in cluster_productions(session, min_cluster_size=args.min_size, sample=args.sample):
                print(f"#{entry['cluster']} ({entry['size']}): {', '.join(entry['terms'])}")
                print(f"    e.g. {' | '.join(entry['examples'])}\n    tags: {', '.join(entry['tags']) or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
