class LlamaPack:

    @staticmethod
    def download(pack_name, pack_path):
        try:
            from llama_index.llama_pack import download_llama_pack

            return download_llama_pack(pack_name, pack_path)
        except Exception as e:
            raise ImportError(f"Error in downloading llama pack: {e}")

    @staticmethod
    def build(pack_name, pack_path, args=[], **kwargs):
        pack = LlamaPack.download(pack_name, pack_path)
        return pack(*args, **kwargs)

    @staticmethod
    def stock_market_pack(pack_path="./stock_market_data_pack", args=[], **kwargs):
        name_ = "StockMarketDataQueryEnginePack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def embedded_table_pack(
        pack_path="./embedded_tables_unstructured_pack", args=[], **kwargs
    ):
        name_ = "RecursiveRetrieverSmallToBigPack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def rag_evaluator_pack(pack_path="./rag_evaluator_pack", args=[], **kwargs):
        name_ = "RagEvaluatorPack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def ollma_pack(pack_path="./ollama_pack", args=[], **kwargs):
        name_ = "OllamaQueryEnginePack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def llm_compiler_agent_pack(
        pack_path="./llm_compiler_agent_pack", args=[], **kwargs
    ):
        name_ = "LLMCompilerAgentPack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def resume_screener_pack(pack_path="./resume_screener_pack", args=[], **kwargs):
        name_ = "ResumeScreenerPack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def ragatouille_retriever_pack(pack_path="./ragatouille_pack", args=[], **kwargs):
        name_ = "RAGatouilleRetrieverPack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def chain_of_table_pack(pack_path="./chain_of_table_pack", args=[], **kwargs):
        name_ = "ChainOfTablePack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def hybrid_fusion_retriever_pack(
        pack_path="./hybrid_fusion_pack", args=[], **kwargs
    ):
        name_ = "HybridFusionRetrieverPack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def neo4j_query_engine_pack(pack_path="./neo4j_pack", args=[], **kwargs):
        name_ = "Neo4jQueryEnginePack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def llava_completion_pack(pack_path="./llava_pack", args=[], **kwargs):
        name_ = "LlavaCompletionPack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def sentence_window_retriever_pack(
        pack_path="./sentence_window_retriever_pack", args=[], **kwargs
    ):
        name_ = "SentenceWindowRetrieverPack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def dense_x_retrieval_pack(pack_path="./dense_pack", args=[], **kwargs):
        name_ = "DenseXRetrievalPack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def zephyr_query_engine_pack(pack_path="./zephyr_pack", args=[], **kwargs):
        name_ = "ZephyrQueryEnginePack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def query_rewriting_retriever_pack(
        pack_path="./query_rewriting_pack", args=[], **kwargs
    ):
        name_ = "QueryRewritingRetrieverPack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def fuzzy_citation_engine_pack(
        pack_path="./fuzzy_citation_pack", args=[], **kwargs
    ):
        name_ = "FuzzyCitationEnginePack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def multidoc_auto_retriever_pack(
        pack_path="./multidoc_autoretrieval_pack", args=[], **kwargs
    ):
        name_ = "MultiDocAutoRetrieverPack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def auto_merging_retriever_pack(
        pack_path="./auto_merging_retriever_pack", args=[], **kwargs
    ):
        name_ = "AutoMergingRetrieverPack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def voyage_query_engine_pack(pack_path="./voyage_pack", args=[], **kwargs):
        name_ = "VoyageQueryEnginePack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def mix_self_consistency_pack(
        pack_path="./mix_self_consistency_pack", args=[], **kwargs
    ):
        name_ = "MixSelfConsistencyPack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def rag_fusion_pipeline_pack(
        pack_path="./rag_fusion_pipeline_pack", args=[], **kwargs
    ):
        name_ = "RAGFusionPipelinePack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def multi_document_agents_pack(
        pack_path="./multi_doc_agents_pack", args=[], **kwargs
    ):
        name_ = "MultiDocumentAgentsPack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def llama_guard_moderator_pack(pack_path="./llamaguard_pack", args=[], **kwargs):
        name_ = "LlamaGuardModeratorPack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def evaluator_benchmarker_pack(
        pack_path="./eval_benchmark_pack", args=[], **kwargs
    ):
        name_ = "EvaluatorBenchmarkerPack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def amazon_product_extraction_pack(
        pack_path="./amazon_product_extraction_pack", args=[], **kwargs
    ):
        name_ = "AmazonProductExtractionPack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def llama_dataset_metadata_pack(
        pack_path="./llama_dataset_metadata_pack", args=[], **kwargs
    ):
        name_ = "LlamaDatasetMetadataPack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def multi_tenancy_rag_pack(pack_path="./multitenancy_rag_pack", args=[], **kwargs):
        name_ = "MultiTenancyRAGPack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def gmail_openai_agent_pack(pack_path="./gmail_pack", args=[], **kwargs):
        name_ = "GmailOpenAIAgentPack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def snowflake_query_engine_pack(pack_path="./snowflake_pack", args=[], **kwargs):
        name_ = "SnowflakeQueryEnginePack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def agent_search_retriever_pack(pack_path="./agent_search_pack", args=[], **kwargs):
        name_ = "AgentSearchRetrieverPack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def vectara_rag_pack(pack_path="./vectara_rag_pack", args=[], **kwargs):
        name_ = "VectaraRagPack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def chroma_autoretrieval_pack(pack_path="./chroma_pack", args=[], **kwargs):
        name_ = "ChromaAutoretrievalPack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def arize_phoenix_query_engine_pack(pack_path="./arize_pack", args=[], **kwargs):
        name_ = "ArizePhoenixQueryEnginePack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def redis_ingestion_pipeline_pack(
        pack_path="./redis_ingestion_pack", args=[], **kwargs
    ):
        name_ = "RedisIngestionPipelinePack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def nebula_graph_query_engine_pack(
        pack_path="./nebulagraph_pack", args=[], **kwargs
    ):
        name_ = "NebulaGraphQueryEnginePack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)

    @staticmethod
    def weaviate_retry_engine_pack(pack_path="./weaviate_pack", args=[], **kwargs):
        name_ = "WeaviateRetryEnginePack"
        return LlamaPack.build(name_, pack_path, args, **kwargs)
