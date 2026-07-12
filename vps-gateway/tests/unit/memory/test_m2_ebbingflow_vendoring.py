"""M2: ebbingflow 组件 vendoring 测试。

验证：
  - 15 个直接复用模块可导入
  - 3 个适配模块可导入且基本功能可用
  - LLMBridge 解耦 token_monitor
  - vendored 组件不引用 vps-gateway config.py
"""
import importlib
import inspect
import pytest


class TestDirectReuseImports:
    """15 个直接复用模块导入测试。"""

    MODULES = [
        "app.adapters.memory.ebbingflow.event_slots",
        "app.adapters.memory.ebbingflow.hybrid_scorer",
        "app.adapters.memory.ebbingflow.identity_canonical",
        "app.adapters.memory.ebbingflow.identity_schema",
        "app.adapters.memory.ebbingflow.identity_state_reducer",
        "app.adapters.memory.ebbingflow.identity_resolver",
        "app.adapters.memory.ebbingflow.identity_inference",
        "app.adapters.memory.ebbingflow.identity_evolution",
        "app.adapters.memory.ebbingflow.identity_conflict_resolver",
        "app.adapters.memory.ebbingflow.event_extractor",
        "app.adapters.memory.ebbingflow.saga_manager",
        "app.adapters.memory.ebbingflow.episode_manager",
        "app.adapters.memory.ebbingflow.vector_storer",
        "app.adapters.memory.ebbingflow.vector_retriever",
        "app.adapters.memory.ebbingflow.sql_event_repository",
    ]

    @pytest.mark.parametrize("module_path", MODULES)
    def test_module_importable(self, module_path):
        """每个直接复用模块可导入。"""
        mod = importlib.import_module(module_path)
        assert mod is not None

    def test_event_slots_action_type(self):
        """event_slots 基本功能：ActionType 枚举。"""
        from app.adapters.memory.ebbingflow.event_slots import ActionType
        assert ActionType.STATE_CHANGE.value == "STATE_CHANGE"
        assert ActionType.PLAN.value == "PLAN"

    def test_hybrid_scorer_basic(self):
        """hybrid_scorer 基本功能：ScoredCandidate 和 HybridScorer。"""
        from app.adapters.memory.ebbingflow.hybrid_scorer import (
            HybridScorer, ScoredCandidate, UnifiedMemoryResult,
        )
        scorer = HybridScorer()
        assert scorer is not None
        c = ScoredCandidate(
            content="test", speaker="user", timestamp="2025-01-01T00:00:00Z",
            source_type="vector_chat", source_name="s1",
        )
        assert c.content == "test"

    def test_identity_canonical_basic(self):
        """identity_canonical 基本功能：实体归一化。"""
        from app.adapters.memory.ebbingflow.identity_canonical import canonicalize_entity
        assert canonicalize_entity("你") == "assistant"
        assert canonicalize_entity("我") == "user"
        assert canonicalize_entity("他") == ""  # 噪音拦截
        assert canonicalize_entity("张三") == "张三"  # 保留具体名


