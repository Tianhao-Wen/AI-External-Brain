"""外脑业务服务层：把存储、检索、会议分析、邮件、PPT 组合成 API/工具可调用的接口。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable

from aieb.brain import knowledge, mailer, meetings, paths, pptgen
from aieb.brain.store import BrainStore
from aieb.config import ProviderConfig

PublishFn = Callable[[Path, str], dict[str, Any]]

#: 知识库问答的系统提示。
#:
#: 引用准确率优化（要求"不要补充说明、只标必需页"、以及生成后的确定性引用剪枝）
#: 在开发集/已暴露测试集/未暴露测试集上都没有取得可验证的提升——引用准确率的变化
#: 落在 ±1.2~2.3 个百分点的噪声范围内，且"不写补充说明"会让答案正确率下降 2 道题。
#: 因此最终**保持原有提示不变**，只把重复的内联提示收敛为这个常量，并把剪枝实现
#: （``knowledge.prune_citations``，默认关闭）与单元测试留在仓库里备查。
ANSWER_SYSTEM = (
    "你是企业知识库问答助手。只能依据给定资料回答，并在句末用 [来源：文件名 · 第N页] 标注依据；"
    "资料中没有的信息必须明确说“资料中未提及”，不得凭常识补充。用简体中文回答。"
)


class BrainService:
    def __init__(
        self,
        *,
        work_dir: str | Path,
        provider: ProviderConfig,
        memory_manager: Any = None,
        publish: PublishFn | None = None,
        retrieval_config: Any = None,
    ) -> None:
        self.work_dir = Path(work_dir).resolve()
        self.provider = provider
        self.store = BrainStore(self.work_dir)
        self.index = knowledge.BM25Index(self.store)
        self.memory_manager = memory_manager
        self._publish = publish
        # 检索配置默认从环境变量读；默认 mode=bm25，与改造前完全一致。
        from aieb.brain.retrieval.config import RetrievalConfig

        self.retrieval_config = retrieval_config or RetrievalConfig.from_env()
        self._retrieval: Any = None
        self._vector_indexer: Any = None
        #: LangGraph 工作流（惰性创建；未安装可选依赖时不影响原有功能）
        self._workflow: Any = None
        self._workflow_store: Any = None

    # ------------------------------------------------------------------
    # 混合检索
    # ------------------------------------------------------------------
    @property
    def retrieval(self) -> Any:
        """惰性构造 RetrievalService（bm25 模式下不加载模型/网络）。"""
        if self._retrieval is None:
            from aieb.brain.retrieval.service import RetrievalService

            self._retrieval = RetrievalService(
                bm25=self.index, config=self.retrieval_config, store=self.store
            )
        return self._retrieval

    @property
    def vector_indexer(self) -> Any:
        """向量索引同步器（与检索服务共享 embedder / 向量库实例）。"""
        if self._vector_indexer is None:
            from aieb.brain.retrieval.index import VectorIndexer

            service = self.retrieval
            self._vector_indexer = VectorIndexer(
                self.store,
                service.embedder,
                service.vector_store,
                scope_id=self.retrieval_config.scope,
                knowledge_base_id=self.retrieval_config.knowledge_base_id,
            )
        return self._vector_indexer

    # ------------------------------------------------------------------
    # LangGraph 工作流
    # ------------------------------------------------------------------
    @property
    def workflow_store(self) -> Any:
        """编排元数据库（运行表 / Trace / 幂等键），与业务库分开。"""
        if self._workflow_store is None:
            from aieb.workflow.persistence import WorkflowStore

            path = self.work_dir / ".aieb" / "workflow" / "workflow.sqlite3"
            self._workflow_store = WorkflowStore(path)
        return self._workflow_store

    @property
    def workflow(self) -> Any:
        """会议 → 风险/待办 → 邮件审批 → 发送 的有状态工作流。

        只在真的用到时才 import langgraph：未装可选依赖时，原有 Agent / RAG /
        会议功能仍然可以正常启动和使用。
        """
        if self._workflow is None:
            from aieb.workflow.deps import build_real_deps
            from aieb.workflow.runner import WorkflowRunner

            deps = build_real_deps(
                service=self, wf_store=self.workflow_store, workspace=self.work_dir
            )
            self._workflow = WorkflowRunner(deps=deps, workspace=self.work_dir)
        return self._workflow

    def close(self) -> None:
        """释放工作流 loop 线程与业务库连接（服务退出时调用）。"""
        if self._workflow is not None:
            try:
                self._workflow.close()
            except Exception:  # noqa: BLE001 - 关闭失败不应影响退出
                pass
            self._workflow = None
        if self._workflow_store is not None:
            try:
                self._workflow_store.close()
            except Exception:  # noqa: BLE001
                pass
            self._workflow_store = None
        self.store.close()

    # ------------------------------------------------------------------
    # 知识库
    # ------------------------------------------------------------------
    def knowledge_context(self, limit: int = 8) -> str:
        """把"知识库里有什么"告诉模型。

        为什么需要它：工具描述里说"可以检索知识库"，但模型并不知道知识库当前
        是否为空、里面有哪些文档。Benchmark 里 3 次"该查不查"的失败，根因就是
        模型缺少这个状态信息，于是选择反问用户"要我查一下吗"。
        没有文档时返回空串，避免让模型误以为有资料可查。
        """
        try:
            docs = [d for d in self.store.list_documents() if d.get("status") in {"ready", "partial"}]
        except Exception:  # noqa: BLE001 - 状态注入失败不应影响主流程
            return ""
        if not docs:
            return (
                "# 知识库状态\n"
                "当前知识库为空（用户还没有上传任何文档）。涉及公司制度、项目资料的问题，"
                "请如实说明资料尚未上传，不要用常识编造，也不要假装查过。"
            )
        lines = ["# 知识库状态", f"当前知识库已加载 {len(docs)} 份文档："]
        for doc in docs[:limit]:
            pages = doc.get("pages")
            suffix = f"（{pages} 页）" if pages else ""
            lines.append(f"- {doc.get('name') or doc.get('filename') or '未命名文档'}{suffix}")
        if len(docs) > limit:
            lines.append(f"- 以及另外 {len(docs) - limit} 份文档")
        lines.append(
            "凡是询问公司制度、流程、金额、比例、周期、标准、条款等**具体事实**的问题，"
            "先调用 SearchKnowledge 检索再回答；检索结果为空时明确说知识库里没有，"
            "不要先反问用户要不要查。"
        )
        return "\n".join(lines)

    def has_documents(self) -> bool:
        """知识库里是否已有可检索的文档（供路由提示判断是否需要强制检索）。"""
        try:
            return any(
                d.get("status") in {"ready", "partial"} for d in self.store.list_documents()
            )
        except Exception:  # noqa: BLE001
            return False

    def upload(self, filename: str, data: bytes) -> dict[str, Any]:
        doc = knowledge.ingest(
            self.store,
            filename=filename,
            data=data,
            upload_dir=paths.uploads_dir(self.work_dir),
            scope_id=self.retrieval_config.scope,
        )
        if doc.get("status") in {"ready", "partial"} and not doc.get("reused"):
            # BM25：增量追加，避免每传一份文档就全量重新分词（批量入库会退化成 O(N²)）
            self.index.add_chunks(
                [
                    {
                        **chunk,
                        "doc_id": doc["id"],
                        "doc_name": doc.get("name", ""),
                        "document_key": doc.get("document_key", ""),
                    }
                    for chunk in self.store.chunks_of(doc["id"])
                ]
            )
            # 向量索引：仅在混合检索启用时同步；bm25 模式下零模型/网络开销
            if self.retrieval_config.uses_vector:
                doc["vector_sync"] = self.vector_indexer.sync_document(doc["id"]).as_dict()
        return doc

    def delete_document(self, doc_id: str) -> bool:
        # 先取 document_key：删完就查不到了，而向量库需要它保证删除彻底
        doc = self.store.get_document(doc_id) or {}
        ok = knowledge.delete_document(self.store, doc_id)
        if ok:
            self.index.invalidate()
            self.index.rebuild()
            if self.retrieval_config.uses_vector:
                try:
                    self.vector_indexer.remove_document(doc_id)
                    if doc.get("document_key"):
                        self.vector_indexer.vector_store.delete_by_document_key(
                            str(doc["document_key"])
                        )
                except Exception:  # noqa: BLE001 - 向量删除失败不应阻断业务删除
                    pass
        return ok

    def delete_activity(self, activity_id: str) -> bool:
        return self.store.delete_activity(activity_id)

    def clear_activities(self) -> int:
        return self.store.clear_activities()

    def _scoped_doc_ids(self, doc_ids: set[str] | None) -> set[str] | None:
        """把显式 doc_ids 与当前作用域取交集。

        BM25 没有 payload 过滤能力（向量路有），所以作用域隔离必须在应用层做。
        注意：**空集合不能直接传给索引** —— `doc_ids` 为空在索引里等同“不过滤”，
        会把整个库吐出来。这里用一个不可能存在的哨兵表达“一个都不允许”。
        """
        try:
            scoped = self.store.document_ids_in_scope(self.retrieval_config.scope)
        except Exception:  # noqa: BLE001 - 老库无 scope 字段时退化为不限制
            return doc_ids
        allowed = scoped if doc_ids is None else (set(doc_ids) & scoped)
        if not allowed:
            return {"__no_document_in_scope__"}
        return allowed

    def search(self, query: str, top_k: int = 5, doc_ids: set[str] | None = None) -> list[dict[str, Any]]:
        """统一检索入口。

        `mode=bm25` 时**直接调用 `BM25Index.search`**（与改造前逐位等价），
        但同样要遵守作用域：否则默认模式下会出现“混合检索做了隔离、BM25 没做”的串库。
        其余模式才走混合检索。降级信息保存在 `last_retrieval`，供调用方如实标注。
        """
        if self.retrieval_config.mode == "bm25":
            self.last_retrieval = {
                "mode": "bm25",
                "degraded": False,
                "routes_used": ["bm25"],
                "scope": self.retrieval_config.scope,
            }
            return self.index.search(
                query, top_k=top_k, doc_ids=self._scoped_doc_ids(doc_ids)
            )
        result = self.retrieval.search(
            query, top_k=top_k, doc_ids=sorted(doc_ids) if doc_ids else None
        )
        self.last_retrieval = result.as_dict()
        return result.hits

    def search_with_trace(
        self, query: str, top_k: int = 5, doc_ids: set[str] | None = None
    ) -> dict[str, Any]:
        """带完整溯源信息的检索（benchmark 与排障用）。"""
        if self.retrieval_config.mode == "bm25":
            scoped = self._scoped_doc_ids(doc_ids)
            hits = self.index.search(query, top_k=top_k, doc_ids=scoped)
            return {
                "hits": hits,
                "trace": {
                    "mode": "bm25",
                    "degraded": False,
                    "routes_used": ["bm25"],
                    "scope": self.retrieval_config.scope,
                },
            }
        result = self.retrieval.search(
            query, top_k=top_k, doc_ids=sorted(doc_ids) if doc_ids else None
        )
        return {"hits": result.hits, "trace": result.as_dict()}

    def build_context(self, hits: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
        """按配置构建上下文，返回 (文本, 四层预算埋点)。

        `legacy` 保持改造前行为（字符预算 + 早停）；`budgeted` 用 token 预算分配。
        """
        if self.retrieval_config.context_budget == "budgeted":
            from aieb.brain.retrieval.context import build_budgeted_context

            bundle = build_budgeted_context(
                hits,
                max_tokens=self.retrieval_config.context_max_tokens,
                chars_per_token=self.retrieval_config.chars_per_token,
                max_chars=self.retrieval_config.context_max_chars,
            )
            return bundle.text, bundle.as_dict()
        text = knowledge.build_context(hits)
        return text, {
            "retrieved": len(hits),
            "selected": len(hits),
            "included": text.count("[来源："),
            "chars": len(text),
            "budget": "legacy",
        }

    async def answer(self, question: str, top_k: int = 5) -> dict[str, Any]:
        """基于知识库回答问题；没有命中就如实说检索不到，不编造。"""
        hits = self.search(question, top_k=top_k)
        citations = knowledge.format_citations(hits)
        if not hits:
            return {
                "answer": "在已上传的文档中没有检索到与该问题相关的内容。"
                          "请确认文档是否已成功解析，或换一种提问方式。",
                "citations": [],
                "grounded": False,
            }
        context, context_meta = self.build_context(hits)
        system = ANSWER_SYSTEM
        prompt = f"资料：\n{context}\n\n问题：{question}"
        try:
            answer = await asyncio.to_thread(_sync_complete, self.provider, system, prompt)
        except Exception as exc:  # noqa: BLE001 - 模型不可用时降级为原文摘录
            excerpt = "\n\n".join(
                f"· {h['doc_name']} 第{h['page']}页：{h['text'][:200]}" for h in hits[:3]
            )
            return {
                "answer": f"模型暂时不可用（{exc}），以下是检索到的原文片段，供你直接核对：\n\n{excerpt}",
                "citations": citations,
                "grounded": True,
                "degraded": True,
            }
        # 确定性引用校验（默认关闭，详见 knowledge.prune_citations 的说明）
        if knowledge.citation_pruning_enabled():
            answer, checked = knowledge.prune_citations(answer, hits)
        else:
            checked = []
        payload: dict[str, Any] = {
            "answer": answer,
            "citations": citations,
            "grounded": True,
            "context_meta": context_meta,
        }
        # 混合检索/降级信息：让界面能如实说明"本次用的是哪种检索"
        trace = getattr(self, "last_retrieval", None)
        if trace:
            payload["retrieval"] = trace
        if checked:
            payload["citation_checks"] = checked
        return payload

    # ------------------------------------------------------------------
    # 会议
    # ------------------------------------------------------------------
    async def analyze_meeting(self, filename: str, text: str) -> dict[str, Any]:
        # 同一份会议记录重复上传（刷新后重传、E2E 反复跑）不应该再分析一遍：
        # 既浪费模型调用，又会让风险/待办/长期记忆成倍重复。命中已有会议时
        # 直接复用，并带上 reused 标记让界面如实说明。
        existing = self.store.find_meeting_by_text(text)
        if existing is not None:
            return {**existing, "reused": True}
        analysis = await meetings.analyze_async(self.provider, text, source_name=filename)
        cleaned = meetings.sanitize(analysis, text)
        meeting = meetings.save(self.store, cleaned, text, filename)
        return meeting

    async def draft_email(self, meeting_id: str, to_addr: str = "", extra: str = "") -> dict[str, Any]:
        meeting = self.store.get_meeting(meeting_id)
        if meeting is None:
            raise KeyError("会议不存在")
        draft = await meetings.draft_email(self.provider, meeting, to_addr=to_addr, extra_instruction=extra)
        return self.store.create_email(
            to_addr=draft["to_addr"], subject=draft["subject"], body=draft["body"],
            meeting_id=meeting_id, kind="reminder",
        )

    def send_email(self, email_id: str, **overrides: Any) -> dict[str, Any]:
        email = self.store.get_email(email_id)
        if email is None:
            return {"ok": False, "message": "邮件草稿不存在", "error_code": "not_found"}
        for key in ("to_addr", "subject", "body"):
            if overrides.get(key):
                email = self.store.update_email(email_id, **{key: overrides[key]}) or email
        if email["status"] == "sent":
            return {
                "ok": False,
                "message": "这封邮件已经发送过，不能重复发送",
                "error_code": "already_sent",
            }
        self.store.update_email(email_id, status="sending", error="")
        result = mailer.send_email(
            to_addr=email["to_addr"], subject=email["subject"], body=email["body"]
        )
        if result["ok"]:
            updated = self.store.update_email(
                email_id, status="sent", error="", sent_at=str(result.get("sent_at") or "")
            )
            self.store.add_activity("email", f"发送会议提醒邮件：{email['subject']}", email["to_addr"])
            return {"ok": True, "message": result["message"], "email": updated}
        updated = self.store.update_email(email_id, status="failed", error=str(result["message"]))
        return {
            "ok": False,
            "message": result["message"],
            "error_code": result.get("error_code", "send_failed"),
            "email": updated,
        }

    # ------------------------------------------------------------------
    # PPT
    # ------------------------------------------------------------------
    async def make_ppt(
        self,
        *,
        topic: str,
        slide_count: int = 8,
        focus: str = "",
        audience: str = "",
    ) -> dict[str, Any]:
        result = await pptgen.generate(
            self.provider,
            self.store,
            topic=topic,
            slide_count=slide_count,
            focus=focus,
            audience=audience,
            index=self.index,
            work_dir=self.work_dir,
        )
        payload: dict[str, Any] = {
            "filename": result["filename"],
            "slide_count": result["slide_count"],
            "outline": result["outline"],
            # 下划线前缀 = 仅供服务端内部（工具复制文件）使用，API 层会剔除
            "_path": str(result["path"]),
        }
        if self._publish is not None:
            payload["file"] = self._publish(Path(result["path"]), result["filename"])
        return payload

    # ------------------------------------------------------------------
    # 工作台 / 记忆
    # ------------------------------------------------------------------
    def dashboard(self) -> dict[str, Any]:
        return {
            "stats": self.store.stats(),
            "recent_meetings": self.store.list_meetings(limit=5),
            "recent_todos": self.store.list_todos()[:5],
            "recent_risks": self.store.list_risks(status="open")[:5],
            "activities": self.store.list_activities(limit=8),
            "documents": self.store.list_documents()[:5],
        }

    def recall(self, query: str = "", limit: int = 8) -> list[dict[str, Any]]:
        """跨会话召回：在结构化事实与对话记忆里按关键词找相关条目。

        这是"新建对话后问上次会议的结论"能答上来的关键：结构化事实存在 SQLite 里，
        不在这轮对话的上下文中，必须由工具显式检索。
        """
        terms = _memory_terms(query)
        candidates: list[dict[str, Any]] = []
        for fact in self.store.list_memory_facts():
            candidates.append({
                "category": fact["category"],
                "content": fact["content"],
                "source": fact.get("source_label") or "会议记录",
                "created_at": fact.get("created_at") or "",
                "kind": "fact",
            })
        if self.memory_manager is not None:
            try:
                for entry in self.memory_manager.list_for_web():
                    candidates.append({
                        "category": entry.get("type") or "note",
                        "content": (entry.get("body") or entry.get("preview") or "").strip(),
                        "source": entry.get("source_label") or "对话记忆",
                        "created_at": "",
                        "kind": "note",
                    })
            except Exception:  # noqa: BLE001 - 记忆文件异常不应影响召回
                pass

        def score(item: dict[str, Any]) -> int:
            if not terms:
                return 1
            text = f"{item['content']} {item['source']}".lower()
            return sum(1 for term in terms if term in text)

        # 只命中"会议"这类通用词不足以证明相关：查询有多个词元时要求至少命中 2 个，
        # 避免把别的会议记忆一起倒出来（长期记忆最怕张冠李戴）。
        min_hits = 2 if len(terms) >= 3 else 1
        scored = [(score(item), item) for item in candidates]
        scored = [(s, item) for s, item in scored if s >= min_hits]
        scored.sort(key=lambda pair: (pair[0], pair[1].get("created_at") or ""), reverse=True)
        return [item for _, item in scored[: max(1, min(int(limit or 8), 30))]]

    def memory_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if self.memory_manager is not None:
            try:
                for entry in self.memory_manager.list_for_web():
                    items.append({
                        "id": f"file:{entry['id']}",
                        "kind": "note",
                        "category": "note",
                        "content": entry.get("preview") or entry.get("title") or "",
                        "title": entry.get("title") or "",
                        "body": entry.get("body") or "",
                        "source_label": entry.get("source_label") or "对话记忆",
                        "created_at": "",
                        "scope": entry.get("scope") or "project",
                    })
            except Exception:  # noqa: BLE001 - 记忆文件异常不应阻塞页面
                pass
        for fact in self.store.list_memory_facts():
            items.append({
                "id": fact["id"],
                "kind": "fact",
                "category": fact["category"],
                "content": fact["content"],
                "title": fact["content"][:40],
                "body": fact["content"],
                "source_label": fact.get("source_label") or "会议记录",
                "created_at": fact.get("created_at") or "",
                "scope": "project",
            })
        return items

    def delete_memory(self, item_id: str) -> tuple[bool, str]:
        if item_id.startswith("file:"):
            if self.memory_manager is None:
                return False, "记忆模块不可用"
            return self.memory_manager.delete_by_id(item_id[len("file:"):])
        ok = self.store.delete_memory_fact(item_id)
        return (ok, "记忆已删除" if ok else "找不到这条记忆")

    def delete_memories(self, item_ids: list[str]) -> tuple[int, list[str]]:
        """批量删除记忆（结构化事实走数据库，对话记忆走记忆文件）。

        返回 (删除条数, 失败的 ID 列表)；逐条删除、互不影响，
        避免一条异常导致整批回退。
        """
        deleted = 0
        failed: list[str] = []
        for item_id in item_ids:
            ok, _message = self.delete_memory(item_id)
            if ok:
                deleted += 1
            else:
                failed.append(item_id)
        return deleted, failed

    def delete_todos(self, todo_ids: list[str]) -> int:
        return self.store.delete_todos(todo_ids)


def _memory_terms(query: str) -> set[str]:
    """记忆检索用的词元：英文单词 + 中文 bigram（不引入分词依赖）。"""
    import re

    text = (query or "").lower()
    terms = {w for w in re.findall(r"[a-z0-9][a-z0-9\-]{1,}", text) if not w.isdigit()}
    for seq in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(seq) == 1:
            terms.add(seq)
        else:
            terms.update(seq[i:i + 2] for i in range(len(seq) - 1))
    return terms


def _sync_complete(provider: ProviderConfig, system: str, prompt: str) -> str:
    return asyncio.run(_complete_async(provider, system, prompt))


async def _complete_async(provider: ProviderConfig, system: str, prompt: str) -> str:
    from aieb.brain import llm

    return await llm.complete(provider, system, prompt)
