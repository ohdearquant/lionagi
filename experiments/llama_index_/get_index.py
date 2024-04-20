class BaseIndex:

    @staticmethod
    def _get_index(
        input_=None,
        # default to OpenAI
        llm=None,
        llm_provider=None,
        llm_kwargs={},
        service_context=None,
        service_context_kwargs={},
        index_type=None,
        index_kwargs={},
        rerank_=False,
        reranker_type=None,
        reranker=None,
        rerank_kwargs={},
        get_engine=False,
        engine_kwargs={},
        from_storage=False,
        storage_context=None,
        strorage_context_kwargs={},
        index_id=None,
        load_index_from_storage_kwargs={},
    ):
        """
        Creates and returns an index or query engine based on the provided parameters.

        Args:
            chunks: The input data to be indexed or queried.
            llm: An instance of a language model for indexing or querying.
            llm_provider: A function to provide an instance of a language model.
            llm_kwargs: Keyword arguments for configuring the language model.
            service_context: An instance of a service context.
            service_context_kwargs: Keyword arguments for configuring the service context.
            index_type: The type of index to create.
            index_kwargs: Keyword arguments for configuring the index.
            rerank_: Boolean flag indicating whether reranking should be applied.
            reranker_type: The type of reranker to use.
            reranker: An instance of a reranker.
            rerank_kwargs: Keyword arguments for configuring the reranker.
            get_engine: Boolean flag indicating whether to return a query engine.
            engine_kwargs: Keyword arguments for configuring the query engine.

        Returns:
            Index or Query Engine: Depending on the 'get_engine' flag, returns an index or query engine.

        Raises:
            Various exceptions if there are errors in creating the index or query engine.
        """

        if from_storage:
            from llama_index import StorageContext, load_index_from_storage

            storage_context = StorageContext.from_defaults(**strorage_context_kwargs)

            if index_id:
                index = load_index_from_storage(
                    storage_context=storage_context,
                    index_id=index_id,
                    **load_index_from_storage_kwargs,
                )
            else:
                raise ValueError("Index ID is required for loading from storage.")

            if rerank_:
                if not reranker:
                    if not reranker_type:
                        from llama_index.postprocessor import LLMRerank

                        reranker_type = LLMRerank
                    reranker = reranker_type(
                        service_context=service_context, **rerank_kwargs
                    )
                engine_kwargs.update({"node_postprocessors": [reranker]})

            if get_engine:
                return (index, index.as_query_engine(**engine_kwargs))
            return index

        if not llm:
            if llm_provider:
                llm = llm_provider(**llm_kwargs)
            else:
                from llama_index.llms import OpenAI

                llm = OpenAI(**llm_kwargs)

        if not service_context:
            from llama_index import ServiceContext

            service_context = ServiceContext.from_defaults(
                llm=llm, **service_context_kwargs
            )

        if not index_type:
            from llama_index import VectorStoreIndex

            index_type = VectorStoreIndex

        index = index_type(input_, service_context=service_context, **index_kwargs)

        if index_id:
            index.index_id = index_id

        if rerank_:
            if not reranker:
                if not reranker_type:
                    from llama_index.postprocessor import LLMRerank

                    reranker_type = LLMRerank
                reranker = reranker_type(
                    service_context=service_context, **rerank_kwargs
                )
            engine_kwargs.update({"node_postprocessors": [reranker]})

        if get_engine:
            return (index, index.as_query_engine(**engine_kwargs))
        return index


