"""Qdrant 与 OpenAI 兼容 Embedding API 的轻量异步客户端。"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote

import httpx

from config.settings import Settings, get_settings


class VectorStoreError(RuntimeError):
    """向量服务调用失败；异常文本不包含 URL、密钥或远端响应正文。"""


class _HTMLTextParser(HTMLParser):
    """提取新闻正文中的文本，避免把 HTML 标签送入 Embedding 模型。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def clean_news_text(value: str | None) -> str:
    """清理正文标签和多余空白，保留可供语义检索的自然语言内容。"""
    parser = _HTMLTextParser()
    try:
        parser.feed(value or "")
        parser.close()
    except Exception:
        return re.sub(r"\s+", " ", value or "").strip()
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def build_embedding_text(news: Any) -> str:
    """使用标题、摘要和正文开头生成向量，限制单篇文稿的调用体积。"""
    parts = [
        f"标题：{news.title or ''}",
        f"摘要：{news.description or ''}",
        f"正文：{clean_news_text(news.content)[:12000]}",
    ]
    return "\n".join(part for part in parts if part.strip())[:16000]


def _headers(api_key: str, *, qdrant: bool = False) -> dict[str, str]:
    if not api_key:
        return {}
    return {"api-key" if qdrant else "Authorization": api_key if qdrant else f"Bearer {api_key}"}


