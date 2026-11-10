"""本地 bge 系列驱动（FlagEmbedding）——**已弃用，新部署请用 local_st**。

保留原因：兼容既有 kind="local_bge" 配置。FlagEmbedding 依赖重、无法按需安装，
新部署请选 `local_st`（sentence-transformers，模型可在「模型管理 → 本地离线向量模型」
里下载、导入、删除）。

原说明：启用方式：pip install FlagEmbedding，然后在此实现 embed/rerank 调用本地模型
（bge-m3 做 embedding，bge-reranker-v2-m3 做重排）。
"""
from __future__ import annotations

from app.providers.base import ProviderHealth


class LocalBGEDriver:
    def __init__(self, *, model_path: str, device: str = "cpu") -> None:
        self.model_path = model_path
        self.device = device
        self._model = None

    def _lazy_load(self):
        try:
            from FlagEmbedding import FlagModel  # type: ignore
        except ImportError as e:  # noqa: BLE001
            raise RuntimeError("未安装 FlagEmbedding，请 pip install FlagEmbedding") from e
        if self._model is None:
            self._model = FlagModel(self.model_path, device=self.device)
        return self._model

    async def embed(self, texts: list[str], *, batch_size: int = 32) -> list[list[float]]:
        import asyncio

        def _run():
            model = self._lazy_load()
            vecs = model.encode(texts, batch_size=batch_size, normalize_embeddings=True)
            return [list(map(float, v)) for v in vecs]

        return await asyncio.to_thread(_run)

    async def health(self) -> ProviderHealth:
        try:
            self._lazy_load()
            return ProviderHealth(ok=True, message="本地模型就绪")
        except Exception as e:  # noqa: BLE001
            return ProviderHealth(ok=False, message=str(e)[:200])

    async def list_models(self) -> list[dict]:
        """本地模型无远端列表，用配置的模型路径作为标识。"""
        return [{"id": self.model_path, "owned_by": "local_bge"}]
