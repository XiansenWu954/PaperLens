"""数据源缓存表。

命中即返回，不重复请求。既是防限流（OpenAlex/ArXiv/S2 都限流），
也满足"本地库存"约束——抓过的查询永不重抓。TTL 默认 7 天（论文元数据稳定）。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json

from django.db import models
from django.utils import timezone


def query_hash(source: str, query: str, **params) -> str:
    """对 (source, query, params) 生成稳定哈希作为缓存键。"""
    payload = json.dumps({"s": source, "q": query, "p": params}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


class DatasourceCache(models.Model):
    source = models.CharField(max_length=32, db_index=True)
    qhash = models.CharField(max_length=64, db_index=True)
    payload = models.JSONField()
    fetched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source", "qhash"], name="uniq_ds_cache")
        ]

    @classmethod
    def get(cls, source: str, qhash: str, ttl_days: int = 7):
        """命中且未过期则返回 payload，否则 None。"""
        cutoff = timezone.now() - dt.timedelta(days=ttl_days)
        try:
            row = cls.objects.get(source=source, qhash=qhash)
        except cls.DoesNotExist:
            return None
        return row.payload if row.fetched_at >= cutoff else None

    @classmethod
    def set(cls, source: str, qhash: str, payload) -> None:
        cls.objects.update_or_create(
            source=source, qhash=qhash, defaults={"payload": payload}
        )
