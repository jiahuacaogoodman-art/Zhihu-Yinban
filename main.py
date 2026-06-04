# -*- coding: utf-8 -*-
"""
@Time    : 2026/03/08 10:10
@Author  : jiahuaCao
@File    : main.py
@Desc    : "智护银伴" 后端应用主入口
"""

import os
from pathlib import Path

# ----------------------------------------------------------------
# .env 自动加载（必须在 `from app.core.config import ...` 之前）
# ----------------------------------------------------------------
# 部署场景里有两类用户会被坑：
#   1) 裸机 `uvicorn main:app ...`：systemd / docker 之外没人帮忙注入 .env，
#      AUTH_TOKEN / PII_ENCRYPTION_KEY / OLLAMA_MODEL_NAME 等就会全部读不到。
#   2) Windows 的 scripts\run.ps1：之前只检查 .env 是否存在，没真正加载。
# python-dotenv 已经在 requirements.txt 里，这里显式调用一次即可全部修好。
#
# 行为约束：
#   - override=False  环境变量优先于 .env（systemd/docker 的注入永远赢）
#   - 找不到 .env 不报错（生产里通常不放 .env，靠运行时注入）
#   - 即便 python-dotenv 缺失也不要因此让进程起不来，只打个 warning
try:
    from dotenv import load_dotenv

    _env_file = Path(__file__).resolve().parent / ".env"
    if _env_file.is_file():
        load_dotenv(dotenv_path=_env_file, override=False)
except Exception as _e:  # pragma: no cover - 仅在 python-dotenv 缺失时触发
    import logging
    logging.getLogger(__name__).warning(
        f"未能加载 .env 文件（python-dotenv 可能未安装）：{_e}"
    )

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from contextlib import asynccontextmanager
import chromadb
from sentence_transformers import SentenceTransformer
from loguru import logger

from app.core.config import (
    CHROMA_DB_PATH,
    CHROMA_COLLECTION_NAME,
    EMBEDDING_MODEL_NAME,
    EMBEDDING_DEVICE,
    EMBEDDING_MODEL_LOCAL_PATH,
    EMBEDDING_ALLOW_DEGRADED,
    EHR_UPLOAD_DIR,
    AUTH_TOKEN,
    LLM_PROVIDER,
)
from app.middleware.auth import AuthTokenMiddleware, ReadAuditMiddleware
from app.routers import auth as auth_router
from app.routers import ehr, nursing
from app.routers import beds, care_levels, handovers, incidents, care_records
from app.routers import admissions, operations
from app.services.pii_crypto import is_encryption_enabled
from app.services.user_store import UserStore

# ----------------------------------------------------------------
# 关键修复：使用 Path(__file__).resolve() 获取绝对路径
# 无论在 Windows 还是 Linux、无论从哪个目录启动，路径始终正确
# ----------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
INDEX_HTML = STATIC_DIR / "index.html"
UPLOAD_DIR = Path(EHR_UPLOAD_DIR)

# 全局应用状态字典，存储 ChromaDB 连接和 Embedding 模型实例
app_state = {}

# ----------------------------------------------------------------
# UserStore 初始化（模块级）
# 为什么在 lifespan 之外做：AuthTokenMiddleware 在 app.add_middleware() 时
# 构造，那时 lifespan 还没跑。把 UserStore 提前到模块级初始化，让中间件
# 能拿到实例引用；同时对测试也更友好（不依赖 TestClient 触发 lifespan）。
#
# UserStore 只操作一个独立 SQLite 文件，无外部依赖，模块级初始化安全。
# ----------------------------------------------------------------
_USER_STORE_DIR = BASE_DIR / "local_auth"
_USER_STORE_DIR.mkdir(parents=True, exist_ok=True)
user_store = UserStore(_USER_STORE_DIR / "users.db")
# 旧部署只配了 AUTH_TOKEN 环境变量 → 自动把它注入为 admin 的 API Key
# （UserStore 为空时才会执行；否则忽略，避免每次启动重复创建）
user_store.bootstrap_legacy_admin(AUTH_TOKEN)