class TestAdaptedModules:
    """3 个适配模块测试。"""

    def test_llm_bridge_importable(self):
        """LLMBridge 适配模块可导入。"""
        from app.adapters.memory.ebbingflow.llm_bridge import LLMBridge, LLMBridgeConfig
        assert LLMBridge is not None
        assert LLMBridgeConfig is not None

    def test_llm_bridge_no_token_monitor(self):
        """LLMBridge 不引用 core.monitoring.token_monitor。"""
        from app.adapters.memory.ebbingflow import llm_bridge
        source = inspect.getsource(llm_bridge)
        assert "token_monitor" not in source
        assert "core.monitoring" not in source

    def test_llm_bridge_no_openai_sdk(self):
        """LLMBridge 不使用 openai SDK，使用 httpx。"""
        from app.adapters.memory.ebbingflow import llm_bridge
        source = inspect.getsource(llm_bridge)
        assert "from openai" not in source
        assert "import openai" not in source
        assert "httpx" in source

    def test_sql_pool_importable(self):
        """sql_pool 适配模块可导入。"""
        from app.adapters.memory.ebbingflow.sql_pool import get_db, set_default_db_path
        assert get_db is not None
        assert set_default_db_path is not None

    def test_sql_pool_no_asyncpg(self):
        """sql_pool 不引用 asyncpg。"""
        from app.adapters.memory.ebbingflow import sql_pool
        source = inspect.getsource(sql_pool)
        assert "asyncpg" not in source
        assert "postgres" not in source.lower()

    @pytest.mark.asyncio
    async def test_sql_pool_get_db(self, tmp_path):
        """sql_pool get_db 基本功能。"""
        from app.adapters.memory.ebbingflow.sql_pool import set_default_db_path, get_db
        set_default_db_path(str(tmp_path / "test.sqlite3"))
        async with get_db() as conn:
            cursor = await conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER)")
            await conn.commit()
        # 能正常创建表说明连接可用

    def test_knowledge_engine_importable(self):
        """knowledge_engine 适配模块可导入。"""
        from app.adapters.memory.ebbingflow.knowledge_engine import KnowledgeEngine
        assert KnowledgeEngine is not None

    def test_knowledge_engine_no_neo4j(self):
        """knowledge_engine 不引用 neo4j driver。"""
        from app.adapters.memory.ebbingflow import knowledge_engine
        source = inspect.getsource(knowledge_engine)
        assert "AsyncGraphDatabase" not in source
        assert "from neo4j" not in source

    def test_knowledge_engine_infer_query_intent(self):
        """infer_query_intent 直接复用——零改动。"""
        from app.adapters.memory.ebbingflow.knowledge_engine import KnowledgeEngine
        assert KnowledgeEngine.infer_query_intent("你花了多少钱") == "fact"
        assert KnowledgeEngine.infer_query_intent("最近怎么样") == "summary"
        assert KnowledgeEngine.infer_query_intent("我们的长期目标") == "long_term"
        assert KnowledgeEngine.infer_query_intent("你好") == "semantic"

    def test_persona_manager_importable(self):
        """persona_manager 适配模块可导入。"""
        from app.adapters.memory.ebbingflow.persona_manager import PersonaManager
        assert PersonaManager is not None

    def test_persona_manager_no_neo4j(self):
        """persona_manager 不引用 neo4j driver。"""
        from app.adapters.memory.ebbingflow import persona_manager
        source = inspect.getsource(persona_manager)
        assert "AsyncGraphDatabase" not in source
        assert "from neo4j" not in source

    def test_persona_manager_helper_functions(self):
        """persona_manager 辅助函数直接复用。"""
        from app.adapters.memory.ebbingflow.persona_manager import (
            extract_user_profile_rewrite,
            _normalize_relationship_to_user,
            _infer_assistant_role,
        )
        # ebbingflow helpers use English regex patterns; verify they work with English text
        result = extract_user_profile_rewrite("I am Zhang San, 25 years old")
        assert result.get("age") == "25"
        assert _normalize_relationship_to_user("user_assistant") == "assistant_to_user"
        # verify function is callable and returns a dict
        assert isinstance(extract_user_profile_rewrite(""), dict)


class TestNoConfigLeak:
    """vendored 组件不引用 vps-gateway config.py。"""

    VENDORED_MODULES = [
        "app.adapters.memory.ebbingflow.event_slots",
        "app.adapters.memory.ebbingflow.hybrid_scorer",
        "app.adapters.memory.ebbingflow.identity_canonical",
        "app.adapters.memory.ebbingflow.identity_schema",
        "app.adapters.memory.ebbingflow.identity_state_reducer",
        "app.adapters.memory.ebbingflow.identity_resolver",
        "app.adapters.memory.ebbingflow.identity_inference",
        "app.adapters.memory.ebbingflow.identity_evolution",
        "app.adapters.memory.ebbingflow.identity_conflict_resolver",
        "app.adapters.memory.ebbingflow.event_extractor",
        "app.adapters.memory.ebbingflow.saga_manager",
        "app.adapters.memory.ebbingflow.episode_manager",
        "app.adapters.memory.ebbingflow.vector_storer",
        "app.adapters.memory.ebbingflow.vector_retriever",
        "app.adapters.memory.ebbingflow.sql_event_repository",
        "app.adapters.memory.ebbingflow.llm_bridge",
        "app.adapters.memory.ebbingflow.sql_pool",
        "app.adapters.memory.ebbingflow.knowledge_engine",
        "app.adapters.memory.ebbingflow.persona_manager",
    ]

    @pytest.mark.parametrize("module_path", VENDORED_MODULES)
    def test_no_vps_gateway_config_import(self, module_path):
        """vendored 组件不引用 vps-gateway 的 config.py。"""
        mod = importlib.import_module(module_path)
        source = inspect.getsource(mod)
        # 不引用 vps-gateway 的 infrastructure.config
        assert "from app.infrastructure.config" not in source
        assert "import app.infrastructure.config" not in source
