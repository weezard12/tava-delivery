"""Product aliases: the names employees actually say ("הסרום הכתום" for a long catalogue title).

An alias belongs to the product (its Konimbo item, or its SKU), so it works in every order.
"""

from sqlalchemy import select

from ..domain.errors import ServiceError
from ..domain.hebrew.text import normalize, significant, tokens
from ..domain.identity import Principal
from ..persistence.models import Line, ProductAlias
from .context import Context
from .presenter import item_key
from .records import audit
from .views import AliasView

MAX_ALIASES_PER_PRODUCT = 20


def view(row: ProductAlias) -> AliasView:
    return AliasView(id=row.id, item_key=row.item_key, alias=row.alias, created_at=row.created_at)


class Aliases:
    def __init__(self, context: Context) -> None:
        self.context = context

    def all(self, key: str | None = None) -> list[AliasView]:
        with self.context.db.session() as session:
            query = select(ProductAlias).order_by(ProductAlias.item_key, ProductAlias.id)
            if key:
                query = query.where(ProductAlias.item_key == key)
            return [view(row) for row in session.scalars(query.limit(500))]

    def add(self, actor: Principal, *, line_id: int, alias: str) -> AliasView:
        text = " ".join(alias.split())
        if not 2 <= len(text) <= 100 or not significant(tokens(text)):
            raise ServiceError("alias_invalid")
        with self.context.db.session(write=True) as session:
            line = session.get(Line, line_id)
            if line is None:
                raise ServiceError("line_not_found")
            key = item_key(line)
            if key is None:
                raise ServiceError("alias_target_required")
            existing = session.scalars(
                select(ProductAlias).where(ProductAlias.item_key == key)
            ).all()
            for row in existing:
                if normalize(row.alias) == normalize(text):
                    return view(row)
            if len(existing) >= MAX_ALIASES_PER_PRODUCT:
                raise ServiceError("alias_invalid")
            now = self.context.now()
            row = ProductAlias(item_key=key, alias=text, created_by=actor.id, created_at=now)
            session.add(row)
            session.flush()
            audit(session, "alias.added", actor=actor.id, now=now, item=key)
            return view(row)

    def remove(self, actor: Principal, alias_id: int) -> None:
        with self.context.db.session(write=True) as session:
            row = session.get(ProductAlias, alias_id)
            if row is None:
                raise ServiceError("alias_not_found")
            audit(
                session, "alias.removed", actor=actor.id, now=self.context.now(), item=row.item_key
            )
            session.delete(row)
