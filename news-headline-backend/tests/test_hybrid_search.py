"""验证全文检索可用时的混合查询和索引缺失时的安全回退。"""

from sqlalchemy.dialects.mysql import dialect as mysql_dialect
from sqlalchemy.exc import SQLAlchemyError

from services.evidence import retrieve_evidence


def test_hybrid_statement_contains_mysql_fulltext_expression():
    class Result:
        def all(self):
            return []

    class Db:
        def __init__(self):
            self.statement = None

        async def execute(self, statement):
            self.statement = statement
            return Result()

    import asyncio

    db = Db()
    asyncio.run(retrieve_evidence(db, "public technology event"))
    compiled = str(db.statement.compile(dialect=mysql_dialect()))
    assert "MATCH (news.title, news.description, news.content) AGAINST" in compiled


def test_hybrid_search_falls_back_when_fulltext_is_unavailable():
    class Result:
        def all(self):
            return []

    class LegacyDb:
        def __init__(self):
            self.calls = 0
            self.rolled_back = False

        async def execute(self, statement):
            self.calls += 1
            if self.calls == 1:
                raise SQLAlchemyError("FULLTEXT index unavailable")
            return Result()

        async def rollback(self):
            self.rolled_back = True

    import asyncio

    db = LegacyDb()
    assert asyncio.run(retrieve_evidence(db, "public technology event")) == []
    assert db.calls == 2
    assert db.rolled_back
