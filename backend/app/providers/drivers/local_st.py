"""本地离线向量模型驱动（sentence-transformers）。

与 `local_bge`（FlagEmbedding 占位，已弃用）的区别：
- 依赖更轻：sentence-transformers + torch，桌面版可按需安装，不必打进安装包；
- 模型有下载管理：`app.providers.local_embed` 负责下载/导入/缓存判定，本驱动只做推理；
- 模型标识约定：Provider 的 `base_url` 存模型标识（如 `BAAI/bge-small-zh-v1.5`），
  与 `local_bge` 用 base_url 存本地路径的习惯一致，不要当成 URL 去请求。

不实现 `chat` / `rerank`：
- `rerank` 若被误配成这个 Provider，`retrieval_service._rerank` 会因驱动没有该方法而跳过，
  不会污染排序结果。要做本地重排请另配 `BAAI/bge-reranker-*`（后续可用同套加载逻辑扩展）。
"""
from __future__ import annotations

import asyncio

from app.core.logging import get_logger
from app.providers import local_embed
from app.providers.base import ProviderHealth

logger = get_logger("provider.local_st")


class LocalSTDriver:
    def __init__(self, *, model_id: str, device: str = "cpu") -> None:
        self.model_id = model_id
        self.device = device

    def _embedder(self):
        return local_embed.get_embedder(self.model_id, device=self.device)

    async def embed(
        self, texts: list[str], *, model: str | None = None, batch_size: int = 32
    ) -> list[list[float]]:
        """批量向量化。模型标识以 Provider 的 base_url 为准；`model` 参数仅为接口兼容。

        首次调用会加载模型（10~30 秒、300~600MB 内存），后续走进程内单例。
        加载与推理都在线程里跑（`asyncio.to_thread`），不阻塞事件循环。
        """
        model_id = model or self.model_id
        emb = (
            self._embedder()
            if model_id == self.model_id
            else local_embed.get_embedder(model_id, device=self.device)
        )
        return await emb.embed(texts, batch_size=batch_size)

    async def health(self) -> ProviderHealth:
        """本地健康检查：只看依赖与模型文件，**不发网络请求**。

        三种失败分别给不同提示，避免用户拿到一个笼统的「失败」：
        - 依赖未装（sentence-transformers / torch 缺失）
        - 模型未下载
        - 模型加载异常（文件损坏、维度不符等）

        **不在健康检查里主动加载模型**（除非它已经在内存里）：
        本方法也被 `health_tasks.periodic_health_check` 每 30 分钟批量调用一次，
        在那里触发加载等于「用户从没用过本地模型，后台却常驻 300~600MB」，
        而且加载本身要 10~30 秒。文件层面能查的（config.json + 权重是否齐全）
        就在这里查完；真正加载到显存/DLL 层的问题，由 `test_provider` 或
        第一次检索暴露——那条路径用户是主动触发的，等得起。
        """
        dep = local_embed.dependency_status()
        if not dep["installed"]:
            return ProviderHealth(
                ok=False,
                message=("本地向量模型依赖未安装：" + (dep.get("error") or "")[:120]) + "\n"
                + local_embed.install_hint(),
            )
        if not local_embed.is_model_cached(self.model_id):
            return ProviderHealth(
                ok=False,
                message=(
                    f"模型「{self.model_id}」尚未下载。"
                    "请到「模型管理 → 本地离线向量模型」点下载，或上传模型 zip 导入。"
                ),
            )
        emb = self._embedder()
        if not emb.loaded:
            dim = local_embed.dimension_of(self.model_id)
            return ProviderHealth(
                ok=True,
                message=f"本地模型文件就绪（{dim} 维，首次调用时加载）" if dim
                else "本地模型文件就绪（首次调用时加载）",
            )
        try:
            # 已经加载过：顺手跑一次前向，把维度不符这类问题暴露在健康状态里
            vecs = await asyncio.to_thread(emb.embed_sync, ["健康检查"])
            dim = len(vecs[0]) if vecs else 0
            return ProviderHealth(ok=True, message=f"本地模型就绪（{dim} 维）")
        except Exception as exc:  # noqa: BLE001
            logger.warning("local_st_health_failed", model=self.model_id, err=str(exc)[:200])
            return ProviderHealth(ok=False, message=f"模型加载失败：{str(exc)[:200]}")

    async def list_models(self) -> list[dict]:
        """返回本地已缓存的模型清单（不联网）。"""
        out = []
        for m in local_embed.scan_local_models():
            out.append(
                {
                    "id": m["id"],
                    "owned_by": "local_st",
                    "dim": m.get("dim") or 0,
                    "source": m.get("source"),
                }
            )
        return out