class QdrantVectorStore:
    """通过 Qdrant REST API 管理单一版本的新闻向量集合。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._collection_ready = False

    @property
    def collection_url(self) -> str:
        return f"{self.settings.qdrant_url}/collections/{quote(self.settings.qdrant_collection, safe='')}"

    async def _request(
        self,
        method: str,
        url: str,
        *,
        qdrant: bool,
        json_body: dict[str, Any] | None = None,
        allow_not_found: bool = False,
        allow_conflict: bool = False,
    ) -> dict[str, Any] | None:
        """发送限时 HTTP 请求，并把远端错误转成不含密钥的内部错误码。"""
        api_key = self.settings.qdrant_api_key if qdrant else self.settings.embedding_api_key
        headers = _headers(api_key, qdrant=qdrant)
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.vector_api_timeout_seconds,
                headers=headers,
            ) as client:
                response = await client.request(method, url, json=json_body)
        except httpx.HTTPError as exc:
            service = "qdrant" if qdrant else "embedding"
            raise VectorStoreError(f"{service}_request_failed") from exc
        if allow_not_found and response.status_code == 404:
            return None
        if allow_conflict and response.status_code == 409:
            return {}
        if response.is_error:
            service = "qdrant" if qdrant else "embedding"
            raise VectorStoreError(f"{service}_http_{response.status_code}")
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            service = "qdrant" if qdrant else "embedding"
            raise VectorStoreError(f"{service}_invalid_json") from exc
        if not isinstance(payload, dict):
            service = "qdrant" if qdrant else "embedding"
            raise VectorStoreError(f"{service}_invalid_response")
        return payload

    async def ensure_collection(self) -> None:
        """按配置维度创建集合，并为常用过滤字段建立索引。"""
        if self._collection_ready:
            return
        collection = await self._request("GET", self.collection_url, qdrant=True, allow_not_found=True)
        if collection is None:
            await self._request(
                "PUT",
                self.collection_url,
                qdrant=True,
                json_body={
                    "vectors": {
                        "size": self.settings.embedding_dimension,
                        "distance": "Cosine",
                    }
                },
                allow_conflict=True,
            )
        else:
            vector_config = (
                collection.get("result", {}).get("config", {}).get("params", {}).get("vectors", {})
            )
            if not isinstance(vector_config, dict):
                raise VectorStoreError("qdrant_vector_config_invalid")
            # 当前客户端写入单一向量；命名向量集合需单独指定向量名称，不能静默沿用。
            if "size" not in vector_config:
                raise VectorStoreError("qdrant_named_vectors_require_new_collection")
            actual_dimension = vector_config.get("size")
            if actual_dimension != self.settings.embedding_dimension:
                raise VectorStoreError("qdrant_dimension_mismatch_use_new_collection")

        # 重复创建索引可能返回冲突；这不影响检索，索引创建失败仍视为集合未初始化成功。
        for field_name, schema in (
            ("status", "keyword"),
            ("embedding_version", "keyword"),
            ("category_id", "integer"),
            ("news_id", "integer"),
        ):
            await self._request(
                "PUT",
                f"{self.collection_url}/index",
                qdrant=True,
                json_body={"field_name": field_name, "field_schema": schema},
                allow_conflict=True,
            )
        self._collection_ready = True

    async def embed(self, text: str) -> list[float]:
        """调用独立配置的 Embedding API，并校验返回向量维度。"""
        base_url = self.settings.embedding_api_base_url.rstrip("/")
        payload = await self._request(
            "POST",
            f"{base_url}/embeddings",
            qdrant=False,
            json_body={"model": self.settings.embedding_model, "input": text},
        )
        try:
            vector = payload["data"][0]["embedding"] if payload else None
        except (KeyError, IndexError, TypeError) as exc:
            raise VectorStoreError("embedding_invalid_response") from exc
        if (
            not isinstance(vector, list)
            or len(vector) != self.settings.embedding_dimension
            or any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in vector)
        ):
            raise VectorStoreError("embedding_dimension_mismatch")
        return [float(value) for value in vector]

    async def upsert_news(self, news: Any, fingerprint: str) -> None:
        """写入单条新闻向量和可过滤的来源元数据。"""
        await self.ensure_collection()
        vector = await self.embed(build_embedding_text(news))
        payload = {
            "news_id": int(news.id),
            "title": news.title,
            "description": news.description,
            # Qdrant 保存清洗后的正文；MySQL 仍是原始内容的唯一事实来源。
            "content": clean_news_text(news.content)[:12000],
            "source_name": news.author,
            "source_url": news.source_url,
            "publish_time": news.publish_time.isoformat() if news.publish_time else None,
            "category_id": int(news.category_id),
            "status": news.status,
            "embedding_version": self.settings.embedding_version,
            "content_fingerprint": fingerprint,
        }
        await self._request(
            "PUT",
            f"{self.collection_url}/points?wait=true",
            qdrant=True,
            json_body={"points": [{"id": int(news.id), "vector": vector, "payload": payload}]},
        )

    async def delete_news(self, news_id: int) -> None:
        """按 MySQL 新闻主键幂等删除 Qdrant 中的对应向量。"""
        await self.ensure_collection()
        await self._request(
            "POST",
            f"{self.collection_url}/points/delete?wait=true",
            qdrant=True,
            json_body={"points": [int(news_id)]},
        )

    async def search(
        self,
        query: str,
        *,
        category_id: int | None = None,
        exclude_news_id: int | None = None,
        limit: int | None = None,
    ) -> list[tuple[int, float]]:
        """以语义向量召回新闻 ID 和相似度，不把 Qdrant payload 当作新闻事实来源。"""
        await self.ensure_collection()
        vector = await self.embed(query[:16000])
        must = [
            {"key": "status", "match": {"value": "published"}},
            {"key": "embedding_version", "match": {"value": self.settings.embedding_version}},
        ]
        if category_id is not None:
            must.append({"key": "category_id", "match": {"value": int(category_id)}})
        must_not = []
        if exclude_news_id is not None:
            must_not.append({"key": "news_id", "match": {"value": int(exclude_news_id)}})
        response = await self._request(
            "POST",
            f"{self.collection_url}/points/query",
            qdrant=True,
            json_body={
                "query": vector,
                "limit": limit or self.settings.vector_search_limit,
                "with_payload": ["news_id", "embedding_version"],
                "filter": {"must": must, "must_not": must_not},
            },
        )
        try:
            points = response["result"]["points"] if response else []
        except (KeyError, TypeError) as exc:
            raise VectorStoreError("qdrant_invalid_search_response") from exc
        results: list[tuple[int, float]] = []
        for point in points:
            try:
                payload = point.get("payload") or {}
                news_id = int(payload.get("news_id", point["id"]))
                score = float(point["score"])
            except (AttributeError, KeyError, TypeError, ValueError):
                continue
            results.append((news_id, score))
        return results

    async def ping(self) -> bool:
        """检查 Qdrant REST 服务是否可访问。"""
        payload = await self._request("GET", f"{self.settings.qdrant_url}/collections", qdrant=True)
        return bool(payload is not None)


async def search_news_vectors(
    query: str,
    *,
    category_id: int | None = None,
    exclude_news_id: int | None = None,
    limit: int | None = None,
) -> list[tuple[int, float]]:
    """为证据检索提供一次向量召回。"""
    return await QdrantVectorStore().search(
        query,
        category_id=category_id,
        exclude_news_id=exclude_news_id,
        limit=limit,
    )