class LlamaIndex:

    @staticmethod
    def kg_index(
        input_=None,
        # default to OpenAI
        llm=None,
        llm_provider=None,
        llm_kwargs={"temperature": 0.1, "model": "gpt-4-1106-preview"},
        service_context=None,
        service_context_kwargs={},
        index_kwargs={"include_embeddings": True},
        rerank_=False,
        reranker_type=None,
        reranker=None,
        rerank_kwargs={"choice_batch_size": 5, "top_n": 3},
        get_engine=False,
        engine_kwargs={"similarity_top_k": 3, "response_mode": "tree_summarize"},
        kg_triplet_extract_fn=None,
        from_storage=False,
        storage_context=None,
        strorage_context_kwargs={},
        index_id=None,
        load_index_from_storage_kwargs={},
    ):
        """
        Creates and returns a KnowledgeGraphIndex based on the provided parameters.

        Args:
            chunks: The input data to be indexed.
            llm: An instance of a language model for indexing.
            llm_provider: A function to provide an instance of a language model.
            llm_kwargs: Keyword arguments for configuring the language model.
            service_context: An instance of a service context.
            service_context_kwargs: Keyword arguments for configuring the service context.
            index_kwargs: Keyword arguments for configuring the index.
            rerank_: Boolean flag indicating whether reranking should be applied.
            reranker_type: The type of reranker to use.
            reranker: An instance of a reranker.
            rerank_kwargs: Keyword arguments for configuring the reranker.
            get_engine: Boolean flag indicating whether to return a query engine.
            engine_kwargs: Keyword arguments for configuring the query engine.
            kg_triplet_extract_fn: Optional function for extracting KG triplets.

        Returns:
            KnowledgeGraphIndex or Query Engine: Depending on the 'get_engine' flag,
            returns a KnowledgeGraphIndex or query engine.

        Raises:
            Various exceptions if there are errors in creating the index or query engine.
        """
        from llama_index import KnowledgeGraphIndex

        index_type_ = ""
        if not from_storage:
            from llama_index.graph_stores import SimpleGraphStore
            from llama_index.storage.storage_context import StorageContext

            graph_store = SimpleGraphStore()
            if storage_context is None:
                storage_context = StorageContext.from_defaults(
                    graph_store=graph_store, **strorage_context_kwargs
                )
            index_kwargs.update({"storage_context": storage_context})
            index_type_ = KnowledgeGraphIndex.from_documents

        elif from_storage:
            index_type_ = KnowledgeGraphIndex

        if kg_triplet_extract_fn:
            index_kwargs.update({"kg_triplet_extract_fn": kg_triplet_extract_fn})

        if storage_context is None:
            from llama_index.graph_stores import SimpleGraphStore
            from llama_index.storage.storage_context import StorageContext

            storage_context = StorageContext.from_defaults(
                graph_store=SimpleGraphStore(), **strorage_context_kwargs
            )

        return BaseIndex._get_index(
            input_=input_,
            llm=llm,
            llm_provider=llm_provider,
            llm_kwargs=llm_kwargs,
            service_context=service_context,
            service_context_kwargs=service_context_kwargs,
            index_type=index_type_,
            index_kwargs=index_kwargs,
            rerank_=rerank_,
            reranker_type=reranker_type,
            reranker=reranker,
            rerank_kwargs=rerank_kwargs,
            get_engine=get_engine,
            engine_kwargs=engine_kwargs,
            from_storage=from_storage,
            storage_context=storage_context,
            strorage_context_kwargs=strorage_context_kwargs,
            index_id=index_id,
            load_index_from_storage_kwargs=load_index_from_storage_kwargs,
        )

    @staticmethod
    def vector_index(
        input_=None,
        # default to OpenAI
        llm=None,
        llm_provider=None,
        llm_kwargs={"temperature": 0.1, "model": "gpt-4-1106-preview"},
        service_context=None,
        service_context_kwargs={},
        index_kwargs={"include_embeddings": True},
        # default to LLMRerank
        rerank_=False,
        reranker_type=None,
        reranker=None,
        rerank_kwargs={"choice_batch_size": 5, "top_n": 3},
        get_engine=False,
        engine_kwargs={"similarity_top_k": 3, "response_mode": "tree_summarize"},
        from_storage=False,
        storage_context=None,
        strorage_context_kwargs={},
        index_id=None,
        load_index_from_storage_kwargs={},
    ):
        """
        Creates and returns a vector index or query engine based on the provided parameters.

        Args:
            chunks: The input data to be indexed or queried.
            llm: An instance of a language model for indexing or querying.
            llm_provider: A function to provide an instance of a language model.
            llm_kwargs: Keyword arguments for configuring the language model.
            service_context: An instance of a service context.
            service_context_kwargs: Keyword arguments for configuring the service context.
            index_kwargs: Keyword arguments for configuring the index.
            rerank_: Boolean flag indicating whether reranking should be applied.
            reranker_type: The type of reranker to use.
            reranker: An instance of a reranker.
            rerank_kwargs: Keyword arguments for configuring the reranker.
            get_engine: Boolean flag indicating whether to return a query engine.
            engine_kwargs: Keyword arguments for configuring the query engine.

        Returns:
            Vector Index or Query Engine: Depending on the 'get_engine' flag,
            returns a vector index or query engine.

        Raises:
            Various exceptions if there are errors in creating the index or query engine.
        """

        return BaseIndex._get_index(
            input_=input_,
            llm=llm,
            llm_provider=llm_provider,
            llm_kwargs=llm_kwargs,
            service_context=service_context,
            service_context_kwargs=service_context_kwargs,
            index_kwargs=index_kwargs,
            rerank_=rerank_,
            reranker_type=reranker_type,
            reranker=reranker,
            rerank_kwargs=rerank_kwargs,
            get_engine=get_engine,
            engine_kwargs=engine_kwargs,
            from_storage=from_storage,
            storage_context=storage_context,
            strorage_context_kwargs=strorage_context_kwargs,
            index_id=index_id,
            load_index_from_storage_kwargs=load_index_from_storage_kwargs,
        )