# ----------------------------------------------------------------
# PII 加密启动检查
# Phase 1B 起：加密覆盖 10 个字段；未配置密钥会带明文风险。
# 在启动时就判断一次并显著告警（仅 logger，不阻塞启动），
# 另外 /health 会把当前状态暴露给运维系统。
# ----------------------------------------------------------------
if is_encryption_enabled():
    logger.success("PII 加密已启用（Fernet / cryptography）— 高敏字段写入 ChromaDB 前会加密")
else:
    logger.warning(
        "PII 加密未启用：PII_ENCRYPTION_KEY 未配置或 cryptography 未安装。"
        "高敏字段（姓名/身份证/床位/联系人/过敏史等）将以明文写入 ChromaDB。"
        "生产环境务必配置 PII_ENCRYPTION_KEY。"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI 应用的生命周期事件，在应用启动时执行初始化，在应用关闭时执行清理。

    启动韧性设计：
      - ChromaDB 初始化失败 → 仍然致命（无法存取任何档案）
      - Embedding 模型加载失败 → 如果 EMBEDDING_ALLOW_DEGRADED=true，
        应用降级启动（RAG 检索不可用，但基础 LLM 对话仍可用）
      - 启动前会做 preflight 检查（缓存目录可写性、模型路径存在性）
    """
    # --- 应用启动时 ---
    logger.info("应用启动中...")
    logger.info(f"项目根目录: {BASE_DIR}")
    logger.info(f"前端页面路径: {INDEX_HTML}")

    # 1. 初始化 ChromaDB 客户端
    logger.info(f"正在连接本地 ChromaDB，数据存储路径: {CHROMA_DB_PATH}")
    try:
        db_client = chromadb.PersistentClient(path=str(CHROMA_DB_PATH))
        app_state["db_client"] = db_client
        logger.success("ChromaDB 连接成功！")

        # 2. 获取或创建 Collection
        collection = db_client.get_or_create_collection(
            name=CHROMA_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"}
        )
        app_state["db_collection"] = collection
        logger.success(f"ChromaDB Collection '{CHROMA_COLLECTION_NAME}' 加载成功！")

    except Exception as e:
        logger.error(f"ChromaDB 初始化失败: {e}")
        raise RuntimeError(f"ChromaDB 初始化失败: {e}") from e

    # 3. 加载 Embedding 模型（带 preflight 检查和降级能力）
    embedding_loaded = False
    model_path = EMBEDDING_MODEL_LOCAL_PATH or EMBEDDING_MODEL_NAME

    # ── Preflight 检查 ──
    _preflight_embedding()

    logger.info(f"正在加载 Embedding 模型: {model_path}，使用设备: {EMBEDDING_DEVICE}")
    try:
        embedding_function = SentenceTransformer(model_path, device=EMBEDDING_DEVICE)
        app_state["embedding_function"] = embedding_function
        app_state["db_collection"].embedding_function = embedding_function
        app_state["rag_available"] = True
        embedding_loaded = True
        logger.success("Embedding 模型加载成功！")
    except Exception as e:
        error_msg = _classify_embedding_error(e)
        logger.error(f"Embedding 模型加载失败: {error_msg}")

        if EMBEDDING_ALLOW_DEGRADED:
            logger.warning(
                "⚠️  降级模式启动：RAG 向量检索功能不可用，但基础 LLM 对话仍正常。\n"
                "    修复建议：\n"
                "    1. 确认缓存目录可写: HF_HOME / SENTENCE_TRANSFORMERS_HOME\n"
                "    2. 设置 EMBEDDING_MODEL_LOCAL_PATH 指向本地模型目录（离线部署）\n"
                "    3. 确认网络可访问 huggingface.co（首次下载）\n"
                "    4. 设置 EMBEDDING_ALLOW_DEGRADED=false 可恢复严格模式（启动失败即退出）"
            )
            app_state["embedding_function"] = None
            app_state["rag_available"] = False
        else:
            raise RuntimeError(f"Embedding 模型加载失败: {error_msg}") from e

    if embedding_loaded:
        logger.info("所有核心模块初始化完成，应用准备就绪！")
    else:
        logger.warning("应用以降级模式启动（RAG 不可用）— 基础功能正常")

    yield

    # --- 应用关闭时 ---
    logger.info("应用关闭中...")
    app_state.clear()
    logger.info("资源已清理，应用已关闭。")


def _preflight_embedding():
    """
    Embedding 加载前的预检查，提前给出结构化的诊断信息。
    不会抛异常，只输出 warning/info 级别日志。
    """
    import os as _os

    # 检查缓存目录可写性
    cache_dirs = {
        "HF_HOME": _os.environ.get("HF_HOME", ""),
        "SENTENCE_TRANSFORMERS_HOME": _os.environ.get("SENTENCE_TRANSFORMERS_HOME", ""),
        "XDG_CACHE_HOME": _os.environ.get("XDG_CACHE_HOME", ""),
    }
    for env_name, dir_path in cache_dirs.items():
        if not dir_path:
            continue
        p = Path(dir_path)
        if p.exists() and not _os.access(str(p), _os.W_OK):
            logger.warning(
                f"⚠️  Preflight: 缓存目录不可写 {env_name}={dir_path}\n"
                f"    Embedding 模型下载/加载将失败。请 chown 或挂载为可写卷。"
            )
        elif not p.exists():
            logger.info(f"Preflight: 缓存目录不存在 {env_name}={dir_path}，将尝试自动创建")
            try:
                p.mkdir(parents=True, exist_ok=True)
                logger.info(f"  → 已创建 {dir_path}")
            except OSError as e:
                logger.warning(f"  → 创建失败: {e}")

    # 检查本地模型路径（如果指定了 EMBEDDING_MODEL_LOCAL_PATH）
    if EMBEDDING_MODEL_LOCAL_PATH:
        local_p = Path(EMBEDDING_MODEL_LOCAL_PATH)
        if not local_p.exists():
            logger.warning(
                f"⚠️  Preflight: EMBEDDING_MODEL_LOCAL_PATH={EMBEDDING_MODEL_LOCAL_PATH} 不存在。\n"
                f"    模型加载将失败。请确认模型文件已下载到该路径。"
            )
        elif not (local_p / "config.json").exists():
            logger.warning(
                f"⚠️  Preflight: {EMBEDDING_MODEL_LOCAL_PATH} 中未找到 config.json。\n"
                f"    可能不是有效的 sentence-transformers 模型目录。"
            )
        else:
            logger.info(f"Preflight: 本地模型路径有效 → {EMBEDDING_MODEL_LOCAL_PATH}")


def _classify_embedding_error(e: Exception) -> str:
    """
    将 embedding 加载异常分类为人类可读的诊断信息。
    """
    msg = str(e)
    if "Permission denied" in msg:
        return (
            f"权限不足 — 缓存目录不可写。\n"
            f"    原始错误: {msg}\n"
            f"    修复: 设置 HF_HOME/SENTENCE_TRANSFORMERS_HOME 到可写目录，或 chown 当前目录"
        )
    elif "No space left" in msg:
        return (
            f"磁盘空间不足。\n"
            f"    原始错误: {msg}\n"
            f"    修复: 清理磁盘或扩容，embedding 模型约需 100-500 MB"
        )
    elif "ConnectionError" in msg or "HTTPSConnectionPool" in msg or "resolve" in msg.lower():
        return (
            f"网络连接失败 — 无法从 HuggingFace 下载模型。\n"
            f"    原始错误: {msg}\n"
            f"    修复: (1) 确认容器可访问 huggingface.co\n"
            f"          (2) 或设置 EMBEDDING_MODEL_LOCAL_PATH 指向预下载的模型目录\n"
            f"          (3) 或配置 HF_ENDPOINT=https://hf-mirror.com（国内镜像）"
        )
    elif "not a valid model" in msg.lower() or "config.json" in msg.lower():
        return (
            f"模型文件无效或不完整。\n"
            f"    原始错误: {msg}\n"
            f"    修复: 删除缓存重新下载，或指定正确的 EMBEDDING_MODEL_LOCAL_PATH"
        )
    else:
        return f"{msg}"


# 创建 FastAPI 应用实例
app = FastAPI(
    title="智护银伴 - 本地 RAG 核心后端",
    description="一个为基层养老院设计的、100%本地化运行的AI护理辅助系统。",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,    # 禁用 /docs Swagger 页面，避免与前端混淆
    redoc_url=None    # 禁用 /redoc 页面
)

# 挂载 API 路由
app.include_router(auth_router.router, prefix="/api", tags=["Auth / Users"])
app.include_router(ehr.router, prefix="/api", tags=["EHR Management"])
app.include_router(nursing.router, prefix="/api", tags=["Nursing Decision Support"])
app.include_router(beds.router, prefix="/api", tags=["Bed Management"])
app.include_router(care_levels.router, prefix="/api", tags=["Care Level"])
app.include_router(handovers.router, prefix="/api", tags=["Handover / SBAR"])
app.include_router(incidents.router, prefix="/api", tags=["Incident Report"])
app.include_router(care_records.router, prefix="/api", tags=["Care Records"])
app.include_router(admissions.router, prefix="/api", tags=["Admission Workflow"])
app.include_router(operations.router, prefix="/api", tags=["Operations Dashboard"])

# ----------------------------------------------------------------
# 鉴权中间件：保护 /api/* 和 /uploads/*
# 三种模式（见 app/middleware/auth.py 顶部注释）：
#   - user_store：UserStore 已有用户（常态，bootstrap_legacy_admin 之后也是此模式）
#   - legacy_token：UserStore 为空但 AUTH_TOKEN 非空（极少出现，bootstrap 成功后即脱离）
#   - disabled：两者都空（仅限开发/测试）
#
# 中间件执行顺序（Starlette 栈是 LIFO，后 add 的先跑）：
#   外层  →  AuthTokenMiddleware    先鉴权，未通过直接 401
#   内层  →  ReadAuditMiddleware    鉴权通过后，对 /uploads/* 的 2xx 响应记 RECORD_PREVIEW
# 所以必须**先** add_middleware(ReadAuditMiddleware)，**后** add_middleware(AuthTokenMiddleware)。
# ----------------------------------------------------------------
app.add_middleware(ReadAuditMiddleware)
app.add_middleware(AuthTokenMiddleware, legacy_token=AUTH_TOKEN, user_store=user_store)

# 把 UserStore 和 auth_mode 挂到 app.state，供路由通过 request.app.state 读取，
# 避免 routers/auth.py 反向 import main 模块（破坏层次）。
app.state.user_store = user_store
if user_store.has_users():
    app.state.auth_mode = "user_store"
    logger.info(f"鉴权已启用（user_store 模式）：/api/* 和 /uploads/* 需要 X-Auth-Token")
elif AUTH_TOKEN:
    app.state.auth_mode = "legacy_token"
    logger.info("鉴权已启用（legacy_token 模式）：/api/* 和 /uploads/* 需要 X-Auth-Token")
else:
    app.state.auth_mode = "disabled"
    logger.warning(
        "AUTH_TOKEN 未配置且 UserStore 为空，鉴权已关闭"
        "（仅限开发/测试；生产请设置 AUTH_TOKEN 或通过 /api/auth/users 建号）"
    )

# ----------------------------------------------------------------
# 关键修复：使用绝对路径挂载静态文件目录
# 确保 Windows 和 Linux 下均能正确找到 static/ 文件夹
# ----------------------------------------------------------------
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    logger.info(f"静态文件目录已挂载: {STATIC_DIR}")
else:
    logger.warning(f"未找到静态文件目录: {STATIC_DIR}，前端页面将不可用")

# 病历照片原件访问目录：文件仍保存在本地磁盘，仅在内网服务中按 URL 预览。
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")
logger.info(f"病历照片上传目录已挂载: {UPLOAD_DIR}")

# ----------------------------------------------------------------
# 全局异常处理：按异常类型分层处理
# ----------------------------------------------------------------
# 设计要点：
# 1. HTTPException / StarletteHTTPException（404、400、413、503 等）走 FastAPI
#    的默认逻辑，保留业务路由里手写的 status_code 和 detail，不能被兜底成 500。
# 2. RequestValidationError（422）保留 Pydantic 的字段级错误信息，便于前端排错。
# 3. 只有真正未处理的 Exception 才落进 500 通道；500 响应体里**不能**回写
#    原始 exception 字符串，避免泄露内部路径 / SQL / 堆栈。完整信息只进日志。
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    detail = exc.detail if exc.detail is not None else ""
    if request.url.path.startswith("/api") or exc.status_code >= 500:
        logger.warning(f"HTTPException {exc.status_code} at {request.url}: {detail}")
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.status_code, "message": str(detail) or "request failed"},
        headers=getattr(exc, "headers", None) or None,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    logger.info(f"请求校验失败 at {request.url}: {exc.errors()}")
    return JSONResponse(
        status_code=422,
        content={
            "code": 422,
            "message": "请求参数校验失败",
            "errors": exc.errors(),
        },
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    # 用 logger.exception 保留完整堆栈到服务端日志；响应体里不回写 exc。
    logger.exception(f"未处理的服务端异常 at {request.url}")
    return JSONResponse(
        status_code=500,
        content={"code": 500, "message": "服务器内部错误，请稍后重试或联系管理员"},
    )

# ----------------------------------------------------------------
# 前端页面入口：使用绝对路径返回 index.html
# ----------------------------------------------------------------
@app.get("/", include_in_schema=False)
async def frontend():
    """返回前端可视化管理页面。"""
    if INDEX_HTML.is_file():
        return FileResponse(str(INDEX_HTML))
    return JSONResponse(
        status_code=404,
        content={"message": f"前端页面文件未找到，请确认 {INDEX_HTML} 存在"}
    )

# 护工端入口
@app.get("/nurse", include_in_schema=False)
async def nurse_frontend():
    """返回护工端页面。"""
    nurse_html = STATIC_DIR / "nurse.html"
    if nurse_html.is_file():
        return FileResponse(str(nurse_html))
    return JSONResponse(
        status_code=404,
        content={"message": f"护工端页面文件未找到，请确认 {nurse_html} 存在"}
    )

# 健康检查端点
@app.get("/health", tags=["Health Check"], summary="健康检查")
async def health_check():
    """
    健康检查端点，供 systemd / k8s / 负载均衡探针使用。

    Phase 1B 起新增运维可观测字段：
      pii_encryption_enabled  布尔，Fernet 加密当前是否生效（密钥配置 + 库可用）
      auth_mode               user_store / legacy_token / disabled
      rag_available           布尔，RAG 向量检索是否可用（embedding 模型是否加载成功）
    """
    return {
        "status": "ok",
        "message": "智护银伴后端服务正在运行",
        "base_dir": str(BASE_DIR),
        "static_dir_exists": STATIC_DIR.is_dir(),
        "index_html_exists": INDEX_HTML.is_file(),
        # 运维可观测 ─────────────────────────────────────────
        "pii_encryption_enabled": is_encryption_enabled(),
        "auth_mode": getattr(app.state, "auth_mode", "unknown"),
        "llm_provider": LLM_PROVIDER,  # ollama / openai
        "rag_available": app_state.get("rag_available", False),
    }


if __name__ == "__main__":
    # 开发模式热重载：仅在显式设置 RELOAD=1 时开启。
    # 生产环境（systemd / docker / uvicorn --workers N）不应启用 reload——
    # 它会和多 worker 冲突，并且每次 .py 变更都会重新加载 embedding 模型。
    reload_enabled = os.getenv("RELOAD", "0").lower() in {"1", "true", "yes"}
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host=host, port=port, reload=reload_enabled)
