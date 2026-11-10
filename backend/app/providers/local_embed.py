"""本地离线向量模型（sentence-transformers）：下载 / 缓存 / 加载 / 导入。

设计要点（勿凭直觉改动）：

1. **推理依赖是可选依赖，绝不能变成硬依赖**。`sentence-transformers` / `torch` 体积
   巨大（torch CPU 版约 520MB），打进桌面安装包会让 390MB 的 win-unpacked 涨到 1.2GB+。
   因此：本模块顶层**不 import** 任何重依赖，只在真正用的时候 `try import`，
   失败时返回可操作的中文提示，而不是让后端启动失败。

2. **冻结态（PyInstaller）不能 import 系统 site-packages**。
   PyInstaller 6.x 的 bootloader 用 `PyConfig_InitIsolatedConfig`，忽略 PYTHONPATH、
   不执行 site 模块，`sys.path` 只有 `_internal` 下的几个条目。所以桌面版用户
   往系统 Python 里 `pip install` 是**无效**的，必须装到 `model_dir/runtime/` 再由
   `_ensure_import_paths()` 显式插进 `sys.path`。见该函数的约束说明。

3. **模型文件放项目内**（`settings.model_dir`），不散落到用户全局 `~/.cache/huggingface`。
   好处：随数据目录一起备份/迁移，离线拷贝直接可用。
   目录结构：
       <model_dir>/hub/models--org--name/…   hf_hub 标准缓存（snapshot + blobs）
       <model_dir>/manual/<name>/…           用户上传 zip 或手拷的模型目录
       <model_dir>/runtime/…                 可选依赖（--target 安装）

4. **镜像站必须走 hf-mirror**，且必须关掉 Xet 传输协议：
   镜像站不代理 xethub 的 CAS 域名，开着会拿到 401。两个环境变量都必须在
   **任何** `huggingface_hub` 导入之前设置，故在模块导入期执行 `_apply_hf_env()`。

5. **进度必须自己算**：`snapshot_download` 没有进度回调。total 从
   `model_info(files_metadata=True)` 预取；增量靠 `tqdm_class` 钩子；再加一层
   1 秒轮询 cache 目录的兜底（跨 hf_hub 版本最稳）。三路取**最大值**，避免进度倒退。

6. **bge 的 query 指令本轮不加**。`bge-*-zh-v1.5` 官方建议查询侧加
   「为这个句子生成表示以用于检索相关文章：」前缀以提升召回，但本仓库的
   `EmbeddingProvider.embed(texts)` 接口不区分 query/doc（见 app/providers/base.py），
   给 query 单独加前缀会破坏 doc/query 向量空间对称，反而降低效果。
   v1.5 版本无指令仍可正常使用。若将来给 Provider 增加 `embed_query` 语义，再补上。
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import importlib.util
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger

logger = get_logger("local_embed")

# 推荐模型：中文效果好、体积适中（CPU 可跑）。dim 用于建 Provider 时写 embedding_dim，
# 与模型真实维度不一致会导致检索时维度错配，改这里前请先确认模型输出维度。
RECOMMENDED_MODELS: list[dict[str, Any]] = [
    {
        "id": "BAAI/bge-small-zh-v1.5",
        "label": "BGE small 中文（推荐）",
        "dim": 512,
        "size": "约 95 MB",
        "note": "中文语义检索效果好，CPU 推理快",
    },
    {
        "id": "BAAI/bge-base-zh-v1.5",
        "label": "BGE base 中文",
        "dim": 768,
        "size": "约 400 MB",
        "note": "精度更高，速度稍慢",
    },
    {
        "id": "shibing624/text2vec-base-chinese",
        "label": "text2vec 中文",
        "dim": 768,
        "size": "约 400 MB",
        "note": "通用中文向量，兼容性好",
    },
    {
        "id": "sentence-transformers/all-MiniLM-L6-v2",
        "label": "MiniLM 多语言轻量",
        "dim": 384,
        "size": "约 90 MB",
        "note": "体积小、速度快，中文一般",
    },
]

_MODEL_DIMS: dict[str, int] = {m["id"]: m["dim"] for m in RECOMMENDED_MODELS}

# 只取推理需要的权重，跳过 TF/Flax/ONNX/GGUF，能省掉一大半流量
_IGNORE_PATTERNS = [
    "*.h5", "*.ot", "*.msgpack", "tf_model.h5", "flax_model*", "*.onnx", "*.tf_*", "*.gguf",
]

# 导入 zip 的上限：模型目录可能含 400MB+ 的 safetensors，沿用组件包的 800MB 会装不下
_MAX_FILES = 2000
_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
_CHUNK = 256 * 1024

# 可选依赖（runtime/）安装参数。
# 装机包清单必须固定：只装推理真正需要的四个包，torch 由官方 CPU 索引提供，
# numpy 明确排除（`_internal` 已带 numpy 2.x，外置一份会撞 ABI，见 _ensure_import_paths）。
_RUNTIME_PACKAGES = (
    "sentence-transformers",
    "transformers",
    "tokenizers",
    "safetensors",
)
_TORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"
_DEFAULT_PYPI = "https://pypi.tuna.tsinghua.edu.cn/simple"
# 冻结态没有 pip、也没有可执行的 Python 解释器：只能借助用户机器上的 python 3.10 x64
_RUNTIME_PY = "3.10"

# 下载状态（进程内单例）。后端重启即归零，但已下落的 blobs 会被 hf_hub 续传。
_progress: dict[str, dict] = {}
_BUSY: set[str] = set()
# 已加载的模型实例：model_id@device -> LocalEmbedder
_instances: dict[str, "LocalEmbedder"] = {}

# huggingface_hub 导入一次就够；失败缓存起来，避免每次调用都重试 import
_hf_import_error: str | None = None


# ------------------------------------------------------------------ 环境与路径


def cache_dir() -> Path:
    """本地模型缓存根目录（项目内，便于随数据一起迁移）。"""
    p = settings.model_path
    p.mkdir(parents=True, exist_ok=True)
    return p


def runtime_dir() -> Path:
    """可选依赖（sentence-transformers / torch）的落盘目录。"""
    return cache_dir() / "runtime"


def _apply_hf_env() -> None:
    """配置 HuggingFace 环境。必须在任何 huggingface_hub 导入之前执行。

    - HF_ENDPOINT 默认走国内镜像（官方域在国内常超时），可用 RAG_HF_ENDPOINT 覆盖
    - HF_HUB_DISABLE_XET=1：Xet 需要访问 xethub 的 CAS 服务，镜像站不代理该域名，
      开着会报 401；关掉后回退普通 HTTP 下载
    - HF_HOME 固定到项目内，避免模型散落到用户全局缓存
    """
    os.environ.setdefault("HF_ENDPOINT", os.environ.get("RAG_HF_ENDPOINT") or "https://hf-mirror.com")
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    # HF_HOME 用强制覆盖：用户可能已在系统里设过别的值，本项目要求模型落在数据目录内
    os.environ["HF_HOME"] = str(cache_dir())


def hf_endpoint() -> str:
    return os.environ.get("HF_ENDPOINT") or "https://hf-mirror.com"


def _ensure_import_paths() -> list[str]:
    """把 `model_dir/runtime` 插到 sys.path，供 `pip --target` 装的可选依赖被导入。

    为什么需要：冻结态（PyInstaller onedir）的 bootloader 用隔离配置初始化解释器，
    **不会**读 PYTHONPATH、**不会**执行 site 模块，`sys.path` 里只有 `_internal` 下的
    几个条目。所以桌面版用户往系统 Python 里 pip install 的包对 exe 完全不可见，
    只有走这条显式路径才能加载。

    安装约束（写在这里是因为踩过一次就够）：
    - 必须用与打包一致的 **CPython 3.10 x64** wheel（cp310-win_amd64），
      否则 `.pyd` 魔数不匹配，报 `DLL load failed while importing ...`；
    - 必须 `--no-deps`，且**不要把 numpy 装进 runtime**：
      `_internal` 已带 numpy 2.x 且优先命中，外部 numpy 会撞 ABI；
    - 保留 `*.dist-info`（transformers 运行时要查 importlib.metadata）。
    """
    paths: list[str] = []
    rt = runtime_dir()
    if not rt.is_dir():
        return paths
    s = str(rt)
    if s not in sys.path:
        sys.path.insert(0, s)
        importlib.invalidate_caches()
    paths.append(s)
    # torch 自己会按 torch._C.__file__ 定位 torch/lib 并 add_dll_directory，
    # 但其它带 DLL 的包（如 safetensors、tokenizers）不会，这里统一补一遍
    if hasattr(os, "add_dll_directory"):
        for p in list(rt.glob("*/lib")) + list(rt.glob("*.libs")):
            if p.is_dir():
                try:
                    os.add_dll_directory(str(p))
                    paths.append(str(p))
                except OSError:  # noqa: BLE001
                    pass
    return paths


def _import_hf() -> Any:
    """导入 huggingface_hub；失败时缓存错误并抛可读异常。"""
    global _hf_import_error
    _apply_hf_env()
    _ensure_import_paths()
    try:
        import huggingface_hub  # noqa: F401

        return huggingface_hub
    except Exception as exc:  # noqa: BLE001
        _hf_import_error = str(exc)[:300]
        raise RuntimeError(
            "缺少 huggingface_hub，无法下载模型。\n"
            "请先安装本地向量模型依赖：pip install sentence-transformers torch"
        ) from exc


def dependency_status() -> dict[str, Any]:
    """检测本地推理依赖是否可用（不触发模型下载、不联网）。

    注意：**不能**只看 runtime 目录是否存在。卸载依赖后，已经被 import 过的模块
    仍留在 `sys.modules` 里，于是前端会显示「已安装」但实际文件早被删了（实测踩到）。
    因此判定顺序是：先看 runtime 里的包目录在不在 → 再看这些包能否被解析。

    **这里刻意不 import torch / sentence-transformers**（勿改回去）：
    - 首次 import torch 约 4s、sentence-transformers 约 10s，而本函数被
      「进模型管理页」「每 30 分钟的 Provider 健康检查」反复调用。原先的实现里
      首次调用会把事件循环整段钉住（实测 /local/status 11.8s 期间 /health 有
      一次 11.7s 无响应），整站看起来就是「登录后卡死」。
    - 只为探测版本号就付这个代价不值得：`importlib.metadata` 读 dist-info，
      `importlib.util.find_spec` 找包位置，都是毫秒级且无副作用。
    - 真正的加载错误（DLL 缺依赖、ABI 不符）不在这里暴露，而是由首次推理时
      `LocalEmbedder._load()` 抛出来；探测层只回答「装了没、装在哪、什么版本」。
    """
    info: dict[str, Any] = {
        "installed": False,
        "torch": False,
        "version": "",
        "error": "",
        "search_paths": [],
        "source": "",   # runtime | site-packages
    }
    _apply_hf_env()
    info["search_paths"] = _ensure_import_paths()

    rt = runtime_dir()
    frozen = bool(getattr(sys, "frozen", False))
    # 冻结态只认 runtime；源码态 runtime 和 venv 都算，但 runtime 存在时必须真的在里面
    if rt.exists():
        if not _runtime_installed():
            info["error"] = f"{rt} 里缺少 sentence-transformers/torch（可能未装完整）"
            return info
        info["source"] = "runtime"
    elif frozen:
        info["error"] = "未安装本地推理依赖"
        return info

    # 已加载的旧模块指向被换掉的路径：清掉，否则下一次真正的 import 会拿到旧对象，
    # 推理时会报 DLL load failed 这类难查的错
    if info["source"] == "runtime" and _modules_stale(rt):
        _drop_loaded_modules()
        importlib.invalidate_caches()

    st_file = _module_file("sentence_transformers")
    info["torch"] = _module_file("torch") is not None
    if st_file is None:
        info["error"] = "未找到 sentence-transformers（依赖未安装或已损坏）"
        return info

    info["version"] = _package_version("sentence-transformers")
    if info["source"] == "runtime" and not st_file.startswith(str(rt)):
        # 命中了 runtime 以外的副本（venv/site-packages），对冻结态等于没装
        info["installed"] = frozen is False
        info["source"] = "site-packages" if info["installed"] else ""
        if not info["installed"]:
            info["error"] = "当前进程加载到的是 runtime 以外的 sentence-transformers"
        return info
    info["installed"] = True
    return info


def _module_file(name: str) -> str | None:
    """包入口文件的绝对路径；找不到（未安装 / 已损坏）返回 None。

    先看 `sys.modules`：已加载过的模块用 `__file__` 最准（`find_spec` 对命名空间
    包会返回 None）。再看 `find_spec` 走 `sys.path` 解析，不执行模块代码。
    """
    mod = sys.modules.get(name)
    if mod is not None:
        f = getattr(mod, "__file__", None)
        if f:
            return str(f)
        # 命名空间包没有 __file__：用它的 __path__ 反推一个位置出来
        paths = list(getattr(mod, "__path__", None) or [])
        return str(Path(paths[0]) / "__init__.py") if paths else None
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return None
    if spec is None:
        return None
    if spec.origin and spec.origin not in ("namespace", "built-in", "frozen"):
        return spec.origin
    paths = list(spec.submodule_search_locations or [])
    return str(Path(paths[0]) / "__init__.py") if paths else None


def _package_version(dist_name: str) -> str:
    """读 dist-info 里的版本号（毫秒级）；读不到返回空串，绝不因此报错。"""
    try:
        return importlib.metadata.version(dist_name)
    except Exception:  # noqa: BLE001
        return ""


def _modules_stale(rt: Path) -> bool:
    """已加载的 sentence_transformers/torch 是否指向 rt 以外的路径（说明文件已被换掉）。"""
    for name in ("sentence_transformers", "torch"):
        mod = sys.modules.get(name)
        f = getattr(mod, "__file__", None)
        if mod is not None and (not f or not str(f).startswith(str(rt))):
            return True
    return False


def install_hint() -> str:
    """给前端的兜底提示：正常情况下点「一键安装依赖」即可，这里只在自动安装不可用时展示。"""
    if getattr(sys, "frozen", False):
        return (
            "自动安装需要本机有 Python 3.10 x64（用于下载与本程序同版本的 wheel）。\n"
            "也可手动执行：\n"
            f'python3.10 -m pip install --target "{runtime_dir()}" '
            "torch --index-url https://download.pytorch.org/whl/cpu\n"
            f'python3.10 -m pip install --target "{runtime_dir()}" --no-deps '
            "sentence-transformers transformers tokenizers safetensors"
        )
    return "点上方「一键安装依赖」即可（约 1GB，装到数据目录，不影响系统 Python）。"


# ------------------------------------------------------------------ 依赖安装 / 卸载

# 冻结态可用的解释器探测缓存：{"exe": "路径"}，空串表示「找过但没有」
_runtime_py_cache: dict[str, str] = {}

# 安装进度（与模型下载分开放，避免共用 progress 时互相覆盖）
_dep_state: dict[str, Any] = {
    "phase": "idle",   # idle | running | done | failed
    "step": "",        # 当前步骤的中文说明
    "log": "",         # 已脱敏的 pip 输出（仅尾部）
    "error": None,
    "updated_at": None,
    "target": None,    # 安装目录
    "python": None,    # 实际使用的解释器
}
_DEP_LOCK = threading.Lock()
# runtime 目录里的“待删除”标记文件：把当前进程加载的 .pyd/.dll 就地删掉在 Windows 上会失败，
# 因此卸载时把目录改名，重启后由 reap_runtime_if_pending() 清理
_DEP_TRASH_SUFFIX = ".trash"


def _dep_set(**kw: Any) -> None:
    with _DEP_LOCK:
        _dep_state.update(kw)
        _dep_state["updated_at"] = int(time.time() * 1000)


def dependency_progress() -> dict[str, Any]:
    with _DEP_LOCK:
        st = dict(_dep_state)
    st["log"] = st.get("log") or ""
    return st


def _dep_busy() -> bool:
    with _DEP_LOCK:
        return _dep_state.get("phase") == "running"


def _pip_env() -> dict[str, str]:
    env = dict(os.environ)
    # 别让用户的全局 pip 配置把包装到别处
    for k in ("PYTHONPATH", "PYTHONHOME", "PIP_REQUIRE_VIRTUALENV"):
        env.pop(k, None)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return env


def _pypi_index() -> str:
    """pip 源：优先沿用用户环境里显式配置的，否则用国内镜像（官方源在国内常超时）。"""
    for key in ("RAG_PYPI_INDEX", "PIP_INDEX_URL"):
        v = os.environ.get(key)
        if v:
            return v
    return _DEFAULT_PYPI


def _python_major_minor(exe: str) -> tuple[int, int] | None:
    try:
        out = subprocess.run(
            [exe, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
            capture_output=True, text=True, timeout=20, env=_pip_env(),
        )
    except Exception:  # noqa: BLE001
        return None
    if out.returncode != 0:
        return None
    try:
        major, minor = (out.stdout or "").strip().split(".", 1)
        return int(major), int(minor)
    except Exception:  # noqa: BLE001
        return None


def _find_runtime_python() -> str | None:
    """找到能装 wheel 的解释器。

    - 源码态：当前解释器就是对的（打包同版本），直接用它；
    - 冻结态：`sys.executable` 是 exe 本身，不能用。依次找 `py -3.10` / `python3.10` / `python`，
      并要求 major.minor 与打包时一致，否则 .pyd 魔数不匹配会报 DLL load failed。

    结果缓存：探测要起子进程跑解释器（每次几百毫秒），而 `/local/status`、
    `/local/dependency` 每次都会问一遍。用户装完 Python 后点「刷新」用
    `refresh_runtime_python()` 让它重新探测。
    """
    if not getattr(sys, "frozen", False):
        return sys.executable
    cached = _runtime_py_cache.get("exe")
    if cached is not None:
        return cached or None
    return _probe_runtime_python()


def refresh_runtime_python() -> str | None:
    """清掉解释器探测缓存并重新找（用户在安装中途装好了 Python 时用）。"""
    _runtime_py_cache.pop("exe", None)
    return _find_runtime_python()


def _probe_runtime_python() -> str | None:
    want = tuple(int(x) for x in _RUNTIME_PY.split(".")[:2])
    candidates: list[list[str]] = []
    if os.name == "nt":
        candidates.append(["py", f"-{_RUNTIME_PY}"])
    candidates += [
        [f"python{_RUNTIME_PY}"],
        ["python3"],
        ["python"],
    ]
    for cmd in candidates:
        exe = cmd[0]
        if shutil.which(exe) is None:
            continue
        got = _python_major_minor(shutil.which(exe) or exe)
        if got != want:
            logger.info("runtime_python_version_mismatch", candidate=exe, got=got, want=want)
            continue
        found = shutil.which(exe) or exe
        _runtime_py_cache["exe"] = found
        return found
    # 缓存负结果（空串代表「找过、没有」），否则每次刷新都要再起一串子进程
    _runtime_py_cache["exe"] = ""
    return None


def _redact(text: str) -> str:
    """pip 输出里可能带代理凭据，落日志/回前端前统一遮挡。"""
    import re

    text = re.sub(r"(https?://)([^/\s:@]+):([^/\s@]+)@", r"\1***:***@", text)
    return text[-6000:]


def _run_pip(argv: list[str], *, on_log: Any = None, timeout: float = 3600) -> tuple[bool, str]:
    """跑一次 pip，逐行累积日志（前端轮询时能看到“正在装什么”）。"""
    logger.info("runtime_pip_start", argv=" ".join(argv)[:300])
    lines: list[str] = []
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=_pip_env(), text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except FileNotFoundError as exc:
        return False, f"找不到 pip：{exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"启动 pip 失败：{exc}"

    deadline = time.time() + timeout
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))
            if len(lines) > 400:  # 只留尾部，避免几万行进度条撑爆状态
                del lines[:200]
            if on_log is not None:
                on_log("\n".join(lines)[-4000:])
            if time.time() > deadline:
                proc.kill()
                return False, "安装超时（超过 1 小时），已中止"
        proc.wait(timeout=60)
    except Exception as exc:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return False, f"安装异常：{str(exc)[:200]}"
    log = "\n".join(lines)
    return proc.returncode == 0, log


def _runtime_installed() -> bool:
    """只看 runtime 目录里是否已经落了 sentence-transformers/torch（不看 sys.path）。"""
    rt = runtime_dir()
    return (rt / "sentence_transformers").is_dir() and (rt / "torch").is_dir()


def _dir_size(p: Path) -> int:
    """递归统计目录字节数。

    用 `os.scandir` 而不是 `Path.rglob`：runtime 装完后有 2.5 万+ 个文件，
    rglob 会为每个条目构造 Path 对象并走一遍通配匹配，实测 10.7s；scandir 只要 1s，
    而本函数的结果会被 `/local/status` 和 `/local/dependency` 每次都取。
    `follow_symlinks=False`：不跟进链接，避免统计到无关目录或陷入环。
    """
    total = 0
    stack = [str(p)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


# runtime 体积缓存：(目录, 结果, 计算时间)。安装/卸载后必须显式失效，
# 否则用户装完看到的大小会一直是 0。
# TTL 取 10 分钟：runtime 只在「装 / 卸」时变大变小，两次安装之间的体积不会自己变，
# 没必要为了显示一个数字每 30 秒重扫 2.5 万个文件（Windows 上要 1 秒）。
_size_cache: dict[str, tuple[int, float]] = {}
_SIZE_TTL = 600.0


def _cached_dir_size(p: Path, *, force: bool = False) -> int:
    key = str(p)
    now = time.time()
    hit = _size_cache.get(key)
    if hit is not None and not force and now - hit[1] < _SIZE_TTL:
        return hit[0]
    size = _dir_size(p) if p.exists() else 0
    _size_cache[key] = (size, now)
    return size


def invalidate_size_cache() -> None:
    _size_cache.clear()


def runtime_status() -> dict[str, Any]:
    """runtime 可选依赖的状态（供前端显示「已安装/未安装/正在安装」）。"""
    rt = runtime_dir()
    installed = _runtime_installed()
    size = _cached_dir_size(rt) if installed else 0
    return {
        "dir": str(rt),
        "installed": installed,
        "size": size,
        "busy": _dep_busy(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": _find_runtime_python() if getattr(sys, "frozen", False) else sys.executable,
        "required_python": _RUNTIME_PY,
        "packages": list(_RUNTIME_PACKAGES),
        "index": _pypi_index(),
        "progress": dependency_progress(),
        # 这次探测是否命中了缓存的「没找到解释器」结果——用户刚装完 Python 时
        # 前端据此提示「点刷新重新探测」，而不是让他反复点安装
        "python_probe_cached": "exe" in _runtime_py_cache,
    }


def _park_runtime_dir(rt: Path) -> Path | None:
    """把现有 runtime 挪到一边（返回新路径），失败则返回 None。

    为什么不在原地重装：一旦本进程 import 过 torch，`torch/lib/c10.dll` 就被锁住，
    pip 覆盖同名文件会 `PermissionError: [WinError 5]`（实测踩到）。
    挪走整个目录是 Windows 上唯一稳的做法，旧目录由 reap_runtime_if_pending() 在
    下次启动时删掉。
    """
    if not rt.exists():
        return None
    trash = rt.with_name(f"{rt.name}{_DEP_TRASH_SUFFIX}-{int(time.time())}")
    try:
        os.replace(rt, trash)
        return trash
    except OSError as exc:  # noqa: BLE001
        logger.warning("runtime_park_failed", error=str(exc)[:200])
        shutil.rmtree(rt, ignore_errors=True)
        if rt.exists():
            return None
        return trash


def _install_task(python: str) -> None:
    """后台安装 runtime 依赖。

    踩过的坑（改之前先读）：
    1. **不能 `--no-deps`**：sentence-transformers 的 METADATA 里 transformers/tokenizers/
       torch 之外的依赖（regex、scipy、scikit-learn、joblib、threadpoolctl、packaging、
       pyyaml、typer、huggingface-hub）在 --no-deps 下不会被装，import 时直接
       `No module named 'regex'`。所以第二步必须让 pip 解依赖。
    2. **torch 单独一步、走官方 CPU 索引**：PyPI 的 torch 默认带 CUDA（2.5GB+）。
    3. **numpy 必须删**：第二步会把 numpy 一起装进 runtime，而 Frozen 态的 `_internal`
       已带 numpy 2.x 且优先命中，外置一份会撞 ABI（见 _ensure_import_paths）。
       删完要验证「能 import」，否则等于装了个跑不起来的包。
    4. **安装前先挪走旧目录**：失败残留/已装过的 runtime 里 c10.dll 可能被本进程锁住，
       原地覆盖必失败（见 _park_runtime_dir）。
    """
    rt = runtime_dir()
    parked: Path | None = None
    try:
        rt.parent.mkdir(parents=True, exist_ok=True)
        _dep_set(phase="running", step="清理上一次的安装残留…", log="", error=None,
                 target=str(rt), python=python)
        parked = _park_runtime_dir(rt)
        rt.mkdir(parents=True, exist_ok=True)

        _dep_set(step="正在安装 torch（CPU 版，约 200MB）…")
        cmd1 = [
            python, "-m", "pip", "install", "--upgrade", "--no-warn-script-location",
            "--no-compile", "--target", str(rt), "torch",
            "--index-url", _TORCH_CPU_INDEX,
            "--extra-index-url", _pypi_index(),
        ]
        ok1, log1 = _run_pip(cmd1, on_log=lambda s: _dep_set(log=s))
        if not ok1:
            # 官方 CPU 源不可达时退回 PyPI（体积大但能装上）
            _dep_set(step="torch CPU 源不可用，改用默认源重试…", log=_redact(log1))
            cmd1b = [
                python, "-m", "pip", "install", "--upgrade", "--no-warn-script-location",
                "--no-compile", "--target", str(rt), "torch", "--index-url", _pypi_index(),
            ]
            ok1, log1 = _run_pip(cmd1b, on_log=lambda s: _dep_set(log=s))
        if not ok1:
            raise RuntimeError("torch 安装失败：\n" + _redact(log1)[-2000:])

        _dep_set(step="正在安装 sentence-transformers（含依赖，约 200MB）…")
        cmd2 = [
            python, "-m", "pip", "install", "--upgrade", "--no-warn-script-location",
            "--no-compile", "--target", str(rt), "sentence-transformers",
            "--index-url", _pypi_index(),
            "--extra-index-url", _TORCH_CPU_INDEX,
        ]
        ok2, log2 = _run_pip(cmd2, on_log=lambda s: _dep_set(log=s))
        if not ok2:
            raise RuntimeError("sentence-transformers 安装失败：\n" + _redact(log2)[-2000:])

        _dep_set(step="清理安装残留…")
        _strip_unwanted(rt)

        # 之前 import 过的 torch 仍在 sys.modules 里（指向被挪走的旧目录），
        # 不清掉的话下面的依赖探测会拿到旧对象，得出错误的结论
        _drop_loaded_modules()
        importlib.invalidate_caches()
        _ensure_import_paths()
        dep = dependency_status()
        if not dep["installed"]:
            raise RuntimeError("依赖已下载但无法导入：" + (dep.get("error") or "未知原因"))
        _dep_set(phase="done", step=f"安装完成（sentence-transformers {dep.get('version') or ''}）",
                 error=None)
        invalidate_size_cache()
        logger.info("runtime_deps_installed", dir=str(rt), version=dep.get("version"))
    except Exception as exc:  # noqa: BLE001
        msg = _redact(str(exc))[:3000]
        _dep_set(phase="failed", step="安装失败", error=msg)
        logger.warning("runtime_deps_install_failed", error=msg[:300])
    finally:
        # 旧目录里被锁的文件删不掉也无所谓，下次启动会清理
        invalidate_size_cache()
        if parked is not None and parked.exists():
            shutil.rmtree(parked, ignore_errors=True)


def _drop_loaded_modules() -> None:
    """把 runtime 提供的模块从 sys.modules 里摘掉，让下一次 import 走新目录。

    只摘顶层包：子模块会在下次 import 时随顶层一起重建。注意这不会卸载已经
    分配的原生内存（Python 做不到），所以正在被引用的模型对象仍需 _instances.clear()。
    """
    _instances.clear()
    top = (
        "torch", "sentence_transformers", "transformers", "tokenizers", "safetensors",
        "huggingface_hub", "regex", "scipy", "sklearn", "joblib", "threadpoolctl",
        "tqdm", "yaml", "typer", "packaging", "filelock", "fsspec",
    )
    for name in list(sys.modules):
        if name == "__main__":
            continue
        # 只摘本模块负责的可选依赖：numpy 不能动（核心向量库在用，且必须继续用
        # _internal 里那一份），torch 在本项目里只由本地向量模型使用，可以摘。
        if name in ("torch", "numpy") or name.split(".", 1)[0] in top:
            sys.modules.pop(name, None)


def _strip_unwanted(rt: Path) -> None:
    """删掉 runtime 里不该存在的包，省空间也避免 ABI 冲突。

    - numpy：`_internal` 已带 2.x，多一份会让 torch 加载到另一份 numpy 的 C 扩展；
      pip 的 RECORD 不删也没关系（我们不走 uninstall，只要求 import 可用）。
    - __pycache__：pip --no-compile 后理论上没有，兜底清一遍。
    """
    for bad in ("numpy", "numpy.libs"):
        p = rt / bad
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
            logger.warning("runtime_numpy_removed", path=str(p))
    for cache in list(rt.rglob("__pycache__")):
        shutil.rmtree(cache, ignore_errors=True)


def start_dependency_install() -> dict[str, Any]:
    """启动后台安装（立刻返回，前端轮询 /local/dependency/progress）。"""
    if _dep_busy():
        return {"started": False, "reason": "依赖正在安装中，请稍候"}
    if dependency_status()["installed"]:
        return {"started": False, "reason": "依赖已安装", "already_installed": True}
    python = refresh_runtime_python()
    if not python:
        return {
            "started": False,
            "reason": (
                f"未找到 Python {_RUNTIME_PY} x64 解释器，无法自动安装。"
                "请先安装 Python 3.10（勾选 x64），或在有该环境的机器上装好后拷贝数据目录。"
            ),
            "need_python": True,
        }
    _dep_set(phase="running", step="准备安装…", log="", error=None, target=str(runtime_dir()),
             python=python)
    from app.tasks.bg import spawn

    spawn("local_embed_install_deps", asyncio.to_thread(_install_task, python))
    return {"started": True, "python": python, "target": str(runtime_dir())}


def uninstall_dependencies() -> dict[str, Any]:
    """卸载 runtime 可选依赖。

    正在被本进程加载的 .pyd/.dll 在 Windows 上删不掉（句柄占用），所以：
    把 runtime 改名成 runtime.trash-<ts>，等下次启动（进程重启、句柄释放）再彻底删。
    """
    if _dep_busy():
        return {"ok": False, "reason": "依赖正在安装中，请稍后再卸载"}
    rt = runtime_dir()
    if not rt.exists():
        return {"ok": True, "message": "依赖未安装，无需卸载"}
    trash = rt.with_name(f"{rt.name}{_DEP_TRASH_SUFFIX}-{int(time.time())}")
    try:
        os.replace(rt, trash)
    except OSError:
        # 目录里有加载中的 DLL 会失败，退化为尽量删（删不掉的留到下次启动）
        shutil.rmtree(rt, ignore_errors=True)
        if rt.exists():
            # 仍然删不掉：把已释放部分清掉，至少让 _runtime_installed 变 false
            for name in ("sentence_transformers", "torch", "transformers", "tokenizers",
                         "safetensors", "huggingface_hub"):
                shutil.rmtree(rt / name, ignore_errors=True)
            invalidate_size_cache()
            return {
                "ok": False,
                "reason": "部分文件被正在运行的进程占用，已标记为待删除；请重启服务后再试一次。",
            }
        trash = None
    # 释放内存里的模型实例，否则下一次推理会用旧模块对象
    _instances.clear()
    invalidate_size_cache()
    _dep_set(phase="idle", step="依赖已卸载", log="", error=None, target=None)
    logger.info("runtime_deps_uninstalled", trash=str(trash) if trash else "")
    return {
        "ok": True,
        "message": "依赖已卸载",
        "pending_cleanup": str(trash) if trash else None,
    }


def reap_runtime_if_pending() -> int:
    """启动时清理上次卸载剩下的 runtime.trash-*（进程重启后句柄已释放）。"""
    root = cache_dir()
    removed = 0
    try:
        for d in root.glob(f"runtime{_DEP_TRASH_SUFFIX}-*"):
            if not d.is_dir():
                continue
            shutil.rmtree(d, ignore_errors=True)
            if not d.exists():
                removed += 1
    except OSError:
        pass
    invalidate_size_cache()
    if removed:
        logger.info("runtime_trash_reaped", count=removed)
    return removed


# ------------------------------------------------------------------ 缓存判定


def _hub_cache_name(model_id: str) -> str:
    """hf_hub 的缓存目录名：org/name → models--org--name。"""
    return "models--" + model_id.replace("/", "--")


def _hub_dir(model_id: str) -> Path:
    root = cache_dir() / "hub"
    d = root / _hub_cache_name(model_id)
    # 删除/清理类操作会拿这个路径去 rmtree：repo id 来自请求体，必须确认结果
    # 仍落在 root 里，否则 `../..` 之类的 id 能把父目录一起删掉
    return _ensure_within(d, root)


def _ensure_within(p: Path, root: Path) -> Path:
    """把 p 解析成绝对路径，并断言它在 root 之内；越界抛 ValidationError。"""
    try:
        resolved = p.resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise ValidationError(f"非法的模型标识（解析后越出缓存目录）：{p.name}") from exc
    return resolved


def is_model_cached(model_id: str) -> bool:
    """判断模型是否已下载到本地缓存（只看本地，不联网）。"""
    if not model_id:
        return False
    try:
        _import_hf()
        from huggingface_hub import scan_cache_dir

        info = scan_cache_dir(cache_dir() / "hub")
        for repo in info.repos:
            if repo.repo_id == model_id and repo.size_on_disk > 0:
                return True
    except Exception:  # noqa: BLE001
        pass
    # 兜底：按目录启发式判断（没有 hf_hub，或 scan_cache_dir 版本不兼容）
    d = _hub_dir(model_id)
    if not d.is_dir():
        return False
    # 有 snapshots 且非空即视为已缓存（download 未完成时 hf_hub 不会建 snapshots 链接）
    snaps = d / "snapshots"
    try:
        return snaps.is_dir() and any(p.is_dir() for p in snaps.iterdir())
    except OSError:
        return False


def manual_dir() -> Path:
    """用户上传/手拷的模型目录根。"""
    p = cache_dir() / "manual"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _looks_like_model(d: Path) -> bool:
    """一个目录像不像 sentence-transformers 模型目录。

    必须有 config.json；再要求存在权重（pytorch_model.bin / model.safetensors）。
    modules.json 是 ST 特有，不强制（也兼容纯 transformers 目录）。
    """
    if not (d / "config.json").is_file():
        return False
    return (d / "model.safetensors").is_file() or (d / "pytorch_model.bin").is_file()


def scan_local_models() -> list[dict[str, Any]]:
    """扫描已下载的模型：hf 缓存里的 + manual 下用户自己放的。

    返回 [{id, path, dim, source}]，id 对 manual 模型用目录名（作为 model_id 使用）。
    """
    out: list[dict[str, Any]] = []

    # 1) hf_hub 缓存
    hub = cache_dir() / "hub"
    if hub.is_dir():
        for d in sorted(hub.glob("models--*")):
            if not d.is_dir():
                continue
            model_id = d.name[len("models--"):].replace("--", "/")
            snaps = d / "snapshots"
            snap = None
            try:
                for p in sorted(snaps.iterdir()):
                    if p.is_dir():
                        snap = p
                        break
            except OSError:
                snap = None
            if snap is None:
                continue
            out.append(
                {
                    "id": model_id,
                    "label": _known_label(model_id),
                    "path": str(snap),
                    "dim": _MODEL_DIMS.get(model_id) or _probe_dim(snap),
                    "source": "hf",
                }
            )

    # 2) manual 目录（用户上传 zip / 手拷）
    for d in sorted(manual_dir().iterdir()):
        if not d.is_dir():
            continue
        # 允许 manual/<name>/<model files> 与 manual/<name>/<subdir>/<model files> 两种布局
        target = d if _looks_like_model(d) else None
        if target is None:
            for sub in sorted(d.iterdir()):
                if sub.is_dir() and _looks_like_model(sub):
                    target = sub
                    break
        if target is None:
            continue
        out.append(
            {
                "id": d.name,
                "label": _known_label(d.name) or d.name,
                "path": str(target),
                "dim": _MODEL_DIMS.get(d.name) or _probe_dim(target),
                "source": "manual",
            }
        )
    return out


def _known_label(model_id: str) -> str:
    for m in RECOMMENDED_MODELS:
        if m["id"] == model_id:
            return m["label"]
    return ""


def _probe_dim(model_dir: Path) -> int:
    """从 config.json 里猜维度，猜不到就 0（前端显示「未知」）。不加载模型。"""
    try:
        import json

        cfg = json.loads((model_dir / "config.json").read_text(encoding="utf-8") or "{}")
        for key in ("hidden_size", "dim", "d_model"):
            v = cfg.get(key)
            if isinstance(v, int) and v > 0:
                return v
    except Exception:  # noqa: BLE001
        pass
    return 0


def dimension_of(model_id: str) -> int:
    """已知模型的维度；未知模型返回 0（由首次推理探测）。"""
    if model_id in _MODEL_DIMS:
        return _MODEL_DIMS[model_id]
    for m in scan_local_models():
        if m["id"] == model_id and m["dim"]:
            return int(m["dim"])
    return 0


# ------------------------------------------------------------------ 下载


def _blank_progress() -> dict:
    return {
        "phase": "idle",  # idle | resolving | downloading | verifying | done | failed
        "received": 0,
        "total": 0,
        "error": None,
        "path": None,
        "updated_at": None,
    }


def _set_progress(model_id: str, **kw) -> None:
    st = _progress.setdefault(model_id, _blank_progress())
    st.update(kw)
    st["updated_at"] = int(time.time() * 1000)


def progress(model_id: str) -> dict:
    """当前下载进度快照。

    total 是「按 ignore_patterns 预估」的体积，可能比真实落盘量略小（文件被重命名/
    追加写了元数据）。这里把 total 抬到 received，保证前端不会出现
    「183MB / 183MB 但进度 150%」这种自相矛盾的显示。
    """
    st = _progress.get(model_id)
    if st is None:
        return _blank_progress()
    out = dict(st)
    received = int(out.get("received") or 0)
    total = int(out.get("total") or 0)
    if received > total:
        out["total"] = received
    return out


def _bump(model_id: str, n: int) -> None:
    """只更新「还在动」的时间戳，**不累加字节**。

    早期版本在这里累加 tqdm 的 update(n)，实测进度会跑到 total 的 200%：
    hf_hub 的 `_AggregatedTqdm` 会同时触发 `update()` 与 `update_transfer()`，
    再加上目录轮询那条路，同一批字节被算了三遍。现在字节数一律以
    `_dir_bytes()`（真实落盘量）为准，这里只负责让前端看到「有进展」。
    """
    st = _progress.setdefault(model_id, _blank_progress())
    st["updated_at"] = int(time.time() * 1000)


def _make_tqdm_class(model_id: str):
    """构造把 update(n) 转发到进度字典的 tqdm 子类。

    hf_hub 2.x 用 `_AggregatedTqdm` 把每个文件 bar 的 update(n) 汇总到父 bar，
    所以这里覆盖 update / update_transfer 两个入口即可拿到字节增量。
    """
    try:
        from tqdm.auto import tqdm as _base_tqdm
    except Exception:  # noqa: BLE001
        return None

    class _ProgressTqdm(_base_tqdm):  # type: ignore[misc, valid-type]
        def update(self, n=1):  # noqa: D102
            try:
                _bump(model_id, n)
            except Exception:  # noqa: BLE001
                pass
            return super().update(n)

        def update_transfer(self, n=1):  # hf_hub 2.x 的内部入口
            try:
                _bump(model_id, n)
            except Exception:  # noqa: BLE001
                pass
            fn = getattr(super(), "update_transfer", None)
            return fn(n) if fn else None

    return _ProgressTqdm


def _dir_bytes(model_id: str) -> int:
    """统计缓存目录里已落的字节数（含 *.incomplete 半截文件）。

    必须走 `_dir_size`（scandir）：本函数被 `_watch_dir` 每 1 秒调用一次，
    下载中的目录里仍有上千个分片文件，rglob 在 Windows 上每次要几百毫秒。
    """
    return _dir_size(_hub_dir(model_id))


async def _watch_dir(model_id: str, stop: asyncio.Event) -> None:
    """进度唯一来源：每 1 秒统计缓存目录的真实落盘字节。

    只用这一条路的原因见 `_bump()` 的说明（tqdm 钩子 + 目录轮询一起累加会重复计数）。
    这里写成「只增不减」：hf_hub 下载完会把 *.incomplete 重命名，统计值偶发回退时
    不要把进度条往回拉。
    """
    while not stop.is_set():
        try:
            n = await asyncio.to_thread(_dir_bytes, model_id)
            st = _progress.setdefault(model_id, _blank_progress())
            if n > int(st.get("received") or 0):
                st["received"] = n
                st["updated_at"] = int(time.time() * 1000)
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            continue


def _resolve_total(model_id: str) -> int:
    """预估总字节数：按同一 ignore_patterns 过滤后的文件大小求和。失败返回 0。"""
    try:
        _import_hf()
        from huggingface_hub import HfApi

        info = HfApi().model_info(model_id, files_metadata=True)
        siblings = getattr(info, "siblings", None) or []
        total = 0
        for s in siblings:
            name = getattr(s, "rfilename", "") or ""
            if _ignored(name):
                continue
            size = getattr(s, "size", None)
            if size is None:
                lfs = getattr(s, "lfs", None)
                size = getattr(lfs, "size", None) if lfs else None
            if isinstance(size, int) and size > 0:
                total += size
        return total
    except Exception as exc:  # noqa: BLE001
        logger.warning("local_embed_total_unavailable", model_id=model_id, err=str(exc)[:160])
        return 0


def _ignored(filename: str) -> bool:
    """按 _IGNORE_PATTERNS 判断文件名是否跳过（只支持 * 通配，够用且无依赖）。"""
    import fnmatch

    for pat in _IGNORE_PATTERNS:
        if fnmatch.fnmatch(filename, pat):
            return True
    return False


async def _download_task(model_id: str) -> None:
    """后台下载：解析总量 → snapshot_download（带 tqdm 钩子）→ 目录轮询兜底。"""
    stop = asyncio.Event()
    watcher: asyncio.Task | None = None
    try:
        _set_progress(model_id, phase="resolving", received=0, total=0, error=None, path=None)
        # 进度监控只覆盖 hf 源；manual/本地目录无下载概念
        total = await asyncio.to_thread(_resolve_total, model_id)
        _set_progress(model_id, phase="downloading", total=total)
        watcher = asyncio.ensure_future(_watch_dir(model_id, stop))

        tqdm_cls = _make_tqdm_class(model_id)

        def _do() -> str:
            _apply_hf_env()
            _import_hf()  # 保证 huggingface_hub 已就绪且 sys.path 正确
            from huggingface_hub import snapshot_download

            kwargs: dict[str, Any] = {
                "repo_id": model_id,
                "cache_dir": str(cache_dir() / "hub"),
                "ignore_patterns": _IGNORE_PATTERNS,
            }
            if tqdm_cls is not None:
                kwargs["tqdm_class"] = tqdm_cls
            return snapshot_download(**kwargs)

        path = await asyncio.to_thread(_do)
        stop.set()
        if watcher is not None:
            await asyncio.gather(watcher, return_exceptions=True)
        final = _dir_bytes(model_id) or int(progress(model_id).get("received") or 0)
        _set_progress(
            model_id,
            phase="done",
            received=final,
            total=final or int(progress(model_id).get("total") or 0),
            path=path,
            error=None,
        )
        logger.info("local_model_downloaded", model_id=model_id, path=path)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)[:300] or "下载失败"
        _set_progress(model_id, phase="failed", error=msg)
        logger.warning("local_model_download_failed", model_id=model_id, error=msg)
    finally:
        stop.set()
        if watcher is not None and not watcher.done():
            watcher.cancel()
        _BUSY.discard(model_id)


def start_download(model_id: str, *, source: str = "hf") -> dict:
    """启动后台下载。立刻返回，前端轮询 progress。

    幂等：已在下载中 → started=False；已缓存 → started=False + already_cached，
    避免重复拉一遍 400MB。
    """
    model_id = (model_id or "").strip()
    if not model_id:
        raise ValidationError("请指定 model_id")
    if "/" not in model_id and _known_label(model_id) == "":
        # 允许用户手填任意 repo id（含单段名），但空串/非法字符要拦
        if any(c.isspace() for c in model_id):
            raise ValidationError("model_id 不能包含空白字符")

    dep = dependency_status()
    if not dep["installed"]:
        return {
            "started": False,
            "reason": "未安装 sentence-transformers，无法下载模型",
            "install_hint": install_hint(),
            "error": dep["error"],
        }
    if source not in ("hf", "modelscope"):
        raise ValidationError(f"不支持的模型来源：{source}")
    if source == "modelscope":
        # ModelScope 的下载协议与 hf_hub 不同，需要额外适配；这里如实告知而不是假装支持
        raise ValidationError(
            "暂不支持直接从 ModelScope 下载。可先用 modelscope 工具下载模型目录，"
            "打包成 zip 后在「导入本地模型」上传"
        )

    if model_id in _BUSY:
        return {"started": False, "reason": "该模型正在下载中，请稍候"}
    if is_model_cached(model_id):
        return {"started": False, "reason": "该模型已下载", "already_cached": True}

    _BUSY.add(model_id)
    _progress[model_id] = {**_blank_progress(), "phase": "resolving",
                           "updated_at": int(time.time() * 1000)}
    from app.tasks.bg import spawn

    spawn(f"local_embed_download:{model_id}", _download_task(model_id))
    return {"started": True, "model_id": model_id}


def delete_model(model_id: str) -> dict:
    """删除已下载的模型目录。正在被加载的实例先卸载。

    `model_id` 来自请求体，而这里要做 rmtree：hub 缓存按 `org/name` 拼目录、
    manual 按目录名拼。两者都必须确认最终路径仍在缓存根之内，
    否则 `../../x` 这类 id 能把缓存根外面的目录删掉。
    """
    model_id = (model_id or "").strip()
    if not model_id:
        raise ValidationError("请指定 model_id")
    if _looks_like_relative_escape(model_id):
        raise ValidationError("非法的模型标识：不允许出现空段或 . / .. 路径段")
    removed: list[str] = []
    # 1) hf 缓存（_hub_dir 内部已做越界校验）
    d = _hub_dir(model_id)
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
        removed.append(str(d))
    # 2) manual 目录（按目录名匹配，只允许单段目录名）
    name = model_id.rsplit("/", 1)[-1]
    if name not in ("", ".", ".."):
        m = _ensure_within(manual_dir() / name, manual_dir())
        if m.is_dir():
            shutil.rmtree(m, ignore_errors=True)
            removed.append(str(m))
    # 3) 释放内存里的实例
    for key in [k for k in _instances if k.split("@", 1)[0] == model_id]:
        _instances.pop(key, None)
    _progress.pop(model_id, None)
    _BUSY.discard(model_id)
    if not removed:
        return {"ok": True, "message": "该模型不在本地缓存中"}
    logger.info("local_model_deleted", model_id=model_id, paths=removed)
    return {"ok": True, "message": "已删除", "paths": removed}


def _looks_like_relative_escape(p: str) -> bool:
    """路径里是否含 `.` / `..` 段（`BAAI/bge-small-zh-v1.5` 这类正常 repo id 不会）。"""
    return any(seg in ("", ".", "..") for seg in p.replace("\\", "/").split("/"))


# ------------------------------------------------------------------ 导入 zip


def _safe_member(name: str) -> bool:
    """拒绝绝对路径、盘符与路径穿越。"""
    if not name or name.startswith("/") or name.startswith("\\"):
        return False
    norm = name.replace("\\", "/")
    if len(norm) > 1 and norm[1] == ":":
        return False
    return ".." not in norm.split("/")


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return ((info.external_attr >> 16) & 0o170000) == 0o120000


def _extract_zip(zip_path: Path, dest: Path) -> int:
    """安全解压到 dest。拒绝绝对路径 / `..` / 符号链接，限制文件数与总量。"""
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise ValidationError(f"不是有效的 zip 文件：{exc}") from exc
    count = 0
    total = 0
    try:
        with zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename.replace("\\", "/")
                if not _safe_member(name):
                    raise ValidationError(f"压缩包内含非法路径（疑似路径穿越）：{info.filename}")
                if _is_symlink(info):
                    raise ValidationError(f"压缩包内含符号链接，已拒绝：{info.filename}")
                count += 1
                total += info.file_size
                if count > _MAX_FILES:
                    raise ValidationError(f"文件数超过上限 {_MAX_FILES}")
                if total > _MAX_TOTAL_BYTES:
                    raise ValidationError("解压后总量超过上限（2GB）")
                target = dest / name
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out, _CHUNK)
                except OSError as exc:
                    raise ValidationError(f"解压失败：{exc}（条目：{name[:120]}）") from exc
    finally:
        pass
    return count


def install_from_zip(zip_path: Path, *, name: str | None = None) -> dict:
    """把上传的模型 zip 装到 manual/<name>/。同步执行，便于直测。

    布局兼容两种：
      - zip 根目录直接是模型文件（config.json 在根）→ 全部内容装进 manual/<name>/
      - zip 里有一个顶层目录（常见于 GitHub/网盘下载）→ 把该目录整体放进 manual/<name>/
    """
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise ValidationError("上传的模型包不存在")
    if zip_path.stat().st_size <= 0:
        raise ValidationError("上传的模型包为空")

    model_name = (name or "").strip() or zip_path.stem or f"manual-{int(time.time())}"
    model_name = model_name.replace("/", "-").replace("\\", "-").strip(".")
    if not model_name:
        raise ValidationError("无法确定模型名，请指定 name")

    final = manual_dir() / model_name
    partial = manual_dir() / f"{model_name}.partial"
    staging = manual_dir() / f"{model_name}.ready"
    shutil.rmtree(partial, ignore_errors=True)
    shutil.rmtree(staging, ignore_errors=True)
    partial.mkdir(parents=True, exist_ok=True)
    try:
        _extract_zip(zip_path, partial)
        # 单层顶层目录 → 视为包了一层，取其内容
        entries = list(partial.iterdir())
        src = partial
        if len(entries) == 1 and entries[0].is_dir():
            src = entries[0]
        if not _looks_like_model(src):
            # 兼容带子目录的布局：找一个像模型的子目录
            found = None
            for sub in src.iterdir():
                if sub.is_dir() and _looks_like_model(sub):
                    found = sub
                    break
            if found is None:
                raise ValidationError(
                    "压缩包里没找到 sentence-transformers 模型文件"
                    "（需要 config.json + model.safetensors 或 pytorch_model.bin）"
                )
            src = found
        # 落位：把 src 内容搬到 staging，再原子替换 final
        staging.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            shutil.move(str(item), str(staging / item.name))
        shutil.rmtree(final, ignore_errors=True)
        os.replace(staging, final)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(partial, ignore_errors=True)

    dim = _probe_dim(final)
    logger.info("local_model_imported", model_id=model_name, path=str(final), dim=dim)
    return {"ok": True, "model_id": model_name, "path": str(final), "dim": dim}


# ------------------------------------------------------------------ 推理


class LocalEmbedder:
    """sentence-transformers 包装：懒加载 + 线程内推理 + 归一化向量。

    注意：首次 `_load()` 会占用 300~600MB 内存并耗时 10~30 秒（视模型大小），
    因此调用方必须走 `asyncio.to_thread`，不能阻塞事件循环。
    """

    def __init__(self, model_id: str, device: str = "cpu") -> None:
        self.model_id = model_id
        self.device = device
        self._model: Any = None
        self._dim = _MODEL_DIMS.get(model_id, 0)
        # 用 threading.Lock 而不是 asyncio.Lock：推理在线程里执行（to_thread），
        # 同一实例的并发加载可能发生在多个线程中
        self._lock = threading.Lock()

    @property
    def dimension(self) -> int:
        return self._dim

    @property
    def loaded(self) -> bool:
        """模型是否已真正加载进内存（健康检查据此决定要不要触发加载）。"""
        return self._model is not None

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model
            _apply_hf_env()
            _ensure_import_paths()
            try:
                from sentence_transformers import SentenceTransformer
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    "本地向量模型依赖未安装。\n" + install_hint()
                ) from exc
            # manual 模型：先用扫描结果定位真实目录；没找到就交给 ST 按 id 解析
            path_or_id = self._resolve_path() or self.model_id
            logger.info("local_model_loading", model=self.model_id, device=self.device)
            model = SentenceTransformer(str(path_or_id), device=self.device)
            try:
                # ST 6.x 把 get_sentence_embedding_dimension 改名为 get_embedding_dimension，
                # 旧名仍可用但会打 FutureWarning；两个都试，兼容用户自带的旧版本
                getter = getattr(model, "get_embedding_dimension", None) or getattr(
                    model, "get_sentence_embedding_dimension"
                )
                self._dim = int(getter())
            except Exception:  # noqa: BLE001
                pass
            self._model = model
            logger.info("local_model_loaded", model=self.model_id, dim=self._dim)
            return self._model

    def _resolve_path(self) -> str | None:
        """manual 模型 → 真实目录；hf 模型 → None（让 ST 走缓存解析）。"""
        for m in scan_local_models():
            if m["id"] == self.model_id and m.get("source") == "manual":
                return str(m["path"])
        return None

    def embed_sync(self, texts: list[str], *, batch_size: int = 32) -> list[list[float]]:
        model = self._load()
        vecs = model.encode(
            list(texts),
            batch_size=batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [list(map(float, v)) for v in vecs]

    async def embed(self, texts: list[str], *, batch_size: int = 32) -> list[list[float]]:
        return await asyncio.to_thread(self.embed_sync, texts, batch_size=batch_size)


def get_embedder(model_id: str, device: str = "cpu") -> LocalEmbedder:
    """按 model_id@device 取单例（进程内）。"""
    key = f"{model_id}@{device}"
    inst = _instances.get(key)
    if inst is None:
        inst = LocalEmbedder(model_id, device=device)
        _instances[key] = inst
    return inst


def unload(model_id: str) -> None:
    """释放模型实例（删模型/切换前调用）。"""
    for key in [k for k in _instances if k.split("@", 1)[0] == model_id]:
        _instances.pop(key, None)


def require_model(model_id: str) -> None:
    """校验模型可用，否则抛可读的 ValidationError（供 API 层直接抛出）。"""
    if not model_id:
        raise NotFoundError("未指定本地模型")
    if not is_model_cached(model_id):
        raise ValidationError(
            f"本地模型「{model_id}」尚未下载。请在「模型管理 → 本地离线向量模型」中点下载。"
        )


# 模块导入即生效：保证后续任何 huggingface_hub 导入都走对端点（见文件头第 4 条）
_apply_hf_env()
